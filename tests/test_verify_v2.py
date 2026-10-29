"""Release checks use public JSON and exact accounting, never network calls."""
import copy
import importlib.util
from pathlib import Path

import pytest

from evalanchor.budget import BudgetLedger


def verifier():
    spec = importlib.util.spec_from_file_location('verify_v2_test', Path(__file__).parents[1]/'scripts/verify_v2.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def budget(tmp_path):
    ledger = BudgetLedger(tmp_path/'test.sqlite3', cap_usd=4.75, campaign={'id':'unit-test','user_allowance_usd':5})
    known = ledger.reserve('known', .01)
    ledger.mark_dispatched(known)
    ledger.settle(known, .0000000001, receipt={'response_hash':'a'*64})
    unknown = ledger.reserve('unknown', .2)
    ledger.mark_dispatched(unknown)
    ledger.mark_unknown(unknown, 'timeout')
    cancelled = ledger.reserve('cancelled', .3)
    ledger.cancel_unstarted(cancelled, 'never dispatched')
    return ledger.export()


def test_exact_rounding_and_unknown_commitments_are_preserved(budget):
    v = verifier();result = v.verify_budget(budget)
    assert result['known_spent_nanodollars'] == 1
    assert result['unknown_reserved_nanodollars'] == 200_000_000
    assert budget['estimated_total_cost_usd'] is None
    assert v._cost_units(.0000000001) == 1


@pytest.mark.parametrize('mutation', ['spent', 'unknown', 'counts', 'event', 'receipt', 'transition', 'alias', 'cap'])
def test_ledger_tampering_is_refused(budget, mutation):
    v = verifier();changed = copy.deepcopy(budget)
    if mutation == 'spent':changed['nanodollars']['known_spent'] += 1
    elif mutation == 'unknown':changed['estimated_total_cost_usd'] = changed['known_spent_usd']
    elif mutation == 'counts':changed['counts']['settled'] += 1
    elif mutation == 'event':changed['events'].pop(1)
    elif mutation == 'receipt':changed['attempts'][0]['receipt']['response_hash'] = 'forged'
    elif mutation == 'transition':changed['events'][1]['kind'] = 'cancelled'
    elif mutation == 'alias':changed['attempts'][0]['status'] = 'cancelled'
    elif mutation == 'cap':changed['nanodollars']['cap'] = 6_000_000_000
    with pytest.raises(v.VerificationError):v.verify_budget(changed)


def test_full_release_never_silently_skips_missing_experiments(tmp_path):
    v = verifier()
    with pytest.raises(v.VerificationError, match='Required public evidence'):
        v.verify_release_v2(tmp_path)
    with pytest.raises(v.VerificationError, match='Unsafe experiment'):
        v.verify_release_v2(tmp_path, experiment_ids=['../private'])


def test_unknown_later_settled_keeps_a_valid_event_chain(tmp_path):
    v = verifier();ledger=BudgetLedger(tmp_path/'ledger.sqlite3', cap_usd=1, campaign={'id':'test'})
    item=ledger.reserve('one',.1);ledger.mark_dispatched(item);ledger.mark_unknown(item,'lost connection')
    ledger.settle(item,.01,receipt={'response_id':'one'})
    assert v.verify_budget(ledger.export())['known_spent_nanodollars'] == 10_000_000


def test_reordered_reservations_cannot_hide_historical_budget_overcommit(tmp_path):
    v=verifier();ledger=BudgetLedger(tmp_path/'history.sqlite3',cap_usd=1,campaign={'id':'test'})
    for key in ('a','b'):
        identity=ledger.reserve(key,.8);ledger.mark_dispatched(identity);ledger.settle(identity,.1,receipt={'id':key})
    value=ledger.export();events=value['events']
    value['events']=[events[index] for index in (0,1,3,4,2,5)]
    for sequence,event in enumerate(value['events'],1):event['sequence']=sequence
    with pytest.raises(v.VerificationError,match='contemporaneous'):
        v.verify_budget(value)


def test_forged_late_halt_does_not_allow_dispatch_after_halt(tmp_path):
    v=verifier();ledger=BudgetLedger(tmp_path/'halt.sqlite3',cap_usd=1,campaign={'id':'test'})
    identity=ledger.reserve('a',.8);ledger.mark_dispatched(identity);ledger.settle(identity,.1,receipt={'id':'a'});ledger.halt('stop')
    value=ledger.export();events=value['events'];value['events']=[events[i] for i in (0,3,1,2)]
    for sequence,event in enumerate(value['events'],1):event['sequence']=sequence
    with pytest.raises(v.VerificationError,match='after campaign halt'):
        v.verify_budget(value)
