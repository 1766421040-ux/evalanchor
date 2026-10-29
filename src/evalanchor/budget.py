"""Persistent conservative API commitments, shared by threads and processes.

Reserve, then persist ``mark_dispatched`` BEFORE opening the network. A duplicate
request key is never permission to send again. Ambiguous attempts retain their
entire reservation across restarts; a retry needs a new key and reservation.
SQLite cannot make a network request atomic with a transaction, so ambiguous
dispatches must not be automatically replayed. This is a local commitment limit,
not an account billing cap: the caller must supply a valid worst-case price bound.
Local SQLite files are supported; network filesystems are not supported.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .contracts import ContractError, canonical


_SCALE = 1_000_000_000  # Integer nanodollars avoid float budget races/rounding down.
_MAX_UNITS = 9_000_000_000_000_000
_LIVE = ("reserved", "dispatched", "unknown")


class BudgetError(ContractError):
    """Budget evidence or a requested state transition is invalid."""


class BudgetExceededError(BudgetError):
    """The reservation would exceed the campaign's committed budget."""


class DuplicateRequestError(BudgetError):
    """This logical attempt already exists; do not dispatch it again."""

    def __init__(self, reservation_id, state):
        self.reservation_id = reservation_id
        self.state = state
        super().__init__(f"Duplicate request refused: {reservation_id} is {state}; do not resend")


class BudgetOverrunError(BudgetError):
    """Actual cost exceeded its bound; expenditure was recorded and sends halted."""


