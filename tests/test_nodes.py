"""One check per node, no API calls. Run: uv run pytest -q"""
from pathlib import Path
import pandas as pd
from atlas.nodes import ingest, detect, resolve, redact, qa, graph, index

FIX = Path(__file__).parent / "fixtures/PRJ-LANTERN_Tariq-Al-Mansoori_Farid-Haddad_2025-02-03.txt"
CONTRIB = pd.DataFrame([dict(Contact_Id="0035000001", Full_Name="Tariq Al-Mansoori", Base_City__c="Dubai", Access_Keywords__c="Gulf banking")])


def doc():
    d = ingest.ingest_file(FIX); d["doc_type"] = "transcript"; d["doc_id"] = "T-FIX"
    return d


def test_ingest_reads_filename_and_header():
    d = doc()
    assert d["project"] == "PRJ-LANTERN" and d["contributor_name"] == "Tariq Al Mansoori" and d["source_name_from_filename"] == "Farid Haddad" and d["doc_date"] == "2025-02-03"


def test_ingest_project_is_the_engagement_code(tmp_path):
    """A report header 'Project: PRJ-BASALT (Reputational due diligence)' must not create a second engagement in the filters."""
    f = tmp_path / "Report_PRJ-BASALT_Reputational-due-diligence_v1.txt"; f.write_text("Project: PRJ-BASALT (Reputational due diligence)\nDate: 2025-03-01\n\nSummary.")
    d = ingest.ingest_file(f)
    assert d["project"] == "PRJ-BASALT" and d["doc_type"] == "deliverable"


def test_detect_reuses_unchanged_documents(monkeypatch):
    """A document whose text hash matches last run's metadata is not sent to the model again."""
    monkeypatch.setattr(detect, "detect_document", lambda d: (_ for _ in ()).throw(AssertionError("extracted again")))
    docs = pd.DataFrame([dict(doc_id="T-FIX", sha256="abc", text="x")])
    old_spans = pd.DataFrame([dict(doc_id="T-FIX", start=0, end=1, type="PERSON")]); old_meta = pd.DataFrame([dict(doc_id="T-FIX", sha256="abc", project="PRJ-X")])
    spans, meta = detect.detect_all(docs, old_spans, old_meta)
    assert len(spans) == 1 and meta.project.iloc[0] == "PRJ-X"


def test_presidio_finds_contact_details():
    types = {s["type"] for s in detect.presidio_spans(doc()["text"])}
    assert {"EMAIL", "PHONE", "PERSON"} <= types


def test_union_merges_overlaps_and_flags_review():
    spans = detect.union([dict(start=0, end=5, type="PERSON", text="Farid", detector="presidio", confidence=0.6, role="n/a"),
                          dict(start=0, end=12, type="PERSON", text="Farid Haddad", detector="claude", confidence=0.95, role="source")])
    assert len(spans) == 1 and spans[0]["detectors"] == "claude+presidio" and spans[0]["role"] == "source" and not spans[0]["needs_review"]
    lone = detect.union([dict(start=0, end=5, type="PERSON", text="Farid", detector="claude", confidence=0.6, role="private")])
    assert lone[0]["needs_review"]                                   # one detector, middling confidence: a person decides
    rules = detect.union([dict(start=0, end=6, type="PERSON", text="Campos", detector="presidio", confidence=0.85, role="n/a")])
    assert rules[0]["informational"] and not rules[0]["needs_review"]   # NER alone: not redacted, the gate re-checks the retained copy


def test_union_splits_a_clause_around_a_name():
    text = "In 2023 Villela personally guaranteed a tranche of USD 18 million"
    spans = detect.union([dict(start=0, end=len(text), type="FINANCIAL", text=text, detector="claude", confidence=0.9, role="n/a"),
                          dict(start=8, end=15, type="PERSON", text="Villela", detector="claude", confidence=0.99, role="third_party")])
    assert [(s["start"], s["end"], s["type"]) for s in spans] == [(0, 8, "FINANCIAL"), (8, 15, "PERSON"), (15, len(text), "FINANCIAL")]


def fixture_inputs():
    d = doc(); text = d["text"]
    spans = detect.union(detect.presidio_spans(text))
    for s in spans: s["text"] = text[s["start"]:s["end"]]; s["doc_id"] = d["doc_id"]
    spans = detect.clip_newlines(spans)
    # the model's role call for two people: a professional third party (kept as a descriptor) and a private one (redacted, never an entity)
    for s in spans:
        if s["text"] == "Nadia Kamel": s.update(role="third_party", title="Head of Compliance", organisation="Meridian Gulf Bank", relationship="successor")
        if s["text"] == "Farid Haddad": s.update(role="source")
        if s["text"] == "F. Haddad": s.update(role="third_party", title="", organisation="Meridian Gulf Bank", relationship="")   # a variant the model could not place
        if s["text"] == "Omar Bin Rashed": s.update(role="private")
    # the initials mention is the entity-resolution test case
    meta = dict(doc_id="T-FIX", source_name="Farid Haddad", source_role="former Head of Compliance", source_org="Meridian Gulf Bank", source_region="Gulf",
                topics=["AML controls", "licensing"], source_former_orgs=["Gulf Fintech Regulatory Office"], contributor_name="Tariq Al-Mansoori", interview_date="2025-02-03")
    return d, meta, spans


