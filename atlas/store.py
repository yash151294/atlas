"""One door to storage. Working files (parquet, csv, json, logs) under the data root; serving tables as Delta under TABLES;
an in-memory DuckDB cache of the serving tables for the app. The same calls work on a laptop folder, on the lakehouse mount
inside a Fabric notebook, and on OneLake from anywhere. On OneLake there is one identity path: an azure-identity token
(the signed-in CLI on a laptop, the managed identity in Azure) for the ADLS DFS API (files) and delta-rs (tables); DuckDB
is only the in-memory query engine, never a storage client.

Write guarantees: every working file is written whole to a sibling `.tmp` path and renamed into place (a reader sees the
old file or the new one, never a partial); Delta tables commit atomically (delta-rs / Spark); logs are append-only, one
record per line. Nothing spans two files transactionally: downstream tables are re-derived from upstream ones by the pipeline."""
import io, json, os, threading, time
from pathlib import Path
import duckdb, numpy as np, pandas as pd, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .config import CATALOG, DATA, MOUNT, ONELAKE, TABLES, path

class _Retry(Retry):
    """urllib3 sleeps for the whole of a Retry-After header, uncapped: a 429 from Azure OpenAI with a long value froze a
    thread for 110 minutes once. Thirty seconds is the most any caller (the app, the pipeline) should ever wait; past that
    the call fails and the caller degrades (a document scored without the second model, a briefing with an error), visibly."""
    def get_retry_after(self, response):
        ra = super().get_retry_after(response)
        return min(ra, 30) if ra else ra


# one session with retries for every OneLake, Eventhouse and Azure OpenAI call: transient TLS drops and 429/5xx are retried with backoff
http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=_Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=frozenset(["GET", "HEAD", "PUT", "PATCH", "POST", "DELETE"]))))

# serving name -> Delta table under TABLES; the app's SQL uses the short names
SERVING = {"sources": "gold_sources", "edges": "gold_edges", "people": "gold_people", "contributors": "gold_contributors",
           "entities": "silver_entities", "redacted": "silver_redacted", "chunks": "gold_chunks", "net_index": "gold_net_index"}
_lock = threading.Lock(); _tok = {}
_memo: dict[str, bytes | None] = {}   # OneLake working files, fetched once per process until written here or a full cache refresh (a run finished elsewhere)


