import copy
import http.client
import json
import re
import sqlite3
import threading

import pytest

from evalanchor.workbench import (
    MAX_REQUEST_BYTES, WorkbenchError, create_workbench_server,
    dashboard_payload, export_dashboard, load_experiments,
)


def experiment(identity="trial-one"):
    return {"schema_version": 2, "id": identity, "benchmark_hash": "a" * 64,
            "provenance": {"kind": "ui_fixture"},
            "status": "complete", "configs": [{"id": "base", "model": "fixture", "prompt_strategy": "minimal"}],
            "records": [{"case_id": "refund-1", "config_id": "base", "group_id": "refund",
                         "repeat": 0, "family": "refund", "split": "test", "status": "completed",
                         "grade": {"task_success": False, "attempted_violations": 1, "executed_violations": 0},
                         "steps": [{"turn": 0, "request": {"input": "customer request"},
                                    "response": {"output": "</script><img src=x onerror=alert(1)>"},
                                    "tool_calls": [{"name": "refund", "arguments": {"amount": 50},
                                                    "result": {"ok": False, "error": "confirmation_required"}}]}],
                         "usage": {"input_tokens": 40, "output_tokens": 20},
                         "estimated_cost_usd": .001, "latency_ms": 150,
                         "final_state": {"refunded": False}}],
            "budget_snapshot": {"limit_usd": 5, "known_usd": .001}}


def write_experiment(root, value, directory="one"):
    folder = root / directory
    folder.mkdir(exist_ok=True)
    path = folder / "experiment.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def server(tmp_path):
    path = write_experiment(tmp_path, experiment())
    httpd = create_workbench_server(tmp_path, 0)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    port = httpd.server_address[1]
    yield tmp_path, path, port
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=3)


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def page_data(port):
    status, raw, headers = request(port, "GET", "/")
    assert status == 200
    assert headers["X-Content-Type-Options"] == "nosniff"
    embedded = re.search(r'<script id="workbench-data" type="application/json">(.*?)</script>', raw.decode(), re.S)
    return json.loads(embedded.group(1))


def review_body(port):
    data = page_data(port)
    body = {"experiment_id": "trial-one", "fingerprint": data["experiments"][0]["fingerprint"],
            "case_id": "refund-1", "config_id": "base", "repeat": 0,
            "state": "needs-fix", "note": "Inspect confirmation before refund."}
    headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
               "X-EvalAnchor-Token": data["review_token"]}
    return body, headers


def test_list_and_details_do_not_read_arbitrary_paths(server):
    root, _, port = server
    (root / "private.json").write_text('{"secret":"not served"}')
    status, raw, _ = request(port, "GET", "/api/experiments")
    assert status == 200
    listing = json.loads(raw)
    assert listing["experiments"][0]["id"] == "trial-one"
    assert "records" not in listing["experiments"][0]
    assert "review_token" not in listing
    status, raw, _ = request(port, "GET", "/api/experiments/trial-one")
    assert status == 200
    assert json.loads(raw)["experiment"]["records"][0]["grade"]["task_success"] is False
    for path in ("/private.json", "/api/experiments/missing", "/api/experiments/../private.json",
                 "/api/experiments/%2e%2e%2fprivate.json", "/api/experiments/trial-one?path=private.json",
                 "/api/experiments/%252e%252e%252fprivate.json", "/api/experiments/%00"):
        status, raw, _ = request(port, "GET", path)
        assert status in (400, 404)
        assert b"not served" not in raw


def test_only_review_api_can_write_and_requires_origin_and_token(server):
    _, _, port = server
    body, headers = review_body(port)
    for change in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                   {"Origin": "http://127.0.0.1:1"}, {"X-EvalAnchor-Token": "wrong"}, {"X-EvalAnchor-Token": "é"}):
        status, _, _ = request(port, "POST", "/api/reviews", json.dumps(body), {**headers, **change})
        assert status == 403
    for missing in ("Origin", "X-EvalAnchor-Token"):
        status, _, _ = request(port, "POST", "/api/reviews", json.dumps(body), {k:v for k,v in headers.items() if k != missing})
        assert status == 403
    status, _, _ = request(port, "POST", "/api/run", json.dumps(body), headers)
    assert status == 404
    status, _, _ = request(port, "GET", "/", headers={"Host": "evil.example"})
    assert status == 403


