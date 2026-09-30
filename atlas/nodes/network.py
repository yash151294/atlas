"""Network search and traversal: who can reach whom, how two people connect, who sits within N hops. Search has two modes decided from the query:
  contributor  the query is (close to) a contributor's name -> that person's network
  reach        anything else (a region, a sector, an organisation, a topic, a role) -> contributors ranked by
               how much of the matching network they reached, with each matched source as dated evidence
Matching a person to a query = semantic similarity of their descriptor line (from the net_index table)
plus exact term overlap, so 'compliance at a Gulf bank' prefers a compliance officer at a Gulf bank over
anyone vaguely financial. Reach decays with time: a contact two years old counts half.
Every read is the app's serving cache (store.cache()): the tables and the descriptor matrix are in memory.
The research desk asks the same table through traverse(): reach (search), the shortest chains between two ends (paths), everyone
within N hops (neighbourhood), and who can re-approach the sources a briefing cites (reach_of). Computed, never generated (ADR 35)."""
import re
from datetime import date
import numpy as np, pandas as pd
from rapidfuzz import fuzz, process
from ..config import PARAMS
from .. import store
from .index import embed_query, terms

N = PARAMS["network"]


def overlap(qs: list[str], text: str) -> tuple[float, list[str]]:
    words = re.findall(r"[a-z0-9&]+", (text or "").lower())
    hit = [t for t in qs if any(w.startswith(t) or (len(t) > 4 and t.startswith(w) and len(w) > 4) for w in words)]
    return (len(hit) / len(qs) if qs else 0.0), hit


def where(text: str, hits: list[str]) -> list[str]:
    """The phrase of the indexed line each hit came from. The line carries the descriptor, the former organisations, the
    region, the sector, the seniority and the access topics, but the page shows only the descriptor, so a match on a topic
    ('central bank supervision Nigeria') read as an unexplained chip against a pharma lawyer. Show the phrase, not the word."""
    segs = [x.strip() for x in re.split(r"[.;]", text or "") if x.strip()]
    out = []
    for t in hits:
        for seg in segs:
            if any(w.startswith(t) or (len(t) > 4 and t.startswith(w) and len(w) > 4) for w in re.findall(r"[a-z0-9&]+", seg.lower())):
                out.append(re.sub(r"^access to:\s*", "", seg)[:70]); break
    return list(dict.fromkeys(out))


def vocab_filters(q: str, regions: list[str], sectors: list[str]) -> tuple[str | None, str | None, list[str]]:
    """A region or sector the query names is a filter, not a word to rank on. 'compliance at a gulf bank' means the Gulf;
    without this the only term that excluded a US pharma source and a Nigerian contact ('gulf') matched nothing and nothing
    required it to, and two of three terms was enough to clear the threshold. Conservative on purpose, and asymmetric: a
    region is a place name, so 'Japan & Korea' may be reached by either half, while a sector's halves are ordinary words an
    analyst types without meaning a filter ('payments', 'legal', 'regulator'), so a sector must be named in full."""
    low = f" {q.lower()} "
    def pick(values, parts_ok):
        for v in values:
            parts = [x.strip() for x in str(v).lower().split("&")] if parts_ok else []
            if str(v).lower() in low or any(len(x) > 3 and f" {x} " in low for x in parts):
                return v, [t for t in terms(str(v)) if t in terms(q)]
        return None, []
    r, rt = pick(regions, True)
    sec, st = pick(sectors, False)
    return r, sec, rt + st


def recency(d: str | None) -> float:
    if not d: return 0.5
    years = (date.today() - date.fromisoformat(d[:10])).days / 365.25
    return 0.5 ** (years / N["recency_half_life_years"])


def contributor_match(q: str, contributors: pd.DataFrame):
    if not q.strip() or not len(contributors): return None
    best = process.extractOne(q.lower(), contributors.name.str.lower().tolist(), scorer=fuzz.token_set_ratio)
    if best and best[1] >= 88:
        return contributors.iloc[best[2]].to_dict()
    return None


