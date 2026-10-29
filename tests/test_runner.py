"""Offline failure-injection tests for execution, evidence, and resume behavior."""

import copy
import json
import sqlite3
import threading

import pytest

from evalanchor import runner
from evalanchor.contracts import ContractError, audit_run, read_json
from evalanchor.providers import ChatProvider, ProviderError


def small_suite(count=3):
    return {
        "schema_version": 1,
        "id": "failure-injection-suite",
        "cases": [
            {
                "id": f"case-{i}",
                "group_id": f"PRIVATE-GROUP-{i}",
                "tags": ["PRIVATE-LABEL"],
                "messages": [{"role": "user", "content": f"Public question {i}"}],
                "scorer": "exact",
                "expected": "PRIVATE-REFERENCE",
            }
            for i in range(count)
        ],
    }


def replay_target(suite):
    return {"id": "fixture", "provider": "replay", "revision": "r1", "outputs": {c["id"]: "PRIVATE-REFERENCE" for c in suite["cases"]}}


class RecordingProvider:
    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior
        self.lock = threading.Lock()

    def generate(self, messages, case_id, repeat):
        with self.lock:
            self.calls.append((copy.deepcopy(messages), case_id, repeat))
            case_calls = sum(c[1:] == (case_id, repeat) for c in self.calls)
        if self.behavior:
            return self.behavior(messages, case_id, repeat, case_calls)
        return {"output": "PRIVATE-REFERENCE", "cost_usd": 0.01, "request": {"messages": messages}}


@pytest.fixture(autouse=True)
def stable_execution_environment(monkeypatch):
    # Other agents may edit source concurrently. Each test controls code identity
    # explicitly, so a coincidental external edit cannot create a flaky failure.
    monkeypatch.setattr(runner, "_engine_hash", lambda: "test-engine-v1")
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)


def test_only_messages_enter_provider_and_mutation_cannot_change_suite(tmp_path):
    suite = small_suite(1)
    original = copy.deepcopy(suite)

    def behavior(messages, case_id, repeat, calls):
        assert messages == original["cases"][0]["messages"]
        assert "PRIVATE-REFERENCE" not in json.dumps(messages)
        assert "PRIVATE-LABEL" not in json.dumps(messages)
        assert "PRIVATE-GROUP" not in json.dumps(messages)
        messages[0]["content"] = "provider-side mutation"
        return {"output": "PRIVATE-REFERENCE"}

    result = runner.run_suite(suite, replay_target(suite), tmp_path, provider=RecordingProvider(behavior), retries=0)
    assert suite == original
    assert result["records"][0]["score"] == 1
    assert audit_run(result)["verified"]


@pytest.mark.parametrize("change", [lambda c: c.update(scorer="typo"), lambda c: c.update(expected=123)])
def test_invalid_scorer_configuration_rejected_before_provider_call(tmp_path, change):
    suite = small_suite(1)
    change(suite["cases"][0])
    provider = RecordingProvider()
    with pytest.raises(ContractError, match="scorer"):
        runner.run_suite(suite, replay_target(suite), tmp_path, provider=provider)
    assert provider.calls == []
    assert not (tmp_path / "journal.sqlite3").exists()


def test_provider_errors_remain_in_complete_run_denominator(tmp_path):
    suite = small_suite()

    def behavior(messages, case_id, repeat, calls):
        if case_id == "case-1":
            raise ProviderError("invalid credentials", retryable=False)
        return {"output": "PRIVATE-REFERENCE"}

    provider = RecordingProvider(behavior)
    run = runner.run_suite(suite, replay_target(suite), tmp_path, provider=provider, retries=2)
    assert run["status"] == "complete"
    assert len(run["records"]) == 3
    assert sum(r["score"] for r in run["records"]) == 2
    failed = next(r for r in run["records"] if r["case_id"] == "case-1")
    assert failed["status"] == "error" and failed["score"] == 0
    assert len(failed["attempts"]) == 1
    assert failed["cost_usd"] is None
    assert len(provider.calls) == 3


