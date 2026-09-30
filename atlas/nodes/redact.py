"""Silver step 3: pseudonymise, don't delete. Each resolved person gets one stable token
([PERSON_0017]) across every document, so the retained corpus stays readable and joins to
the graph. What is kept next to the token depends on role: sources and professional third
parties keep a descriptor (title, organisation, relationship); private individuals keep
nothing. The token->identity mapping lives in a separate vault table with its own retention
clock and access control. Policy dial: full | pseudonym_vault | descriptor_only."""
import json, re
from datetime import date, timedelta
import pandas as pd
from ..config import PARAMS
from .resolve import norm

ALWAYS = {"PHONE", "EMAIL", "ADDRESS", "DATE_OF_BIRTH", "ID_NUMBER", "FINANCIAL", "FAMILY", "NATIONALITY"}


def descriptor_of(e: dict, policy: str) -> str:
    if policy == "descriptor_only" and e.get("descriptor_public"):
        return e["descriptor_public"]
    return ", ".join(filter(None, [e.get("role") or "", e.get("org") or ""]))


TOKEN_N = re.compile(r"^\[([A-Z_]+?)_(\d{4})\]$")


def _seed_counters(prev_vault: pd.DataFrame | None) -> tuple[dict, dict, dict]:
    """Last run's token numbers, so a person keeps the token the corpus already carries. Without a seed the counter numbers by
    order of first encounter across the whole corpus, and one new file sorting early re-numbers every person in every document:
    measured, 199 of 199 entities and 52 of 52 source tokens (15 Sep).
    People and organisations are keyed by the normalised values the vault holds for their token, never by entity id: ids are
    positional too (contributors from the export first, then sources in file-name order), so appending five contributor rows
    re-pointed 185 of 316 ids and re-issued 185 of 199 tokens (measured 16 Sep). A name does not move. Per-document tokens
    (contact details, private individuals) and manual redactions keep their own keys, which are stable already.
    Returns (counters keyed as redact_all keys them, name -> number per type, every number already held per type: a number
    another document holds is never re-issued but still counts against the maximum)."""
    counters: dict = {}; names: dict = {}; held: dict = {}
    if prev_vault is None or not len(prev_vault): return counters, names, held
    for r in prev_vault.to_dict("records"):
        m = TOKEN_N.match(str(r.get("token") or ""))
        if not m: continue
        t, n = m.group(1), int(m.group(2))
        held.setdefault(t, set()).add(n)
        eid = str(r.get("entity_id") or "")
        if r.get("role") == "manual": key = ("manual", r.get("doc_id"), r["token"])
        elif t in ("PERSON", "ORG"):
            if v := norm(str(r.get("value") or "")): names.setdefault(t, {}).setdefault(v, n)
            if not eid.startswith("PRIV:"): continue   # an entity id is positional; the names above carry this one
            key = eid                                  # a private individual's id is the document and the text: stable
        else: key = (r.get("doc_id"), r.get("start"))
        counters.setdefault(t, {}).setdefault(key, n)
    return counters, names, held


def _known_as(e: dict) -> list[str]:
    """Every normalised surface form this entity is known by, canonical first: what the previous vault's values are matched against."""
    if not e: return []
    forms = [e.get("canonical") or ""] + str(e.get("aliases") or "").split("; ")
    return [x for x in dict.fromkeys(norm(f) for f in forms if f) if x]


