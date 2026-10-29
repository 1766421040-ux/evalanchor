"""Malformed nested JSON must be invalid evidence (exit 3), never regression."""
from copy import deepcopy
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from evalanchor import cli
from evalanchor.contracts import ContractError, audit_run, case_hash, protocol, suite_hash, validate_suite


def suite():
    return {"schema_version": 1, "id": "nested-types", "cases": [{"id": "c", "group_id": "g", "tags": [], "messages": [{"role": "user", "content": "q"}], "scorer": "exact", "expected": "a"}]}


def run():
    definition = suite()
    case = definition["cases"][0]
    return {"schema_version": 1, "id": "r", "created_at": "2026-10-29T00:00:00Z", "status": "complete",
            "protocol": protocol(1), "suite": {"id": definition["id"], "fingerprint": suite_hash(definition), "case_count": 1},
            "suite_definition": definition, "target": {"id": "fixture"}, "provenance": {},
            "records": [{"case_id": "c", "group_id": "g", "repeat": 0, "tags": [], "input_hash": case_hash(case),
                         "status": "ok", "output": "a", "score": 1, "latency_ms": 0, "cost_usd": None, "usage": {}}]}


@pytest.mark.parametrize("field", ["protocol", "suite", "target", "provenance"])
@pytest.mark.parametrize("value", [None, [], "invalid", 7])
def test_nested_run_objects_reject_wrong_types(field, value):
    data = run()
    data[field] = value
    with pytest.raises(ContractError, match=field):
        audit_run(data)


@pytest.mark.parametrize("value", [None, [], "row", 3])
def test_records_must_be_objects(value):
    data = run()
    data["records"] = [value]
    with pytest.raises(ContractError, match="record"):
        audit_run(data)


@pytest.mark.parametrize("value", [[], {}, None, True])
def test_status_and_message_role_unhashable_values_become_contract_errors(value):
    data = run()
    data["status"] = value
    with pytest.raises(ContractError):
        audit_run(data)
    data = run()
    data["records"][0]["status"] = value
    with pytest.raises(ContractError):
        audit_run(data)
    definition = suite()
    definition["cases"][0]["messages"][0]["role"] = value
    with pytest.raises(ContractError):
        validate_suite(definition)


@pytest.mark.parametrize("value", [None, [], "iid", {}, {"independent_groups": 1}, {"independent_groups": 1.0}, {"independent_groups": "true"}, {"independent_groups": []}])
def test_sampling_requires_object_and_explicit_boolean(value):
    definition = suite()
    definition["sampling"] = deepcopy(value)
    with pytest.raises(ContractError, match="sampling"):
        validate_suite(definition)
    data = run()
    data["provenance"]["sampling"] = deepcopy(value)
    with pytest.raises(ContractError, match="sampling"):
        audit_run(data)


@pytest.mark.parametrize("field", ["score", "latency_ms", "cost_usd"])
def test_huge_integers_are_rejected_without_float_overflow(field):
    data = run()
    data["records"][0][field] = 10 ** 400
    with pytest.raises(ContractError, match=field):
        audit_run(data)


def test_usage_overflow_is_rejected_before_resource_aggregation():
    data = run()
    data["records"][0]["usage"] = {"total_tokens": 10 ** 400}
    with pytest.raises(ContractError, match="usage.total_tokens"):
        audit_run(data)


@pytest.mark.parametrize("scorer,expected", [("exact", []), ("contains_all", {}), ("json_fields", {"fields": []}),
                                           ("json_fields", {"fields": {}, "allow_extra": []}),
                                           ("tool_call", {"name": [], "arguments": {}}),
                                           ("tool_call", {"name": "t", "arguments": []}),
                                           ("grounded_answer", {"answer": "a", "citations": [{}], "abstain": False}),
                                           ("grounded_answer", {"answer": "a", "citations": [], "abstain": []})])
def test_common_bad_scorer_configs_report_contract_error(scorer, expected):
    definition = suite()
    definition["cases"][0].update(scorer=scorer, expected=expected)
    with pytest.raises(ContractError, match="scorer configuration"):
        validate_suite(definition)


@pytest.mark.parametrize("mutation", ["protocol", "record", "status", "sampling", "huge"])
def test_cli_invalid_evidence_returns_three_not_regression(tmp_path, capsys, mutation):
    data = run()
    if mutation == "protocol":
        data["protocol"] = []
    elif mutation == "record":
        data["records"] = [None]
    elif mutation == "status":
        data["status"] = []
    elif mutation == "sampling":
        data["provenance"]["sampling"] = {"independent_groups": 1}
    else:
        data["records"][0]["latency_ms"] = 10 ** 400
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    assert cli.main(["validate", str(path)]) == 3
    assert "EvalAnchor:" in capsys.readouterr().err


def test_real_cli_process_exit_code_for_malformed_nested_json(tmp_path):
    data = run()
    data["protocol"] = []
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run([sys.executable, "-m", "evalanchor", "validate", str(path)], capture_output=True, text=True, env=env, timeout=10)
    assert completed.returncode == 3
    assert "Traceback" not in completed.stderr
    assert "run.protocol must be an object" in completed.stderr
