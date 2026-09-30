"""The runner. A declarative list of nodes; each names its catalog inputs and outputs and the runner loads,
calls and saves (Kedro's node / catalog / runner idiom without the dependency, so adopting Kedro proper is a
mechanical move). A manifest records rows, seconds and model spend per node. Resume from any step with --from.
Usage: uv run python -m atlas.pipeline [--from step] [--only step]   (ATLAS_FORCE_DETECT=1 re-extracts unchanged documents)"""
import sys, time
from dataclasses import dataclass
from typing import Callable
import pandas as pd
from .config import PARAMS
from . import llm, store
from .nodes import ingest, detect, resolve, redact, qa, graph, index


def load(name: str):
    return store.read(name)   # None when an optional input (a table from an earlier run) does not exist yet


def save(name: str, obj):
    if obj is not None: store.write(name, obj)


def _resolve(docs, meta, spans, contributors, old_mentions, old_queue):
    """Identity decisions (confirmed / rejected) and span decisions (confirm / restore) made by a person are re-applied on every run."""
    ents, mentions, queue, links = resolve.resolve_all(docs, meta, spans, contributors)
    if old_queue is not None and len(old_queue) and len(queue) and "status" in old_queue.columns:
        decided = old_queue[old_queue.status.isin(["confirmed", "rejected"])][["doc_id", "mention", "status"]].drop_duplicates(["doc_id", "mention"])
        queue = queue.drop(columns=["status"]).merge(decided, on=["doc_id", "mention"], how="left"); queue["status"] = queue.status.fillna("pending")
        for r in decided[decided.status == "rejected"].itertuples():   # the same rule the Review page applies: a rejected link becomes a private token
            sel = (mentions.doc_id == r.doc_id) & (mentions.text == r.mention) & (mentions.decision.isin(("provisional", "exact")))   # exact too: a queue row is raised against one (ADR 45)
            mentions.loc[sel, ["entity_id", "decision", "role"]] = [f"PRIV:{r.doc_id}:{r.mention.lower()}", "private", "private"]
    if old_mentions is not None and "review_decision" in old_mentions.columns:
        keep = old_mentions.dropna(subset=["review_decision"])[["doc_id", "start", "end", "type", "review_decision"]].drop_duplicates(["doc_id", "start", "end", "type"])
        mentions = mentions.merge(keep, on=["doc_id", "start", "end", "type"], how="left")
    return ents, mentions, queue, links


def _redact(docs, ents, mentions, links, old, old_vault):
    """State carried across runs: a person's approvals and manual redactions survive; policy approvals are re-decided by the gate."""
    seed = old_vault
    if old is not None and "source_token" in old.columns and links is not None and len(links):
        # a source never named in the body has no vault row, but the retained copy carries its token: seed those too, or 8 of 52 move
        src = old[["doc_id", "source_token"]].merge(links[["doc_id", "source_entity"]], on="doc_id", how="inner").dropna()
        extra = pd.DataFrame(dict(token=src.source_token, entity_id=src.source_entity, type="PERSON", role="source", doc_id=src.doc_id, start=-1,
                                  value=src.source_entity.map(dict(zip(ents.entity_id, ents.canonical)))))   # the seed is keyed by name, so these rows carry one
        seed = pd.concat([seed, extra], ignore_index=True) if seed is not None and len(seed) else extra
    red, vault = redact.redact_all(docs, mentions, ents, links, prev_vault=seed)   # last run's numbering is kept: tokens are identity, not sequence
    if old is not None:
        keep = old[(old.approved == True) & (old.get("approval_mode", pd.Series(index=old.index, dtype=object)) == "human")][["doc_id", "approved", "approved_by", "approved_at", "raw_delete_after", "approval_mode"]]
        red = red.drop(columns=["approved", "approved_by", "approved_at", "raw_delete_after"]).merge(keep, on="doc_id", how="left")
        red["approved"] = red.approved.fillna(False).astype(bool)
    if old_vault is not None and "role" in old_vault.columns:
        red, vault = redact.reapply_manual(red, vault, old_vault[old_vault.role == "manual"])
    return red, vault


def _qa(red, vault, meta, queue):
    queue = queue if queue is not None else pd.DataFrame(columns=["doc_id", "status"])
    g = qa.gate(red, vault, meta, queue)
    return g, qa.apply(red, g)   # the gated table overwrites silver_redacted; a Kedro project would name it a second dataset


