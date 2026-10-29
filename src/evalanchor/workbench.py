"""Local Agent experiment workbench and a self-contained read-only export.

The HTTP surface reads experiment artifacts and writes human annotations only.
It has no provider imports and cannot launch evaluations or model requests.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .metrics import strict_json_loads

MAX_EXPERIMENT_BYTES = 128 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024
REVIEW_STATES = frozenset({"unreviewed", "confirmed", "needs-fix", "resolved"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REVIEW_DB = ".evalanchor-reviews.sqlite3"
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


class WorkbenchError(ValueError):
    """Invalid or unsupported artifact, rather than a model failure."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _fingerprint(experiment: dict) -> str:
    return hashlib.sha256(json.dumps(experiment, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identifier(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and ".." not in value


def _stat_key(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino, stat.st_dev


def _validator_identity():
    source = Path(__file__).parent
    return tuple((name, _stat_key(source / name)) for name in
                 ("workbench.py", "agent_runner.py", "agent_analysis.py", "orderops.py", "agent_api.py", "contracts.py"))


def _verify_experiment(experiment):
    fixture = experiment.get("provenance", {}).get("kind") == "ui_fixture"
    if "benchmark" in experiment or "protocol" in experiment:
        from .agent_runner import audit_experiment
        audit = audit_experiment(experiment)
        if "analysis" in experiment:
            from .agent_analysis import summarize_experiment
            recomputed = summarize_experiment(experiment)
            # Canonical JSON also distinguishes JSON true/1 and integer/float.
            if json.dumps(recomputed, sort_keys=True, allow_nan=False) != json.dumps(experiment["analysis"], sort_keys=True, allow_nan=False):
                raise WorkbenchError("Saved analysis differs from replayed and recomputed analysis")
        return {"state": "verified", "kind": experiment.get("provenance", {}).get("kind", "unknown"),
                "replay": audit, "analysis_verified": "analysis" in experiment,
                "complete": experiment.get("status") == "complete"}
    if fixture and "analysis" not in experiment:
        return {"state": "ui_fixture", "kind": "ui_fixture", "analysis_verified": False,
                "note": "Synthetic UI fixture; shape checks only, no model-performance evidence"}
    raise WorkbenchError("Experiment needs benchmark/protocol replay evidence; only explicit ui_fixture may omit it")


def _read_item(path, directory):
    key = (_stat_key(path), _validator_identity())
    with _CACHE_LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
    raw = path.read_bytes()
    if _stat_key(path) != key[0]:
        raise WorkbenchError("Experiment changed while being read; refresh to read a complete snapshot")
    experiment = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(experiment, dict) or type(experiment.get("schema_version")) is not int or experiment["schema_version"] != 2:
        raise WorkbenchError("Expected an experiment with schema_version=2")
    if not _identifier(experiment.get("id")):
        raise WorkbenchError("Experiment ID must be a safe 1–128 character identifier")
    if not isinstance(experiment.get("records"), list) or not isinstance(experiment.get("configs"), list):
        raise WorkbenchError("Experiment records and configs must be arrays")
    if any(not isinstance(row, dict) for row in experiment["records"] + experiment["configs"]):
        raise WorkbenchError("Experiment records and configs must contain objects")
    item = {"experiment": experiment, "fingerprint": _fingerprint(experiment),
            "source": f"{directory.name}/experiment.json", "content_sha256": hashlib.sha256(raw).hexdigest()}
    try:
        item["verification"] = _verify_experiment(experiment)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        item = {"invalid": True, "id": experiment["id"], "source": item["source"], "error": str(exc)}
    with _CACHE_LOCK:
        if len(_CACHE) >= 128:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[str(path)] = (key, item)
    return item


def _load_catalog(workspace: str | Path):
    """Read only direct, non-hidden children containing experiment.json.

    Malformed artifacts are listed as warnings. Duplicate IDs are all excluded:
    directory ordering must never choose which experiment a review targets.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise WorkbenchError("Workspace must be an existing directory")
    results, warnings, invalid, seen, duplicates = [], [], [], set(), set()
    children = sorted(root.iterdir(), key=lambda p: p.name)
    for directory in children:
        if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
            continue
        path = directory / "experiment.json"
        if not path.exists():
            continue
        try:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
                raise WorkbenchError("Symlink or non-file artifacts are not supported")
            if path.stat().st_size > MAX_EXPERIMENT_BYTES:
                raise WorkbenchError("Experiment exceeds the 128 MiB display limit")
            item = _read_item(path, directory)
            identity = item.get("id") if item.get("invalid") else item["experiment"]["id"]
            if identity in seen:
                duplicates.add(identity)
            seen.add(identity)
            if item.get("invalid"):
                invalid.append(item)
                warnings.append(f"INVALID {identity}: {item['error']}")
            else:
                results.append(item)
        except (ValueError, OSError, UnicodeError, RecursionError) as exc:
            warnings.append(f"{directory.name}/experiment.json: {type(exc).__name__}: {exc}")
    for identity in sorted(duplicates):
        warnings.append(f"Duplicate experiment ID excluded: {identity}")
        invalid.append({"invalid": True, "id": identity, "error": "Duplicate experiment ID"})
    return [item for item in results if item["experiment"]["id"] not in duplicates], warnings, invalid


def load_experiments(workspace: str | Path) -> tuple[list[dict], list[str]]:
    items, warnings, _ = _load_catalog(workspace)
    return items, warnings


def _db_path(workspace: Path) -> Path:
    path = workspace / _REVIEW_DB
    if path.is_symlink():
        raise WorkbenchError("Review database must not be a symlink")
    return path


def _connect(workspace: Path):
    connection = sqlite3.connect(_db_path(workspace), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS reviews (
        experiment_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        case_id TEXT NOT NULL, config_id TEXT NOT NULL, repeat INTEGER NOT NULL,
        state TEXT NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(experiment_id, fingerprint, case_id, config_id, repeat))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS review_history (
        id INTEGER PRIMARY KEY, experiment_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        case_id TEXT NOT NULL, config_id TEXT NOT NULL, repeat INTEGER NOT NULL,
        state TEXT NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    return connection


def _reviews(workspace: Path) -> list[dict]:
    path = _db_path(workspace)
    if not path.exists():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM reviews ORDER BY updated_at, experiment_id, case_id")]
        except sqlite3.OperationalError as exc:
            raise WorkbenchError("Review database is not a valid EvalAnchor annotation store") from exc


def dashboard_payload(workspace: str | Path, *, mode="export", token=None) -> dict:
    root = Path(workspace).resolve()
    experiments, warnings, invalid = _load_catalog(root)
    return {"mode": mode, "experiments": experiments, "warnings": warnings,
            "invalid_experiments": invalid, "reviews": _reviews(root), "generated_at": _now(), "review_token": token}


def _html(payload: dict) -> str:
    serialized = _dump(payload).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (Path(__file__).parent / "templates" / "workbench.html").read_text(encoding="utf-8")
    return template.replace("__EVALANCHOR_WORKBENCH_DATA__", serialized)


def export_dashboard(workspace: str | Path, destination: str | Path) -> Path:
    """Export embedded evidence and current annotations without a write API."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_html(dashboard_payload(workspace)), encoding="utf-8")
    return path


def _save_review(workspace: Path, body: dict) -> dict:
    required = {"experiment_id", "fingerprint", "case_id", "config_id", "repeat", "state", "note"}
    if not isinstance(body, dict) or set(body) != required:
        raise WorkbenchError("Review must contain exactly the documented identity, state and note fields")
    if not _identifier(body["experiment_id"]):
        raise WorkbenchError("Invalid experiment ID")
    for key in ("case_id", "config_id"):
        if not isinstance(body[key], str) or not 1 <= len(body[key]) <= 256:
            raise WorkbenchError(f"Invalid {key}")
    if type(body["repeat"]) is not int or not 0 <= body["repeat"] <= 100000:
        raise WorkbenchError("Invalid repeat")
    if not isinstance(body["state"], str) or body["state"] not in REVIEW_STATES:
        raise WorkbenchError("Unknown review state")
    if not isinstance(body["note"], str) or len(body["note"]) > 4000:
        raise WorkbenchError("Review note must be at most 4000 characters")
    experiments, _ = load_experiments(workspace)
    item = next((item for item in experiments if item["experiment"]["id"] == body["experiment_id"]), None)
    if item is None:
        raise WorkbenchError("Experiment is missing or ambiguous")
    if body["fingerprint"] != item["fingerprint"]:
        raise WorkbenchError("Experiment changed; refresh before annotating")
    matches = [record for record in item["experiment"]["records"]
               if (record.get("case_id"), record.get("config_id"), record.get("repeat", 0)) ==
               (body["case_id"], body["config_id"], body["repeat"])]
    if len(matches) != 1:
        raise WorkbenchError("Review must target one existing, unambiguous record")
    review = {**body, "updated_at": _now()}
    values = tuple(review[key] for key in ("experiment_id", "fingerprint", "case_id", "config_id", "repeat", "state", "note", "updated_at"))
    connection = _connect(workspace)
    try:
        with connection:
            connection.execute("""INSERT INTO review_history
                (experiment_id, fingerprint, case_id, config_id, repeat, state, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", values)
            connection.execute("""INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id,fingerprint,case_id,config_id,repeat)
                DO UPDATE SET state=excluded.state, note=excluded.note, updated_at=excluded.updated_at""", values)
    finally:
        connection.close()
    return review


def create_workbench_server(workspace: str | Path, port=8780) -> ThreadingHTTPServer:
    """Create a loopback-only server (port=0 is useful for local tests)."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise WorkbenchError("Workspace must be an existing directory")
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EvalAnchorWorkbench/2"

        def log_message(self, format, *args):
            # Artifact contents and review notes should never enter access logs.
            return

        def _hosts(self):
            bound = self.server.server_address[1]
            return {f"127.0.0.1:{bound}", f"localhost:{bound}"}

        def _host_valid(self):
            return self.headers.get("Host", "").lower() in self._hosts()

        def _send(self, code, value, content_type="application/json; charset=utf-8"):
            data = value.encode("utf-8") if isinstance(value, str) else _dump(value).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _route(self):
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise WorkbenchError("Unsupported URL")
            route = unquote(parsed.path)
            if "\\" in route or ".." in route or "\x00" in route:
                raise WorkbenchError("Invalid path")
            return route

        def do_GET(self):
            if not self._host_valid():
                return self._send(403, {"error": "Loopback Host header required"})
            try:
                route = self._route()
                if route == "/":
                    return self._send(200, _html(dashboard_payload(root, mode="live", token=token)), "text/html; charset=utf-8")
                if route == "/api/experiments":
                    payload = dashboard_payload(root, mode="live")
                    # Listing contains light metadata; details are a separate API.
                    payload["experiments"] = [{"id": item["experiment"]["id"], "fingerprint": item["fingerprint"],
                        "status": item["experiment"].get("status"), "benchmark_hash": item["experiment"].get("benchmark_hash"),
                        "configs": item["experiment"]["configs"], "record_count": len(item["experiment"]["records"]),
                        "source": item["source"], "verification": item["verification"]} for item in payload["experiments"]]
                    payload.pop("review_token", None)
                    return self._send(200, payload)
                prefix = "/api/experiments/"
                if route.startswith(prefix) and _identifier(route[len(prefix):]):
                    identity = route[len(prefix):]
                    items, _, invalid = _load_catalog(root)
                    item = next((item for item in items if item["experiment"]["id"] == identity), None)
                    if item:
                        return self._send(200, {**item, "reviews": [review for review in _reviews(root) if review["experiment_id"] == identity]})
                    rejected = next((item for item in invalid if item["id"] == identity), None)
                    if rejected:
                        return self._send(422, {"status": "invalid", **rejected})
                return self._send(404, {"error": "Not found"})
            except (ValueError, OSError, sqlite3.Error) as exc:
                return self._send(400, {"error": str(exc)})

        def do_POST(self):
            if not self._host_valid():
                return self._send(403, {"error": "Loopback Host header required"})
            if self.headers.get("Origin") not in {f"http://{host}" for host in self._hosts()}:
                return self._send(403, {"error": "Same-port loopback Origin required"})
            supplied_token = self.headers.get("X-EvalAnchor-Token", "")
            if not supplied_token.isascii() or not secrets.compare_digest(supplied_token, token):
                return self._send(403, {"error": "Review token required; refresh the workbench page"})
            try:
                if self._route() != "/api/reviews":
                    return self._send(404, {"error": "Not found"})
                if self.headers.get("Transfer-Encoding"):
                    return self._send(400, {"error": "Chunked requests are not supported"})
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self._send(415, {"error": "application/json required"})
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    return self._send(413, {"error": "Review JSON must be 1–16384 bytes"})
                self.connection.settimeout(5)
                body = strict_json_loads(self.rfile.read(length).decode("utf-8"))
                return self._send(200, {"review": _save_review(root, body)})
            except (ValueError, OSError, sqlite3.Error) as exc:
                return self._send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve_workbench(workspace: str | Path, port=8780):
    server = create_workbench_server(workspace, port)
    print(f"EvalAnchor workbench: http://127.0.0.1:{server.server_address[1]} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