def test_recoverable_retry_preserves_attempts_and_unknown_total_cost(tmp_path):
    suite = small_suite(1)

    def behavior(messages, case_id, repeat, calls):
        if calls == 1:
            raise ProviderError("rate limited", retryable=True)
        return {"output": "PRIVATE-REFERENCE", "cost_usd": 0.03, "usage": {"prompt_tokens": 4}}

    provider = RecordingProvider(behavior)
    run = runner.run_suite(suite, replay_target(suite), tmp_path, provider=provider, retries=1, max_calls=2)
    record = run["records"][0]
    assert len(provider.calls) == 2
    assert [a["status"] for a in record["attempts"]] == ["error", "ok"]
    assert record["status"] == "ok" and record["score"] == 1
    assert record["cost_usd"] is None  # the failed attempt's billing is unknown


def test_retry_exhaustion_stops_at_reserved_call_budget(tmp_path):
    suite = small_suite(1)

    def behavior(*args):
        raise ProviderError("unavailable", retryable=True)

    provider = RecordingProvider(behavior)
    run = runner.run_suite(suite, replay_target(suite), tmp_path, provider=provider, retries=2, max_calls=3)
    assert len(provider.calls) == 3
    assert run["records"][0]["score"] == 0
    assert run["records"][0]["status"] == "error"


def test_worst_case_budget_rejected_before_any_call(tmp_path):
    suite = small_suite(2)
    provider = RecordingProvider()
    with pytest.raises(ContractError, match="Worst-case calls 8"):
        runner.run_suite(suite, replay_target(suite), tmp_path, provider=provider, repeats=2, retries=1, max_calls=7)
    assert not provider.calls
    assert not (tmp_path / "journal.sqlite3").exists()


def test_executor_defect_saves_other_concurrent_successes_and_resumes_only_missing(tmp_path):
    suite = small_suite()

    def behavior(messages, case_id, repeat, calls):
        if case_id == "case-1":
            raise ValueError("programming defect must not become a model error")
        return {"output": "PRIVATE-REFERENCE"}

    first = RecordingProvider(behavior)
    with pytest.raises(RuntimeError, match="executor defect"):
        runner.run_suite(suite, replay_target(suite), tmp_path, provider=first, workers=3, retries=0)
    partial = read_json(tmp_path / "run.json")
    assert partial["status"] == "incomplete"
    assert {r["case_id"] for r in partial["records"]} == {"case-0", "case-2"}
    assert audit_run(partial)["verified"]
    saved = copy.deepcopy(partial["records"])

    second = RecordingProvider()
    complete = runner.run_suite(suite, replay_target(suite), tmp_path, provider=second, workers=3, retries=0, resume=True)
    assert [c[1] for c in second.calls] == ["case-1"]
    assert complete["status"] == "complete"
    assert [r for r in complete["records"] if r["case_id"] != "case-1"] == saved


def test_complete_resume_does_not_even_construct_provider(tmp_path, monkeypatch):
    suite = small_suite(1)
    first = runner.run_suite(suite, replay_target(suite), tmp_path, retries=0)

    def forbidden(_):
        pytest.fail("Completed resume must not initialize a provider")

    monkeypatch.setattr(runner, "make_provider", forbidden)
    resumed = runner.run_suite(suite, replay_target(suite), tmp_path, retries=0, resume=True)
    assert resumed["records"] == first["records"]
    assert resumed["created_at"] == first["created_at"]


@pytest.mark.parametrize("mutation", ["target", "outputs", "engine", "retry_policy"])
def test_resume_rejects_changed_execution_identity_before_call(tmp_path, monkeypatch, mutation):
    suite = small_suite(1)
    target = replay_target(suite)
    runner.run_suite(suite, target, tmp_path, retries=0)
    altered = copy.deepcopy(target)
    retries = 0
    if mutation == "target":
        altered["revision"] = "r2"
    elif mutation == "outputs":
        altered["outputs"]["case-0"] = "different raw output"
    elif mutation == "engine":
        monkeypatch.setattr(runner, "_engine_hash", lambda: "changed-scorer-source")
    else:
        retries = 1
    provider = RecordingProvider()
    with pytest.raises(ContractError, match="Resume rejected"):
        runner.run_suite(suite, altered, tmp_path, retries=retries, provider=provider, resume=True)
    assert not provider.calls


