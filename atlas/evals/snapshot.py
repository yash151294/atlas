"""Measure and freeze the evaluation numbers (redaction, resolution, retrieval, faithfulness, the gate, cost) to evals_snapshot.json; the docs quote these.
uv run python -m atlas.evals.snapshot"""
import json
from .. import llm, store
from ..config import PARAMS
from . import redaction_eval, resolve_eval


def numbers() -> dict:
    """Redaction and resolution evals computed live, retrieval and faithfulness from their cached files, the gate's counts from the serving table, cost from the ledger."""
    q = lambda sql: store.cache().q(sql)
    out = {}
    for name, fn in [("redaction", redaction_eval.evaluate), ("resolution", resolve_eval.evaluate)]:
        try: out[name] = fn()
        except Exception as e: out[name] = dict(error=str(e))
    t = store.read_text("retrieval_eval.json"); out["retrieval"] = json.loads(t) if t else dict(error="run: uv run python -m atlas.evals.run")
    t = store.read_text("faithfulness_eval.json"); out["faithfulness"] = json.loads(t) if t else dict(error="run: uv run python -m atlas.evals.faithfulness_eval")
    out["provider"] = llm.provider()
    red = q("SELECT tier, approved, approval_mode, risk_score, residual_hits, special_category FROM redacted") if store.cache().has("redacted") else []
    n = len(red) or 1
    out["gate"] = dict(documents=len(red), auto=sum(1 for r in red if r["tier"] == "auto"), review=sum(1 for r in red if r["tier"] == "review"),
                       auto_rate=sum(1 for r in red if r["tier"] == "auto") / n, with_residual_hits=sum(1 for r in red if any(not h.get("informational") for h in json.loads(r["residual_hits"] or "[]"))),
                       special_category=sum(1 for r in red if json.loads(r.get("special_category") or "[]")),
                       threshold=PARAMS["qa"]["auto_approve_below"], mean_risk=sum(r["risk_score"] or 0 for r in red) / n,
                       projected_15k=dict(human_reviews=round(15000 * sum(1 for r in red if r["tier"] == "review") / n)))
    calls = llm.calls()
    by = {}
    for c in calls:
        b = by.setdefault(c["purpose"], dict(calls=0, cost_usd=0.0, input=0, output=0, cache_read=0))
        b["calls"] += 1; b["cost_usd"] += c["cost_usd"]; b["input"] += c["input"]; b["output"] += c["output"]; b["cache_read"] += c["cache_read"]
    n_docs = len(red)
    n_chunks = q("SELECT count(*) AS n FROM chunks")[0]["n"] if store.cache().has("chunks") else 0
    mean = lambda k: (by[k]["cost_usd"] / by[k]["calls"]) if k in by and by[k]["calls"] else 0.0
    per_doc = mean("detect.extract") + mean("qa.residual") + mean("index.context") * (n_chunks / max(n_docs, 1))
    recent = [c for c in calls if c["purpose"] in ("answer", "plan")][-20:]
    per_answer = (sum(c["cost_usd"] for c in recent if c["purpose"] == "answer") / max(1, sum(1 for c in recent if c["purpose"] == "answer"))
                  + sum(c["cost_usd"] for c in recent if c["purpose"] == "plan") / max(1, sum(1 for c in recent if c["purpose"] == "plan"))) if recent else None
    ans = [c for c in calls if c["purpose"] == "answer"]
    out["cost"] = dict(by_purpose={k: {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by.items()},
                       per_document_usd=round(per_doc, 4), per_answer_usd=round(per_answer, 4) if per_answer else None,
                       answer_model=ans[-1]["model"] if ans else None, corpus_15k_usd=round(per_doc * 15000), corpus_15k_batch_usd=round(per_doc * 15000 * 0.5))
    return out


if __name__ == "__main__":
    out = numbers()
    store.write_text("evals_snapshot.json", json.dumps(out, indent=1, default=str))
    r, t, c, g = out["redaction"], out["retrieval"], out["cost"], out["gate"]
    print(f"redaction recall {r.get('overall_recall')} precision {r.get('overall_precision')} docs {r.get('n_docs')}")
    print("retrieval", t.get("table")); print("gate", g["auto"], "by policy of", g["documents"], "·", g["review"], "for a person")
    print("cost", c.get("per_document_usd"), "per document,", c.get("per_answer_usd"), "per briefing")
