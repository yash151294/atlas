"""Silver step 4: quality gate. At 15,000 documents nobody approves each one by hand, so the
retained copy is checked, scored and tiered:
  residual scan   every value the detectors found must be gone from the retained text; then an
                  independent pass (rules engine, and optionally a second model) reads the retained
                  text looking for any name or contact detail that survived
  risk score      residual hits, unresolved identity questions, review-band spans, sensitive types,
                  a transcript with no identified source, a degraded (model-less) extraction
  tier            auto  : below the threshold -> approved by policy, raw deletion clock starts
                  review: above it            -> human queue with the reasons listed"""
import json, re
import pandas as pd
from pydantic import BaseModel, Field
from ..config import PARAMS
from ..llm import complete

TOKEN = re.compile(r"\[(?:PERSON|ORG|PHONE|EMAIL|ADDRESS|FINANCIAL|FAMILY|NATIONALITY|DATE_OF_BIRTH|ID_NUMBER)_\d{4}\]")
CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s().-]{8,}\d")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$|^(19|20)\d{2}$")


def plausible(h: dict) -> bool:
    """A residual hit must be something a person could be identified by: not a token, not a date, a phone with enough digits."""
    t = h["text"].strip()
    if not t or TOKEN.fullmatch(t) or DATE.match(t): return False
    if h["kind"] in ("contact_pattern", "model_contact") and "@" not in t and sum(c.isdigit() for c in t) < 9: return False
    if h["kind"] in ("model_name", "ner_person") and (not any(c.isalpha() for c in t) or POSSESSIVE.match(t)): return False   # 'my uncle' identifies nobody
    return True


SPECIAL = ["health", "political opinion", "religion or belief", "ethnic or racial origin", "sexual orientation or sex life", "trade union membership",
           "genetic or biometric", "criminal allegation or conviction"]   # UK GDPR Art. 9 categories plus Art. 10 offence data


class Residual(BaseModel):
    names: list[str] = Field(default_factory=list, description="personal names (of people, not organisations or places) still present verbatim in the text")
    contact_details: list[str] = Field(default_factory=list, description="phone numbers, emails, home addresses, ID numbers still present verbatim")
    special_category: list[str] = Field(default_factory=list, description=f"which of these kinds of information the text states about an identifiable person (a token counts as identifiable): {'; '.join(SPECIAL)}. Empty if none. Use the labels exactly.")


RESIDUAL_SYSTEM = """You audit a pseudonymised interview transcript. Names have been replaced by tokens like [PERSON_0012].
List any personal name of a human being that is still present verbatim (first names, surnames, nicknames, initials), and any phone number, email,
home address or ID number. Organisations, places, job titles and project codenames are not personal data: do not list them. Return exact strings only.
Separately, say whether the text states special-category information (health, political opinion, religion, ethnic origin, sexual orientation, trade union
membership, genetic or biometric data) or a criminal allegation or conviction about any person, including people shown as tokens."""


POSSESSIVE = re.compile(r"^(my|his|her|their|our|the)\s", re.I)


def residual_rules(redacted_text: str, values: list[str], presidio=None, orgs: list[str] = ()) -> list[dict]:
    hits = []
    clean = TOKEN.sub(" ", redacted_text)
    low = clean.lower()
    orgs_l = [o.lower() for o in orgs if o]
    IDENTIFYING = {"PERSON", "PHONE", "EMAIL", "ID_NUMBER", "DATE_OF_BIRTH"}   # an address leaking in a second spelling is the model's job (model_contact)
    for v in values:                                   # leakage: a detected identifier that is still there (e.g. a second spelling the span missed)
        v, t = (v if isinstance(v, tuple) else (v, "PERSON"))
        v = v.strip()
        if t in IDENTIFYING and len(v) > 3 and re.search(r"(?<![\w])" + re.escape(v.lower()) + r"(?![\w])", low):   # whole words: 'Hotel' inside 'Hotel Palacio' is not a leak
            hits.append(dict(kind="leaked_value", text=v))
    for m in CONTACT.finditer(clean):
        hits.append(dict(kind="contact_pattern", text=m.group(0).strip()))
    if presidio is not None:
        for r in presidio.analyze(text=clean, language="en", entities=["PERSON"], score_threshold=PARAMS["qa"]["presidio_threshold"]):
            t = " ".join(clean[r.start:r.end].split())
            if len(t.split()) >= 2 and t[0].isupper() and not any(t.lower() in o or o in t.lower() for o in orgs_l):   # two capitalised words the NER calls a person and no organisation knows
                hits.append(dict(kind="ner_person", text=t, score=round(float(r.score), 2), informational=True))   # shown, not scored: NER alone on this corpus is a street, a company or a place (measured); the second model scores names
    seen, out = set(), []
    for h in hits:
        if not plausible(h): continue
        if h["text"].lower() not in seen:
            seen.add(h["text"].lower()); out.append(h)
    return out


def residual_model(redacted_text: str) -> tuple[list[dict], list[str]]:
    """Residual identifiers, and which special-category / offence kinds the retained copy states about a person."""
    r = complete(f"<text>\n{redacted_text}\n</text>", system=RESIDUAL_SYSTEM, purpose="qa.residual", schema=Residual, effort="low", max_tokens=3000)   # reasoning tokens count against the cap on gpt-5 models
    hits = [h for h in [dict(kind="model_name", text=n) for n in r.names if n.lower() in redacted_text.lower()] +
            [dict(kind="model_contact", text=c) for c in r.contact_details if c in redacted_text] if plausible(h)]
    return hits, sorted({c for c in r.special_category if c in SPECIAL})


