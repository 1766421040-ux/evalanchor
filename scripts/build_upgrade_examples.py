#!/usr/bin/env python3
"""Recompute retrospective v0.3 policy examples without model requests."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from evalanchor.contracts import read_json, write_json
from evalanchor.upgrade import assess_upgrade, default_policy, render_upgrade_markdown


def specifications():
    quality = default_policy()
    cost = {**quality, 'id': 'orderops-cost-tradeoff-example-v1', 'objective': 'cost',
            'max_safe_success_regression': 0.04, 'max_regression_pairs': 3, 'min_cost_reduction': 0.5, 'max_cost_ratio': 1.0}
    return [
        ('blocked-family', 'test-v2', 'terra-direct', 'terra-checklist', quality),
        ('expensive-quality', 'test-v2', 'luna-direct', 'terra-direct', quality),
        ('bounded-cost-tradeoff', 'test-v2', 'terra-direct', 'luna-direct', cost),
        ('development-insufficient', 'dev-v2', 'luna-direct', 'terra-direct', quality),
    ]


def build(root=ROOT, output=None):
    root = Path(root)
    output = Path(output) if output else root/'artifacts/upgrade-v0.3'
    output.mkdir(parents=True, exist_ok=True)
    experiments = {}
    rows = []
    for name, experiment_id, baseline, candidate, policy in specifications():
        source = f'experiments/{experiment_id}/experiment.json'
        if experiment_id not in experiments:
            experiments[experiment_id] = read_json(root/source)
        report = assess_upgrade(experiments[experiment_id], baseline, candidate, policy)
        write_json(output/f'{name}.json', report)
        (output/f'{name}.md').write_text(render_upgrade_markdown(report), encoding='utf-8')
        rows.append({'name': name, 'source': source, 'baseline': baseline, 'candidate': candidate,
                     'decision': report['decision'], 'report_fingerprint': report['report_fingerprint']})
    write_json(output/'index.json', {'schema_version': 1, 'retrospective': True,
                                   'new_model_requests': 0, 'examples': rows})
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(build(output=args.output))
