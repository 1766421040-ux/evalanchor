#!/usr/bin/env python3
"""Safely restore the public, compressed evidence shipped by the Git repository.

Only explicitly allowed experiment/report paths may be restored. Hashes cover
both compressed and original bytes. Different existing files, traversal,
symlinks, oversized payloads and truncated/concatenated output are refused.
No network, credentials, SQLite or model calls are involved. macOS/Linux only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from evalanchor.metrics import strict_json_loads

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILES = 128
MAX_MANIFEST_BYTES = 1024 * 1024
CHUNK = 1024 * 1024


class RestoreError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RestoreError(message)


def safe_source(value):
    require(isinstance(value, str) and "\\" not in value and "\x00" not in value, "Invalid source path")
    parts = value.split("/")
    require(all(p and p not in (".", "..") for p in parts), "Traversal/empty path component refused")
    allowed = (len(parts) == 3 and parts[0] == "experiments" and
               bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", parts[1])) and parts[2] in ("experiment.json", "budget-receipt.json"))
    allowed |= value in ("artifacts/orderops-v2/index.html", "artifacts/orderops-v2/dashboard.html", "artifacts/orderops-v2/campaign-budget.json")
    require(allowed, "Evidence source is outside the explicit public path allowlist")
    return PurePosixPath(value)


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1 and manifest.get("codec") == "gzip", "Unsupported evidence manifest")
    rows = manifest.get("files")
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_FILES, "Invalid evidence file count")
    paths, objects, total = set(), set(), 0
    for row in rows:
        require(isinstance(row, dict), "Evidence manifest entry must be an object")
        safe_source(row.get("path"))
        require(row["path"] not in paths, "Duplicate evidence destination")
        paths.add(row["path"])
        for field in ("sha256", "compressed_sha256"):
            require(isinstance(row.get(field), str) and bool(re.fullmatch(r"[0-9a-f]{64}", row[field])), "Invalid SHA256")
        require(row.get("compressed_path") == f"objects/{row['compressed_sha256']}.gz", "Compressed object name must equal its checksum")
        for field in ("bytes", "compressed_bytes"):
            require(type(row.get(field)) is int and 0 < row[field] <= MAX_FILE_BYTES, "Evidence size exceeds allowed bounds")
        total += row["bytes"]
    require(total <= MAX_TOTAL_BYTES, "Total restored evidence exceeds 1 GiB")
    return rows


def _root_fd(root):
    require(hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd, "Secure restoration requires POSIX descriptor-relative operations (macOS/Linux)")
    root = Path(root).absolute()
    require(root.is_dir() and not root.is_symlink(), "Project root must be a non-symlink directory")
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _parent_fd(root_fd, parts, create=False):
    current = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_file(root_fd, parts, limit):
    parent = _parent_fd(root_fd, parts[:-1])
    try:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= limit, "Expected a bounded regular evidence file")
            raw = stream.read(limit + 1)
            require(len(raw) <= limit, "Evidence file grew beyond its size allowance")
            return raw
    finally:
        os.close(parent)


def _check_existing(parent, name, row):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_size == row["bytes"], "Existing evidence differs; refusing to overwrite")
        digest = hashlib.sha256()
        for data in iter(lambda: stream.read(CHUNK), b""):
            digest.update(data)
        require(digest.hexdigest() == row["sha256"], "Existing evidence differs; refusing to overwrite")
    return True


def restore(root=ROOT, *, verify_only=False):
    root_fd = _root_fd(root)
    restored, existing = [], []
    try:
        manifest = strict_json_loads(_read_file(root_fd, ("evidence", "manifest.json"), MAX_MANIFEST_BYTES).decode("utf-8"))
        rows = validate_manifest(manifest)
        for row in rows:
            destination = safe_source(row["path"])
            parent = _parent_fd(root_fd, destination.parts[:-1], create=not verify_only)
            temporary = None
            try:
                if _check_existing(parent, destination.name, row):
                    existing.append(row["path"])
                    continue
                require(not verify_only, f"Restored evidence is missing: {row['path']}")
                compressed_parts = ("evidence", *PurePosixPath(row["compressed_path"]).parts)
                object_parent = _parent_fd(root_fd, compressed_parts[:-1])
                try:
                    descriptor = os.open(compressed_parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=object_parent)
                finally:
                    os.close(object_parent)
                with os.fdopen(descriptor, "rb") as source:
                    metadata = os.fstat(source.fileno())
                    require(stat.S_ISREG(metadata.st_mode) and metadata.st_size == row["compressed_bytes"], "Compressed evidence size mismatch")
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        digest.update(chunk)
                    require(digest.hexdigest() == row["compressed_sha256"], "Compressed evidence checksum mismatch")
                    source.seek(0)
                    temporary = ".restore-" + secrets.token_hex(16)
                    output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
                    digest, size = hashlib.sha256(), 0
                    with os.fdopen(output_fd, "wb") as output, gzip.GzipFile(fileobj=source, mode="rb") as decoder:
                        while chunk := decoder.read(min(CHUNK, row["bytes"] - size + 1)):
                            size += len(chunk)
                            require(size <= row["bytes"], "Decompressed evidence exceeds declared size")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    require(size == row["bytes"] and digest.hexdigest() == row["sha256"], "Decompressed evidence size/checksum mismatch")
                    # Exclusive publication under the same held parent directory.
                    os.link(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=parent)
                    temporary = None
                restored.append(row["path"])
            finally:
                if temporary:
                    os.unlink(temporary, dir_fd=parent)
                os.close(parent)
        return {"verified": True, "mode": "verify_existing" if verify_only else "restore", "restored": restored,
                "already_verified": existing, "files": len(rows), "original_bytes": sum(r["bytes"] for r in rows),
                "note": "Public artifact bytes only. SHA256 verifies manifest consistency, not authorship or model-generation authenticity."}
    finally:
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--verify-only", action="store_true", help="Verify restored files without creating or changing any file")
    args = parser.parse_args(argv)
    print(json.dumps(restore(args.root, verify_only=args.verify_only), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, EOFError) as exc:
        raise SystemExit(f"Evidence restoration failed: {exc}") from None
