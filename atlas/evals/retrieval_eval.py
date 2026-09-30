"""Retrieval pass@k on a golden question set, one row per retrieval configuration, so the
room sees what each stage of contextual retrieval buys. Golden questions are generated once
from the corpus by Claude (question + the chunk that answers it) and saved to parquet. Every row runs on one engine,
named per row: the keyword leg is BM25 in DuckDB and a term-hit count in the Eventhouse, so a mixed table is unreadable."""
import pandas as pd
from pydantic import BaseModel, Field
from .. import eventhouse, store
from ..config import PARAMS
from ..llm import complete
from ..nodes import index, rerank


class QA(BaseModel):
    question: str = Field(description="a question an analyst would ask that this chunk answers; do not name people, use their role or organisation")
    answer: str


def build_golden(n: int = 25) -> pd.DataFrame:
    chunks = store.read("gold_chunks")
    pick = chunks[chunks.text.str.len() > 600].sample(n, random_state=7)
    rows = []
    for c in pick.itertuples():
        qa = complete(f"<chunk project=\"{c.project}\">\n{c.text}\n</chunk>\nWrite one specific question this chunk answers and the short answer.", purpose="eval.golden", schema=QA, effort="low")
        rows.append(dict(question=qa.question, answer=qa.answer, golden_chunk=c.chunk_id, golden_doc=c.doc_id, project=c.project))
    df = pd.DataFrame(rows); store.write("gold_golden_qa", df); return df


def engine_of(mode: str) -> str:
    """Which engine served this row, recorded because the table is only readable if every row ran on one: until 15 Sep
    index.retrieve sent only hybrid to the Eventhouse and the single-index rows fell back to DuckDB, so the table compared
    BM25 in DuckDB against term hits in KQL. Set EVENTHOUSE_URI to measure the engine the app runs on, unset it for BM25."""
    return "eventhouse" if eventhouse.configured() else "duckdb"


def evaluate(ks=(5, 10, 20)) -> dict:
    qa = store.read("gold_golden_qa")
    configs = {"Keyword only": dict(mode="bm25"), "Embeddings only": dict(mode="dense"), "Hybrid (RRF)": dict(mode="hybrid"), "Hybrid + rerank": dict(mode="hybrid", rr=True)}
    table = []
    for name, cfg in configs.items():
        hits = {k: 0 for k in ks}
        for q in qa.itertuples():
            res = index.retrieve(q.question, k=max(ks) if not cfg.get("rr") else PARAMS["index"]["recall_k"], mode=cfg["mode"])
            if cfg.get("rr"):
                res = rerank.rerank(q.question, res, k=max(ks))
            ids = [r["chunk_id"] for r in res]
            docs = [r["doc_id"] for r in res]
            for k in ks:
                hits[k] += (q.golden_chunk in ids[:k]) or (q.golden_doc in docs[:k] and k >= 10)
        table.append(dict(config=name, engine=engine_of(cfg["mode"]), **{f"pass@{k}": round(hits[k] / len(qa), 3) for k in ks}))
    return dict(n_questions=int(len(qa)), engines=sorted({r["engine"] for r in table}), table=table, contextual_headers=PARAMS["index"]["contextual_headers"])
