"""Portable offline report. Raw model output is always rendered as text."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def write_report(destination: str | Path, *, suite: dict | None, runs: list[dict], comparisons: list[dict], judge_audit: dict | None = None) -> Path:
    payload = {"suite": suite, "runs": runs, "comparisons": comparisons, "judge_audit": judge_audit, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (Path(__file__).parent / "templates" / "report.html").read_text(encoding="utf-8")
    html = template.replace("__EVALANCHOR_DATA__", serialized)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
