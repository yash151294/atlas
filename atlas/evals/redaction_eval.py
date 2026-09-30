"""Redaction recall/precision per PII type against the generator's labelled spans.
Recall is the number that matters: a missed name is a retained identity."""
import json, re
from pathlib import Path
import pandas as pd
from .. import store
from ..config import REPO

LAB = REPO / "data/labels"


def key(t: str) -> str:
    t = re.sub(r"\[\[\w+:\w+\|", "", t or "").replace("]]", "")   # generator markup nested inside a labelled span
    return re.sub(r"\s+", " ", t.strip().lower().rstrip("'s"))


CONTACT = {"PHONE", "EMAIL", "ADDRESS", "ID_NUMBER"}


def matches(g, preds):
    """A gold span is found if a predicted span of a compatible type covers the same text (or contains it)."""
    gk = key(g["text"])
    for p in preds:
        if p["type"] != g["type"] and not (g["type"] in CONTACT and p["type"] in CONTACT):
            continue
        pk = key(p["text"])
        if gk == pk or gk in pk or (pk in gk and len(pk) > 3):
            return p
    return None


def evaluate(spans: pd.DataFrame | None = None, by_detector: bool = True) -> dict:
    spans = spans if spans is not None else store.read("silver_spans")
    docs = store.read("bronze_documents")[["doc_id", "filename"]]
    rows, misses = [], []
    for d in docs.to_dict("records"):
        lab = LAB / (Path(d["filename"]).stem + ".json")
        if not lab.exists():
            continue
        gold = [g for g in json.loads(lab.read_text())["spans"] if g["type"] not in ("ORG", "DATE")]
        # a name tagged once is the same personal data everywhere: one gold row per distinct surface text
        seen, uniq = set(), []
        for g in gold:
            if (g["type"], key(g["text"])) not in seen:
                seen.add((g["type"], key(g["text"]))); uniq.append(g)
        pred = [p for p in spans[spans.doc_id == d["doc_id"]].to_dict("records") if p["type"] != "ORG"
                and not (p["detectors"] == "presidio" and p["type"] in ("ADDRESS", "NATIONALITY", "PERSON"))]   # informational hits are not redacted, so not scored (a name found only by the rules counts as a miss)
        for g in uniq:
            hit = matches(g, pred)
            rows.append(dict(doc_id=d["doc_id"], type=g["type"], kind="gold", found=hit is not None, detectors=hit["detectors"] if hit else ""))
            if hit is None:
                misses.append(dict(doc_id=d["doc_id"], type=g["type"], text=re.sub(r"\[\[\w+:\w+\|", "", g["text"]).replace("]]", "")))
        pseen = set()
        for p in pred:
            k = (p["type"], key(p["text"]))
            if k in pseen: continue
            pseen.add(k)
            rows.append(dict(doc_id=d["doc_id"], type=p["type"], kind="pred", correct=matches(dict(text=p["text"], type=p["type"]), uniq) is not None, detectors=p["detectors"]))
    df = pd.DataFrame(rows)
    if df.empty:
        return dict(per_type=[], misses=[], n_docs=0)
    per = []
    for t, g in df.groupby("type"):
        gold_n = int((g.kind == "gold").sum()); rec = float(g[g.kind == "gold"].found.mean()) if gold_n else None
        pred_n = int((g.kind == "pred").sum()); prec = float(g[g.kind == "pred"].correct.mean()) if pred_n else None
        per.append(dict(type=t, gold=gold_n, recall=rec, predicted=pred_n, precision=prec))
    out = dict(per_type=per, misses=misses[:50], n_docs=int(df.doc_id.nunique()),
               overall_recall=float(df[df.kind == "gold"].found.mean()), overall_precision=float(df[df.kind == "pred"].correct.mean()))
    if by_detector:
        g = df[df.kind == "gold"]
        out["recall_by_detector"] = {k: float(v) for k, v in {"rules only": (g.detectors == "presidio").mean(), "model only": (g.detectors == "claude").mean(),
                                                               "both agreed": (g.detectors == "claude+presidio").mean(), "missed": (g.found == False).mean()}.items()}
    return out
