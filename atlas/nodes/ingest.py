"""Bronze: files as received. Word/PDF -> text + whatever metadata the filename or
header block gives us. Nothing is interpreted here; that is silver's job."""
import hashlib, re
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from docx import Document
from pypdf import PdfReader

# contributor-named filename: PRJ-ORCHID_Priya-Raghunathan_Lim-Wei-Sheng_2026-03-04.docx
NAMED = re.compile(r"^(?P<project>PRJ-[A-Z]+)_(?P<contributor>[^_]+)_(?P<source>[^_]+)_(?P<date>\d{4}-\d{2}-\d{2})\.(docx|pdf|txt)$")
CODE = re.compile(r"PRJ-[A-Z]+")
HEADER = {k: re.compile(rf"^{k}:\s*(.+)$", re.M | re.I) for k in ["Project", "Contributor", "Date", "Location", "Type"]}


def read_text(p: Path) -> str:
    if p.suffix == ".docx":
        return "\n".join(par.text for par in Document(p).paragraphs)
    if p.suffix == ".pdf":
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages)
    return p.read_text()


def ingest_file(p: Path) -> dict:
    text = read_text(p)
    rec = dict(doc_id=p.stem[:80], filename=p.name, path=str(p), ext=p.suffix[1:],
               sha256=hashlib.sha256(text.encode()).hexdigest()[:16],
               ingested_at=datetime.now(timezone.utc).isoformat(), n_chars=len(text), text=text)
    m = NAMED.match(p.name)
    rec.update(project=None, contributor_name=None, source_name_from_filename=None, doc_date=None)
    if m:
        rec.update(project=m["project"], contributor_name=m["contributor"].replace("-", " "),
                   source_name_from_filename=m["source"].replace("-", " "), doc_date=m["date"])
    for k, rx in HEADER.items():
        h = rx.search(text[:1500])
        if h:
            v = h.group(1).strip()
            if k == "Project" and not rec["project"]: rec["project"] = (CODE.match(v) or [v])[0]   # "PRJ-BASALT (Reputational due diligence)" -> the code
            if k == "Contributor" and not rec["contributor_name"]: rec["contributor_name"] = v
            if k == "Date" and not rec["doc_date"]: rec["doc_date"] = v
            if k == "Location": rec["location"] = v
            if k == "Type": rec["header_type"] = v
    rec["doc_type"] = "deliverable" if p.name.lower().startswith("report") or "deliverable" in p.name.lower() else "transcript"
    return rec


def ingest_dir(raw_dir) -> pd.DataFrame:
    files = sorted(q for q in Path(raw_dir).iterdir() if q.suffix in (".docx", ".pdf", ".txt"))
    df = pd.DataFrame([ingest_file(q) for q in files])
    return df