def test_review_persists_with_history_without_mutating_experiment(server):
    root, path, port = server
    original = path.read_bytes()
    body, headers = review_body(port)
    for note, state in (("diagnosed", "needs-fix"), ("fix verified", "resolved")):
        status, raw, _ = request(port, "POST", "/api/reviews", json.dumps({**body, "note": note, "state": state}), headers)
        assert status == 200, raw
    data = page_data(port)
    assert data["reviews"][0]["note"] == "fix verified"
    assert data["reviews"][0]["state"] == "resolved"
    assert path.read_bytes() == original
    with sqlite3.connect(root / ".evalanchor-reviews.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM review_history").fetchone()[0] == 2
    snapshot = export_dashboard(root, root / "snapshot.html").read_text()
    assert '"mode":"export"' in snapshot
    assert '"review_token":null' in snapshot
    assert "fix verified" in snapshot


def test_review_rejects_changed_artifact_and_unknown_record(server):
    _, path, port = server
    body, headers = review_body(port)
    for changed in ({"case_id": "missing"}, {"config_id": "missing"}, {"repeat": True},
                    {"repeat": 5}, {"state": "approved-for-production"}, {"note": "x" * 4001},
                    {"unexpected": "writefile"}):
        status, _, _ = request(port, "POST", "/api/reviews", json.dumps({**body, **changed}), headers)
        assert status == 400
    value = experiment()
    value["records"][0]["grade"]["task_success"] = True
    path.write_text(json.dumps(value))
    status, raw, _ = request(port, "POST", "/api/reviews", json.dumps(body), headers)
    assert status == 400
    assert "changed" in json.loads(raw)["error"]


def test_review_rejects_ambiguous_record(server):
    _, path, port = server
    value = experiment()
    value["records"].append(copy.deepcopy(value["records"][0]))
    path.write_text(json.dumps(value))
    body, headers = review_body(port)
    status, raw, _ = request(port, "POST", "/api/reviews", json.dumps(body), headers)
    assert status == 400
    assert "unambiguous" in json.loads(raw)["error"]


def test_review_size_content_type_and_json_validation(server):
    _, _, port = server
    body, headers = review_body(port)
    status, _, _ = request(port, "POST", "/api/reviews", json.dumps(body), {**headers, "Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = request(port, "POST", "/api/reviews", "x" * (MAX_REQUEST_BYTES + 1), headers)
    assert status == 413
    for invalid in ('{"repeat":0,"repeat":1}', '{"note":NaN}', '[]', ''):
        status, _, _ = request(port, "POST", "/api/reviews", invalid, headers)
        assert status in (400, 413)


def test_loader_rejects_duplicate_ids_bad_json_and_symlinks(tmp_path):
    write_experiment(tmp_path, experiment(), "one")
    write_experiment(tmp_path, experiment(), "two")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "experiment.json").write_text('{"schema_version":2,"schema_version":3}')
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "experiment.json").write_text(json.dumps(experiment("hidden")))
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "experiment.json").symlink_to(tmp_path / "one" / "experiment.json")
    items, warnings = load_experiments(tmp_path)
    assert items == []
    assert any("Duplicate experiment ID" in message for message in warnings)
    assert any("Duplicate object key" in message for message in warnings)
    assert any("Symlink" in message for message in warnings)