def now() -> str:
    """One timestamp format for every log record."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """NaN and numpy scalars become None and Python objects: what JSON, DuckDB and the nodes expect from a parquet round trip."""
    return df.astype(object).where(pd.notnull(df), None)


# ---------- OneLake (ADLS Gen2 DFS API) ----------
def token(scope: str = "https://storage.azure.com/.default") -> str:
    """Entra token from the environment: managed identity in Azure, the signed-in CLI on a laptop."""
    t = _tok.get(scope)
    if not t or t.expires_on - time.time() < 300:
        from azure.identity import DefaultAzureCredential
        t = DefaultAzureCredential(exclude_interactive_browser_credential=True).get_token(scope); _tok[scope] = t
    return t.token


def _split(uri: str) -> tuple[str, str, str]:   # abfss://ws@host/lh/Files/x -> (host, ws, lh/Files/x)
    ws, rest = uri[len("abfss://"):].split("@", 1); host, p = rest.split("/", 1)
    return host, ws, p


def _dfs(uri: str) -> str:   # abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Files/x -> https://onelake.dfs.fabric.microsoft.com/ws/lh/Files/x
    host, ws, p = _split(uri)
    return f"https://{host}/{ws}/{p}"


def _h(): return {"Authorization": f"Bearer {token()}"}


def _upload(uri: str, data: bytes):
    u = _dfs(uri)
    http.put(u + "?resource=file", headers=_h()).raise_for_status()
    if data:   # the DFS append call rejects an empty body; an empty file is just the create
        http.patch(u + "?action=append&position=0", headers={**_h(), "Content-Length": str(len(data))}, data=data).raise_for_status()
        http.patch(u + f"?action=flush&position={len(data)}", headers=_h()).raise_for_status()


def _put(uri: str, data: bytes):
    """Whole-file write: upload to <path>.tmp, then rename over the destination (one DFS call), so a concurrent reader never sees a partial file."""
    tmp = uri + ".tmp"; _upload(tmp, data)
    host, ws, p = _split(tmp)
    r = http.put(_dfs(uri), headers={**_h(), "x-ms-rename-source": f"/{ws}/{p}"})
    if not r.ok:   # rename unsupported on this path: fall back to the direct write, then drop the temp
        _upload(uri, data); http.delete(_dfs(tmp), headers=_h())


def _get(uri: str) -> bytes | None:
    r = http.get(_dfs(uri), headers=_h())
    return r.content if r.ok else None


_append_lock = threading.Lock()


def _append(uri: str, data: bytes, attempts: int = 2):
    """Append to a OneLake file at its current length. One lock per process: the pipeline's worker threads and the app's request
    threads share a writer partition, and two appends racing on the same position fail with a 400 on flush. A conflict that
    still slips through (another writer on the same file) is retried once from a fresh length."""
    with _append_lock:
        for attempt in range(attempts):
            u = _dfs(uri); r = http.head(u, headers=_h())
            if not r.ok:
                return _upload(uri, data)
            pos = int(r.headers.get("Content-Length", "0"))
            a = http.patch(u + f"?action=append&position={pos}", headers={**_h(), "Content-Length": str(len(data))}, data=data)
            f = http.patch(u + f"?action=flush&position={pos + len(data)}", headers=_h()) if a.ok else a
            if f.ok: return
            if attempt == attempts - 1: f.raise_for_status()


def _delta_opts() -> dict | None:
    return {"bearer_token": token(), "use_fabric_endpoint": "true"} if ONELAKE else None


def read_table_arrow(serving_name: str):
    """A Delta serving table as an Arrow table (delta-rs; a laptop folder, the mount, or OneLake with the identity token)."""
    from deltalake import DeltaTable
    return DeltaTable(table_uri(serving_name), storage_options=_delta_opts()).to_pyarrow_table()


# ---------- working files (catalog datasets) ----------
def _write_local(p: Path, write):
    """Atomic local write: to a sibling temp file, then an os.replace (atomic on POSIX and NTFS)."""
    p.parent.mkdir(parents=True, exist_ok=True); tmp = p.with_name(p.name + ".tmp")
    try:
        write(tmp); os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)   # a failed write leaves nothing behind


def exists(name: str) -> bool:
    p = path(name)
    return http.head(_dfs(p), headers=_h()).ok if ONELAKE else Path(p).exists()


def read(name: str):
    """A catalog dataset as a DataFrame (parquet, csv) or object (json); None if it does not exist yet."""
    p, fmt = path(name), CATALOG[name].get("format", "parquet")
    if fmt == "dir": return p
    if fmt == "json": return json.loads(read_text(CATALOG[name]["path"])) if exists(name) else None   # never memoised: the run manifest is polled for change
    if ONELAKE:
        if name not in _memo: _memo[name] = _get(p) if exists(name) else None   # ponytail: whole-file memo; per-document reads when the corpus outgrows memory
        if _memo[name] is None: return None
        buf = io.BytesIO(_memo[name])
        return pd.read_csv(buf) if fmt == "csv" else pd.read_parquet(buf)
    if not exists(name): return None
    return pd.read_csv(p) if fmt == "csv" else pd.read_parquet(p)


def write(name: str, df):
    p, fmt = path(name), CATALOG[name].get("format", "parquet"); _memo.pop(name, None)
    if fmt == "json": return write_text(CATALOG[name]["path"], json.dumps(df, indent=1, default=str))
    if ONELAKE:
        buf = io.BytesIO(); df.to_parquet(buf); return _put(p, buf.getvalue())
    _write_local(Path(p), df.to_parquet)


def read_text(rel: str) -> str:
    """A text file under the data root, '' if missing."""
    if ONELAKE:
        b = _get(f"{DATA}/{rel}"); return b.decode() if b else ""
    p = Path(DATA) / rel; return p.read_text() if p.exists() else ""


def write_text(rel: str, text: str):
    if ONELAKE: return _put(f"{DATA}/{rel}", text.encode())
    _write_local(Path(DATA) / rel, lambda t: t.write_text(text))


def _writer() -> str:
    import socket
    return f"{socket.gethostname().split('.')[0]}-{os.getpid()}"


def append_line(rel: str, record: dict):
    """Append-only logs (audit, vault access, retention, model calls, sign-ins): one JSON object per line. On OneLake every
    writer appends to its own file under <log>/ (a file created through the lakehouse mount cannot be appended through the
    DFS API, and two processes must never append to one file); readers merge the folder."""
    line = json.dumps(record, default=str) + "\n"
    if ONELAKE: return _append(f"{DATA}/{rel[:-len('.jsonl')] if rel.endswith('.jsonl') else rel}/{_writer()}.jsonl", line.encode())
    p = Path(DATA) / rel; p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f: f.write(line)


def _list(uri: str) -> list[str]:
    """Files directly under a OneLake folder (DFS list); [] if the folder does not exist."""
    host, ws, p = _split(uri)
    r = http.get(f"https://{host}/{ws}?resource=filesystem&directory={p}&recursive=false", headers=_h())
    return [f"abfss://{ws}@{host}/{x['name']}" for x in r.json().get("paths", []) if not x.get("isDirectory")] if r.ok else []


def read_lines(rel: str) -> list[dict]:
    """Every record of a log, oldest first: the single file (development, the mount, seeded history) plus, on OneLake, every writer's partition."""
    texts = [read_text(rel)]
    if ONELAKE:
        folder = f"{DATA}/{rel[:-len('.jsonl')] if rel.endswith('.jsonl') else rel}"
        texts += [(_get(u) or b"").decode() for u in sorted(_list(folder))]
    rows = [json.loads(l) for t in texts for l in t.splitlines() if l.strip()]
    return sorted(rows, key=lambda r: str(r.get("ts", "")))


