"""Adapter contract tests. Synthetic log objects below are test doubles only."""
import copy
import json
import hashlib
from pathlib import Path

import pytest

from evalanchor.adapters import export_inspect, export_promptfoo, import_inspect, import_promptfoo
from evalanchor.contracts import assert_comparable, case_hash, suite_hash


@pytest.fixture
def suite():
    return {"schema_version": 1, "id": "adapter-contract-test", "title": "Test fixture",
            "cases": [{"id": f"case-{i}", "group_id": f"group-{i}", "tags": ["fixture"],
                       "messages": [{"role": "user", "content": f"Return OK {i}"}],
                       "scorer": "exact", "expected": "OK"} for i in range(2)]}


def binding(suite, case):
    return {"schema_version": 1, "suite_hash": suite_hash(suite), "case_id": case["id"],
            "case_hash": case_hash(case), "repeats": 1}


def inspect_log(suite):
    return {"status": "success", "version": 2, "eval": {"model": "mockllm/model"},
            "samples": [{"id": c["id"], "epoch": 1, "input": c["messages"],
                         "messages": c["messages"] + [{"role": "assistant", "content": "OK"}],
                         "metadata": {"evalanchor": binding(suite, c)},
                         "output": {"choices": [{"message": {"content": "OK"}}]},
                         "scores": {"upstream": {"value": 0}}} for c in suite["cases"]]}


def promptfoo_log(suite):
    return {"version": 3, "results": {"results": [
        {"provider": {"id": "fixture", "label": "evalanchor-fixture"},
         "testCase": {"metadata": {"evalanchor": {**binding(suite, c), "repeat": 0}}},
         "prompt": {"raw": json.dumps(c["messages"])}, "response": {"output": "OK"},
         "score": 0, "success": False, "error": "Native assertion failed"}
        for c in suite["cases"]]}}


def save(tmp_path, data, name="log.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.mark.parametrize("kind", ["inspect", "promptfoo"])
def test_round_trip_rescores_raw_output_not_native_score(tmp_path, suite, kind):
    maker, importer = (inspect_log, import_inspect) if kind == "inspect" else (promptfoo_log, import_promptfoo)
    run = importer(save(tmp_path, maker(suite)), suite, "import-test")
    assert [r["score"] for r in run["records"]] == [1.0, 1.0]
    assert all(r["status"] == "ok" for r in run["records"])
    assert run["provenance"]["synthetic"]
    assert run["provenance"]["source_sha256"]


@pytest.mark.parametrize("kind", ["inspect", "promptfoo"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "case_hash", "input", "unknown", "repeat"])
def test_contract_damage_is_rejected(tmp_path, suite, kind, mutation):
    data = inspect_log(suite) if kind == "inspect" else promptfoo_log(suite)
    rows = data["samples"] if kind == "inspect" else data["results"]["results"]
    row = rows[0]
    meta = row["metadata"]["evalanchor"] if kind == "inspect" else row["testCase"]["metadata"]["evalanchor"]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(row))
    elif mutation == "case_hash":
        meta["case_hash"] = "0" * 64
    elif mutation == "input":
        if kind == "inspect":
            row["input"] = [{"role": "user", "content": "changed"}]
        else:
            row["prompt"]["raw"] = '[{"role":"user","content":"changed"}]'
    elif mutation == "unknown":
        if kind == "inspect":
            row["id"] = "unknown"
        else:
            meta["case_id"] = "unknown"
    elif mutation == "repeat":
        if kind == "inspect":
            row["epoch"] = 0
        else:
            meta["repeat"] = True
    importer = import_inspect if kind == "inspect" else import_promptfoo
    with pytest.raises(ValueError):
        importer(save(tmp_path, data), suite, "bad")


def test_changed_private_expectation_also_rejected(tmp_path, suite):
    data = inspect_log(suite)
    changed = copy.deepcopy(suite)
    changed["cases"][0]["expected"] = "wrong"
    with pytest.raises(ValueError, match="hash"):
        import_inspect(save(tmp_path, data), changed, "bad")


def test_provider_failure_is_kept_and_scores_zero(tmp_path, suite):
    data = promptfoo_log(suite)
    row = data["results"]["results"][0]
    row["response"] = {"error": "connection timeout"}
    run = import_promptfoo(save(tmp_path, data), suite, "failed-provider")
    assert len(run["records"]) == 2
    assert run["records"][0]["score"] == 0
    assert run["records"][0]["status"] == "error"


def test_explicit_provider_selection_required(tmp_path, suite):
    data = promptfoo_log(suite)
    rows = data["results"]["results"]
    extra = copy.deepcopy(rows)
    for row in extra:
        row["provider"]["id"] = "second"
    rows.extend(extra)
    path = save(tmp_path, data)
    with pytest.raises(ValueError, match="Multiple providers"):
        import_promptfoo(path, suite, "ambiguous")
    assert len(import_promptfoo(path, suite, "chosen", "second")["records"]) == 2


