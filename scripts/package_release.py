#!/usr/bin/env python3
"""Build a deterministic source/evidence ZIP from an explicit project allowlist.

The packager never follows symlinks or reads excluded environment/key files.
It requires POSIX descriptor-relative file operations (macOS/Linux).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVE_PREFIX = "evalanchor-0.2.0"
ROOT_FILES = frozenset({
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md",
    "Makefile", "pyproject.toml", "MANIFEST.in", "requirements-tested.txt",
    ".gitignore", ".env.example",
})
DIRECTORIES = frozenset({"src", "tests", "scripts", "docs", "research", "examples", "artifacts", "experiments", "evidence", ".github"})
EXPERIMENT_FILES = frozenset({"experiment.json", "analysis.json", "budget-receipt.json", "failed-requests.json", "index.html", "dashboard.html"})
REQUIRED_EVIDENCE = frozenset({
    "src/evalanchor/__init__.py",
    "examples/integrations/verified/receipt.json",
    "examples/integrations/verified/inspect/verified.eval",
    "examples/integrations/verified/inspect/imported-run.json",
    "examples/integrations/verified/promptfoo/results.json",
    "examples/integrations/verified/promptfoo/imported-run.json",
    "artifacts/local-qwen-final/run.json",
    "artifacts/local-qwen-final/suite.json",
    "artifacts/local-qwen-final/target.json",
    "artifacts/demo/comparison-candidate.json",
    "artifacts/demo/comparison-fixed.json",
    "artifacts/ui-verification.json",
    "artifacts/openai-preflight.json",
    "artifacts/openai-luna-smoke/run.json",
    "artifacts/openai-luna-smoke/smoke-summary.json",
    "artifacts/openai-luna-smoke/comparison-qwen-luna.json",
    "artifacts/openai-luna-smoke/index.html",
    "docs/figures/overview.jpg",
    "docs/figures/orderops-review.png",
    "docs/figures/orderops-results.png",
    "docs/figures/orderops-results.pdf",
    "docs/figures/orderops-results.metadata.json",
    "src/evalanchor/workbench.py",
    "src/evalanchor/templates/workbench.html",
    "scripts/verify_v2.py",
    "scripts/restore_evidence.py",
    "scripts/build_evidence_bundle.py",
    "scripts/check_horizon_budget.py",
    "scripts/summarize_orderops_results.py",
    "scripts/plot_orderops_results.py",
    "evidence/manifest.json",
    "docs/product-goal.md",
    "docs/orderops-data-card.md",
    "docs/v2-experiment-design.md",
    "docs/reproducibility.md",
    "docs/orderops-results.md",
    "examples/orderops/benchmark.json",
    "examples/orderops/configs.json",
    "artifacts/orderops-v2/campaign-budget.json",
    "artifacts/orderops-v2/frozen-protocol.json",
    "artifacts/orderops-v2/horizon-comparison.json",
    "artifacts/orderops-v2/pricing-verification.json",
    "artifacts/orderops-v2/horizon-start-check.json",
    "artifacts/orderops-v2/preregistration-amendment-resource-horizon.json",
    "artifacts/orderops-v2/results-summary.json",
    "artifacts/orderops-v2/failure-details.json",
    "artifacts/orderops-v2/failure-details.csv",
    *{f"experiments/{identity}/{name}" for identity in ("pilot-v2", "dev-v2", "dev-terminal-v2", "test-v2", "test-long-v2")
      for name in ("experiment.json", "analysis.json", "budget-receipt.json")},
})
EXCLUDED_COMPONENTS = frozenset({
    "__pycache__", "node_modules", "site-packages", "dist-packages", "venv",
    "secrets", "secret", "credentials", "credential", "keys", "key",
    "api_keys", "api-keys", "vault", "keychain", "密钥", "凭据", "calls",
    "dist", "build",
})
EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".lock", ".pem", ".key", ".p12", ".pfx", ".jks",
    ".keystore", ".kdbx", ".der", ".crt", ".safetensors", ".bin", ".pt",
    ".pth", ".onnx", ".gguf", ".ckpt", ".h5", ".dylib", ".so",
    ".zip", ".whl", ".tar", ".gz", ".bz2", ".xz", ".7z",
})
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class PackagingError(ValueError):
    pass


def archive_name(relative: str | PurePosixPath) -> str:
    """Return a portable ZIP member name, rejecting traversal and drive syntax."""
    value = str(relative)
    parts = value.split("/")
    if not value or "\\" in value or "\x00" in value or ":" in value or any(p in {"", ".", ".."} for p in parts):
        raise PackagingError("Unsafe archive member path")
    if PurePosixPath(value).is_absolute():
        raise PackagingError("Archive member must be relative")
    return f"{ARCHIVE_PREFIX}/{value}"


def excluded(relative: PurePosixPath) -> bool:
    """Filename-only filtering: excluded secrets are never opened to inspect."""
    if len(relative.parts) == 1 and relative.name in ROOT_FILES:
        return False
    if len(relative.parts) >= 2 and relative.parts[0] == "evidence" and relative.parts[1] in {"objects", "parts"}:
        return True
    if relative.parts and relative.parts[0] == "experiments":
        if len(relative.parts) > 3 or (len(relative.parts) == 3 and relative.name not in EXPERIMENT_FILES):
            return True
    for index, component in enumerate(relative.parts):
        lowered = component.casefold()
        if component.startswith(".") and not (index == 0 and component == ".github"):
            return True
        if lowered in EXCLUDED_COMPONENTS:
            return True
        if "secret" in lowered or "credential" in lowered or lowered in {"auth.json", "token.json", "tokens.json", "key.json", "keys.json", "id_rsa", "id_ed25519"}:
            return True
        if re.search(r"(?:^|[_.-])(?:api[-_]?key|access[-_]?token|refresh[-_]?token|private[-_]?key)(?:[_.-]|$)", lowered):
            return True
        if lowered.endswith(".env") or lowered.startswith("trace.log"):
            return True
    name = relative.name.casefold()
    return (
        PurePosixPath(name).suffix in EXCLUDED_SUFFIXES
        or bool(re.search(r"\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$", name))
    )


def _zip_info(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name(relative), date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if relative.endswith(".sh") else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _walk_archive(archive: zipfile.ZipFile, directory_fd: int, prefix: PurePosixPath, records: list[dict]) -> None:
    # Every directory and file is opened relative to an already held descriptor.
    # O_NOFOLLOW prevents a race from turning an allowed path into a symlink to
    # an excluded credential file between directory listing and opening it.
    with os.scandir(directory_fd) as scan:
        names = sorted(entry.name for entry in scan)
    for name in names:
        relative = prefix / name
        if not prefix.parts and name not in ROOT_FILES and name not in DIRECTORIES:
            continue
        if excluded(relative):
            continue
        archive_name(relative)
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise PackagingError(f"Symlink refused: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if not prefix.parts and name not in DIRECTORIES:
                raise PackagingError(f"Expected a root file: {relative}")
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                _walk_archive(archive, child_fd, relative, records)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            if relative.parts[0] == "experiments" and len(relative.parts) != 3:
                continue
            if not prefix.parts and name not in ROOT_FILES:
                raise PackagingError(f"Expected an allowed directory: {relative}")
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise PackagingError(f"Non-regular file refused: {relative}")
                digest = hashlib.sha256()
                size = 0
                with archive.open(_zip_info(str(relative)), mode="w") as output:
                    while chunk := os.read(file_fd, 1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                after = os.fstat(file_fd)
                if size != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns:
                    raise PackagingError(f"File changed during packaging: {relative}")
                records.append({"path": str(relative), "bytes": size, "sha256": digest.hexdigest()})
            finally:
                os.close(file_fd)
        else:
            raise PackagingError(f"Special file refused: {relative}")


def build_release(root: str | Path, destination: str | Path | None = None, *, required_evidence=REQUIRED_EVIDENCE) -> dict:
    """Package one source tree, verify mandatory evidence, atomically publish ZIP.

    The explicit ``required_evidence`` hook supports small fixture trees in tests;
    the CLI always enforces the real deliverable set. Existing ZIPs are refused.
    """
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise PackagingError("Project root must be an existing non-symlink directory")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise PackagingError("Safe packaging requires POSIX descriptor-relative file operations")
    dist = root / "dist"
    if dist.is_symlink():
        raise PackagingError("Release dist directory must not be a symlink")
    dist.mkdir(exist_ok=True)
    target = Path(destination).absolute() if destination is not None else dist / f"{ARCHIVE_PREFIX}.zip"
    if target.parent.resolve() != dist.resolve() or target.name != Path(target.name).name or target.suffix != ".zip":
        raise PackagingError("Release destination must be a ZIP directly inside project dist/")
    if target.exists() or target.is_symlink():
        raise PackagingError("Release destination already exists; use a new ZIP filename")
    required = set(ROOT_FILES) | {str(p) for p in required_evidence}
    for name in required:
        archive_name(name)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temp_fd, temp_name = tempfile.mkstemp(prefix=".release-", suffix=".tmp", dir=dist)
    os.close(temp_fd)
    temp_path = Path(temp_name)
    records: list[dict] = []
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as archive:
            _walk_archive(archive, root_fd, PurePosixPath(), records)
            missing = sorted(required - {record["path"] for record in records})
            if missing:
                raise PackagingError("Required source/evidence missing: " + ", ".join(missing))
            manifest = {"schema_version": 1, "archive_prefix": ARCHIVE_PREFIX, "files": sorted(records, key=lambda r: r["path"]), "note": "SHA256 binds included bytes; no claim of model-generation authenticity. v1 evidence and v2 raw experiment/analysis/budget exports are retained. Environment files, keys, dependencies, model weights, redundant calls caches, databases and nested release archives are excluded."}
            archive.writestr(_zip_info("RELEASE_MANIFEST.json"), json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        # Publish with exclusive creation. Never overwrite an existing release.
        os.link(temp_path, target)
        temp_path.unlink()
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(target), "file_count": len(records) + 1, "source_evidence_file_count": len(records), "uncompressed_bytes": sum(r["bytes"] for r in records), "zip_bytes": target.stat().st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(root_fd)
        if temp_path.exists():
            temp_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--name", default=f"{ARCHIVE_PREFIX}.zip", help="ZIP filename inside project dist/")
    args = parser.parse_args(argv)
    if args.name != Path(args.name).name or "/" in args.name or "\\" in args.name:
        raise PackagingError("--name must be a plain ZIP filename")
    result = build_release(args.root, args.root / "dist" / args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackagingError, OSError) as exc:
        raise SystemExit(str(exc)) from None
