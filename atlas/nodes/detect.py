"""Silver step 1: find people, organisations and PII in a document, and say who each person is.
Two detectors, unioned: Presidio (regex + spaCy NER, deterministic, cheap) and Claude (structured
extraction, understands context). Claude also classifies every person by role, which is what lets
the redactor keep professional descriptors and drop private individuals:
  source        the interviewee            -> pseudonym token, descriptor retained, graph node
  contributor   the interviewer            -> pseudonym token, linked to Salesforce, graph node
  third_party   a named professional        -> pseudonym token, descriptor retained, second-degree graph node
  private       family, friends, no role    -> redacted, never an entity, never in the graph
Disagreement or middling confidence -> review flag. Recall matters more than precision here."""
import logging, os, re
from typing import Literal
import pandas as pd
from pydantic import BaseModel, Field
from ..config import PARAMS
from ..llm import complete

TYPES = Literal["PERSON", "ORG", "PHONE", "EMAIL", "ADDRESS", "DATE_OF_BIRTH", "NATIONALITY", "FINANCIAL", "FAMILY", "ID_NUMBER"]
ROLES = Literal["source", "contributor", "third_party", "private", "n/a"]
REGIONS = ["UK & Ireland", "Western Europe", "Nordics", "Central & Eastern Europe", "Gulf", "Levant & North Africa", "Sub-Saharan Africa",
           "South Asia", "Southeast Asia", "Japan & Korea", "Greater China", "Australia & NZ", "North America", "Latin America", "Global"]
SECTORS = ["banking & financial services", "fintech & payments", "energy & natural resources", "industrial & manufacturing", "logistics & transport",
           "technology & telecoms", "healthcare & pharma", "government & regulator", "legal & professional services", "media", "consumer & retail",
           "real estate & construction", "other"]
PRESIDIO_MAP = {"PERSON": "PERSON", "ORGANIZATION": "ORG", "PHONE_NUMBER": "PHONE", "EMAIL_ADDRESS": "EMAIL", "LOCATION": "ADDRESS",
                "NRP": "NATIONALITY", "IBAN_CODE": "FINANCIAL", "CREDIT_CARD": "FINANCIAL", "US_PASSPORT": "ID_NUMBER", "UK_NHS": "ID_NUMBER",
                "US_SSN": "ID_NUMBER", "US_DRIVER_LICENSE": "ID_NUMBER", "URL": None, "DATE_TIME": None, "IP_ADDRESS": None}
_analyzer = None


def analyzer():
    global _analyzer
    if _analyzer is None:
        from presidio_analyzer import AnalyzerEngine  # lazy: the Fabric live notebook runs model-only
        _analyzer = AnalyzerEngine()  # loads spaCy en_core_web_lg
    return _analyzer


class Mention(BaseModel):
    """Structured-output schema limit: every field required, no unions, no defaults. Empty string = unknown."""
    text: str = Field(description="exact surface text as it appears in the document")
    type: str = Field(description="PERSON | ORG | PHONE | EMAIL | ADDRESS | DATE_OF_BIRTH | NATIONALITY | FINANCIAL | FAMILY | ID_NUMBER")
    role: str = Field(description="PERSON only: source | contributor | third_party (named professional with a role or organisation) | private (family, friend, assistant, anyone named in a personal context, or unsure). n/a for non-persons")
    title: str = Field(description="PERSON third_party/source: job title or role as stated, e.g. 'founder', 'Head of Compliance'; else empty")
    organisation: str = Field(description="PERSON third_party/source: organisation as stated; else empty")
    relationship: str = Field(description="PERSON third_party: how the source knows them, e.g. 'former boss', 'regulator counterpart', 'subject of the report'; else empty")
    confidence: float = Field(description="0 to 1")