def _load(chunks, sources, edges, ents, red, people, contributors):
    """Serving tables as Delta (laptop folder, lakehouse mount or OneLake), the network's embedding index, and the Eventhouse's
    external table over the passages. The app's cache reloads from these; nothing else is a copy."""
    for name, df in [("sources", sources), ("edges", edges), ("people", people), ("contributors", contributors), ("entities", ents), ("redacted", red), ("chunks", chunks),
                     ("net_index", index.network_texts(sources, people))]:
        store.write_table(name, df)
    from . import eventhouse
    if eventhouse.configured(): eventhouse.ensure_external_table()
    try:
        store.cache().refresh()
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("cache refresh skipped: %s", e)


@dataclass
class Node:
    name: str
    func: Callable
    inputs: list[str]
    outputs: list[str]


PIPELINE = [
    Node("ingest", ingest.ingest_dir, ["raw_dir"], ["bronze_documents"]),
    Node("detect", detect.detect_all, ["bronze_documents", "silver_spans", "silver_doc_meta"], ["silver_spans", "silver_doc_meta"]),   # incremental: unchanged documents keep last run's spans
    Node("resolve", _resolve, ["bronze_documents", "silver_doc_meta", "silver_spans", "salesforce_export", "silver_mentions", "silver_review_queue"],
         ["silver_entities", "silver_mentions", "silver_review_queue", "silver_links"]),
    Node("redact", _redact, ["bronze_documents", "silver_entities", "silver_mentions", "silver_links", "silver_redacted", "silver_vault"], ["silver_redacted", "silver_vault"]),
    Node("qa", _qa, ["silver_redacted", "silver_vault", "silver_doc_meta", "silver_review_queue"], ["silver_qa", "silver_redacted"]),
    Node("graph", graph.build_graph, ["silver_entities", "silver_mentions", "silver_links", "bronze_documents", "silver_redacted"],
         ["gold_sources", "gold_edges", "gold_people", "gold_contributors"]),
    Node("index", index.build_chunks, ["silver_redacted"], ["gold_chunks"]),
    Node("load", _load, ["gold_chunks", "gold_sources", "gold_edges", "silver_entities", "silver_redacted", "gold_people", "gold_contributors"], []),
]
STEPS = [n.name for n in PIPELINE]


def run(from_step: str = "ingest", only: str | None = None):
    manifest = dict(started=time.strftime("%Y-%m-%dT%H:%M:%S"), params=PARAMS, steps={})
    for node in [n for n in PIPELINE if n.name == only] if only else PIPELINE[STEPS.index(from_step):]:
        t0 = time.time(); cost0 = sum(c["cost_usd"] for c in llm.calls())
        outs = node.func(*[load(i) for i in node.inputs])
        outs = outs if isinstance(outs, tuple) else (outs,)
        for name, obj in zip(node.outputs, outs): save(name, obj)
        n = len(outs[0]) if outs[0] is not None and hasattr(outs[0], "__len__") else 1
        manifest["steps"][node.name] = dict(rows=int(n), seconds=round(time.time() - t0, 1), llm_cost_usd=round(sum(c["cost_usd"] for c in llm.calls()) - cost0, 4))
        print(f"{node.name:8s} rows={n:5d} {manifest['steps'][node.name]['seconds']:6.1f}s ${manifest['steps'][node.name]['llm_cost_usd']:.4f}", flush=True)
    manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    old = (store.read("run_manifest") if (only or from_step != "ingest") else None) or {"steps": {}}
    old["steps"].update(manifest["steps"]); old.update({k: v for k, v in manifest.items() if k != "steps"})
    store.write("run_manifest", old)


def reindex_document(doc_id: str):
    """After a human edits a retained copy (restores a false positive), rebuild that document's chunks and reload."""
    red = load("silver_redacted"); new = index.build_chunks(red[red.doc_id == doc_id])
    C = load("gold_chunks"); save("gold_chunks", pd.concat([C[C.doc_id != doc_id], new], ignore_index=True))
    run(only="load")


if __name__ == "__main__":
    a = sys.argv
    run(from_step=a[a.index("--from") + 1] if "--from" in a else "ingest", only=a[a.index("--only") + 1] if "--only" in a else None)
