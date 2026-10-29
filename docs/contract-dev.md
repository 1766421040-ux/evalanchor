# Internal contract, version 1

This file fixes module interfaces for implementation. Public docs supersede implementation notes.

Suite JSON: `schema_version:1`, `id`, `title`, `description`, `cases:[]`.
Case: `id:str`, `group_id:str`, `tags:list[str]`, `messages:list[{role,content}]`, `scorer:str`, `expected:any`.
Scorers return `{score:float [0,1], reason:str, details:dict}` and never repair raw output.
`metrics.score_case(case, output:str) -> dict`.
`datasets.demo_suite() -> dict`; `datasets.demo_outputs(suite, variant:str) -> dict[str,str]` with baseline/candidate/fixed variants. Fixtures explicitly synthetic, outputs not models.

`contracts.load_suite(path) -> dict` validates; `contracts.validate_suite(suite) -> dict`; `contracts.fingerprint(obj) -> str` is canonical SHA256 JSON. `contracts.case_hash(case) -> str` binds all case fields. `contracts.suite_hash(suite) -> str`. `contracts.protocol(repeats:int) -> dict` has repeats, scorer_version, fingerprint. Only messages enter model request; expected/tags/group never do.

Run dictionary: suite_definition=the full bound suite (required for audit/compare), schema_version=1, id, created_at, status=complete|incomplete, suite={id,fingerprint,case_count}, protocol={repeats,scorer_version,fingerprint}, target={id,provider,model,revision,...}, records=[...], provenance={...}.
Record: case_id, group_id, tags, repeat (0-based), input_hash (case_hash), output (raw str), status=ok|error, error (null|str), score (float), reason (str), latency_ms (float), usage (dict), cost_usd (null|float). Error records score zero, never omitted. Every case/repeat key exactly once for complete runs. Actual request/response retained separately where available.
Root writes `contracts.validate_run(run)` and `contracts.assert_comparable(left,right)`; compare requires exact same case/repeat/hash/group/tag grid, suite fingerprint, protocol fingerprint, complete statuses; source provider may differ.

`analysis.compare_runs(baseline:dict,candidate:dict,policy:dict|None=None) -> dict` result with decision=pass|fail|inconclusive, reasons:list[str], overall and slices list/dict, pairs list (case_id, repeat, group_id,tags, baseline_score,candidate_score,delta,baseline_output,candidate_output), policy, warnings. Stats agent may refine schema then tell root. `analysis.default_policy() -> dict`. Use independent groups as inferential unit, equal group weights; explicit conservative Hoeffding bounds formal, cluster bootstrap descriptive only. Detect critical regressions and report denominators. No missing rows dropped.

Adapters owned by framework agent: `adapters.export_inspect(suite, destination:Path)` generates runnable task; `adapters.export_promptfoo(suite,destination:Path)` generates config; `adapters.import_inspect(source:Path,suite:dict,run_id:str, scorer_name:str|None=None)->dict`; `adapters.import_promptfoo(source:Path,suite:dict,run_id:str,provider_id:str|None=None)->dict`; third lm_eval import if feasible. All imported raw outputs re-scored locally; retain native scores/provenance. Validate exact original input and/or source-preserved exported case hash, no silent row matching, rejects ambiguous/missing/duplicate data. No invented upstream logs; generate actual Inspect offline mock run for integration evidence if install works.

Judge audit (metrics/data agent): `judge.audit_judgments(rows:list[dict])->dict`, rows: case_id, winner_ab=A|B|tie|invalid, winner_ba=A|B|tie|invalid, reference=A|B|tie optional. Swapped ordering must be normalized, malformed counted, report positional disagreement and gold agreement. Does not count judge preferences as correctness. Optional create judge requests without target names and explicit untrusted output delimiting.

Root owns runner/providers/store/report/cli/project config/README. Agents own assigned modules/tests and research docs only. Use stdlib core Python>=3.12 for v0.2 exact historical cost replay (v0.1 originally used >=3.10); optional frameworks extras. No shared git commits during parallel development.
