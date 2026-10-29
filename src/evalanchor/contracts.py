"""Strict, versioned evidence contracts. Hashes establish identity, not truth."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from . import SCORER_VERSION


class ContractError(ValueError):
    """An evidence artifact is malformed or cannot support the requested comparison."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def loads(text: str):
    def invalid(value):
        raise ContractError(f"Non-finite JSON value: {value}")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ContractError("Non-finite JSON numeric overflow")
        return number
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=invalid, parse_float=finite_float)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ContractError(f"Invalid JSON: {exc}") from exc


def read_json(path: str | Path):
    return loads(Path(path).read_text(encoding="utf-8"))


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def write_json(path: str | Path, obj: Any):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")


def _object(value, name):
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def _sampling(value, name):
    _object(value, name)
    if type(value.get("independent_groups")) is not bool:
        raise ContractError(f"{name}.independent_groups must be explicitly boolean")
    if "kind" in value:
        _text(value["kind"], f"{name}.kind")


def validate_suite(suite: dict) -> dict:
    from .metrics import score_case
    if not isinstance(suite, dict) or type(suite.get("schema_version")) is not int or suite.get("schema_version") != 1:
        raise ContractError("Suite schema_version must be 1")
    _text(suite.get("id"), "suite.id")
    if "sampling" in suite:
        _sampling(suite["sampling"], "suite.sampling")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ContractError("Suite requires non-empty cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ContractError("Every case must be an object")
        for name in ("id", "group_id", "scorer"):
            _text(case.get(name), f"case.{name}")
        if case["id"] in seen:
            raise ContractError(f"Duplicate case ID: {case['id']}")
        seen.add(case["id"])
        if "expected" not in case:
            raise ContractError(f"Missing expected: {case['id']}")
        tags = case.get("tags")
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags) or len(set(tags)) != len(tags):
            raise ContractError("Case tags must be unique non-empty strings")
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ContractError("Case messages must be non-empty")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ContractError("Messages contain only role and content")
            if not isinstance(message["role"], str) or message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str):
                raise ContractError("Invalid message role/content")
        try:
            score_case(case, "")
        except ValueError as exc:
            raise ContractError(f"Invalid scorer configuration for {case['id']}: {exc}") from exc
    canonical(suite)
    return suite


def load_suite(path: str | Path) -> dict:
    return validate_suite(read_json(path))


def case_hash(case: dict) -> str:
    return fingerprint(case)


def suite_hash(suite: dict) -> str:
    return fingerprint(validate_suite(suite))


def protocol(repeats: int = 1) -> dict:
    if type(repeats) is not int or not 1 <= repeats <= 1000:
        raise ContractError("repeats must be an integer in [1,1000]")
    data = {"repeats": repeats, "scorer_version": SCORER_VERSION}
    return {**data, "fingerprint": fingerprint(data)}


def _finite_number(value, name, lower=0, upper=None):
    valid = type(value) in (int, float)
    if valid:
        try:
            valid = math.isfinite(value) and value >= lower and (upper is None or value <= upper)
        except (OverflowError, TypeError, ValueError):
            valid = False
    if not valid:
        raise ContractError(f"Invalid {name}: must be finite and within declared range")