def score(doc: dict, hits: list[dict], meta: dict, queue_n: int) -> tuple[float, list[str]]:
    w = PARAMS["qa"]["weights"]; s, why = 0.0, []
    scored = [h for h in hits if not h.get("informational")]
    if scored:
        s += w["residual"] * min(len(scored), 3); why.append(f"{len(scored)} possible residual identifier(s) in the retained text")
    if doc.get("n_review", 0):
        s += w["review_span"] * min(doc["n_review"], 5); why.append(f"{doc['n_review']} span(s) found by one detector only at middling confidence")
    if queue_n:
        s += w["identity_queue"] * min(queue_n, 3); why.append(f"{queue_n} unresolved identity question(s)")
    if doc.get("sensitive"):
        s += w["sensitive"]; why.append("contains ID number or date of birth")
    if doc.get("special_category"):
        s += w["special_category"]; why.append(f"special-category content ({', '.join(doc['special_category'])}): an Art. 9/10 condition must cover retention")
    if doc["doc_type"] == "transcript" and not doc.get("source_token"):
        s += w["no_source"]; why.append("transcript with no identified source")
    if meta.get("degraded"):
        s += w["degraded"]; why.append(f"extraction ran without the model: {meta.get('degraded_reason') or 'rules only'}")
    return round(min(s, 1.0), 3), why


def gate(redacted: pd.DataFrame, vault: pd.DataFrame, meta: pd.DataFrame, queue: pd.DataFrame, use_presidio: bool = True, use_model: bool | None = None) -> pd.DataFrame:
    p = PARAMS["qa"]; use_model = p["residual_model_check"] if use_model is None else use_model
    presidio = None
    if use_presidio:
        from .detect import analyzer; presidio = analyzer()
    M = {m["doc_id"]: m for m in meta.astype(object).where(pd.notnull(meta), None).to_dict("records")} if len(meta) else {}
    orgs = []
    try:
        from .. import store
        E = store.read("silver_entities"); orgs = E[E.type == "ORG"].canonical.dropna().tolist() if E is not None else []
    except Exception: pass
    def one(d):
        vals = vault[vault.doc_id == d["doc_id"]]
        hits = residual_rules(d["redacted_text"], list(zip(vals.value, vals.type)), presidio, orgs)
        cats = []
        if use_model:
            try:
                more, cats = residual_model(d["redacted_text"]); low = d["redacted_text"].lower()
                hits += [h for h in more if not (h["kind"] == "model_name" and any(h["text"].lower() in o.lower() and o.lower() in low for o in orgs))]   # 'Kurosawa' inside 'Kurosawa Heavy Industries' is a company, not a person
            except Exception as e:
                import logging; logging.getLogger(__name__).warning("residual model check unavailable for %s: %s", d["doc_id"], e)
        seen, uniq = {}, []
        for h in hits:   # the rules and the model often agree; one row per string, and a string the model also named is no longer informational
            k = " ".join(h["text"].split()).lower()
            if k in seen:
                if not h.get("informational"): seen[k].pop("informational", None)
                continue
            seen[k] = h; h["text"] = " ".join(h["text"].split()) if h["kind"] != "leaked_value" else h["text"]; uniq.append(h)
        hits = uniq
        d["sensitive"] = bool(vals.type.isin(["ID_NUMBER", "DATE_OF_BIRTH"]).any()); d["special_category"] = cats
        qn = int(((queue.doc_id == d["doc_id"]) & (queue.status == "pending")).sum()) if len(queue) else 0
        risk, why = score(d, hits, M.get(d["doc_id"], {}), qn)
        tier = "auto" if risk < p["auto_approve_below"] else "review"
        return dict(doc_id=d["doc_id"], risk_score=risk, risk_reasons=json.dumps(why), residual_hits=json.dumps(hits), special_category=json.dumps(cats), tier=tier)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as ex:   # ponytail: Presidio's analyzer is used from several threads; fine for a demo corpus
        rows = list(ex.map(one, redacted.to_dict("records")))
    return pd.DataFrame(rows)


def apply(redacted: pd.DataFrame, qa: pd.DataFrame) -> pd.DataFrame:
    """Merge the gate result into the retained table and auto-approve the low-risk tier."""
    from .redact import approve
    red = redacted.drop(columns=[c for c in ["risk_score", "risk_reasons", "residual_hits", "special_category", "tier"] if c in redacted.columns])
    red = red.merge(qa, on="doc_id", how="left")
    if "approval_mode" not in red.columns: red["approval_mode"] = None
    policy = f'policy:auto-approve<{PARAMS["qa"]["auto_approve_below"]}'
    human = red.approval_mode == "human"                       # a person's approval stands; the policy re-decides everything else on every run
    red.loc[~human, ["approved", "approved_by", "approved_at", "raw_delete_after", "approval_mode"]] = [False, None, None, None, None]
    for doc_id in red[(red.tier == "auto") & ~human].doc_id:
        red = approve(red, doc_id, policy, mode="auto")
    red["approved"] = red.approved.astype(bool)
    return red