def test_export_escapes_evidence_and_has_no_external_assets(tmp_path):
    write_experiment(tmp_path, experiment())
    path = export_dashboard(tmp_path, tmp_path / "exports" / "dashboard.html")
    raw = path.read_text()
    assert "<img src=x" not in raw
    embedded = re.search(r'<script id="workbench-data" type="application/json">(.*?)</script>', raw, re.S).group(1)
    data = json.loads(embedded)
    assert data["experiments"][0]["experiment"] == experiment()
    assert data["mode"] == "export" and data["review_token"] is None
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)=["\']https?://', raw)
    assert ".innerHTML" not in raw
    assert "textContent" in raw
    assert not (tmp_path / ".evalanchor-reviews.sqlite3").exists()


def test_empty_workspace_is_valid_and_invalid_workspace_rejected(tmp_path):
    assert dashboard_payload(tmp_path)["experiments"] == []
    with pytest.raises(WorkbenchError):
        create_workbench_server(tmp_path / "missing", 0)
    with pytest.raises(WorkbenchError):
        dashboard_payload(tmp_path / "missing")


def test_annotation_symlink_never_written(server):
    root, _, port = server
    body, headers = review_body(port)
    outside = root / "unrelated.txt"
    outside.write_text("preserve")
    (root / ".evalanchor-reviews.sqlite3").symlink_to(outside)
    status, _, _ = request(port, "POST", "/api/reviews", json.dumps(body), headers)
    assert status == 400
    assert outside.read_text() == "preserve"


def test_saved_analysis_tampering_is_422_and_cache_invalidates(server, monkeypatch):
    from evalanchor.agent_analysis import summarize_experiment
    from evalanchor.agent_runner import run_experiment, default_configs
    from evalanchor.orderops import benchmark_suite, build_cases
    from test_agent_runner import ScriptedClient
    import evalanchor.agent_runner as runner

    root, _, port = server
    case = build_cases('dev')[0]
    native = run_experiment(benchmark_suite(), default_configs()[:1], root/'audited',
                            client=ScriptedClient([case]), case_ids=[case['id']], split='dev')
    native['analysis'] = summarize_experiment(native)
    path = root/'audited'/'experiment.json'
    path.write_text(json.dumps(native))
    calls = []
    original = runner.audit_experiment
    def spy(value):
        calls.append(value['id'])
        return original(value)
    monkeypatch.setattr(runner, 'audit_experiment', spy)
    status, raw, _ = request(port, 'GET', '/api/experiments/audited')
    assert status == 200, raw
    assert json.loads(raw)['verification']['analysis_verified']
    count = len(calls)
    assert count >= 2  # The independent summarizer performs its own replay.
    assert request(port, 'GET', '/api/experiments/audited')[0] == 200
    assert len(calls) == count
    native['analysis']['configs'][0]['safe_success_rate'] = 0.5
    path.write_text(json.dumps(native))
    status, raw, _ = request(port, 'GET', '/api/experiments/audited')
    assert status == 422
    assert json.loads(raw)['status'] == 'invalid'
    assert 'analysis differs' in json.loads(raw)['error']
    assert len(calls) > count
    assert b'"id":"audited"' in request(port, 'GET', '/api/experiments')[1]
    assert all(item['experiment']['id'] != 'audited' for item in page_data(port)['experiments'])


def test_resigned_episode_tampering_is_not_displayed_as_verified(server):
    from evalanchor.agent_runner import run_experiment, default_configs
    from evalanchor.contracts import fingerprint
    from evalanchor.orderops import benchmark_suite, build_cases
    from test_agent_runner import ScriptedClient

    root, _, port = server
    case = build_cases('dev')[0]
    native = run_experiment(benchmark_suite(), default_configs()[:1], root/'replay-check',
                            client=ScriptedClient([case]), case_ids=[case['id']], split='dev')
    record = native['records'][0]
    record['final_state']['forged'] = True
    record['fingerprint'] = fingerprint({k:v for k,v in record.items() if k != 'fingerprint'})
    (root/'replay-check'/'experiment.json').write_text(json.dumps(native))
    status, raw, _ = request(port, 'GET', '/api/experiments/replay-check')
    assert status == 422
    assert 'recomputed task grade mismatch' in json.loads(raw)['error']
