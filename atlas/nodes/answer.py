"""The research desk: plan -> retrieve in parallel -> rerank -> briefing with per-finding citations -> validate.
A fast planner turns the question (plus the thread so far) into one to four search queries and structured
filters drawn from the graph's vocabulary (projects, regions, sectors, organisations, dates). Retrieval is
the same hybrid contextual stack for every sub-query; results are fused, reranked against the original
question, and the answer model writes a briefing whose every finding cites chunk ids. Citations are then
checked deterministically: a finding whose citations are not in the evidence is dropped, not shown."""
import re
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel, Field, model_validator
from ..config import PARAMS
from .. import store
from ..llm import complete
from .index import retrieve
from .rerank import rerank
from . import network

A = PARAMS["answer"]


class Plan(BaseModel):
    queries: list[str] = Field(description="1 to 4 self-contained search queries that together cover the question; resolve pronouns using the thread")
    projects: list[str] = Field(default_factory=list, description="only if the question names or clearly implies an engagement code")
    regions: list[str] = Field(default_factory=list, description="only from the given region labels, only if the question is about a region")
    sectors: list[str] = Field(default_factory=list, description="only from the given sector labels, only if the question is about a sector")
    orgs: list[str] = Field(default_factory=list, description="organisation names the question is about, as written")
    date_from: str | None = Field(default=None, description="YYYY-MM-DD if the question limits the period")
    date_to: str | None = None
    doc_types: list[str] = Field(default_factory=list, description="['transcript'] or ['deliverable'] only if the question asks for one kind")
    network_kind: str = Field(default="", description="'reach' when the question asks who can reach, contact or introduce people matching a description; 'path' when it asks how two named people or organisations are connected or the shortest path between them; 'neighbourhood' when it asks who is within some number of hops of one person; else empty")
    network_from: str = Field(default="", description="path or neighbourhood: the starting person or organisation as written (a contributor's name, a token like [PERSON_0012], an organisation, or a short description)")
    network_to: str = Field(default="", description="path: the other end as written; reach: what is being reached, as a short description")
    network_hops: int = Field(default=0, description="the depth the question asks for, else 0")

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, d):
        """gpt-5-mini sends null for a field it deems not applicable; drop it so the default applies instead of failing the whole plan."""
        if isinstance(d, dict):
            d = {k: v for k, v in d.items() if v is not None}
            if not str(d.get("network_hops", 0)).isdigit(): d.pop("network_hops", None)
        return d


class Finding(BaseModel):
    claim: str = Field(description="one or two sentences, specific, no citation markers inside the text")
    citations: list[str] = Field(description="chunk ids that support this claim")
    who: str = Field(default="", description="who said it: the source's token and descriptor, or 'report' for a deliverable")
    date: str | None = Field(default=None, description="date of the evidence, YYYY-MM-DD")


class Event(BaseModel):
    date: str = Field(description="only as precise as the evidence: YYYY, YYYY-MM or YYYY-MM-DD; never invent a day or month")
    event: str
    citations: list[str]


class Briefing(BaseModel):
    headline: str = Field(description="one sentence that answers the question directly; if the evidence does not answer it, say so")
    findings: list[Finding] = Field(description="3 to 8 findings ordered by importance; each must be supported by its citations")
    timeline: list[Event] = Field(default_factory=list, description="only when dates matter to the answer")
    gaps: list[str] = Field(default_factory=list, description="what the corpus does not say that the question needs, 0 to 3 items")
    confidence: float = Field(ge=0, le=1)
    not_in_corpus: bool = False
    follow_ups: list[str] = Field(default_factory=list, description="up to 3 questions the corpus could answer next")


