"""Retrieval in production runs in the Fabric Eventhouse: an external Delta table over the same gold_chunks the pipeline
writes (no ingestion, one source of truth), queried with series_cosine_similarity, keyword term hits and reciprocal-rank
fusion, filters first. Same shape as the DuckDB path the app uses in development.
Env: EVENTHOUSE_URI (https://<cluster>.kusto.fabric.microsoft.com), EVENTHOUSE_DB."""
import os, time
from .config import TABLES
from .nodes.index import terms
from .store import http, token

URI, DB = os.environ.get("EVENTHOUSE_URI", "").rstrip("/"), os.environ.get("EVENTHOUSE_DB", "")
TABLE = "gold_chunks"


def configured() -> bool:
    return bool(URI and DB)


def _post(kind: str, csl: str) -> dict:
    last = None
    for attempt in range(4):   # the session retries 5xx; a dropped connection surfaces here and is retried too
        try:
            r = http.post(f"{URI}/v1/rest/{kind}", headers={"Authorization": f"Bearer {token('https://kusto.kusto.windows.net/.default')}", "Content-Type": "application/json"}, json={"db": DB, "csl": csl}, timeout=60)
            if not r.ok: raise RuntimeError(f"eventhouse {kind}: {r.status_code} {r.text[:400]}")
            return r.json()
        except (ConnectionError, OSError, RuntimeError) as e:
            last = e
            if isinstance(e, RuntimeError) and " 4" in str(e)[:22]: raise   # a bad query is not transient
            time.sleep(1.5 * (attempt + 1))
    raise last


def rows(csl: str) -> list[dict]:
    t = _post("query", csl)["Tables"][0]; cols = [c["ColumnName"] for c in t["Columns"]]
    return [dict(zip(cols, r)) for r in t["Rows"]]


def ensure_external_table():
    """Point the Eventhouse at the Delta table the pipeline just wrote; idempotent, part of the load step."""
    if not TABLES.startswith("abfss://"): return
    _post("mgmt", f".create-or-alter external table {TABLE} kind=delta (h@'{TABLES}/gold_chunks;impersonate')")


def _lit(v: str) -> str: return "'" + str(v).replace("'", "\\'") + "'"


def kql(vec: list[float], qterms: list[str], filters: dict | None, k: int, w_dense: float, w_bm25: float) -> str:
    f = filters or {}; where = ["1 == 1"]
    for key, col in [("projects", "['project']"), ("regions", "source_region"), ("sectors", "source_sector"), ("doc_types", "doc_type"), ("source_tokens", "source_token")]:
        if f.get(key): where.append(f"{col} in (" + ",".join(_lit(v) for v in f[key]) + ")")
    if f.get("orgs"): where.append("(" + " or ".join(f"tolower(coalesce(source_org, '')) contains {_lit(o.lower())} or tolower(ctx_text) contains {_lit(o.lower())}" for o in f["orgs"]) + ")")
    if f.get("date_from"): where.append(f"todatetime(doc_date) >= datetime({f['date_from'][:10]})")   # KQL orders datetimes, not strings
    if f.get("date_to"): where.append(f"todatetime(doc_date) <= datetime({f['date_to'][:10]})")
    kw = " + ".join(f"toint(tolower(ctx_text) has {_lit(t)})" for t in qterms) or "0"
    return f"""let q = dynamic([{",".join(f"{x:.7f}" for x in vec)}]);
external_table('{TABLE}')
| where {" and ".join(where)}
| extend dense = series_cosine_similarity(embedding, q), kw = {kw}
| order by dense desc | extend dense_rank = row_number()
| order by kw desc, {"dense desc" if w_dense else "chunk_id asc"} | extend bm25_rank = row_number()
| extend fused = {w_dense} / (60.0 + dense_rank) + {w_bm25} / (60.0 + bm25_rank)
| top {k} by fused desc
| project chunk_id, doc_id, n, doc_type, ['project'], doc_date, source_token, source_descriptor, source_org, source_region, source_sector, speakers, header, people, text, fused, bm25_rank, dense_rank"""


def retrieve(vec: list[float], question: str, filters: dict | None, k: int, w_dense: float, w_bm25: float) -> list[dict]:
    return rows(kql(vec, terms(question), filters, k, w_dense, w_bm25))
