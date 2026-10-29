from collections import Counter

import pytest

from evalanchor.datasets import demo_outputs, demo_suite, local_smoke_suite
from evalanchor.metrics import score_case


def test_suite_has_real_groups_and_explicit_synthetic_scope():
    suite = demo_suite()
    assert len(suite["cases"]) == 64
    assert len({x["id"] for x in suite["cases"]}) == 64
    groups = Counter(x["group_id"] for x in suite["cases"])
    assert len(groups) == 16 and set(groups.values()) == {4}
    assert suite["sampling"]["independent_groups"] is False
    assert all("synthetic" in x["tags"] for x in suite["cases"])
    assert {x["scorer"] for x in suite["cases"]} == {"exact", "json_exact", "json_fields", "contains_all", "tool_call", "grounded_answer"}


def test_candidate_average_improves_while_critical_contracts_regress():
    suite = demo_suite()
    outputs = {name: demo_outputs(suite, name) for name in ("baseline", "candidate", "fixed")}
    totals = {name: sum(score_case(case, predictions[case["id"]])["score"] for case in suite["cases"]) for name, predictions in outputs.items()}
    assert totals == {"baseline": 40, "candidate": 56, "fixed": 64}
    critical = [case for case in suite["cases"] if "critical" in case["tags"]]
    assert len(critical) == 8
    assert all(score_case(case, outputs["baseline"][case["id"]])["score"] == 1 for case in critical)
    assert all(score_case(case, outputs["candidate"][case["id"]])["score"] == 0 for case in critical)


def test_demo_does_not_substitute_references_for_custom_cases():
    suite = demo_suite()
    suite["cases"][0]["expected"]["fields"]["passengers"] = 2.0
    with pytest.raises(ValueError, match="exact built-in"):
        demo_outputs(suite, "fixed")


def test_suite_returns_independent_fresh_objects():
    a, b = demo_suite(), demo_suite()
    a["cases"][0]["tags"].append("modified")
    assert "modified" not in b["cases"][0]["tags"]


def test_unknown_fixture_variant_rejected():
    with pytest.raises(ValueError):
        demo_outputs(demo_suite(), "actual-model")


def test_local_smoke_selection_has_one_original_case_per_group_and_no_fixtures():
    suite = local_smoke_suite()
    assert len(suite["cases"]) == len({c["group_id"] for c in suite["cases"]}) == 16
    assert sum("lang:zh" in c["tags"] for c in suite["cases"]) == 8
    source = {c["id"]: c for c in demo_suite()["cases"]}
    assert all(source[c["id"]] == c for c in suite["cases"])
    assert suite["sampling"]["independent_groups"] is False
    with pytest.raises(ValueError, match="exact built-in"):
        demo_outputs(suite, "fixed")
