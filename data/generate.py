"""Generate synthetic interview transcripts and client deliverables with Claude.
Text comes back with inline markup [[TYPE:ref|surface text]] so we get exact labelled
spans for the redaction eval for free. Run: uv run python data/generate.py [--limit N]
`--render` writes data/raw/ from the committed labels instead, with no model calls (a fresh clone has labels, not raw files)."""
import json, re, sys
from pathlib import Path
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas.llm import complete

HERE = Path(__file__).parent
W = json.loads((HERE / "world.json").read_text())
RAW = HERE / "raw"; LAB = HERE / "labels"; LAB.mkdir(exist_ok=True)
by = lambda k: {x["id"]: x for x in W[k]}
C, O, S, P = by("contributors"), by("organisations"), by("sources"), by("projects")
TAG = re.compile(r"\[\[(?P<type>[A-Z_]+)(?::(?P<ref>[A-Za-z0-9_-]+))?\|(?P<text>[^\]]+)\]\]")

SYSTEM = """You write realistic synthetic material for a corporate advisory firm's internal software demo (business due diligence, regulatory outlook, reputation checks on companies). Nothing here involves hacking, surveillance, or covert methods; sources are business contacts speaking openly.
Everything is fictional. Never use real people, real companies or real phone numbers/emails of real entities.
Mark every piece of personal or identifying information inline with double-bracket tags so it can be labelled:
[[PERSON:S12|Farid Haddad]] for a named person (ref = source id if it is the interviewed source, contributor id for the contributor, or a fresh id like X1, X2 for third parties),
[[ORG:O01|Meridian Gulf Bank]] for organisations (ref = org id, or fresh id Y1, Y2 for new ones),
[[PHONE|+971 50 123 4567]], [[EMAIL|f.haddad@example.ae]], [[ADDRESS|Villa 14, Al Bateen, Abu Dhabi]],
[[DATE_OF_BIRTH|12 March 1971]], [[NATIONALITY|Lebanese]], [[FAMILY|his brother-in-law runs the family business]],
[[FINANCIAL|a personal loan of AED 2.4m]], [[ID_NUMBER|passport LB0192837]].
Tag EVERY occurrence, including nicknames and initials. Do not tag the project codename, job titles alone, or generic places (cities, countries) unless part of an address.
Write in British English. Output only the document text, no preamble."""

DIALOGUE = """Write an interview transcript for project {pid} ({ptype}). Target: {target}. Brief: {brief}

Header block (exact keys, one per line): Project, Contributor, Date, Location, Type, Source.
- Contributor: {cname} ({cbg}, based in {cbase}); write as [[PERSON:{cid}|{cname}]].
- Source: {sname}, {srole} at {sorg} ({sregion}); write as [[PERSON:{sid}|{sname}]]. {former}
- Date: {date}. Location: a plausible venue in {sregion}. Type: {kind}.
- Source line in header: {source_line}

Then a speaker-labelled dialogue of 26 to 36 turns, alternating "CONTRIBUTOR:" and "SOURCE:", in natural spoken English with hesitations and specifics. The source talks about: {topics}. Include concrete, checkable claims (numbers, dates, names of colleagues, meetings) relevant to the brief, at least two names of third parties, one phone number or email volunteered in passing, one family or personal detail, and one financial detail about a person. {variant_hint}
End with a "CONTRIBUTOR NOTES" section of 4 to 6 sentences: reliability of the source, how the contributor knows them, and what they could ask next time."""

NOTES = """Write contributor interview notes (not a verbatim transcript) for project {pid} ({ptype}). Target: {target}. Brief: {brief}
Header block (exact keys, one per line): Project, Contributor, Date, Location, Type, Source. Contributor: [[PERSON:{cid}|{cname}]]. Source: [[PERSON:{sid}|{sname}]], {srole} at {sorg}. Date: {date}. Type: {kind}. Source line: {source_line}
Then 6 to 9 paragraphs of narrative notes in the first person by the contributor, covering: {topics}. Include concrete claims with numbers and dates, two third-party names, one contact detail, one family detail, one financial detail about a person. {variant_hint}
End with a short "Assessment" paragraph on reliability and access."""

DELIV = """Write a client deliverable (a human-source intelligence report) for project {pid} ({ptype}). Client: {client}. Target: {target}. Brief: {brief}
Structure: Title line; "Key findings" (5 bullets); "Sources consulted" (list sources as "Source A", "Source B" etc. with a one-line description of their role and access, no names); then 4 to 6 findings sections with headings, each 2 to 3 paragraphs attributing claims to "Source A/B/C"; "Assessment and outlook"; "Gaps and next steps".
The sources correspond to these people, in this order: {source_list}. Do not print their names anywhere; refer only to Source A/B/C. Tag any organisation names with [[ORG:id|name]] tags and any third-party individuals named in findings with [[PERSON:Xn|name]] tags. Length about 900 to 1200 words."""

def strip(marked: str):
    """Marked text -> (plain text, spans with exact offsets)."""
    out, spans, pos = [], [], 0
    for m in TAG.finditer(marked):
        out.append(marked[pos:m.start()]); start = sum(len(x) for x in out)
        out.append(m["text"]); spans.append(dict(start=start, end=start + len(m["text"]), type=m["type"], ref=m["ref"], text=m["text"]))
        pos = m.end()
    out.append(marked[pos:])
    return "".join(out), spans