def delete_file(location: str) -> bool:
    """Remove a raw file by the location ingest recorded; True if it was there."""
    if location.startswith("abfss://"):
        return http.delete(_dfs(location), headers=_h()).ok
    p = Path(location)
    if p.exists(): p.unlink(); return True
    return False


# ---------- serving tables (Delta) ----------
def table_uri(serving_name: str) -> str:
    return f"{TABLES}/{SERVING.get(serving_name, serving_name)}"


def write_table(serving_name: str, df: pd.DataFrame):
    """A Delta serving table: one atomic commit, readers see the previous version until it lands. Inside a Fabric notebook
    Spark writes it (the runtime's own Delta writer, no extra libraries); elsewhere delta-rs writes it to a folder or to OneLake."""
    uri = table_uri(serving_name); name = SERVING.get(serving_name, serving_name)
    df = clean(df) if len(df) else df
    if MOUNT:
        from pyspark.sql import SparkSession
        tmp = Path(f"{DATA}/tmp/{name}.parquet"); tmp.parent.mkdir(parents=True, exist_ok=True); df.to_parquet(tmp)
        SparkSession.getActiveSession().read.parquet(f"Files/tmp/{name}.parquet").write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(name)
        tmp.unlink(missing_ok=True)   # the staging parquet is not a copy anyone reads
        return
    from deltalake import write_deltalake
    if not ONELAKE: Path(uri).parent.mkdir(parents=True, exist_ok=True)
    write_deltalake(uri, df, mode="overwrite", schema_mode="overwrite", storage_options=_delta_opts())


def _eventhouse_serves_retrieval() -> bool:
    """True when the Eventhouse answers index.retrieve, so the cache's keyword index over the passages would never be queried.
    Imported here, not at module scope: eventhouse imports store."""
    from . import eventhouse
    return eventhouse.configured()


class Cache:
    """The app's serving copy: every Delta table loaded into one in-memory DuckDB (with an FTS index on the passages), refreshed
    from storage at startup and after a write. Queries run in milliseconds; storage is read once per refresh, not per request."""
    def __init__(self):
        self.con = duckdb.connect(); self.con.execute("INSTALL fts; LOAD fts;")
        import tempfile; self.con.execute(f"SET temp_directory = '{tempfile.gettempdir()}/duckdb-{os.getpid()}'")   # spills go to /tmp, never the (read-only) working directory
        self.loaded = set(); self.lock = threading.Lock(); self._net = None

    def refresh(self, *names: str):
        """Reload serving tables from their Delta files (delta-rs, one identity path) into the in-memory engine."""
        with self.lock:
            if not names: _memo.clear()   # a full refresh follows a run in another process, which rewrote the working files too
            for n in names or SERVING:
                uri = table_uri(n)
                if not ONELAKE and not Path(uri).exists(): continue
                arrow = read_table_arrow(n)
                if arrow.num_rows == 0: continue
                self.con.register("_incoming", arrow)
                if n in ("chunks", "net_index"):
                    dim = self.con.execute("SELECT len(embedding) FROM _incoming LIMIT 1").fetchone()[0]
                    self.con.execute(f"CREATE OR REPLACE TABLE {n} AS SELECT * EXCLUDE (embedding), embedding::FLOAT[{dim}] AS embedding FROM _incoming")
                    if n == "chunks" and not _eventhouse_serves_retrieval():
                        self.con.execute("PRAGMA create_fts_index('chunks', 'chunk_id', 'ctx_text', overwrite=1)")   # 30 MB the Eventhouse makes dead: only index.retrieve's development path reads it
                    if n == "net_index": self._net = None
                else:
                    self.con.execute(f"CREATE OR REPLACE TABLE {n} AS SELECT * FROM _incoming")
                self.con.unregister("_incoming")
                self.loaded.add(n)

    def df(self, sql: str, params=()) -> pd.DataFrame:
        return clean(self.con.cursor().execute(sql, params).df())

    def q(self, sql: str, params=()) -> list[dict]:
        return self.df(sql, params).to_dict("records")

    def has(self, name: str) -> bool:
        return name in self.loaded

    def net_index(self) -> tuple[pd.DataFrame, np.ndarray]:
        """The network's descriptor lines and their unit-normalised embedding matrix, built once per refresh (a search is one matrix product)."""
        if self._net is None:
            idx = self.df("SELECT entity_id, kind, text, embedding FROM net_index")
            M = np.vstack([np.asarray(v, dtype="float32") for v in idx.embedding]) if len(idx) else np.zeros((0, 1), dtype="float32")
            M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
            self._net = (idx.drop(columns=["embedding"]), M)
        return self._net


_cache = None


def cache() -> Cache:
    """The process's serving cache. A refresh that failed (storage or identity not ready yet) is retried on the next call, never memoised."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = Cache()
    if not _cache.has("chunks"):
        _cache.refresh()
    return _cache
