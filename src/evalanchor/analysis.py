"""Paired, group-weighted evidence analysis with conservative release decisions.

Repeated outputs and cases sharing group_id are never independent sample units.
Bootstrap intervals are descriptive. Formal inference requires an explicitly
claimed sampling design; that claim is not statistical proof of independence.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
import random
from statistics import mean, median

from . import contracts


def default_policy() -> dict:
    return {
        "alpha": 0.05,
        "margin": 0.05,
        "required_tags": [],
        "critical_tags": ["critical"],
        "assume_independent_groups": False,
        "min_groups": 10,
        "bootstrap_samples": 2000,
        "seed": 20261029,
        "max_error_rate": 0.0,
    }


def _policy(custom: dict | None) -> dict:
    policy = default_policy()
    if custom is not None:
        if not isinstance(custom, dict):
            raise contracts.ContractError("Policy must be an object")
        unknown = set(custom) - set(policy)
        if unknown:
            raise contracts.ContractError(f"Unknown policy fields: {sorted(unknown)}")
        policy.update(deepcopy(custom))
    for key in ("alpha", "margin", "max_error_rate"):
        value = policy[key]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise contracts.ContractError(f"Policy {key} must be finite")
    if not 0 < policy["alpha"] < 1:
        raise contracts.ContractError("Policy alpha must be in (0,1)")
    if not 0 <= policy["margin"] <= 1 or not 0 <= policy["max_error_rate"] <= 1:
        raise contracts.ContractError("Policy margin and max_error_rate must be in [0,1]")
    for key, low, high in (("min_groups", 2, 10000000), ("bootstrap_samples", 100, 100000)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise contracts.ContractError(f"Policy {key} must be an integer in [{low},{high}]")
    if type(policy["seed"]) is not int or type(policy["assume_independent_groups"]) is not bool:
        raise contracts.ContractError("Policy seed must be integer and independence declaration boolean")
    for key in ("required_tags", "critical_tags"):
        tags = policy[key]
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags) or len(tags) != len(set(tags)):
            raise contracts.ContractError(f"Policy {key} must contain unique non-empty strings")
    return policy


def _quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _interval_decision(lower: float, upper: float, margin: float) -> str:
    if lower >= -margin:
        return "pass"
    if upper < -margin:
        return "fail"
    return "inconclusive"


def _summarize(pairs: list[dict], policy: dict, alpha: float, tag: str | None = None) -> dict:
    # Equal repeat means per case, equal case means per group, equal group means.
    # This guards against both generation and template pseudoreplication.
    grouped = defaultdict(lambda: defaultdict(list))
    for pair in pairs:
        grouped[pair["group_id"]][pair["case_id"]].append(pair)
    groups = []
    for group_id in sorted(grouped):
        cases = grouped[group_id]
        b = mean(mean(p["baseline_score"] for p in rows) for rows in cases.values())
        c = mean(mean(p["candidate_score"] for p in rows) for rows in cases.values())
        groups.append({"group_id": group_id, "n_cases": len(cases), "n_pairs": sum(map(len, cases.values())),
                       "baseline_mean": b, "candidate_mean": c, "delta": c - b})
    n = len(groups)
    base = {"tag": tag, "n_groups": n, "n_cases": len({p["case_id"] for p in pairs}),
            "n_pairs": len(pairs), "groups": groups, "weighting": "equal_groups_equal_cases_equal_repeats"}
    if not n:
        return {**base, "baseline_mean": None, "candidate_mean": None, "delta": None,
                "formal_interval": None, "bootstrap_interval": None, "conditional_decision": "inconclusive",
                "insufficient_groups": True}
    b = mean(g["baseline_mean"] for g in groups)
    c = mean(g["candidate_mean"] for g in groups)
    differences = [g["delta"] for g in groups]
    delta = mean(differences)
    radius = math.sqrt(2.0 * math.log(2.0 / alpha) / n)
    low, high = max(-1.0, delta - radius), min(1.0, delta + radius)
    rng = random.Random(policy["seed"])
    draws = [math.fsum(rng.choice(differences) for _ in range(n)) / n
             for _ in range(policy["bootstrap_samples"])]
    ci = {"lower": low, "upper": high, "radius": radius, "alpha": alpha,
          "method": "hoeffding_bonferroni", "unit": "group", "requires_independent_groups": True}
    bootstrap = {"lower": _quantile(draws, policy["alpha"] / 2),
                 "upper": _quantile(draws, 1 - policy["alpha"] / 2),
                 "method": "paired_group_percentile", "descriptive_only": True,
                 "alpha": policy["alpha"], "samples": policy["bootstrap_samples"], "seed": policy["seed"],
                 "degenerate": len(set(differences)) == 1}
    return {**base, "baseline_mean": b, "candidate_mean": c, "delta": delta,
            "formal_interval": ci, "bootstrap_interval": bootstrap,
            "conditional_decision": _interval_decision(low, high, policy["margin"]) if n >= policy["min_groups"] else "inconclusive",
            "insufficient_groups": n < policy["min_groups"]}


def _resources(run: dict) -> dict:
    records = run["records"]
    costs = [r.get("cost_usd") for r in records]
    known_costs = [v for v in costs if v is not None]
    latency = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    token_keys = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")
    token_summary = {}
    for key in token_keys:
        values = [r.get("usage", {}).get(key) for r in records if isinstance(r.get("usage", {}), dict)]
        known = [v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0]
        if known:
            token_summary[key] = {"known_subtotal": math.fsum(known), "known_records": len(known),
                                  "unknown_records": len(records) - len(known)}
    return {"cost": {"total_usd": math.fsum(known_costs) if len(known_costs) == len(records) else None,
                     "known_subtotal_usd": math.fsum(known_costs), "known_records": len(known_costs),
                     "unknown_records": len(records) - len(known_costs)},
            "latency_ms": {"mean": mean(latency) if latency else None, "median": median(latency) if latency else None,
                           "p95": _quantile(latency, 0.95) if latency else None,
                           "known_records": len(latency), "unknown_records": len(records) - len(latency)},
            "usage": token_summary,
            "note": "Measured run resources; recorded latency is descriptive and may depend on retries, caching, or concurrency."}


def compare_runs(baseline: dict, candidate: dict, policy: dict | None = None) -> dict:
    """Compare complete compatible runs. Invalid evidence raises ContractError.

    A fail can come from a concrete finite-suite critical/error contract violation
    even without a population sampling claim. A statistical pass/fail is conditional
    on an explicit independent-group sampling declaration and preset policy.
    """
    contracts.assert_comparable(baseline, candidate)
    policy = _policy(policy)
    right = {(r["case_id"], r["repeat"]): r for r in candidate["records"]}
    pairs = []
    for left in sorted(baseline["records"], key=lambda r: (r["case_id"], r["repeat"])):
        r = right[(left["case_id"], left["repeat"])]
        pairs.append({"case_id": left["case_id"], "repeat": left["repeat"], "group_id": left["group_id"],
                      "tags": list(left["tags"]), "input_hash": left["input_hash"],
                      "baseline_score": left["score"], "candidate_score": r["score"],
                      "delta": r["score"] - left["score"],
                      "baseline_output": left["output"], "candidate_output": r["output"],
                      "baseline_status": left["status"], "candidate_status": r["status"],
                      "baseline_reason": left.get("reason", ""), "candidate_reason": r.get("reason", ""),
                      "baseline_error": left.get("error"), "candidate_error": r.get("error")})
    tags = sorted({tag for pair in pairs for tag in pair["tags"]} | set(policy["required_tags"]))
    family_size = 1 + len(tags)
    adjusted_alpha = policy["alpha"] / family_size
    overall = _summarize(pairs, policy, adjusted_alpha)
    slices = [_summarize([p for p in pairs if tag in p["tags"]], policy, adjusted_alpha, tag) for tag in tags]
    required = [overall] + [s for s in slices if s["tag"] in policy["required_tags"]]
    critical_pairs = [p for p in pairs if set(p["tags"]) & set(policy["critical_tags"])]
    critical = [p for p in critical_pairs if p["delta"] < 0]
    baseline_errors = sum(p["baseline_status"] == "error" for p in pairs)
    candidate_errors = sum(p["candidate_status"] == "error" for p in pairs)
    count = len(pairs)
    baseline_error_rate = baseline_errors / count
    candidate_error_rate = candidate_errors / count
    sampling = {name: deepcopy(run.get("provenance", {}).get("sampling", {}))
                for name, run in (("baseline", baseline), ("candidate", candidate))}
    declarations_match = all(isinstance(s, dict) and s.get("independent_groups") is True for s in sampling.values())
    inference_enabled = policy["assume_independent_groups"] and declarations_match
    reasons, warnings = [], []
    if not inference_enabled:
        reasons.append("Population inference is not enabled: policy and both run sampling declarations must explicitly claim independent groups; declarations are not proof.")
    if any(s["insufficient_groups"] for s in required):
        reasons.append("Overall or a required slice has fewer than min_groups independent group identifiers (a missing required slice also blocks passage).")
    fixed_fail = False
    if critical:
        fixed_fail = True
        reasons.append(f"Finite-suite critical contract regression: {len(critical)} paired observations decreased on critical tags.")
    if candidate_error_rate > policy["max_error_rate"]:
        fixed_fail = True
        reasons.append(f"Candidate error rate {candidate_error_rate:.6f} exceeds the fixed acceptance limit {policy['max_error_rate']:.6f}; errors remain in the denominator.")
    if baseline_error_rate > policy["max_error_rate"]:
        reasons.append("Baseline errors exceed the acceptance limit; an error-degraded baseline cannot certify passage.")
    if fixed_fail:
        decision = "fail"
        decision_basis = "finite_suite_acceptance_contract"
    elif inference_enabled and any(s["conditional_decision"] == "fail" for s in required):
        decision = "fail"
        decision_basis = "conditional_population_inference"
        reasons.append("At least one required comparison has its adjusted upper bound below the allowed regression margin.")
    elif inference_enabled and baseline_error_rate <= policy["max_error_rate"] and all(s["conditional_decision"] == "pass" for s in required):
        decision = "pass"
        decision_basis = "conditional_population_inference"
        reasons.append("All required adjusted lower bounds satisfy the predeclared non-inferiority margin, conditional on the sampling assumptions.")
    else:
        decision = "inconclusive"
        decision_basis = "insufficient_evidence"
        if not all(s["conditional_decision"] == "pass" for s in required):
            reasons.append("The evidence does not establish all required non-inferiority claims; absence of detected regression is not passage.")
    warnings.extend([
        "Bootstrap intervals are descriptive approximations and never determine the release decision.",
        "Group identifiers and hashes verify organization/identity, not independence, representative sampling, correctness, or absence of training contamination.",
        "Coverage assumes the policy, sample size, groups and slices were fixed before inspecting outcomes; repeated peeking or tuning until passage is unsupported.",
        "A pass concerns these scores and the specified tolerance, not global model superiority or production safety.",
    ])
    if overall["bootstrap_interval"]["degenerate"]:
        warnings.append("Observed group differences are constant: the bootstrap interval degenerates, while the formal interval remains nonzero-width.")
    if any(s.get("kind") == "synthetic_contract_suite" for s in sampling.values() if isinstance(s, dict)):
        if all(r.get("provenance", {}).get("kind") == "synthetic_fixture" for r in (baseline, candidate)):
            warnings.append("Synthetic fixtures validate the evaluation software and finite-suite contracts; they are not measured model capabilities or IID population evidence.")
        else:
            warnings.append("Synthetic scenario inputs measure only the observed outputs on these finite contracts; they do not establish general model capability or IID population evidence.")
    return {"schema_version": 1, "baseline_id": baseline["id"], "candidate_id": candidate["id"],
            "decision": decision, "decision_basis": decision_basis, "reasons": reasons, "warnings": warnings,
            "policy": policy, "overall": overall, "slices": slices, "pairs": pairs,
            "quality": {"n_pairs": count, "baseline_errors": baseline_errors, "candidate_errors": candidate_errors,
                        "baseline_error_rate": baseline_error_rate, "candidate_error_rate": candidate_error_rate,
                        "critical_pairs": len(critical_pairs), "critical_cases": len({p["case_id"] for p in critical_pairs}),
                        "critical_groups": len({p["group_id"] for p in critical_pairs}),
                        "critical_regression_pairs": len(critical), "critical_regression_cases": len({p["case_id"] for p in critical}),
                        "critical_regression_groups": len({p["group_id"] for p in critical}),
                        "critical_regressions": [{k: p[k] for k in ("case_id", "repeat", "group_id", "tags", "baseline_score", "candidate_score", "delta")} for p in critical]},
            "resources": {"baseline": _resources(baseline), "candidate": _resources(candidate)},
            "assumptions": {"inference_enabled": inference_enabled, "independence_is_proven": False,
                            "sampling": sampling, "family_size": family_size, "family_alpha": policy["alpha"],
                            "per_comparison_alpha": adjusted_alpha, "formal_unit": "group",
                            "fixed_policy_required": True,
                            "zero_difference_groups_needed_for_margin": math.ceil(2 * math.log(2 / adjusted_alpha) / policy["margin"] ** 2) if policy["margin"] > 0 else None},
            "evidence": {"baseline_fingerprint": contracts.fingerprint(baseline),
                         "candidate_fingerprint": contracts.fingerprint(candidate),
                         "policy_fingerprint": contracts.fingerprint(policy),
                         "suite_fingerprint": baseline["suite"]["fingerprint"],
                         "protocol_fingerprint": baseline["protocol"]["fingerprint"]}}
