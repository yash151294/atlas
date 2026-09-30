"""Gold: retained documents -> contextualised chunks -> two indexes (BM25 + dense) in DuckDB.
Anthropic's contextual retrieval: Claude writes a short situating header per chunk with the
whole document in a cached prompt; header + who-is-who legend + chunk is what gets indexed by
both BM25 and the embedding model. Retrieval fuses both with reciprocal rank fusion and applies
any structured filters (project, region, sector, organisation, dates) before ranking; a cross-encoder
reranks. Serving copies are Delta tables (store.write_table); the Eventhouse reads them in production."""
import os, re
from functools import lru_cache
import numpy as np, pandas as pd
from ..config import PARAMS
from ..llm import complete
from .. import store
from .redact import legend

P = PARAMS["index"]
TURN = re.compile(r"^(CONTRIBUTOR|SOURCE|[A-Z ]{3,20}):", re.M)
TOKEN = re.compile(r"\[PERSON_\d{4}\]")
STOP = set("a an the of at in on for to and or with who can reach someone anyone find me our we is are was were what which any about from into by has have had do does how".split())
_emb = None


class _AzureEmbedder:
    """Same .embed() shape as fastembed, served by the tenant's Azure OpenAI deployment."""
    def embed(self, texts, batch_size=64):
        from .. import llm_azure
        for v in llm_azure.embed(list(texts)):
            yield np.asarray(v, dtype="float32")


def embedder():
    global _emb
    if _emb is None:
        from .. import llm_azure
        if llm_azure.enabled() and os.environ.get("AZURE_OPENAI_EMBED"):
            _emb = _AzureEmbedder()
        else:
            from fastembed import TextEmbedding
            _emb = TextEmbedding(model_name=P["embedding_model"])
    return _emb


@lru_cache(maxsize=512)
def embed_query(q: str) -> tuple[float, ...]:
    """The query vector, memoised per process: the same question asked again (a follow-up, an eval pass) costs no model call."""
    return tuple(float(x) for x in list(embedder().embed([q]))[0])


def terms(q: str) -> list[str]:
    """Content words of a query for exact-term matching (the Eventhouse keyword score and the network's term overlap)."""
    return [t for t in re.findall(r"[a-z0-9&]+", q.lower()) if t not in STOP and len(t) > 2]


def units(text: str, doc_type: str) -> list[str]:
    """Transcripts split on speaker turns; deliverables on blank lines / headings."""
    if doc_type == "transcript" and TURN.search(text):
        parts = re.split(r"\n(?=(?:CONTRIBUTOR|SOURCE|CONTRIBUTOR NOTES|Assessment)\b)", text)
        return [p.strip() for p in parts if p.strip()]
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def chunk(text: str, doc_type: str) -> list[dict]:
    target = P["chunk_target_tokens"] * 4  # ~4 chars per token
    us, out, cur = units(text, doc_type), [], []
    def flush():
        if cur:
            out.append("\n".join(cur))
    for u in us:
        if sum(map(len, cur)) + len(u) > target and cur:
            flush(); cur = cur[-P["chunk_overlap_turns"]:] if P["chunk_overlap_turns"] else []
        cur.append(u)
    flush()
    return [dict(n=i, text=c, speakers="; ".join(sorted(set(TURN.findall(c))))) for i, c in enumerate(out)]


CTX_SYSTEM = "You situate chunks of a document for search. Answer only with the context, 40 to 80 words, no preamble. Refer to people by their token and descriptor from the legend, never guess names."


def contextualise(doc_text: str, chunks: list[dict], people_json: str = "[]") -> list[str]:
    if not P["contextual_headers"]:
        return [""] * len(chunks)
    sys = f"{CTX_SYSTEM}\n<legend>\n{legend(people_json)}\n</legend>\n<document>\n{doc_text}\n</document>"   # cached: same prefix for every chunk of the document
    try:
        return [complete(f"<chunk>\n{c['text']}\n</chunk>\nGive a short succinct context to situate this chunk within the overall document (who is speaking about what, which project, which organisation) for the purposes of improving search retrieval of the chunk.",
                     system=sys, purpose="index.context", effort="low", max_tokens=300) for c in chunks]
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("contextual headers unavailable: %s", e)
        return [""] * len(chunks)


