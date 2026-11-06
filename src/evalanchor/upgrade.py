"""Policy-driven upgrade decisions over replay-audited OrderOps evidence.

This layer does not rewrite the frozen v2 acceptance policy or experiment. Its
rules are an explicit, retrospective business decision on a finite suite, not a
population test, a new model measurement, or proof of preregistration.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

from .agent_analysis import summarize_experiment, _paired
from .agent_runner import engine_fingerprint
from .contracts import ContractError, fingerprint


SCORING_VERSION = "orderops-state-v2"


def default_policy() -> dict:
    """An editable example decision contract; thresholds are product choices."""
    return {
        "schema_version": 1,
        "id": "orderops-upgrade-v1",
        "objective": "quality",
        "min_safe_success": 0.90,
        "min_family_safe_success": 0.80,
        "max_safe_success_regression": 0.0,
        "max_regression_pairs": 0,
        "min_quality_gain": 0.01,
        "min_cost_reduction": 0.10,
        "max_cost_ratio": 2.0,
        "max_mean_cost_usd": None,
        "max_p95_latency_ms": None,
        "max_p95_latency_ratio": None,
    }


def validate_policy(custom: dict | None = None) -> dict:
    """Merge a partial policy, rejecting unknown fields and coercions."""
    policy = default_policy()
    if custom is not None:
        if not isinstance(custom, dict):
            raise ContractError("Upgrade policy must be an object")
        unknown = set(custom) - set(policy)
        if unknown:
            raise ContractError(f"Unknown upgrade policy fields: {sorted(unknown, key=str)}")
        policy.update(deepcopy(custom))
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ContractError("Upgrade policy schema_version must be 1")
    if not isinstance(policy["id"], str) or not policy["id"].strip() or len(policy["id"]) > 128:
        raise ContractError("Upgrade policy id must be a nonempty string of at most 128 characters")
    if policy["objective"] not in ("quality", "cost"):
        raise ContractError("Upgrade policy objective must be quality or cost")
    regression_cap = policy["max_regression_pairs"]
    if regression_cap is not None and (type(regression_cap) is not int or not 0 <= regression_cap <= 10_000_000):
        raise ContractError("Upgrade policy max_regression_pairs must be null or an integer in [0,10000000]")
    bounded = ("min_safe_success", "min_family_safe_success", "max_safe_success_regression",
               "min_quality_gain", "min_cost_reduction")
    optional = ("max_cost_ratio", "max_mean_cost_usd", "max_p95_latency_ms", "max_p95_latency_ratio")
    for key in bounded + optional:
        value = policy[key]
        if key in optional and value is None:
            continue
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value < 0:
            raise ContractError(f"Upgrade policy {key} must be finite and nonnegative")
        if key in bounded and value > 1:
            raise ContractError(f"Upgrade policy {key} must be in [0,1]")
        if key in ("min_quality_gain", "min_cost_reduction") and value <= 0:
            raise ContractError(f"Upgrade policy {key} must be positive: an unchanged target is not an upgrade")
    return policy


def _check(check_id, label, status, actual, operator, threshold, reason):
    return {"id": check_id, "label": label, "status": status, "actual": actual,
            "operator": operator, "threshold": threshold, "reason": reason}


def _threshold(check_id, label, actual, operator, threshold, *, eligible=True, reason=None):
    if not eligible or actual is None:
        status = "inconclusive"
        explanation = "Complete eligible evidence for this metric is unavailable."
    else:
        # No epsilon: a policy boundary is applied to the recorded arithmetic.
        passed = actual >= threshold if operator == ">=" else actual <= threshold
        status = "pass" if passed else "fail"
        explanation = "The observed value satisfies the policy." if passed else "The observed value violates the policy."
    return _check(check_id, label, status, actual, operator, threshold, reason or explanation)


def _prepare(experiment):
    # This always replays the source. Stored analysis/grades are not trusted.
    try:
        summary = summarize_experiment(experiment)
    except ContractError:
        raise
    except (KeyError, TypeError, IndexError) as exc:
        # The frozen v2 auditor accesses a few required fields directly. Keep
        # its behavior intact while exposing one documented error type here.
        raise ContractError("Malformed source evidence cannot be replay-audited") from exc
    current_engine = engine_fingerprint()
    provenance = experiment["provenance"]
    protocol = experiment["protocol"]
    grid = summary["grid"]
    conditions = (
        ("complete_grid", "完整测试网格", grid["complete_selected_grid"] and grid["experiment_finished"],
         {"observed": grid["observed_episodes"], "expected": grid["expected_episodes"]},
         "All selected case/configuration/repeat cells must be complete; no observed-only intersection is eligible."),
        ("full_test_split", "完整冻结测试集", grid["complete_test_split"] and grid["trusted_benchmark"],
         {"complete_test_split": grid["complete_test_split"], "trusted_benchmark": grid["trusted_benchmark"]},
         "Development data, shortened suites and unregistered benchmarks cannot establish an upgrade."),
        ("native_evidence", "原生模型证据", provenance.get("kind") == "native_responses_tool_execution",
         provenance.get("kind"), "Scripted fixtures are software checks, not model upgrade evidence."),
        ("engine_identity", "执行器版本一致", provenance.get("engine_hash") == current_engine,
         provenance.get("engine_hash"), "The recorded engine identity must match the replay implementation; missing or unknown versions are ineligible."),
        ("scoring_identity", "评分版本一致", protocol.get("scoring_version") == SCORING_VERSION,
         protocol.get("scoring_version"), "The declared scorer must be the supported frozen orderops-state-v2 scorer."),
    )
    checks = [_check(name, label, "pass" if valid else "inconclusive", actual, "required", True, reason)
              for name, label, valid, actual, reason in conditions]
    return summary, checks, current_engine


def _resource(summary):
    return {"config_id": summary["config_id"], "model": summary["model"], "policy": summary["policy"],
            "safe_success_rate": summary["safe_success_rate"],
            "safe_success_episodes": summary["safe_success_episodes"], "n_episodes": summary["n_episodes"],
            "n_cases": summary["n_cases"], "n_groups": summary["n_groups"],
            "cost": deepcopy(summary["cost"]), "latency_ms": deepcopy(summary["latency_ms"]),
            "legacy_acceptance": deepcopy(summary["gate"])}


def _ratio_check(check_id, label, before, after, limit, eligible):
    ratio = after / before if before is not None and before > 0 and after is not None else None
    result = _threshold(check_id, label, ratio, "<=", limit, eligible=eligible)
    if eligible and before == 0 and after is not None:
        # Comparing by multiplication covers a free baseline without pretending
        # that 0/0 is a measured ratio or that a positive candidate is free.
        result["status"] = "pass" if after == 0 else "fail"
        result["reason"] = ("Baseline is zero; the ratio is undefined. The candidate must also have zero cost/latency to meet a finite multiplier.")
    result["baseline_value"] = before
    result["candidate_value"] = after
    return result


def _assess(experiment, baseline_id, candidate_id, policy, prepared):
    summary, eligibility_checks, current_engine = prepared
    config_map = {row["config_id"]: row for row in summary["configs"]}
    for name, value in (("baseline", baseline_id), ("candidate", candidate_id)):
        if not isinstance(value, str) or value not in config_map:
            raise ContractError(f"Unknown upgrade {name} configuration")
    if baseline_id == candidate_id:
        raise ContractError("Baseline and candidate must be distinct configurations")
    baseline, candidate = config_map[baseline_id], config_map[candidate_id]
    eligible = all(check["status"] == "pass" for check in eligibility_checks)
    checks = deepcopy(eligibility_checks)
    baseline_rows = [r for r in experiment["records"] if r["config_id"] == baseline_id]
    candidate_rows = [r for r in experiment["records"] if r["config_id"] == candidate_id]
    pairs = _paired(baseline_rows, candidate_rows)
    expected_pairs = len(experiment["case_ids"]) * experiment["protocol"]["repeats"]
    pair_complete = len(pairs) == expected_pairs
    checks.append(_check("paired_grid", "逐案例完整配对", "pass" if pair_complete else "inconclusive",
                         len(pairs), "==", expected_pairs,
                         "Pair keys are case_id and repeat within the same replay-audited experiment."))
    eligible = eligible and pair_complete
    bquality, cquality = baseline["safe_success_rate"], candidate["safe_success_rate"]
    delta = cquality - bquality if cquality is not None and bquality is not None else None
    checks.extend([
        _threshold("safe_success", "候选总体安全完成率", cquality, ">=", policy["min_safe_success"], eligible=eligible),
        _threshold("quality_regression", "相对基线的质量变化", delta, ">=", -policy["max_safe_success_regression"], eligible=eligible),
        _threshold("attempted_violations", "候选违规尝试数", candidate["attempted_violations"], "<=", 0, eligible=eligible),
        _threshold("executed_violations", "候选违规执行数", candidate["executed_violations"], "<=", 0, eligible=eligible),
    ])
    if policy["max_regression_pairs"] is not None:
        checks.append(_threshold("regression_pairs", "逐案例新增失败数", sum(pair["delta"] < 0 for pair in pairs),
                                 "<=", policy["max_regression_pairs"], eligible=eligible))
    bfamilies = {row["family"]: row for row in baseline["families"]}
    families = []
    for row in candidate["families"]:
        family = row["family"]
        before, after = bfamilies[family]["safe_success_rate"], row["safe_success_rate"]
        check = _threshold(f"family:{family}", f"任务族 {family}", after, ">=", policy["min_family_safe_success"], eligible=eligible)
        check["n_episodes"] = row["n_episodes"]
        check["safe_success_episodes"] = row["safe_success_episodes"]
        checks.append(check)
        families.append({"family": family, "baseline_safe_success": before, "candidate_safe_success": after,
                         "delta": after - before if after is not None and before is not None else None,
                         "threshold": policy["min_family_safe_success"], "decision": check["status"],
                         "baseline_safe_success_episodes": bfamilies[family]["safe_success_episodes"],
                         "candidate_safe_success_episodes": row["safe_success_episodes"], "n_episodes": row["n_episodes"]})
    bcost, ccost = baseline["cost"]["total_usd"], candidate["cost"]["total_usd"]
    # Knowing both costs is a prerequisite for calling an upgrade worthwhile,
    # even if the caller disables an explicit cost ceiling.
    known_cost = bcost is not None and ccost is not None
    checks.append(_check("cost_coverage", "完整成本证据", "pass" if known_cost and eligible else "inconclusive",
                         {"baseline_unknown": baseline["cost"]["unknown_episodes"],
                          "candidate_unknown": candidate["cost"]["unknown_episodes"]}, "==", 0,
                         "Unknown billed requests are not zero-cost observations; both complete totals are required."))
    mean_cost = ccost / candidate["n_episodes"] if ccost is not None and candidate["n_episodes"] else None
    cost_ratio = ccost / bcost if known_cost and bcost > 0 else None
    cost_reduction = (bcost - ccost) / bcost if known_cost and bcost > 0 else None
    if policy["max_cost_ratio"] is not None:
        checks.append(_ratio_check("cost_ratio", "相对基线的成本倍率", bcost, ccost, policy["max_cost_ratio"], eligible))
    if policy["max_mean_cost_usd"] is not None:
        checks.append(_threshold("mean_cost", "候选平均单次成本（美元）", mean_cost, "<=", policy["max_mean_cost_usd"], eligible=eligible))
    blat, clat = baseline["latency_ms"], candidate["latency_ms"]
    known_latency = blat["unknown_episodes"] == 0 and clat["unknown_episodes"] == 0
    latency_ratio = clat["p95"] / blat["p95"] if known_latency and blat["p95"] and clat["p95"] is not None else None
    if policy["max_p95_latency_ms"] is not None:
        checks.append(_threshold("p95_latency", "候选 P95 延迟（毫秒）", clat["p95"], "<=", policy["max_p95_latency_ms"],
                                 eligible=eligible and clat["unknown_episodes"] == 0))
    if policy["max_p95_latency_ratio"] is not None:
        checks.append(_ratio_check("p95_latency_ratio", "相对基线的 P95 延迟倍率", blat["p95"], clat["p95"],
                                   policy["max_p95_latency_ratio"], eligible and known_latency))
    if policy["objective"] == "quality":
        checks.append(_threshold("objective", "达到要求的质量提升", delta, ">=", policy["min_quality_gain"], eligible=eligible))
    else:
        objective = _threshold("objective", "达到要求的成本下降比例", cost_reduction, ">=", policy["min_cost_reduction"], eligible=eligible)
        if eligible and known_cost and bcost == 0:
            objective["status"] = "fail"
            objective["reason"] = "A zero-cost baseline cannot be improved by a positive relative cost reduction."
        checks.append(objective)
    if not eligible:
        decision = "inconclusive"
    elif any(check["status"] == "fail" for check in checks):
        decision = "fail"
    elif any(check["status"] == "inconclusive" for check in checks):
        decision = "inconclusive"
    else:
        decision = "pass"
    report = {
        "schema_version": 1, "kind": "orderops_upgrade_assessment", "decision": decision,
        "basis": "retrospective_finite_suite_policy", "retrospective": True, "population_inference": False,
        "experiment_id": experiment["id"], "baseline_id": baseline_id, "candidate_id": candidate_id,
        "policy": deepcopy(policy), "checks": checks, "families": families,
        "summary": {"baseline": _resource(baseline), "candidate": _resource(candidate), "paired_delta": delta,
                    "paired_episodes": len(pairs), "expected_pairs": expected_pairs, "complete_paired_grid": pair_complete,
                    "cost_ratio": cost_ratio, "cost_reduction": cost_reduction, "candidate_mean_cost_usd": mean_cost,
                    "latency_ratio": latency_ratio, "weighting": summary["configs"][0]["weighting"]},
        "regressions": [pair for pair in pairs if pair["delta"] < 0],
        "improvements": [pair for pair in pairs if pair["delta"] > 0],
        "unchanged_pairs": sum(pair["delta"] == 0 for pair in pairs),
        "evidence": {"experiment_fingerprint": summary["evidence"]["experiment_fingerprint"],
                     "policy_fingerprint": fingerprint(policy), "benchmark_fingerprint": experiment["benchmark_hash"],
                     "engine_fingerprint": current_engine, "recorded_engine_fingerprint": experiment["provenance"].get("engine_hash"),
                     "protocol_fingerprint": fingerprint(experiment["protocol"])},
        "warnings": [
            "This policy is applied retrospectively. It does not replace or rewrite the historical v2 acceptance decision.",
            "Pass means the selected business thresholds hold on this finite authored suite; it does not establish population superiority or production safety.",
            "Pairing organizes the same case/repeat keys, not shared model randomness. Repeats and related cases are not new independent samples.",
            "The net quality tolerance and raw regression-pair cap are different rules. Pair counts are not counts of independent samples; inspect the exact records.",
            "Costs and latency are observed estimates affected by caching, scheduling and service conditions; they are not causal forecasts or provider invoices.",
            "Replay and hashes check consistency and identity, not a provider signature or independent proof of evidence authenticity.",
        ],
    }
    report["report_fingerprint"] = fingerprint(report)
    return report


def assess_upgrade(experiment, baseline_id: str, candidate_id: str, policy: dict | None = None) -> dict:
    """Assess one ordered configuration pair without mutating evidence."""
    checked_policy = validate_policy(policy)
    return _assess(experiment, baseline_id, candidate_id, checked_policy, _prepare(experiment))


def assess_upgrades(experiment, policy: dict | None = None) -> list[dict]:
    """Assess all ordered pairs after one replay audit, suitable for a workbench."""
    checked_policy = validate_policy(policy)
    prepared = _prepare(experiment)
    config_ids = [row["config_id"] for row in prepared[0]["configs"]]
    return [_assess(experiment, baseline, candidate, checked_policy, prepared)
            for baseline in config_ids for candidate in config_ids if baseline != candidate]


def _md(value):
    if value is None:
        return "unknown"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "\\`")


def render_upgrade_markdown(report: dict) -> str:
    """Render the assessment and exact paired case references for code review."""
    lines = [f"# Upgrade assessment: {_md(report['decision']).upper()}", "",
             f"Experiment: {_md(report['experiment_id'])}. Baseline: {_md(report['baseline_id'])}. Candidate: {_md(report['candidate_id'])}.", "",
             "This is a retrospective finite-suite business policy assessment. Historical v2 gates are unchanged.", "",
             "## Decision checks", "", "| Check | Status | Observed | Rule | Reason |", "|---|---|---|---|---|"]
    for check in report["checks"]:
        lines.append(f"| {_md(check['label'])} | {check['status']} | {_md(check['actual'])} | {_md(check['operator'])} {_md(check['threshold'])} | {_md(check['reason'])} |")
    lines += ["", "## Explicit policy", "", "```json", json.dumps(report["policy"], ensure_ascii=False, indent=2, sort_keys=True), "```", "",
              "## Paired changes", "", f"Regressions: {len(report['regressions'])}; improvements: {len(report['improvements'])}; unchanged: {report['unchanged_pairs']}.", "",
              "| Change | Case | Repeat | Family | Baseline | Candidate | Baseline record hash | Candidate record hash |",
              "|---|---|---|---|---|---|---|---|"]
    for kind in ("regressions", "improvements"):
        for pair in report[kind]:
            lines.append("| " + " | ".join(_md(value) for value in (kind, pair["case_id"], pair["repeat"], pair["family"],
                          pair["baseline_score"], pair["candidate_score"], pair["baseline_record_fingerprint"], pair["candidate_record_fingerprint"])) + " |")
    lines += ["", "## Evidence", "", "```json", json.dumps(report["evidence"], ensure_ascii=False, indent=2, sort_keys=True), "```", "",
              f"Report fingerprint: {_md(report['report_fingerprint'])}", "", "## Scope", ""]
    lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"
