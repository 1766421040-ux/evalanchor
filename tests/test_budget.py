"""Budget commitments survive crashes and concurrent workers without free retries."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from evalanchor.budget import (BudgetError, BudgetExceededError, BudgetLedger,
                               BudgetOverrunError, DuplicateRequestError)


def test_known_unknown_and_active_commitments_survive_restart(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = BudgetLedger(path, 1, campaign={"plan": "frozen-v2"})
    known = ledger.reserve("known", .4, {"model": "test"})
    ledger.mark_dispatched(known)
    ledger.settle(known, .2, receipt={"response_id": "response-1"})
    unknown = ledger.reserve("timeout", .3)
    ledger.mark_dispatched(unknown)
    ledger.mark_unknown(unknown, "response lost")
    active = ledger.reserve("not-sent", .1)
    ledger.close()

    restarted = BudgetLedger(path, 1, campaign={"plan": "frozen-v2"})
    state = restarted.snapshot()
    assert state["known_spent_usd"] == .2
    assert state["unknown_reserved_usd"] == .3
    assert state["reserved_usd"] == .1
    assert state["committed_usd"] == .6
    assert state["remaining_usd"] == .4
    assert state["estimated_total_cost_usd"] is None
    with pytest.raises(BudgetExceededError):
        restarted.reserve("too-much", .400000001)
    assert restarted.lookup("timeout")["actual_known_cost_usd"] is None
    assert restarted.lookup("not-sent")["reservation_id"] == active
    assert restarted.lookup("absent") is None


@pytest.mark.parametrize("state", ["reserved", "dispatched", "unknown", "settled", "cancelled"])
def test_duplicate_key_never_authorizes_another_network_call(tmp_path, state):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    ref = ledger.reserve("same-attempt", .2)
    if state in ("dispatched", "unknown", "settled"):
        ledger.mark_dispatched(ref)
    if state == "unknown":
        ledger.mark_unknown(ref, "timeout")
    if state == "settled":
        ledger.settle(ref, .1)
    if state == "cancelled":
        ledger.cancel_unstarted(ref, "not sent")
    with pytest.raises(DuplicateRequestError) as exc:
        ledger.reserve("same-attempt", .2)
    assert exc.value.reservation_id == ref and exc.value.state == state
    assert len(ledger.entries()) == 1


def test_ambiguous_retry_consumes_a_separate_full_reservation(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    first = ledger.reserve("logical-1-attempt-0", .6)
    ledger.mark_dispatched(first)
    ledger.mark_unknown(first, "read timeout may be billed")
    with pytest.raises(BudgetExceededError):
        ledger.reserve("logical-1-attempt-1", .6)
    with pytest.raises(BudgetError, match="never-dispatched"):
        ledger.cancel_unstarted(first, "pretend timeout is free")
    assert ledger.snapshot()["committed_usd"] == .6


def test_never_dispatched_cancel_releases_money_but_not_key(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    ref = ledger.reserve("one", 1)
    ledger.cancel_unstarted(ref, "failed writing intent before send")
    ledger.cancel_unstarted(ref, "already cancelled")
    assert ledger.snapshot()["remaining_usd"] == 1
    with pytest.raises(BudgetError):
        ledger.mark_dispatched(ref)
    with pytest.raises(BudgetError):
        ledger.settle(ref, 0)
    ledger.reserve("new-attempt", 1)


def test_dispatch_cannot_be_claimed_twice_even_after_restart(tmp_path):
    path = tmp_path / "budget.db"
    ledger = BudgetLedger(path, 1)
    ref = ledger.reserve("one", .3)
    ledger.mark_dispatched(ref)
    restarted = BudgetLedger(path, 1)
    assert restarted.get(ref)["state"] == "dispatched"
    with pytest.raises(BudgetError, match="do not resend"):
        restarted.mark_dispatched(ref)
    with pytest.raises(BudgetError):
        restarted.cancel_unstarted(ref, "cannot prove unsent")


def test_settlement_from_recovered_raw_receipt_is_idempotent(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    ref = ledger.reserve("one", .8)
    ledger.mark_dispatched(ref)
    ledger.mark_unknown(ref, "crashed after response")
    receipt = {"response_id": "r1", "input_tokens": 50}
    ledger.settle(ref, .25, receipt=receipt)
    ledger.settle(ref, .25, receipt=receipt)
    assert ledger.snapshot()["estimated_total_cost_usd"] == .25
    assert ledger.snapshot()["remaining_usd"] == .75
    assert ledger.snapshot()["counts"]["unknown"] == 0
    with pytest.raises(BudgetError, match="Conflicting"):
        ledger.settle(ref, .24, receipt=receipt)
    with pytest.raises(BudgetError, match="Conflicting"):
        ledger.settle(ref, .25, receipt={"response_id": "different"})
    with pytest.raises(BudgetError):
        ledger.mark_unknown(ref, "overwrite known expenditure")
    with sqlite3.connect(ledger.path) as db:
        events = [row[0] for row in db.execute("SELECT kind FROM events ORDER BY sequence")]
    assert events == ["reserved", "dispatched", "unknown", "settled"]


def test_actual_bound_breach_is_recorded_before_halt_exception(tmp_path):
    path = tmp_path / "budget.db"
    ledger = BudgetLedger(path, 1)
    first = ledger.reserve("bad-estimate", .2)
    pending = ledger.reserve("already-reserved", .1)
    ledger.mark_dispatched(first)
    with pytest.raises(BudgetOverrunError, match="cost recorded"):
        ledger.settle(first, 1.1)
    restarted = BudgetLedger(path, 1)
    state = restarted.snapshot()
    assert state["halted"] is True
    assert state["known_spent_usd"] == 1.1
    assert state["committed_usd"] == 1.2
    assert state["remaining_usd"] == 0
    with pytest.raises(BudgetExceededError, match="halted"):
        restarted.reserve("later", .01)
    with pytest.raises(BudgetExceededError, match="halted"):
        restarted.mark_dispatched(pending)
    restarted.cancel_unstarted(pending, "halted before send")
    assert restarted.snapshot()["known_spent_usd"] == 1.1


def test_manual_halt_persists_but_allows_reconciliation_and_unsent_release(tmp_path):
    path = tmp_path / "budget.db"
    ledger = BudgetLedger(path, 1)
    unknown = ledger.reserve("inflight", .4)
    waiting = ledger.reserve("waiting", .2)
    ledger.mark_dispatched(unknown)
    ledger.mark_unknown(unknown, "unverifiable tier")
    ledger.halt("Reported usage contradicts preflight upper bound")
    ledger.halt("second call must not overwrite the original reason")
    restarted = BudgetLedger(path, 1)
    assert restarted.snapshot()["halt_reason"] == "Reported usage contradicts preflight upper bound"
    with pytest.raises(BudgetExceededError, match="halted"):
        restarted.reserve("new", .01)
    with pytest.raises(BudgetExceededError, match="halted"):
        restarted.mark_dispatched(waiting)
    restarted.cancel_unstarted(waiting, "never sent")
    restarted.settle(unknown, .3, receipt={"response_id": "recovered"})
    assert restarted.snapshot()["known_spent_usd"] == .3
    assert restarted.snapshot()["halted"] is True
    assert len([event for event in restarted.export()["events"] if event["kind"] == "halted"]) == 1


@pytest.mark.parametrize("changed_cap", [.5, 1.01, 5])
def test_reopen_cannot_reset_or_increase_cap(tmp_path, changed_cap):
    path = tmp_path / "budget.db"
    BudgetLedger(path, 1, campaign={"plan": "a"})
    with pytest.raises(BudgetError, match="budget reset"):
        BudgetLedger(path, changed_cap)
    with pytest.raises(BudgetError, match="budget reset"):
        BudgetLedger(path, 1, campaign={"plan": "b"})
    assert BudgetLedger(path, 1).snapshot()["campaign"] == {"plan": "a"}


def test_eight_thread_reservation_race_cannot_overspend(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", .5)
    barrier = threading.Barrier(8)

    def reserve(index):
        barrier.wait(timeout=10)
        try:
            return ledger.reserve(f"thread-{index}", .1)
        except BudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert len([ref for ref in results if ref is not None]) == 5
    assert ledger.snapshot()["committed_usd"] == .5
    assert ledger.snapshot()["remaining_usd"] == 0


def test_eight_processes_share_one_atomic_budget(tmp_path):
    path = tmp_path / "budget.db"
    BudgetLedger(path, 1)
    script = """
