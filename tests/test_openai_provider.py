"""No-network tests for the real OpenAI API adapter and its budget entrypoint."""

import copy
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from evalanchor.contracts import ContractError
from evalanchor.datasets import local_smoke_suite
from evalanchor.openai_provider import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    OpenAIResponsesProvider,
    budget_plan,
    estimate_request,
    load_api_key,
    parse_response,
)
from evalanchor.providers import ProviderError


def body(text="exact\nraw"):
    return {
        "id": "resp-test",
        "model": "gpt-6-luna",
        "status": "completed",
        "service_tier": "default",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


def raw(value):
    return json.dumps(value).encode("utf-8")


def config():
    return {"provider": "openai-responses", "model": "gpt-6-luna", "base_url": "https://api.openai.com/v1", "max_tokens": 256, "api_key_env": "OPENAI_API_KEY"}


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.value[:size]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = raw(body()) if response is None else response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.response)


def test_env_loader_reads_literal_single_key_and_never_executes(tmp_path):
    marker = tmp_path / "should-not-exist"
    env = tmp_path / ".env.local"
    env.write_text(f"IGNORED=$(touch {marker})\nOPENAI_API_KEY='sk-test-local-token'\n")
    assert load_api_key(env, environ={}) == "sk-test-local-token"
    assert not marker.exists()


