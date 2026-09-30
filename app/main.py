"""Three analyst surfaces (Network, Research, Review) and one governance page (Compliance) over the gold tables.
Thin on purpose: the pipeline owns the logic; this reads parquet/DuckDB and calls the nodes.
Identity: Microsoft Entra sign-in when ENTRA_* is configured (production), one named analyst otherwise (development); engagement scope is OneLake security."""
import collections, json, logging, os, secrets, threading, time
from concurrent.futures import Future, ThreadPoolExecutor
import pandas as pd
from fastapi import FastAPI, Body, Depends, Request
from urllib.parse import quote
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from itsdangerous import BadSignature, URLSafeTimedSerializer
from atlas.config import REPO, PARAMS, ONELAKE
from atlas import llm, pipeline, retention, store
from atlas.nodes import answer, redact, index, network, rerank
from atlas.nodes.detect import TYPES

app = FastAPI(title="Atlas")
app.add_middleware(GZipMiddleware, minimum_size=1000)   # Container Apps ingress does not compress; a document or a briefing is text that shrinks several times

# ---------- identity: Microsoft Entra sign-in (auth-code flow), a signed session cookie, the user's UPN on every logged action ----------
ENTRA = {k: os.environ.get(f"ENTRA_{k}") for k in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET")}
AUTH = all(ENTRA.values())                                   # unset in development: one named analyst, no login
REQUIRE_AUTH = ONELAKE or os.environ.get("ATLAS_REQUIRE_AUTH") == "1"   # production fails closed: no sign-in configured means nothing is served
APP_URL = os.environ.get("APP_URL", "http://localhost:8765").rstrip("/")
SESSION_HOURS = 8
signer = URLSafeTimedSerializer(os.environ.get("SESSION_SECRET", "dev"), salt="atlas-session")   # timed: a captured cookie dies with the session
DEV_USER = dict(name="Analyst (development)", upn="analyst@atlas.local")
MUTATE = threading.Lock()   # ponytail: one lock serialises every read-modify-write of the working tables (one replica); per-document locks if throughput matters
PHOTOS: dict[str, tuple[str, bytes]] = {}   # ponytail: profile photos fetched at sign-in live in process memory; a restart shows initials until the next sign-in


def _msal():
    import msal
    return msal.ConfidentialClientApplication(ENTRA["CLIENT_ID"], authority=f"https://login.microsoftonline.com/{ENTRA['TENANT_ID']}", client_credential=ENTRA["CLIENT_SECRET"])


def _photo(access_token: str | None) -> tuple[str, bytes] | None:
    """The signed-in user's 48px profile photo from Microsoft Graph (User.Read covers it); None when they have none or Graph is slow."""
    if not access_token: return None
    try:
        import requests
        r = requests.get("https://graph.microsoft.com/v1.0/me/photos/48x48/$value", headers={"Authorization": f"Bearer {access_token}"}, timeout=5)
        return (r.headers.get("content-type", "image/jpeg"), r.content) if r.ok else None
    except Exception as e:
        logging.getLogger(__name__).info("profile photo skipped: %s", e); return None


def session(request: Request) -> dict | None:
    if not AUTH: return DEV_USER
    try:
        return signer.loads(request.cookies.get("atlas_s", ""), max_age=SESSION_HOURS * 3600)
    except BadSignature:   # tampered, or expired (SignatureExpired is a BadSignature)
        return None


def who(request: Request) -> str:
    """The signed-in user's UPN: what every approval, decision, reveal and erasure is logged under."""
    return (session(request) or {}).get("upn") or "unknown"


@app.get("/healthz")
def healthz():
    """Liveness and readiness: 200 once the serving cache holds the passages, 503 before (a readiness probe target)."""
    ready = store._cache is not None and store._cache.has("chunks")
    return JSONResponse(dict(ok=True, ready=ready, auth=AUTH), status_code=200 if ready else 503)


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    p = request.url.path
    if REQUIRE_AUTH and not AUTH and not p.startswith("/healthz"):
        return JSONResponse({"error": "sign-in is not configured for this deployment"}, status_code=503)
    if AUTH and not p.startswith(("/auth/", "/static/", "/healthz", "/welcome")) and session(request) is None:
        if p.startswith("/api/"): return JSONResponse({"error": "sign in required"}, status_code=401)
        return RedirectResponse("/welcome?next=" + quote(p + ("?" + request.url.query if request.url.query else ""), safe=""))   # the landing page, then back to the page they asked for
    return await call_next(request)


def _safe_next(n: str) -> str:
    """Where to go after sign-in: a path on this app, never another host (an open redirect is a phishing hop)."""
    return n if n.startswith("/") and not n.startswith("//") and not n.startswith("/\\") else "/"


@app.get("/welcome")
def welcome(request: Request, next: str = "/"):
    """The landing page: the Atlas lockup, one line on what the app is, Sign in with Microsoft; a signed-in user goes straight through to where they were going."""
    if AUTH and session(request) is not None: return RedirectResponse(_safe_next(next))
    return page("landing")


@app.get("/auth/login")
def auth_login(next: str = "/"):
    """Start the Entra sign-in: redirect to Microsoft with a state cookie and remember where to return; straight home when sign-in is not configured."""
    if not AUTH: return RedirectResponse(_safe_next(next))
    state = secrets.token_urlsafe(16)
    url = _msal().get_authorization_request_url(["User.Read"], state=state, redirect_uri=f"{APP_URL}/auth/callback")
    resp = RedirectResponse(url); secure = APP_URL.startswith("https")
    resp.set_cookie("atlas_state", state, httponly=True, samesite="lax", secure=secure, max_age=600); resp.set_cookie("atlas_next", _safe_next(next), httponly=True, samesite="lax", secure=secure, max_age=600); return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    """Finish the sign-in: check the state, exchange the code, set the signed session cookie, log the sign-in event."""
    if error: return JSONResponse({"error": error, "description": error_description}, status_code=400)
    if state != request.cookies.get("atlas_state"): return JSONResponse({"error": "state mismatch"}, status_code=400)
    r = _msal().acquire_token_by_authorization_code(code, scopes=["User.Read"], redirect_uri=f"{APP_URL}/auth/callback")
    if "id_token_claims" not in r: return JSONResponse({"error": r.get("error"), "description": r.get("error_description")}, status_code=400)
    c = r["id_token_claims"]; user = dict(name=c.get("name"), upn=c.get("preferred_username") or c.get("upn") or c.get("oid"))
    if photo := _photo(r.get("access_token")): PHOTOS[user["upn"]] = photo
    resp = RedirectResponse(_safe_next(request.cookies.get("atlas_next", "/"))); resp.set_cookie("atlas_s", signer.dumps(user), httponly=True, samesite="lax", secure=APP_URL.startswith("https"), max_age=SESSION_HOURS * 3600); resp.delete_cookie("atlas_state"); resp.delete_cookie("atlas_next")
    store.append_line("access_log.jsonl", dict(ts=store.now(), event="sign_in", **user))
    return resp


@app.get("/auth/logout")
def auth_logout():
    """Clear the session cookie and sign out of Microsoft."""
    resp = RedirectResponse(f"https://login.microsoftonline.com/{ENTRA['TENANT_ID']}/oauth2/v2.0/logout?post_logout_redirect_uri={APP_URL}/" if AUTH else "/")
    resp.delete_cookie("atlas_s"); return resp


@app.get("/api/me")
def api_me(request: Request):
    """The signed-in user (name, UPN), whether sign-in is configured and whether a profile photo is held; the header's account menu shows it."""
    s = session(request) or {}
    return dict(s, auth=AUTH, photo=s.get("upn") in PHOTOS)


@app.get("/api/me/photo")
def api_me_photo(request: Request):
    """The signed-in user's profile photo as fetched from Microsoft Graph at sign-in; 404 when none is held."""
    p = PHOTOS.get((session(request) or {}).get("upn"))
    return Response(p[1], media_type=p[0]) if p else JSONResponse({"error": "no photo"}, status_code=404)


@app.on_event("startup")
def warm_models():
    """Load the serving cache from storage and the embedding and reranker models once, so the first request is not the slow one;
    then watch the run manifest so a pipeline run that finished elsewhere (the scheduled notebook) reaches the cache within minutes."""
    try:
        store.cache(); index.embedder(); rerank.reranker()
    except Exception as e:
        logging.getLogger(__name__).warning("warm-up skipped: %s", e)
    threading.Thread(target=_follow_pipeline_runs, daemon=True).start()


def _follow_pipeline_runs(every: int = 300):
    seen = (store.read("run_manifest") or {}).get("finished")
    while True:
        time.sleep(every)
        try:
            now = (store.read("run_manifest") or {}).get("finished")
            if now != seen:
                seen = now; store.cache().refresh(); logging.getLogger(__name__).info("serving cache refreshed after the pipeline run finished at %s", now)
        except Exception as e:
            logging.getLogger(__name__).warning("manifest check skipped: %s", e)


STATIC = REPO / "app/static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"): resp.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static"): resp.headers["Cache-Control"] = "no-cache"   # revalidate by ETag: a 304 when unchanged, never stale after a deploy
    return resp


