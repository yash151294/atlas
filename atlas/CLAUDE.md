# Pipeline layer

Kedro-shaped without Kedro (ADR 7). `pipeline.PIPELINE` is a declarative list of `Node(name, function, inputs, outputs)` over catalog keys; `pipeline.run(from_step, only)` loads the inputs, calls the function, saves the outputs and writes `run_manifest` (rows, seconds, model spend per node). `pipeline.reindex_document(doc_id)` rebuilds one document's chunks. Nodes in `atlas/nodes/` are pure functions over DataFrames; the wrappers `_resolve`, `_redact`, `_qa`, `_load` in `atlas/pipeline.py` carry human decisions across runs and fan out to storage.

## Nodes and data flow
<!-- generated:nodes -->
| node | function | reads | writes |
|---|---|---|---|
| ingest | `ingest.ingest_dir` | raw_dir | bronze_documents |
| detect | `detect.detect_all` | bronze_documents, silver_spans, silver_doc_meta | silver_spans, silver_doc_meta |
| resolve | `_resolve` | bronze_documents, silver_doc_meta, silver_spans, salesforce_export, silver_mentions, silver_review_queue | silver_entities, silver_mentions, silver_review_queue, silver_links |
| redact | `_redact` | bronze_documents, silver_entities, silver_mentions, silver_links, silver_redacted, silver_vault | silver_redacted, silver_vault |
| qa | `_qa` | silver_redacted, silver_vault, silver_doc_meta, silver_review_queue | silver_qa, silver_redacted |
| graph | `graph.build_graph` | silver_entities, silver_mentions, silver_links, bronze_documents, silver_redacted | gold_sources, gold_edges, gold_people, gold_contributors |
| index | `index.build_chunks` | silver_redacted | gold_chunks |
| load | `_load` | gold_chunks, gold_sources, gold_edges, silver_entities, silver_redacted, gold_people, gold_contributors | the serving tables, the Eventhouse table, the cache |
<!-- /generated -->

