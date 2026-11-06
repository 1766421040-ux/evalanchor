"""Paid execution plans must fail invalid selections before credential access."""
from pathlib import Path

import pytest

from evalanchor.agent_cli import main
from evalanchor.contracts import ContractError


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('extra', [
    ['--repeats', '0'], ['--workers', '20'], ['--max-turns', '1'],
    ['--schedule-seed', '-1'], ['--case-id', 'dev-lookup-0-dev', '--case-id', 'typo'],
])
def test_invalid_paid_plan_never_reads_credentials(tmp_path, monkeypatch, extra):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid plan accessed credentials')
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key', forbidden)
    output = tmp_path / 'not-created'
    with pytest.raises(ContractError):
        main(['run', '--benchmark', str(ROOT/'examples/orderops/benchmark.json'),
              '--configs', str(ROOT/'examples/orderops/configs.json'), '--output', str(output),
              '--execute', *extra])
    assert not output.exists()


def test_paid_run_requires_explicit_campaign_before_credentials(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Unconfigured execution accessed credentials')
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key', forbidden)
    with pytest.raises(ContractError, match='explicit'):
        main(['run', '--output', str(tmp_path/'run'), '--execute'])
    assert not (tmp_path/'run').exists()


def test_plan_accepts_new_budget_without_credentials(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key', lambda *_: pytest.fail('read key'))
    assert main(['run', '--output', str(tmp_path/'run'), '--budget', '20', '--campaign-id', 'new']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['shared_ledger_cap_usd'] == 20
    assert plan['campaign_id'] == 'new'
    assert not (tmp_path/'run').exists()


def test_budget_reads_nondefault_cap_without_writing(tmp_path, capsys):
    import json
    from evalanchor.campaign import open_campaign
    path = tmp_path/'ledger.db'
    open_campaign(path, 'my-campaign', 2)
    before = path.read_bytes()
    assert main(['budget', '--ledger', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['cap_usd'] == 2
    assert path.read_bytes() == before
    with pytest.raises(ContractError, match='overwrite'):
        main(['budget', '--ledger', str(path), '--output', str(path)])
    assert path.read_bytes() == before


def test_analysis_writes_separate_artifact_and_rejects_alias(tmp_path, monkeypatch):
    from evalanchor.contracts import write_json, read_json
    source=tmp_path/'experiment.json'
    write_json(source, {'analysis': {'old': True}, 'id': 'fixture'})
    before=source.read_bytes()
    monkeypatch.setattr('evalanchor.agent_analysis.summarize_experiment', lambda _: {'recomputed': True})
    assert main(['analyze', str(source)]) == 0
    assert source.read_bytes() == before
    assert read_json(tmp_path/'analysis.json') == {'recomputed': True}
    alias=tmp_path/'alias.json'
    alias.hardlink_to(source)
    with pytest.raises(ContractError, match='overwrite'):
        main(['analyze', str(source), '--output', str(alias)])
    assert source.read_bytes() == before


def test_execute_conflict_preserves_all_old_files_before_credentials(tmp_path, monkeypatch):
    output=tmp_path/'existing'
    output.mkdir()
    receipt=output/'budget-receipt.json'
    receipt.write_text('old receipt')
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key', lambda *_: pytest.fail('read key'))
    ledger=tmp_path/'new-ledger.db'
    with pytest.raises(ContractError, match='existing files'):
        main(['run','--output',str(output),'--ledger',str(ledger),'--budget','20','--campaign-id','new','--execute'])
    assert receipt.read_text() == 'old receipt'
    assert list(output.iterdir()) == [receipt]
    assert not ledger.exists()


def test_resume_missing_or_mismatched_manifest_is_read_only(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr('evalanchor.openai_provider.load_api_key', lambda *_: pytest.fail('read key'))
    output=tmp_path/'resume-me'
    argv=['run','--output',str(output),'--budget','1','--campaign-id','new','--execute','--resume']
    with pytest.raises(ContractError, match='existing journal'):
        main(argv)
    assert not output.exists()
    output.mkdir()
    journal=output/'journal.sqlite3'
    with sqlite3.connect(journal) as db:
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO metadata VALUES ('manifest','{}')")
    before=journal.read_bytes()
    with pytest.raises(ContractError, match='identity changed'):
        main(argv)
    assert journal.read_bytes() == before
    assert not (output/'calls').exists()


def test_horizon_output_must_not_overwrite_inputs(tmp_path, monkeypatch):
    source=tmp_path/'original.json'
    source.write_text('evidence')
    monkeypatch.setattr('evalanchor.agent_analysis.compare_horizons', lambda *_: pytest.fail('should preflight'))
    with pytest.raises(ContractError, match='overwrite'):
        main(['compare-horizons',str(source),str(tmp_path/'b.json'),'--output',str(source)])
    assert source.read_text() == 'evidence'


@pytest.mark.parametrize(('decision','code'), [('pass',0),('fail',1),('inconclusive',2)])
def test_assessment_preserves_input_and_writes_artifacts_with_ci_status(tmp_path, monkeypatch, decision, code):
    from evalanchor.contracts import write_json, read_json
    source=tmp_path/'experiment.json'
    write_json(source, {'id':'example'})
    before=source.read_bytes()
    report={'decision':decision, 'report_fingerprint':'bound-to-source-and-policy'}
    monkeypatch.setattr('evalanchor.upgrade.assess_upgrade', lambda *a: report)
    monkeypatch.setattr('evalanchor.upgrade.render_upgrade_markdown', lambda r: 'review '+r['decision'])
    output, markdown=tmp_path/'report.json',tmp_path/'report.md'
    assert main(['assess',str(source),'--baseline','a','--candidate','b','--output',str(output),'--markdown',str(markdown)]) == code
    assert source.read_bytes() == before
    assert read_json(output) == report
    assert markdown.read_text() == 'review '+decision
    with pytest.raises(ContractError, match='overwrite'):
        main(['assess',str(source),'--baseline','a','--candidate','b','--output',str(output),'--markdown',str(output)])


def test_export_refuses_raw_evidence_destination(tmp_path):
    source=tmp_path/'experiment.json'
    source.write_text('original')
    with pytest.raises(ContractError, match='.html'):
        main(['export','--workspace',str(tmp_path),'--output',str(source)])
    assert source.read_text() == 'original'
