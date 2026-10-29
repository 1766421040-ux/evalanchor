#!/usr/bin/env python3
"""Replay released Agent evidence and audit public budget JSON, without API calls.

The default command requires the complete v0.2 release set. --experiment is an
explicit development/subset check and never returns release_verified=true.
SQLite journals, response caches and credentials are neither read nor required.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evalanchor.agent_analysis import summarize_experiment
from evalanchor.agent_runner import audit_experiment
from evalanchor.contracts import canonical, fingerprint, read_json

REQUIRED_EXPERIMENTS = ("pilot-v2", "dev-v2", "dev-terminal-v2", "test-v2", "test-long-v2")
STATES = ("reserved", "dispatched", "unknown", "settled", "cancelled")
SCALE = 1_000_000_000


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def _integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum, f"{name} must be an integer >= {minimum}")
    return value


def _cost_units(value):
    require(type(value) in (int, float), "Cost must be a finite nonnegative number")
    amount = Decimal(str(value))
    require(amount.is_finite() and amount >= 0, "Cost must be finite and nonnegative")
    parts = amount.as_tuple()
    return int(Decimal((parts.sign, parts.digits, parts.exponent + 9)).to_integral_value(rounding=ROUND_CEILING))


def _usd_matches(value, units, label):
    require(type(value) in (int, float) and value == units / SCALE, f"{label}: USD display differs from integer accounting")


def verify_budget(receipt):
    """Rebuild exported attempt states, integer totals and event transitions."""
    require(receipt.get("schema_version") == 1 and receipt.get("currency") == "USD", "Unsupported budget receipt")
    require(receipt.get("accounting_unit") == "nanodollar", "Exact nanodollar accounting is required")
    require(isinstance(receipt.get("attempts"), list) and isinstance(receipt.get("events"), list), "Missing budget attempts/events")
    units = receipt["nanodollars"]
    cap = _integer(units["cap"], "cap", 1)
    require(cap <= 5 * SCALE, "Campaign cap exceeds the authorized $5 ceiling")
    require(type(receipt.get("halted")) is bool, "Budget halted flag must be boolean")
    require(isinstance(receipt.get("campaign"), dict), "Campaign identity is required")
    attempts, keys = {}, set()
    totals = {"known_spent": 0, "active_reserved": 0, "unknown_reserved": 0}
    counts = {state: 0 for state in STATES}
    for attempt in receipt["attempts"]:
        identity, key, state = attempt["id"], attempt["request_key"], attempt["state"]
        require(isinstance(identity, str) and identity and identity not in attempts, "Duplicate/invalid budget attempt ID")
        require(isinstance(key, str) and key and key not in keys, "Duplicate/invalid budget request key")
        require(attempt.get("reservation_id") == identity and attempt.get("status") == state and state in STATES, "Attempt identity/state alias mismatch")
        reserved = _integer(attempt["reserved_nanodollars"], "reserved amount", 1)
        _usd_matches(attempt["estimated_upper_usd"], reserved, key)
        actual = attempt["actual_known_cost_nanodollars"]
        if state == "settled":
            actual = _integer(actual, "settled amount")
            _usd_matches(attempt["actual_known_cost_usd"], actual, key)
            require(isinstance(attempt.get("receipt"), dict), "Settled attempt needs a receipt")
            totals["known_spent"] += actual
        else:
            require(actual is None and attempt["actual_known_cost_usd"] is None, "Unsettled cost must remain unknown")
            if state in ("reserved", "dispatched"):
                totals["active_reserved"] += reserved
            elif state == "unknown":
                totals["unknown_reserved"] += reserved
        counts[state] += 1
        attempts[identity] = attempt
        keys.add(key)
    totals["committed"] = sum(totals.values())
    totals["remaining"] = max(0, cap - totals["committed"])
    for field, value in totals.items():
        require(units.get(field) == value, f"Budget integer total changed: {field}")
    require(receipt.get("counts") == counts, "Budget state counts do not match attempts")
    for display, field in (("cap_usd", "cap"), ("known_spent_usd", "known_spent"),
                           ("reserved_usd", "active_reserved"), ("unknown_reserved_usd", "unknown_reserved"),
                           ("committed_usd", "committed"), ("remaining_usd", "remaining")):
        _usd_matches(receipt.get(display), units[field], display)
    expected_total = totals["known_spent"] / SCALE if not totals["active_reserved"] and not totals["unknown_reserved"] else None
    require(receipt.get("estimated_total_cost_usd") == expected_total, "Unknown commitments cannot be hidden in the estimated total")
    require(totals["known_spent"] <= 5 * SCALE, "Recorded known costs exceed the authorized $5 ceiling")
    require(totals["committed"] <= cap or receipt["halted"], "A commitment overrun must halt the campaign")

    state_by_id, last_receipt, reasons = {}, {}, {}
    exposure, peak_exposure, halt_seen = 0, 0, False
    event_counts = Counter()
    for sequence, event in enumerate(receipt["events"], 1):
        require(event.get("sequence") == sequence, "Budget events must have a complete contiguous sequence")
        kind, identity, detail = event["kind"], event["attempt_id"], event["detail"]
        require(isinstance(detail, dict), "Budget event detail must be an object")
        _integer(event["created_ns"], "event timestamp")
        event_counts[kind] += 1
        if identity == "campaign":
            require(kind == "halted" and receipt["halted"] and not halt_seen, "Invalid campaign event")
            halt_seen = True
            continue
        require(identity in attempts, "Event refers to an absent attempt")
        previous, attempt = state_by_id.get(identity), attempts[identity]
        if kind == "reserved":
            require(previous is None and detail.get("upper_nanodollars") == attempt["reserved_nanodollars"], "Invalid/duplicate reservation event")
            require(not halt_seen, "Reservation occurred after campaign halt")
            exposure += attempt["reserved_nanodollars"]
            require(exposure <= cap, "Reservation exceeded the contemporaneous committed budget")
        elif kind == "dispatched":
            require(previous == "reserved", "Dispatch must follow one reservation")
            require(not halt_seen, "Dispatch occurred after campaign halt")
        elif kind == "unknown":
            require(previous in ("reserved", "dispatched") and isinstance(detail.get("reason"), str), "Invalid unknown-charge transition")
            reasons[identity] = detail["reason"]
        elif kind == "cancelled":
            require(previous == "reserved" and isinstance(detail.get("reason"), str), "Only an unstarted reservation can be cancelled")
            reasons[identity] = detail["reason"]
            exposure -= attempt["reserved_nanodollars"]
        elif kind == "settled":
            require(previous in ("dispatched", "unknown"), "Settlement must follow dispatch or an unknown charge")
            require(detail.get("actual_nanodollars") == attempt["actual_known_cost_nanodollars"], "Settlement event amount changed")
            breached = attempt["actual_known_cost_nanodollars"] > attempt["reserved_nanodollars"]
            require(detail.get("bound_breach") is breached, "Settlement breach flag changed")
            require(not breached or receipt["halted"], "Known bound breach must halt dispatch")
            last_receipt[identity] = detail.get("receipt")
            exposure += attempt["actual_known_cost_nanodollars"] - attempt["reserved_nanodollars"]
            halt_seen |= breached
        else:
            raise VerificationError(f"Unknown budget event kind: {kind}")
        state_by_id[identity] = kind
        peak_exposure = max(peak_exposure, exposure)
    for identity, attempt in attempts.items():
        require(state_by_id.get(identity) == attempt["state"], "Attempt final state differs from event history")
        if attempt["state"] == "settled":
            require(last_receipt.get(identity) == attempt["receipt"], "Settlement receipt changed")
        require(reasons.get(identity) == attempt.get("reason"), "Attempt reason differs from event history")
    require(exposure == totals["committed"], "Replayed commitment differs from final total")
    require(halt_seen == receipt["halted"], "Campaign halt flag lacks matching event/bound-breach evidence")
    return {"verified": True, "attempts": len(attempts), "event_counts": dict(event_counts),
            "known_spent_nanodollars": totals["known_spent"], "unknown_reserved_nanodollars": totals["unknown_reserved"],
            "active_reserved_nanodollars": totals["active_reserved"], "cap_nanodollars": cap,
            "peak_committed_nanodollars": peak_exposure}


def _read(path):
    require(path.is_file() and not path.is_symlink(), f"Required public evidence missing or symlinked: {path}")
    return read_json(path)


def verify_experiment_directory(directory):
    experiment = _read(directory / "experiment.json")
    require(experiment.get("id") == directory.name, "Experiment ID must match its evidence directory")
    require(experiment.get("schema_version") == 2, "Expected v2 experiment")
    require(experiment.get("status") == "complete", "Release evidence must contain a complete experiment")
    require(experiment.get("provenance", {}).get("kind") == "native_responses_tool_execution", "Release experiment must retain actual model-call provenance")
    audit = audit_experiment(experiment)
    analysis = summarize_experiment(experiment)
    require(canonical(analysis) == canonical(experiment.get("analysis")), "Embedded analysis differs from independently recomputed analysis")
    require(canonical(analysis) == canonical(_read(directory / "analysis.json")), "analysis.json differs from independently recomputed analysis")
    budget = _read(directory / "budget-receipt.json")
    budget_audit = verify_budget(budget)
    # A snapshot may have been taken just before another worker/experiment
    # settled a request. The final public export is authoritative; totals may
    # increase but cannot refer to another campaign or silently change its cap.
    snapshot = experiment["budget_snapshot"]
    require(snapshot.get("campaign") == budget["campaign"] and snapshot.get("cap_usd") == budget["cap_usd"], "Experiment budget snapshot belongs to a different campaign")
    require(snapshot.get("known_spent_usd", 0) <= budget["known_spent_usd"], "Experiment snapshot spends more than its later export")
    by_key = {a["request_key"]: a for a in budget["attempts"]}
    step_keys, response_ids = set(), set()
    exact_cost = 0
    for record in experiment["records"]:
        for step in record["steps"]:
            key = step["request_key"]
            expected = f"{experiment['id']}:{record['config_id']}:{record['case_id']}:{record['repeat']}:{step['turn']}"
            require(key == expected and key not in step_keys, "Duplicate/misbound episode request key")
            step_keys.add(key)
            require(key in by_key, "A model step is missing from the public budget export")
            attempt = by_key[key]
            require(attempt["metadata"].get("request_hash") == step["request_hash"], "Ledger request hash does not match native transcript")
            require(attempt["metadata"].get("model") == step["request"]["model"], "Ledger model does not match native request")
            require(attempt["metadata"].get("max_output_tokens") == step["request"]["max_output_tokens"], "Ledger output budget changed")
            response_id = step["response"]["id"]
            require(step["response"].get("model") == step["request"]["model"], "Response model differs from the requested model")
            require(response_id not in response_ids, "Duplicate native response ID")
            response_ids.add(response_id)
            cost = step.get("estimated_cost_usd")
            if cost is not None:
                require(attempt["state"] == "settled", "Known model cost is not settled in the ledger")
                require(attempt["receipt"].get("response_hash") == step["response_hash"] and attempt["receipt"].get("response_id") == response_id, "Ledger response receipt does not match native evidence")
                require(attempt["actual_known_cost_nanodollars"] == _cost_units(cost), "Per-request rounded ledger cost mismatch")
                exact_cost += attempt["actual_known_cost_nanodollars"]
            else:
                require(attempt["state"] in ("unknown", "dispatched"), "Unknown step cost cannot silently become a known/zero charge")
    scoped = [a for a in budget["attempts"] if a["request_key"].startswith(experiment["id"] + ":")]
    orphaned = [a["request_key"] for a in scoped if a["request_key"] not in step_keys]
    failures = {r["failed_request"]["request_key"]: r["failed_request"] for r in experiment["records"] if r.get("failed_request")}
    failure_path = directory / "failed-requests.json"
    public_failures = _read(failure_path) if failure_path.exists() else []
    require(isinstance(public_failures, list), "Public failed-request receipts must be a list")
    failed_receipts = {row["request_key"]: row for row in public_failures}
    require(len(failed_receipts) == len(public_failures), "Duplicate public failed-request receipt")
    for key in orphaned:
        require(key in failures, "Unexplained ledger attempt has no episode failed_request evidence")
        attempt = by_key[key]
        if attempt["state"] == "settled":
            require(key in failed_receipts, "Settled failed request needs its public raw response receipt")
            saved = failed_receipts[key]
            require(fingerprint(saved["request"]) == saved["request_hash"] == attempt["metadata"]["request_hash"], "Failed-request input receipt mismatch")
            require(fingerprint(saved["response"]) == saved["response_hash"] == attempt["receipt"]["response_hash"], "Failed-request response receipt mismatch")
            require(saved["response"]["id"] == attempt["receipt"]["response_id"], "Failed response ID mismatch")
            from evalanchor.agent_api import response_usage
            _, cost = response_usage(saved["response"], saved["request"]["model"], saved["request"]["max_output_tokens"])
            require(cost is not None and _cost_units(cost) == attempt["actual_known_cost_nanodollars"], "Failed-request known charge is not supported by its raw response")
        else:
            require(attempt["state"] in ("unknown", "cancelled"), "Unresolved attempt must retain an explicit unknown charge or unstarted cancellation")
    result = {"id": experiment["id"], "verified": True, "episodes": len(experiment["records"]), "model_steps": len(step_keys),
              "split": experiment["protocol"]["split"], "benchmark_hash": experiment["benchmark_hash"],
              "audit": audit, "budget_audit": budget_audit, "episode_step_cost_nanodollars": exact_cost,
              "other_attempt_keys_retained_in_budget": orphaned,
              "config_decisions": {c["config_id"]: c["gate"]["decision"] for c in analysis["configs"]}}
    return result, experiment, budget


def verify_release_v2(root=ROOT, *, experiment_ids=None):
    root = Path(root)
    subset = experiment_ids is not None
    identities = list(experiment_ids) if subset else list(REQUIRED_EXPERIMENTS)
    require(identities and len(set(identities)) == len(identities), "Specify distinct experiment IDs")
    require(all(isinstance(i, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", i) for i in identities), "Unsafe experiment ID")
    rows, evidence, seen_response_ids = [], {}, set()
    for identity in identities:
        row, experiment, receipt = verify_experiment_directory(root / "experiments" / identity)
        rows.append(row)
        evidence[identity] = experiment, receipt
        for record in experiment["records"]:
            for step in record["steps"]:
                native_id = step["response"]["id"]
                require(native_id not in seen_response_ids, "A native response ID is reused across release experiments")
                seen_response_ids.add(native_id)
    output = {"verified": True, "release_verified": not subset, "scope": "selected_experiments" if subset else "v0.2_release",
              "experiments": rows, "note": "Original tools replayed; saved analyses recomputed; public budget JSON audited. No API calls or local caches required."}
    if not subset:
        final = _read(root / "artifacts/orderops-v2/campaign-budget.json")
        output["campaign"] = verify_budget(final)
        for identity, (_, receipt) in evidence.items():
            require(receipt["campaign"] == final["campaign"] and receipt["cap_usd"] == final["cap_usd"], "Evidence split into inconsistent campaign ledgers")
            require(receipt["events"] == final["events"][:len(receipt["events"])], f"{identity} budget history is not a prefix of the final campaign")
        require(final["reserved_usd"] == 0 and final["counts"]["dispatched"] == 0, "Release campaign still has active requests")
        frozen = _read(root / "artifacts/orderops-v2/frozen-protocol.json")
        require(frozen.get("fingerprint") == fingerprint({k:v for k,v in frozen.items() if k != "fingerprint"}), "Frozen protocol fingerprint mismatch")
        require(final["cap_usd"] == frozen.get("ledger_limit_usd") and frozen.get("user_limit_usd") == 5, "Final budget differs from frozen authorization")
        plans = frozen.get("experiments")
        require(isinstance(plans, list) and {row.get("id") for row in plans} == {"test-v2", "test-long-v2"} and len(plans) == 2,
                "Frozen protocol must define both the primary and resource-sensitivity test")
        for plan in plans:
            test = evidence[plan["id"]][0]
            require(test["protocol"]["split"] == "test", "Frozen experiment is not a test split")
            require(frozen.get("benchmark_hash") == test["benchmark_hash"], "Frozen benchmark differs from test evidence")
            require(canonical(frozen["configs"]) == canonical(test["configs"]), "Test configuration matrix changed after freezing")
            require(canonical(plan["protocol"]) == canonical(test["protocol"]), "Test protocol changed after freezing")
            require(plan["case_ids"] == test["case_ids"], "Test case selection changed after freezing")
            require(frozen["prompt_snapshots"] == test["provenance"]["prompts"], "Test prompts differ from the frozen snapshots")
            require(frozen["engine_hash"] == test["provenance"]["engine_hash"], "Test engine differs from the frozen implementation")
        output["frozen_protocol_sha256"] = hashlib.sha256((root / "artifacts/orderops-v2/frozen-protocol.json").read_bytes()).hexdigest()
        from evalanchor.agent_analysis import compare_horizons
        horizon = compare_horizons(evidence["test-v2"][0], evidence["test-long-v2"][0])
        require(canonical(horizon) == canonical(_read(root / "artifacts/orderops-v2/horizon-comparison.json")), "Saved horizon comparison differs from recomputation")
        output["horizon_comparison_verified"] = True
        pricing = _read(root / "artifacts/orderops-v2/pricing-verification.json")
        require(pricing.get("fingerprint") == fingerprint({k:v for k,v in pricing.items() if k != "fingerprint"}), "Pricing receipt fingerprint mismatch")
        require(pricing.get("rates_used") == frozen["pricing"]["per_million_tokens_usd"] and pricing.get("checked_at") == frozen["pricing"]["checked_at"], "Pricing receipt differs from the frozen rates")
        amendment = _read(root / "artifacts/orderops-v2/preregistration-amendment-resource-horizon.json")
        from check_horizon_budget import check_horizon_budget
        start = check_horizon_budget(evidence["dev-v2"][0], evidence["test-v2"][0],
                                    evidence["test-v2"][1], frozen, amendment)
        require(start["allow_start"] is True, "Resource-sensitivity experiment lacks an affordable preregistered launch")
        require(canonical(start) == canonical(_read(root / "artifacts/orderops-v2/horizon-start-check.json")), "Saved horizon launch decision differs from its cost-only recomputation")
        output["horizon_launch_check_verified"] = True
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--experiment", action="append", help="Explicit subset/development audit; not a complete release verification")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = verify_release_v2(args.root, experiment_ids=args.experiment)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(f"V2 verification failed: {exc}") from None