@pytest.mark.parametrize("contents", ["OPENAI_API_KEY=$(echo secret)", "OPENAI_API_KEY=`echo secret`", "OPENAI_API_KEY=x\nOPENAI_API_KEY=y", "OPENAI_API_KEY=", "OPENAI_API_KEY='two words secret'"])
def test_env_loader_rejects_unsafe_or_ambiguous_values_without_echoing(contents, tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(contents)
    with pytest.raises(ContractError) as exc:
        load_api_key(env, environ={})
    assert "echo secret" not in str(exc.value)
    assert "two words secret" not in str(exc.value)


def test_environment_key_precedes_file_and_no_global_mutation(tmp_path):
    environment = {"OPENAI_API_KEY": "sk-test-environment-value"}
    assert load_api_key(tmp_path / "missing", environ=environment) == environment["OPENAI_API_KEY"]
    assert set(environment) == {"OPENAI_API_KEY"}


def test_budget_covers_16_or_32_calls_without_claiming_billing_cap():
    suite = local_smoke_suite()
    single = budget_plan(suite)
    double = budget_plan(suite, repeats=2, max_output_tokens=512)
    assert single["planned_calls"] == 16 and double["planned_calls"] == 32
    assert single["retries"] == double["retries"] == 0
    assert single["conservative_estimate_usd"] < double["conservative_estimate_usd"] <= 0.05
    assert single["input_reserve_usd_per_million"] == 0.25
    assert "not an account billing cap" in single["budget_note"]


def test_budget_rejects_large_prompts_extra_labels_or_more_repeats():
    with pytest.raises(ContractError, match="4096"):
        estimate_request([{"role": "user", "content": "x" * 5000}])
    with pytest.raises(ContractError, match="only role/content"):
        estimate_request([{"role": "user", "content": "x", "expected": "secret"}])
    with pytest.raises(ContractError, match="repeats"):
        budget_plan(local_smoke_suite(), repeats=3)
    with pytest.raises(ContractError, match="256 or 512"):
        estimate_request([{"role": "user", "content": "x"}], 8192)


def test_payload_uses_real_responses_parameters_and_no_labels_headers_or_key_in_logs():
    opener = FakeOpener()
    provider = OpenAIResponsesProvider(config(), api_key="sk-private-test-value", opener=opener, max_calls=16)
    messages = [{"role": "system", "content": "Follow the contract"}, {"role": "user", "content": "public input"}]
    result = provider.generate(messages, "private-case-id", 0)
    request = opener.requests[0]
    payload = json.loads(request.data)
    assert request.full_url == ENDPOINT
    assert payload["input"] == messages
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == 256
    assert payload["service_tier"] == "default"
    assert payload["truncation"] == "disabled"
    assert payload["store"] is False
    assert "temperature" not in payload and "tools" not in payload
    assert "private-case-id" not in json.dumps(payload)
    assert "sk-private-test-value" not in json.dumps(result)
    assert "Authorization" not in json.dumps(result)
    assert result["output"] == "exact\nraw"
    assert result["response"] == body()
    assert result["cost_usd"] == pytest.approx((90 * 0.10 + 10 * 0.01 + 20 * 0.50) / 1_000_000)


def test_call_cap_and_reservations_stop_before_network():
    opener = FakeOpener()
    provider = OpenAIResponsesProvider(config(), api_key="sk-private-test-value", opener=opener, max_calls=1)
    message = [{"role": "user", "content": "x"}]
    provider.generate(message, "x", 0)
    with pytest.raises(ProviderError, match="limit"):
        provider.generate(message, "x", 1)
    assert len(opener.requests) == 1
    too_small = OpenAIResponsesProvider(config(), api_key="sk-private-test-value", opener=opener, budget_usd=0.00001)
    with pytest.raises(ProviderError, match="limit"):
        too_small.generate(message, "x", 0)
    assert len(opener.requests) == 1


@pytest.mark.parametrize("changed", [{"model": "gpt-6-astra"}, {"base_url": "https://untrusted.invalid/v1"}, {"max_tokens": 4096}, {"api_key_env": "OTHER_KEY"}])
def test_unpriced_models_custom_endpoint_or_excessive_output_rejected(changed):
    with pytest.raises(ContractError):
        OpenAIResponsesProvider({**config(), **changed}, api_key="sk-private-test-value", opener=FakeOpener())


@pytest.mark.parametrize("mutation", [
    lambda b: b.pop("id"),
    lambda b: b.pop("model"),
    lambda b: b.pop("status"),
    lambda b: b.pop("output"),
    lambda b: b.update(status="failed"),
    lambda b: b.update(output=[]),
    lambda b: b["usage"].update(input_tokens=True),
    lambda b: b["usage"].update(output_tokens=257, total_tokens=357),
    lambda b: b["usage"].update(total_tokens=121),
    lambda b: b["usage"]["input_tokens_details"].update(cached_tokens=101),
    lambda b: b["usage"]["output_tokens_details"].update(reasoning_tokens=21),
    lambda b: b["output"][0].update(content=[{"type": "output_text", "text": None}]),
])
def test_missing_invalid_or_contradictory_response_fields_are_rejected(mutation):
    value = body()
    mutation(value)
    with pytest.raises(ProviderError, match="Malformed"):
        parse_response(raw(value), max_output_tokens=256)


@pytest.mark.parametrize("data", [b'{"id":"one","id":"two"}', b'{"x":NaN}', b'{"x":1e999}', b'\xff', b'x' * (MAX_RESPONSE_BYTES + 1)])
def test_duplicate_nonfinite_malformed_and_oversized_bodies_rejected(data):
    with pytest.raises(ProviderError):
        parse_response(data, max_output_tokens=256)


def test_incomplete_and_refusal_text_retained_without_retry_or_repair():
    value = body("{\"partial\":")
    value.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    result = parse_response(raw(value), max_output_tokens=256)
    assert result["output"] == '{"partial":'
    assert result["response"]["status"] == "incomplete"
    value["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot comply."}]
    assert parse_response(raw(value), max_output_tokens=256)["output"] == "Cannot comply."


def test_empty_incomplete_has_preserved_usage_and_empty_raw_output():
    value = body()
    value.update(status="incomplete", output=[], incomplete_details={"reason": "max_output_tokens"})
    result = parse_response(raw(value), max_output_tokens=256)
    assert result["output"] == "" and result["usage"]["completion_tokens"] == 20