PLAN_SYSTEM = """You plan searches over a corpus of pseudonymised interview transcripts and intelligence reports for a strategic-intelligence firm.
Decompose the analyst's question into the smallest set of search queries that will retrieve the evidence needed (1 for a simple question, up to 4 for
a comparison or a multi-part question). Queries are full phrases, not keywords. Only set filters when the question clearly implies them; a wrong
filter hides evidence, so prefer no filter. Use only the labels given.
The corpus also carries a network: contributors (the firm's interviewers, named in the vocabulary) who interviewed sources (shown as tokens with a
descriptor), the professionals those sources named, and their organisations. When the question asks who can reach, contact or introduce someone, set
network_kind 'reach' and put what is being reached in network_to. When it asks how two named people or organisations are connected, or the shortest
path between them, set 'path' with both ends. When it asks who is within some number of hops of one person, set 'neighbourhood' with network_from and
network_hops. Copy names, tokens and organisations as written. When the network alone answers the question (a path, a neighbourhood, a plain who can
reach), return no queries; when the question also asks what people said, return queries as well."""

SYSTEM = """You are the research desk for a strategic-intelligence firm. You write briefings only from the evidence chunks provided.
People appear as tokens like [PERSON_0012]; each chunk lists who they are (the source, the contributor, or someone mentioned, with a descriptor).
Refer to people by token and descriptor, e.g. "[PERSON_0012] (former Head of Compliance, Meridian Gulf Bank)". Never guess a name.
Every finding must be supported by the chunks it cites. Cite exactly the chunks that contain the facts stated: every number, date, name or
position in a finding must appear in at least one of its cited chunks. Prefer one finding per chunk; if a finding combines two chunks, cite both.
Do not carry a detail from an uncited chunk into a finding, and do not infer timing, causes or outcomes the chunks do not state.
Write the headline as the direct answer. Findings are specific: numbers, dates, positions, who said what. If the evidence does not answer the
question, say so in the headline, set not_in_corpus=true and put the closest evidence in findings. British English, analyst tone."""


NET_TASK = (" Who can reach whom is already answered above these passages, computed from the network table. Do not restate it, do not judge whether the"
            " corpus answers it, and do not make it the subject of your headline: your headline answers only the rest of the question, from the passages.")
# It used to say only "answered separately ... write only about what the passages say", and the model read that as licence to rule on reach anyway:
# a briefing opened "The corpus does not state who can reach a compliance officer at a Gulf bank" directly under a computed block naming four
# contributors who can. A module constant so the wording can be A/B'd against a question set rather than argued about.


def facets() -> dict:
    con = store.cache().con.cursor()
    return dict(projects=[r[0] for r in con.execute("SELECT DISTINCT project FROM redacted WHERE project IS NOT NULL ORDER BY 1").fetchall()],
                regions=[r[0] for r in con.execute("SELECT DISTINCT region_norm FROM sources WHERE region_norm IS NOT NULL ORDER BY 1").fetchall()],
                sectors=[r[0] for r in con.execute("SELECT DISTINCT sector FROM sources WHERE sector IS NOT NULL ORDER BY 1").fetchall()],
                orgs=[r[0] for r in con.execute("SELECT DISTINCT org FROM sources WHERE org IS NOT NULL AND org <> '' ORDER BY 1").fetchall()],
                dates=con.execute("SELECT min(doc_date), max(doc_date) FROM redacted").fetchone(),
                contributors=[r[0] for r in con.execute("SELECT name FROM contributors ORDER BY name").fetchall()])


