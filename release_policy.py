"""Shared filename-only policy for every distributable format.

No excluded file needs to be opened. The policy cannot detect arbitrary secrets
hidden under innocent filenames; review public source/evidence before release.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import tomllib

ROOT_FILES = frozenset({
    "README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md",
    "Makefile", "pyproject.toml", "MANIFEST.in", "requirements-tested.txt",
    ".gitignore", ".env.example", "setup.py", "release_policy.py",
})
DIRECTORIES = frozenset({"src", "tests", "scripts", "docs", "research", "examples", "artifacts", "experiments", "evidence", ".github"})
EXPERIMENT_FILES = frozenset({"experiment.json", "analysis.json", "budget-receipt.json", "failed-requests.json", "index.html", "dashboard.html"})
EXCLUDED_COMPONENTS = frozenset({
    "__pycache__", "node_modules", "site-packages", "dist-packages", "venv",
    "secrets", "secret", "credentials", "credential", "keys", "key",
    "api_keys", "api-keys", "token", "tokens", "api_tokens", "api-tokens", "vault", "keychain", "密钥", "凭据", "calls",
    "dist", "build",
})
EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".lock", ".pem", ".key", ".p12", ".pfx", ".jks",
    ".keystore", ".kdbx", ".der", ".crt", ".safetensors", ".bin", ".pt",
    ".pth", ".onnx", ".gguf", ".ckpt", ".h5", ".dylib", ".so",
    ".zip", ".whl", ".tar", ".gz", ".bz2", ".xz", ".7z",
})


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



def release_version(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parent
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?", version):
        raise ValueError("Invalid release version in pyproject.toml")
    return version


def allowed_source(relative: str | PurePosixPath, *, metadata: bool = False) -> bool:
    """Allow regular release sources plus setuptools' explicit generated metadata."""
    value = str(relative)
    if not value or "\\" in value or "\x00" in value or ":" in value:
        return False
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    path = PurePosixPath(value)
    if metadata and value in {"PKG-INFO", "setup.cfg"}:
        return True
    if path.is_absolute() or (parts[0] not in DIRECTORIES and value not in ROOT_FILES):
        return False
    return not excluded(path)


def require_regular_source(relative: str) -> None:
    """Build backends must not follow a file or parent directory symlink."""
    path = Path(relative)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"Symlink refused in distributable source: {relative}")
    if not path.is_file():
        raise ValueError(f"Non-regular distributable source: {relative}")
