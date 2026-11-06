#!/usr/bin/env python3
"""Strictly reproduce every published v0.3 upgrade-policy example."""
from pathlib import Path
import json
import hashlib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'scripts'))
from build_upgrade_examples import specifications
from evalanchor.contracts import read_json, canonical
from evalanchor.upgrade import assess_upgrade, render_upgrade_markdown


def verify_release_v3(root=ROOT):
    root = Path(root)
    directory = root/'artifacts/upgrade-v0.3'
    experiments, rows = {}, []
    expected_decisions = ['fail', 'fail', 'pass', 'inconclusive']
    for (name, experiment_id, baseline, candidate, policy), expected in zip(specifications(), expected_decisions, strict=True):
        source = f'experiments/{experiment_id}/experiment.json'
        if experiment_id not in experiments:
            experiments[experiment_id] = read_json(root/source)
        report = assess_upgrade(experiments[experiment_id], baseline, candidate, policy)
        if report['decision'] != expected:
            raise ValueError(f'{name}: unexpected decision')
        if canonical(read_json(directory/f'{name}.json')) != canonical(report):
            raise ValueError(f'{name}: saved decision evidence differs from replay')
        if (directory/f'{name}.md').read_text(encoding='utf-8') != render_upgrade_markdown(report):
            raise ValueError(f'{name}: Markdown report differs from verified decision')
        rows.append({'name': name, 'source': source, 'baseline': baseline, 'candidate': candidate,
                     'decision': report['decision'], 'report_fingerprint': report['report_fingerprint']})
    expected_index = {'schema_version': 1, 'retrospective': True, 'new_model_requests': 0, 'examples': rows}
    if canonical(read_json(directory/'index.json')) != canonical(expected_index):
        raise ValueError('v0.3 example index mismatch')
    qa = read_json(directory/'workbench-qa.json')
    if (qa.get('kind') != 'workbench_software_qa_not_model_experiment' or qa.get('model_requests') != 0 or
            set(qa['source_sha256']) != {'src/evalanchor/workbench.py', 'src/evalanchor/templates/workbench.html', 'tests/test_workbench.py'} or
            {item['path'] for item in qa['screenshots']} != {'docs/figures/upgrade-workbench.png', 'docs/figures/upgrade-paired-trace.png'} or
            len(qa['screenshots']) != 2):
        raise ValueError('Incomplete UI QA receipt')
    for name, digest in qa['source_sha256'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
            raise ValueError('UI QA source changed: '+name)
    for item in qa['screenshots']:
        data = (root/item['path']).read_bytes()
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('UI screenshot changed: '+item['path'])
    return {'verified': True, 'retrospective': True, 'examples': rows, 'ui_receipt_hashes_verified': True, 'new_model_requests': 0}


if __name__ == '__main__':
    print(json.dumps(verify_release_v3(), ensure_ascii=False, indent=2))
