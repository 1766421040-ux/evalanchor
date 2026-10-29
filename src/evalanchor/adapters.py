"""Strict round-trip adapters for EvalAnchor-exported framework runs.

Native scores are evidence, never substituted for the local scoring contract.
The import contract intentionally rejects arbitrary dashboards/summary files.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import case_hash, fingerprint, loads, protocol, suite_hash, validate_suite
from .metrics import score_case


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _binding(suite: dict, case: dict, repeats: int) -> dict:
    return {"schema_version": 1, "suite_hash": suite_hash(suite),
            "case_id": case["id"], "case_hash": case_hash(case), "repeats": repeats}


def _digest_bytes(hex_digest: str) -> dict:
    """Typed non-secret digest; Promptfoo redacts bare 64-hex strings as secrets."""
    return {"algorithm": "sha256", "bytes": list(bytes.fromhex(hex_digest))}


def _digest(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"algorithm", "bytes"}:
        parts = value["bytes"]
        if (value["algorithm"] == "sha256" and isinstance(parts, list) and len(parts) == 32
                and all(type(p) is int and 0 <= p <= 255 for p in parts)):
            return bytes(parts).hex()
    return None


def _check_repeats(repeats: int) -> None:
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 1000:
        raise ValueError("repeats must be an integer in [1,1000]")


def export_inspect(suite: dict, destination: Path, repeats: int = 1) -> Path:
    """Write a runnable Inspect task; the default invocation uses its mock model."""
    validate_suite(suite)
    _check_repeats(repeats)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _write(destination / "suite.json", suite)
    task_path = destination / "task.py"
    task_path.write_text(
        '"""Generated EvalAnchor task. Only messages are sent to the model."""\n'
        "import json\nfrom pathlib import Path\nfrom inspect_ai import task\n"
        "from evalanchor.adapters import build_inspect_task\n\n"
        "@task\ndef evalanchor_suite():\n"
        "    suite = json.loads(Path(__file__).with_name('suite.json').read_text(encoding='utf-8'))\n"
        f"    return build_inspect_task(suite, repeats={repeats})\n",
        encoding="utf-8",
    )
    (destination / "README.md").write_text(
        "# Inspect round trip\n\nRun from this directory with EvalAnchor and inspect-ai installed:\n\n"
        "```sh\ninspect eval task.py --model mockllm/model --log-dir logs --log-format json\n```\n\n"
        "The default command is an infrastructure fixture, not a model-quality experiment. "
        "Replace the model only when a real endpoint is configured. Import the generated log "
        "against the same suite.json; changed input, case hashes, missing rows or repeats are rejected.\n",
        encoding="utf-8",
    )
    return task_path


def build_inspect_task(suite: dict, repeats: int = 1):
    """Build an actual Inspect Task through its public Python extension API."""
    validate_suite(suite)
    _check_repeats(repeats)
    try:
        from inspect_ai import Task
        from inspect_ai.dataset import Sample
        from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageUser
        from inspect_ai.scorer import Score, mean, scorer
        from inspect_ai.solver import generate
    except ImportError as exc:
        raise RuntimeError("Inspect integration requires: pip install 'evalanchor[inspect]'") from exc
    constructors = {"system": ChatMessageSystem, "user": ChatMessageUser, "assistant": ChatMessageAssistant}
    cases = {c["id"]: c for c in suite["cases"]}

    @scorer(metrics=[mean()], name="evalanchor_contract")
    def contract_scorer():
        async def score(state, target):
            result = score_case(cases[str(state.sample_id)], state.output.completion)
            return Score(value=result["score"], answer=state.output.completion,
                         explanation=result["reason"], metadata=result.get("details", {}))
        return score

    samples = [Sample(id=c["id"], input=[constructors[m["role"]](content=m["content"])
                      for m in c["messages"]], metadata={"evalanchor": _binding(suite, c, repeats)})
               for c in suite["cases"]]
    return Task(dataset=samples, solver=generate(), scorer=contract_scorer(), epochs=repeats)


def export_promptfoo(suite: dict, destination: Path, repeats: int = 1) -> Path:
    """Write Promptfoo JSON config and an explicitly synthetic Python provider.

    The provider fixture is isolated from the suite's private scoring fields.
    Replace providers in config to run a real model; keep prompt and metadata.
    """
    validate_suite(suite)
    _check_repeats(repeats)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _write(destination / "suite.json", suite)
    tests = []
    for c in suite["cases"]:
        for repeat in range(repeats):
            binding = _binding(suite, c, repeats)
            binding["suite_hash"] = _digest_bytes(binding["suite_hash"])
            binding["case_hash"] = _digest_bytes(binding["case_hash"])
            binding["repeat"] = repeat
            tests.append({"description": f"{c['id']} / repetition {repeat}",
                          "vars": {"messages_json": json.dumps(c["messages"], ensure_ascii=False)},
                          "metadata": {"evalanchor": binding}})
    config = {"description": "EvalAnchor round trip — synthetic fixture by default",
              "prompts": ["{{messages_json}}"],
              "providers": [{"id": "file://fixture_provider.py", "label": "evalanchor-fixture"}],
              "tests": tests, "evaluateOptions": {"maxConcurrency": 1}}
    config_path = destination / "promptfooconfig.json"
    _write(config_path, config)
    # Fixtures contain no suite targets: default returns a visibly synthetic marker.
    _write(destination / "fixture_outputs.json", {
        fingerprint(c["messages"]): "EVALANCHOR_SYNTHETIC_FIXTURE" for c in suite["cases"]})
    (destination / "fixture_provider.py").write_text(
        '"""Synthetic transport fixture; never a real model result."""\n'
        "import hashlib\nimport json\nfrom pathlib import Path\n\n"
        "def call_api(prompt, options, context):\n"
        "    messages = json.loads(prompt)\n"
        "    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',', ':'))\n"
        "    key = hashlib.sha256(canonical.encode('utf-8')).hexdigest()\n"
        "    fixtures = json.loads(Path(__file__).with_name('fixture_outputs.json').read_text(encoding='utf-8'))\n"
        "    if key not in fixtures:\n        raise ValueError('Unexpected fixture prompt')\n"
        "    return {'output': fixtures[key], 'metadata': {'evalanchor_synthetic_fixture': True}}\n",
        encoding="utf-8",
    )
    (destination / "README.md").write_text(
        "# Promptfoo round trip\n\n"
        "```sh\npromptfoo eval --config promptfooconfig.json --output results.json --no-cache\n```\n\n"
        "Default provider returns synthetic strings, not model responses. Replace providers with "
        "your configured model to evaluate it. Keep messages_json prompt and evalanchor metadata intact. "
        "The exporter explicitly expands repeats; do not also pass --repeat. "
        "Native assertion scores are preserved but raw outputs are rescored on import.\n",
        encoding="utf-8",
    )
    return config_path


def _json_file(source: Path) -> dict:
    if Path(source).stat().st_size > 100_000_000:
        raise ValueError("Import exceeds the 100 MB JSON size limit")
    data = loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON evaluation object")
    return data


def _messages(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Expected original structured messages; string-only input is not accepted")
    result = []
    for message in value:
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Only text messages are supported by this adapter version")
        result.append({"role": message.get("role"), "content": message["content"]})
    return result


def _record(case: dict, repeat: int, output: Any, error: Any, *, latency=0, usage=None,
            native=None, cost=None) -> dict:
    if not isinstance(output, str):
        raise ValueError("Raw output must be a string; the importer never repairs or serializes it")
    failed = error is not None
    result = {"score": 0.0, "reason": "provider_error", "details": {}} if failed else score_case(case, output)
    return {"case_id": case["id"], "group_id": case["group_id"], "tags": case["tags"],
            "repeat": repeat, "input_hash": case_hash(case), "output": output,
            "status": "error" if failed else "ok", "error": str(error) if failed else None,
            "score": result["score"], "reason": result["reason"],
            "latency_ms": float(latency or 0), "usage": usage or {}, "cost_usd": cost,
            "details": result.get("details", {}), "native": native or {}}


def _check_binding(binding: Any, case: dict, suite: dict) -> int:
    if not isinstance(binding, dict) or binding.get("schema_version") != 1:
        raise ValueError("Missing or unsupported EvalAnchor export metadata")
    if (binding.get("case_id") != case["id"] or _digest(binding.get("case_hash")) != case_hash(case)
            or _digest(binding.get("suite_hash")) != suite_hash(suite)):
        raise ValueError("Case identity, case hash, or suite hash changed")
    repeats = binding.get("repeats")
    _check_repeats(repeats)
    return repeats


def _finish(source: Path, suite: dict, run_id: str, records: list[dict], repeats: int,
            target: dict, provenance: dict) -> dict:
    expected = {(c["id"], r) for c in suite["cases"] for r in range(repeats)}
    keys = [(r["case_id"], r["repeat"]) for r in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate case/repeat rows in framework output")
    if set(keys) != expected:
        raise ValueError("Missing, unknown, or unexpected case/repeat rows in framework output")
    run = {"schema_version": 1, "id": run_id,
           "created_at": datetime.now(timezone.utc).isoformat(), "status": "complete",
           "suite": {"id": suite["id"], "fingerprint": suite_hash(suite), "case_count": len(suite["cases"])},
           "suite_definition": suite,
           "protocol": protocol(repeats), "target": target, "records": records,
           "provenance": {"kind": "framework_import", "source_file": Path(source).name,
                          "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                          "sampling": suite.get("sampling", {"kind": "unspecified", "independent_groups": False}),
                          "rescored_locally": True, **provenance}}
    from .contracts import audit_run
    audit_run(run, suite)
    return run


def import_inspect(source: Path, suite: dict, run_id: str, scorer_name: str | None = None) -> dict:
    """Read an actual Inspect log exported by this adapter, preserving native scores."""
    validate_suite(suite)
    source = Path(source)
    if source.suffix == ".eval":
        try:
            from inspect_ai.log import read_eval_log
        except ImportError as exc:
            raise RuntimeError("Reading .eval requires inspect-ai; JSON import needs no optional dependency") from exc
        data = read_eval_log(str(source)).model_dump(mode="json")
    else:
        data = _json_file(source)
    if data.get("status") != "success":
        raise ValueError("Inspect log must have success status; incomplete runs cannot become complete evidence")
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Inspect log has no raw samples")
    cases = {c["id"]: c for c in suite["cases"]}
    records, counts = [], set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("Inspect samples must be objects")
        cid = sample.get("id")
        if cid not in cases:
            raise ValueError(f"Unknown Inspect sample ID: {cid!r}")
        case = cases[cid]
        count = _check_binding((sample.get("metadata") or {}).get("evalanchor"), case, suite)
        counts.add(count)
        if _messages(sample.get("input")) != case["messages"]:
            raise ValueError("Inspect original messages changed")
        transcript = sample.get("messages")
        prompt_length = len(case["messages"])
        if (not isinstance(transcript, list)
                or _messages(transcript[:prompt_length]) != case["messages"]):
            raise ValueError("Inspect executed transcript changed the original prompt")
        if sample.get("error") is None and (
                len(transcript) != prompt_length + 1 or transcript[-1].get("role") != "assistant"):
            raise ValueError("This adapter accepts only the exported single-generation Inspect workflow")
        epoch = sample.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or not 1 <= epoch <= count:
            raise ValueError("Invalid Inspect epoch")
        output = sample.get("output") or {}
        completion = output.get("completion")
        if completion is None:
            choices = output.get("choices") or []
            if choices:
                if len(choices) != 1:
                    raise ValueError("Multiple Inspect output choices are ambiguous")
                completion = choices[0].get("message", {}).get("content")
                if isinstance(completion, list):
                    if not all(isinstance(c, dict) and c.get("type") == "text" for c in completion):
                        raise ValueError("Non-text Inspect completion is unsupported")
                    completion = "".join(c["text"] for c in completion)
            elif sample.get("error") is not None:
                completion = ""
            else:
                raise ValueError("Inspect sample has no raw completion")
        scores = sample.get("scores") or {}
        if scorer_name is not None and scorer_name not in scores:
            raise ValueError(f"Requested native scorer {scorer_name!r} is absent")
        records.append(_record(case, epoch - 1, completion, sample.get("error"),
                               latency=1000 * sample.get("total_time", 0),
                               usage=output.get("usage"), native={"scores": scores,
                               "latency_source": "Inspect sample total_time; includes task work",
                               "model": output.get("model"), "sample_id": cid, "epoch": epoch}))
    if len(counts) != 1:
        raise ValueError("Inconsistent exported repetition counts")
    native_eval = data.get("eval") or {}
    model = native_eval.get("model", "unknown")
    return _finish(source, suite, run_id, records, counts.pop(),
                   {"id": f"inspect:{model}", "provider": "inspect", "model": model,
                    "revision": None},
                   {"framework": "inspect-ai", "log_schema_version": data.get("version"),
                    "native_eval": native_eval,
                    "synthetic": str(model).startswith("mockllm/")})


def import_promptfoo(source: Path, suite: dict, run_id: str, provider_id: str | None = None) -> dict:
    """Import full Promptfoo JSON, selecting exactly one provider and raw prompt."""
    validate_suite(suite)
    source = Path(source)
    data = _json_file(source)
    wrapper = data.get("results")
    rows = wrapper.get("results") if isinstance(wrapper, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected full Promptfoo JSON results.results with raw rows")
    def identity(row):
        if not isinstance(row, dict):
            raise ValueError("Promptfoo result rows must be objects")
        p = row.get("provider")
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            raise ValueError("Every Promptfoo row must identify its provider")
        return p["id"]
    identities = {identity(row) for row in rows}
    if provider_id is None:
        if len(identities) != 1:
            raise ValueError("Multiple providers: explicitly select provider_id")
        provider_id = identities.pop()
    if provider_id not in {identity(row) for row in rows}:
        raise ValueError("Selected Promptfoo provider is absent")
    selected = [row for row in rows if identity(row) == provider_id]
    cases = {c["id"]: c for c in suite["cases"]}
    records, counts, labels = [], set(), set()
    for row in selected:
        test = row.get("testCase") or {}
        binding = (test.get("metadata") or {}).get("evalanchor")
        cid = binding.get("case_id") if isinstance(binding, dict) else None
        if cid not in cases:
            raise ValueError("Unknown or missing Promptfoo case ID")
        case = cases[cid]
        count = _check_binding(binding, case, suite)
        counts.add(count)
        repeat = binding.get("repeat")
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 0 <= repeat < count:
            raise ValueError("Invalid exported Promptfoo repeat")
        raw_prompt = (row.get("prompt") or {}).get("raw")
        try:
            messages = loads(raw_prompt)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Promptfoo raw rendered prompt must preserve messages JSON") from exc
        if _messages(messages) != case["messages"]:
            raise ValueError("Promptfoo original messages changed")
        response = row.get("response") or {}
        # A native assertion failure is not a provider failure. The raw response
        # is rescored locally; transport errors have no successful response.
        error = response.get("error") or (row.get("error") if "output" not in response else None)
        output = response.get("output", "" if error else None)
        labels.add(row["provider"].get("label", ""))
        records.append(_record(case, repeat, output, error,
                               latency=row.get("latencyMs"), usage=response.get("tokenUsage"),
                               cost=row.get("cost", response.get("cost")), native={"score": row.get("score"),
                               "success": row.get("success"), "gradingResult": row.get("gradingResult"),
                               "error": row.get("error"),
                               "provider": row["provider"]}))
    if len(counts) != 1:
        raise ValueError("Inconsistent exported repetition counts")
    return _finish(source, suite, run_id, records, counts.pop(),
                   {"id": f"promptfoo:{provider_id}", "provider": "promptfoo",
                    "model": provider_id, "revision": None},
                   {"framework": "promptfoo", "export_version": data.get("version", wrapper.get("version")),
                    "synthetic": "evalanchor-fixture" in labels,
                    "native_provider_labels": sorted(labels)})