import sys
from evalanchor.budget import BudgetLedger, BudgetExceededError
ledger = BudgetLedger(sys.argv[1], 1)
try:
    ledger.reserve(sys.argv[2], .26)
except BudgetExceededError:
    sys.exit(7)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    children = [subprocess.Popen([sys.executable, "-c", script, str(path), f"child-{i}"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                for i in range(8)]
    codes = []
    for child in children:
        out, err = child.communicate(timeout=30)
        assert child.returncode in (0, 7), (out, err)
        codes.append(child.returncode)
    assert codes.count(0) == 3 and codes.count(7) == 5
    assert BudgetLedger(path, 1).snapshot()["committed_usd"] == .78


def test_decimal_boundaries_round_reservations_up_and_cap_down(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", "0.0000000029")
    ledger.reserve("fraction", "0.0000000001")
    ledger.reserve("fraction-2", "0.0000000001")
    with pytest.raises(BudgetExceededError):
        ledger.reserve("third", "0.0000000001")
    assert ledger.snapshot()["cap_usd"] == .000000002
    assert ledger.snapshot()["committed_usd"] == .000000002


def test_cap_rounding_is_not_changed_by_decimal_context_precision(tmp_path):
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 3
        ledger = BudgetLedger(tmp_path / "budget.db", "0.999999999999999999999999999999999999")
        assert ledger.export()["nanodollars"]["cap"] == 999999999
        with pytest.raises(BudgetExceededError):
            ledger.reserve("rounded-up", "0.99999999900000000000000000000000001")


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "NaN", "1e999999999", {}, None])
def test_invalid_money_never_creates_an_attempt(tmp_path, bad):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    with pytest.raises(BudgetError):
        ledger.reserve("bad", bad)
    assert ledger.entries() == []


def test_bad_metadata_and_illegal_settlement_do_not_change_commitments(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    with pytest.raises(BudgetError):
        ledger.reserve("bad", .5, {"nonfinite": float("nan")})
    ref = ledger.reserve("valid", .5)
    with pytest.raises(BudgetError, match="Cannot settle reserved"):
        ledger.settle(ref, 0)
    assert ledger.snapshot()["reserved_usd"] == .5
    assert len(ledger.entries()) == 1


def test_export_can_recompute_exact_commitment_without_sqlite(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1, campaign={"protocol_hash": "frozen"})
    known = ledger.reserve("known", .2, {"case_id": "c1"})
    ledger.mark_dispatched(known)
    ledger.settle(known, .123456789, receipt={"response_id": "r1"})
    unknown = ledger.reserve("unknown", .3)
    ledger.mark_dispatched(unknown)
    ledger.mark_unknown(unknown, "timeout")
    ledger.reserve("active", .1)
    exported = ledger.export()
    computed = sum(row["actual_known_cost_nanodollars"] if row["state"] == "settled"
                   else row["reserved_nanodollars"] if row["state"] in ("reserved", "dispatched", "unknown")
                   else 0 for row in exported["attempts"])
    assert computed == exported["nanodollars"]["committed"] == 523456789
    assert exported["estimated_total_cost_usd"] is None
    assert exported["attempts"][1]["actual_known_cost_nanodollars"] is None
    assert exported["attempts"][0]["metadata"] == {"case_id": "c1"}
    assert exported["events"][-1]["kind"] == "reserved"
