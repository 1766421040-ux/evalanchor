"""Statistical kernel tests isolate replay, which is exercised in runner tests.

The autouse mock is explicit: these tiny records are not native API evidence.
Production summarize_experiment always calls the independent replay auditor.
"""
from copy import deepcopy

import pytest

from evalanchor.agent_analysis import _pair_stats, compare_horizons, summarize_experiment
from evalanchor.contracts import ContractError, fingerprint


@pytest.fixture(autouse=True)
def mock_replay_auditor(monkeypatch):
    def audit(experiment):
        return {"verified": True, "episodes": len(experiment["records"]), "test_only_mock": True}
    monkeypatch.setattr("evalanchor.agent_runner.audit_experiment", audit)
    # The default two-case kernel fixture stands in for the source-registered
    # benchmark; tests that edit/drop cases must lose trust even with new hashes.
    monkeypatch.setattr("evalanchor.orderops.benchmark_suite", lambda: experiment()["benchmark"])


def experiment(*, repeats=2, cases=None, four_configs=False):
    cases = cases or [{"id": "a", "group_id": "g1", "family": "cancel", "split": "test"},
                      {"id": "b", "group_id": "g2", "family": "refund", "split": "test"}]
    configs = [{"id": "luna-direct", "model": "gpt-6-luna", "prompt_strategy": "direct", "max_output_tokens": 512},
               {"id": "luna-checklist", "model": "gpt-6-luna", "prompt_strategy": "checklist", "max_output_tokens": 512}]
    if four_configs:
        configs += [{"id": "terra-direct", "model": "gpt-6.1-terra", "prompt_strategy": "direct", "max_output_tokens": 512},
                    {"id": "terra-checklist", "model": "gpt-6.1-terra", "prompt_strategy": "checklist", "max_output_tokens": 512}]
    benchmark = {"cases": cases, "sampling": {"kind": "authored_business_worlds", "independent_groups": False}}
    records = []
    for cfg in configs:
        for case in cases:
            for repeat in range(repeats):
                records.append({"config_id": cfg["id"], "case_id": case["id"], "group_id": case["group_id"],
                                "family": case["family"], "split": case["split"], "repeat": repeat, "status": "completed",
                                "grade": {"task_success": True, "attempted_violations": 0, "executed_violations": 0},
                                "steps": [], "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                                "estimated_cost_usd": .01, "known_cost_subtotal_usd": .01, "latency_ms": 10,
                                "fingerprint": "mock-kernel-record"})
    return {"schema_version": 2, "id": "kernel", "status": "complete", "benchmark": benchmark,
            "benchmark_hash": fingerprint(benchmark), "configs": configs, "case_ids": [case["id"] for case in cases],
            "protocol": {"repeats": repeats, "split": cases[0]["split"], "max_turns": 8},
            "provenance": {"kind": "native_responses_tool_execution"}, "records": records}


def test_complete_test_contract_pass_is_explicitly_finite():
    result = summarize_experiment(experiment())
    assert result["grid"]["complete_test_split"]
    assert result["grid"]["complete_selected_grid"]
    assert all(row["gate"]["decision"] == "pass" for row in result["configs"])
    assert result["population_inference"]["enabled"] is False
    assert all(row["gate"]["population_inference"] is False for row in result["configs"])
    assert result["comparisons"][0]["bootstrap_interval"]["descriptive_only"] is True
    assert result["comparisons"][0]["bootstrap_interval"]["degenerate"] is True


def test_more_cases_in_one_template_do_not_outweigh_other_groups():
    cases = [{"id": str(i), "group_id": "large", "family": "cancel", "split": "test"} for i in range(9)]
    cases += [{"id": "last", "group_id": "small", "family": "refund", "split": "test"}]
    source = experiment(cases=cases)
    for record in source["records"]:
        record["grade"]["task_success"] = record["group_id"] == "large"
    result = summarize_experiment(source)
    assert result["configs"][0]["task_success_episodes"] == 18
    assert result["configs"][0]["n_episodes"] == 20
    assert result["configs"][0]["safe_success_rate"] == .5  # Not 18/20.
    assert result["configs"][0]["gate"]["decision"] == "fail"


def test_hundred_repeats_never_create_hundred_independent_groups():
    pairs = [{"case_id": "a", "repeat": 0, "group_id": "g1", "baseline_score": 0, "candidate_score": 1},
             {"case_id": "b", "repeat": 0, "group_id": "g2", "baseline_score": 1, "candidate_score": 0}]
    once = _pair_stats(pairs)
    repeated = _pair_stats([{**row, "repeat": i} for row in pairs for i in range(100)])
    assert repeated["n_pairs"] == 200 and repeated["n_groups"] == once["n_groups"] == 2
    assert repeated["delta"] == once["delta"] == 0
    assert repeated["bootstrap_interval"] == once["bootstrap_interval"]


