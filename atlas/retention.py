"""Retention and data-subject operations. The pipeline sets the clocks (raw_delete_after on approval,
expires on every vault row); this module reads them and acts. Every action is appended to
data/retention_log.jsonl so a DPO can see what was deleted, when, and under which rule."""
from datetime import date
import pandas as pd
from .config import PARAMS
from . import store
from .nodes.resolve import norm

LOG = "retention_log.jsonl"


def _log(**rec):
    store.append_line(LOG, dict(ts=store.now(), **rec))


def log(n: int = 50) -> list[dict]:
    return store.read_lines(LOG)[-n:][::-1]


def report(today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    red = store.read("silver_redacted"); vault = store.read("silver_vault")
    docs = store.read("bronze_documents")[["doc_id", "path"]]
    red = red.merge(docs, on="doc_id", how="left")
    approved = red[red.approved == True]
    due = approved[approved.raw_delete_after.fillna("9999") <= today]
    exp = vault[vault.expires <= today]
    p = PARAMS["redact"]
    return dict(today=today, policy=dict(raw_days_after_approval=p["raw_retention_days_after_approval"], vault_days=p["vault_retention_days"], identity_policy=p["identity_policy"]),
                raw=dict(total=int(len(red)), approved=int(len(approved)), pending=int((red.approved != True).sum()), due_for_deletion=int(len(due)),
                         already_deleted=int(red.get("raw_deleted_at", pd.Series(dtype=object)).notna().sum()) if "raw_deleted_at" in red else 0,
                         next_deletion=approved.raw_delete_after.dropna().min() if len(approved) else None,
                         due_files=due[["doc_id", "filename", "raw_delete_after"]].head(20).to_dict("records")),
                vault=dict(rows=int(len(vault)), expired=int(len(exp)), next_expiry=vault.expires.min() if len(vault) else None, by_type=vault.type.value_counts().to_dict()))


def enforce(who: str, today: str | None = None) -> dict:
    """Delete raw files past their date and vault rows past expiry. Retained copies and tokens stay: without the vault they are anonymous."""
    today = today or date.today().isoformat()
    red = store.read("silver_redacted"); vault = store.read("silver_vault")
    docs = store.read("bronze_documents")
    if "raw_deleted_at" not in red.columns: red["raw_deleted_at"] = None
    due = red[(red.approved == True) & (red.raw_delete_after.fillna("9999") <= today) & red.raw_deleted_at.isna()]
    deleted = []
    for d in due.to_dict("records"):
        p = docs[docs.doc_id == d["doc_id"]].path
        if len(p) and p.iloc[0]: store.delete_file(str(p.iloc[0]))
        red.loc[red.doc_id == d["doc_id"], "raw_deleted_at"] = today
        deleted.append(d["doc_id"]); _log(action="delete_raw", doc_id=d["doc_id"], rule=f"{PARAMS['redact']['raw_retention_days_after_approval']} days after approval", who=who)
    # the raw text column is the other copy of the original: drop it for deleted documents
    docs.loc[docs.doc_id.isin(deleted), "text"] = ""
    expired = vault[vault.expires <= today]
    if len(expired):
        _log(action="expire_vault", rows=int(len(expired)), docs=sorted(set(expired.doc_id)), rule=f"{PARAMS['redact']['vault_retention_days']} days", who=who)
    vault = vault[vault.expires > today]
    store.write("silver_redacted", red); store.write("silver_vault", vault); store.write("bronze_documents", docs)
    return dict(raw_deleted=deleted, vault_expired=int(len(expired)))


def subject(name: str, who: str, reason: str) -> dict:
    """Subject access: everything held about a named person, across vault, entities and retained copies. The one bypass of pseudonymisation the right of access needs, so it is logged like a reveal: who, the request reference, the entity ids found (never the name)."""
    from rapidfuzz import fuzz
    vault = store.read("silver_vault"); ents = store.read("silver_entities")
    n = norm(name)
    people = vault[vault.type == "PERSON"].copy()
    people["score"] = people.value.map(lambda v: fuzz.token_set_ratio(norm(v), n))
    hits = people[people.score >= 85]
    eids = sorted(set(hits.entity_id.dropna()))
    E = ents[ents.entity_id.isin(eids)].astype(object).where(pd.notnull(ents[ents.entity_id.isin(eids)]), None)
    docs = sorted(set(hits.doc_id))
    _log(action="subject_lookup", who=who, reason=reason, rows=int(len(hits)), entity_ids=eids)
    return dict(name=name, matches=int(len(hits)), entities=E[["entity_id", "canonical", "kind", "role", "org", "aliases", "n_docs"]].to_dict("records"),
                documents=docs, tokens=sorted(set(hits.token)),
                held=dict(vault_rows=int(len(hits)), earliest=hits.created.min() if len(hits) else None, expires=hits.expires.max() if len(hits) else None),
                note="Retained copies contain only the tokens listed; the vault rows are the personal data. Erasure deletes the vault rows and the entity record; tokens stay and become anonymous.")


def erase(entity_ids: list[str], who: str, reason: str) -> dict:
    vault = store.read("silver_vault"); ents = store.read("silver_entities")
    sel = vault.entity_id.isin(entity_ids)
    n = int(sel.sum()); docs = sorted(set(vault[sel].doc_id))
    vault = vault[~sel]
    ents.loc[ents.entity_id.isin(entity_ids), ["canonical", "aliases", "norm"]] = ["[erased]", "", ""]
    store.write("silver_vault", vault); store.write("silver_entities", ents)
    _log(action="erase", entity_ids=entity_ids, vault_rows=n, docs=docs, who=who, reason=reason)
    return dict(vault_rows_deleted=n, documents=docs)