def write_docx(p: Path, text: str):
    d = Document()
    for line in text.split("\n"):
        d.add_paragraph(line)
    d.save(p)

def write_pdf(p: Path, text: str):
    st = getSampleStyleSheet(); doc = SimpleDocTemplate(str(p), pagesize=A4)
    flow = []
    for line in text.split("\n"):
        flow.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;") or " ", st["BodyText"])); flow.append(Spacer(1, 2))
    doc.build(flow)

def transcript(t):
    c, s, p = C[t["contributor"]], S[t["source"]], P[t["project"]]
    variant = s["name_variants"][0] if s["name_variants"] else None
    variant_hint = f'Refer to the source once by the variant "{variant}" (tag it [[PERSON:{s["id"]}|{variant}]]).' if variant else ""
    former = f'Formerly at {O[s["former_orgs"][0]]["name"]}; mention this.' if s["former_orgs"] else ""
    source_line = ("(see filename)" if t["naming"] == "filename" else f'[[PERSON:{s["id"]}|{s["name"]}]], {s["role"]}, [[ORG:{s["org"]}|{O[s["org"]]["name"]}]]')
    kind = "Interview transcript" if t["style"] == "dialogue" else "Interview notes"
    tmpl = DIALOGUE if t["style"] == "dialogue" else NOTES
    prompt = tmpl.format(pid=p["id"], ptype=p["type"], target=p["target"], brief=p["brief"], cname=c["name"], cbg=c["background"], cbase=c["base"], cid=c["id"],
                         sname=s["name"], srole=s["role"], sorg=f'[[ORG:{s["org"]}|{O[s["org"]]["name"]}]]', sregion=s["region"], sid=s["id"], former=former,
                         date=t["date"], kind=kind, source_line=source_line, topics=s["topics"], variant_hint=variant_hint)
    marked = complete(prompt, system=SYSTEM, purpose="generate.transcript", effort="low", max_tokens=6000)
    text, spans = strip(marked)
    slug = lambda n: n.replace(" ", "-").replace(".", "")
    if t["naming"] == "filename":
        fname = f'{p["id"]}_{slug(c["name"])}_{slug(s["name"])}_{t["date"]}.{t["fmt"]}'
    else:
        fname = f'{p["id"]} interview {t["date"]} {t["id"]}.{t["fmt"]}'
    (write_docx if t["fmt"] == "docx" else write_pdf)(RAW / fname, text)
    (LAB / f'{Path(fname).stem}.json').write_text(json.dumps(dict(assignment=t, filename=fname, spans=spans, text=text), indent=1))
    return fname

def deliverable(p, n):
    srcs = [S[x] for x in p["sources"]][:6]
    source_list = "; ".join(f'Source {chr(65+i)} = {s["name"]}, {s["role"]} at {O[s["org"]]["name"]}' for i, s in enumerate(srcs))
    marked = complete(DELIV.format(pid=p["id"], ptype=p["type"], client=p["client"], target=p["target"], brief=p["brief"], source_list=source_list),
                      system=SYSTEM, purpose="generate.deliverable", effort="low", max_tokens=6000)
    body, spans = strip(marked)
    pre = f'Project: {p["id"]}\nType: Client deliverable\nDate: 2026-0{n}-20\n\n'
    text = pre + body
    for sp in spans: sp["start"] += len(pre); sp["end"] += len(pre)
    fname = f'Report_{p["id"]}_{p["type"].replace(" ", "-")}_v{n}.docx'
    write_docx(RAW / fname, text)
    (LAB / f'{Path(fname).stem}.json').write_text(json.dumps(dict(project=p["id"], filename=fname, spans=spans, text=text, source_map=[s["id"] for s in srcs]), indent=1))
    return fname

def render():
    """The committed labels hold every document's text: write the raw files a fresh clone lacks."""
    RAW.mkdir(exist_ok=True)
    for f in sorted(LAB.glob("*.json")):
        lab = json.loads(f.read_text()); p = RAW / lab["filename"]
        if not p.exists(): (write_pdf if p.suffix == ".pdf" else write_docx)(p, lab["text"]); print("rendered", p.name)

if __name__ == "__main__" and "--render" in sys.argv:
    render()
elif __name__ == "__main__":
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10**6
    todo = [t for t in W["transcripts"] if not any(LAB.glob(f'*{t["id"]}*')) and not any(LAB.glob(f'{t["project"]}_*_{t["date"]}.json'))]
    from concurrent.futures import ThreadPoolExecutor
    def safe(t):
        try: return transcript(t)
        except Exception as e: return f"FAILED {e}"[:200]
    with ThreadPoolExecutor(6) as ex:
        for t, fname in zip(todo[:limit], ex.map(safe, todo[:limit])):
            print("transcript", t["id"], t["project"], "->", fname, flush=True)
    for i, p in enumerate(W["projects"]):
        for n in (1, 2) if i < 2 else (1,):
            fname = f'Report_{p["id"]}_{p["type"].replace(" ", "-")}_v{n}.docx'
            if not (RAW / fname).exists() and limit > 0:
                try: print("deliverable ->", deliverable(p, n), flush=True)
                except Exception as e: print("deliverable FAILED", p["id"], str(e)[:160], flush=True)