def test_export_contains_no_target_in_model_prompt(tmp_path, suite):
    path = export_promptfoo(suite, tmp_path / "promptfoo", repeats=2)
    config = json.loads(path.read_text())
    assert config["prompts"] == ["{{messages_json}}"]
    assert len(config["tests"]) == 4
    for row in config["tests"]:
        assert set(row["vars"]) == {"messages_json"}
        assert "expected" not in row["metadata"]["evalanchor"]
    assert export_inspect(suite, tmp_path / "inspect").exists()


def test_promptfoo_structured_output_not_implicitly_json_encoded(tmp_path, suite):
    data = promptfoo_log(suite)
    data["results"]["results"][0]["response"]["output"] = {"answer": "OK"}
    with pytest.raises(ValueError, match="Raw output"):
        import_promptfoo(save(tmp_path, data), suite, "bad")


def test_inspect_incomplete_log_rejected(tmp_path, suite):
    data = inspect_log(suite)
    data["status"] = "error"
    with pytest.raises(ValueError, match="success status"):
        import_inspect(save(tmp_path, data), suite, "bad")


def test_duplicate_json_keys_rejected(tmp_path, suite):
    p = tmp_path / "duplicate.json"
    p.write_text('{"status":"error","status":"success"}')
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        import_inspect(p, suite, "bad")


def test_no_fuzzy_case_id_match(tmp_path, suite):
    data = inspect_log(suite)
    data["samples"][0]["id"] = "CASE-0"
    with pytest.raises(ValueError, match="Unknown Inspect sample ID"):
        import_inspect(save(tmp_path, data), suite, "bad")


def test_promptfoo_typed_digest_preserves_identity(tmp_path, suite):
    config = json.loads(export_promptfoo(suite, tmp_path / "export").read_text())
    data = promptfoo_log(suite)
    for row, test in zip(data["results"]["results"], config["tests"]):
        row["testCase"]["metadata"] = test["metadata"]
    run = import_promptfoo(save(tmp_path, data), suite, "typed")
    assert [row["score"] for row in run["records"]] == [1.0, 1.0]
    data["results"]["results"][0]["testCase"]["metadata"]["evalanchor"]["case_hash"]["bytes"][0] = True
    with pytest.raises(ValueError, match="hash"):
        import_promptfoo(save(tmp_path, data), suite, "corrupt")


def test_inspect_changed_solver_prompt_cannot_reuse_original_input(tmp_path, suite):
    data = inspect_log(suite)
    data["samples"][0]["messages"] = [{"role": "user", "content": "Hidden altered instruction"},
                                       {"role": "assistant", "content": "OK"}]
    with pytest.raises(ValueError, match="executed transcript"):
        import_inspect(save(tmp_path, data), suite, "bad-solver")


def test_real_framework_evidence_roundtrip_and_hashes():
    """Read committed genuine logs; this test does not manufacture native results."""
    root = Path(__file__).resolve().parents[1] / "examples" / "integrations" / "verified"
    receipt_path = root / "receipt.json"
    if not receipt_path.exists():
        pytest.skip("Run scripts/verify_integrations.py to produce genuine framework evidence")
    receipt = json.loads(receipt_path.read_text())
    native = receipt["frameworks"]
    if any(native.get(k, {}).get("status") != "passed" for k in ("inspect-ai", "promptfoo")):
        pytest.skip("Both upstream frameworks have not yet been executed")
    suite = json.loads((root / "inspect" / "suite.json").read_text())
    runs = []
    for key, importer in (("inspect-ai", import_inspect), ("promptfoo", import_promptfoo)):
        evidence = native[key]
        source = root / evidence["native_log"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == evidence["native_log_sha256"]
        imported_path = root / evidence["imported_run"]
        assert hashlib.sha256(imported_path.read_bytes()).hexdigest() == evidence["imported_run_sha256"]
        run = importer(source, suite, f"checked-{key}")
        assert len(run["records"]) == 128
        assert sum(r["score"] for r in run["records"]) == 80
        runs.append(run)
    assert_comparable(*runs)
    by_key = lambda run: {(r["case_id"], r["repeat"]): (r["output"], r["score"]) for r in run["records"]}
    assert by_key(runs[0]) == by_key(runs[1])


def test_real_inspect_binary_evidence():
    pytest.importorskip("inspect_ai")
    root = Path(__file__).resolve().parents[1] / "examples" / "integrations" / "verified" / "inspect"
    if not (root / "verified.eval").exists():
        pytest.skip("No genuine .eval evidence yet")
    suite = json.loads((root / "suite.json").read_text())
    run = import_inspect(root / "verified.eval", suite, "binary-evidence")
    assert len(run["records"]) == 128
    assert sum(r["score"] for r in run["records"]) == 80
