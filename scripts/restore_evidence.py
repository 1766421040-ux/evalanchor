#!/usr/bin/env python3
"""Safely restore the public, compressed evidence shipped by the Git repository.

Only explicitly allowed experiment/report paths may be restored. Hashes cover
both compressed and original bytes. Different existing files, traversal,
symlinks, oversized payloads and truncated/concatenated output are refused.
No network, credentials, SQLite or model calls are involved. macOS/Linux only.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from evalanchor.metrics import strict_json_loads

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILES = 128
MAX_MANIFEST_BYTES = 1024 * 1024
CHUNK = 1024 * 1024
MAX_PART_BYTES = 128 * 1024
MAX_ENCODED_PART_BYTES = 4 * ((MAX_PART_BYTES + 2) // 3)
PUBLIC_TRANSPORT_FILES = frozenset({
    "artifacts/demo/index.html",
    "artifacts/orderops-v2/horizon-comparison.json",
    "artifacts/orderops-v2/results-summary.json",
    "examples/integrations/verified/inspect/logs/2026-10-29T13-36-54-00-00_task_Ar8s4H6rxqXoDBN5bK9paQ.json",
    "examples/integrations/verified/promptfoo/results.json",
})


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
    allowed |= value in PUBLIC_TRANSPORT_FILES
    require(allowed, "Evidence source is outside the explicit public path allowlist")
    return PurePosixPath(value)


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and type(manifest.get("schema_version")) is int and
            manifest["schema_version"] in (1, 2) and manifest.get("codec") == "gzip", "Unsupported evidence manifest")
    version = manifest["schema_version"]
    require(version != 2 or manifest.get("part_encoding") == "base64", "Unsupported part encoding")
    rows = manifest.get("files")
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_FILES, "Invalid evidence file count")
    paths, total, compressed_total = set(), 0, 0
    for row in rows:
        require(isinstance(row, dict), "Evidence manifest entry must be an object")
        safe_source(row.get("path"))
        require(row["path"] not in paths, "Duplicate evidence destination")
        paths.add(row["path"])
        for field in ("sha256", "compressed_sha256"):
            require(isinstance(row.get(field), str) and bool(re.fullmatch(r"[0-9a-f]{64}", row[field])), "Invalid SHA256")
        for field in ("bytes", "compressed_bytes"):
            require(type(row.get(field)) is int and 0 < row[field] <= MAX_FILE_BYTES, "Evidence size exceeds allowed bounds")
        if version == 1:
            require("compressed_parts" not in row and "part_count" not in row, "Ambiguous compressed object representation")
            require(row.get("compressed_path") == f"objects/{row['compressed_sha256']}.gz", "Compressed object name must equal its checksum")
        else:
            require("compressed_path" not in row, "Ambiguous compressed object representation")
            count = (row["compressed_bytes"] + MAX_PART_BYTES - 1) // MAX_PART_BYTES
            require(type(row.get("part_count")) is int and row["part_count"] == count, "Compressed part count mismatch")
            parts = row.get("compressed_parts")
            require(isinstance(parts, list) and len(parts) == count, "Compressed part list is incomplete")
            decoded_total = 0
            for index, part in enumerate(parts):
                require(isinstance(part, dict) and set(part) == {"path", "bytes", "sha256", "decoded_bytes", "decoded_sha256"}, "Invalid compressed part fields")
                for field in ("sha256", "decoded_sha256"):
                    require(isinstance(part.get(field), str) and bool(re.fullmatch(r"[0-9a-f]{64}", part[field])), "Invalid part SHA256")
                expected_size = min(MAX_PART_BYTES, row["compressed_bytes"] - index * MAX_PART_BYTES)
                require(type(part.get("decoded_bytes")) is int and part["decoded_bytes"] == expected_size, "Decoded part size/count mismatch")
                encoded_size = 4 * ((expected_size + 2) // 3)
                require(type(part.get("bytes")) is int and part["bytes"] == encoded_size <= MAX_ENCODED_PART_BYTES, "Encoded part size mismatch")
                require(part.get("path") == f"parts/{row['compressed_sha256']}.{index:04d}.{part['sha256']}.b64", "Part path/order must equal its checksums")
                decoded_total += part["decoded_bytes"]
            require(decoded_total == row["compressed_bytes"], "Compressed part byte sum mismatch")
        total += row["bytes"]
        compressed_total += row["compressed_bytes"]
    require(total <= MAX_TOTAL_BYTES, "Total restored evidence exceeds 1 GiB")
    require(compressed_total <= MAX_TOTAL_BYTES, "Total compressed evidence exceeds 1 GiB")
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


@contextmanager
def _compressed_source(root_fd, row):
    """Verify each bounded ASCII part, then the reconstructed gzip stream."""
    if "compressed_parts" in row:
        with tempfile.TemporaryFile(mode="w+b") as source:
            digest, size = hashlib.sha256(), 0
            for part in row["compressed_parts"]:
                encoded = _read_file(root_fd, ("evidence", *PurePosixPath(part["path"]).parts), MAX_ENCODED_PART_BYTES)
                require(len(encoded) == part["bytes"] and hashlib.sha256(encoded).hexdigest() == part["sha256"], "Encoded part size/checksum mismatch")
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except binascii.Error as exc:
                    raise RestoreError("Invalid strict base64 part") from exc
                require(base64.b64encode(decoded) == encoded, "Noncanonical base64 part")
                require(len(decoded) == part["decoded_bytes"] and hashlib.sha256(decoded).hexdigest() == part["decoded_sha256"], "Decoded part size/checksum mismatch")
                digest.update(decoded)
                size += len(decoded)
                source.write(decoded)
            require(size == row["compressed_bytes"] and digest.hexdigest() == row["compressed_sha256"], "Compressed evidence size/checksum mismatch")
            source.seek(0)
            yield source
        return
    # Legacy schema 1 remains readable; new bundles always use bounded ASCII parts.
    parts = ("evidence", *PurePosixPath(row["compressed_path"]).parts)
    parent = _parent_fd(root_fd, parts[:-1])
    try:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_size == row["compressed_bytes"], "Compressed evidence size mismatch")
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
        require(digest.hexdigest() == row["compressed_sha256"], "Compressed evidence checksum mismatch")
        source.seek(0)
        yield source


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
                with _compressed_source(root_fd, row) as source:
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
