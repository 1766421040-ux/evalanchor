"""Small stable CLI; CI returns 0 pass, 1 regression, 2 insufficient, 3 invalid."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
from pathlib import Path

from .contracts import ContractError, audit_run, load_suite, read_json, validate_run, write_json


def _parser():
    p = argparse.ArgumentParser(prog="evalanchor", description="Auditable LLM evaluation and release evidence")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="Run synthetic fixtures end-to-end without models or API keys")
    d.add_argument("--output", type=Path, default=Path("runs/demo"))
    v = sub.add_parser("validate", help="Validate a suite or result file")
    v.add_argument("path", type=Path)
    r = sub.add_parser("run", help="Evaluate a replay, local model, or chat-completions endpoint")
    r.add_argument("--suite", required=True, type=Path)
    r.add_argument("--target", required=True, type=Path)
    r.add_argument("--output", required=True, type=Path)
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--retries", type=int, default=1)
    r.add_argument("--max-calls", type=int, default=10000)
    r.add_argument("--resume", action="store_true")
    c = sub.add_parser("compare", help="Strict paired comparison and three-state gate")
    c.add_argument("baseline", type=Path)
    c.add_argument("candidate", type=Path)
    c.add_argument("--policy", type=Path)
    c.add_argument("--output", required=True, type=Path)
    c.add_argument("--report", type=Path)
    c.add_argument("--suite", type=Path)
    e = sub.add_parser("export", help="Export a runnable framework task/config")
    e.add_argument("framework", choices=["inspect", "promptfoo"])
    e.add_argument("--suite", required=True, type=Path)
    e.add_argument("--output", required=True, type=Path)
    e.add_argument("--repeats", type=int, default=1)
    i = sub.add_parser("import", help="Import matching framework evidence and re-score raw outputs")
    i.add_argument("framework", choices=["inspect", "promptfoo"])
    i.add_argument("source", type=Path)
    i.add_argument("--suite", required=True, type=Path)
    i.add_argument("--id", required=True)
    i.add_argument("--provider-id")
    i.add_argument("--output", required=True, type=Path)
    j = sub.add_parser("judge-audit", help="Audit saved swapped-order judge decisions")
    j.add_argument("source", type=Path)
    j.add_argument("--output", required=True, type=Path)
    s = sub.add_parser("serve", help="Serve only a generated HTML report on loopback")
    s.add_argument("report", type=Path)
    s.add_argument("--port", type=int, default=8777)
    sub.add_parser("arena", help="OrderOps agent benchmark, experiments, replay and workbench")
    return p


def demo(output: Path):
    from .analysis import compare_runs, default_policy
    from .datasets import demo_outputs, demo_suite
    from .judge import audit_judgments
    from .report import write_report
    from .runner import run_suite
    if output.exists() and any(output.iterdir()):
        raise ContractError("Demo output must be a new or empty directory")
    suite = demo_suite()
    runs = []
    for variant in ("baseline", "candidate", "fixed"):
        target = {"id": variant, "provider": "replay", "model": "synthetic-fixture", "revision": "v1", "outputs": demo_outputs(suite, variant)}
        runs.append(run_suite(suite, target, output / variant, repeats=2, retries=0))
    policy = default_policy()
    comparisons = []
    for run in runs[1:]:
        comparison = compare_runs(runs[0], run, policy)
        comparison["baseline_id"] = runs[0]["id"]
        comparison["candidate_id"] = run["id"]
        comparisons.append(comparison)
        write_json(output / f"comparison-{run['id']}.json", comparison)
    judgments = [
        {"case_id": "stable-a", "winner_ab": "A", "winner_ba": "B", "reference": "A"},
        {"case_id": "position-biased", "winner_ab": "A", "winner_ba": "A", "reference": "B"},
        {"case_id": "invalid-response", "winner_ab": "invalid", "winner_ba": "B", "reference": "A"},
        {"case_id": "stable-tie", "winner_ab": "tie", "winner_ba": "tie", "reference": "tie"},
    ]
    audit = audit_judgments(judgments)
    audit["provenance"] = "Synthetic diagnostic decisions, not real model judges or human annotations"
    write_json(output / "judge-input.json", judgments)
    write_json(output / "judge-audit.json", audit)
    write_json(output / "suite.json", suite)
    write_json(output / "policy.json", policy)
    path = write_report(output / "index.html", suite=suite, runs=runs, comparisons=comparisons, judge_audit=audit)
    print(json.dumps({"report": str(path.resolve()), "decisions": [c["decision"] for c in comparisons], "kind": "synthetic fixture; not model quality"}, ensure_ascii=False, indent=2))


def _serve(path: Path, port: int):
    data = path.read_bytes()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"EvalAnchor: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'arena':
        from .agent_cli import main as arena_main
        try:
            return arena_main(arguments[1:])
        except (ContractError, ValueError, OSError, RuntimeError) as exc:
            print(f"EvalAnchor: {exc}", file=sys.stderr)
            return 3
    args = _parser().parse_args(argv)
    try:
        if args.command == "demo":
            demo(args.output)
        elif args.command == "validate":
            value = read_json(args.path)
            if "cases" in value:
                load_suite(args.path)
            else:
                audit_run(value)
            print("valid")
        elif args.command == "run":
            from .providers import prepare_target
            from .runner import run_suite
            target = prepare_target(read_json(args.target), args.target.parent)
            result = run_suite(load_suite(args.suite), target, args.output, repeats=args.repeats, workers=args.workers, retries=args.retries, max_calls=args.max_calls, resume=args.resume)
            print(json.dumps({"run": str((args.output / "run.json").resolve()), "records": len(result["records"]), "status": result["status"]}))
        elif args.command == "compare":
            from .analysis import compare_runs
            from .report import write_report
            baseline, candidate = read_json(args.baseline), read_json(args.candidate)
            result = compare_runs(baseline, candidate, read_json(args.policy) if args.policy else None)
            result.update(baseline_id=baseline["id"], candidate_id=candidate["id"])
            write_json(args.output, result)
            if args.report:
                write_report(args.report, suite=load_suite(args.suite) if args.suite else None, runs=[baseline, candidate], comparisons=[result])
            print(json.dumps({"decision": result["decision"], "reasons": result["reasons"]}, ensure_ascii=False, indent=2))
            return {"pass": 0, "fail": 1, "inconclusive": 2}[result["decision"]]
        elif args.command == "export":
            from . import adapters
            path = getattr(adapters, f"export_{args.framework}")(load_suite(args.suite), args.output, repeats=args.repeats)
            print(path)
        elif args.command == "import":
            from . import adapters
            kwargs = {"provider_id": args.provider_id} if args.framework == "promptfoo" and args.provider_id else {}
            run = getattr(adapters, f"import_{args.framework}")(args.source, load_suite(args.suite), args.id, **kwargs)
            write_json(args.output, run)
            print(f"Imported {len(run['records'])} records")
        elif args.command == "judge-audit":
            from .judge import audit_judgments
            result = audit_judgments(read_json(args.source))
            write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            _serve(args.report, args.port)
        return 0
    except (ContractError, ValueError, OSError, RuntimeError) as exc:
        print(f"EvalAnchor: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
