"""New campaign identities and read-only reports must preserve accounting."""
import hashlib
import json
import sqlite3

import pytest

from evalanchor.budget import BudgetError, BudgetLedger
from evalanchor.campaign import ReadOnlyLedger, open_campaign, validate_campaign


def test_new_campaign_above_old_release_cap_and_read_only_report(tmp_path):
    path = tmp_path / 'new.sqlite3'
    ledger = open_campaign(path, 'new-project', 12)
    attempt = ledger.reserve('call-1', .5)
    ledger.mark_dispatched(attempt)
    ledger.settle(attempt, .2)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = ReadOnlyLedger(path).export()
    assert report['cap_usd'] == 12
    assert report['known_spent_usd'] == .2
    assert report['campaign']['id'] == 'new-project'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError):
        ReadOnlyLedger(path).reserve('must-not-write', .1)


def test_legacy_metadata_survives_reopen_and_mismatch_is_rejected(tmp_path):
    path = tmp_path / 'old.sqlite3'
    manifest = {'id': 'old-campaign', 'user_allowance_usd': 5, 'purpose': 'frozen'}
    BudgetLedger(path, cap_usd=4.75, campaign=manifest)
    assert open_campaign(path, 'old-campaign', 4.75).snapshot()['campaign'] == manifest
    for identity, cap in [('other', 4.75), ('old-campaign', 9)]:
        with pytest.raises(BudgetError, match='reset'):
            open_campaign(path, identity, cap)
    assert ReadOnlyLedger(path).snapshot()['cap_usd'] == 4.75


@pytest.mark.parametrize('cap', [0, -1, float('inf'), float('nan'), True, 1e-12, 9_000_001])
def test_invalid_cap(cap):
    with pytest.raises(BudgetError):
        validate_campaign('valid', cap)


@pytest.mark.parametrize('identity', ['', 'a/b', 'with space', None, 'x'*129])
def test_invalid_identity(identity):
    with pytest.raises(BudgetError):
        validate_campaign(identity, 1)


def test_missing_read_only_ledger_does_not_create_parent(tmp_path):
    path = tmp_path / 'missing' / 'ledger.db'
    with pytest.raises(BudgetError):
        ReadOnlyLedger(path)
    assert not path.parent.exists()


def test_invalid_database_does_not_get_initialized(tmp_path):
    path = tmp_path / 'invalid.db'
    path.write_text('not a database')
    with pytest.raises(BudgetError):
        ReadOnlyLedger(path)
    assert path.read_text() == 'not a database'


@pytest.fixture
def resume_case(tmp_path):
    from evalanchor.contracts import canonical, write_json
    output = tmp_path / 'run'
    output.mkdir()
    ledger_path = tmp_path / 'original.db'
    ledger = open_campaign(ledger_path, 'original', 2)
    key = 'run:config:case:0:0'
    reservation = ledger.reserve(key, .5, {'request_hash': 'original-input'})
    ledger.mark_dispatched(reservation)
    ledger.settle(reservation, .2, receipt={'response_hash': 'original-output', 'response_id': 'resp-1'})
    step = {'request_key': key, 'request_hash': 'original-input', 'response_hash': 'original-output',
            'response': {'id': 'resp-1'}, 'estimated_cost_usd': .2}
    record = {'steps': [step], 'failed_request': None}
    with sqlite3.connect(output/'journal.sqlite3') as db:
        db.execute('CREATE TABLE records(value TEXT)')
        db.execute('INSERT INTO records VALUES (?)', (canonical({'payload': record}),))
    write_json(output/'experiment.json', {'status':'complete', 'budget_snapshot':ledger.snapshot(), 'records':[record]})
    write_json(output/'budget-receipt.json', ledger.export())
    return output, ledger_path, ledger, step


def test_completed_legacy_resume_requires_its_original_ledger(resume_case, tmp_path):
    from evalanchor.campaign import validate_resume_campaign
    output, path, _, _ = resume_case
    original = {p.name:p.read_bytes() for p in output.iterdir()}
    assert validate_resume_campaign(output, path, 'original', 2)['verified']
    with pytest.raises(BudgetError, match='existing'):
        validate_resume_campaign(output, tmp_path/'absent.db', 'original', 2)
    assert not (tmp_path/'absent.db').exists()
    fresh = tmp_path/'fresh.db'
    open_campaign(fresh, 'original', 2)
    with pytest.raises(BudgetError, match='known spending'):
        validate_resume_campaign(output, fresh, 'original', 2)
    assert {p.name:p.read_bytes() for p in output.iterdir()} == original


