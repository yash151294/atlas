# Atlas: two problems, one pipeline

A personal experiment: how far can one person get, in a few days, building a privacy-first AI pipeline for a research firm that interviews expert sources? The firm has two problems. It cannot see who among its contributors can reach whom, and it cannot keep the transcripts it has paid for, because they are full of personal data. Atlas is an attempt at solving both with one extraction pass.

Every person, company and transcript is synthetic. This is an experiment, not a product: nothing here has been used on real data, and `docs/compliance.md` is an engineering view, not legal advice.

One extraction pass over each transcript finds people, organisations, topics and personal-data spans. From that pass: (1) a contributor → source network with dated, provenance-carrying edges, searchable by anyone; (2) a pseudonymised copy of every transcript and report that the firm can retain, with a separately secured vault, plus a research desk that answers questions over that corpus with citations. Every person is classified by role, so sources and professionally described third parties keep a descriptor and private individuals are removed; a quality gate checks each retained copy for surviving identifiers and special-category content, approves low-risk copies by policy and queues the rest; retention and data-subject rights are enforced as code (`docs/compliance.md`). The decisions behind all of it, including what was measured and reverted, are in `docs/architecture.md`.

## Run it
```
uv sync                                   # Python 3.12, all dependencies
uv run python -m spacy download en_core_web_lg
echo "ANTHROPIC_API_KEY=..." > .env       # never committed; the only key local development needs (embeddings and the reranker run locally)
uv run python data/build_world.py         # deterministic synthetic world (contributors, sources, projects) and the Salesforce-style export
uv run python data/generate.py --render   # the raw documents from the committed labels, no model calls
# uv run python data/generate.py          # or: Claude writes a fresh corpus, with labelled spans (costs model calls, changes the ground truth)
uv run python -m atlas.pipeline           # ingest → detect → resolve → redact → qa → graph → index → load (Delta tables + Eventhouse); same in Fabric on a schedule; detect skips unchanged documents (ATLAS_FORCE_DETECT=1 to re-extract)
uv run python -m atlas.evals.run          # golden questions + retrieval pass@k
uv run python -m atlas.evals.snapshot     # measures and freezes the evaluation numbers (redaction, retrieval, faithfulness, gate, cost) to data/evals_snapshot.json
uv run uvicorn app.main:app --port 8765   # http://localhost:8765, signed in as a development analyst; with ATLAS_ROOT=abfss://… in .env it serves from a Fabric lakehouse
uv run pytest -q                          # one check per node, no API calls
uv run python fabric/deploy.py --data --run   # after `az login`: workspace, lakehouse, Eventhouse, the wheel, conf, notebooks, daily schedules; --data seeds OneLake from data/ (first deploy only); --run runs 10_pipeline
uv run python azure/deploy_app.py         # the app on Azure Container Apps (UK South): builds and pushes the image, sets the app's settings and secrets from .env, the managed identity's Fabric access, the Entra redirect URI
```

The optional production path is Microsoft Fabric plus Azure Container Apps (`fabric/CLAUDE.md`, `azure/CLAUDE.md`). With it, every merge to `main` runs the tests, then the `deploy` job in `.github/workflows/ci.yml` builds the image, pushes it to the registry tagged with the commit and rolls the Container App to it, failing unless a healthy revision is running that image within three minutes. It signs in as an Entra app registration through a federated credential (no Azure secret in GitHub) that holds only AcrPush on the registry and Container Apps Contributor on the app; without the repository variables it needs, the job is skipped and CI only runs the tests. Settings and secrets are not touched by CI; `azure/deploy_app.py` remains the way to change those. A push that changes only Markdown or `docs/` skips the run altogether.

## Context for people and agents
`CLAUDE.md` at the root is the map: what the system is, its invariants, and where to look for what. Each layer folder (`atlas/`, `app/`, `conf/`, `fabric/`, `azure/`, `tests/`, `data/`) carries its own `CLAUDE.md`: that layer's documentation for a person, and the context an agent loads only when it works there. Decisions are in `docs/architecture.md`, the compliance design in `docs/compliance.md`. The derived parts of every `CLAUDE.md` (module maps, pipeline nodes, API routes, catalog, parameter sections, environment variables, tests, the decision index) are generated from the code by `scripts/context.py`, so they cannot drift: the pre-commit hook regenerates them on every commit (`git config core.hooksPath .githooks`, once per clone) and CI fails when they are stale or a `CLAUDE.md` names a path that does not exist.

## Layout
- `conf/` catalog (dataset → path) and parameters (model, thresholds, policy dial). Config lives outside code.
- `atlas/nodes/` pure functions per stage; `atlas/pipeline.py` the runner and the live single-file path; `atlas/evals/` the harness; `atlas/llm.py` the one door to Claude with a cost ledger.
- `app/` FastAPI and plain HTML pages: three analyst tasks, Network (who can reach whom), Research (briefings with evidence), Review (the gate's queue); one governance page, Compliance (retention, rights, logs, assumptions). Users sign in with their Microsoft account (Entra); the signed-in identity is on every logged action; engagement scope is a platform property (OneLake security).
- `data/` (development root) world spec, generator, raw files, labels, bronze/silver/gold parquet, Delta serving tables under `data/tables/`. In production the root is the Fabric lakehouse (`ATLAS_ROOT` in `.env`): the same layout under `Files/` and `Tables/`, the Eventhouse reading the passages in place, the app's in-memory cache refreshed from OneLake.
- `fabric/` REST deployment script and three notebooks that run the same node code in a Fabric lakehouse.
- `azure/` the Container App deployment script, the in-tenancy model notes and the Foundry deployment definitions. `.github/workflows/ci.yml` tests on every push and deploys every merge to `main`.
- `docs/` architecture decisions and the compliance design. `scripts/context.py` and `.githooks/` keep the context files current.

## License
MIT, see `LICENSE`.