def page(n, request: Request | None = None):
    """A static page; with a request, the signed-in user and the review queue are inlined as window.ATLAS so the header is complete at first paint without two round trips."""
    html = (STATIC / f"{n}.html").read_text()
    if request is not None:
        s = session(request) or {}
        try: queue = q("SELECT count(*) AS n FROM redacted WHERE tier='review' AND NOT approved")[0]["n"] if store.cache().has("redacted") else None
        except Exception: queue = None
        state = json.dumps(dict(me=dict(s, auth=AUTH, photo=s.get("upn") in PHOTOS), queue=queue)).replace("</", "<\\/")
        html = html.replace("<!doctype html>", f"<!doctype html><script>window.ATLAS={state}</script>", 1)
    return HTMLResponse(html)


def _page_route(n):
    def handler(request: Request): return page(n, request)
    handler.__name__ = f"page_{n}"; return handler


def records(df): return store.clean(df).to_dict("records")


def q(sql, params=()):
    return store.cache().q(sql, params)


def _log(rel, **rec):
    store.append_line(rel, dict(ts=store.now(), **rec))


for name in ["network", "research", "documents", "compliance"]:
    app.get(f"/{name}")(_page_route(name))
app.get("/")(_page_route("index"))


@app.get("/api/home")
def home():
    """Home page figures: counts, the gate (queue, risk histogram, special-category flags), coverage by region and sector, the pace of interviews, contributors by reach, and when the serving tables were last refreshed."""
    c = store.cache(); refreshed = (store.read("run_manifest") or {}).get("finished")
    if not all(c.has(t) for t in ("sources", "contributors", "edges", "redacted")): return dict(refreshed=refreshed, counts={}, queue={})
    S, C, E, R = (c.df(f"SELECT * FROM {t}") for t in ("sources", "contributors", "edges", "redacted"))
    waiting = int(((R.tier == "review") & ~R.approved.astype(bool)).sum())
    bins = R.risk_score.fillna(0).mul(20).round().div(20).value_counts().sort_index()   # the gate's scores step by 0.05, so a dot histogram at that step is exact
    gate = dict(threshold=PARAMS["qa"]["auto_approve_below"], bins=[dict(x=float(x), n=int(n)) for x, n in bins.items()], auto=int((R.approval_mode == "auto").sum()), waiting=waiting,
                special=collections.Counter(k for x in R.special_category.fillna("[]") for k in json.loads(x)).most_common())
    counts = dict(sources=len(S), contributors=len(C), people=q("SELECT count(*) AS n FROM people")[0]["n"] if c.has("people") else 0, documents=len(R), waiting=waiting)
    return dict(refreshed=refreshed, counts=counts, queue=dict(review=waiting), gate=gate, **network.overview(S, C, E))


