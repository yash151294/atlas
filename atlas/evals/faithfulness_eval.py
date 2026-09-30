"""Citation precision and faithfulness, judged. For a sample of golden questions: run the real
answer path, then ask a judge model whether each finding is supported by the chunks it cites (a finding
may draw on several chunks; the judge sees all of them).
The judge is a tool, not the truth: the rubric is here in plain sight and a human spot-checks a few.
uv run python -m atlas.evals.faithfulness_eval"""
import json, sys
import pandas as pd
from pydantic import BaseModel, Field
from .. import store
from ..config import PARAMS
from ..llm import complete
from ..nodes import answer


class Verdict(BaseModel):
    supported: bool = Field(description="true if the sentence's factual content is stated in or directly entailed by the cited chunk text")
    reason: str = Field(description="one line")


RUBRIC = """You are auditing a research briefing for faithfulness. You will see one finding and the chunk(s) it cites.
Supported = every factual claim in the finding appears in the cited chunks or follows directly from them, taken together. Paraphrase is fine; new facts, numbers or names not in any cited chunk are not.
Judge only against the chunks shown, not your own knowledge."""


def evaluate(n: int = 8) -> dict:
    qa = store.read("gold_golden_qa").sample(n, random_state=3)
    rows = []
    for q in qa.itertuples():
        a = answer.ask(q.question)
        chunks = {c["chunk_id"]: c for c in a["candidates"]}
        units = [(f["claim"], f["citations"]) for f in a.get("findings", [])]   # a finding is the unit of citation
        for sent, cites in units:
            valid = [c for c in cites if c in chunks]
            if not valid:
                rows.append(dict(question=q.question, sentence=sent, cited=cites, citation_valid=False, supported=False, reason="cited id not in the evidence shown to the model")); continue
            ev = "\n\n".join(f"<chunk id=\"{c}\">\n{chunks[c]['text']}\n</chunk>" for c in valid)
            v = complete(f"<sentence>{sent}</sentence>\n{ev}\nIs the sentence supported?", system=RUBRIC, purpose="eval.judge", schema=Verdict, model=PARAMS["llm"]["judge_model"], effort="low", max_tokens=300)
            rows.append(dict(question=q.question, sentence=sent, cited=cites, citation_valid=True, supported=v.supported, reason=v.reason))
    df = pd.DataFrame(rows)
    out = dict(n_questions=int(len(qa)), n_sentences=int(len(df)), citation_precision=float(df.citation_valid.mean()) if len(df) else None,
               faithfulness=float(df[df.citation_valid].supported.mean()) if df.citation_valid.any() else None,
               unsupported=df[~df.supported][["question", "sentence", "reason"]].head(12).to_dict("records"), rubric=RUBRIC)
    store.write_text("faithfulness_eval.json", json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    o = evaluate(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
    print(f"sentences {o['n_sentences']}  citation precision {o['citation_precision']}  faithfulness {o['faithfulness']}")
    for u in o["unsupported"]: print(" -", u["sentence"][:100], "|", u["reason"][:100])