def _units(value, label, *, positive=False, is_cap=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BudgetError(f"{label} must be a finite USD amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (positive and amount == 0):
            raise BudgetError(f"{label} must be finite and {'positive' if positive else 'nonnegative'}")
        if amount > Decimal(_MAX_UNITS // _SCALE):
            raise BudgetError(f"{label} is too large")
        # Construct the scaled Decimal exactly: arithmetic under the caller's
        # decimal context could round a just-below-boundary cap upward first.
        parts = amount.as_tuple()
        scaled = Decimal((parts.sign, parts.digits, parts.exponent + 9)).to_integral_value(
            rounding=ROUND_FLOOR if is_cap else ROUND_CEILING)
        if positive and scaled == 0:
            raise BudgetError(f"{label} is smaller than one nanodollar")
        return int(scaled)
    except (DecimalException, ValueError, OverflowError) as exc:
        if isinstance(exc, BudgetError):
            raise
        raise BudgetError(f"{label} must be a finite USD amount") from exc


def _json(value, label):
    if not isinstance(value, dict):
        raise BudgetError(f"{label} must be an object")
    try:
        return canonical(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BudgetError(f"{label} must contain finite JSON values") from exc


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise BudgetError(f"{label} must be a nonempty string")
    return value


class BudgetLedger:
    """One immutable cap per database; transactions, not lifetime locks, serialize writes.

    ``campaign`` optionally pins a plan/pricing manifest. Reopening with an
    explicitly supplied different manifest or cap is refused. Each method opens
    its own connection, allowing one handle to be used by eight worker threads.
    Unknown charges are not known expenditure and are never reported as zero.
    """

    def __init__(self, path, cap_usd=4.75, *, campaign=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cap = _units(cap_usd, "cap_usd", positive=True, is_cap=True)
        manifest = _json(campaign, "campaign") if campaign is not None else None
        # Schema initialization and cap validation also use the writer lock.
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS campaign (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, cap INTEGER NOT NULL, manifest TEXT NOT NULL, halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','unknown','settled','cancelled')), reserved INTEGER NOT NULL CHECK(reserved>0), actual INTEGER CHECK(actual>=0), metadata TEXT NOT NULL, receipt TEXT, reason TEXT, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_ns INTEGER NOT NULL)")
            existing = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if existing is None:
                if db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                    raise BudgetError("Ledger has attempts but no campaign; refusing a budget reset")
                db.execute("INSERT INTO campaign(id,version,cap,manifest) VALUES(1,1,?,?)", (cap, manifest or "{}"))
            elif existing["version"] != 1 or existing["cap"] != cap or (manifest is not None and manifest != existing["manifest"]):
                raise BudgetError("Existing ledger cap or campaign differs; refusing a budget reset")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, attempt_id, kind, detail):
        db.execute("INSERT INTO events(attempt_id,kind,detail,created_ns) VALUES(?,?,?,?)", (attempt_id, kind, canonical(detail), time.time_ns()))

    @staticmethod
    def _row(db, reservation_id):
        _text(reservation_id, "reservation_id")
        row = db.execute("SELECT * FROM attempts WHERE id=?", (reservation_id,)).fetchone()
        if row is None:
            raise BudgetError("Unknown reservation_id")
        return row

    @staticmethod
    def _totals(db):
        rows = db.execute("SELECT state,COUNT(*) AS n,SUM(reserved) AS reserved,SUM(actual) AS actual FROM attempts GROUP BY state").fetchall()
        counts = {state: 0 for state in (*_LIVE, "settled", "cancelled")}
        spent = active = unknown = 0
        for row in rows:
            counts[row["state"]] = row["n"]
            if row["state"] == "settled":
                spent += row["actual"] or 0
            elif row["state"] == "unknown":
                unknown += row["reserved"]
            elif row["state"] in ("reserved", "dispatched"):
                active += row["reserved"]
        return spent, active, unknown, counts

    def reserve(self, request_key, estimated_upper_usd, metadata=None):
        """Commit the full upper bound, returning a new reservation ID once durable."""
        _text(request_key, "request_key")
        amount = _units(estimated_upper_usd, "estimated_upper_usd", positive=True)
        encoded = _json({} if metadata is None else metadata, "metadata")
        reservation_id = uuid.uuid4().hex
        with self._transaction() as db:
            existing = db.execute("SELECT id,state FROM attempts WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                raise DuplicateRequestError(existing["id"], existing["state"])
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if campaign["halted"]:
                raise BudgetExceededError(f"Campaign halted: {campaign['halt_reason']}")
            spent, active, unknown, _ = self._totals(db)
            if spent + active + unknown + amount > campaign["cap"]:
                raise BudgetExceededError("Reservation exceeds remaining budget, including unresolved charges")
            now = time.time_ns()
            db.execute("INSERT INTO attempts(id,request_key,state,reserved,metadata,created_ns,updated_ns) VALUES(?,?,'reserved',?,?,?,?)", (reservation_id, request_key, amount, encoded, now, now))
            self._event(db, reservation_id, "reserved", {"upper_nanodollars": amount})
        return reservation_id

    def mark_dispatched(self, reservation_id):
        """Persist before network send. A second claim is refused, even after a crash."""
        with self._transaction() as db:
            row = self._row(db, reservation_id)
            if row["state"] != "reserved":
                raise BudgetError(f"Cannot dispatch {row['state']} attempt; do not resend")
            campaign = db.execute("SELECT halted FROM campaign WHERE id=1").fetchone()
            if campaign["halted"]:
                raise BudgetExceededError("Campaign halted; do not send reserved attempt")
            db.execute("UPDATE attempts SET state='dispatched',updated_ns=? WHERE id=?", (time.time_ns(), reservation_id))
            self._event(db, reservation_id, "dispatched", {})

    def settle(self, reservation_id, actual_known_cost, *, receipt=None):
        """Replace a reservation with trusted known cost; identical settlement is idempotent.

        A bound breach is committed to the ledger and halts future dispatches
        BEFORE BudgetOverrunError is raised. It must never disappear by rollback.
        """
        amount = _units(actual_known_cost, "actual_known_cost")
        encoded = _json({} if receipt is None else receipt, "receipt")
        breach = False
        with self._transaction() as db:
            row = self._row(db, reservation_id)
            if row["state"] == "settled":
                if row["actual"] != amount or row["receipt"] != encoded:
                    raise BudgetError("Conflicting settlement refused")
                return
            if row["state"] not in ("dispatched", "unknown"):
                raise BudgetError(f"Cannot settle {row['state']} attempt")
            breach = amount > row["reserved"]
            db.execute("UPDATE attempts SET state='settled',actual=?,receipt=?,updated_ns=? WHERE id=?", (amount, encoded, time.time_ns(), reservation_id))
            self._event(db, reservation_id, "settled", {"actual_nanodollars": amount, "bound_breach": breach, "receipt": json.loads(encoded)})
            if breach:
                db.execute("UPDATE campaign SET halted=1,halt_reason=? WHERE id=1", (f"Actual cost exceeded reserved bound for {reservation_id}",))
        if breach:
            raise BudgetOverrunError("Actual cost exceeded reservation; cost recorded and campaign halted")

    def mark_unknown(self, reservation_id, reason):
        """An interrupted/ambiguous attempt retains its complete commitment."""
        _text(reason, "reason")
        with self._transaction() as db:
            row = self._row(db, reservation_id)
            if row["state"] == "unknown":
                return
            if row["state"] not in ("reserved", "dispatched"):
                raise BudgetError(f"Cannot mark {row['state']} attempt unknown")
            db.execute("UPDATE attempts SET state='unknown',reason=?,updated_ns=? WHERE id=?", (reason, time.time_ns(), reservation_id))
            self._event(db, reservation_id, "unknown", {"reason": reason})

    def cancel_unstarted(self, reservation_id, reason):
        """Release only an attempt whose dispatch has never been claimed."""
        _text(reason, "reason")
        with self._transaction() as db:
            row = self._row(db, reservation_id)
            if row["state"] == "cancelled":
                return
            if row["state"] != "reserved":
                raise BudgetError("Only a never-dispatched reservation can be cancelled")
            db.execute("UPDATE attempts SET state='cancelled',reason=?,updated_ns=? WHERE id=?", (reason, time.time_ns(), reservation_id))
            self._event(db, reservation_id, "cancelled", {"reason": reason})

    def halt(self, reason):
        """Persistently stop new reservations/dispatches when a price bound fails.

        Existing charges can still be reconciled and never-sent reservations
        cancelled. There is intentionally no automatic unhalt or budget reset.
        """
        _text(reason, "reason")
        with self._transaction() as db:
            campaign = db.execute("SELECT halted FROM campaign WHERE id=1").fetchone()
            if not campaign["halted"]:
                db.execute("UPDATE campaign SET halted=1,halt_reason=? WHERE id=1", (reason,))
                self._event(db, "campaign", "halted", {"reason": reason})

    def snapshot(self, include_attempts=False):
        # A read transaction makes the cap, states, and totals one consistent view.
        with self._connection() as db:
            db.execute("BEGIN")
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            spent, active, unknown, counts = self._totals(db)
            committed = spent + active + unknown
            result = {"schema_version": 1, "currency": "USD", "cap_usd": campaign["cap"] / _SCALE,
                    "known_spent_usd": spent / _SCALE, "reserved_usd": active / _SCALE,
                    "unknown_reserved_usd": unknown / _SCALE, "committed_usd": committed / _SCALE,
                    "remaining_usd": max(0, campaign["cap"] - committed) / _SCALE,
                    "estimated_total_cost_usd": spent / _SCALE if not active and not unknown else None,
                    "halted": bool(campaign["halted"]), "halt_reason": campaign["halt_reason"],
                    "counts": counts, "campaign": json.loads(campaign["manifest"])}
            if include_attempts:
                result["accounting_unit"] = "nanodollar"
                result["nanodollars"] = {"cap": campaign["cap"], "known_spent": spent,
                                        "active_reserved": active, "unknown_reserved": unknown,
                                        "committed": committed, "remaining": max(0, campaign["cap"] - committed)}
                result["attempts"] = [self._public(row) for row in db.execute("SELECT * FROM attempts ORDER BY created_ns,id")]
                result["events"] = [{**dict(row), "detail": json.loads(row["detail"])}
                                    for row in db.execute("SELECT * FROM events ORDER BY sequence")]
            return result

    @staticmethod
    def _public(row):
        result = dict(row)
        result["reservation_id"] = result["id"]
        result["status"] = result["state"]
        result["reserved_nanodollars"] = result["reserved"]
        result["actual_known_cost_nanodollars"] = result["actual"]
        result["estimated_upper_usd"] = result.pop("reserved") / _SCALE
        amount = result.pop("actual")
        result["actual_known_cost_usd"] = amount / _SCALE if amount is not None else None
        result["metadata"] = json.loads(result["metadata"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] is not None else None
        return result

    def get(self, reservation_id):
        with self._connection() as db:
            return self._public(self._row(db, reservation_id))

    def lookup(self, request_key):
        _text(request_key, "request_key")
        with self._connection() as db:
            row = db.execute("SELECT * FROM attempts WHERE request_key=?", (request_key,)).fetchone()
            return self._public(row) if row is not None else None

    def entries(self):
        with self._connection() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM attempts ORDER BY created_ns,id")]

    def export(self):
        """Consistent offline-auditable JSON; callers must keep metadata/receipts public.

        Authentication headers/API keys must never be placed in these fields.
        Exact integer amounts accompany display USD values; unknown stays null.
        """
        return self.snapshot(include_attempts=True)

    def close(self):
        """Connections are scoped to operations; retained handles hold no DB lock."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