def plan(question: str, history: list[dict], fac: dict) -> Plan:
    thread = "\n".join(f"Q: {h['question']}\nA: {h['headline']}" for h in history[-A["history_turns"]:]) or "(none)"
    user = (f"<thread>\n{thread}\n</thread>\n<vocabulary>\nprojects: {', '.join(fac['projects'])}\nregions: {', '.join(fac['regions'])}\n"
            f"sectors: {', '.join(fac['sectors'])}\norganisations: {', '.join(fac['orgs'][:60])}\ncontributors: {', '.join(fac['contributors'])}\n"
            f"documents dated {fac['dates'][0]} to {fac['dates'][1]}\n</vocabulary>\n"
            f"<question>{question}</question>\nPlan the searches.")
    try:
        p = complete(user, system=PLAN_SYSTEM, purpose="plan", schema=Plan, model=A.get("planner_model"), effort="low", max_tokens=800)
        p.network_kind = p.network_kind.strip().lower() if p.network_kind in ("reach", "path", "neighbourhood") else ""
        p.queries = [q for q in p.queries if q.strip()][:4] or ([] if p.network_kind else [question])   # a pure network question needs no passages
        p.regions = [r for r in p.regions if r in fac["regions"]]; p.sectors = [s for s in p.sectors if s in fac["sectors"]]
        p.projects = [x for x in p.projects if x in fac["projects"]]
        return p
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("planner unavailable: %s", e)
        return Plan(queries=[question])


def _filters(p: Plan, doc_type: str | None) -> dict:
    return dict(projects=p.projects, regions=p.regions, sectors=p.sectors, orgs=p.orgs, date_from=p.date_from, date_to=p.date_to,
                doc_types=p.doc_types or ([doc_type] if doc_type else []))


def gather(p: Plan, project, doc_type) -> list[dict]:
    """Every sub-query in parallel; fused by reciprocal rank across queries. Filters that hide everything are dropped."""
    f = _filters(p, doc_type)
    def one(q, filt):
        return retrieve(q, k=PARAMS["index"]["recall_k"], project=project, filters=filt)
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda q: one(q, f), p.queries))
    if sum(len(r) for r in results) < 5 and any(v for v in f.values()):   # the planner's filter was too tight: retry without it
        with ThreadPoolExecutor(4) as ex:
            results = list(ex.map(lambda q: one(q, dict(doc_types=f["doc_types"])), p.queries))
        p.projects, p.regions, p.sectors, p.orgs, p.date_from, p.date_to = [], [], [], [], None, None
    score, rows = {}, {}
    for res in results:
        for i, c in enumerate(res):
            score[c["chunk_id"]] = score.get(c["chunk_id"], 0) + 1 / (60 + i); rows[c["chunk_id"]] = c
    out = [rows[c] for c in sorted(score, key=score.get, reverse=True)]
    for c in out: c["fused"] = score[c["chunk_id"]]
    return out


class Check(BaseModel):
    supported: bool = Field(description="true if every factual claim in the finding appears in, or follows directly from, the cited chunks taken together")
    reason: str = Field(description="one short line: what is unsupported, or 'supported'")


VERIFY_SYSTEM = """You check one finding of a research briefing against the evidence chunks it cites. Supported means every factual claim (numbers, dates,
names, positions, causes) is stated in, or follows directly from, the cited chunks taken together. Paraphrase is fine. Anything not in the chunks is unsupported.
Judge only against the chunks shown."""


def verify(findings: list[dict], cited: dict) -> list[dict]:
    """Second opinion, cheap and parallel: a finding the checker cannot support is shown flagged, never silently."""
    def one(f):
        ev = "\n\n".join(f"<chunk id=\"{c}\">\n{cited[c]['text']}\n</chunk>" for c in f["citations"] if c in cited)
        try:
            v = complete(f"<finding>{f['claim']}</finding>\n{ev}\nIs the finding supported?", system=VERIFY_SYSTEM, purpose="verify", schema=Check, effort="low", max_tokens=2500)
            return dict(f, verified=bool(v.supported), verify_note="" if v.supported else v.reason)
        except Exception as e:
            return dict(f, verified=None, verify_note=f"checker unavailable: {str(e)[:80]}")
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, findings))