def redact_all(docs: pd.DataFrame, mentions: pd.DataFrame, entities: pd.DataFrame, links: pd.DataFrame, prev_vault: pd.DataFrame | None = None):
    p = PARAMS["redact"]; policy = p["identity_policy"]
    E = {e["entity_id"]: e for e in entities.astype(object).where(pd.notnull(entities), None).to_dict("records")}
    vault, out = [], []
    counters, seeded, held = _seed_counters(prev_vault)
    def token(t, key, known=()):
        d = counters.setdefault(t, {})
        if key not in d:
            taken = set(d.values())
            n = next((v for k in known if (v := seeded.get(t, {}).get(k)) is not None and v not in taken), None)   # the number this person already carries
            d[key] = n if n is not None else max(taken | held.get(t, set()), default=0) + 1   # max, not len: a seeded map has gaps
        return f"[{t}_{d[key]:04d}]"
    for d in docs.to_dict("records"):
        text = d["text"]; ms = mentions[mentions.doc_id == d["doc_id"]].sort_values("start", ascending=False)
        decided = ms["review_decision"] if "review_decision" in ms.columns else pd.Series(None, index=ms.index, dtype=object)
        n_rev = int((ms[ms.type != "ORG"].needs_review & decided[ms.type != "ORG"].isna()).sum()) if len(ms) else 0   # organisations are not redacted under this policy; decided spans need no one
        people = {}
        for m in ms.to_dict("records"):
            if m.get("review_decision") == "restore": continue   # a person said this is not personal data
            t = m["type"]; eid = m.get("entity_id"); role = m.get("role") or "n/a"
            if t == "PERSON":
                if policy == "full": continue
                if m["detectors"] == "presidio" and role in ("n/a", "private") and not str(eid).startswith("P"):
                    continue   # rules-only name the model did not place: informational (see detect.union); the gate re-checks the retained copy
                if not eid: eid = f"PRIV:{d['doc_id']}:{m['text'].lower()}"; role = "private"
                e = E.get(eid, {})
                tok = token("PERSON", eid, _known_as(e))
                kind = e.get("kind") if e else "private"
                if kind == "third_party": kind = "mentioned"
                people[tok] = dict(token=tok, entity_id=eid, kind=kind or "private",
                                   descriptor=descriptor_of(e, policy) if e else "", relationship=e.get("relationship", "") if e else "")
            elif t == "ORG":
                if policy != "descriptor_only": continue
                tok = token("ORG", eid, _known_as(E.get(eid, {})))
            elif t in ALWAYS:
                if t in ("ADDRESS", "NATIONALITY") and m["detectors"] == "presidio":
                    continue  # a city or demonym from NER alone is not personal data; only redacted when the model agrees
                tok = token(t, (d["doc_id"], m["start"]))
            else:
                continue
            text = text[:m["start"]] + tok + text[m["end"]:]
            vault.append(dict(token=tok, doc_id=d["doc_id"], start=m["start"], end=m["end"], type=t, role=role, value=m["text"], entity_id=eid,
                              created=date.today().isoformat(), expires=(date.today() + timedelta(days=p["vault_retention_days"])).isoformat()))
        link = links[links.doc_id == d["doc_id"]].iloc[0] if len(links[links.doc_id == d["doc_id"]]) else {}
        se = link.get("source_entity") if len(link) else None
        if not isinstance(se, str): se = None   # a deliverable has no source; parquet hands back NaN, which is truthy and once minted a token nobody holds
        src = E.get(se, {}) if se else {}
        out.append(dict(doc_id=d["doc_id"], filename=d["filename"], doc_type=d["doc_type"], project=d.get("project"), doc_date=d.get("doc_date"),
                        policy=policy, redacted_text=text, n_redactions=len([v for v in vault if v["doc_id"] == d["doc_id"]]), n_review=n_rev,
                        source_token=token("PERSON", se, _known_as(src)) if se and policy != "full" else (src.get("canonical") if se else None),
                        source_descriptor=descriptor_of(src, policy) if src else "", source_org=src.get("org"), source_region=src.get("region_norm") or src.get("region"),
                        source_sector=src.get("sector"), people=json.dumps(list(people.values())),
                        approved=False, approved_by=None, approved_at=None, raw_delete_after=None))
    return pd.DataFrame(out), pd.DataFrame(vault)