def pipeline_tables():
    d, meta, spans = fixture_inputs()
    ents, mentions, review, links = resolve.resolve_all(pd.DataFrame([d]), pd.DataFrame([meta]), pd.DataFrame(spans), CONTRIB)
    red, vault = redact.redact_all(pd.DataFrame([d]), mentions, ents, links)
    return d, ents, mentions, review, links, red, vault


def test_resolve_links_contributor_to_salesforce_and_initials_to_review():
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    contrib = ents[ents.kind == "contributor"].iloc[0]
    assert contrib.ext_id == "0035000001" and links.contributor_entity.iloc[0] == contrib.entity_id
    src = ents[ents.kind == "source"].iloc[0]
    assert src.canonical == "Farid Haddad"
    assert any((review.mention == "F. Haddad") & (review.candidate_entity == src.entity_id)) if len(review) else False


def test_roles_decide_who_becomes_an_entity():
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    tp = ents[ents.kind == "third_party"]
    assert list(tp.canonical) == ["Nadia Kamel"] and tp.org.iloc[0] == "Meridian Gulf Bank"     # only the professionally described person
    priv = mentions[(mentions.type == "PERSON") & (mentions.decision == "private")]
    assert len(priv) and priv.entity_id.str.startswith("PRIV:").all()                             # private people are redacted without becoming a node
    info = mentions[(mentions.type == "PERSON") & (mentions.decision == "informational")]
    assert len(info) and info.entity_id.isna().all() and (info.detectors == "presidio").all()      # rules-only names the model did not place: not redacted, gate re-checks
    people = __import__("json").loads(red.people.iloc[0])
    kinds = {p["kind"] for p in people}
    assert {"source", "mentioned", "private"} <= kinds
    assert "Head of Compliance, Meridian Gulf Bank" in redact.legend(red.people.iloc[0])


def test_a_same_name_at_another_organisation_is_queued_not_merged_in_silence():
    """The exact-name tier linked two people with one name and no organisation check, silently: on the demo corpus a central
    banker and someone at the company under investigation were one node. The link still stands (a person changes employer, and
    an organisation is often a description), but it becomes a question a reviewer can reject, which splits them."""
    r = resolve.Resolver(CONTRIB)
    a = r._add("PERSON", "Chinedu Okafor", kind="third_party", org="Central Bank of Nigeria")
    same, dec = r.match_person("Chinedu Okafor", "Central Bank of Nigeria, Abuja", None, "T-1", allow_fuzzy_link=False)
    assert (same, dec) == (a, "exact") and not r.review                      # the same organisation, said differently: no question
    other, dec = r.match_person("Chinedu Okafor", "PayNaira Ltd", None, "T-2", allow_fuzzy_link=False)
    assert (other, dec) == (a, "exact"), "the link is kept: splitting on a stated organisation loses people who moved"
    assert len(r.review) == 1 and r.review[0]["doc_id"] == "T-2" and r.review[0]["org_agrees"] is False
    r.match_person("Chinedu Okafor", "PayNaira Ltd", None, "T-2", allow_fuzzy_link=False)
    assert len(r.review) == 1, "one question per document and mention, however often the name recurs"
    src, dec = r.match_person("Chinedu Okafor", "PayNaira Ltd", None, "T-3", allow_fuzzy_link=False, deterministic=True)
    assert (src, dec) == (a, "exact") and len(r.review) == 1                 # the file name is tier 1: a model-read organisation does not question it


def test_a_provisional_link_never_hardens_into_an_alias():
    """'Tadashi Kurosawa' was linked to Takeshi in the review band, became an alias, and every later mention then matched
    exactly, so rejecting the guess left the hardened mentions merged."""
    r = resolve.Resolver(CONTRIB)
    a = r._add("PERSON", "Takeshi Kurosawa", kind="third_party", org="Kurosawa Heavy Industries")
    eid, dec = r.match_person("Tadashi Kurosawa", "Kurosawa Heavy Industries", None, "T-1", allow_fuzzy_link=False)
    assert (eid, dec) == (a, "provisional")
    r._touch(eid, "T-1", "2025-06-18", "Tadashi Kurosawa", dec)
    assert not r.entities[a]["aliases"], "an unconfirmed guess became a name the entity answers to"
    assert r.match_person("Tadashi Kurosawa", "", None, "T-2", allow_fuzzy_link=False)[1] == "provisional"   # still a question, never 'exact'