def test_binding_is_private_idempotent_and_refuses_relocation(resume_case, tmp_path):
    import shutil
    from evalanchor.campaign import bind_campaign, validate_resume_campaign
    output, path, _, _ = resume_case
    binding = bind_campaign(output, path, 'original', 2)
    assert (output/'calls/campaign-binding.json').is_file()
    assert not (output/'campaign-binding.json').exists()
    assert bind_campaign(output, path, 'original', 2) == binding
    assert validate_resume_campaign(output, path, 'original', 2)['binding_present']
    alternate=tmp_path/'relocated.db'
    shutil.copy2(path, alternate)
    with pytest.raises(BudgetError, match='bound'):
        validate_resume_campaign(output, alternate, 'original', 2)
    with pytest.raises(BudgetError, match='bound'):
        bind_campaign(output, alternate, 'original', 2)


@pytest.mark.parametrize('tamper', ['missing-request', 'request-hash', 'response-hash', 'response-id', 'amount'])
def test_resume_rejects_missing_or_changed_original_settlement(resume_case, tamper):
    from evalanchor.campaign import validate_resume_campaign
    output, path, _, _ = resume_case
    # Retain aggregate spend where possible: identity-level checks must catch it.
    with sqlite3.connect(path) as db:
        if tamper == 'missing-request': db.execute("UPDATE attempts SET request_key='different-request'")
        elif tamper == 'request-hash': db.execute("UPDATE attempts SET metadata='{}'")
        elif tamper == 'response-hash': db.execute("UPDATE attempts SET receipt=?", (json.dumps({'response_hash':'changed','response_id':'resp-1'}),))
        elif tamper == 'response-id': db.execute("UPDATE attempts SET receipt=?", (json.dumps({'response_hash':'original-output','response_id':'other'}),))
        elif tamper == 'amount': db.execute('UPDATE attempts SET actual=300000000')
    with pytest.raises(BudgetError, match='accounting|settlement'):
        validate_resume_campaign(output, path, 'original', 2)


def test_resume_rejects_lost_event_prefix(resume_case):
    from evalanchor.campaign import validate_resume_campaign
    output, path, _, _ = resume_case
    with sqlite3.connect(path) as db: db.execute("UPDATE events SET detail='{}' WHERE kind='reserved'")
    with pytest.raises(BudgetError, match='event history'):
        validate_resume_campaign(output, path, 'original', 2)


def test_resume_unknown_charge_must_not_become_free_or_cancelled(resume_case):
    from evalanchor.campaign import validate_resume_campaign
    from evalanchor.contracts import read_json, write_json, canonical
    output, path, ledger, step = resume_case
    reservation=ledger.reserve('run:config:case:0:1', .4, {'request_hash':'unknown-input'})
    ledger.mark_dispatched(reservation)
    ledger.mark_unknown(reservation, 'lost response')
    unknown={**step, 'request_key':'run:config:case:0:1', 'request_hash':'unknown-input', 'estimated_cost_usd':None}
    experiment=read_json(output/'experiment.json')
    experiment['records'][0]['steps'].append(unknown)
    experiment['budget_snapshot']=ledger.snapshot()
    write_json(output/'experiment.json',experiment)
    with sqlite3.connect(output/'journal.sqlite3') as db:
        db.execute('UPDATE records SET value=?',(canonical({'payload':experiment['records'][0]}),))
    write_json(output/'budget-receipt.json',ledger.export())
    assert validate_resume_campaign(output,path,'original',2)['verified']
    with sqlite3.connect(path) as db: db.execute("UPDATE attempts SET state='cancelled' WHERE id=?",(reservation,))
    with pytest.raises(BudgetError, match='unknown-charge'):
        validate_resume_campaign(output,path,'original',2)


def test_legacy_journal_without_budget_evidence_fails_closed(tmp_path):
    from evalanchor.campaign import validate_resume_campaign
    output=tmp_path/'empty'
    output.mkdir()
    path=tmp_path/'ledger.db'
    open_campaign(path,'original',1)
    with sqlite3.connect(output/'journal.sqlite3') as db:db.execute('CREATE TABLE records(value TEXT)')
    with pytest.raises(BudgetError, match='lacks original'):
        validate_resume_campaign(output,path,'original',1)