def reapply_manual(red: pd.DataFrame, vault: pd.DataFrame, manual: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A reviewer's manual redactions (vault rows with role=manual) are re-applied to freshly built retained copies: every occurrence of the
    value becomes its token again (renumbered only if this run already issued that token to something else) and the vault row is kept."""
    if not len(manual): return red, vault
    rows, red = [], red.copy()
    for r in manual.to_dict("records"):
        i = red.index[red.doc_id == r["doc_id"]]
        if not len(i): continue
        i = i[0]; rt = red.at[i, "redacted_text"]
        if r["value"] not in rt: continue
        tok = r["token"]
        if ((vault.token == tok) & (vault.doc_id == r["doc_id"])).any() or tok in rt:   # collision with a token this run issued
            n = 1 + max([int(x) for x in re.findall(rf"\[{r['type']}_(\d+)\]", " ".join(red.redacted_text) + " ".join(x["token"] for x in rows))] or [0]); tok = f"[{r['type']}_{n:04d}]"
        count = rt.count(r["value"]); red.at[i, "redacted_text"] = rt.replace(r["value"], tok); red.at[i, "n_redactions"] = int(red.at[i, "n_redactions"]) + count
        rows.append({**r, "token": tok})
    return red, pd.concat([vault, pd.DataFrame(rows)], ignore_index=True) if rows else vault


def _next_token(t: str, red: pd.DataFrame, vault: pd.DataFrame) -> str:
    used = [int(x) for x in re.findall(rf"\[{t}_(\d+)\]", " ".join(red.redacted_text.fillna("")) + " " + " ".join(vault.token.fillna("")))]
    return f"[{t}_{1 + max(used, default=0):04d}]"


def manual_redact(red: pd.DataFrame, vault: pd.DataFrame, entities: pd.DataFrame | None, doc_id: str, text: str, t: str = "PERSON"):
    """A reviewer redacts text the detectors missed. Whole words only; an organisation name is never cut into (a surname inside a
    company name is not personal data); a name the document already holds a token for keeps that token (one person, one token).
    Returns (red, vault, token, count); token is None when nothing matched."""
    i = red.index[red.doc_id == doc_id][0]; rt = red.at[i, "redacted_text"]; low = rt.lower()
    orgs = [o for o in (entities[entities.type == "ORG"].canonical.dropna().tolist() if entities is not None else []) if o and o.lower() in low]
    protected = [(m.start(), m.end()) for o in orgs for m in re.finditer(re.escape(o), rt, re.I)] if t != "ORG" else []
    hits = [m for m in re.finditer(r"(?<!\w)" + re.escape(text) + r"(?!\w)", rt) if not any(a <= m.start() and m.end() <= b for a, b in protected)]
    if not hits: return red, vault, None, 0
    people = json.loads(red.at[i, "people"] or "[]"); tok = eid = None
    if t == "PERSON" and entities is not None:
        n = norm(text); exact, partial = [], []
        for p in people:   # the same person already in this document: by canonical name or alias, or a single name token only one person here carries
            e = entities[entities.entity_id == p["entity_id"]]
            norms = {norm(x) for x in (([e.canonical.iloc[0]] + str(e.aliases.iloc[0] or "").split("; ")) if len(e) else [p["entity_id"].split(":")[-1]]) if x}
            if n in norms: exact.append(p)
            elif len(n.split()) == 1 and any(n in x.split() for x in norms): partial.append(p)
        pick = exact[0] if exact else (partial[0] if len(partial) == 1 else None)
        if pick: tok, eid = pick["token"], pick["entity_id"]
    if tok is None:
        tok = _next_token(t, red, vault)
        if t == "PERSON": people.append(dict(token=tok, entity_id=None, kind="private", descriptor="", relationship="")); red.at[i, "people"] = json.dumps(people)
    for m in reversed(hits): rt = rt[:m.start()] + tok + rt[m.end():]
    red.at[i, "redacted_text"] = rt; red.at[i, "n_redactions"] = int(red.at[i, "n_redactions"]) + len(hits)
    row = dict(token=tok, doc_id=doc_id, start=-1, end=-1, type=t, role="manual", value=text, entity_id=eid, created=date.today().isoformat(),
               expires=(date.today() + timedelta(days=PARAMS["redact"]["vault_retention_days"])).isoformat())
    return red, pd.concat([vault, pd.DataFrame([row])], ignore_index=True), tok, len(hits)


def undo_manual(red: pd.DataFrame, vault: pd.DataFrame, doc_id: str, token: str):
    """Reverse a manual redaction: the text comes back, the vault row goes. Only for a token that exists through manual redaction alone
    (a token shared with the extraction cannot be told apart by position). Returns (red, vault, value, count); value None if not undoable."""
    mine = vault[(vault.doc_id == doc_id) & (vault.token == token)]
    if not len(mine) or (mine.role != "manual").any(): return red, vault, None, 0
    i = red.index[red.doc_id == doc_id][0]; rt = red.at[i, "redacted_text"]; value = mine.value.iloc[0]; count = rt.count(token)
    red.at[i, "redacted_text"] = rt.replace(token, value); red.at[i, "n_redactions"] = max(0, int(red.at[i, "n_redactions"]) - count)
    red.at[i, "people"] = json.dumps([p for p in json.loads(red.at[i, "people"] or "[]") if p["token"] != token])
    return red, vault[~((vault.doc_id == doc_id) & (vault.token == token))], value, count


def legend(people_json: str, tokens: set[str] | None = None) -> str:
    """Plain-text 'who is who' for a document or chunk: what the retrieval index and the answer model see."""
    lines = []
    for p in json.loads(people_json or "[]"):
        if tokens is not None and p["token"] not in tokens: continue
        if p["kind"] == "private":
            lines.append(f'{p["token"]} = private individual (redacted)')
        else:
            who = {"source": "the source", "contributor": "the contributor (interviewer)", "mentioned": "mentioned"}.get(p["kind"], p["kind"])
            rel = f' ({p["relationship"]})' if p.get("relationship") else ""
            lines.append(f'{p["token"]} = {who}{", " + p["descriptor"] if p.get("descriptor") else ""}{rel}')
    return "\n".join(lines)


def approve(redacted: pd.DataFrame, doc_id: str, who: str, mode: str = "human") -> pd.DataFrame:
    p = PARAMS["redact"]
    i = redacted.index[redacted.doc_id == doc_id]
    redacted.loc[i, ["approved", "approved_by", "approved_at", "raw_delete_after"]] = [True, who, date.today().isoformat(), (date.today() + timedelta(days=p["raw_retention_days_after_approval"])).isoformat()]
    if "approval_mode" in redacted.columns:
        redacted.loc[i, "approval_mode"] = mode
    return redacted