def test_a_contributor_who_has_left_is_marked_not_hidden():
    """Salesforce Status__c was not read at all, so a leaver looked as reachable as anyone. Their interviews stay in the
    network as history; the status travels to the contributor table the pages read."""
    left = pd.DataFrame([dict(CONTRIB.iloc[0], Status__c="Left")])
    d, meta, spans = fixture_inputs()
    ents, mentions, review, links = resolve.resolve_all(pd.DataFrame([d]), pd.DataFrame([meta]), pd.DataFrame(spans), left)
    red, vault = redact.redact_all(pd.DataFrame([d]), mentions, ents, links)
    sources, edges, people, contributors = graph.build_graph(ents, mentions, links, pd.DataFrame([d]), red)
    assert contributors.status.iloc[0] == "Left"
    assert int(contributors.n_transcripts.iloc[0]) == 1 and (edges.type == "INTERVIEWED").any()   # the history stays


def test_qa_gate_flags_leaks_and_auto_approves_clean_docs():
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    hits = qa.residual_rules(red.redacted_text.iloc[0], vault.value.tolist(), presidio=None)
    assert not [h for h in hits if h["kind"] == "leaked_value"]
    leaked = red.redacted_text.iloc[0] + " Call Nadia Kamel on +971 50 123 4567."
    found = {h["text"] for h in qa.residual_rules(leaked, vault.value.tolist(), presidio=None)}
    assert {"Nadia Kamel", "+971 50 123 4567"} <= found                      # a leaked name and a phone number both surface (one hit per string)
    assert not qa.plausible(dict(kind="model_contact", text="2026-01-20")) and not qa.plausible(dict(kind="model_name", text="[PERSON_0012]"))
    meta = pd.DataFrame([dict(doc_id="T-FIX", degraded=False)]); queue = pd.DataFrame(columns=["doc_id", "status"])
    g = qa.gate(red, vault, meta, queue, use_presidio=False, use_model=False)
    assert g.tier.iloc[0] == "auto" and qa.apply(red, g).approved.iloc[0] == True          # clean retained copy: approved by policy
    leaky = red.copy(); leaky["redacted_text"] = leaked
    g2 = qa.gate(leaky, vault, meta, queue, use_presidio=False, use_model=False)
    assert g2.risk_score.iloc[0] > g.risk_score.iloc[0] and g2.tier.iloc[0] == "review"     # a surviving name and phone number send it to a person
    assert qa.apply(leaky, g2).approved.iloc[0] == False


def test_redact_replaces_pii_consistently_and_fills_vault():
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    t = red.redacted_text.iloc[0]
    assert "+971 50 123 4567" not in t and "n.kamel@example.ae" not in t
    assert "[PHONE_" in t and "[EMAIL_" in t and "[PERSON_" in t
    assert (vault.value == "+971 50 123 4567").any() and vault.expires.iloc[0] > vault.created.iloc[0]
    assert red.approved.iloc[0] == False and red.raw_delete_after.iloc[0] is None
    # a deliverable has no source: the link is a parquet null, which is truthy, and once minted a source token no vault row backed
    none = redact.redact_all(pd.DataFrame([d]), mentions, ents, links.assign(source_entity=float("nan")))[0]
    assert none.source_token.iloc[0] is None
    red2 = redact.approve(red, "T-FIX", "analyst@example.com")
    assert red2.approved.iloc[0] == True and red2.raw_delete_after.iloc[0] is not None


