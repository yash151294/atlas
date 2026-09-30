"""Build the golden set (once) and cache the retrieval eval table. uv run python -m atlas.evals.run"""
from .. import store
import json
from . import retrieval_eval
if not store.exists("gold_golden_qa"):
    retrieval_eval.build_golden()
store.write_text("retrieval_eval.json", json.dumps(retrieval_eval.evaluate(), indent=1))
print(store.read_text("retrieval_eval.json"))