# ---------- Network ----------
@app.get("/api/network/facets")
def network_facets():
    """Controlled vocabularies (regions, sectors, projects) for the Network filters."""
    return network.facets()


@app.get("/api/network/search")
def network_search(q: str = "", region: str = "", sector: str = "", project: str = "", since: str = "", k: int = 10):
    """Who can reach whom: a contributor's profile when the query is a contributor's name, else contributors ranked by reach."""
    return network.search(q, region=region or None, sector=sector or None, project=project or None, since=since or None, k=k)


@app.get("/api/network/contributor")
def network_contributor(id: str):
    """One contributor's profile: the sources reached, projects, recency."""
    return network.profile(id)


@app.get("/api/network/source")
def network_source(id: str):
    """One source's card: descriptor and the interviews that reached them."""
    return network.source_card(id)


@app.get("/api/network/graph")
def network_graph(ids: str = "", since: str = "", until: str = "", topics: bool = False):
    """Ego graph (nodes, edges) around the given ids within a date window, optionally with topics."""
    keep = [i for i in ids.split(",") if i]
    return network.ego(keep, since=since or None, until=until or None, topics=topics) if keep else dict(nodes=[], edges=[])


# ---------- Documents ----------
def _load_red():
    return store.read("silver_redacted")


def _save_red(red):
    """The working copy (parquet, what the pipeline re-reads) and the serving copy (Delta, what the app and Fabric read)."""
    store.write("silver_redacted", red); store.write_table("redacted", red); store.cache().refresh("redacted")