def build_chunks(redacted: pd.DataFrame, workers: int = 6) -> pd.DataFrame:
    from concurrent.futures import ThreadPoolExecutor
    cache = {}
    old = store.read("gold_chunks")   # headers already written for identical chunk text are reused
    if old is not None:
        cache = {(r.doc_id, r.text): r.header for r in old[["doc_id", "text", "header"]].itertuples() if r.header}
    R = store.clean(redacted)
    def one(d):
        cs = chunk(d["redacted_text"], d["doc_type"])
        todo = [c for c in cs if (d["doc_id"], c["text"]) not in cache]
        new = dict(zip([c["text"] for c in todo], contextualise(d["redacted_text"], todo, d.get("people") or "[]"))) if todo else {}
        hs = [cache.get((d["doc_id"], c["text"]), new.get(c["text"], "")) for c in cs]
        rows = []
        for c, h in zip(cs, hs):
            who = legend(d.get("people") or "[]", set(TOKEN.findall(c["text"])))
            rows.append(dict(chunk_id=f'{d["doc_id"]}#{c["n"]}', doc_id=d["doc_id"], n=c["n"], doc_type=d["doc_type"], project=d.get("project"), doc_date=d.get("doc_date"),
                             source_token=d.get("source_token"), source_descriptor=d.get("source_descriptor"), source_org=d.get("source_org"),
                             source_region=d.get("source_region"), source_sector=d.get("source_sector"), speakers=c["speakers"], header=h, people=who, text=c["text"],
                             ctx_text="\n\n".join(filter(None, [h, ("People: " + who.replace("\n", "; ")) if who else "", c["text"]]))))
        return rows
    with ThreadPoolExecutor(workers) as ex:
        rows = [r for rs in ex.map(one, R.to_dict("records")) for r in rs]
    df = pd.DataFrame(rows)
    df["embedding"] = [v.tolist() for v in embedder().embed(df.ctx_text.tolist(), batch_size=32)]
    return df


def network_texts(sources: pd.DataFrame, people: pd.DataFrame) -> pd.DataFrame:
    """One searchable line per person in the network (sources and mentioned professionals)."""
    S, Pp = store.clean(sources), store.clean(people)
    rows = [dict(entity_id=s["entity_id"], kind="source", text=". ".join(filter(None, [s["descriptor"], s["former_orgs"] and f'formerly {s["former_orgs"]}', s["region_norm"], s["country"], s["region"], s["sector"], s["seniority"], s["topics"] and f'access to: {s["topics"]}'])))
            for s in S.to_dict("records")]
    via_region = {}
    for s in S.to_dict("records"):
        via_region[s["entity_id"]] = ". ".join(filter(None, [s["region_norm"], s["country"], s["sector"]]))
    rows += [dict(entity_id=p["entity_id"], kind="mentioned", text=". ".join(filter(None, [p["descriptor"], p["relationship"] and f'({p["relationship"]} of a source)', via_region.get((p["via"] or "").split("; ")[0], "")])))
             for p in Pp.to_dict("records")]
    df = pd.DataFrame(rows)
    df["embedding"] = [v.tolist() for v in embedder().embed(df.text.tolist(), batch_size=64)] if len(df) else []
    return df


def _q(s):  # SQL string literal
    return "'" + str(s).replace("'", "''") + "'"