def validate_run(run: dict) -> dict:
    if not isinstance(run, dict) or type(run.get("schema_version")) is not int or run.get("schema_version") != 1:
        raise ContractError("Run schema_version must be 1")
    for name in ("id", "created_at"):
        _text(run.get(name), f"run.{name}")
    if not isinstance(run.get("status"), str) or run.get("status") not in {"complete", "incomplete"}:
        raise ContractError("Invalid run status")
    proto = _object(run.get("protocol", {}), "run.protocol")
    if proto != protocol(proto.get("repeats")):
        raise ContractError("Unknown or mismatched scoring protocol fingerprint")
    suite = _object(run.get("suite", {}), "run.suite")
    _text(suite.get("id"), "run.suite.id")
    _text(suite.get("fingerprint"), "run.suite.fingerprint")
    if type(suite.get("case_count")) is not int or suite["case_count"] < 1:
        raise ContractError("Invalid suite case_count")
    target = _object(run.get("target", {}), "run.target")
    _text(target.get("id"), "run.target.id")
    provenance = _object(run.get("provenance", {}), "run.provenance")
    if "sampling" in provenance:
        _sampling(provenance["sampling"], "run.provenance.sampling")
    records = run.get("records")
    if not isinstance(records, list):
        raise ContractError("Run records must be a list")
    keys = set()
    per_case = {}
    for row in records:
        _object(row, "run record")
        for name in ("case_id", "group_id", "input_hash"):
            _text(row.get(name), name)
        if type(row.get("repeat")) is not int or not 0 <= row["repeat"] < proto["repeats"]:
            raise ContractError("Invalid repeat index")
        key = (row["case_id"], row["repeat"])
        if key in keys:
            raise ContractError(f"Duplicate record: {key}")
        keys.add(key)
        tags = row.get("tags")
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags) or len(tags) != len(set(tags)):
            raise ContractError("Invalid record tags")
        identity = (row["input_hash"], row["group_id"], tuple(sorted(tags)))
        if per_case.setdefault(row["case_id"], identity) != identity:
            raise ContractError("Case identity changed between repeats")
        if not isinstance(row.get("status"), str) or row.get("status") not in {"ok", "error"} or not isinstance(row.get("output"), str):
            raise ContractError("Invalid record output/status")
        _finite_number(row.get("score"), "score", 0, 1)
        _finite_number(row.get("latency_ms", 0), "latency_ms")
        if row.get("cost_usd") is not None:
            _finite_number(row["cost_usd"], "cost_usd")
        if row.get("usage") is not None:
            usage = _object(row["usage"], "record.usage")
            for name in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
                if name in usage and usage[name] is not None:
                    _finite_number(usage[name], f"usage.{name}")
        if row["status"] == "error" and row["score"] != 0:
            raise ContractError("Error records must score zero")
    if run["status"] == "complete":
        expected_count = suite["case_count"] * proto["repeats"]
        if len(records) != expected_count or len(per_case) != suite["case_count"]:
            raise ContractError("Complete run has missing cases or repeats")
    canonical(run)
    return run


def assert_comparable(baseline: dict, candidate: dict):
    for run in (baseline, candidate):
        audit_run(run)
        if run["status"] != "complete":
            raise ContractError("Incomplete runs cannot be compared")
    for name in ("suite", "protocol"):
        if baseline[name] != candidate[name]:
            raise ContractError(f"Mismatched {name}: compare only identical evaluation contracts")
    def grid(run):
        return {(r["case_id"], r["repeat"]): (r["input_hash"], r["group_id"], tuple(sorted(r["tags"]))) for r in run["records"]}
    if grid(baseline) != grid(candidate):
        raise ContractError("Mismatched sample grid, input hash, groups, or tags")


def audit_run(run: dict, suite: dict | None = None) -> dict:
    """Recompute scores from preserved raw output and the bound original suite."""
    from .metrics import score_case
    validate_run(run)
    suite = suite if suite is not None else run.get("suite_definition")
    if suite is None:
        raise ContractError("A verifiable suite_definition is required; import or re-run this evidence")
    validate_suite(suite)
    expected_suite = {"id": suite["id"], "fingerprint": suite_hash(suite), "case_count": len(suite["cases"])}
    if run["suite"] != expected_suite:
        raise ContractError("Suite definition does not match the run fingerprint")
    sampling = suite.get("sampling", {"kind": "unspecified", "independent_groups": False})
    if run.get("provenance", {}).get("sampling", {"kind": "unspecified", "independent_groups": False}) != sampling:
        raise ContractError("Sampling provenance does not match the bound suite declaration")
    cases = {c["id"]: c for c in suite["cases"]}
    for record in run["records"]:
        case = cases.get(record["case_id"])
        if case is None or record["input_hash"] != case_hash(case) or record["group_id"] != case["group_id"] or sorted(record["tags"]) != sorted(case["tags"]):
            raise ContractError("Record identity does not match its original suite case")
        expected = score_case(case, record["output"])["score"] if record["status"] == "ok" else 0.0
        if record["score"] != expected:
            raise ContractError(f"Score tampering or scoring drift: {record['case_id']} repeat {record['repeat']}")
    return {"verified": True, "records": len(run["records"]), "suite_hash": expected_suite["fingerprint"], "note": "Content consistency, not independent proof of model generation."}