def match_scores(q: str, drop: list[str] = ()) -> dict[str, dict]:
    """Every person in the network scored against the query: min-max-normalised cosine of their descriptor line plus exact
    term overlap. Terms already spent as a filter (a region, a sector) are dropped from the overlap, so they neither rank
    everyone equally nor pad a weak match; the embedding still sees the whole question."""
    idx, M = store.cache().net_index()
    if not len(idx): return {}
    qv = np.asarray(embed_query(q), dtype="float32"); cos = M @ (qv / (np.linalg.norm(qv) + 1e-9))
    cos_n = (cos - cos.min()) / (cos.max() - cos.min() + 1e-9); qs = [t for t in terms(q) if t not in drop]
    out = {}
    for (eid, kind, text), cn in zip(idx[["entity_id", "kind", "text"]].itertuples(index=False), cos_n):
        ov, hit = overlap(qs, text)
        out[eid] = dict(score=round(0.55 * float(cn) + 0.45 * ov, 3), hits=hit, where=where(text, hit), kind=kind)
    return out


def facets() -> dict:
    c = store.cache().con.cursor()
    return dict(regions=[r[0] for r in c.execute("SELECT region_norm, count(*) FROM sources WHERE region_norm IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()],
                sectors=[r[0] for r in c.execute("SELECT sector, count(*) FROM sources WHERE sector IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()],
                projects=[r[0] for r in c.execute("SELECT DISTINCT project FROM redacted WHERE project IS NOT NULL ORDER BY 1").fetchall()],
                contributors=[r[0] for r in c.execute("SELECT name FROM contributors ORDER BY name").fetchall()],
                latest=c.execute("SELECT max(doc_date) FROM redacted").fetchone()[0],
                orgs=[r[0] for r in c.execute("SELECT org, count(*) FROM sources WHERE org <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 40").fetchall()])


def overview(S: pd.DataFrame, C: pd.DataFrame, E: pd.DataFrame) -> dict:
    """Home figures from the network tables: coverage by region and sector (sources, reach today, who holds them, last contact), the pace of interviews over 24 months, contributors by sources reached (quiet after a year)."""
    today = date.today(); cutoff = today.replace(year=today.year - 1).isoformat()
    S = S.copy(); S["reach"] = S.last_seen.map(recency)
    holders = lambda g: len({c for v in g.contributors.fillna("") for c in v.split("; ") if c})
    agg = lambda g: dict(n=int(len(g)), reach=round(float(g.reach.sum()), 2), last=g.last_seen.max(), contributors=holders(g))
    cells = [dict(region=r, sector=x, **agg(g)) for (r, x), g in S.dropna(subset=["region_norm", "sector"]).groupby(["region_norm", "sector"])]
    regions = sorted([dict(region=r, **agg(g)) for r, g in S.dropna(subset=["region_norm"]).groupby("region_norm")], key=lambda r: -r["reach"])
    sectors = sorted([dict(sector=x, n=int(len(g))) for x, g in S.dropna(subset=["sector"]).groupby("sector")], key=lambda x: -x["n"])
    iv = E[E.type == "INTERVIEWED"].date.fillna("").str[:7]
    ym = [f"{today.year + (today.month - 1 - i) // 12}-{(today.month - 1 - i) % 12 + 1:02d}" for i in range(23, -1, -1)]
    pace = [dict(m=m, n=int((iv == m).sum())) for m in ym]
    contributors = sorted([dict(id=c.entity_id, name=c.name, base=c.base, n_sources=int(c.n_sources), n_transcripts=int(c.n_transcripts), last_active=c.last_active, regions=c.regions,
                                status=getattr(c, "status", "") or "", quiet=(c.last_active or "") < cutoff) for c in C.itertuples()], key=lambda c: (c["n_sources"], c["last_active"] or ""), reverse=True)
    return dict(coverage=dict(cells=cells, regions=regions, sectors=sectors, warm=int((S.last_seen.fillna("") >= cutoff).sum()), single=int((S.n_contributors == 1).sum()),
                              max_cell_reach=max([c["reach"] for c in cells], default=1.0), half_life=N["recency_half_life_years"]),
                pace=dict(months=pace, last12=sum(p["n"] for p in pace[12:]), prev12=sum(p["n"] for p in pace[:12])), contributors=contributors)


def _interviews() -> pd.DataFrame:
    return store.cache().df("SELECT e.src AS contributor, e.dst AS source, e.doc_id, e.project, e.date FROM edges e WHERE e.type='INTERVIEWED'")


def search(q: str = "", region: str | None = None, sector: str | None = None, project: str | None = None, since: str | None = None, k: int = 10) -> dict:
    c = store.cache()
    C = c.df("SELECT * FROM contributors"); S = c.df("SELECT * FROM sources"); Pp = c.df("SELECT * FROM people"); IV = _interviews()
    who = contributor_match(q, C)
    if who and not (region or sector or project):
        return dict(mode="contributor", query=q, results=[profile(who["entity_id"])], filters=dict(region=region, sector=sector, project=project, since=since), read_from_query=[])
    # a region or sector the query names becomes a filter (an explicit one from the page always wins)
    vr, vs, spent = vocab_filters(q, sorted(S.region_norm.dropna().unique()), sorted(S.sector.dropna().unique())) if q.strip() else (None, None, [])
    read_from_query = [x for x in (vr if not region else None, vs if not sector else None) if x]
    region, sector = region or vr, sector or vs
    # candidate sources after structured filters
    keep = S
    if region: keep = keep[(keep.region_norm == region) | keep.region.fillna("").str.contains(region, case=False) | keep.country.fillna("").str.contains(region, case=False)]
    if sector: keep = keep[keep.sector == sector]
    if project: keep = keep[keep.projects.fillna("").str.contains(project, regex=False)]
    if since: keep = keep[keep.last_seen.fillna("") >= since]
    scores = match_scores(q, spent) if q.strip() else {}
    thr = N["match_threshold"] if q.strip() else -1
    matched_sources = {s["entity_id"]: s for s in keep.to_dict("records") if (scores.get(s["entity_id"], {}).get("score", 1.0) >= thr)}
    matched_people = {p["entity_id"]: p for p in Pp.to_dict("records") if q.strip() and scores.get(p["entity_id"], {}).get("score", 0) >= thr
                      and any(v in matched_sources or v in keep.entity_id.values for v in (p["via"] or "").split("; "))}
    by_source = {}
    for iv in IV.to_dict("records"):
        by_source.setdefault(iv["source"], []).append(iv)
    results = {}
    def bump(cid, amount):
        results.setdefault(cid, dict(reach=0.0, sources=[], mentioned=[]))["reach"] += amount
    for sid, s in matched_sources.items():
        sc = scores.get(sid, {}).get("score", 1.0)
        for cid in set(iv["contributor"] for iv in by_source.get(sid, [])):
            mine = sorted([iv for iv in by_source[sid] if iv["contributor"] == cid], key=lambda x: x["date"] or "", reverse=True)
            last = mine[0]["date"]
            bump(cid, sc * recency(last))
            results[cid]["sources"].append(dict(entity_id=sid, token=s["token"], descriptor=s["descriptor"], org=s["org"], region=s["region_norm"] or s["region"], sector=s["sector"],
                                                seniority=s["seniority"], score=sc, why=scores.get(sid, {}).get("hits", []), why_in=scores.get(sid, {}).get("where", []), last_date=last, n_transcripts=len(mine),
                                                projects=sorted({x["project"] for x in mine if x["project"]}), evidence=[dict(doc_id=x["doc_id"], date=x["date"], project=x["project"]) for x in mine],
                                                n_mentioned=s["n_mentioned"], shared_with=len(set(iv["contributor"] for iv in by_source[sid])) - 1))
    for pid, p in matched_people.items():
        sc = scores.get(pid, {}).get("score", 0)
        for sid in (p["via"] or "").split("; "):
            if sid not in S.entity_id.values: continue
            src = S[S.entity_id == sid].iloc[0]
            for cid in set(iv["contributor"] for iv in by_source.get(sid, [])):
                bump(cid, 0.5 * sc * recency(src["last_seen"]))
                results[cid]["mentioned"].append(dict(entity_id=pid, token=p["token"], descriptor=p["descriptor"], relationship=p["relationship"], score=sc,
                                                      why=scores.get(pid, {}).get("hits", []), why_in=scores.get(pid, {}).get("where", []), via_token=src["token"], via_descriptor=src["descriptor"], last_date=p["last_seen"]))
    out = []
    for cid, r in results.items():
        row = C[C.entity_id == cid]
        if not len(row): continue
        row = row.iloc[0].to_dict()
        r["sources"].sort(key=lambda x: (x["score"], x["last_date"] or ""), reverse=True); r["mentioned"].sort(key=lambda x: x["score"], reverse=True)
        out.append(dict(**row, reach=round(r["reach"], 2), n_matched=len(r["sources"]), matched_sources=r["sources"][: N["max_sources_per_contributor"]],
                        matched_mentioned=r["mentioned"][:5], last_contact=max([x["last_date"] or "" for x in r["sources"]] or [""]) or None))
    out.sort(key=lambda x: (x["reach"], x["last_contact"] or ""), reverse=True)
    return dict(mode="reach", query=q, terms=[t for t in terms(q) if t not in spent], results=out[:k], n_sources_matched=len(matched_sources),
                n_mentioned_matched=len(matched_people), read_from_query=read_from_query,
                filters=dict(region=region, sector=sector, project=project, since=since))


def profile(entity_id: str) -> dict:
    c = store.cache()
    row = c.df("SELECT * FROM contributors WHERE entity_id=?", [entity_id])
    if not len(row): return {}
    row = row.iloc[0].to_dict()
    IV = c.df("SELECT e.dst AS source, e.doc_id, e.project, e.date FROM edges e WHERE e.type='INTERVIEWED' AND e.src=?", [entity_id])
    S = c.df("SELECT * FROM sources"); Pp = c.df("SELECT * FROM people"); allIV = _interviews()
    by_id = {s["entity_id"]: s for s in S.to_dict("records")}
    sources = []
    for sid, g in IV.groupby("source"):
        s = by_id.get(sid)
        if s is None: continue
        others = sorted(set(allIV[(allIV.source == sid) & (allIV.contributor != entity_id)].contributor))
        g = g.sort_values("date", ascending=False)
        mentioned = Pp[Pp.entity_id.isin((s["mentioned"] or "").split("; "))]
        sources.append(dict(entity_id=sid, token=s["token"], descriptor=s["descriptor"], org=s["org"], region=s["region_norm"] or s["region"], country=s["country"], sector=s["sector"],
                            seniority=s["seniority"], topics=(s["topics"] or "").split("; ")[:8], former_orgs=s["former_orgs"], last_date=g.date.iloc[0], first_date=g.date.iloc[-1],
                            n_transcripts=int(len(g)), projects=sorted({x for x in g.project if x}), evidence=g[["doc_id", "date", "project"]].to_dict("records"),
                            shared_with=others, mentioned=[dict(token=p["token"], descriptor=p["descriptor"], relationship=p["relationship"], entity_id=p["entity_id"]) for p in mentioned.to_dict("records")]))
    sources.sort(key=lambda x: x["last_date"] or "", reverse=True)
    overlap_ = {}
    for s in sources:
        for o in s["shared_with"]:
            overlap_.setdefault(o, []).append(s["token"])
    names = dict(c.con.cursor().execute("SELECT entity_id, name FROM contributors").fetchall())
    coverage = dict(regions=sorted({s["region"] for s in sources if s["region"]}), sectors=sorted({s["sector"] for s in sources if s["sector"]}),
                    orgs=sorted({s["org"] for s in sources if s["org"]}), projects=sorted({p for s in sources for p in s["projects"]}))
    timeline = sorted([dict(date=e["date"], project=e["project"], doc_id=e["doc_id"], token=by_id.get(e["source"], {}).get("token", e["source"]),
                            descriptor=by_id.get(e["source"], {}).get("descriptor", "")) for e in IV.to_dict("records")], key=lambda x: x["date"] or "")
    row["n_mentioned"] = sum(len(s["mentioned"]) for s in sources)
    return dict(**row, sources=sources, coverage=coverage, timeline=timeline,
                overlap=[dict(entity_id=o, name=names.get(o, o), shared_tokens=t) for o, t in sorted(overlap_.items(), key=lambda kv: -len(kv[1]))])


def source_card(entity_id: str) -> dict:
    c = store.cache()
    s = c.df("SELECT * FROM sources WHERE entity_id=?", [entity_id])
    if not len(s): return {}
    s = s.iloc[0].to_dict()
    hist = c.df("SELECT e.doc_id, e.project, e.date, e.confidence, c.name AS contributor, c.entity_id AS contributor_id FROM edges e JOIN contributors c ON c.entity_id=e.src WHERE e.type='INTERVIEWED' AND e.dst=? ORDER BY e.date", [entity_id])
    Pp = c.df("SELECT * FROM people WHERE entity_id IN (SELECT dst FROM edges WHERE type='MENTIONED' AND src=?)", [entity_id])
    s["mentioned"] = Pp.to_dict("records")   # replaces the id list from the gold table with full rows
    return dict(**s, history=hist.to_dict("records"), topics_list=(s["topics"] or "").split("; "))


def ego(ids: list[str], since: str | None = None, until: str | None = None, topics: bool = False) -> dict:
    """Two-hop neighbourhood of the focus entities, dated edges filtered to the window."""
    c = store.cache()
    E = c.df("SELECT * FROM edges"); S = {s["entity_id"]: s for s in c.df("SELECT * FROM sources").to_dict("records")}
    Pp = {p["entity_id"]: p for p in c.df("SELECT * FROM people").to_dict("records")}; C = {x["entity_id"]: x for x in c.df("SELECT * FROM contributors").to_dict("records")}
    O = dict(c.con.cursor().execute("SELECT entity_id, canonical FROM entities WHERE type='ORG'").fetchall())
    if since: E = E[(E.type.isin(["WORKS_AT", "FORMERLY_AT", "HAS_ACCESS_TO"])) | (E.date.fillna("") >= since)]
    if until: E = E[(E.type.isin(["WORKS_AT", "FORMERLY_AT", "HAS_ACCESS_TO"])) | (E.date.fillna("9999") <= until)]
    if not topics: E = E[E.type != "HAS_ACCESS_TO"]
    focus = set(ids)
    hop1 = E[E.src.isin(focus) | E.dst.isin(focus)]
    near = set(hop1.src) | set(hop1.dst)
    # second hop only from sources (their orgs and mentioned people), never fanning out through other contributors
    hop2 = E[(E.src.isin(near & set(S)) & E.type.isin(["WORKS_AT", "FORMERLY_AT", "MENTIONED", "HAS_ACCESS_TO"]))]
    edges = pd.concat([hop1, hop2]).drop_duplicates(subset=["src", "dst", "type", "doc_id"])
    cap = {}
    keep = []
    for e in edges.to_dict("records"):   # at most six topic spokes per source keeps the map legible
        if e["type"] == "HAS_ACCESS_TO":
            cap[e["src"]] = cap.get(e["src"], 0) + 1
            if cap[e["src"]] > 6: continue
        keep.append(e)
    nodes = {}
    for e in keep:
        for n in (e["src"], e["dst"]):
            if n in nodes: continue
            if n in C: nodes[n] = dict(id=n, kind="contributor", label=C[n]["name"], title=f'{C[n]["base"]} · {C[n]["n_sources"]} sources', focus=n in focus)
            elif n in S: nodes[n] = dict(id=n, kind="source", label=f'{S[n]["token"]}\n{(S[n]["descriptor"] or "")[:44]}', title=S[n]["descriptor"], n=S[n]["n_transcripts"], focus=n in focus)
            elif n in Pp: nodes[n] = dict(id=n, kind="mentioned", label=(Pp[n]["descriptor"] or Pp[n]["token"] or "")[:28], title=f'{Pp[n]["token"]} {Pp[n]["descriptor"]} ({Pp[n]["relationship"]})', focus=n in focus)
            elif n in O: nodes[n] = dict(id=n, kind="org", label=O[n], focus=n in focus)
            elif n.startswith("topic:"): nodes[n] = dict(id=n, kind="topic", label=n[6:])
            else: nodes[n] = dict(id=n, kind="org", label=n.split(":", 1)[-1])
    return dict(nodes=list(nodes.values()), edges=[dict(source=e["src"], target=e["dst"], type=e["type"], doc_id=e["doc_id"], date=e["date"], project=e["project"]) for e in keep])


# ---------- traversal for the research desk (ADR 35) ----------
def _when(d: str | None) -> str:
    """An ISO date as a person writes it: 11 June 2026."""
    return date.fromisoformat(d[:10]).strftime("%-d %B %Y") if d else ""


def _names() -> dict[str, tuple[str, str]]:
    """Node id -> (kind, label) for everything the edge table can name: contributors by name, people by token and descriptor, organisations by name."""
    c = store.cache().con.cursor()
    out = {r[0]: ("contributor", r[1]) for r in c.execute("SELECT entity_id, name FROM contributors").fetchall()}
    out |= {r[0]: ("source", f"{r[1]} {r[2] or ''}".strip()) for r in c.execute("SELECT entity_id, token, descriptor FROM sources").fetchall()}
    out |= {r[0]: ("mentioned", f"{r[1]} {r[2] or ''}".strip()) for r in c.execute("SELECT entity_id, token, descriptor FROM people").fetchall()}
    orgs = c.execute("SELECT entity_id, canonical FROM entities WHERE type='ORG'").fetchall(); canon = {l.lower(): l for _, l in orgs}
    out |= {i: ("org", l) for i, l in orgs}
    out |= {r[0]: ("org", canon.get(r[0][4:], r[0][4:])) for r in c.execute("SELECT DISTINCT dst FROM edges WHERE dst LIKE 'org:%'").fetchall()}   # FORMERLY_AT keys the employer by its lowercased name
    return out


def resolve_node(text: str, names: dict[str, tuple[str, str]]) -> list[str]:
    """What the analyst wrote -> node ids: a token exactly, a contributor or an organisation by fuzzy name, else the people whose descriptor line matches the description."""
    t = (text or "").strip()
    if not t: return []
    tok = [i for i, (k, l) in names.items() if k in ("source", "mentioned") and l.split(" ")[0] == t]
    if tok: return tok
    C = store.cache().df("SELECT entity_id, name FROM contributors")
    who = contributor_match(t, C)
    if who: return [who["entity_id"]]
    orgs = [(i, l) for i, (k, l) in names.items() if k == "org"]
    best = process.extractOne(t.lower(), [l.lower() for _, l in orgs], scorer=fuzz.token_set_ratio) if orgs else None
    if best and best[1] >= 88: return [i for i, l in orgs if l.lower() == orgs[best[2]][1].lower()]   # the entity id and the org: string id are the same organisation
    sc = match_scores(t)
    return [e for e, v in sorted(sc.items(), key=lambda kv: -kv[1]["score"]) if v["score"] >= N["match_threshold"]][:20]


def _bfs(E: pd.DataFrame, src: list[str], max_hops: int, dst: frozenset = frozenset()):
    """Layered breadth-first search over the edges as undirected, every parent kept at a node's first depth (so ties in shortest paths survive) but one edge per parent, the latest dated (two interviews of the same source are one route); stops at the layer that reaches dst."""
    adj = {}   # ponytail: adjacency rebuilt per question; memoise on cache refresh when the edge table passes 100k rows
    for e in E.itertuples(index=False):
        adj.setdefault(e.src, []).append((e.dst, e)); adj.setdefault(e.dst, []).append((e.src, e))
    depth = {s: 0 for s in src}; parents = {s: [] for s in src}; layer = list(src)
    while layer and not (dst & set(layer)) and depth[layer[0]] < max_hops:
        nxt = []
        for n in layer:
            for m, e in adj.get(n, []):
                if m not in depth: depth[m] = depth[n] + 1; parents[m] = []; nxt.append(m)
                if depth[m] == depth[n] + 1:
                    i = next((k for k, (p, _) in enumerate(parents[m]) if p == n), None)
                    if i is None: parents[m].append((n, e))
                    elif (e.date or "") > (parents[m][i][1].date or ""): parents[m][i] = (n, e)
        layer = nxt
    return depth, parents, [n for n in layer if n in dst]


def paths(E: pd.DataFrame, src: list[str], dst: list[str], max_hops: int = 4, limit: int = 10) -> list[list[dict]]:
    """Every shortest chain from any src node to any dst node, each step with its edge type, document, date and engagement."""
    depth, parents, found = _bfs(E, src, max_hops, frozenset(dst))
    out = []
    def walk(n, tail):
        if len(out) >= limit: return
        if not parents[n]: out.append(tail); return
        for p, e in parents[n]: walk(p, [dict(src=p, dst=n, type=e.type, forward=e.src == p, doc_id=e.doc_id, date=e.date, project=e.project)] + tail)
    for n in found: walk(n, [])
    return out


def neighbourhood(E: pd.DataFrame, ids: list[str], hops: int = 2) -> list[dict]:
    """Everyone within N hops of the given nodes, nearest first, each with the node it was reached through."""
    depth, parents, _ = _bfs(E, ids, hops)
    return [dict(id=n, depth=d, via=parents[n][0][0], type=parents[n][0][1].type, forward=parents[n][0][1].src == parents[n][0][0], date=parents[n][0][1].date)
            for n, d in sorted(depth.items(), key=lambda kv: kv[1]) if d > 0]


def reach_of(tokens: list[str]) -> list[dict]:
    """Who can re-approach these sources: every contributor who interviewed them, with dates and engagements, from the edge table."""
    if not tokens: return []
    rows = store.cache().df(f"SELECT s.token, s.descriptor, c.name, c.entity_id AS contributor_id, e.date, e.project, e.doc_id FROM edges e JOIN sources s ON s.entity_id=e.dst "
                            f"JOIN contributors c ON c.entity_id=e.src WHERE e.type='INTERVIEWED' AND s.token IN ({','.join('?' * len(tokens))}) ORDER BY s.token, e.date DESC", list(tokens))
    out = {}
    for r in rows.to_dict("records"):
        out.setdefault(r["token"], dict(token=r["token"], descriptor=r["descriptor"], contacts=[]))["contacts"].append(dict(name=r["name"], contributor_id=r["contributor_id"], date=r["date"], project=r["project"], doc_id=r["doc_id"]))
    return list(out.values())


def traverse(kind: str, frm: str = "", to: str = "", hops: int = 0, region: str | None = None, sector: str | None = None, project: str | None = None) -> dict:
    """The research desk's network question, answered from the edge table and never generated: reach, the shortest chains between two ends, or everyone within N hops.
    Returns the rows for the page and a one-line headline."""
    names = _names(); lab = lambda n: names.get(n, ("org", n.split(":", 1)[-1]))
    short = lambda n: lab(n)[1].split(" ")[0] if lab(n)[0] in ("source", "mentioned") else lab(n)[1]   # a token or a name; the block carries the descriptor
    long = lambda n: f"{short(n)} ({lab(n)[1].split(' ', 1)[1]})" if lab(n)[0] in ("source", "mentioned") and " " in lab(n)[1] else lab(n)[1]   # the findings' convention: token (descriptor)
    plural = lambda n, w: f"{n} {w if n == 1 else ('people' if w == 'person' else w + 's')}"
    join = lambda xs: xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    if kind == "reach":
        r = search(to or frm, region=region, sector=sector, project=project, k=8)
        if r["mode"] == "contributor":   # the query was a contributor's name: their network, in the reach shape
            p = r["results"][0]
            res = [dict(name=p["name"], entity_id=p["entity_id"], reach=None, n_matched=len(p["sources"]), matched_sources=p["sources"], last_contact=p["sources"][0]["last_date"] if p["sources"] else None)]
            return dict(kind="reach", query=r["query"], results=res, headline=f"{p['name']} has interviewed {plural(len(p['sources']), 'source')} across {plural(len(p['coverage']['projects']), 'engagement')}" + (f", most recently on {_when(res[0]['last_contact'])}." if res[0]["last_contact"] else "."))
        latest = max([x["last_contact"] or "" for x in r["results"]] or [""])
        head = (f"{plural(len(r['results']), 'contributor')} can reach {plural(r['n_sources_matched'], 'matching source')}" + (f" and {plural(r['n_mentioned_matched'], 'person')} they named" if r["n_mentioned_matched"] else "") + (f"; the most recent contact was on {_when(latest)}." if latest else ".")) if r["results"] else "No contributor has reached anyone matching that."
        return dict(kind="reach", query=r["query"], results=r["results"], headline=head)
    E = store.cache().df("SELECT src, dst, type, doc_id, date, project FROM edges WHERE type <> 'HAS_ACCESS_TO'")   # ponytail: topics excluded, they would join everyone through 'compliance'; a topic-aware path is a flag away
    a = resolve_node(frm, names)
    if kind == "path":
        b = resolve_node(to, names); hops = min(hops or 4, 6)
        if not a or not b: return dict(kind="path", paths=[], headline=f"Could not place {' or '.join(repr(x) for x, ids in ((frm, a), (to, b)) if not ids)} in the network.")
        ps = paths(E, a, b, max_hops=hops)
        if not ps: return dict(kind="path", paths=[], headline=f"No connection found between {short(a[0])} and {short(b[0])} through shared sources, the people they named or their organisations.")
        L = len(ps[0]); rows = [[dict(**s, src_kind=lab(s["src"])[0], src_label=lab(s["src"])[1], dst_kind=lab(s["dst"])[0], dst_label=lab(s["dst"])[1]) for s in p] for p in ps]
        A, B = (short(ps[0][0]["src"]), short(ps[0][-1]["dst"])) if L else (short(a[0]),) * 2
        mids = list(dict.fromkeys(short(s["dst"]) for p in ps for s in p[:-1]))   # the connectors, in route order, once each
        if L == 0: head = f"{A} and {B} are the same entry in the network."
        elif L == 1: head = f"{A} and {B} are directly connected."
        elif L == 2: head = f"{A} and {B} are connected through {long(ps[0][0]['dst']) if len(mids) == 1 else join(mids)}."
        else: head = f"{A} reaches {B} through {join([short(s['dst']) for s in ps[0][:-1]])}" + (f", and there {'is' if len(ps) == 2 else 'are'} {plural(len(ps) - 1, 'other route')}." if len(ps) > 1 else ".")
        return dict(kind="path", paths=rows, hops=L, headline=head)
    hops = min(hops or 2, 6)
    if not a: return dict(kind="neighbourhood", rows=[], headline=f"Could not place {frm!r} in the network.")
    rows, per = [], {}
    for r in neighbourhood(E, a, hops):
        per[r["depth"]] = per.get(r["depth"], 0) + 1
        if per[r["depth"]] <= 40: rows.append(dict(r, kind=lab(r["id"])[0], label=lab(r["id"])[1], via_kind=lab(r["via"])[0], via_label=lab(r["via"])[1]))   # ponytail: 40 per step keeps the block readable; the map has the rest
    further = {2: "one step further", 3: "two steps further"}
    more = [f"{per[d]} more {further.get(d, f'{d - 1} steps further')}" for d in sorted(per) if d > 1]
    head = f"{short(a[0])} is directly connected to {plural(per.get(1, 0), 'contact')}" + (f", with {join(more)}" if more else "") + "." if per else f"{short(a[0])} has no connections in the network."
    return dict(kind="neighbourhood", rows=rows, hops=hops, headline=head)
