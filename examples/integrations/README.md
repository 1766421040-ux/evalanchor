# Real framework execution evidence

`verified/receipt.json` points to genuine logs produced by Inspect AI 0.3.286 and Promptfoo 0.125.1, plus their locally rescored EvalAnchor runs. Both use synthetic baseline responses from the authored demo suite: 64 cases × 2 repetitions. The fixed outputs score 62.5% by construction. **These files verify integration and do not measure a language model.**

Inspect's `.eval` artifact is written by the official serializer from the same successful run. Promptfoo's native JSON is written by its actual CLI and Python provider. File hashes, versions and the local Inspect storage-directory adaptation are recorded in the receipt.

Reproduce with `scripts/verify_integrations.py` as documented in [integrations.md](../../docs/integrations.md). Native diagnostic traces and `.runtime` databases are temporary and excluded from release packaging. Early failed local-environment attempts are kept locally under `.runtime/previous-attempts` rather than mixed with the current verified logs.

The exported example task/config is editable. Changing inputs, targets, case IDs or repeat counts requires a new export and new run; importers reject changed identities instead of aligning rows heuristically.
