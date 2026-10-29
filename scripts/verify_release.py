#!/usr/bin/env python3
"""Recompute checked-in evidence without API calls, GPU, or optional frameworks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from evalanchor.contracts import audit_run, read_json
from evalanchor.analysis import compare_runs


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_v1(root=ROOT):
    root = Path(root)
    receipt_path = root / 'examples/integrations/verified/receipt.json'
    receipt = read_json(receipt_path)
    require(set(receipt['frameworks']) == {'inspect-ai', 'promptfoo'}, 'Required framework evidence missing')
    checked = []
    for name, info in receipt['frameworks'].items():
        require(info['status'] == 'passed', f'{name} did not pass')
        required = {'native_log', 'native_log_sha256', 'imported_run', 'imported_run_sha256'}
        if name == 'inspect-ai':
            required |= {'binary_log', 'binary_log_sha256'}
        require(required <= info.keys(), f'{name} required evidence references missing')
        for key in ('native_log', 'binary_log', 'imported_run'):
            if key not in info:
                continue
            path = receipt_path.parent / info[key]
            require(hashlib.sha256(path.read_bytes()).hexdigest() == info[key + '_sha256'], f'{name}/{key} checksum mismatch')
        run = read_json(receipt_path.parent / info['imported_run'])
        audit_run(run)
        require(len(run['records']) == info['samples'] == 128, 'Legacy release evidence mismatch at line 37')
        require(sum((r['score'] for r in run['records'])) == 80, 'Legacy release evidence mismatch at line 38')
        checked.append(name)
    demo = root / 'artifacts/demo'
    baseline = read_json(demo / 'baseline/run.json')
    require((demo / 'index.html').is_file(), 'Portable report missing')
    for name, expected in [('candidate','fail'),('fixed','inconclusive')]:
        candidate = read_json(demo / name / 'run.json')
        result = compare_runs(baseline, candidate)
        require(result['decision'] == expected, 'Legacy release evidence mismatch at line 46')
        saved = read_json(demo / f'comparison-{name}.json')
        require(result == saved, f'{name} saved comparison drift')
        checked.append(f'demo-{name}')
    path = root/'artifacts/local-qwen-final/run.json'
    run = read_json(path)
    audit_run(run)
    require(run['status'] == 'complete' and len(run['records']) == 16, 'Legacy release evidence mismatch at line 53')
    require(all((r['status'] == 'ok' for r in run['records'])), 'Legacy release evidence mismatch at line 54')
    require(sum((r['score'] for r in run['records'])) == 2, 'Legacy release evidence mismatch at line 55')
    require(sum((r['usage']['completion_tokens'] for r in run['records'])) == 636, 'Legacy release evidence mismatch at line 56')
    checked.append('local-qwen-final')
    api = root/'artifacts/openai-luna-smoke'
    api_run = read_json(api/'run.json')
    audit_run(api_run)
    require(api_run['status'] == 'complete' and len(api_run['records']) == 16, 'Legacy release evidence mismatch at line 61')
    require(all((r['status'] == 'ok' and r['response']['status'] == 'completed' for r in api_run['records'])), 'Legacy release evidence mismatch at line 62')
    require(sum((r['score'] for r in api_run['records'])) == 15, 'Legacy release evidence mismatch at line 63')
    require(sum((r['usage']['prompt_tokens'] for r in api_run['records'])) == 1350, 'Legacy release evidence mismatch at line 64')
    require(sum((r['usage']['completion_tokens'] for r in api_run['records'])) == 394, 'Legacy release evidence mismatch at line 65')
    require(all((r['cost_usd'] is not None for r in api_run['records'])), 'Legacy release evidence mismatch at line 66')
    require(abs(sum((r['cost_usd'] for r in api_run['records'])) - 0.000332) < 1e-12, 'Legacy release evidence mismatch at line 67')
    summary = read_json(api/'smoke-summary.json')
    require(summary['calls_attempted'] == summary['completed_responses'] == 16, 'Legacy release evidence mismatch at line 69')
    require(summary['contract_passes'] == 15 and summary['unknown_cost_records'] == 0, 'Legacy release evidence mismatch at line 70')
    comparison = compare_runs(run, api_run)
    require(comparison['decision'] == 'inconclusive', 'Legacy release evidence mismatch at line 72')
    require(comparison == read_json(api / 'comparison-qwen-luna.json'), 'Legacy release evidence mismatch at line 73')
    require((api / 'index.html').is_file(), 'Legacy release evidence mismatch at line 74')
    checked.extend(['openai-luna-smoke', 'qwen-openai-comparison'])
    return {'verified': True, 'checks': checked, 'note': 'Saved v0.1 evidence recomputed; no new model inference or paid calls.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--v1-only', action='store_true', help='Verify retained v0.1 evidence only; not a v0.2 release check')
    args = parser.parse_args(argv)
    result = {'verified': True, 'version': '0.2.0', 'v1': verify_v1(args.root)}
    if not args.v1_only:
        from verify_v2 import verify_release_v2
        result['v2'] = verify_release_v2(args.root)
    result['release_verified'] = not args.v1_only
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(f'Release verification failed: {exc}') from None