def test_human_decisions_survive_a_rerun():
    """What a person decided is re-applied when the pipeline rebuilds the tables: a rejected identity link, a restored span, a manual redaction."""
    from atlas import pipeline
    d, meta, spans = fixture_inputs()
    _, ents, mentions, review, links, red, vault = pipeline_tables()
    phone = mentions[mentions.type == "PHONE"].iloc[0]
    old_m = mentions.assign(review_decision=None); old_m.loc[old_m.start == phone.start, "review_decision"] = "restore"
    old_q = review.assign(status="rejected")                                     # 'F. Haddad' was judged a different person
    ents2, m2, q2, links2 = pipeline._resolve(pd.DataFrame([d]), pd.DataFrame([meta]), pd.DataFrame(spans), CONTRIB, old_m, old_q)
    assert (q2.status == "rejected").all() and (m2[m2.text == "F. Haddad"].decision == "private").all()
    assert m2.loc[m2.start == phone.start, "review_decision"].iloc[0] == "restore"
    manual = pd.DataFrame([dict(token="[PERSON_0200]", doc_id="T-FIX", start=-1, end=-1, type="PERSON", role="manual", value="Meridian", entity_id=None, created="2026-09-13", expires="2027-09-13")])
    old_red = redact.approve(red.assign(approval_mode=None), "T-FIX", "reviewer@example.com", mode="human")
    red2, vault2 = pipeline._redact(pd.DataFrame([d]), ents2, m2, links2, old_red, pd.concat([vault, manual]))
    t = red2.redacted_text.iloc[0]
    assert phone.text in t                                                       # the restored phone number is back in the text
    assert "Meridian" not in t and (vault2.role == "manual").sum() == 1
    g = qa.gate(red2, vault2, pd.DataFrame([dict(doc_id="T-FIX", degraded=False)]), q2, use_presidio=False, use_model=False)
    out = qa.apply(red2, g)
    assert out.approved_by.iloc[0] == "reviewer@example.com" and out.approval_mode.iloc[0] == "human"    # the gate never overwrites a person's approval


def test_manual_redaction_keeps_organisations_and_reuses_tokens():
    """A reviewer's redaction is whole-word, never cuts into an organisation name, reuses the token of a person the document already holds, and can be undone."""
    ents = pd.DataFrame([dict(entity_id="P0001", type="PERSON", canonical="Nadia Kamel", aliases="Kamel"), dict(entity_id="O0001", type="ORG", canonical="Meridian Gulf Bank", aliases="")])
    red = pd.DataFrame([dict(doc_id="D", redacted_text="Met [PERSON_0001] at Meridian Gulf Bank. Kamel joined later; Ms Kamel left. Meridian is big.", n_redactions=1,
                             people=__import__("json").dumps([dict(token="[PERSON_0001]", entity_id="P0001", kind="mentioned", descriptor="", relationship="")]))])
    vault = pd.DataFrame([dict(token="[PERSON_0001]", doc_id="D", start=4, end=15, type="PERSON", role="third_party", value="Nadia Kamel", entity_id="P0001", created="2026-09-13", expires="2027-09-13")])
    red, vault, tok, n = redact.manual_redact(red, vault, ents, "D", "Kamel")
    assert tok == "[PERSON_0001]" and n == 2 and red.redacted_text.iloc[0].count("[PERSON_0001]") == 3          # same person, same token
    red, vault, tok, n = redact.manual_redact(red, vault, ents, "D", "Meridian")
    assert tok == "[PERSON_0002]" and n == 1 and "Meridian Gulf Bank" in red.redacted_text.iloc[0]                # the bare word is redacted, the bank's name is not cut
    assert any(p["token"] == tok and p["kind"] == "private" for p in __import__("json").loads(red.people.iloc[0]))
    assert redact.manual_redact(red, vault, ents, "D", "Gulf")[2] is None                                         # only inside an organisation name: nothing to redact
    red, vault, value, n = redact.undo_manual(red, vault, "D", "[PERSON_0002]")
    assert value == "Meridian" and n == 1 and red.redacted_text.iloc[0].endswith("Meridian is big.") and not (vault.token == "[PERSON_0002]").any()
    assert redact.undo_manual(red, vault, "D", "[PERSON_0001]")[2] is None                                        # shared with the extraction: not undoable


def test_surname_alone_links_to_the_one_person_in_the_document():
    """A rules-only 'Ogata' after 'Masaru Ogata' is that person; a surname three people share stays unlinked and informational."""
    d = doc(); d["text"] = "Masaru Ogata runs the marine division. Ogata is the stronger operator. Ichiro Kurosawa and Junichi Kurosawa disagree; Kurosawa senior decides."
    d["contributor_name"] = None; d["source_name_from_filename"] = None
    spans = []
    for name, role, detector, conf, info in [("Masaru Ogata", "third_party", "claude+presidio", 0.94, False), ("Ogata", "n/a", "presidio", 0.85, True),
                                             ("Ichiro Kurosawa", "third_party", "claude", 0.95, False), ("Junichi Kurosawa", "third_party", "claude", 0.95, False), ("Kurosawa", "n/a", "presidio", 0.8, True)]:
        i = {"Ogata": d["text"].index(" Ogata is") + 1, "Kurosawa": d["text"].index("Kurosawa senior")}.get(name, d["text"].index(name))   # the standalone surnames
        spans.append(dict(doc_id="T-FIX", start=i, end=i + len(name), type="PERSON", text=name, role=role, detectors=detector, confidence=conf, informational=info, needs_review=False,
                          title="head of marine division" if "Ogata" in name else "executive", organisation="Kurosawa Heavy Industries", relationship=""))
    ents, mentions, review, links = resolve.resolve_all(pd.DataFrame([d]), pd.DataFrame([dict(doc_id="T-FIX", topics=[], source_former_orgs=[])]), pd.DataFrame(spans), CONTRIB)
    ogata = ents[ents.canonical == "Masaru Ogata"].entity_id.iloc[0]
    assert (mentions[mentions.text == "Ogata"].entity_id == ogata).all() and mentions[mentions.text == "Ogata"].decision.iloc[0] == "same_doc_name"
    assert mentions[mentions.text == "Kurosawa"].decision.iloc[0] == "informational"      # two Kurosawas in the document: ambiguous
    red, vault = redact.redact_all(pd.DataFrame([d]), mentions, ents, links)
    t = red.redacted_text.iloc[0]; tok = vault[vault.value == "Masaru Ogata"].token.iloc[0]
    assert "Ogata" not in t and t.count(tok) == 2 and "Kurosawa senior" in t                  # one token for the person; the ambiguous surname is left for the gate
    red, vault, tok2, n = redact.manual_redact(red.assign(people=red.people), vault, ents, "T-FIX", "Kurosawa")
    assert tok2 not in (None, tok) and n == 1                                                # a reviewer redacting the ambiguous surname gets a new token, not a guess


