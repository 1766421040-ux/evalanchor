#!/usr/bin/env python3
"""Create content-addressed gzip evidence and a portable public manifest.

Default input is public experiment JSON plus per-experiment/campaign budgets. Optional report paths are
restricted to the v2 workbench HTML allowlist. No response cache, database,
credential directory or arbitrary filesystem input is inspected.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from restore_evidence import (MAX_FILE_BYTES, MAX_MANIFEST_BYTES, MAX_PART_BYTES,
                              PUBLIC_TRANSPORT_FILES, _read_file, _root_fd,
                              safe_source, validate_manifest, RestoreError)


def build_bundle(root=ROOT, *, reports=()):
    root = Path(root).absolute()
    candidates = sorted(p.relative_to(root).as_posix() for p in (root / "experiments").glob("*/experiment.json"))
    candidates += sorted(p.relative_to(root).as_posix() for p in (root / "experiments").glob("*/budget-receipt.json"))
    if (root / "artifacts/orderops-v2/campaign-budget.json").exists():
        candidates.append("artifacts/orderops-v2/campaign-budget.json")
    candidates += sorted(name for name in PUBLIC_TRANSPORT_FILES if (root / name).exists())
    candidates += list(reports)
    candidates.sort()
    if not candidates or len(set(candidates)) != len(candidates):
        raise RestoreError("Need at least one distinct public evidence file")
    root_fd = _root_fd(root)
    rows, objects = [], []
    try:
        for name in candidates:
            relative = safe_source(name)
            raw = _read_file(root_fd, relative.parts, MAX_FILE_BYTES)
            output = io.BytesIO()
            with gzip.GzipFile(filename="", fileobj=output, mode="wb", compresslevel=9, mtime=0) as compressor:
                compressor.write(raw)
            compressed = output.getvalue()
            digest = hashlib.sha256(compressed).hexdigest()
            parts = []
            for index, offset in enumerate(range(0, len(compressed), MAX_PART_BYTES)):
                chunk = compressed[offset:offset + MAX_PART_BYTES]
                encoded = base64.b64encode(chunk)
                encoded_hash = hashlib.sha256(encoded).hexdigest()
                part_path = f"parts/{digest}.{index:04d}.{encoded_hash}.b64"
                parts.append({"path": part_path, "bytes": len(encoded), "sha256": encoded_hash,
                              "decoded_bytes": len(chunk), "decoded_sha256": hashlib.sha256(chunk).hexdigest()})
                objects.append((part_path, encoded))
            rows.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                         "compressed_bytes": len(compressed), "compressed_sha256": digest,
                         "part_count": len(parts), "compressed_parts": parts})
    finally:
        os.close(root_fd)
    manifest = {"schema_version": 2, "release_version": "0.2.0", "codec": "gzip", "part_encoding": "base64", "files": rows,
                "note": "Public evidence only. Bounded base64 parts carry gzip bytes; parts, complete compressed streams and originals are all hashed. Restore with python scripts/restore_evidence.py; no model calls."}
    validate_manifest(manifest)
    serialized = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(serialized) > MAX_MANIFEST_BYTES:
        raise RestoreError("Evidence manifest exceeds the bounded reader allowance")
    output = root / "evidence"
    if output.is_symlink() or (output / "parts").is_symlink():
        raise RestoreError("Evidence output directories must not be symlinks")
    (output / "parts").mkdir(parents=True, exist_ok=True)
    for relative, encoded in objects:
        path = output / relative
        if path.is_symlink():
            raise RestoreError("Compressed part must not be a symlink")
        if path.exists():
            if path.read_bytes() != encoded:
                raise RestoreError("Existing content-addressed part differs")
        else:
            with path.open("xb") as stream:
                stream.write(encoded)
    path = output / "manifest.json"
    if path.is_symlink():
        raise RestoreError("Evidence manifest must not be a symlink")
    temporary = output / ".manifest.tmp"
    with temporary.open("xb") as stream:
        stream.write(serialized)
    os.replace(temporary, path)
    return {"manifest": str(path), "files": len(rows), "original_bytes": sum(r["bytes"] for r in rows),
            "compressed_bytes": sum(r["compressed_bytes"] for r in rows),
            "encoded_bytes": sum(len(data) for _, data in objects), "part_references": len(objects),
            "unique_parts": len({name for name, _ in objects}),
            "max_part_bytes": max(len(data) for _, data in objects),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", action="append", default=[], help="Also compress an allowed v2 workbench HTML report")
    args = parser.parse_args(argv)
    print(json.dumps(build_bundle(args.root, reports=args.report), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Evidence bundle failed: {exc}") from None
