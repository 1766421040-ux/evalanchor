"""Execute upstream frameworks with synthetic providers, then verify round trips.

No paid endpoint or real model is used. A passing receipt proves integration only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from evalanchor.adapters import (build_inspect_task, export_inspect, export_promptfoo,
                                import_inspect, import_promptfoo)
from evalanchor.contracts import fingerprint, suite_hash, write_json
from evalanchor.datasets import demo_outputs, demo_suite


def check_roundtrip(run, outputs, repeats):
    assert len(run["records"]) == len(outputs) * repeats
    assert run["status"] == "complete"
    assert all(row["output"] == outputs[row["case_id"]] for row in run["records"])
    assert all(row["status"] == "ok" for row in run["records"])
    assert sum(row["score"] for row in run["records"]) == 40 * repeats


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/integrations/verified"))
    parser.add_argument("--promptfoo-bin", type=Path, help="Installed promptfoo executable; no automatic download")
    parser.add_argument("--skip-inspect", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    suite = demo_suite()
    outputs = demo_outputs(suite, "baseline")
    receipt = {"generated_at": datetime.now(timezone.utc).isoformat(), "synthetic": True,
               "purpose": "Actual framework integration acceptance; not model capability results",
               "suite_hash": suite_hash(suite), "python_version": sys.version.split()[0],
               "frameworks": {}}
    if args.skip_inspect and (root / "receipt.json").exists():
        earlier = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        if earlier.get("suite_hash") == suite_hash(suite):
            receipt["frameworks"].update(earlier.get("frameworks", {}))
    repeats = 2
    if not args.skip_inspect:
        from inspect_ai import eval as inspect_eval
        from inspect_ai.log import write_eval_log
        from inspect_ai.model import ModelOutput, ModelUsage, get_model
        inspect_dir = root / "inspect"
        export_inspect(suite, inspect_dir, repeats=repeats)
        # Inspect 0.3.286 also writes discovery/cleanup state through platformdirs,
        # without a public data-root setting on macOS. Redirect only these storage
        # paths in this verification process; no runner/scorer/model code changes.
        from inspect_ai._util import appdirs
        runtime_dir = inspect_dir / ".runtime"
        appdirs.user_data_path = lambda package: runtime_dir / "data" / package
        appdirs.user_cache_path = lambda package: runtime_dir / "cache" / package
        # Keep the upstream diagnostic trace inside the requested output root.
        os.environ["INSPECT_TRACE_FILE"] = str(inspect_dir / "trace.log")
        by_messages = {fingerprint(c["messages"]): outputs[c["id"]] for c in suite["cases"]}

        def response(messages, tools, tool_choice, config):
            original = [{"role": m.role, "content": m.text} for m in messages]
            output = ModelOutput.from_content(model="mockllm/model", content=by_messages[fingerprint(original)])
            # A synthetic provider has no real token bill. Explicit zero fixture
            # usage also avoids downloading a tokenizer for this offline check.
            output.usage = ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0)
            return output

        model = get_model("mockllm/model", custom_outputs=response)
        logs = inspect_eval(build_inspect_task(suite, repeats=repeats), model=model,
                            log_dir=str(inspect_dir / "logs"), log_format="json",
                            display="plain", max_connections=1,
                            log_realtime=False, ctl_server=False)
        if len(logs) != 1 or logs[0].status != "success":
            raise RuntimeError("Actual Inspect execution did not succeed")
        source = Path(logs[0].location)
        run = import_inspect(source, suite, "inspect-real-framework-fixture")
        check_roundtrip(run, outputs, repeats)
        binary_log = inspect_dir / "verified.eval"
        write_eval_log(logs[0], location=binary_log, format="eval")
        binary_run = import_inspect(binary_log, suite, "inspect-binary-real-framework-fixture")
        check_roundtrip(binary_run, outputs, repeats)
        imported = inspect_dir / "imported-run.json"
        write_json(imported, run)
        receipt["frameworks"]["inspect-ai"] = {
            "version": importlib.metadata.version("inspect-ai"), "status": "passed",
            "samples": len(run["records"]), "repeats": repeats,
            "native_log": str(source.relative_to(root)), "native_log_sha256": sha(source),
            "binary_log": str(binary_log.relative_to(root)), "binary_log_sha256": sha(binary_log),
            "imported_run": str(imported.relative_to(root)), "imported_run_sha256": sha(imported),
            "provider": "mockllm/model", "output_preserved": True, "local_rescoring_verified": True,
            "runtime_path_override": "Inspect appdirs data/cache redirected locally; evaluation code unmodified",
            "usage": "Synthetic zero token counts; no token-cost measurement"}
        write_json(root / "receipt.json", receipt)

    if args.promptfoo_bin:
        executable = args.promptfoo_bin.resolve()
        promptfoo_dir = root / "promptfoo"
        export_promptfoo(suite, promptfoo_dir, repeats=repeats)
        write_json(promptfoo_dir / "fixture_outputs.json", {
            fingerprint(c["messages"]): outputs[c["id"]] for c in suite["cases"]})
        env = {**os.environ, "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_PYTHON": sys.executable,
               "PROMPTFOO_CONFIG_DIR": str(promptfoo_dir / ".runtime"), "NO_COLOR": "1"}
        version = subprocess.run([str(executable), "--version"], env=env, capture_output=True,
                                 text=True, check=True, timeout=30).stdout.strip()
        source = promptfoo_dir / "results.json"
        command = [str(executable), "eval", "--config", "promptfooconfig.json", "--output", "results.json", "--no-cache"]
        result = subprocess.run(command, cwd=promptfoo_dir, env=env, capture_output=True, text=True, timeout=240)
        (promptfoo_dir / "execution.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (promptfoo_dir / "execution.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Promptfoo failed with exit {result.returncode}; see execution.stderr.txt")
        run = import_promptfoo(source, suite, "promptfoo-real-framework-fixture")
        check_roundtrip(run, outputs, repeats)
        imported = promptfoo_dir / "imported-run.json"
        write_json(imported, run)
        receipt["frameworks"]["promptfoo"] = {
            "version": version, "status": "passed", "samples": len(run["records"]), "repeats": repeats,
            "native_log": str(source.relative_to(root)), "native_log_sha256": sha(source),
            "imported_run": str(imported.relative_to(root)), "imported_run_sha256": sha(imported),
            "provider": "local Python synthetic fixture", "output_preserved": True,
            "local_rescoring_verified": True}
    else:
        receipt["frameworks"]["promptfoo"] = {"status": "not_executed", "reason": "No --promptfoo-bin supplied"}
    write_json(root / "receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
