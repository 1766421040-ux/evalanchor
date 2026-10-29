"""Generated EvalAnchor task. Only messages are sent to the model."""
import json
from pathlib import Path
from inspect_ai import task
from evalanchor.adapters import build_inspect_task

@task
def evalanchor_suite():
    suite = json.loads(Path(__file__).with_name('suite.json').read_text(encoding='utf-8'))
    return build_inspect_task(suite, repeats=2)
