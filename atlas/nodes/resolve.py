"""Silver step 2: identity resolution. Three tiers, in this order:
1. deterministic keys (Salesforce contact for contributors; normalised name + organisation for sources)
2. scored fuzzy match (rapidfuzz) - auto-link only above the auto_merge score AND with organisation agreement
3. everything in the review band goes to a human queue; PERSON entities are never merged on fuzzy evidence alone.
Who becomes an entity is decided by role: sources, contributors and professionally described third parties do;
private individuals never do (they get a per-document token and are redacted). A name the rules engine found
but the model did not place professionally is treated as private: recall-first, and it keeps the graph clean.
Every mention keeps its provenance (doc, offsets), so a wrong merge can be undone."""
import re, unicodedata
import pandas as pd
from rapidfuzz import fuzz, process
from ..config import PARAMS

def aslist(x):
    """parquet round-trips lists as numpy arrays; make them plain lists."""
    return [] if x is None or (hasattr(x, "__len__") and len(x) == 0) else list(x)


TITLES = re.compile(r"^(dr|mr|mrs|ms|prof|sir|sheikh)\.?\s+", re.I)
HONORIFICS = re.compile(r"[-\s](san|sama|sensei|ji|bhai)\b\.?$", re.I)


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = HONORIFICS.sub("", TITLES.sub("", re.sub(r"['\u2019]s$", "", s.strip().lower().rstrip(".,;:"))))
    return re.sub(r"[^a-z0-9 ]", " ", s).strip()