def ask(question: str, project: str | None = None, doc_type: str | None = None, history: list[dict] | None = None) -> dict:
    history = history or []
    fac = facets()
    p = plan(question, history, fac)
    net = network.traverse(p.network_kind, p.network_from, p.network_to, p.network_hops, region=(p.regions or [None])[0], sector=(p.sectors or [None])[0],
                           project=project or (p.projects or [None])[0]) if p.network_kind else None   # computed from the edge table; the answer model never sees it (ADR 35)
    cands = gather(p, project, doc_type) if p.queries else []
    empty = dict(question=question, plan=p.model_dump(), headline=net["headline"] if net else "The corpus holds no evidence for that question.", findings=[], timeline=[], gaps=[],
                 citations=[], confidence=0.0, not_in_corpus=not net, follow_ups=[], candidates=[], dropped=0, network=net, reach=[],
                 headline_verified=None, headline_note="")
    if not cands:
        return empty
    top = rerank(question, cands, k=A["top_k"])
    evidence = "\n\n".join(f"<chunk id=\"{c['chunk_id']}\" project=\"{c['project']}\" date=\"{c['doc_date']}\" type=\"{c['doc_type']}\">\n"
                           f"{('Context: ' + c['header']) if c.get('header') else ''}\n{('People: ' + c['people'].replace(chr(10), '; ')) if c.get('people') else ''}\n{c['text']}\n</chunk>" for c in top)
    task = "Write the briefing." + (NET_TASK if net else "")   # the question decides the shape: a "who could we re-approach" question yields a sourcing briefing, a "how has the position changed" one a timeline (the mode selector was removed, ADR 33)
    thread = "\n".join(f"Q: {h['question']}\nA: {h['headline']}" for h in history[-A["history_turns"]:])
    user_msg = (f"<thread>\n{thread}\n</thread>\n" if thread else "") + f"<evidence>\n{evidence}\n</evidence>\n\n<question>{question}</question>\n{task}"
    b = complete(user_msg, system=SYSTEM, purpose="answer", schema=Briefing, effort=A.get("effort"), max_tokens=A.get("max_tokens"), model=A.get("model"))
    cited = {c["chunk_id"]: c for c in top}
    findings, dropped = [], 0
    for f in b.findings:
        ok = [c for c in f.citations if c in cited]
        if not ok:
            dropped += 1; continue   # a claim without evidence in front of the model is not shown
        findings.append(dict(claim=re.sub(r"\s*\[[^\]]+#\d+\]", "", f.claim).strip(), citations=ok, who=f.who, date=f.date or (cited[ok[0]].get("doc_date"))))
    head = {}
    if A.get("verify", True) and findings:
        # the headline is checked too, against the union of what the findings cite: it is the one sentence an analyst
        # reads aloud, and it used to be the only generated sentence nothing validated (a briefing led with "about eleven
        # months" while the checker was rejecting that same figure three lines below it). Same pool, so no added latency.
        checked = verify(findings + [dict(claim=b.headline, citations=sorted({c for f in findings for c in f["citations"]}))], cited)
        findings, head = checked[:-1], checked[-1]
        findings.sort(key=lambda f: f.get("verified") is False)   # supported first; flagged ones stay visible at the end
    timeline = [dict(date=e.date, event=e.event, citations=[c for c in e.citations if c in cited]) for e in b.timeline]
    timeline = [e for e in timeline if e["citations"]]
    order = []
    for f in findings:
        for c in f["citations"]:
            if c not in order: order.append(c)
    return dict(question=question, plan=p.model_dump(), headline=net["headline"] if net else b.headline, text_headline=b.headline if net else "", findings=findings, timeline=timeline, gaps=b.gaps,
                citations=[cited[c] for c in order], confidence=b.confidence, not_in_corpus=(b.not_in_corpus or not findings) and not net,
                follow_ups=b.follow_ups[:3], candidates=top, dropped=dropped, network=net,
                reach=network.reach_of(sorted({cited[c]["source_token"] for c in order if cited[c].get("source_token")})),   # who can re-approach the sources cited
                headline_verified=head.get("verified"), headline_note=head.get("verify_note", ""),
                n_verified=sum(1 for f in findings if f.get("verified")), n_flagged=sum(1 for f in findings if f.get("verified") is False))
