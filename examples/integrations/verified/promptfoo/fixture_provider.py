"""Synthetic transport fixture; never a real model result."""
import hashlib
import json
from pathlib import Path

def call_api(prompt, options, context):
    messages = json.loads(prompt)
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    key = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    fixtures = json.loads(Path(__file__).with_name('fixture_outputs.json').read_text(encoding='utf-8'))
    if key not in fixtures:
        raise ValueError('Unexpected fixture prompt')
    return {'output': fixtures[key], 'metadata': {'evalanchor_synthetic_fixture': True}}
