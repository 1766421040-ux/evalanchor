"""Concurrent evaluation with bounded calls and resumable immutable observations."""
from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import time
from pathlib import Path

from .contracts import ContractError, audit_run, case_hash, fingerprint, protocol, suite_hash, validate_run, validate_suite, write_json
from .providers import ProviderError, make_provider, prepare_target, public_target
from .store import RunStore


def _engine_hash():
    root = Path(__file__).parent
    return fingerprint({name: (root / name).read_text() for name in ("contracts.py", "metrics.py", "providers.py", "openai_provider.py", "runner.py", "store.py")})


def run_suite(suite: dict, target: dict, output: str | Path, *, repeats=1, workers=4, retries=1, max_calls=10000, resume=False, provider=None) -> dict:
    from .metrics import score_case
    validate_suite(suite)
    if type(workers) is not int or not 1 <= workers <= 32 or type(retries) is not int or not 0 <= retries <= 5:
        raise ContractError("workers must be 1..32, retries 0..5")
    proto = protocol(repeats)
    target = prepare_target(target)
    if target["provider"] == "transformers" and workers != 1:
        raise ContractError("Local transformers inference requires workers=1")
    required_calls = len(suite["cases"]) * repeats * (retries + 1)
    if type(max_calls) is not int or required_calls > max_calls:
        raise ContractError(f"Worst-case calls {required_calls} exceed max_calls={max_calls}")
    out = Path(output)
    if out.exists() and not (out / "journal.sqlite3").exists() and any(p.name != "journal.sqlite3.lock" for p in out.iterdir()):
        raise ContractError("Output directory is not an EvalAnchor journal and is not empty")
    identity = {"suite": suite_hash(suite), "protocol": proto, "target": public_target(target), "engine_hash": _engine_hash(), "retries": retries}
    store = RunStore(out / "journal.sqlite3", identity, resume)
    try:
        created = store.get_metadata("created_at") or dt.datetime.now(dt.timezone.utc).isoformat()
        store.set_metadata("created_at", created)
        base = {"schema_version": 1, "id": target["id"], "created_at": created, "status": "incomplete", "suite": {"id": suite["id"], "fingerprint": suite_hash(suite), "case_count": len(suite["cases"])}, "protocol": proto, "target": public_target(target), "provenance": {"engine_hash": identity["engine_hash"], "kind": "synthetic_fixture" if target["provider"] == "replay" else "model_inference", "sampling": suite.get("sampling", {"kind": "unspecified", "independent_groups": False}), "retry_policy": {"max_retries": retries, "retryable": "transport, HTTP429, HTTP5xx"}, "cost_note": "Recorded successful response token pricing only; failed attempts may have incurred unknown provider charges."}}
        write_json(out / "suite.json", suite)
        write_json(out / "target.json", public_target(target))
        base["suite_definition"] = suite
        # Audit the entire saved journal before trusting resume completion keys.
        base["records"] = store.records()
        audit_run(base)
        done = {(r["case_id"], r["repeat"]) for r in store.records()}
        pending = [(c, rep) for c in suite["cases"] for rep in range(repeats) if (c["id"], rep) not in done]

        def execute(case, rep):
            started = time.perf_counter()
            attempts = []
            response = None
            for index in range(retries + 1):
                stamp = time.perf_counter()
                try:
                    response = engine.generate(copy.deepcopy(case["messages"]), case["id"], rep)
                    attempts.append({"index": index, "status": "ok", "latency_ms": (time.perf_counter() - stamp) * 1000})
                    break
                except ProviderError as exc:
                    attempts.append({"index": index, "status": "error", "error": str(exc), "retryable": exc.retryable, "latency_ms": (time.perf_counter() - stamp) * 1000})
                    if not exc.retryable or index == retries:
                        break
                    time.sleep(min(0.25 * 2 ** index, 2))
            raw = response["output"] if response else ""
            score = score_case(case, raw) if response else {"score": 0.0, "reason": "provider_error", "details": {}}
            return {"case_id": case["id"], "group_id": case["group_id"], "tags": case["tags"], "repeat": rep, "input_hash": case_hash(case), "output": raw, "status": "ok" if response else "error", "error": None if response else attempts[-1]["error"], "score": score["score"], "reason": score["reason"], "details": score.get("details", {}), "latency_ms": (time.perf_counter() - started) * 1000, "usage": response.get("usage", {}) if response else {}, "cost_usd": response.get("cost_usd") if response and len(attempts) == 1 else None, "attempts": attempts, "request": response.get("request") if response else {"messages": case["messages"]}, "response": response.get("response") if response else None}

        try:
            if pending:
                engine = provider or make_provider(target)
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(execute, case, rep) for case, rep in pending]
                    failures = []
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            record = future.result()
                        except Exception as exc:
                            failures.append(exc)
                        else:
                            store.append(record)
                    if failures:
                        raise RuntimeError(f"{len(failures)} executor defect(s); completed evidence saved; first: {type(failures[0]).__name__}: {failures[0]}") from failures[0]
            base["status"] = "complete"
        finally:
            base["records"] = store.records()
            write_json(out / "run.json", base)
        audit_run(base)
        return base
    finally:
        store.close()