@pytest.mark.parametrize("status", ["model_error", "budget_exhausted", "turn_limit", "interrupted", "no_finish"])
def test_terminal_errors_stay_in_quality_denominator(status):
    source = experiment()
    row = source["records"][0]
    row["status"] = status
    row["grade"]["task_success"] = False
    row["estimated_cost_usd"] = None
    result = summarize_experiment(source)
    first = result["configs"][0]
    assert first["n_episodes"] == 4 and first["safe_success_episodes"] == 3
    assert first["safe_success_rate"] == .75
    assert first["status_counts"][status] == 1
    assert first["cost"]["total_usd"] is None
    assert first["cost"]["known_subtotal_usd"] == .04
    assert first["gate"]["decision"] == "fail"


@pytest.mark.parametrize("field", ["attempted_violations", "executed_violations"])
def test_one_unsafe_action_fails_even_when_goal_is_achieved(field):
    source = experiment()
    source["records"][0]["grade"][field] = 1
    row = summarize_experiment(source)["configs"][0]
    assert row["task_success_rate"] == 1
    assert row["safe_success_rate"] == .75
    assert row["gate"]["decision"] == "fail"
    assert row[field] == 1


def test_replay_auditor_must_accept_before_any_grade_is_used(monkeypatch):
    def refuse(_):
        raise ContractError("Raw response and grade mismatch")
    monkeypatch.setattr("evalanchor.agent_runner.audit_experiment", refuse)
    with pytest.raises(ContractError, match="Raw response"):
        summarize_experiment(experiment())


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_repeat", "wrong_group", "wrong_config", "wrong_split"])
def test_complete_grid_and_identity_mismatches_are_rejected(mutation):
    source = experiment()
    if mutation == "missing":
        source["records"].pop()
    elif mutation == "duplicate":
        source["records"].append(deepcopy(source["records"][0]))
    elif mutation == "wrong_repeat":
        source["records"][0]["repeat"] = 100
    elif mutation == "wrong_group":
        source["records"][0]["group_id"] = "invented-independent-unit"
    elif mutation == "wrong_config":
        source["records"][0]["config_id"] = "other"
    elif mutation == "wrong_split":
        source["records"][0]["split"] = "dev"
    with pytest.raises(ContractError):
        summarize_experiment(source)


def test_incomplete_experiment_may_display_counts_but_cannot_pass():
    source = experiment()
    source["status"] = "running"
    source["records"].pop()
    result = summarize_experiment(source)
    assert result["grid"]["missing_episodes"] == 1
    assert all(row["gate"]["decision"] == "inconclusive" for row in result["configs"])
    assert not result["comparisons"][0]["complete_paired_grid"]
    assert result["comparisons"][0]["n_pairs"] == 3
    assert "luna-checklist" in result["pareto"]["excluded_incomplete_ids"]


def test_empty_running_experiment_does_not_invent_zero_cost_or_success():
    source = experiment()
    source["status"] = "running"
    source["records"] = []
    result = summarize_experiment(source)
    assert result["grid"]["observed_episodes"] == 0
    assert result["configs"][0]["safe_success_rate"] is None
    assert result["configs"][0]["cost"]["total_usd"] is None
    assert result["comparisons"][0]["bootstrap_interval"] is None
    assert result["pareto"]["frontier_config_ids"] == []


@pytest.mark.parametrize("kind", ["dev", "pilot", "scripted_fixture"])
def test_development_pilot_or_fixture_cannot_claim_release_pass(kind):
    source = experiment()
    if kind == "dev":
        source["protocol"]["split"] = "dev"
        for row in source["benchmark"]["cases"] + source["records"]:
            row["split"] = "dev"
    elif kind == "pilot":
        source["case_ids"] = ["a"]
        source["records"] = [row for row in source["records"] if row["case_id"] == "a"]
    else:
        source["provenance"]["kind"] = "scripted_fixture"
    result = summarize_experiment(source)
    assert all(row["gate"]["decision"] == "inconclusive" for row in result["configs"])


def test_unknown_cost_is_excluded_and_not_a_free_pareto_winner():
    source = experiment()
    source["records"][0]["estimated_cost_usd"] = None
    result = summarize_experiment(source)
    assert result["configs"][0]["cost"]["total_usd"] is None
    assert result["configs"][0]["cost"]["known_subtotal_usd"] == .04
    assert result["pareto"]["frontier_config_ids"] == ["luna-checklist"]
    assert result["pareto"]["excluded_unknown_cost_ids"] == ["luna-direct"]


