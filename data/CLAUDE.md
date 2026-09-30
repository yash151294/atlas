# Development data root

`data/` mirrors the lakehouse layout: `raw/` (files as received and the Salesforce export), `bronze/`, `silver/`, `gold/` parquet, `tables/` Delta serving tables, the run manifest, evals outputs (`evals_snapshot.json`, `retrieval_eval.json`, `faithfulness_eval.json`) and the JSONL logs. Committed: `world.json` (the synthetic world), `build_world.py` (a deterministic generator of contributors, sources, projects and transcript assignments), `generate.py` (Claude writes the transcripts and reports, with labelled spans into `labels/`; `--render` rebuilds `raw/` from the labels with no model calls, which is how a fresh clone gets its documents), and `labels/` (the evals' ground truth). Everything else is produced by the pipeline and ignored by git; `fabric/deploy.py --data` seeds OneLake from here on a first deploy only. Regenerating the corpus costs model calls and changes the ground truth, so the evals are re-run after it.
<!-- generated:modules data -->
- `data/build_world.py` (140 lines): Deterministic synthetic world for the demo. Every person, company and project is
- `data/generate.py` (135 lines): Generate synthetic interview transcripts and client deliverables with Claude
<!-- /generated -->
