# Inspect round trip

Run from this directory with EvalAnchor and inspect-ai installed:

```sh
inspect eval task.py --model mockllm/model --log-dir logs --log-format json
```

The default command is an infrastructure fixture, not a model-quality experiment. Replace the model only when a real endpoint is configured. Import the generated log against the same suite.json; changed input, case hashes, missing rows or repeats are rejected.