@app.get("/api/docs")
def docs_list():
    """The Review queue: every retained document with its gate result; unapproved review-tier documents first, then by risk."""
    return q("SELECT doc_id, filename, doc_type, project, doc_date, source_token, source_descriptor, source_region, source_sector, n_redactions, n_review, approved, approved_by, approved_at, "
             "approval_mode, raw_delete_after, risk_score, risk_reasons, residual_hits, special_category, tier FROM redacted ORDER BY (tier='review' AND NOT approved) DESC, risk_score DESC, doc_date DESC")


@app.get("/api/docs/doc")
def docs_doc(doc_id: str):
    """One document for review: raw text, retained copy, spans, vault rows without values, identity questions, legend, residual hits, risk reasons."""
    raw = store.read("bronze_documents").query("doc_id == @doc_id").iloc[0]
    red = q("SELECT * FROM redacted WHERE doc_id=?", [doc_id])[0]
    mentions = store.read("silver_mentions"); mentions = mentions[mentions.doc_id == doc_id].sort_values("start")
    if "review_decision" not in mentions.columns: mentions["review_decision"] = None
    vault = store.read("silver_vault"); vault = vault[vault.doc_id == doc_id].sort_values("start")
    queue = store.read("silver_review_queue")
    queue = queue[queue.doc_id == doc_id] if queue is not None and len(queue) else pd.DataFrame()
    mentions.loc[mentions.type == "ORG", "needs_review"] = False
    hits = json.loads(red.get("residual_hits") or "[]")
    return dict(raw_text=raw.text, redacted=red, spans=records(mentions), vault=records(vault.drop(columns=["value"])), queue=records(queue) if len(queue) else [],
                people=json.loads(red.get("people") or "[]"), legend=redact.legend(red.get("people") or "[]"), residual_hits=hits, risk_reasons=json.loads(red.get("risk_reasons") or "[]"))


@app.post("/api/docs/approve")
def docs_approve(doc_id: str, who: str = Depends(who)):
    """A person approves a retained copy; logged under their name; starts the raw-deletion clock."""
    with MUTATE:
        red = redact.approve(_load_red(), doc_id, who, mode="human"); _save_red(red)
    _log("audit_log.jsonl", doc_id=doc_id, decision="approve", who=who)
    return q("SELECT doc_id, approved, approved_by, approval_mode, raw_delete_after FROM redacted WHERE doc_id=?", [doc_id])[0]


def _recount(red, mentions, doc_id):
    m = mentions[(mentions.doc_id == doc_id) & (mentions.type != "ORG")]
    red.loc[red.doc_id == doc_id, "n_review"] = int((m.needs_review & m.review_decision.isna()).sum())
    return red