def test_bound_empty_journal_can_resume_after_initialization_crash(tmp_path):
    from evalanchor.campaign import bind_campaign, validate_resume_campaign
    output=tmp_path/'empty'
    path=tmp_path/'ledger.db'
    open_campaign(path,'original',1)
    bind_campaign(output,path,'original',1)
    with sqlite3.connect(output/'journal.sqlite3') as db:db.execute('CREATE TABLE records(value TEXT)')
    assert validate_resume_campaign(output,path,'original',1)['verified']


def test_completed_cli_resume_uses_original_accounting_without_model_calls(tmp_path, monkeypatch):
    from evalanchor.agent_cli import main
    from evalanchor.agent_runner import run_experiment, default_configs
    from evalanchor.contracts import write_json, read_json
    from evalanchor.orderops import benchmark_suite
    from test_agent_runner import ScriptedClient

    benchmark = benchmark_suite()
    case = next(case for case in benchmark['cases'] if case['split'] == 'dev')
    configs = default_configs()[:1]
    output = tmp_path/'resume-case'
    ledger_path = tmp_path/'original.db'
    original = open_campaign(ledger_path,'original',2)

    class AccountedFixture(ScriptedClient):
        def generate(self, **kwargs):
            reply = super().generate(**kwargs)
            attempt = self.ledger.reserve(reply['request_key'],.1,{'request_hash':reply['request_hash']})
            self.ledger.mark_dispatched(attempt)
            self.ledger.settle(attempt,reply['estimated_cost_usd'],receipt={
                'response_hash':reply['response_hash'],'response_id':reply['response']['id']})
            return reply

    client=AccountedFixture([case]);client.ledger=original
    first=run_experiment(benchmark,configs,output,client=client,case_ids=[case['id']],split='dev',repeats=1)
    write_json(output/'budget-receipt.json',original.export())
    benchmark_path,configs_path=tmp_path/'benchmark.json',tmp_path/'configs.json'
    write_json(benchmark_path,benchmark);write_json(configs_path,configs)
    common=['run','--benchmark',str(benchmark_path),'--configs',str(configs_path),'--output',str(output),
            '--budget','2','--campaign-id','original','--case-id',case['id'],'--execute','--resume']
    monkeypatch.setattr('evalanchor.agent_api.ResponsesClient.generate',lambda *a,**k:pytest.fail('resending completed request'))
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key',lambda *_:pytest.fail('invalid resume read key'))
    before=(output/'experiment.json').read_bytes(),(output/'budget-receipt.json').read_bytes()
    with pytest.raises(BudgetError,match='existing'):
        main([*common,'--ledger',str(tmp_path/'absent.db')])
    wrong=tmp_path/'wrong.db';open_campaign(wrong,'original',2)
    with pytest.raises(BudgetError,match='accounting'):
        main([*common,'--ledger',str(wrong)])
    assert before==((output/'experiment.json').read_bytes(),(output/'budget-receipt.json').read_bytes())
    assert not (output/'calls').exists()
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key',lambda *_:'synthetic-never-transmitted')
    assert main([*common,'--ledger',str(ledger_path)])==0
    assert read_json(output/'experiment.json')['records']==first['records']
    assert read_json(output/'budget-receipt.json')==original.export()
    assert (output/'calls/campaign-binding.json').is_file()


def test_unjournaled_cached_response_keeps_original_pending_commitment(resume_case):
    from evalanchor.campaign import validate_resume_campaign
    from evalanchor.contracts import write_json, fingerprint
    from evalanchor.agent_api import make_request
    output,path,ledger,_=resume_case
    key='run:config:case:0:1'
    request=make_request('gpt-6-luna',[],[],512)
    response={'id':'pending-response','model':'gpt-6-luna','service_tier':'default',
              'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0,
                       'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}
    attempt=ledger.reserve(key,.3,{'request_hash':fingerprint(request)})
    ledger.mark_dispatched(attempt)
    cache={'request_key':key,'request_hash':fingerprint(request),'request':request,
           'reservation_id':attempt,'response_hash':fingerprint(response),'response':response}
    write_json(output/'calls/pending.json',cache)
    assert validate_resume_campaign(output,path,'original',2)['verified']
    ledger.mark_unknown(attempt,'interrupted after saving response')
    assert validate_resume_campaign(output,path,'original',2)['verified']
    with sqlite3.connect(path) as db: db.execute("UPDATE attempts SET state='cancelled' WHERE id=?",(attempt,))
    with pytest.raises(BudgetError,match='settlement'):
        validate_resume_campaign(output,path,'original',2)
