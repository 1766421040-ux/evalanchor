#!/usr/bin/env python3
"""Cost-only preregistered launch check; no model calls and no score selection.

Exit 0 permits the optional complete 12-turn matrix, 2 withholds permission on
valid but insufficient/unfinished evidence, and 3 rejects invalid evidence.
This planning forecast is not a cost upper bound. The persistent request ledger
must still reserve every dispatched call and can stop an unfinished matrix.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evalanchor.agent_runner import audit_experiment
from evalanchor.contracts import ContractError, canonical, fingerprint, read_json
from evalanchor.orderops import benchmark_suite
from scripts.verify_v2 import VerificationError, _cost_units, verify_budget


def require(condition, message):
    if not condition:
        raise ContractError(message)


def _money(value, name):
    require(type(value) in (int, float), f"{name} must be a finite numeric USD amount")
    amount = Decimal(str(value))
    require(amount.is_finite() and amount >= 0, f"{name} must be finite and nonnegative")
    return amount


def _profile(experiment, config_ids, expected_per_config):
    profiles = []
    for cid in config_ids:
        rows = [row for row in experiment["records"] if row["config_id"] == cid]
        costs = [_money(row["estimated_cost_usd"], "episode estimated cost") for row in rows if row.get("estimated_cost_usd") is not None]
        complete = len(rows) == expected_per_config and len(costs) == len(rows)
        profiles.append({"config_id": cid, "observed_episodes": len(rows), "expected_episodes": expected_per_config,
                         "unknown_cost_episodes": len(rows) - len(costs),
                         "mean": sum(costs, Decimal(0)) / len(rows) if rows and complete else None})
    return profiles


def check_horizon_budget(development, primary, budget, frozen, amendment):
    """Validate complete evidence, then apply exactly the registered cost formula."""
    with localcontext() as context:
        context.prec = 50
        return _check(development, primary, budget, frozen, amendment)


def _check(development, primary, budget, frozen, amendment):
    require(isinstance(frozen, dict) and frozen.get("fingerprint") == fingerprint({k: v for k, v in frozen.items() if k != "fingerprint"}), "Frozen protocol fingerprint mismatch")
    require(isinstance(amendment, dict), "Horizon amendment must be an object")
    require(amendment.get("status") == "preregistered_not_executed", "Unexpected preregistration status")
    require(datetime.fromisoformat(amendment.get("updated_at_utc", amendment["created_at_utc"]).replace("Z", "+00:00")) <= datetime.fromisoformat(frozen["frozen_at"].replace("Z", "+00:00")), "Horizon amendment was not frozen before testing")
    gate = amendment["budget_launch_gate"]
    require(gate["horizon_multiplier"] == 1.5 and gate["extra_forecast_margin"] == 1.25
            and gate["safety_buffer_usd"] == .25 and gate["rounding_headroom_usd"] == .000004608,
            "Registered horizon forecast coefficients changed")
    require(amendment["primary_unchanged"]["expected_episodes"] == 384
            and amendment["secondary_resource_sensitivity"]["expected_episodes"] == 384,
            "Horizon amendment does not describe the complete 384-cell matrices")
    require(gate["ledger_cap_usd"] == frozen["ledger_limit_usd"] == 4.75 and frozen["user_limit_usd"] == 5,
            "Frozen campaign authorization differs from the registered launch gate")
    registry_hash = fingerprint(benchmark_suite())
    require(frozen["benchmark_hash"] == amendment["secondary_resource_sensitivity"]["benchmark_hash"] == registry_hash,
            "Launch plan must use the complete registered OrderOps benchmark")
    plans = {p["id"]: p for p in frozen["experiments"]}
    require(set(plans) == {"test-v2", "test-long-v2"}, "Frozen protocols must name both horizon matrices")
    require(plans["test-v2"]["protocol"]["max_turns"] == 8 and plans["test-long-v2"]["protocol"]["max_turns"] == 12,
            "Frozen horizon limits changed")
    require(primary["id"] == "test-v2" and development["id"] == "dev-v2", "Launch inputs must be the complete development and primary experiments")
    require(primary["protocol"] == plans["test-v2"]["protocol"] and primary["case_ids"] == plans["test-v2"]["case_ids"],
            "Primary test selection or protocol differs from freezing")
    require(primary["provenance"]["prompts"] == frozen["prompt_snapshots"] and primary["provenance"]["engine_hash"] == frozen["engine_hash"],
            "Primary test prompt or engine differs from freezing")
    config_ids = [cfg["id"] for cfg in frozen["configs"]]
    require(len(config_ids) == 4 and len(set(config_ids)) == 4, "Launch forecast requires all four configurations")
    for experiment, split, repeats, expected_cases in ((development, "dev", 1, 24), (primary, "test", 2, 48)):
        require(experiment.get("schema_version") == 2 and experiment.get("status") in ("running", "complete", "interrupted", "failed"), "Invalid experiment schema or state")
        require(experiment["provenance"].get("kind") == "native_responses_tool_execution", "Scripted or unidentified data cannot authorize paid launch")
        require(experiment["benchmark_hash"] == registry_hash and fingerprint(experiment["benchmark"]) == registry_hash, "Experiment benchmark changed")
        require(experiment["configs"] == frozen["configs"], "Development and primary model/configuration matrix must match")
        require(experiment["protocol"]["split"] == split and type(experiment["protocol"]["repeats"]) is int
                and experiment["protocol"]["repeats"] == repeats and experiment["protocol"]["max_turns"] == 8,
                "Cost source must use the registered split, repeats and eight-turn horizon")
        expected_ids = {case["id"] for case in experiment["benchmark"]["cases"] if case["split"] == split}
        require(len(expected_ids) == expected_cases and len(experiment["case_ids"]) == expected_cases and set(experiment["case_ids"]) == expected_ids,
                "A pilot or shortened suite cannot provide the launch cost forecast")
        expected_grid = {(cid, case_id, repeat) for cid in config_ids for case_id in expected_ids for repeat in range(repeats)}
        require(all(type(row.get("repeat")) is int for row in experiment["records"]), "Episode repeat must be an integer")
        observed_grid = {(row["config_id"], row["case_id"], row["repeat"]) for row in experiment["records"]}
        require(len(observed_grid) == len(experiment["records"]) and observed_grid <= expected_grid, "Duplicate or unexpected forecast matrix cell")
        require(experiment["status"] != "complete" or observed_grid == expected_grid, "Complete cost source is missing matrix cells")
        # Replay/grade verification protects evidence integrity; no grade value
        # is read by the launch rule or appears in the cost-only output.
        audit_experiment(experiment)
    budget_audit = verify_budget(budget)
    require(budget["cap_usd"] == frozen["ledger_limit_usd"], "Wrong campaign cap in budget receipt")
    by_key = {attempt["request_key"]: attempt for attempt in budget["attempts"]}
    for experiment in (development, primary):
        snapshot = experiment["budget_snapshot"]
        require(snapshot["campaign"] == budget["campaign"] and snapshot["cap_usd"] == budget["cap_usd"], "Experiment and launch budget use different campaigns")
        require(snapshot["known_spent_usd"] <= budget["known_spent_usd"], "Budget receipt predates known experiment spending")
        for row in experiment["records"]:
            for step in row["steps"]:
                key = step["request_key"]
                require(key in by_key, "Model call absent from launch budget receipt")
                attempt = by_key[key]
                require(attempt["metadata"].get("request_hash") == step["request_hash"], "Launch ledger request hash changed")
                if step.get("estimated_cost_usd") is not None:
                    require(attempt["state"] == "settled" and attempt["receipt"].get("response_hash") == step["response_hash"]
                            and attempt["actual_known_cost_nanodollars"] == _cost_units(step["estimated_cost_usd"]),
                            "Launch ledger cost/response differs from model receipt")
    dev_profile = _profile(development, config_ids, 24)
    test_profile = _profile(primary, config_ids, 96)
    reasons = []
    if development["status"] != "complete" or primary["status"] != "complete":
        reasons.append("Development and primary test must both be complete before launch.")
    if budget["halted"]:
        reasons.append("Campaign is halted.")
    if budget["nanodollars"]["active_reserved"] or budget["nanodollars"]["unknown_reserved"]:
        reasons.append("Active requests or unresolved charges remain in the campaign.")
    if any(profile["mean"] is None for profile in dev_profile + test_profile):
        reasons.append("Every cell of both complete matrices must have a known episode cost.")
    means_known = all(profile["mean"] is not None for profile in dev_profile + test_profile)
    rows, mean_sum = [], Decimal(0)
    for dev, test in zip(dev_profile, test_profile):
        chosen = max(dev["mean"], test["mean"]) if dev["mean"] is not None and test["mean"] is not None else None
        if chosen is not None:
            mean_sum += chosen
        def public(profile):
            return {**{k: v for k, v in profile.items() if k != "mean"},
                    "mean_episode_usd": float(profile["mean"]) if profile["mean"] is not None else None,
                    "mean_episode_usd_decimal": str(profile["mean"]) if profile["mean"] is not None else None}
        rows.append({"config_id": dev["config_id"], "development": public(dev), "primary": public(test),
                     "chosen_mean_usd": float(chosen) if chosen is not None else None,
                     "chosen_mean_source": ("development" if dev["mean"] >= test["mean"] else "primary") if chosen is not None else None})
    projection = Decimal(96) * mean_sum * Decimal("1.5") * Decimal("1.25") + Decimal("0.000004608") if means_known else None
    committed = Decimal(budget["nanodollars"]["committed"]) / Decimal(1_000_000_000)
    cap, buffer = Decimal("4.75"), Decimal("0.25")
    total = committed + projection + buffer if projection is not None else None
    margin = cap - total if total is not None else None
    if margin is not None and margin < 0:
        reasons.append("Projected complete secondary matrix plus safety buffer exceeds remaining commitment capacity.")
    return {"schema_version": 1, "allow_start": not reasons, "decision_basis": "preregistered_cost_only_launch_gate",
            "quality_scores_used": False, "reasons": reasons, "config_cost_sources": rows,
            "ledger_cap_usd": float(cap), "committed_usd": float(committed), "available_after_commitments_usd": float(cap - committed),
            "safety_buffer_usd": float(buffer), "projected_long_usd": float(projection) if projection is not None else None,
            "projected_long_usd_decimal": str(projection) if projection is not None else None,
            "projected_total_with_buffer_usd": float(total) if total is not None else None,
            "margin_to_cap_usd": float(margin) if margin is not None else None,
            "formula": "96 * sum(max(dev_mean_c, primary_mean_c)) * 1.5 * 1.25 + 0.000004608",
            "forecast_is_a_cost_upper_bound": False,
            "warning": "A planning allowance does not guarantee completion. Every future call still requires a conservative ledger reservation; do not shrink/select the secondary matrix by observed scores.",
            "budget_audit": budget_audit,
            "evidence": {"development_fingerprint": fingerprint({k: v for k, v in development.items() if k != "analysis"}),
                         "primary_fingerprint": fingerprint({k: v for k, v in primary.items() if k != "analysis"}),
                         "budget_fingerprint": fingerprint(budget), "frozen_fingerprint": frozen["fingerprint"],
                         "amendment_fingerprint": fingerprint(amendment)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, default=ROOT / "experiments/dev-v2/experiment.json")
    parser.add_argument("--primary", type=Path, default=ROOT / "experiments/test-v2/experiment.json")
    parser.add_argument("--budget", type=Path, default=ROOT / "experiments/test-v2/budget-receipt.json")
    parser.add_argument("--frozen", type=Path, default=ROOT / "artifacts/orderops-v2/frozen-protocol.json")
    parser.add_argument("--amendment", type=Path, default=ROOT / "artifacts/orderops-v2/preregistration-amendment-resource-horizon.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = check_horizon_budget(*(read_json(path) for path in (args.development, args.primary, args.budget, args.frozen, args.amendment)))
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0 if result["allow_start"] else 2
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        print(f"Horizon launch check rejected invalid evidence: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
