from pathlib import Path
import json
import re
import subprocess
import sys

from evalanchor.cli import main
from evalanchor.contracts import read_json
from evalanchor.report import write_report


def test_report_keeps_html_payload_as_json_text(tmp_path):
    payload = '</script><img src=x onerror=alert(1)>\u2028&'
    path = write_report(tmp_path / 'report.html', suite={'content': payload}, runs=[], comparisons=[])
    raw = path.read_text()
    assert '<img src=x' not in raw
    embedded = re.search(r'<script id="data" type="application/json">(.*?)</script>', raw, re.S).group(1)
    assert json.loads(embedded)['suite']['content'] == payload
    assert 'textContent' in raw


def test_demo_cli_and_exit_semantics(tmp_path):
    out = tmp_path / 'demo'
    assert main(['demo', '--output', str(out)]) == 0
    assert main(['validate', str(out / 'candidate/run.json')]) == 0
    for variant, expected in [('candidate', 1), ('fixed', 2)]:
        assert main(['compare', str(out/'baseline/run.json'), str(out/f'{variant}/run.json'), '--output', str(out/f'{variant}.json')]) == expected
    assert main(['demo', '--output', str(out)]) == 3
    run = read_json(out/'candidate/run.json')
    run['records'][0]['score'] = 1 - run['records'][0]['score']
    (out/'tampered.json').write_text(json.dumps(run))
    assert main(['validate', str(out/'tampered.json')]) == 3


def test_openai_dry_run_never_requires_key(tmp_path):
    import os
    env = {k:v for k,v in os.environ.items() if k != 'OPENAI_API_KEY'}
    command = [sys.executable, 'scripts/run_openai_smoke.py', '--env-file', str(tmp_path/'does-not-exist')]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['mode'] == 'dry-run'
    assert data['planned_calls'] == 16
    assert data['conservative_estimate_usd'] <= 0.05