def test_dropping_hard_cases_and_rehashing_suite_cannot_create_release_pass():
    source = experiment()
    source["case_ids"] = ["a"]
    source["benchmark"]["cases"] = [case for case in source["benchmark"]["cases"] if case["id"] == "a"]
    source["benchmark_hash"] = fingerprint(source["benchmark"])
    source["records"] = [row for row in source["records"] if row["case_id"] == "a"]
    result = summarize_experiment(source)
    assert result["grid"]["complete_selected_grid"] and result["grid"]["complete_test_split"]
    assert result["grid"]["trusted_benchmark"] is False
    assert all(row["gate"]["decision"] == "inconclusive" for row in result["configs"])
    assert all(not row["gate"]["trusted_benchmark"] for row in result["configs"])


def test_unknown_usage_preserves_unknown_counts_and_known_subtotal():
    source = experiment()
    source["records"][0]["usage"]["input_tokens"] = None
    source["records"][0]["usage"]["output_tokens"] = None
    source["records"][0]["usage"]["total_tokens"] = None
    result = summarize_experiment(source)
    assert result["configs"][0]["usage"]["input_tokens"] == {"known_subtotal": 300, "unknown_episodes": 1}
    source["records"][0]["usage"]["known_input_tokens"] = 40
    partial = summarize_experiment(source)
    assert partial["configs"][0]["usage"]["input_tokens"] == {"known_subtotal": 340, "unknown_episodes": 1}


def test_empirical_pareto_handles_ties_and_strict_dominance():
    source = experiment()
    tied = summarize_experiment(source)
    assert set(tied["pareto"]["frontier_config_ids"]) == {"luna-direct", "luna-checklist"}
    for row in source["records"]:
        if row["config_id"] == "luna-checklist":
            row["estimated_cost_usd"] = row["known_cost_subtotal_usd"] = .005
    cheaper = summarize_experiment(source)
    assert cheaper["pareto"]["frontier_config_ids"] == ["luna-checklist"]
    assert cheaper["pareto"]["descriptive_only"] is True


def test_two_prompt_interventions_are_distinct_from_cross_model_references():
    result = summarize_experiment(experiment(four_configs=True))
    primary = [row for row in result["comparisons"] if row["primary"]]
    assert {(row["baseline_id"], row["candidate_id"]) for row in primary} == {
        ("luna-direct", "luna-checklist"), ("terra-direct", "terra-checklist")}
    assert len(result["comparisons"]) == 5
    assert all(row["comparison_kind"] == "within_model_prompt_intervention" for row in primary)
    secondary = [row for row in result["comparisons"] if row["secondary"]]
    assert {(row["baseline_id"], row["candidate_id"]) for row in secondary} == {
        ("luna-direct", "terra-direct"), ("luna-checklist", "terra-checklist")}
    assert all(row["comparison_kind"] == "cross_model_same_prompt" for row in secondary)
    reference = next(row for row in result["comparisons"] if row["baseline_id"] == "luna-direct" and row["candidate_id"] == "terra-checklist")
    assert reference["roles"] == ["baseline_reference"] and not reference["primary"] and not reference["secondary"]


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(protocol=[]),
    lambda e: e["records"][0].update(status=[]),
    lambda e: e["records"][0]["grade"].update(task_success=1),
    lambda e: e["records"][0]["grade"].update(attempted_violations=True),
    lambda e: e["records"][0].update(estimated_cost_usd=10 ** 1000),
    lambda e: e["configs"].append(deepcopy(e["configs"][0])),
    lambda e: e["case_ids"].append("a"),
    lambda e: e.pop("benchmark_hash"),
    lambda e: e["protocol"].update(max_turns=True),
    lambda e: e["configs"][0].update(max_output_tokens=[]),
    lambda e: e["records"][0].update(steps=[[]]),
])
def test_malformed_types_or_duplicate_definitions_raise_contract_error(mutate):
    source = experiment()
    mutate(source)
    with pytest.raises(ContractError):
        summarize_experiment(source)


def test_existing_analysis_is_excluded_from_its_own_evidence_hash():
    source = experiment()
    first = summarize_experiment(source)
    source["analysis"] = first
    second = summarize_experiment(source)
    assert first == second


def horizon_experiments():
    short = experiment()
    long = experiment()
    for name, source, turns, seed in (("short", short, 8, 20261029), ("long", long, 12, 20261030)):
        source["id"] = name
        source["protocol"].update(max_turns=turns, scoring_version="frozen-v2",
                                  scheduling={"kind": "seeded_case_repeat_blocks", "seed": seed, "workers": 8})
        source["provenance"].update(engine_hash="identical-frozen-engine", prompts={"direct": "direct frozen", "checklist": "checklist frozen"})
        for row in source["records"]:
            row["grade"]["goal_satisfied"] = True
            identity = f"{name}:{row['config_id']}:{row['case_id']}:{row['repeat']}:0"
            row["steps"] = [{"request_key": identity, "request": {}, "response": {"id": identity},
                             "request_hash": "kernel-only", "response_hash": "kernel-only", "tool_calls": []}]
            if name == "long":
                row["estimated_cost_usd"] = row["known_cost_subtotal_usd"] = .015
            elif row["config_id"] == "luna-checklist" and row["case_id"] == "a":
                row["status"] = "turn_limit"
                row["grade"]["task_success"] = False
    return short, long


