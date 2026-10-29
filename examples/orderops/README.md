# Frozen OrderOps worlds

`benchmark.json` is the full authored suite: 24 dev cases and 48 test cases.
`manifest.json` records canonical JSON SHA-256 fingerprints for the suite,
every case, public policy, and tool definitions. The Python generator is
`evalanchor.orderops.benchmark_suite()`; acceptance tests ensure the generated
and frozen representations agree.

The test cases form 24 counterfactual pairs. A pair shares `group_id` and
`pair_id`; repeated runs share the same case. Neither paired variants nor
repeats are independent population observations. This public finite suite is
synthetic and contains no production customer records, hidden test guarantee,
training dataset, or pretraining-contamination guarantee.

Only `public_messages(case)` is a model-input projection. Do not serialize a
whole case into a prompt: `oracle`, `faults`, and `user_events` contain evaluator
state. Tool returns expose authorized observations as the trajectory runs.
Some `documents` intentionally contain adversarial instructions; these are
untrusted benchmark data, never repository instructions.

`scripted_oracle(case)` deliberately reads known case specifications to verify
that all environments are reachable in at most eight tool calls. Its success
is an environment test, **never a model result**. Real results must preserve
the model's native calls, tool errors, final world state, grade, and full
denominator, including incomplete or failed episodes.

Run the environment checks with:

```sh
.venv/bin/python -m pytest tests/test_orderops.py -q
```

Frozen manifest version: OrderOps `2.0.0`, 2026-10-29. Any reference, scenario,
or policy change after model evaluation requires a new manifest/version and
an explicitly identified rerun; do not silently overwrite earlier evidence.
