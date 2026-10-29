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