def test_tokens_survive_a_new_document():
    """A person keeps their token when the corpus changes underneath them. Numbering followed order of first encounter, so one
    file sorting early re-numbered every person in every document (measured on the demo corpus: 199 of 199 entities)."""
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    before = {r["entity_id"]: r["token"] for r in vault.to_dict("records") if r["type"] == "PERSON" and r["entity_id"]}
    assert before, "fixture issues no person tokens"
    docs = pd.DataFrame([d])
    # an earlier document would take the low numbers first; seeded from last run's vault, the tokens already issued hold
    seeded = redact.redact_all(docs, mentions, ents, links, prev_vault=vault)[1]
    after = {r["entity_id"]: r["token"] for r in seeded.to_dict("records") if r["type"] == "PERSON" and r["entity_id"]}
    assert after == before
    # a number another document already holds is never re-issued, even when no mention of ours carries it
    held = pd.DataFrame([dict(token="[PERSON_0900]", doc_id="T-OTHER", start=-1, end=-1, type="PERSON", role="source", value="x", entity_id="P9999")])
    reserved = redact.redact_all(docs, mentions, ents, links, prev_vault=pd.concat([vault, held], ignore_index=True))[1]
    assert "[PERSON_0900]" not in set(reserved.token), "a number held elsewhere was handed to a second person"
    assert {r["entity_id"]: r["token"] for r in reserved.to_dict("records") if r["type"] == "PERSON" and r["entity_id"]} == before


def test_tokens_survive_renumbered_entity_ids():
    """A contributor added to the Salesforce export shifts every entity id after it (ids are positional), and seeding the
    token counter by entity id then handed people each other's numbers: 185 of 199 re-issued on the demo corpus (16 Sep).
    The seed follows the name the vault holds, so the same person keeps the same token under any id."""
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    name = dict(zip(ents.entity_id, ents.canonical))
    before = {name[r["entity_id"]]: r["token"] for r in vault.to_dict("records") if r["type"] == "PERSON" and r["entity_id"] in name}
    assert before, "fixture issues no person tokens"
    shift = lambda e: f"P{int(e[1:]) + 5:04d}" if isinstance(e, str) and e.startswith("P") and e[1:].isdigit() else e
    ents2 = ents.assign(entity_id=ents.entity_id.map(shift))
    mentions2 = mentions.assign(entity_id=mentions.entity_id.map(shift))
    links2 = links.assign(contributor_entity=links.contributor_entity.map(shift), source_entity=links.source_entity.map(shift))
    red2, vault2 = redact.redact_all(pd.DataFrame([d]), mentions2, ents2, links2, prev_vault=vault)
    name2 = dict(zip(ents2.entity_id, ents2.canonical))
    after = {name2[r["entity_id"]]: r["token"] for r in vault2.to_dict("records") if r["type"] == "PERSON" and r["entity_id"] in name2}
    assert after == before
    assert red2.source_token.iloc[0] == red.source_token.iloc[0], "the source's token moved with its id"


def test_graph_has_interviewed_edge_with_provenance():
    d, ents, mentions, review, links, red, vault = pipeline_tables()
    sources, edges, people, contributors = graph.build_graph(ents, mentions, links, pd.DataFrame([d]), red)
    iv = edges[edges.type == "INTERVIEWED"]
    assert len(iv) == 1 and iv.doc_id.iloc[0] == "T-FIX" and iv.date.iloc[0] == "2025-02-03"
    assert sources.n_transcripts.iloc[0] == 1 and (edges.type == "HAS_ACCESS_TO").sum() == 2 and (edges.type == "FORMERLY_AT").sum() == 1
    assert (edges.type == "MENTIONED").sum() == 1 and len(people) == 1 and people.via.iloc[0] == sources.entity_id.iloc[0]   # second-degree reach
    assert contributors.n_sources.iloc[0] == 1 and contributors.n_mentioned.iloc[0] == 1


