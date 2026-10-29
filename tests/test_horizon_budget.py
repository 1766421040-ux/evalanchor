"""Cost gate kernel tests mock transcript replay; real evidence is separately audited."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from evalanchor.budget import BudgetLedger
from evalanchor.contracts import ContractError, read_json
from evalanchor.orderops import benchmark_suite
from scripts import check_horizon_budget as gate


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "audit_experiment", lambda experiment: {"verified": True})
    root = Path(__file__).resolve().parents[1]
    frozen = read_json(root / "artifacts/orderops-v2/frozen-protocol.json")
    amendment = read_json(root / "artifacts/orderops-v2/preregistration-amendment-resource-horizon.json")
    ledger = BudgetLedger(tmp_path / "budget.db", 4.75, campaign={"id": "gate-kernel"})
    ref = ledger.reserve("prior-charge", .5)
    ledger.mark_dispatched(ref)
    ledger.settle(ref, .5)
    benchmark = benchmark_suite()
    sources = []
    for split, repeats, cost in (("dev", 1, .001), ("test", 2, .002)):
        ids = [case["id"] for case in benchmark["cases"] if case["split"] == split]
        records = [{"config_id": cfg["id"], "case_id": cid, "repeat": rep,
                    "estimated_cost_usd": cost, "steps": [], "grade": {"task_success": True}}
                   for cfg in frozen["configs"] for cid in ids for rep in range(repeats)]
        sources.append({"id": "dev-v2" if split == "dev" else "test-v2", "schema_version": 2, "status": "complete",
                        "benchmark": deepcopy(benchmark), "benchmark_hash": frozen["benchmark_hash"],
                        "configs": deepcopy(frozen["configs"]), "case_ids": ids, "records": records,
                        "protocol": {"split": "dev", "max_turns": 8, "repeats": 1} if split == "dev" else deepcopy(frozen["experiments"][0]["protocol"]),
                        "provenance": {"kind": "native_responses_tool_execution", "prompts": deepcopy(frozen["prompt_snapshots"]), "engine_hash": frozen["engine_hash"]},
                        "budget_snapshot": ledger.snapshot()})
    return sources, ledger, frozen, amendment


def check(inputs):
    sources, ledger, frozen, amendment = inputs
    return gate.check_horizon_budget(*sources, ledger.export(), frozen, amendment)


def test_exact_forecast_formula_and_safety_buffer(inputs):
    result = check(inputs)
    assert result["allow_start"] is True
    assert result["projected_long_usd"] == pytest.approx(96 * .008 * 1.5 * 1.25 + .000004608)
    assert result["projected_total_with_buffer_usd"] == pytest.approx(2.190004608)
    assert result["margin_to_cap_usd"] == pytest.approx(2.559995392)
    assert all(row["chosen_mean_source"] == "primary" for row in result["config_cost_sources"])
    assert result["quality_scores_used"] is False
    assert result["forecast_is_a_cost_upper_bound"] is False


def test_quality_outcomes_are_not_launch_inputs(inputs):
    before = check(inputs)
    for source in inputs[0]:
        for row in source["records"]:
            row["grade"] = {"task_success": False, "attempted_violations": 999}
    after = check(inputs)
    assert after["allow_start"] == before["allow_start"] is True
    assert after["projected_long_usd"] == before["projected_long_usd"]


@pytest.mark.parametrize("condition", ["running", "unknown_episode_cost", "active", "unknown_charge", "halted", "expensive"])
def test_valid_insufficient_evidence_withholds_launch(inputs, condition):
    sources, ledger, _, _ = inputs
    if condition == "running":
        sources[1]["status"] = "running"
        sources[1]["records"].pop()
    elif condition == "unknown_episode_cost":
        sources[1]["records"][0]["estimated_cost_usd"] = None
    elif condition == "active":
        ledger.reserve("active", .1)
    elif condition == "unknown_charge":
        ref = ledger.reserve("unknown", .1)
        ledger.mark_dispatched(ref)
        ledger.mark_unknown(ref, "timeout")
    elif condition == "halted":
        ledger.halt("bound invalid")
    elif condition == "expensive":
        for row in sources[1]["records"]:
            row["estimated_cost_usd"] = .02
    result = check(inputs)
    assert result["allow_start"] is False and result["reasons"]


@pytest.mark.parametrize("mutation", ["prompt", "engine", "missing", "duplicate", "short_suite", "bad_frozen_hash", "late_amendment", "unbudgeted_step"])
def test_invalid_evidence_is_rejected(inputs, mutation):
    sources, _, frozen, amendment = inputs
    primary = sources[1]
    if mutation == "prompt":
        primary["provenance"]["prompts"]["direct"] = "changed"
    elif mutation == "engine":
        primary["provenance"]["engine_hash"] = "changed"
    elif mutation == "missing":
        primary["records"].pop()
    elif mutation == "duplicate":
        primary["records"].append(deepcopy(primary["records"][0]))
    elif mutation == "short_suite":
        sources[0]["case_ids"].pop()
    elif mutation == "bad_frozen_hash":
        frozen["engine_hash"] = "forged"
    elif mutation == "late_amendment":
        amendment["updated_at_utc"] = "2026-10-30T00:00:00Z"
    elif mutation == "unbudgeted_step":
        primary["records"][0]["steps"] = [{"request_key": "unpaid-step"}]
    with pytest.raises((ContractError, ValueError)):
        check(inputs)


def test_cli_exit_codes_and_optional_receipt(inputs, tmp_path):
    sources, ledger, frozen, amendment = inputs
    values = dict(development=sources[0], primary=sources[1], budget=ledger.export(), frozen=frozen, amendment=amendment)
    args = []
    for name, value in values.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        args += ["--" + name, str(path)]
    output = tmp_path / "decision.json"
    assert gate.main(args + ["--output", str(output)]) == 0
    assert json.loads(output.read_text())["allow_start"] is True
    values["primary"]["status"] = "running"
    (tmp_path / "primary.json").write_text(json.dumps(values["primary"]))
    assert gate.main(args) == 2
    (tmp_path / "primary.json").write_text('{"broken":true}')
    assert gate.main(args) == 3