def name_score(a: str, b: str) -> float:
    """token_set_ratio plus an initial-vs-forename rule ('F. Haddad' vs 'Farid Haddad' -> review band)."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    s = fuzz.token_set_ratio(a, b)
    ta, tb = a.split(), b.split()
    if ta and tb and ta[-1] == tb[-1] and (len(ta[0]) == 1 or len(tb[0]) == 1) and ta[0][0] == tb[0][0]:
        s = max(s, 85.0)
    elif len(ta) > 1 and len(tb) > 1:
        fore_ok = ta[0] == tb[0] or ta[0].startswith(tb[0]) or tb[0].startswith(ta[0]) or fuzz.ratio(ta[0], tb[0]) >= 70
        sur_ok = ta[-1] == tb[-1] or fuzz.ratio(ta[-1], tb[-1]) >= 85
        if not (fore_ok and sur_ok):
            s = min(s, 60.0)   # 'Daisuke Kurosawa' vs 'Takeshi Kurosawa', 'Hiroshi Nakamura' vs 'Hiroshi Nakagawa': a shared token is not a variant
    return float(s)


def private_id(doc_id: str, text: str) -> str:
    return f"PRIV:{doc_id}:{norm(text)}"


class Resolver:
    def __init__(self, contributors: pd.DataFrame):
        self.p = PARAMS["resolve"]
        self.entities: dict[str, dict] = {}
        self.mentions: list[dict] = []
        self.review: list[dict] = []
        self._queued: set[tuple[str, str]] = set()
        self._n = {"PERSON": 0, "ORG": 0}
        for _, c in contributors.iterrows():
            self._add("PERSON", c["Full_Name"], kind="contributor", role="", org="", region=c.get("Base_City__c", ""),
                      ext_id=str(c["Contact_Id"]), topics=c.get("Access_Keywords__c", ""), status=str(c.get("Status__c") or "").strip())

    def _add(self, etype, name, **kw):
        self._n[etype] += 1
        eid = f"{'P' if etype == 'PERSON' else 'O'}{self._n[etype]:04d}"
        self.entities[eid] = dict(entity_id=eid, type=etype, canonical=name, norm=norm(name), aliases=set(),
                                  kind=kw.get("kind", "third_party"), role=kw.get("role", ""), org=kw.get("org", ""),
                                  region=kw.get("region", ""), topics=kw.get("topics", ""), ext_id=kw.get("ext_id", ""),
                                  status=kw.get("status", ""),   # contributors only: Salesforce Status__c, so a leaver is marked rather than implied to be reachable
                                  relationship=kw.get("relationship", ""), mentioned_by=set(),
                                  region_norm=None, country=None, sector=None, seniority=None, descriptor_public="",
                                  first_seen=None, last_seen=None, docs=set())
        return eid

    def _touch(self, eid, doc_id, date, alias, decision: str = ""):
        """An alias is what a later document matches on exactly, so a provisional link never contributes one: 'Tadashi Kurosawa',
        linked to Takeshi in the review band, became an alias and every later mention then matched exactly, hardening a guess
        into a deterministic merge no rejection could unpick (ADR 45)."""
        e = self.entities[eid]; e["docs"].add(doc_id)
        if decision == "provisional": alias = None
        if alias and norm(alias) != e["norm"]:
            e["aliases"].add(alias)
        if date:
            e["first_seen"] = min(filter(None, [e["first_seen"], date])); e["last_seen"] = max(filter(None, [e["last_seen"], date]))

    def _candidates(self, etype, kinds=None):
        return [e for e in self.entities.values() if e["type"] == etype and (kinds is None or e["kind"] in kinds)]

    def exact_person(self, name, kinds=None):
        n = norm(name)
        for e in self._candidates("PERSON", kinds):
            if e["norm"] == n or n in {norm(a) for a in e["aliases"]}:
                return e["entity_id"]
        return None

    def org_agrees(self, org_hint: str, org: str) -> bool | None:
        """True, False, or None when either side names no organisation: unknown is not a disagreement."""
        if not org_hint or not org:
            return None
        return fuzz.token_set_ratio(norm(org_hint), norm(org)) >= 80

    def queue(self, doc_id, e, mention, score, org_ok, reason):
        """A link a person should look at, one row per document and mention: a name recurs in a document and the queue is a question, not a count."""
        if (doc_id, mention) in self._queued:
            return
        self._queued.add((doc_id, mention))
        self.review.append(dict(doc_id=doc_id, mention=mention, candidate_entity=e["entity_id"], candidate_name=e["canonical"],
                                score=round(score, 1), org_agrees=bool(org_ok), status="pending", reason=reason))

    def match_person(self, name, org_hint, kinds, doc_id, allow_fuzzy_link, deterministic: bool = False):
        """Return (entity_id, decision). Deterministic on norm name; fuzzy -> review queue.
        deterministic: the name came from the file name or Salesforce (tier 1), so a model-read organisation does not question it."""
        n = norm(name)
        eid = self.exact_person(name, kinds)
        if eid:
            e = self.entities[eid]
            if not deterministic and self.org_agrees(org_hint, e["org"]) is False:
                # the same name at an organisation that disagrees is the one way two people became one node in silence: keep the link,
                # because splitting breaks the cases where a person changed employer or the stated organisation is a description
                # ('external consultant to PayNaira' against 'PayNaira Ltd'), and put the question to a person (ADR 45)
                self.queue(doc_id, e, name, 100.0, False, f'same name, but this document says {org_hint} and the record says {e["org"]}')
            return eid, "exact"
        cands = self._candidates("PERSON", kinds)
        if cands:
            best = max(cands, key=lambda e: name_score(name, e["canonical"]))
            s = name_score(name, best["canonical"])
            org_ok = self.org_agrees(org_hint, best["org"]) is True
            if s >= self.p["auto_merge_score"] and (org_ok or allow_fuzzy_link):
                return best["entity_id"], "auto"
            if s >= self.p["review_score"]:
                bare = len(n.split()) == 1
                self.queue(doc_id, best, name, s, org_ok,
                           "bare first name or surname; never linked without a person" if bare else
                           ("initial/forename or spelling variant" if s < self.p["auto_merge_score"] else "name match, organisation unconfirmed"))
                if bare:
                    return None, "new"                    # a bare first name is never linked, even provisionally
                return best["entity_id"], "provisional"   # linked provisionally, flagged for a human
        return None, "new"

    def same_doc_role(self, sp, doc_id):
        """A nickname or surname with the same title and organisation as a professional already seen in this document is that person."""
        t, o, n = norm(sp.get("title", "")), norm(sp.get("organisation", "")), norm(sp["text"])
        if not o: return None, None
        for e in self._candidates("PERSON", {"third_party", "source"}):
            if doc_id not in e["docs"] or norm(e["org"]) != o: continue
            et = norm(e["role"]); toks = set(e["norm"].split()); one = len(n.split()) == 1
            surname = one and n in toks                      # 'Sera-san', 'Ferrão': one token of a name already seen at the same organisation
            nickname = one and bool(t) and et == t           # 'Tavinho', founder of Aurora = the founder of Aurora already seen
            variant = not one and bool(t) and et == t and name_score(sp["text"], e["canonical"]) >= self.p["review_score"]
            if surname or nickname or variant:               # a different full name with the same title is a different person (a successor, a co-founder)
                return e["entity_id"], "same_doc_role"
        return None, None

    def same_doc_name(self, tok: str, doc_id: str, *also):
        """'Ogata' where Masaru Ogata is the only Ogata in this document, 'Farid' where the source is Farid Haddad: a single name token
        that belongs to exactly one person already seen in this document (or its source or contributor) is that person. Two people
        sharing the token (three Kurosawas) is ambiguous and stays unlinked."""
        hits = {e["entity_id"] for e in self.entities.values() if e["type"] == "PERSON" and (doc_id in e["docs"] or e["entity_id"] in also) and tok in e["norm"].split()}
        return next(iter(hits)) if len(hits) == 1 else None

    def match_org(self, name):
        n = norm(name)
        cands = self._candidates("ORG")
        for e in cands:
            if e["norm"] == n:
                return e["entity_id"]
        if cands:
            best, s, _ = process.extractOne(n, [e["norm"] for e in cands], scorer=fuzz.token_set_ratio)
            if s >= self.p["auto_merge_score"]:
                return cands[[e["norm"] for e in cands].index(best)]["entity_id"]
        return self._add("ORG", name, kind="org")

    def resolve_doc(self, doc: dict, meta: dict, spans: list[dict]):
        doc_id, date = doc["doc_id"], (doc.get("doc_date") or meta.get("interview_date"))
        # contributor: Salesforce is the system of record
        contrib_eid = None
        cname = doc.get("contributor_name") or meta.get("contributor_name")
        if cname:
            contrib_eid, dec = self.match_person(cname, "", {"contributor"}, doc_id, allow_fuzzy_link=True, deterministic=True)
            if contrib_eid is None:
                contrib_eid = self._add("PERSON", cname, kind="contributor")
            self._touch(contrib_eid, doc_id, date, cname, dec)
        # source: filename beats the model; organisation agreement gates auto-links
        source_eid = None
        sname = doc.get("source_name_from_filename") or meta.get("source_name")
        if sname:
            org_eid = self.match_org(meta["source_org"]) if meta.get("source_org") else None
            source_eid, dec = self.match_person(sname, meta.get("source_org", ""), {"source", "third_party"}, doc_id, allow_fuzzy_link=False, deterministic=bool(doc.get("source_name_from_filename")))
            if source_eid is None:
                source_eid = self._add("PERSON", sname, kind="source", role=meta.get("source_role", ""), org=meta.get("source_org", ""),
                                       region=meta.get("source_region", ""), topics="; ".join(aslist(meta.get("topics"))))
            e = self.entities[source_eid]; e["kind"] = "source"
            e["role"] = e["role"] or meta.get("source_role", ""); e["org"] = e["org"] or meta.get("source_org", "")
            e["region"] = e["region"] or meta.get("source_region", "")
            for k in ("region_norm", "country", "sector", "seniority"):
                src = {"region_norm": "source_region"}.get(k, f"source_{k}")
                e[k] = e[k] or meta.get(src)
            e["descriptor_public"] = e["descriptor_public"] or meta.get("source_descriptor_public", "")
            e["topics"] = "; ".join(sorted(set(filter(None, e["topics"].split("; "))) | set(aslist(meta.get("topics")))))
            e["org_entity"] = org_eid; e.setdefault("former_orgs", set()).update(aslist(meta.get("source_former_orgs")))
            self._touch(source_eid, doc_id, date, sname, dec)
        # every span -> an entity (people with a professional role, orgs), a private token, or a PII bucket
        for sp in spans:
            eid, dec, role = None, "pii", sp.get("role", "n/a")
            if sp["type"] == "PERSON":
                if role == "source" and source_eid:
                    eid, dec = source_eid, "role:source"
                elif role == "contributor" and contrib_eid:
                    eid, dec = contrib_eid, "role:contributor"
                elif role == "third_party":
                    eid, dec = self.same_doc_role(sp, doc_id)   # 'Tavinho' = founder, Aurora = the founder of Aurora already seen in this document
                    if eid is None:
                        eid, dec = self.match_person(sp["text"], sp.get("organisation", ""), None, doc_id, allow_fuzzy_link=False)
                    if eid is None:
                        eid = self._add("PERSON", sp["text"], kind="third_party", role=sp.get("title", ""), org=sp.get("organisation", ""),
                                        relationship=sp.get("relationship", ""))
                    e = self.entities[eid]
                    e["role"] = e["role"] or sp.get("title", ""); e["org"] = e["org"] or sp.get("organisation", "")
                    e["relationship"] = e["relationship"] or sp.get("relationship", "")
                    if source_eid and eid != source_eid: e["mentioned_by"].add(source_eid)
                else:  # private, or a rules-only hit the model did not place professionally
                    known, how = self.exact_person(sp["text"]), "exact"   # a known person's name in a new spelling context still links
                    if not known and len(norm(sp["text"]).split()) == 1:
                        known, how = self.same_doc_name(norm(sp["text"]), doc_id, *filter(None, [source_eid, contrib_eid])), "same_doc_name"
                    if not known and sp["detectors"] == "presidio" and role == "n/a" and sp.get("informational", True):
                        eid, dec, role = None, "informational", "n/a"   # not redacted, not an entity: NER alone; the gate re-reads the retained copy
                        self.mentions.append(dict(doc_id=doc_id, start=sp["start"], end=sp["end"], type=sp["type"], text=sp["text"], role=role, entity_id=eid,
                                                  decision=dec, detectors=sp["detectors"], confidence=sp["confidence"], needs_review=sp["needs_review"]))
                        continue
                    if known:
                        eid, dec = known, how
                        if how == "same_doc_name": role = self.entities[known]["kind"]
                    elif role != "private" and len(norm(sp["text"]).split()) > 1:   # 'F. Haddad': a variant of someone known goes to the queue, never becomes a node
                        eid, dec = self.match_person(sp["text"], "", None, doc_id, allow_fuzzy_link=False)
                    if not eid:
                        eid, dec, role = private_id(doc_id, sp["text"]), "private", "private"
                if not eid.startswith("PRIV:"):
                    self._touch(eid, doc_id, date, sp["text"], dec)
            elif sp["type"] == "ORG":
                eid, dec = self.match_org(sp["text"]), "org"
                self._touch(eid, doc_id, date, sp["text"])
            self.mentions.append(dict(doc_id=doc_id, start=sp["start"], end=sp["end"], type=sp["type"], text=sp["text"], role=role,
                                      entity_id=eid, decision=dec, detectors=sp["detectors"], confidence=sp["confidence"], needs_review=sp["needs_review"]))
        for mn in [x for x in self.mentions if x["doc_id"] == doc_id and x["decision"] == "informational" and len(norm(x["text"]).split()) == 1]:   # a surname seen before the full name
            eid = self.same_doc_name(norm(mn["text"]), doc_id, *filter(None, [source_eid, contrib_eid]))
            if eid: mn.update(entity_id=eid, decision="same_doc_name", role=self.entities[eid]["kind"]); self._touch(eid, doc_id, date, mn["text"])
        return contrib_eid, source_eid

    def tables(self):
        ents = pd.DataFrame([{**e, "aliases": "; ".join(sorted(e["aliases"])), "docs": "; ".join(sorted(e["docs"])), "n_docs": len(e["docs"]),
                              "former_orgs": "; ".join(sorted(e.get("former_orgs", set()))), "mentioned_by": "; ".join(sorted(e["mentioned_by"]))}
                             for e in self.entities.values()])
        return ents, pd.DataFrame(self.mentions), pd.DataFrame(self.review)


def resolve_all(docs: pd.DataFrame, meta: pd.DataFrame, spans: pd.DataFrame, contributors: pd.DataFrame):
    r = Resolver(contributors)
    clean = lambda df: df.astype(object).where(pd.notnull(df), None)   # parquet nulls arrive as NaN floats
    docs, meta = clean(docs), clean(meta)
    m_by = {m["doc_id"]: m for m in meta.to_dict("records")}
    links = []
    for d in docs.to_dict("records"):
        sp = spans[spans.doc_id == d["doc_id"]].sort_values("start").to_dict("records")
        c, s = r.resolve_doc(d, m_by.get(d["doc_id"], {}), sp)
        links.append(dict(doc_id=d["doc_id"], contributor_entity=c, source_entity=s))
    ents, mentions, review = r.tables()
    return ents, mentions, review, pd.DataFrame(links)
