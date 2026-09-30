# Atlas: the map

Atlas is a personal experiment in building an AI knowledge pipeline for a research firm that interviews expert sources. One extraction pass over interview transcripts and reports feeds two products: a contributor → source **network** (who can reach whom, with dated provenance) and a **pseudonymised retained corpus** with a research desk that answers with citations. A quality **gate** approves low-risk retained copies by policy and queues the rest for a person. Every person, company and transcript is synthetic. Domain words: a *contributor* (the firm's contractor) interviews a *source*; a *third party* is a professional mentioned in passing; a *private* individual is removed. "Interview" in this codebase always means that domain.

## Layers
Each folder's `CLAUDE.md` is that layer's documentation and is loaded when you read files there. Read it before editing the layer.

| layer | folder | holds |
|---|---|---|
| configuration | `conf/` | catalog (dataset → path), parameters (models, thresholds, the policy dial), the environment variables the code reads |
| pipeline | `atlas/` | pure nodes, the declarative runner, the storage door, the model doors, retention, evals |
| app | `app/` | FastAPI API and plain HTML/JS pages: Network, Research, Review; Compliance |
| production data plane | `fabric/` | Fabric deploy script, notebooks 10/20/30, OneLake and Eventhouse gotchas |
| hosting and models | `azure/` | Container App deploy, the CI deploy identity, Azure OpenAI and Foundry |
| tests | `tests/` | one check per node, no network |
| development data root | `data/` | the synthetic world and generator; what is committed, what is derived |

Decisions with their reasons: `docs/architecture.md` (index at the end of this file). Compliance design: `docs/compliance.md`.

## Invariants
- `atlas/store.py` is the only door to storage; `atlas/llm.py` (Claude) and `atlas/llm_azure.py` (Azure OpenAI, plain REST) are the only doors to models. Nodes name datasets by catalog key and never build paths or call SDKs.
- One data plane. `ATLAS_ROOT` picks a laptop folder (`ATLAS_ROOT=.`) or the lakehouse (`abfss://…`); the layout is the same. No second retrieval engine and no database file: the app's read model is an in-memory DuckDB cache rebuilt from the Delta serving tables (ADR 20, 28).
- Working files are written whole to a `.tmp` sibling and renamed into place; logs are append-only JSONL (per-writer partitions on OneLake); every read-modify-write endpoint in the app runs under one lock, `MUTATE` (ADR 26).
- Human decisions survive a full re-run: approvals with their mode, span decisions, manual redactions and identity decisions are carried by `pipeline._resolve` and `pipeline._redact` (ADR 30).
- Every endpoint that logs takes the signed-in UPN (`who`); every model call lands in the ledger with purpose, tokens, cost and provider (ADR 10, 23).
- Recall over precision in detection; people are never merged on fuzzy evidence alone; private individuals never become entities (ADR 3, 4, 11).
- Config lives in `conf/`, not in code; secrets in `.env`, never committed.

## Working here
- Run: development `ATLAS_ROOT=. uv run …`; production is the default because `.env` carries `ATLAS_ROOT`. App: `uv run uvicorn app.main:app --port 8765`. Tests: `ATLAS_ROOT=. uv run pytest -q` (no model or network calls).
- Style: dense modules; the first line of a docstring states the purpose (the module maps in each layer are generated from it, so keep it true); one function per concern; no speculative abstraction. A deliberately cut corner gets a comment naming its ceiling and the upgrade.
- A change in a layer updates that layer's `CLAUDE.md` prose in the same commit when behaviour, a contract or a gotcha changed (the hook nudges; generated sections refresh themselves). A decision gets an ADR in `docs/architecture.md`. Non-trivial logic gets one test in `tests/test_nodes.py`.
- Ship: branch, PR, merge to `main`. CI runs the tests and `scripts/context.py --check`, then builds the image and rolls the Container App. Never run `azure/deploy_app.py` for a code change; it is for settings and secrets. Pushes that change only Markdown or `docs/` skip CI.
- Orientation: prefer the generated maps (modules, nodes, routes, catalog, tests) to grepping. Do not read `uv.lock`, `data/labels/`, or raw notebook JSON (edit notebooks through parsed cells; see `fabric/CLAUDE.md`).
- Context upkeep: `uv run python scripts/context.py` regenerates the derived sections and checks every path named in a `CLAUDE.md`; `--check` only verifies. The pre-commit hook in `.githooks/` does this on every commit: once per clone, `git config core.hooksPath .githooks`.

## Decisions (`docs/architecture.md`)
<!-- generated:adrs -->
- 1. One extraction pass, two products.
- 2. Pseudonymise with a vault, not delete.
- 3. Two PII detectors unioned.
- 4. Identity resolution in three tiers.
- 5. Relational edges, not a graph database.
- 6. Anthropic contextual retrieval.
- 7. Kedro-shaped without Kedro.
- 8. Fabric by script.
- 9. Degraded mode.
- 10. Every model call in a ledger.
- 11. Role-aware pseudonymisation (13 Sep).
- 12. Approval gate instead of per-document approval (13 Sep).
- 13. Contributor-centric network (13 Sep).
- 14. Agentic-lite retrieval, not GraphRAG (13 Sep).
- 15. Retention and rights as code (13 Sep).
- 16. One user, no app-level roles (13 Sep, second pass).
- 17. Special-category and offence content is detected, counted and priced into the gate (13 Sep, second pass).
- 18. Names inside clauses are split out before the span merge (13 Sep, second pass).
- 19. The gate scores model-confirmed residuals, shows rules-only ones (13 Sep, second pass).
- 20. One data plane (13 Sep, third pass).
- 21. Retrieval runs in the Eventhouse over the Delta table it never copies (13 Sep, third pass).
- 22. Azure AI Search built, measured and removed (13 Sep).
- 23. Identity is Microsoft Entra (13 Sep, third pass).
- 24. The Azure OpenAI client is plain REST (13 Sep, third pass).
- 25. The app runs on Azure Container Apps in UK South (13 Sep, third pass).
- 26. Writes are atomic, mutations serialised, consistency by re-derivation (13 Sep, review pass).
- 27. The pipeline notebook takes its resume step as a Fabric parameter, and a code deploy never touches data (13 Sep, review pass).
- 28. The app's read model is an in-memory DuckDB (13 Sep, review pass, stated).
- 29. Extraction is incremental and the app follows the schedule (13 Sep, review pass).
- 30. Human decisions are durable across runs (13 Sep, review pass, found in use).
- 31. Manual redaction is whole-word, organisation-safe, token-consistent and reversible (14 Sep, found in use).
- 32. A lone surname resolves to the one person the document already holds (14 Sep, found in use).
- 33. The analyst surface keeps only what an analyst does (14 Sep, feedback).
- 34. Open work survives leaving the page (14 Sep, feedback).
- 35. The research desk traverses the network, computed not generated (14 Sep).
- 36. The analyst surface has one design system, and a landing page stands before sign-in (15 Sep, feedback).
- 37. Home is an analytics page over the serving tables (15 Sep, feedback).
- 38. The application is called Atlas (15 Sep, feedback).
- 39. A pseudonym token is identity, so it is carried across runs (15 Sep, found in use).
- 40. The headline is checked like a finding (15 Sep, found in use).
- 41. The research desk is reproducible up to the model, and that boundary is measured, not assumed (15 Sep).
- 42. A region the query names is a filter, and a match shows the phrase it came from (15 Sep, found in use).
- 43. Determinism stops at the model; the machinery to hide that was built, measured and reverted (16 Sep).
- 44. The application names only itself (16 Sep).
- 45. An exact name is evidence; a disagreeing organisation is a question, not a split (16 Sep).
- 46. A contributor who has left is marked, not hidden (16 Sep).
<!-- /generated -->
