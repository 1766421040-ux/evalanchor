"""Deterministic, versioned output contracts; no output repair or semantic judge.

``grounded_answer`` checks a structured answer/citation contract. It does not
infer entailment, discover missing facts, or measure open-ended faithfulness.
"""

from __future__ import annotations

import json
import math
from typing import Any

from . import SCORER_VERSION

SCORERS = frozenset({"exact", "json_exact", "json_fields", "contains_all", "tool_call", "grounded_answer"})


class InvalidJSON(ValueError):
    """JSON is ambiguous, nonstandard, or malformed."""


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJSON(f"Duplicate object key: {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise InvalidJSON(f"Non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise InvalidJSON("JSON number exceeds finite float range")
    return number


def strict_json_loads(output: str) -> Any:
    """Parse one JSON value without markdown stripping or coercing values."""
    if not isinstance(output, str):
        raise InvalidJSON("Output must be a string")
    try:
        return json.loads(output, object_pairs_hook=_object, parse_constant=_constant, parse_float=_finite_float)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InvalidJSON(f"Malformed JSON: {exc}") from exc


def strict_equal(actual: Any, expected: Any) -> bool:
    """JSON structural equality; bool, integer, and float are distinct types."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(strict_equal(actual[k], expected[k]) for k in expected)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(strict_equal(a, e) for a, e in zip(actual, expected))
    if isinstance(actual, float) and (not math.isfinite(actual) or not math.isfinite(expected)):
        return False
    return actual == expected


def _subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(actual[k], v) for k, v in expected.items())
    return strict_equal(actual, expected)


def _result(passed: bool, success: str, failure: str, **details: Any) -> dict:
    return {"score": float(passed), "reason": success if passed else failure, "details": details}


def score_case(case: dict, output: str) -> dict:
    """Score raw output against a case's explicit expected contract.

    Invalid outputs score zero. Invalid scorer configuration raises ValueError
    so a configuration defect cannot masquerade as a model failure.
    """
    scorer = case.get("scorer")
    if scorer not in SCORERS:
        raise ValueError(f"Unknown scorer: {scorer!r}")
    if "expected" not in case:
        raise ValueError("Missing expected contract")
    expected = case["expected"]
    if not isinstance(output, str):
        return _result(False, "", "Output must be a raw string", error_type="output_type")
    if scorer == "exact":
        if not isinstance(expected, str):
            raise ValueError("exact expected must be a string")
        return _result(output == expected, "Exact string match", "String differs; no whitespace or case normalization", metric_kind="deterministic_contract")
    if scorer == "contains_all":
        if not isinstance(expected, list) or not expected or not all(isinstance(x, str) and x for x in expected):
            raise ValueError("contains_all expected must be a nonempty list of nonempty strings")
        missing = [value for value in expected if value not in output]
        return _result(not missing, "All required case-sensitive substrings present", "Required substrings missing", missing=missing, limitation="Keyword presence does not establish semantic correctness")
    if scorer == "json_fields":
        if not isinstance(expected, dict) or set(expected) - {"fields", "allow_extra"} or not isinstance(expected.get("fields"), dict) or type(expected.get("allow_extra", False)) is not bool:
            raise ValueError("json_fields expected must contain fields:object and optional allow_extra:bool")
    if scorer == "tool_call":
        if not isinstance(expected, dict) or set(expected) != {"name", "arguments"} or not isinstance(expected["name"], str) or not expected["name"] or not isinstance(expected["arguments"], dict):
            raise ValueError("tool_call expected must contain exactly name:string and arguments:object")
    if scorer == "grounded_answer":
        if not isinstance(expected, dict) or set(expected) != {"answer", "citations", "abstain"}:
            raise ValueError("grounded_answer expected must contain answer, citations, abstain")
        if type(expected["abstain"]) is not bool or not isinstance(expected["citations"], list) or not all(isinstance(x, str) and x for x in expected["citations"]) or len(set(expected["citations"])) != len(expected["citations"]):
            raise ValueError("Invalid grounded_answer expected citations or abstain")
        if expected["abstain"] and expected["answer"] is not None:
            raise ValueError("An abstention must have answer=null")
    try:
        parsed = strict_json_loads(output)
    except (InvalidJSON, ValueError) as exc:
        return _result(False, "", str(exc), error_type="invalid_json")
    if scorer == "json_exact":
        return _result(strict_equal(parsed, expected), "Exact JSON structure and types match", "JSON structure, value, or type differs", metric_kind="deterministic_contract")
    if scorer == "json_fields":
        allow_extra = expected.get("allow_extra", False)
        passed = _subset(parsed, expected["fields"]) if allow_extra else strict_equal(parsed, expected["fields"])
        return _result(passed, "Required JSON fields and types match", "JSON field contract failed", allow_extra=allow_extra)
    if scorer == "tool_call":
        passed = isinstance(parsed, dict) and set(parsed) == {"name", "arguments"} and strict_equal(parsed, expected)
        return _result(passed, "Tool name and all argument types/values match", "Tool call contract failed", executed=False)
    if not isinstance(parsed, dict) or set(parsed) != {"answer", "citations", "abstain"}:
        return _result(False, "", "Expected exactly answer, citations, abstain", error_type="invalid_answer_schema")
    citations = parsed["citations"]
    valid_citations = isinstance(citations, list) and all(isinstance(x, str) and x for x in citations) and len(set(citations)) == len(citations)
    citation_match = valid_citations and set(citations) == set(expected["citations"])
    answer_match = strict_equal(parsed["answer"], expected["answer"])
    abstention_match = type(parsed["abstain"]) is bool and parsed["abstain"] == expected["abstain"]
    return _result(bool(citation_match and answer_match and abstention_match), "Answer value, citation IDs, and abstention contract match", "Answer/citation/abstention contract failed", answer_match=answer_match, citations_match=bool(citation_match), abstention_match=abstention_match, limitation="Exact evidence contract; not a semantic entailment judge")
