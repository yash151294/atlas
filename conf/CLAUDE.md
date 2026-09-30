# Configuration layer

Config travels with the code; data lives under one root. `atlas/config.py` loads `.env`, resolves `ATLAS_ROOT` (a laptop folder, the `/lakehouse/default` mount inside a Fabric notebook, or an `abfss://…` lakehouse) into `DATA` (`data/` or `Files/`) and `TABLES` (`data/tables/` or `Tables/`), and reads the catalog and parameters from `conf/` (`ATLAS_CONF` overrides the folder; the container sets it to its own copy). Nodes name datasets by catalog key; `config.path(name)` is the only place a path is built.

## Catalog (`conf/catalog.yaml`)
Layers: raw → bronze (text as received, immutable) → silver (spans, doc_meta, entities, mentions, links, review_queue, vault, redacted, qa) → gold (network tables, chunks). The load node writes the serving copies as Delta tables under TABLES; `store.SERVING` maps the app's short names (`sources`, `edges`, `people`, `contributors`, `chunks`, `entities`, `redacted`) to `gold_*` and `silver_*` table names.
<!-- generated:catalog -->
| dataset | path under the data root | format | note |
|---|---|---|---|
| raw_dir | raw | dir |  |
| salesforce_export | raw/salesforce_contributors.csv | csv |  |
| world | world.json | json |  |
| bronze_documents | bronze/documents.parquet | parquet | raw text + metadata, immutable |
| silver_spans | silver/spans.parquet | parquet | PII/entity spans with detector + confidence |
| silver_doc_meta | silver/doc_meta.parquet | parquet | per-document extraction (source, project, date, region, sector, degraded) |
| silver_links | silver/links.parquet | parquet | doc -> contributor entity, source entity |
| silver_entities | silver/entities.parquet | parquet | resolved entities (people, orgs, topics) |
| silver_mentions | silver/mentions.parquet | parquet | entity mention -> resolved entity id |
| silver_review_queue | silver/review_queue.parquet | parquet |  |
| silver_vault | silver/vault.parquet | parquet | token -> real value (restricted) |
| silver_redacted | silver/redacted.parquet | parquet | retained copies |
| silver_qa | silver/qa.parquet | parquet | residual scan, risk score, tier per document |
| gold_sources | gold/sources.parquet | parquet |  |
| gold_edges | gold/edges.parquet | parquet |  |
| gold_people | gold/people.parquet | parquet | mentioned professionals: second-degree reach |
| gold_contributors | gold/contributors.parquet | parquet |  |
| gold_chunks | gold/chunks.parquet | parquet | contextualised chunks + embeddings |
| gold_golden_qa | gold/golden_qa.parquet | parquet |  |
| run_manifest | run_manifest.json | json |  |
<!-- /generated -->

## Parameters (`conf/parameters.yaml`)
Sections and their keys; values and the reason for each are commented in the file. A detect or index parameter change needs a re-run from that node (`uv run python -m atlas.pipeline --from index`); after a prompt or model change, `ATLAS_FORCE_DETECT=1` re-extracts every document. An `answer` or `network` parameter is read on every request, so it takes effect at the next restart and never needs a re-run: `answer.top_k` is how many reranked chunks the model is shown (12, where recall plateaus; 20 cost 70% more evidence text for the same recall), and it is not `index.rerank_k`, which is how many the reranker returns.
<!-- generated:parameters -->
- **llm**: model, judge_model, effort, max_tokens
- **detect**: presidio_threshold, review_band, strict_types
- **resolve**: auto_merge_score, review_score
- **redact**: identity_policy, vault_retention_days, raw_retention_days_after_approval
- **qa**: residual_model_check, presidio_threshold, auto_approve_below, weights
- **network**: match_threshold, recency_half_life_years, max_sources_per_contributor
- **index**: chunk_target_tokens, chunk_overlap_turns, embedding_model, reranker_model, recall_k, rerank_candidates, rerank_k, rerank_max_chars, rrf_semantic_weight, rrf_bm25_weight, contextual_headers
- **answer**: planner_model, history_turns, verify, model, top_k, effort, max_tokens
<!-- /generated -->

## Environment variables the code reads
Endpoints and keys come from `.env` (never committed). In the container the four secrets are Container Apps secrets and the rest environment variables, both set by `azure/deploy_app.py`; in Fabric notebooks `fabric/deploy.py` substitutes them at deploy time. Not in the list below because they are read by name or by a library: `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` (built as `ENTRA_{k}` in `app/main.py`) and `ANTHROPIC_API_KEY` (read by the Anthropic SDK).
<!-- generated:env -->
- `ANTHROPIC_FOUNDRY_BASE_URL`: atlas/llm.py
- `ANTHROPIC_FOUNDRY_RESOURCE`: atlas/llm.py
- `APP_URL`: app/main.py
- `ATLAS_CONF`: atlas/config.py
- `ATLAS_FORCE_DETECT`: atlas/nodes/detect.py
- `ATLAS_REQUIRE_AUTH`: app/main.py
- `ATLAS_ROOT`: atlas/config.py
- `ATLAS_TABLES`: atlas/config.py
- `AZURE_OPENAI_API_KEY`: atlas/llm_azure.py
- `AZURE_OPENAI_CHAT`: atlas/llm_azure.py
- `AZURE_OPENAI_EMBED`: atlas/llm_azure.py, atlas/nodes/index.py
- `AZURE_OPENAI_ENDPOINT`: atlas/llm.py, atlas/llm_azure.py
- `AZURE_OPENAI_FOR`: atlas/llm.py
- `AZURE_OPENAI_REASONING`: atlas/llm_azure.py
- `AZURE_REGION`: atlas/llm.py
- `EVENTHOUSE_DB`: atlas/eventhouse.py
- `EVENTHOUSE_URI`: atlas/eventhouse.py
- `FOUNDRY_DEPLOYMENTS`: atlas/llm.py
- `FOUNDRY_REGION`: atlas/llm.py
- `SESSION_SECRET`: app/main.py
<!-- /generated -->
