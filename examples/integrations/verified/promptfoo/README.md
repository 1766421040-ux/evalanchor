# Promptfoo round trip

```sh
promptfoo eval --config promptfooconfig.json --output results.json --no-cache
```

Default provider returns synthetic strings, not model responses. Replace providers with your configured model to evaluate it. Keep messages_json prompt and evalanchor metadata intact. The exporter explicitly expands repeats; do not also pass --repeat. Native assertion scores are preserved but raw outputs are rescored on import.
