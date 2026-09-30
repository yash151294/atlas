"""Cross-encoder rerank of the fused candidates. Local ONNX model for the demo; Cohere,
Voyage or Claude-as-reranker are the production options inside the tenancy."""
from ..config import PARAMS
_rr = None


def reranker():
    global _rr
    if _rr is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _rr = TextCrossEncoder(model_name=PARAMS["index"]["reranker_model"])
    return _rr


def rerank(query: str, cands: list[dict], k: int | None = None) -> list[dict]:
    if not cands:
        return []
    n = PARAMS["index"].get("rerank_max_chars", 1500)
    cands = cands[: PARAMS["index"].get("rerank_candidates", len(cands))]
    scores = list(reranker().rerank(query, [(c["header"] + "\n" + c["text"] if c.get("header") else c["text"])[:n] for c in cands]))
    for c, s in zip(cands, scores):
        c["rerank"] = float(s)
    return sorted(cands, key=lambda c: c["rerank"], reverse=True)[: (k or PARAMS["index"]["rerank_k"])]
