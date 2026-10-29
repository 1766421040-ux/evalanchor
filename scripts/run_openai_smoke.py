#!/usr/bin/env python3
"""Dry run by default; --execute sends 16 (or 32 with --repeats 2) requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Direct source-tree execution also works before an editable package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evalanchor.contracts import ContractError, read_json, write_json
from evalanchor.datasets import local_smoke_suite
from evalanchor.openai_provider import OpenAIResponsesProvider, budget_plan, load_api_key
from evalanchor.runner import run_suite


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually send the pre-budgeted requests")
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=1)
    parser.add_argument("--max-output-tokens", type=int, choices=(256, 512), default=256)
    parser.add_argument("--env-file", type=Path, default=root / ".env.local")
    parser.add_argument("--output", type=Path, default=root / "runs/openai-luna-smoke")
    args = parser.parse_args(argv)
    suite = local_smoke_suite()
    target = read_json(root / "examples/openai-target.json")
    target["max_tokens"] = args.max_output_tokens
    # The custom provider is outside the original core engine hash. Bind its
    # actual source to target revision so the journal cannot silently mix code.
    from evalanchor import openai_provider
    provider_hash = hashlib.sha256(Path(openai_provider.__file__).read_bytes()).hexdigest()
    target["revision"] += ":provider-sha256=" + provider_hash
    plan = budget_plan(suite, repeats=args.repeats, max_output_tokens=args.max_output_tokens)
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", **plan}, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    # Keys are loaded only in execution mode and are never placed in config,
    # report, command arguments, stdout, or persisted HTTP headers.
    key = load_api_key(args.env_file)
    provider = OpenAIResponsesProvider(target, api_key=key, max_calls=plan["planned_calls"])
    result = run_suite(suite, target, args.output, repeats=args.repeats, workers=1, retries=0, max_calls=plan["planned_calls"], provider=provider)
    records = result["records"]
    known = [r["cost_usd"] for r in records if r["cost_usd"] is not None]
    summary = {
        "calls_attempted": provider.calls,
        "records": len(records),
        "valid_response_records": sum(r["status"] == "ok" for r in records),
        "completed_responses": sum(isinstance(r.get("response"), dict) and r["response"].get("status") == "completed" for r in records),
        "incomplete_responses": sum(isinstance(r.get("response"), dict) and r["response"].get("status") == "incomplete" for r in records),
        "contract_passes": sum(r["score"] for r in records),
        "known_cost_subtotal_usd": sum(known),
        "unknown_cost_records": len(records) - len(known),
        "observed_models": sorted({r["response"]["model"] for r in records if isinstance(r.get("response"), dict) and isinstance(r["response"].get("model"), str)}),
        "output": str(args.output.resolve()),
        "note": "Real API outputs on 16 synthetic scenarios; no population-level model ranking. Remote alias is not proof of immutable weights.",
    }
    write_json(args.output / "budget-plan.json", plan)
    write_json(args.output / "smoke-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        # Contract errors are authored messages and never interpolate key values.
        raise SystemExit(str(exc)) from None
