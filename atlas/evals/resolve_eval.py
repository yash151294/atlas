"""Entity resolution against world.json: did every mention of a source resolve to one entity,
and did no two sources collapse into one? Pairwise precision/recall over PERSON mentions."""
import json, re
from itertools import combinations

key = lambda t: re.sub(r"\s+", " ", (t or "").strip().lower())
import pandas as pd
from .. import store
from ..config import REPO


def evaluate() -> dict:
    W = json.loads((REPO / "data/world.json").read_text())
    labels = {}
    for lab in (REPO / "data/labels").glob("*.json"):
        j = json.loads(lab.read_text())
        for s in j["spans"]:
            if s["type"] == "PERSON" and s["ref"]:
                ref = s["ref"] if s["ref"][0] in "SC" else f"{lab.stem}:{s['ref']}"   # X refs are fresh per document
                labels[(lab.stem, key(s["text"]))] = ref
    docs = store.read("bronze_documents")[["doc_id", "filename"]]
    stem = dict(zip(docs.doc_id, docs.filename.str.replace(r"\.(docx|pdf)$", "", regex=True)))
    m = store.read("silver_mentions"); m = m[m.type == "PERSON"].copy()
    m["gold"] = [labels.get((stem[r.doc_id], key(r.text))) for r in m.itertuples()]
    m = m.dropna(subset=["gold", "entity_id"])   # informational (rules-only) mentions have no entity
    # sample pairs across documents for tractability
    sample = m.sample(min(len(m), 600), random_state=1)
    tp = fp = fn = 0
    for a, b in combinations(sample.itertuples(), 2):
        same_gold, same_pred = a.gold == b.gold, a.entity_id == b.entity_id
        tp += same_gold and same_pred; fp += (not same_gold) and same_pred; fn += same_gold and (not same_pred)
    review = store.read("silver_review_queue") if store.exists("silver_review_queue") else pd.DataFrame()
    return dict(n_mentions=int(len(m)), pairwise_precision=tp / (tp + fp) if tp + fp else None, pairwise_recall=tp / (tp + fn) if tp + fn else None,
                review_queue=int(len(review)), sources_expected=len({t["source"] for t in W["transcripts"]}),
                sources_found=int(store.read("silver_entities").query("kind == 'source'").shape[0]))