def filter_clause(f: dict | None) -> str:
    """Structured filters from the planner or the UI, all applied before ranking."""
    if not f:
        return "1=1"
    parts = []
    if f.get("projects"): parts.append("project IN (" + ",".join(map(_q, f["projects"])) + ")")
    if f.get("doc_types"): parts.append("doc_type IN (" + ",".join(map(_q, f["doc_types"])) + ")")
    if f.get("regions"): parts.append("source_region IN (" + ",".join(map(_q, f["regions"])) + ")")
    if f.get("sectors"): parts.append("source_sector IN (" + ",".join(map(_q, f["sectors"])) + ")")
    if f.get("orgs"): parts.append("(" + " OR ".join(f"lower(coalesce(source_org,'')) LIKE {_q('%' + o.lower() + '%')} OR lower(ctx_text) LIKE {_q('%' + o.lower() + '%')}" for o in f["orgs"]) + ")")
    if f.get("date_from"): parts.append(f"doc_date >= {_q(f['date_from'])}")
    if f.get("date_to"): parts.append(f"doc_date <= {_q(f['date_to'])}")
    if f.get("source_tokens"): parts.append("source_token IN (" + ",".join(map(_q, f["source_tokens"])) + ")")
    return " AND ".join(parts) or "1=1"


def retrieve(query: str, k: int | None = None, project: str | None = None, doc_type: str | None = None, mode: str = "hybrid", filters: dict | None = None) -> list[dict]:
    """Hybrid retrieval, filters before ranking. In production the Eventhouse answers (series_cosine_similarity over the Delta
    table); in development the app's in-memory DuckDB does the same with BM25 and a cosine scan. mode: bm25 | dense | hybrid
    (the single-index modes exist for the evals). Returns fused top-k with per-index ranks."""
    from .. import eventhouse
    f = dict(filters or {})
    if project: f["projects"] = [project]
    if doc_type: f["doc_types"] = [doc_type]
    n, kk = P["recall_k"], (k or P["rerank_k"])
    if eventhouse.configured():
        w = {"hybrid": (P["rrf_semantic_weight"], P["rrf_bm25_weight"]), "dense": (1.0, 0.0), "bm25": (0.0, 1.0)}[mode]   # zeroing a leg leaves the other leg's ranking: the eval's single-index rows run on the engine that serves the app
        rows = eventhouse.retrieve(list(embed_query(query)), query, f, kk, *w)
        for r in rows: r.setdefault("fused", 0.0)
        return rows
    c = store.cache(); where = " AND ".join(["1=1", filter_clause(f)])
    bm = c.con.cursor().execute(f"SELECT chunk_id, fts_main_chunks.match_bm25(chunk_id, ?) AS s FROM chunks WHERE {where} AND s IS NOT NULL ORDER BY s DESC LIMIT {n}", [query]).fetchall() if mode != "dense" else []
    dn = []
    if mode != "bm25":
        q = list(embed_query(query))
        dn = c.con.cursor().execute(f"SELECT chunk_id, array_cosine_similarity(embedding, ?::FLOAT[{len(q)}]) AS s FROM chunks WHERE {where} ORDER BY s DESC LIMIT {n}", [q]).fetchall()
    score, rank = {}, {}
    for w, res, key in [(P["rrf_semantic_weight"], dn, "dense_rank"), (P["rrf_bm25_weight"], bm, "bm25_rank")]:
        for i, (cid, _) in enumerate(res):
            score[cid] = score.get(cid, 0) + w / (60 + i); rank.setdefault(cid, {})[key] = i + 1
    top = sorted(score, key=score.get, reverse=True)[:kk]
    if not top:
        return []
    rows = c.df(f"SELECT chunk_id, doc_id, n, doc_type, project, doc_date, source_token, source_descriptor, source_org, source_region, source_sector, speakers, header, people, text FROM chunks WHERE chunk_id IN ({','.join('?' * len(top))})", top)
    rows["fused"] = rows.chunk_id.map(score); rows["bm25_rank"] = rows.chunk_id.map(lambda c: rank[c].get("bm25_rank")); rows["dense_rank"] = rows.chunk_id.map(lambda c: rank[c].get("dense_rank"))
    return rows.sort_values("fused", ascending=False).to_dict("records")
