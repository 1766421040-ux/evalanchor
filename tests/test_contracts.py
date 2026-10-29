"""Strict identity and score re-audit checks, independent of the demo fixtures."""

import copy

import pytest

from evalanchor.contracts import ContractError, assert_comparable, audit_run, case_hash, fingerprint, loads, protocol, suite_hash, validate_suite
from evalanchor.runner import run_suite


def suite():
    return {"schema_version": 1, "id": "contracts-test", "cases": [{"id": "one", "group_id": "group-one", "tags": ["critical"], "messages": [{"role": "user", "content": "Return OK"}], "scorer": "exact", "expected": "OK"}]}


def artifact(tmp_path):
    return run_suite(suite(), {"id": "fixture", "provider": "replay", "outputs": {"one": "OK"}}, tmp_path, retries=0)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', 'NaN', 'Infinity', '-Infinity', '1e999', '{"x":-1e999}'])
def test_contract_json_rejects_ambiguous_and_nonfinite_values(raw):
    with pytest.raises(ContractError):
        loads(raw)


def test_fingerprint_is_order_independent_and_type_sensitive():
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})
    assert fingerprint({"a": True}) != fingerprint({"a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 1.0})


@pytest.mark.parametrize("field,value", [("expected", "different"), ("group_id", "different-group"), ("tags", ["ordinary"]), ("messages", [{"role": "user", "content": "different prompt"}]), ("scorer", "contains_all")])
def test_case_identity_binds_prompt_reference_scoring_and_grouping(field, value):
    original = suite()["cases"][0]
    changed = copy.deepcopy(original)
    changed[field] = value
    assert case_hash(original) != case_hash(changed)


def test_validation_prevents_duplicate_case_ids_and_label_in_message():
    data = suite()
    data["cases"].append(copy.deepcopy(data["cases"][0]))
    with pytest.raises(ContractError, match="Duplicate"):
        validate_suite(data)
    data = suite()
    data["cases"][0]["messages"][0]["expected"] = "OK"
    with pytest.raises(ContractError, match="only role and content"):
        validate_suite(data)


@pytest.mark.parametrize("value", [True, 1.0])
def test_schema_version_requires_an_integer(value):
    data = suite()
    data["schema_version"] = value
    with pytest.raises(ContractError, match="schema_version"):
        validate_suite(data)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, 1001])
def test_repeat_protocol_rejects_invalid_counts(value):
    with pytest.raises(ContractError):
        protocol(value)


def test_reaudit_detects_score_tampering(tmp_path):
    run = artifact(tmp_path)
    run["records"][0]["score"] = 0
    with pytest.raises(ContractError, match="Score tampering"):
        audit_run(run)


@pytest.mark.parametrize("value", [True, 1.0])
def test_run_schema_version_requires_an_integer(tmp_path, value):
    run = artifact(tmp_path)
    run["schema_version"] = value
    with pytest.raises(ContractError, match="schema_version"):
        audit_run(run)


def test_reaudit_detects_changed_raw_output_without_rescore(tmp_path):
    run = artifact(tmp_path)
    run["records"][0]["output"] = "WRONG"
    with pytest.raises(ContractError, match="Score tampering"):
        audit_run(run)


def test_reaudit_rejects_reference_change_even_if_score_still_passes(tmp_path):
    run = artifact(tmp_path)
    run["suite_definition"]["cases"][0]["expected"] = "WRONG"
    run["records"][0]["output"] = "WRONG"
    with pytest.raises(ContractError, match="fingerprint"):
        audit_run(run)


def test_comparison_refuses_missing_and_duplicate_rows(tmp_path):
    original = artifact(tmp_path)
    missing = copy.deepcopy(original)
    missing["records"] = []
    with pytest.raises(ContractError, match="missing"):
        assert_comparable(original, missing)
    duplicate = copy.deepcopy(original)
    duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
    with pytest.raises(ContractError, match="Duplicate"):
        assert_comparable(original, duplicate)


def test_incomplete_cannot_compare_even_when_existing_rows_are_valid(tmp_path):
    original = artifact(tmp_path)
    partial = copy.deepcopy(original)
    partial["status"] = "incomplete"
    partial["records"] = []
    assert audit_run(partial)["verified"]
    with pytest.raises(ContractError, match="Incomplete"):
        assert_comparable(original, partial)


def test_forged_group_or_tag_cannot_change_slice_membership(tmp_path):
    original = artifact(tmp_path)
    for field, value in (("group_id", "other"), ("tags", ["ordinary"])):
        changed = copy.deepcopy(original)
        changed["records"][0][field] = value
        with pytest.raises(ContractError, match="identity"):
            audit_run(changed)


def test_comparison_checks_protocol_hash(tmp_path):
    original = artifact(tmp_path)
    changed = copy.deepcopy(original)
    changed["protocol"]["fingerprint"] = "forged"
    with pytest.raises(ContractError, match="protocol"):
        assert_comparable(original, changed)