@app.post("/api/docs/span")
def docs_span(doc_id: str, start: int, decision: str, who: str = Depends(who)):
    """confirm: keep the redaction. restore: it was not personal data, put the text back (vault row removed, chunks rebuilt)."""
    with MUTATE:
        mentions = store.read("silver_mentions")
        if "review_decision" not in mentions.columns: mentions["review_decision"] = None
        sel = (mentions.doc_id == doc_id) & (mentions.start == start)
        mentions.loc[sel, "review_decision"] = decision
        red = _load_red(); reindex = False
        if decision == "restore":
            vault = store.read("silver_vault")
            row = vault[(vault.doc_id == doc_id) & (vault.start == start)]
            if len(row):
                tok, val = row.token.iloc[0], row.value.iloc[0]
                i = red.index[red.doc_id == doc_id][0]
                red.loc[i, "redacted_text"] = red.loc[i, "redacted_text"].replace(tok, val)
                red.loc[i, "n_redactions"] = int(red.loc[i, "n_redactions"]) - int(len(row))
                vault = vault[~((vault.doc_id == doc_id) & (vault.token == tok))]; store.write("silver_vault", vault); reindex = True
        red = _recount(red, mentions, doc_id); store.write("silver_mentions", mentions); _save_red(red)
        if reindex: pipeline.reindex_document(doc_id)
    _log("audit_log.jsonl", doc_id=doc_id, start=start, decision=decision, who=who)
    return dict(ok=True, reindexed=reindex)


@app.post("/api/docs/redact_text")
def docs_redact_text(doc_id: str, text: str, type: str = "PERSON", who: str = Depends(who)):
    """A reviewer found something the detectors missed: whole-word occurrences become a token (an existing one if the document already holds this person), organisation names are left alone, chunks are rebuilt."""
    if type not in TYPES.__args__: return JSONResponse({"error": f"type must be one of {', '.join(TYPES.__args__)}"}, status_code=400)
    with MUTATE:
        red, vault, tok, count = redact.manual_redact(_load_red(), store.read("silver_vault"), store.read("silver_entities"), doc_id, text, type)
        if not tok: return dict(ok=False, error="not found in the retained copy as a whole word outside an organisation name")
        store.write("silver_vault", vault); _save_red(red); pipeline.reindex_document(doc_id)
    _log("audit_log.jsonl", doc_id=doc_id, decision="manual_redaction", type=type, token=tok, count=count, who=who)
    return dict(ok=True, token=tok, count=count)


@app.post("/api/docs/unredact")
def docs_unredact(doc_id: str, token: str, who: str = Depends(who)):
    """Reverse a manual redaction (the text returns, the vault row goes), then rebuild the document's chunks."""
    with MUTATE:
        red, vault, value, count = redact.undo_manual(_load_red(), store.read("silver_vault"), doc_id, token)
        if value is None: return dict(ok=False, error="only a token created by manual redaction can be undone")
        store.write("silver_vault", vault); _save_red(red); pipeline.reindex_document(doc_id)
    _log("audit_log.jsonl", doc_id=doc_id, decision="undo_manual_redaction", token=token, count=count, who=who)
    return dict(ok=True, count=count)


@app.post("/api/docs/queue")
def docs_queue(doc_id: str, mention: str, decision: str, who: str = Depends(who)):
    """Identity decision: confirmed -> the provisional link stands; rejected -> the mention becomes its own private token. Re-runs the document's downstream steps."""
    with MUTATE:
        queue = store.read("silver_review_queue")
        queue.loc[(queue.doc_id == doc_id) & (queue.mention == mention), "status"] = decision
        store.write("silver_review_queue", queue)
        if decision == "rejected":
            m = store.read("silver_mentions")
            sel = (m.doc_id == doc_id) & (m.text == mention) & (m.decision.isin(("provisional", "exact")))   # an exact name at a disagreeing organisation is queued too (ADR 45)
            m.loc[sel, ["entity_id", "decision", "role"]] = [f"PRIV:{doc_id}:{mention.lower()}", "private", "private"]
            store.write("silver_mentions", m)
            for step in ["redact", "qa", "graph"]: pipeline.run(only=step)
            pipeline.reindex_document(doc_id)
        else:
            pipeline.run(only="qa"); pipeline.run(only="load")
    _log("audit_log.jsonl", doc_id=doc_id, mention=mention, decision=decision, who=who)
    return dict(ok=True)


