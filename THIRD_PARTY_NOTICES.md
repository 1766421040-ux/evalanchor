# Attribution and licensing

EvalAnchor code and its explicitly labelled synthetic cases/outputs are MIT.
The project is an integration and independently written implementation, not a renamed upstream fork.

- **Inspect AI**: MIT, UK AI Security Institute / Meridian Labs and contributors. Optional installed dependency; public Task/Sample/Scorer/log APIs are called. [Source and license](https://github.com/UKGovernmentBEIS/inspect_ai).
- **Promptfoo**: MIT, Promptfoo contributors. Installed separately for validation. Generated provider wrappers call its documented interfaces; contract scoring runs locally after import. [Source and license](https://github.com/promptfoo/promptfoo).
- **Transformers**: Apache-2.0; **PyTorch**: BSD-style license. Optional local inference dependencies, not vendored in the release.
- **Qwen2.5-0.5B-Instruct**: Apache-2.0 according to its model repository. Local smoke work reuses separately installed weights; weights are not included. [Model repository](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct).
- **promptstats**, **FastChat**, **AlpacaEval**, **Inspect**, **Evidently** and the other audited projects are credited in research notes as prior work. No upstream algorithm or evaluation idea is claimed as novel merely because it is implemented here.
- **τ-bench / τ²-bench**: the [Sierra research project](https://github.com/sierra-research/tau2-bench) informed the comparison of state-based task evaluation and tool execution. OrderOps uses independently authored code, policies, cases, and scripted user events; no upstream benchmark dataset is copied, and these are not official τ-bench scores or a dual-control reproduction.
- **Matplotlib** is an optional local plotting dependency used to render the released empirical figures. It is not required by the runtime and its implementation is not vendored.
- **Phoenix** uses ELv2 in the inspected source and was not incorporated; **AlpacaEval data** carries CC BY-NC 4.0 and was not copied. Code and dataset licenses must be checked separately for future integrations.

Framework-generated verification logs contain EvalAnchor's own synthetic cases and outputs. Their framework versions, fixture status and provenance remain explicit. No proprietary third-party dataset, API credential, model checkpoint, or third-party source checkout is distributed with this project.
