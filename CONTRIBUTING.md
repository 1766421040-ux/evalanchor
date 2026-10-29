# Development

Use Python 3.12+; the full optional integration environment was verified on 3.12.
Core package has zero runtime dependencies. Install `.[test]` for tests and
`.[inspect]` for Inspect execution. `requirements-tested.txt` records the complete
local 3.12 optional environment; it is not the minimal installation requirement.

First run `python scripts/restore_evidence.py` to restore public evidence after
cloning. Run `python -m pytest -q`, `python scripts/verify_release.py`, then
`python -m evalanchor demo --output runs/new-name` for changes to evidence logic.
Do not rerun paid/model experiments merely to pass software tests.

Version 0.2 replays saved trajectory costs exactly. Python 3.12 changed float
summation relative to 3.10/3.11, which can change the last bit of historical
trajectory totals. Keep Python 3.12+ for this release; do not loosen comparison
tolerances or rewrite frozen records to bypass that compatibility constraint.

When scoring semantics change, bump SCORER_VERSION and regenerate separately
versioned evidence. Never rewrite historical raw outputs, silently drop errors,
or repair JSON in a scored result. Add a falsifying boundary case rather than a
test that only mirrors the implementation.

Adapters must preserve upstream provenance and reject incomplete or ambiguous
logs. New dataset licenses are independent of code licenses. Do not commit keys,
model weights, runtime databases, `.env` files, or private datasets.

Security-sensitive report checks include script-tag escaping, use of textContent
for raw outputs, no directory listing from the local server, literal-only key
loading, no redirects carrying Authorization, and single-writer resume locks.