# ---------- Vault ----------
@app.post("/api/vault/reveal")
def vault_reveal(token: str, doc_id: str, reason: str, who: str = Depends(who)):
    """Reveal one token's value for a reason; every reveal is logged with who, token, document and reason."""
    vault = store.read("silver_vault"); row = vault[(vault.token == token) & (vault.doc_id == doc_id)]
    if not len(row): return dict(error="no such token")
    rec = dict(ts=store.now(), who=who, token=token, doc_id=doc_id, reason=reason, type=row.type.iloc[0])
    store.append_line("vault_access.jsonl", rec)
    return dict(token=token, value=row.value.iloc[0], type=row.type.iloc[0], role=row.role.iloc[0] if "role" in row else None, expires=row.expires.iloc[0], logged=rec)


# ---------- Research ----------
ASKS: dict[str, Future] = {}   # the page's job id -> the briefing it started; leaving the desk does not stop it and coming back rejoins it
ASKPOOL = ThreadPoolExecutor(8, thread_name_prefix="ask")   # the work outlives the request that started it: a navigation drops the response, never the model calls already paid for
ASKLOCK = threading.Lock()   # held only around the lookup, never while a briefing is being written


def _ask_job(job: str, run):
    """Start this briefing, or join the one this job id already has: a question is asked once however often the page reconnects."""
    with ASKLOCK:
        f = ASKS.get(job) if job else None
        if f is None:
            f = ASKPOOL.submit(run)
            if job:
                ASKS[job] = f
                for k in list(ASKS)[:-8]: ASKS.pop(k)   # ponytail: the last eight briefings are rejoinable, in this process only (one replica, as MUTATE assumes); a shared store if that changes
    return f.result()


@app.post("/api/research/ask")
def research_ask(body: dict = Body(...)):
    """The thread so far comes with the question (the page keeps it); the only state the server holds is the briefing in flight, under the job id the page made, so leaving the desk mid-question rejoins it rather than asking again."""
    hist = [dict(question=str(h.get("question", ""))[:2000], headline=str(h.get("headline", ""))[:2000]) for h in (body.get("history") or [])[-PARAMS["answer"]["history_turns"]:]]
    return _ask_job(str(body.get("job") or "")[:64],
                    lambda: answer.ask(body["question"], project=body.get("project") or None, doc_type=body.get("doc_type") or None, history=hist))


@app.get("/api/provider")
def api_provider():
    """Which provider serves each purpose (Claude first-party, Foundry, Azure OpenAI)."""
    return llm.provider()


@app.get("/api/chunk")
def api_chunk(chunk_id: str):
    """One passage by id, without its embedding, for evidence display."""
    return q("SELECT * EXCLUDE (embedding) FROM chunks WHERE chunk_id=?", [chunk_id])[0]


@app.get("/api/projects")
def api_projects():
    """Engagement codes present in the retained corpus."""
    return [r["project"] for r in q("SELECT DISTINCT project FROM redacted WHERE project IS NOT NULL ORDER BY 1")]


# ---------- Compliance ----------
@app.get("/api/compliance/retention")
def compliance_retention():
    """What retention is due: raw files past their deletion date, vault rows past expiry."""
    return retention.report()


@app.post("/api/compliance/retention/enforce")
def compliance_enforce(who: str = Depends(who)):
    """Run retention now under the signed-in name (delete raw files, expire vault rows), then reload the serving tables."""
    with MUTATE:
        out = retention.enforce(who); pipeline.run(only="load")
    return out


@app.get("/api/compliance/subject")
def compliance_subject(name: str, reason: str, who: str = Depends(who)):
    """Subject access: everything held under a name across vault, entities and documents; the lookup is logged with who, the request reference and the entity ids found."""
    return retention.subject(name, who, reason)


@app.post("/api/compliance/erase")
def compliance_erase(body: dict = Body(...), who: str = Depends(who)):
    """Erasure: delete vault rows and entity records for the given ids with a reason, logged, then reload the serving tables."""
    with MUTATE:
        out = retention.erase(body["entity_ids"], who, body.get("reason", "")); pipeline.run(only="load")
    return out


@app.get("/api/compliance/log")
def compliance_log():
    """The logs newest first: retention actions, vault reveals (last 100), review decisions (last 50)."""
    return dict(retention=retention.log(), vault_access=store.read_lines("vault_access.jsonl")[::-1][:100], review=store.read_lines("audit_log.jsonl")[::-1][:50])