class Extraction(BaseModel):
    doc_kind: str = Field(description="transcript | deliverable | other")
    project: str = Field(description="project codename or empty")
    interview_date: str = Field(description="YYYY-MM-DD or empty")
    contributor_name: str = Field(description="the interviewer's full name or empty")
    source_name: str = Field(description="the interviewed source's full name if identifiable, else empty")
    source_role: str
    source_org: str
    source_former_orgs: list[str]
    source_region: str = Field(description="one of the controlled region labels, or empty")
    source_country: str
    source_sector: str = Field(description="one of the controlled sector labels for the source's current or most relevant organisation, or empty")
    source_seniority: str = Field(description="board/c-suite | senior | mid | junior | official | unknown")
    source_descriptor_public: str = Field(description="a generalised, non-identifying descriptor, e.g. 'former senior compliance executive at a Gulf bank'")
    topics: list[str] = Field(description="5 to 10 short access/topic keywords the source has real access to")
    summary: str = Field(description="two sentences, no names")
    mentions: list[Mention]


STRICT = """Be strict about everything except people. These are NOT personal data and must never be listed: calendar dates, years, months, durations; deal sizes,
revenues, valuations, percentages, share prices, any company or market figure; cities, countries, regions, markets; case, contract or company registration numbers.
  DATE_OF_BIRTH  only a person's stated birth date or birth year.
  FINANCIAL      a person's own finances: a personal guarantee, a pledge of their own shares, a loan secured on their own assets, personal debt, salary, personal bank details.
  FAMILY         a statement about a person's relatives (spouse, children, parents, siblings, in-laws).
  ADDRESS        a street or building address, not a city or country.
  NATIONALITY    a person's stated nationality or ethnicity, not a demonym describing a market.
  ID_NUMBER      a person's passport, national ID or licence number.
Every surface form of a known person's name is its own PERSON mention: honorific forms (Sera-san), initials (N. Kamel), first name alone, nicknames."""
# measured 13 Sep: gpt-5-mini needs STRICT (without it every year and amount is redacted); Opus loses FINANCIAL/FAMILY recall with it. Parameter: detect.strict_types.

SYSTEM = f"""You extract structured information from interview transcripts and intelligence reports for a strategic-intelligence firm.
Identify the interviewed SOURCE (the person being interviewed), the CONTRIBUTOR (the interviewer), and every other person named.
List every span of personal or identifying information exactly as written (names, nicknames, initials, phones, emails, addresses, dates of birth,
nationalities, family details, personal financial details, ID numbers), one mention per distinct surface string. Organisations are listed too (type ORG).
Be exhaustive: missing a name is worse than an extra one. Do not include job titles alone, cities, countries, or the project codename.
{STRICT if PARAMS["detect"].get("strict_types") else ""}
Classify every PERSON by role. The firm keeps professional descriptors (title, organisation, relationship) and removes names; it never keeps
anything about private individuals. So:
- source / contributor: as above. Every surface form of their name (first name, nickname, initials) gets the same role.
- third_party: a named person described in a professional capacity (a job title, an organisation, or a professional relationship to the source such
  as former boss, colleague, counterpart at a regulator, executive of the company discussed). Fill title, organisation, relationship from the text.
- private: family members, friends, assistants, drivers, anyone named in a personal context (health, home, family), and anyone you cannot place
  professionally. When unsure, choose private.
Region labels: {"; ".join(REGIONS)}. Sector labels: {"; ".join(SECTORS)}.
source_descriptor_public must not name the organisation or a unique title: say the kind of organisation and level, e.g. 'former senior compliance executive at a Gulf bank'."""


def presidio_spans(text: str) -> list[dict]:
    res = analyzer().analyze(text=text, language="en", score_threshold=PARAMS["detect"]["presidio_threshold"])
    out = []
    for r in res:
        t = PRESIDIO_MAP.get(r.entity_type)
        if t:
            out.append(dict(start=r.start, end=r.end, type=t, text=text[r.start:r.end], detector="presidio", confidence=round(float(r.score), 3),
                            role="n/a", title="", organisation="", relationship=""))
    return out


