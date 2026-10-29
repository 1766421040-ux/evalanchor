#!/usr/bin/env python3
"""Create content-addressed gzip evidence and a portable public manifest.

Default input is public experiment JSON plus per-experiment/campaign budgets. Optional report paths are
restricted to the v2 workbench HTML allowlist. No response cache, database,
credential directory or arbitrary filesystem input is inspected.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from restore_evidence import MAX_FILE_BYTES, _read_file, _root_fd, safe_source, validate_manifest, RestoreError


def build_bundle(root=ROOT, *, reports=()):
    root = Path(root).absolute()
    candidates = sorted(p.relative_to(root).as_posix() for p in (root / "experiments").glob("*/experiment.json"))
    candidates += sorted(p.relative_to(root).as_posix() for p in (root / "experiments").glob("*/budget-receipt.json"))
    if (root / "artifacts/orderops-v2/campaign-budget.json").exists():
        candidates.append("artifacts/orderops-v2/campaign-budget.json")
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
            rows.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                         "compressed_path": f"objects/{digest}.gz", "compressed_bytes": len(compressed), "compressed_sha256": digest})
            objects.append(compressed)
    finally:
        os.close(root_fd)
    manifest = {"schema_version": 1, "release_version": "0.2.0", "codec": "gzip", "files": rows,
                "note": "Only public experiment/report evidence. Restore with python scripts/restore_evidence.py before offline verification; model calls are never rerun."}
    validate_manifest(manifest)
    output = root / "evidence"
    if output.is_symlink() or (output / "objects").is_symlink():
        raise RestoreError("Evidence output directories must not be symlinks")
    (output / "objects").mkdir(parents=True, exist_ok=True)
    for row, compressed in zip(rows, objects):
        path = output / row["compressed_path"]
        if path.is_symlink():
            raise RestoreError("Compressed object must not be a symlink")
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["compressed_sha256"]:
                raise RestoreError("Existing content-addressed object differs")
        else:
            with path.open("xb") as stream:
                stream.write(compressed)
    path = output / "manifest.json"
    if path.is_symlink():
        raise RestoreError("Evidence manifest must not be a symlink")
    temporary = output / ".manifest.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)
    return {"manifest": str(path), "files": len(rows), "original_bytes": sum(r["bytes"] for r in rows),
            "compressed_bytes": sum(r["compressed_bytes"] for r in rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


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