@pytest.mark.parametrize("mutation", [
    lambda b: b.pop("usage"),
    lambda b: b["usage"].pop("input_tokens_details"),
    lambda b: b["usage"]["input_tokens_details"].pop("cache_write_tokens"),
    lambda b: b["usage"]["input_tokens_details"].update(cache_write_tokens=20),
    lambda b: b.update(model="unrecognized-model"),
    lambda b: b.update(service_tier="priority"),
])
def test_unknown_billing_conditions_remain_unknown(mutation):
    value = body()
    mutation(value)
    result = parse_response(raw(value), max_output_tokens=256)
    assert result["cost_usd"] is None
    assert result["response"] == value


def test_all_text_fragments_concatenate_without_rewriting_and_preserve_model_identity():
    value = body()
    value["model"] = "gpt-6-luna-observed-snapshot"
    value["output"] = [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": " A\n"}, {"type": "output_text", "text": "B "}]},
    ]
    result = parse_response(raw(value), max_output_tokens=256)
    assert result["output"] == " A\nB "
    assert result["response"]["model"] == value["model"]


def test_http_failure_stops_subsequent_network_calls_and_never_echoes_body():
    error = urllib.error.HTTPError(ENDPOINT, 401, "no", {}, io.BytesIO(b"sk-private-test-value"))
    opener = FakeOpener(error=error)
    provider = OpenAIResponsesProvider(config(), api_key="sk-private-test-value", opener=opener)
    with pytest.raises(ProviderError) as first:
        provider.generate([{"role": "user", "content": "x"}], "one", 0)
    assert first.value.retryable is False
    assert "sk-private-test-value" not in str(first.value)
    with pytest.raises(ProviderError, match="halted"):
        provider.generate([{"role": "user", "content": "x"}], "two", 0)
    assert len(opener.requests) == 1


def load_script():
    path = Path(__file__).parents[1] / "scripts/run_openai_smoke.py"
    spec = importlib.util.spec_from_file_location("evalanchor_openai_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_defaults_to_dry_run_without_reading_keys_or_constructing_client(monkeypatch, capsys, tmp_path):
    script = load_script()

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run must not read a key or construct an API client")

    monkeypatch.setattr(script, "load_api_key", forbidden)
    monkeypatch.setattr(script, "OpenAIResponsesProvider", forbidden)
    assert script.main(["--env-file", str(tmp_path / "missing")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "dry-run" and plan["planned_calls"] == 16


def test_execute_entrypoint_runs_exact_suite_through_fake_without_leaking_key(monkeypatch, capsys, tmp_path):
    script = load_script()
    env = tmp_path / ".env.local"
    env.write_text("OPENAI_API_KEY=sk-fake-private-local-key\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class FakeProvider:
        calls = 0

        def generate(self, messages, case_id, repeat):
            self.calls += 1
            return {"output": "unaltered fake response", "usage": {}, "cost_usd": None, "request": {"input": messages}, "response": {"model": "offline-test-double"}}

    fake = FakeProvider()
    observed_config = []

    def construct(config, *, api_key, max_calls):
        assert api_key == "sk-fake-private-local-key"
        assert max_calls == 16
        assert "provider-sha256=" in config["revision"]
        observed_config.append(config)
        return fake

    monkeypatch.setattr(script, "OpenAIResponsesProvider", construct)
    output = tmp_path / "result"
    assert script.main(["--execute", "--env-file", str(env), "--output", str(output)]) == 0
    run = json.loads((output / "run.json").read_text())
    assert fake.calls == len(run["records"]) == 16
    assert run["protocol"]["repeats"] == 1
    assert run["provenance"]["retry_policy"]["max_retries"] == 0
    assert run["target"]["provider"] == "openai-responses"
    assert "sk-fake-private-local-key" not in (output / "run.json").read_text()
    assert "sk-fake-private-local-key" not in capsys.readouterr().out
    summary = json.loads((output / "smoke-summary.json").read_text())
    assert summary["unknown_cost_records"] == 16