In words: files as received → **ingest** (Word/PDF → text, filename fields, engagement code, `sha256`) → **detect** (Presidio ∪ Claude structured extraction; every person gets a role: source, contributor, third_party, private; region and sector from a controlled vocabulary; incremental by text hash) → **resolve** (three tiers: deterministic keys, scored fuzzy with organisation agreement, a human queue; only sources, contributors and professional third parties become entities) → **redact** (one stable token per person, a per-document who-is-who legend, the vault token → value) → **qa** (residual scan, an independent second model, risk score and tier; auto-approval by policy below the threshold) → **graph** (sources, edges including INTERVIEWED and MENTIONED, people, contributors) → **index** (turn-based chunks, contextual header plus legend, embeddings) → **load** (Delta serving tables, the Eventhouse external table, the app's cache).

## Contracts other layers rely on
- `store`: `read(name)` / `write(name, df)` by catalog key (parquet, csv, json; on OneLake a parquet or csv working file is memoised per process until `write(name)` or a full `Cache.refresh()`, json never); `exists`; `read_text` / `write_text`; `append_line` / `read_lines` for JSONL logs (on OneLake each writer appends to its own partition under `Files/<log>/`, readers merge); `delete_file` for the raw files retention removes; `write_table` / `read_table_arrow` / `table_uri` for Delta; `cache()` for the in-memory DuckDB of the serving tables (the network's descriptor matrix, and an FTS index on the passages only when the Eventhouse is not serving retrieval: it costs 30 MB and nothing in production reads it), refreshed at startup and after writes; `now()` for the one log timestamp format; `clean(df)` before JSON; `http`, the retrying session. Writes are atomic.
- `llm.complete(...)`: the Claude door; every call is written to the ledger `llm.LOG` with purpose, tokens, cache hits, cost, provider and prompt version; `llm.provider()` reports the routing; Foundry is the same call once `FOUNDRY_DEPLOYMENTS` is set. `llm_azure.complete` / `llm_azure.embed`: the Azure OpenAI door (plain REST), used for the purposes in `AZURE_OPENAI_FOR`.
- `network.search`: a region or sector the query names becomes a filter and is spent rather than ranked on (`vocab_filters`; an explicit filter from the page wins, and a sector must be named in full), and each matched person carries `why_in`, the phrase of their indexed line the hit came from — the line holds topics and former organisations the page never shows, so a bare word read as unexplained (ADR 42).
- `eventhouse`: `configured()`, `ensure_external_table()` (idempotent, run by load), `retrieve(...)` (KQL: filters first, `series_cosine_similarity`, term hits, reciprocal-rank fusion). `index.retrieve` calls it for every mode when configured, mapping the mode to the two RRF weights, else the cache's BM25 and cosine scan. Retrieval is deterministic on both engines, verified in and across processes (ADR 40); what varies in a briefing is the model calls, and the planner most of all, because it decides the evidence.
- `retention`: `report()`, `enforce(who)`, `subject(name)`, `erase(entity_ids, who, reason)`, `log()`; every action appended to the retention log.
- Node entry points: `ingest.ingest_dir` / `ingest_file`; `detect.detect_all` / `detect_document` (`presidio_spans`, `claude_extract`, `claude_spans`, `split_around_people`, `union`, `fallback_extraction`); `resolve.resolve_all` and `Resolver` (`same_doc_name`); `redact.redact_all`, `reapply_manual`, `manual_redact`, `undo_manual`, `legend`, `approve`; `qa.residual_rules`, `residual_model`, `score`, `gate`, `apply`; `graph.build_graph`; `index.build_chunks`, `contextualise`, `embed_query`, `retrieve`; `rerank.rerank`; `network.search`, `profile`, `source_card`, `ego`, `facets`, `overview`, `traverse` (`resolve_node`, `paths`, `neighbourhood`), `reach_of`; `answer.plan`, `gather`, `verify`, `ask`.

## Gotchas (each cost real time; see the ADR named)
- A clause holding a person's name is split around the name before the span union (`detect.split_around_people`), else the clause is typed PERSON and a sentence lands in the vault (ADR 18).
- A rules-only name the model did not place is informational, never an entity (ADR 19); a lone surname resolves to the one person already in the document, two candidates stay ambiguous (`Resolver.same_doc_name`, ADR 32).
- An exact normalised name links two people; a stated organisation that disagrees adds a queue row rather than splitting them, and a rejection there applies to exact links as well as provisional ones. A provisional link contributes no alias, or the guess matches exactly next time and hardens (`Resolver.match_person`, `_touch`, ADR 45). A name from the file name or Salesforce is tier 1 and a model-read organisation does not question it (`deterministic=True`).
- Manual redaction is whole-word, never inside an organisation name, and reuses the person's existing token; `undo_manual` reverses it; `reapply_manual` carries it across runs (ADR 30, 31).
- A person's token is seeded from the previous vault by their normalised name, never by entity id: ids are positional, so five contributor rows appended to the Salesforce export re-pointed 185 of 316 ids and once re-issued 185 of 199 tokens (ADR 39). A document with no source entity gets no `source_token`: the parquet null is truthy and used to mint one.
- An approval carries its mode (human or policy) across runs; without it a human approval is re-decided as policy (ADR 32).
- Extraction runs on Claude Opus on the first-party API: in-tenancy gpt-5-mini measured 86.7% recall against 93 to 95%, and Azure's content filter blocked an asset-tracing transcript. `detect.strict_types` is for gpt-5-mini only; on Claude it costs FINANCIAL and FAMILY recall (ADR 19).
- urllib3 honours a Retry-After header uncapped: Azure OpenAI 429s froze qa threads for 110 minutes; `store._Retry` caps it at 30 seconds.
- Worker threads appending to one OneLake log partition raced on the file length and failed: appends hold a lock and retry once (ADR 32).
- Degraded mode: without a model, detection is Presidio only, headers are empty, `degraded` is set in doc_meta and priced into the gate (ADR 9).
- After a repair run in production check three things: `approval_mode` on every approved row, empty contextual headers, residual hits.

## Modules
<!-- generated:modules atlas -->
- `atlas/config.py` (24 lines): Roots and catalog. Config (conf/) travels with the code; data lives under one root that is a laptop folder in
- `atlas/eventhouse.py` (66 lines): Retrieval in production runs in the Fabric Eventhouse: an external Delta table over the same gold_chunks the pipeline
- `atlas/llm.py` (99 lines): One door to Claude. Every call is logged with tokens, cache hits and cost so the
- `atlas/llm_azure.py` (92 lines): Azure OpenAI path: models and embeddings served from a resource in the tenant's own subscription and region
- `atlas/pipeline.py` (124 lines): The runner. A declarative list of nodes; each names its catalog inputs and outputs and the runner loads,
- `atlas/retention.py` (87 lines): Retention and data-subject operations. The pipeline sets the clocks (raw_delete_after on approval,
- `atlas/store.py` (298 lines): One door to storage. Working files (parquet, csv, json, logs) under the data root; serving tables as Delta under TABLES;
<!-- /generated -->
<!-- generated:modules atlas/nodes -->
- `atlas/nodes/answer.py` (216 lines): The research desk: plan -> retrieve in parallel -> rerank -> briefing with per-finding citations -> validate
- `atlas/nodes/detect.py` (237 lines): Silver step 1: find people, organisations and PII in a document, and say who each person is
- `atlas/nodes/graph.py` (78 lines): Gold: the network as tables. Nodes = contributors, sources, mentioned professionals, organisations,
- `atlas/nodes/index.py` (181 lines): Gold: retained documents -> contextualised chunks -> two indexes (BM25 + dense) in DuckDB
- `atlas/nodes/ingest.py` (50 lines): Bronze: files as received. Word/PDF -> text + whatever metadata the filename or
- `atlas/nodes/network.py` (385 lines): Network search and traversal: who can reach whom, how two people connect, who sits within N hops. Search has two modes decided from the query:
- `atlas/nodes/qa.py` (156 lines): Silver step 4: quality gate. At 15,000 documents nobody approves each one by hand, so the
- `atlas/nodes/redact.py` (199 lines): Silver step 3: pseudonymise, don't delete. Each resolved person gets one stable token
- `atlas/nodes/rerank.py` (23 lines): Cross-encoder rerank of the fused candidates. Local ONNX model for the demo; Cohere,
- `atlas/nodes/resolve.py` (266 lines): Silver step 2: identity resolution. Three tiers, in this order:
<!-- /generated -->
<!-- generated:modules atlas/evals -->
- `atlas/evals/__init__.py` (2 lines): Evaluation harness. Ground truth comes from the generator's labelled spans and world.json,
- `atlas/evals/faithfulness_eval.py` (50 lines): Citation precision and faithfulness, judged. For a sample of golden questions: run the real
- `atlas/evals/redaction_eval.py` (73 lines): Redaction recall/precision per PII type against the generator's labelled spans
- `atlas/evals/resolve_eval.py` (35 lines): Entity resolution against world.json: did every mention of a source resolve to one entity,
- `atlas/evals/retrieval_eval.py` (50 lines): Retrieval pass@k on a golden question set, one row per retrieval configuration, so the
- `atlas/evals/run.py` (8 lines): Build the golden set (once) and cache the retrieval eval table. uv run python -m atlas.evals.run
- `atlas/evals/snapshot.py` (51 lines): Measure and freeze the evaluation numbers (redaction, resolution, retrieval, faithfulness, the gate, cost) to evals_snapshot.json; the docs quote these
<!-- /generated -->

## Evals
`atlas/evals/run.py` builds the golden question set once and caches the retrieval table; `redaction_eval` (recall and precision per type against the generator's labelled spans), `resolve_eval` (against `data/world.json`), `retrieval_eval` (pass@k per configuration, every row on one engine and labelled with it: set `EVENTHOUSE_URI` to measure the engine the app runs on, unset it for the DuckDB path whose keyword leg is real BM25), `faithfulness_eval` (judged citation precision); `snapshot.py` freezes the Quality page numbers to `data/evals_snapshot.json`. Quote the gate's tier split as "about 80% by policy": it moves by a document or two between qa runs.