def claude_extract(text: str) -> Extraction:
    ex = complete(f"<document>\n{text}\n</document>\nExtract per the schema.", system=SYSTEM, purpose="detect.extract", schema=Extraction)
    for k in ("project", "interview_date", "contributor_name", "source_name", "source_role", "source_org", "source_region", "source_country", "source_sector"):
        if not (getattr(ex, k) or "").strip(): setattr(ex, k, None)   # '' means unknown on the wire; None in the tables
    if ex.source_region not in REGIONS: ex.source_region = _closest(ex.source_region, REGIONS)
    if ex.source_sector not in SECTORS: ex.source_sector = _closest(ex.source_sector, SECTORS)
    types, roles = set(TYPES.__args__), set(ROLES.__args__)
    ex.mentions = [m for m in ex.mentions if m.text.strip()]
    for m in ex.mentions:   # the model gets plain strings (schema complexity limit); values are normalised here
        m.type = m.type.upper().replace(" ", "_"); m.role = m.role.lower().replace(" ", "_")
        if m.type not in types: m.type = {"ORGANISATION": "ORG", "ORGANIZATION": "ORG", "TELEPHONE": "PHONE", "DOB": "DATE_OF_BIRTH"}.get(m.type, "PERSON" if m.role in roles - {"n/a"} else "ORG")
        if m.role not in roles: m.role = "private" if m.type == "PERSON" else "n/a"
        if m.type == "PERSON" and m.role == "n/a": m.role = "private"
    if ex.doc_kind not in ("transcript", "deliverable", "other"): ex.doc_kind = "other"
    return ex


def _closest(v, options):
    if not v: return None
    from rapidfuzz import process, fuzz
    best = process.extractOne(v, options, scorer=fuzz.partial_token_set_ratio)
    return best[0] if best and best[1] >= 70 else None


LABEL = re.compile(r"^(source|contributor|interviewee|respondent)\s+[a-z]\d?$", re.I)   # 'Source A' in a deliverable is already anonymous


def claude_spans(text: str, ex: Extraction) -> list[dict]:
    out = []
    for m in ex.mentions:
        if LABEL.match(m.text.strip()): continue
        for hit in re.finditer(re.escape(m.text), text):
            out.append(dict(start=hit.start(), end=hit.end(), type=m.type, text=m.text, detector="claude", confidence=m.confidence, role=m.role,
                            title=m.title, organisation=m.organisation, relationship=m.relationship))
    return out


def split_around_people(spans: list[dict]) -> list[dict]:
    """A FAMILY, FINANCIAL or ADDRESS clause that contains a person's name is cut around the name, so the name stays a PERSON
    mention (an entity, a stable token) and the rest of the clause keeps its own type. Without this the merge below would
    swallow the clause as one PERSON span: the vault would hold a sentence as someone's name and the clause's type is lost."""
    people = [p for p in spans if p["type"] == "PERSON"]
    out = []
    for s in spans:
        inside = sorted([p for p in people if s["type"] != "PERSON" and p["start"] >= s["start"] and p["end"] <= s["end"] and (p["start"], p["end"]) != (s["start"], s["end"])], key=lambda p: p["start"])
        if not inside:
            out.append(s); continue
        pos = s["start"]
        for p in inside:
            if p["start"] > pos: out.append({**s, "start": pos, "end": p["start"]})
            pos = max(pos, p["end"])
        if pos < s["end"]: out.append({**s, "start": pos, "end": s["end"]})
    return out


def union(spans: list[dict]) -> list[dict]:
    """Merge overlapping spans; keep the widest, the higher confidence, and note which detectors agreed."""
    spans = sorted(split_around_people(spans), key=lambda s: (s["start"], -s["end"]))
    merged = []
    for s in spans:
        if merged and s["start"] < merged[-1]["end"]:
            m = merged[-1]
            m["end"] = max(m["end"], s["end"]); m["detectors"] |= {s["detector"]}
            if s["confidence"] > m["confidence"] or (s["detector"] == "claude" and m["role"] == "n/a"):
                m.update(type=s["type"], confidence=max(m["confidence"], s["confidence"]), role=s["role"] if s["role"] != "n/a" else m["role"],
                         title=s.get("title") or m.get("title", ""), organisation=s.get("organisation") or m.get("organisation", ""),
                         relationship=s.get("relationship") or m.get("relationship", ""))
        else:
            merged.append({**s, "detectors": {s["detector"]}})
    lo, hi = PARAMS["detect"]["review_band"]
    for m in merged:
        m["text"] = None  # filled by caller with document text
        m["detectors"] = "+".join(sorted(m["detectors"]))
        lone = len(m["detectors"].split("+")) == 1
        # rules-only hits the model did not place are informational, not redacted: NER 'LOCATION'/'NRP' alone is a city or a demonym, and NER 'PERSON'
        # alone on this corpus is a place, a company or a filler word almost every time (measured: recall_by_detector in the redaction eval).
        # The safety net is the gate: Presidio and a second model re-read the retained copy, and a surviving name flags the document for a person.
        informational = m["detectors"] == "presidio" and m["type"] in ("ADDRESS", "NATIONALITY", "PERSON")
        m["informational"] = informational
        m["needs_review"] = bool(lone and lo <= m["confidence"] <= hi and not informational)
        m["entity_hint"] = ", ".join(filter(None, [m.get("title", ""), m.get("organisation", "")]))
    return merged