def test_horizon_full_matrix_distinguishes_business_goal_from_finish():
    short, long = horizon_experiments()
    result = compare_horizons(short, long)
    checklist = next(row for row in result["configs"] if row["config_id"] == "luna-checklist")
    assert checklist["n_pairs"] == 4 and checklist["n_groups"] == 2
    assert checklist["metrics"]["safe_success"]["delta"] == .5
    assert checklist["metrics"]["completed"]["delta"] == .5
    assert checklist["metrics"]["goal_satisfied"]["delta"] == 0
    assert checklist["cost"]["total_delta_usd"] == pytest.approx(.02)
    interaction = result["interactions"][0]
    assert interaction["prompt_effect_8"] == -.5
    assert interaction["prompt_effect_12"] == 0
    assert interaction["difference_in_differences"] == .5
    assert interaction["bootstrap_interval"]["descriptive_only"] is True
    assert result["additional_independent_task_groups"] == 0
    assert result["population_inference"] is False
    assert checklist["baseline_gate"]["decision"] == "fail" and checklist["candidate_gate"]["decision"] == "pass"


@pytest.mark.parametrize("mutation", ["prompt", "engine", "config", "missing", "repeats", "wrong_horizon", "worker_count", "scoring", "reuse_response", "incomplete", "fixture", "unknown_goal"])
def test_horizon_comparison_rejects_changed_design_or_reused_evidence(mutation):
    short, long = horizon_experiments()
    if mutation == "prompt":
        long["provenance"]["prompts"]["checklist"] += " tuned on test failures"
    elif mutation == "engine":
        long["provenance"]["engine_hash"] = "changed scorer"
    elif mutation == "config":
        long["configs"][0]["max_output_tokens"] = 1024
    elif mutation == "missing":
        long["records"].pop()
    elif mutation == "repeats":
        long["protocol"]["repeats"] = 3
    elif mutation == "wrong_horizon":
        long["protocol"]["max_turns"] = 10
    elif mutation == "worker_count":
        long["protocol"]["scheduling"]["workers"] = 4
    elif mutation == "scoring":
        long["protocol"]["scoring_version"] = "finish no longer needed"
    elif mutation == "reuse_response":
        long["records"][0]["steps"][0]["response"]["id"] = short["records"][0]["steps"][0]["response"]["id"]
    elif mutation == "incomplete":
        long["status"] = "running"
    elif mutation == "fixture":
        long["provenance"]["kind"] = "scripted_fixture"
    elif mutation == "unknown_goal":
        long["records"][0]["grade"]["goal_satisfied"] = None
    with pytest.raises(ContractError):
        compare_horizons(short, long)


def test_horizon_failure_only_selection_is_refused_even_when_both_sides_match():
    short, long = horizon_experiments()
    for source in (short, long):
        source["case_ids"] = ["a"]
        source["records"] = [row for row in source["records"] if row["case_id"] == "a"]
    with pytest.raises(ContractError, match="complete test matrices"):
        compare_horizons(short, long)


def test_horizon_swapped_case_definitions_cannot_be_rehashed_into_comparability():
    short, long = horizon_experiments()
    long["benchmark"]["cases"][0]["new_goal"] = "easier answer"
    long["benchmark_hash"] = fingerprint(long["benchmark"])
    with pytest.raises(ContractError, match="registered frozen benchmark"):
        compare_horizons(short, long)


def test_horizon_unknown_cost_does_not_compare_only_the_known_subset():
    short, long = horizon_experiments()
    long["records"][0]["estimated_cost_usd"] = None
    result = compare_horizons(short, long)
    direct = next(row for row in result["configs"] if row["config_id"] == "luna-direct")
    assert direct["cost"]["candidate_total_usd"] is None
    assert direct["cost"]["total_delta_usd"] is None
    assert direct["cost"]["group_comparison"] is None
    assert direct["cost"]["candidate_unknown_episodes"] == 1
    assert direct["metrics"]["safe_success"]["n_pairs"] == 4


def test_horizon_case_and_config_order_need_not_match_the_seeded_schedule():
    short, long = horizon_experiments()
    long["case_ids"].reverse()
    long["configs"].reverse()
    long["records"].reverse()
    result = compare_horizons(short, long)
    assert result["n_cases"] == 2 and result["repeats"] == 2
    assert result["interactions"][0]["difference_in_differences"] == .5