def test_chunking_by_speaker_turns():
    cs = index.chunk(doc()["text"], "transcript")
    assert len(cs) >= 2 and all(c["text"] for c in cs) and "SOURCE" in cs[0]["speakers"]


def test_store_writes_are_atomic(tmp_path, monkeypatch):
    """A write lands whole or not at all: a failure mid-write leaves the previous file untouched and no temp file behind."""
    from atlas import store
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    store.write_text("note.txt", "v1")
    class Boom(Exception): pass
    def bad(p): p.write_text("partial"); raise Boom()
    with __import__("pytest").raises(Boom):
        store._write_local(tmp_path / "note.txt", bad)
    assert store.read_text("note.txt") == "v1" and [x.name for x in tmp_path.iterdir()] == ["note.txt"]


def test_store_round_trip_and_cache(tmp_path, monkeypatch):
    """A serving table written as Delta is what the cache serves; the same code path OneLake uses in production."""
    from atlas import store
    monkeypatch.setattr(store, "TABLES", str(tmp_path / "tables"))
    df = pd.DataFrame(dict(entity_id=["P1", "P2"], name=["A", "B"], base=["Dubai", "London"]))
    store.write_table("contributors", df)
    c = store.Cache(); c.refresh("contributors")
    assert c.has("contributors") and c.q("SELECT name FROM contributors WHERE base='Dubai'") == [dict(name="A")]


def test_store_memoises_onelake_working_files(monkeypatch):
    """On OneLake a working file is fetched once per process: a second read costs no round trip, a write or a full cache refresh fetches it again."""
    from atlas import store
    fetched = []
    buf = __import__("io").BytesIO(); pd.DataFrame(dict(doc_id=["T-1"], text=["hello"])).to_parquet(buf)
    monkeypatch.setattr(store, "ONELAKE", True); monkeypatch.setattr(store, "exists", lambda name: True); monkeypatch.setattr(store, "_put", lambda p, data: None)
    monkeypatch.setattr(store, "_get", lambda p: fetched.append(p) or buf.getvalue()); monkeypatch.setattr(store, "_memo", {}); monkeypatch.setattr(store, "SERVING", {})
    assert store.read("silver_mentions").text[0] == "hello" and store.read("silver_mentions").text[0] == "hello" and len(fetched) == 1
    store.write("silver_mentions", pd.DataFrame(dict(doc_id=["T-1"]))); store.read("silver_mentions"); assert len(fetched) == 2
    store.Cache().refresh(); store.read("silver_mentions"); assert len(fetched) == 3


def test_eventhouse_kql_shape():
    """The two KQL rules that broke production once: dates compare as datetimes, and the project column is bracketed."""
    from atlas import eventhouse
    csl = eventhouse.kql([0.1, 0.2], ["licence", "gulf"], dict(projects=["PRJ-LANTERN"], regions=["Gulf"], date_from="2025-01-07", date_to="2026-07-02"), 20, 0.8, 0.2)
    assert "todatetime(doc_date) >= datetime(2025-01-07)" in csl and "['project'] in ('PRJ-LANTERN')" in csl and "series_cosine_similarity(embedding, q)" in csl
    assert "has 'licence'" in csl and "| top 20 by fused desc" in csl


def test_eventhouse_honours_a_single_index_mode():
    """A zero weight drops that leg, and a keyword-only query must not fall back to the dense leg for its tie-break:
    with both, the eval's four rows run on the engine the app runs on instead of two rows quietly using DuckDB."""
    from atlas import eventhouse
    kw_only = eventhouse.kql([0.1, 0.2], ["licence"], None, 20, 0.0, 1.0)
    assert "order by kw desc, chunk_id asc" in kw_only and "0.0 / (60.0 + dense_rank)" in kw_only
    assert "order by kw desc, dense desc" in eventhouse.kql([0.1, 0.2], ["licence"], None, 20, 0.8, 0.2)