def fallback_extraction(doc: dict) -> Extraction:
    """Degraded mode (no model available): filename + header only. The pipeline still runs; the Evals page shows the recall gap."""
    g = lambda k: doc.get(k) if isinstance(doc.get(k), str) else ""
    return Extraction(doc_kind=g("doc_type") or "other", project=g("project"), interview_date=g("doc_date"), contributor_name=g("contributor_name"),
                      source_name=g("source_name_from_filename"), source_role="", source_org="", source_former_orgs=[], source_region="", source_country="",
                      source_sector="", source_seniority="unknown", source_descriptor_public="", topics=[], summary="", mentions=[])


def clip_newlines(spans: list[dict]) -> list[dict]:
    """A span never crosses a line: NER sometimes runs a name into the next header line ('Priya Raghunathan\nDate')."""
    for s in spans:
        if "\n" in s["text"]:
            s["end"] = s["start"] + s["text"].index("\n"); s["text"] = s["text"][: s["text"].index("\n")]
    return [s for s in spans if s["text"].strip()]


def detect_document(doc: dict, use_presidio: bool = True) -> tuple[list[dict], dict]:
    text = doc["text"]
    degraded = False
    try:
        ex = claude_extract(text)
    except Exception as e:  # no key, network down, refusal, content filter: degrade, never stop the pipeline
        import logging; logging.getLogger(__name__).warning("model extraction unavailable for %s: %s", doc["doc_id"], str(e)[:300])
        ex = fallback_extraction(doc); degraded = "the provider's content filter blocked the document" if "content_filter" in str(e) else "model unavailable"
    spans = union((presidio_spans(text) if use_presidio else []) + claude_spans(text, ex))
    for s in spans:
        s["text"] = text[s["start"]:s["end"]]; s["doc_id"] = doc["doc_id"]
    spans = clip_newlines(spans)
    meta = ex.model_dump(exclude={"mentions"}); meta["doc_id"] = doc["doc_id"]; meta["sha256"] = doc.get("sha256"); meta["degraded"] = bool(degraded); meta["degraded_reason"] = degraded or ""
    return spans, meta


def detect_all(docs: pd.DataFrame, old_spans: pd.DataFrame | None = None, old_meta: pd.DataFrame | None = None, workers: int = 6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Incremental: a document whose text hash matches the stored metadata keeps its spans and metadata from the last run, so a
    scheduled run costs nothing unless files changed. ATLAS_FORCE_DETECT=1 re-extracts everything (a prompt or model change)."""
    from concurrent.futures import ThreadPoolExecutor
    reuse = set()
    if old_meta is not None and old_spans is not None and "sha256" in old_meta.columns and not os.environ.get("ATLAS_FORCE_DETECT"):
        known = dict(zip(old_meta.doc_id, old_meta.sha256))
        reuse = {d for d, h in zip(docs.doc_id, docs.sha256) if h and known.get(d) == h}
    todo = docs[~docs.doc_id.isin(reuse)]
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(detect_document, todo.to_dict("records")))
    spans = pd.concat([old_spans[old_spans.doc_id.isin(reuse)] if reuse else pd.DataFrame(), pd.DataFrame([s for sp, _ in results for s in sp])], ignore_index=True)
    meta = pd.concat([old_meta[old_meta.doc_id.isin(reuse)] if reuse else pd.DataFrame(), pd.DataFrame([m for _, m in results])], ignore_index=True)
    if reuse: logging.getLogger(__name__).info("detect: reused %d unchanged documents, extracted %d", len(reuse), len(todo))
    return spans, meta
