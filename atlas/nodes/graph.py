"""Gold: the network as tables. Nodes = contributors, sources, mentioned professionals, organisations,
topics. Edges carry provenance (which document, which offsets, when). Relational on purpose: the
questions an analyst asks are 1 to 3 hops and a recursive CTE covers that. The table is already a property graph
(src, dst, type, properties), so Graph in Fabric (GQL over these same OneLake tables) is the upgrade when
path depth or graph algorithms earn it; a separate graph database would be a second system to secure.
Edge types: INTERVIEWED (contributor -> source, one per transcript), WORKS_AT / FORMERLY_AT (source -> org),
HAS_ACCESS_TO (source -> topic), MENTIONED (source -> professional they named: second-degree reach)."""
import json
import pandas as pd


def _s(x):
    return x if isinstance(x, str) else ""


def build_graph(entities: pd.DataFrame, mentions: pd.DataFrame, links: pd.DataFrame, docs: pd.DataFrame, redacted: pd.DataFrame):
    doc = docs.set_index("doc_id"); tok = dict(zip(redacted.doc_id, redacted.source_token))
    E = entities.astype(object).where(pd.notnull(entities), None)
    ents = E.set_index("entity_id")
    # a person's token is the same in every document (redact keys tokens by entity), so read it back from the vault-free retained table
    person_tok = {}
    for pj in redacted.people.fillna("[]"):
        for p in json.loads(pj):
            person_tok.setdefault(p["entity_id"], p["token"])
    edges = []
    for l in links.dropna(subset=["contributor_entity", "source_entity"]).to_dict("records"):
        d = doc.loc[l["doc_id"]]
        m = mentions[(mentions.doc_id == l["doc_id"]) & (mentions.entity_id == l["source_entity"])].sort_values("start")
        edges.append(dict(src=l["contributor_entity"], dst=l["source_entity"], type="INTERVIEWED", doc_id=l["doc_id"], project=d.get("project"),
                          date=d.get("doc_date"), start=int(m.start.iloc[0]) if len(m) else None, end=int(m.end.iloc[0]) if len(m) else None,
                          confidence=float(m.confidence.max()) if len(m) else 1.0))
    src_of_doc = dict(zip(links.doc_id, links.source_entity))
    for eid, e in ents[ents.kind == "third_party"].iterrows():
        for did in filter(None, _s(e.docs).split("; ")):
            se = src_of_doc.get(did)
            if not isinstance(se, str) or se == eid: continue   # deliverables have no source (NaN)
            m = mentions[(mentions.doc_id == did) & (mentions.entity_id == eid)].sort_values("start")
            edges.append(dict(src=se, dst=eid, type="MENTIONED", doc_id=did, project=doc.loc[did].get("project"), date=doc.loc[did].get("doc_date"),
                              start=int(m.start.iloc[0]) if len(m) else None, end=int(m.end.iloc[0]) if len(m) else None,
                              confidence=float(m.confidence.max()) if len(m) else 0.8))
    sources, people = [], []
    for eid, e in ents[ents.kind == "source"].iterrows():
        ivs = [x for x in edges if x["dst"] == eid and x["type"] == "INTERVIEWED"]
        contribs = sorted({x["src"] for x in ivs})
        dids = "; ".join(sorted({x["doc_id"] for x in ivs}))
        for t in filter(None, _s(e.topics).split("; ")):
            edges.append(dict(src=eid, dst=f"topic:{t.strip().lower()}", type="HAS_ACCESS_TO", doc_id=dids, project=None, date=e.last_seen, start=None, end=None, confidence=0.9))
        if e.get("org_entity"):
            edges.append(dict(src=eid, dst=e["org_entity"], type="WORKS_AT", doc_id=dids, project=None, date=e.last_seen, start=None, end=None, confidence=0.9))
        for fo in filter(None, _s(e.get("former_orgs")).split("; ")):
            edges.append(dict(src=eid, dst=f"org:{fo.strip().lower()}", type="FORMERLY_AT", doc_id=dids, project=None, date=e.last_seen, start=None, end=None, confidence=0.8))
        mentioned = sorted({x["dst"] for x in edges if x["src"] == eid and x["type"] == "MENTIONED"})
        sources.append(dict(entity_id=eid, token=person_tok.get(eid) or next((tok[x["doc_id"]] for x in ivs if x["doc_id"] in tok), None),
                            descriptor=", ".join(filter(None, [_s(e.role), _s(e.org)])), descriptor_public=_s(e.descriptor_public), role=_s(e.role), org=_s(e.org),
                            region=_s(e.region), region_norm=_s(e.region_norm) or None, country=_s(e.country) or None, sector=_s(e.sector) or None,
                            seniority=_s(e.seniority) or None, topics=_s(e.topics), former_orgs=_s(e.get("former_orgs")),
                            contributors="; ".join(contribs), n_contributors=len(contribs), n_transcripts=len(ivs),
                            first_seen=e.first_seen, last_seen=e.last_seen, projects="; ".join(sorted({x["project"] for x in ivs if x["project"]})),
                            mentioned="; ".join(mentioned), n_mentioned=len(mentioned), aliases=_s(e.aliases), n_docs=e.n_docs))
    for eid, e in ents[ents.kind == "third_party"].iterrows():
        via = sorted({x["src"] for x in edges if x["dst"] == eid and x["type"] == "MENTIONED"})
        if not via: continue   # named only in a deliverable with no source link: not reachable through anyone
        people.append(dict(entity_id=eid, token=person_tok.get(eid), descriptor=", ".join(filter(None, [_s(e.role), _s(e.org)])), role=_s(e.role), org=_s(e.org),
                           relationship=_s(e.relationship), via="; ".join(via), n_via=len(via), first_seen=e.first_seen, last_seen=e.last_seen, n_docs=e.n_docs))
    contributors = []
    for eid, e in ents[ents.kind == "contributor"].iterrows():
        ivs = [x for x in edges if x["src"] == eid and x["type"] == "INTERVIEWED"]
        srcs = sorted({x["dst"] for x in ivs})
        S = [s for s in sources if s["entity_id"] in srcs]
        contributors.append(dict(entity_id=eid, name=e.canonical, ext_id=_s(e.ext_id), base=_s(e.region), access_keywords=_s(e.topics),
                                 status=_s(e.get("status")),   # Salesforce Status__c: a contributor who has left keeps their edges as history and is marked, never silently counted as reach
                                 n_sources=len(srcs), n_transcripts=len(ivs), n_mentioned=sum(s["n_mentioned"] for s in S),
                                 regions="; ".join(sorted({s["region_norm"] for s in S if s["region_norm"]})),
                                 sectors="; ".join(sorted({s["sector"] for s in S if s["sector"]})),
                                 orgs="; ".join(sorted({s["org"] for s in S if s["org"]})),
                                 projects="; ".join(sorted({x["project"] for x in ivs if x["project"]})),
                                 first_active=min([x["date"] for x in ivs if x["date"]], default=None), last_active=max([x["date"] for x in ivs if x["date"]], default=None)))
    return pd.DataFrame(sources), pd.DataFrame(edges), pd.DataFrame(people), pd.DataFrame(contributors)