def test_subject_lookup_is_logged_without_the_name(monkeypatch):
    """A name lookup is the one bypass of pseudonymisation the right of access needs; it leaves a row with who, why and the entity ids, never the name."""
    from atlas import retention
    vault = pd.DataFrame([dict(doc_id="T-FIX", token="[PERSON_0001]", type="PERSON", value="Farid Haddad", entity_id="E1", created="2025-02-03", expires="2026-02-03")])
    ents = pd.DataFrame([dict(entity_id="E1", canonical="Farid Haddad", kind="source", role="CFO", org="Gulf Bank", aliases="", n_docs=1)])
    monkeypatch.setattr(retention.store, "read", lambda key: {"silver_vault": vault, "silver_entities": ents}[key])
    rows = []; monkeypatch.setattr(retention.store, "append_line", lambda rel, rec: rows.append((rel, rec)))
    out = retention.subject("Farid Haddad", who="dpo@example.com", reason="DSAR-42")
    assert out["matches"] == 1 and out["tokens"] == ["[PERSON_0001]"]
    (rel, rec), = rows
    assert rel == retention.LOG and rec["action"] == "subject_lookup" and rec["who"] == "dpo@example.com" and rec["reason"] == "DSAR-42" and rec["entity_ids"] == ["E1"] and "Haddad" not in str(rec)


def test_research_traverses_the_network():
    """Shortest chains between two contributors run through the sources they share, each step dated; a neighbourhood lists who sits at each hop."""
    from atlas.nodes import network
    E = pd.DataFrame([dict(src="C1", dst="S1", type="INTERVIEWED", doc_id="d1", date="2025-05-12", project="PRJ-A"), dict(src="C2", dst="S1", type="INTERVIEWED", doc_id="d2", date="2024-11-02", project="PRJ-B"),
                      dict(src="C1", dst="S2", type="INTERVIEWED", doc_id="d3", date="2025-01-01", project="PRJ-A"), dict(src="C2", dst="S2", type="INTERVIEWED", doc_id="d4", date="2025-02-01", project="PRJ-B"),
                      dict(src="S1", dst="P1", type="MENTIONED", doc_id="d1", date="2025-05-12", project="PRJ-A"), dict(src="S2", dst="O1", type="WORKS_AT", doc_id="d3", date=None, project=None),
                      dict(src="C3", dst="S3", type="INTERVIEWED", doc_id="d5", date="2025-03-03", project="PRJ-C"), dict(src="S3", dst="O1", type="WORKS_AT", doc_id="d5", date=None, project=None),
                      dict(src="C1", dst="S1", type="INTERVIEWED", doc_id="d6", date="2026-01-01", project="PRJ-A")])   # a second interview of the same source is the same route
    ps = network.paths(E, ["C1"], ["C2"])
    assert len(ps) == 2 and all(len(p) == 2 for p in ps) and {p[0]["dst"] for p in ps} == {"S1", "S2"}   # two shortest routes, one per shared source
    assert next(p for p in ps if p[0]["dst"] == "S1")[0]["date"] == "2026-01-01"                            # the step carries the latest interview
    assert [s["forward"] for s in ps[0]] == [True, False]                                                   # C1 interviewed S1; S1 was interviewed by C2
    assert [s["dst"] for s in network.paths(E, ["C1"], ["C3"])[0]] == ["S2", "O1", "S3", "C3"]                                # four hops through a shared organisation
    assert network.paths(E, ["C1"], ["C3"], max_hops=3) == [] and network.paths(E, ["C1"], ["P1"])[0][-1]["type"] == "MENTIONED"
    assert {r["id"]: r["depth"] for r in network.neighbourhood(E, ["C1"], 2)} == {"S1": 1, "S2": 1, "C2": 2, "P1": 2, "O1": 2}


def test_verify_keeps_the_order_it_was_given(monkeypatch):
    """ask() checks the headline by appending it as the last unit and reading it back off the end, so verify must return
    its verdicts in input order; if it ever reordered, a finding's verdict would be shown against the headline."""
    from atlas.nodes import answer
    monkeypatch.setattr(answer, "complete", lambda *a, **k: answer.Check(supported=True, reason="supported"))
    units = [dict(claim=f"finding {i}", citations=["D#1"]) for i in range(6)] + [dict(claim="the headline", citations=["D#1"])]
    out = answer.verify(units, {"D#1": dict(text="evidence")})
    assert [f["claim"] for f in out] == [u["claim"] for u in units]
    assert out[-1]["claim"] == "the headline" and out[-1]["verified"] is True


def test_plan_tolerates_nulls_from_the_model():
    """A null or a non-numeric depth from the planner model falls back to the field's default instead of failing the plan."""
    from atlas.nodes.answer import Plan
    p = Plan.model_validate(dict(queries=["x"], regions=None, network_kind=None, network_from=None, network_hops="two"))
    assert p.regions == [] and p.network_kind == "" and p.network_hops == 0 and Plan.model_validate(dict(queries=["x"], network_hops="3")).network_hops == 3