def test_tampered_journal_score_cannot_be_trusted_on_resume(tmp_path):
    suite = small_suite(1)
    target = replay_target(suite)
    runner.run_suite(suite, target, tmp_path, retries=0)
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        row = json.loads(db.execute("SELECT value FROM records").fetchone()[0])
        row["score"] = 0.0
        db.execute("UPDATE records SET value=?", (json.dumps(row),))
    provider = RecordingProvider()
    with pytest.raises(ContractError, match="Score tampering"):
        runner.run_suite(suite, target, tmp_path, retries=0, provider=provider, resume=True)
    assert not provider.calls


class HTTPResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.body[:size]


def test_chat_payload_has_no_labels_or_case_identifiers_and_never_logs_key(monkeypatch):
    target = {"id": "private-target", "provider": "chat-completions", "model": "test-model", "base_url": "https://model.invalid/v1", "api_key_env": "EVALANCHOR_TEST_API_KEY"}
    monkeypatch.setenv("EVALANCHOR_TEST_API_KEY", "PRIVATE-CREDENTIAL")
    provider = ChatProvider(target)
    captured = []

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return HTTPResponse({"choices": [{"message": {"content": "answer"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    provider.opener = Opener()
    messages = [{"role": "user", "content": "public question"}]
    result = provider.generate(messages, "PRIVATE-CASE-ID", 7)
    sent = json.loads(captured[0].data)
    assert sent["messages"] == messages
    assert set(sent) == {"model", "messages", "temperature", "max_tokens", "stream"}
    assert "PRIVATE-CASE-ID" not in json.dumps(sent)
    assert "PRIVATE-CREDENTIAL" not in json.dumps(result)
    assert result["cost_usd"] is None


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (500, True), (503, True), (302, False)])
def test_http_retry_classification_does_not_record_response_body(status, retryable):
    import io
    import urllib.error

    provider = ChatProvider({"base_url": "https://model.invalid/v1", "model": "fake"})

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"PRIVATE-CREDENTIAL"))

    provider.opener = Opener()
    with pytest.raises(ProviderError) as error:
        provider.generate([], "case", 0)
    assert error.value.retryable is retryable
    assert "PRIVATE-CREDENTIAL" not in str(error.value)


@pytest.mark.parametrize("failed_filename", ["suite.json", "target.json", "run.json"])
def test_write_failure_releases_lock_even_when_exception_traceback_is_retained(tmp_path, monkeypatch, failed_filename):
    suite = small_suite(1)
    target = replay_target(suite)
    provider = RecordingProvider()
    original_write = runner.write_json

    def failing_write(path, value):
        if path.name == failed_filename:
            raise OSError("injected disk write failure")
        return original_write(path, value)

    with monkeypatch.context() as patcher:
        patcher.setattr(runner, "write_json", failing_write)
        # Keeping ExceptionInfo alive preserves the old run's stack and store.
        # Relying on garbage collection for lock release would fail this test.
        with pytest.raises(OSError, match="injected disk write failure") as saved_error:
            runner.run_suite(suite, target, tmp_path, retries=0, provider=provider)
    assert saved_error.value.__traceback__ is not None
    replacement = RecordingProvider()
    completed = runner.run_suite(suite, target, tmp_path, retries=0, resume=True, provider=replacement)
    assert completed["status"] == "complete"
    assert audit_run(completed)["verified"]
    if failed_filename == "run.json":
        # The successful observation was committed even when final export failed.
        assert len(provider.calls) == 1
        assert replacement.calls == []
    else:
        assert provider.calls == []
        assert len(replacement.calls) == 1


def test_initial_metadata_failure_releases_lock_before_retry(tmp_path, monkeypatch):
    suite = small_suite(1)
    target = replay_target(suite)
    provider = RecordingProvider()
    with monkeypatch.context() as patcher:
        def fail_metadata(*args, **kwargs):
            raise OSError("injected metadata write failure")
        patcher.setattr(runner.RunStore, "set_metadata", fail_metadata)
        with pytest.raises(OSError, match="metadata write failure") as saved_error:
            runner.run_suite(suite, target, tmp_path, retries=0, provider=provider)
    assert saved_error.value.__traceback__ is not None
    assert provider.calls == []
    completed = runner.run_suite(suite, target, tmp_path, retries=0, resume=True, provider=provider)
    assert completed["status"] == "complete"
    assert len(provider.calls) == 1
