import json

import pytest

from evalanchor.metrics import InvalidJSON, score_case, strict_equal, strict_json_loads


def score(scorer, expected, raw):
    return score_case({"scorer": scorer, "expected": expected}, raw)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"a":{"x":1,"x":1}}', 'NaN', 'Infinity', '-Infinity', '{"x":1e9999}', '```json\n{}\n```', '{}{}', '{"x":1,}'])
def test_ambiguous_or_nonstandard_json_rejected(raw):
    with pytest.raises(InvalidJSON):
        strict_json_loads(raw)
    assert score("json_exact", {}, raw)["score"] == 0


@pytest.mark.parametrize("actual,expected", [(True, 1), (False, 0), (1, 1.0), ({"x": True}, {"x": 1}), ([1, True], [1, 1]), (float("nan"), float("nan"))])
def test_json_type_coercion_is_never_equality(actual, expected):
    assert not strict_equal(actual, expected)


def test_json_object_order_irrelevant_but_arrays_ordered():
    assert score("json_exact", {"a": 1, "b": 2}, '{"b":2,"a":1}')["score"] == 1
    assert score("json_exact", [1, 2], '[2,1]')["score"] == 0


def test_exact_does_not_strip_or_lowercase():
    assert score("exact", "OK", "OK")["score"] == 1
    assert score("exact", "OK", "OK\n")["score"] == 0
    assert score("exact", "OK", "ok")["score"] == 0


def test_json_fields_extra_fields_require_explicit_opt_in():
    expected = {"fields": {"user": {"id": 1}}}
    raw = '{"user":{"id":1,"email":"x"},"other":0}'
    assert score("json_fields", expected, raw)["score"] == 0
    assert score("json_fields", {**expected, "allow_extra": True}, raw)["score"] == 1
    assert score("json_fields", {**expected, "allow_extra": True}, '{"user":{"id":true}}')["score"] == 0


def test_tool_call_name_and_arguments_are_exact_and_never_executed():
    expected = {"name": "refund_preview", "arguments": {"amount": 1250, "notify": False}}
    valid = score("tool_call", expected, json.dumps(expected))
    assert valid["score"] == 1 and valid["details"]["executed"] is False
    for modified in (
        {**expected, "name": "refund_execute"},
        {**expected, "arguments": {"amount": True, "notify": False}},
        {**expected, "arguments": {"amount": "1250", "notify": False}},
        {**expected, "extra": "ignored?"},
    ):
        assert score("tool_call", expected, json.dumps(modified))["score"] == 0


def test_grounded_contract_requires_answer_and_exact_unique_citation_set():
    expected = {"answer": 10, "citations": ["doc-a", "doc-b"], "abstain": False}
    assert score("grounded_answer", expected, '{"answer":10,"citations":["doc-b","doc-a"],"abstain":false}')["score"] == 1
    for actual in (
        {**expected, "answer": 11},
        {**expected, "citations": ["doc-a", "doc-a", "doc-b"]},
        {**expected, "citations": ["doc-a", "nonexistent"]},
        {**expected, "abstain": 0},
        {**expected, "extra": True},
    ):
        assert score("grounded_answer", expected, json.dumps(actual))["score"] == 0


def test_abstention_contract_rejects_an_invented_answer():
    expected = {"answer": None, "citations": [], "abstain": True}
    assert score("grounded_answer", expected, json.dumps(expected))["score"] == 1
    assert score("grounded_answer", expected, '{"answer":"unknown","citations":[],"abstain":true}')["score"] == 0


def test_contains_is_explicitly_only_a_keyword_contract():
    result = score("contains_all", ["CONFLICT", "ESCALATE"], "CONFLICT: ESCALATE")
    assert result["score"] == 1
    assert "semantic" in result["details"]["limitation"]
    assert score("contains_all", ["CONFLICT"], "conflict")["score"] == 0


@pytest.mark.parametrize("scorer,expected", [("unknown", "ok"), ("exact", 1), ("contains_all", []), ("json_fields", {"fields": {}, "allow_extra": 1}), ("tool_call", {"name": "x"}), ("grounded_answer", {"answer": "x", "citations": [], "abstain": True})])
def test_invalid_metric_configuration_is_not_a_model_failure(scorer, expected):
    with pytest.raises(ValueError):
        score(scorer, expected, "{}")