def test_sign_in_returns_to_a_local_path_only():
    """After sign-in the app goes back to the page that was asked for, never to another host (an open redirect is a phishing hop)."""
    from app.main import _safe_next
    assert _safe_next("/documents?doc=T-1") == "/documents?doc=T-1"
    assert _safe_next("//evil.example/") == "/" and _safe_next("https://evil.example") == "/" and _safe_next("") == "/"


def test_a_question_left_mid_briefing_is_rejoined_not_asked_again():
    """Switching task dropped the response, so the desk asked the question again on the way back: a second set of model
    calls for a briefing the server was already writing. The page's job id joins the run in flight instead."""
    import threading
    from app.main import _ask_job
    calls, started, release = [], threading.Event(), threading.Event()
    def run():
        calls.append(1); started.set(); release.wait(5); return dict(headline="one briefing")
    threading.Thread(target=lambda: _ask_job("j1", run), daemon=True).start()   # asked, then the analyst left the page
    assert started.wait(5)
    threading.Timer(0.1, release.set).start()
    assert _ask_job("j1", run) == dict(headline="one briefing") and len(calls) == 1   # back on the desk: the same run, one set of model calls
    assert _ask_job("", run)["headline"] == "one briefing" and len(calls) == 2        # without a job id every ask is its own run


def test_a_region_in_the_query_filters_and_is_not_ranked_on():
    """'compliance at a gulf bank' returned a US pharma lawyer whose topics happened to say 'central bank supervision
    Nigeria' and 'external compliance review': two of three terms was enough, and 'gulf' matched nothing and was not
    required to. A region the query names is now a filter; a sector must be named in full, because its halves
    ('payments', 'legal', 'regulator') are words an analyst types without meaning one."""
    from atlas.nodes import network
    regions = ["Gulf", "Japan & Korea", "North America"]
    sectors = ["banking & financial services", "fintech & payments"]
    r, sec, spent = network.vocab_filters("compliance at a gulf bank", regions, sectors)
    assert r == "Gulf" and sec is None and spent == ["gulf"]
    assert network.vocab_filters("japan merger review", regions, sectors)[0] == "Japan & Korea"   # either half of a place name
    assert network.vocab_filters("payments licensing", regions, sectors)[1] is None               # not the sector 'fintech & payments'
    assert network.vocab_filters("banking & financial services in the gulf", regions, sectors)[1] == "banking & financial services"
    # the phrase a hit came from, so a topic match is not an unexplained chip against a descriptor that does not carry the word
    line = "former General Counsel, Corvus Pharmaceuticals. North America. access to: central bank supervision Nigeria; external compliance review"
    assert network.where(line, ["bank", "compliance"]) == ["central bank supervision Nigeria", "external compliance review"]


def test_home_overview_counts_reach_holders_and_pace():
    """Home's coverage counts sources per region and sector with the contributors who hold them and the last contact, reach decays with age; pace counts interviews per month over 24 months; a contributor is quiet after a year."""
    from datetime import date
    from atlas.nodes import network
    t = date.today(); recent = t.replace(day=1).isoformat(); old = t.replace(year=t.year - 2).isoformat()
    S = pd.DataFrame([dict(entity_id="S1", region_norm="Gulf", sector="banking", contributors="C1; C2", n_contributors=2, last_seen=recent),
                      dict(entity_id="S2", region_norm="Gulf", sector="banking", contributors="C1", n_contributors=1, last_seen=old),
                      dict(entity_id="S3", region_norm="Nordics", sector="fintech", contributors="C2", n_contributors=1, last_seen=old)])
    C = pd.DataFrame([dict(entity_id="C1", name="A", base="Dubai", n_sources=2, n_transcripts=2, last_active=recent, regions="Gulf"), dict(entity_id="C2", name="B", base="Oslo", n_sources=2, n_transcripts=2, last_active=old, regions="Gulf; Nordics")])
    E = pd.DataFrame([dict(type="INTERVIEWED", date=recent), dict(type="INTERVIEWED", date=old), dict(type="MENTIONED", date=recent)])
    o = network.overview(S, C, E)
    gulf = next(c for c in o["coverage"]["cells"] if c["region"] == "Gulf")
    assert gulf["n"] == 2 and gulf["contributors"] == 2 and gulf["last"] == recent and 1 < gulf["reach"] < 2   # a two-year-old contact counts half
    assert o["coverage"]["warm"] == 1 and o["coverage"]["single"] == 2 and [r["region"] for r in o["coverage"]["regions"]] == ["Gulf", "Nordics"]
    assert len(o["pace"]["months"]) == 24 and o["pace"]["last12"] == 1 and o["pace"]["prev12"] == 0   # the two-year-old interview is outside the window; a mention is not an interview
    assert [c["quiet"] for c in o["contributors"]] == [False, True]
