"""Report diagnostics never replace oracle scores or silently publish partial tests."""
import copy
import importlib.util
from pathlib import Path

import pytest

from evalanchor.contracts import ContractError, fingerprint

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/summarize_orderops_results.py"
spec = importlib.util.spec_from_file_location("orderops_results", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def record():
    return {"status": "completed", "group_id": "g", "case_id": "c", "grade": {
        "task_success": True, "goal_satisfied": True, "attempted_violations": 0,
        "executed_violations": 0, "failures": []}, "final_state": {"ledger": []}}


def test_safe_records_are_not_failure_examples():
    value = record()
    assert report.safe_success(value)
    assert report.classify_failure(value) == []
    assert not report.write_completed_but_reporting_failed(value)


def test_written_goal_without_finish_is_reported_but_never_promoted_to_success():
    value = record()
    value["status"] = "turn_limit"
    value["grade"].update(task_success=False, failures=["Missing valid finish tool call", "turn_limit"])
    value["final_state"]["ledger"] = [{"transaction_id": "tx1"}]
    assert report.write_completed_but_reporting_failed(value)
    assert not report.safe_success(value)
    assert report.classify_failure(value) == ["no_finish_or_turn_limit"]
    value["grade"]["attempted_violations"] = 1
    assert not report.write_completed_but_reporting_failed(value)
    assert "unsafe_attempt" in report.classify_failure(value)


def test_unchanged_lookup_goal_is_not_mislabeled_as_successful_write():
    value = record()
    value["grade"].update(task_success=False, failures=["Required final facts are missing or wrong"])
    assert not report.write_completed_but_reporting_failed(value)
    assert report.classify_failure(value) == ["wrong_facts"]


def test_diagnostics_are_multilabel_and_preserve_wrong_disposition_and_evidence():
    value = record()
    value["grade"].update(task_success=False, failures=[
        "Final outcome differs from required disposition", "Required observed evidence was not cited"])
    assert report.classify_failure(value) == ["wrong_disposition", "missing_or_wrong_evidence"]


def test_model_interface_error_is_distinct_from_expected_business_error():
    value = record()
    value["grade"].update(task_success=False, failures=["Final outcome differs from required disposition"])
    assert "interface_error" not in report.classify_failure(value)
    value["status"] = "model_error"
    assert "interface_error" in report.classify_failure(value)


def test_group_weighting_does_not_count_repeats_as_independent_groups():
    first = record()
    first["value"] = 0
    second = copy.deepcopy(first)
    second.update(group_id="other", case_id="other", value=1)
    assert report.weighted([first] * 10 + [second], lambda row: row["value"]) == .5


def frozen_pair():
    experiment = {"id":"test-v2", "status":"complete", "benchmark_hash":"hash", "configs":[{"id":"c"}],
                  "case_ids":["case"], "protocol":{"max_turns":8},
                  "provenance":{"prompts":{"direct":"fixed"},"engine_hash":"engine", "kind":"native_responses_tool_execution"}}
    frozen = {"benchmark_hash":"hash", "configs":[{"id":"c"}], "prompt_snapshots":{"direct":"fixed"}, "engine_hash":"engine",
              "experiments":[{"id":"test-v2","case_ids":["case"],"protocol":{"max_turns":8}}]}
    return experiment, frozen


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(status="running"),
    lambda value: value["protocol"].update(max_turns=12),
    lambda value: value["provenance"]["prompts"].update(direct="changed"),
    lambda value: value["provenance"].update(kind="fixture"),
])
def test_formal_validation_refuses_unfinished_changed_or_fixture_evidence(mutation):
    experiment, frozen = frozen_pair()
    report.validate_frozen_experiment(experiment,frozen)
    mutation(experiment)
    with pytest.raises(ContractError):
        report.validate_frozen_experiment(experiment,frozen)


def test_formal_loading_requires_both_test_inputs_before_writing(tmp_path, monkeypatch):
    frozen = {"experiments":[]}
    frozen["fingerprint"] = fingerprint(frozen)
    path = tmp_path / "protocol.json"
    import json
    path.write_text(json.dumps(frozen))
    output = tmp_path / "report.md"
    output.write_text("existing evidence report")
    with pytest.raises(ContractError,match="missing"):
        report.main(["--workspace",str(tmp_path),"--protocol",str(path),"--output",str(output)])
    assert output.read_text() == "existing evidence report"


def test_complete_primary_with_unfinished_secondary_still_cannot_publish(tmp_path, monkeypatch):
    experiment, frozen = frozen_pair()
    secondary = copy.deepcopy(experiment)
    secondary.update(id="test-long-v2",status="running")
    secondary["protocol"]["max_turns"] = 12
    frozen["experiments"].append({"id":"test-long-v2","case_ids":["case"],"protocol":{"max_turns":12}})
    frozen["fingerprint"] = fingerprint(frozen)
    def fake_read(path):
        if path.name == "protocol.json": return frozen,"hash"
        if path.parent.name == "test-v2": return experiment,"hash"
        if path.parent.name == "test-long-v2": return secondary,"hash"
        return {"id":path.parent.name,"status":"complete","protocol":{"split":"dev"}},"hash"
    monkeypatch.setattr(report,"_read_snapshot",fake_read)
    monkeypatch.setattr(Path,"is_file",lambda self:True)
    with pytest.raises(ContractError,match="test-long-v2 is not complete"):
        report.load_campaign(tmp_path,tmp_path/"protocol.json")


def test_development_preview_never_reads_test_evidence(tmp_path, monkeypatch):
    frozen={"experiments":[],"benchmark_hash":"bench"}
    frozen["fingerprint"] = fingerprint(frozen)
    accessed=[]
    def fake_read(path):
        accessed.append(path.parent.name)
        if path.name=="protocol.json": return frozen,"hash"
        assert path.parent.name in report.DEVELOPMENT
        return {"id":path.parent.name,"status":"complete","records":[],"protocol":{"split":"dev"},
                "provenance":{"prompts":{"direct":"test"}}},"hash"
    monkeypatch.setattr(report,"_read_snapshot",fake_read)
    monkeypatch.setattr(Path,"is_file",lambda self:True)
    monkeypatch.setattr(report,"summarize_experiment",lambda value:{"grid":{"experiment_finished":True,"complete_selected_grid":True},"configs":[],"comparisons":[]})
    value=report.load_campaign(tmp_path,tmp_path/"protocol.json",development_preview=True)
    assert value["formal_confirmation_complete"] is False
    assert not set(report.CONFIRMATION) & set(accessed)


def test_raw_json_code_fence_cannot_be_closed_by_model_text():
    original = '```\n<script>untrusted</script>\n```'
    block = report._json_block([original])
    assert block.startswith("````json\n") and block.endswith("\n````")
    assert "<script>untrusted</script>" in block


def test_representatives_are_bounded_deterministic_and_prefer_written_failure():
    rows = [{"experiment_id":"dev-v2","case_id":f"c{i}","config_id":"cfg","repeat":0,"family":f"f{i%3}",
             "taxonomy":["wrong_facts"],"write_completed_but_reporting_failed":i==12} for i in range(20)]
    first = report.select_representatives(rows)
    assert len(first) <= 8
    assert first[0]["case_id"] == "c12"
    assert first == report.select_representatives(list(reversed(rows)))
