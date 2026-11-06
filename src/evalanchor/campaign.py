"""Reusable campaign configuration around the frozen v2 accounting engine.

Opening a report never creates a ledger or changes its immutable budget. New
campaigns must explicitly name an identity and a finite spending limit.
"""
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3

from .budget import BudgetError, BudgetLedger, _units


def validate_campaign(campaign_id, cap_usd):
    if not isinstance(campaign_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", campaign_id):
        raise BudgetError("campaign-id must be 1–128 ASCII letters, digits, dots, underscores or hyphens")
    _units(cap_usd, "budget", positive=True, is_cap=True)
    return {"id": campaign_id, "purpose": "User-configured OrderOps evaluation", "schema_version": 1}


class ReadOnlyLedger(BudgetLedger):
    """Reuse audited snapshot/export logic with a SQLite read-only connection."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise BudgetError("An existing campaign ledger is required")
        try:
            with self._connection() as db:
                rows = db.execute("SELECT version FROM campaign WHERE id=1").fetchall()
                if len(rows) != 1 or rows[0]["version"] != 1:
                    raise BudgetError("Unsupported or missing campaign metadata")
        except sqlite3.DatabaseError as exc:
            raise BudgetError("Not a valid campaign ledger") from exc

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def open_campaign(path, campaign_id, cap_usd):
    manifest = validate_campaign(campaign_id, cap_usd)
    if Path(path).exists():
        snapshot = ReadOnlyLedger(path).export()
        if (snapshot["campaign"].get("id") != campaign_id or
                snapshot["nanodollars"]["cap"] != _units(cap_usd, "budget", positive=True, is_cap=True)):
            raise BudgetError("Existing ledger identity or cap differs; refusing a budget reset")
        # Preserve legacy metadata as well as any application-specific fields.
        manifest = snapshot["campaign"]
    return BudgetLedger(path, cap_usd=cap_usd, campaign=manifest)


def _binding(output, ledger_path, campaign_id, cap):
    validate_campaign(campaign_id, cap)
    return {
        'schema_version': 1,
        'ledger_path': str(Path(ledger_path).resolve()),
        'campaign_id': campaign_id,
        'cap_nanodollars': _units(cap, 'budget', positive=True, is_cap=True),
    }


def bind_campaign(output, ledger_path, campaign_id, cap):
    """Pin the private execution directory to its original ledger, once only.

    The sidecar lives in calls/, which the frozen runner allows before creating
    its journal and which every public distribution excludes.
    """
    import os
    from .contracts import canonical, read_json
    expected = _binding(output, ledger_path, campaign_id, cap)
    ledger = ReadOnlyLedger(ledger_path).export()
    if ledger['campaign'].get('id') != campaign_id or ledger['nanodollars']['cap'] != expected['cap_nanodollars']:
        raise BudgetError('Cannot bind a different campaign ledger')
    directory = Path(output) / 'calls'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'campaign-binding.json'
    if path.exists():
        if read_json(path) != expected:
            raise BudgetError('Existing experiment is bound to a different campaign ledger')
        return expected
    try:
        # Exclusive creation prevents a second launcher silently changing the
        # binding. On interruption, malformed sidecars fail closed on resume.
        with path.open('x', encoding='utf-8') as stream:
            stream.write(canonical(expected) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if read_json(path) != expected:
            raise BudgetError('Existing experiment is bound to a different campaign ledger')
    return expected


def validate_resume_campaign(output, ledger_path, campaign_id, cap):
    """Check accounting before any resumed execution, including zero-job runs.

    A complete journal still contains paid evidence: skipping its model calls
    must never replace the original bill with a new empty ledger. Legacy runs
    without a sidecar are checked against their snapshot, journal, receipts and
    cached responses, and can then acquire a binding via bind_campaign().
    """
    from .contracts import canonical, read_json, loads
    output = Path(output)
    expected = _binding(output, ledger_path, campaign_id, cap)
    current = ReadOnlyLedger(ledger_path).export()  # Never creates a ledger.
    if current['campaign'].get('id') != campaign_id or current['nanodollars']['cap'] != expected['cap_nanodollars']:
        raise BudgetError('Resume requires the original campaign identity and cap')
    sidecar = output / 'calls/campaign-binding.json'
    bound = sidecar.exists()
    if bound and read_json(sidecar) != expected:
        raise BudgetError('Resume requires the originally bound ledger path, identity and cap')

    def snapshot_matches(snapshot):
        if (not isinstance(snapshot, dict) or snapshot.get('campaign') != current['campaign'] or
                _units(snapshot.get('cap_usd'), 'saved cap', positive=True, is_cap=True) != current['nanodollars']['cap'] or
                _units(snapshot.get('known_spent_usd'), 'saved spent') > current['nanodollars']['known_spent']):
            raise BudgetError('Resume ledger disagrees with the saved campaign or loses known spending')

    source = output / 'experiment.json'
    experiment = read_json(source) if source.is_file() else None
    if experiment is not None:
        snapshot_matches(experiment.get('budget_snapshot'))
    elif not bound:
        raise BudgetError('Legacy resume lacks original campaign evidence; refusing a budget reset')

    by_key = {row['request_key']: row for row in current['attempts']}

    def check_request(step, *, cached=False):
        key = step['request_key']
        attempt = by_key.get(key)
        if attempt is None or attempt['metadata'].get('request_hash') != step['request_hash']:
            raise BudgetError('Resume ledger is missing original request accounting')
        if cached and attempt['id'] != step['reservation_id']:
            raise BudgetError('Resume cached response belongs to a different reservation')
        if cached:
            from .agent_api import response_usage
            _, cost = response_usage(step['response'], step['request']['model'], step['request']['max_output_tokens'])
        else:
            cost = step['estimated_cost_usd']
        if cost is not None:
            # A crash after the cache write and before settlement is recoverable
            # by ResponsesClient. Its full reservation must still be present.
            if cached and attempt['state'] in ('dispatched', 'unknown'):
                return
            receipt = attempt.get('receipt') or {}
            if (attempt['state'] != 'settled' or receipt.get('response_hash') != step['response_hash'] or
                    receipt.get('response_id') != step['response']['id'] or
                    attempt['actual_known_cost_nanodollars'] != _units(cost, 'recorded step cost')):
                raise BudgetError('Resume ledger settlement differs from original response evidence')
        elif attempt['state'] not in ('dispatched', 'unknown'):
            raise BudgetError('Resume must retain the full unknown-charge reservation')

    try:
        journal = output / 'journal.sqlite3'
        with sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.execute('BEGIN')
            records = [loads(row[0])['payload'] for row in db.execute('SELECT value FROM records')]
        if experiment is not None:
            records.extend(experiment['records'])
        for record in records:
            for step in record['steps']:
                check_request(step)
            failed = record.get('failed_request')
            if failed and failed['request_key'] not in by_key and failed.get('category') != 'budget':
                raise BudgetError('Resume ledger loses a failed request commitment')
        # Receipts written before an interrupted episode reached the journal
        # still belong to the original ledger.
        for cached in sorted((output / 'calls').glob('*.json')):
            if cached.name != 'campaign-binding.json':
                check_request(read_json(cached), cached=True)
        saved_path = output / 'budget-receipt.json'
        if saved_path.is_file():
            saved = read_json(saved_path)
            snapshot_matches(saved)
            events = saved['events']
            if not isinstance(events, list) or canonical(current['events'][:len(events)]) != canonical(events):
                raise BudgetError('Resume ledger does not preserve the original budget event history')
    except (KeyError, TypeError, sqlite3.DatabaseError) as exc:
        raise BudgetError('Resume lacks valid original accounting evidence') from exc
    return {'verified': True, 'campaign_id': campaign_id, 'ledger_path': expected['ledger_path'],
            'checked_records': len(records), 'binding_present': bound}
