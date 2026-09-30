# Production data plane: Microsoft Fabric

Everything here is created by `fabric/deploy.py` through the Fabric REST API and the OneLake DFS endpoint; the script is the audit trail. Workspace **Atlas** (a Fabric trial capacity is enough), lakehouse `atlas_lakehouse`, Eventhouse `atlas_eventhouse` (its query URI goes in `.env` as `EVENTHOUSE_URI`). `az login --allow-no-subscriptions` is enough for Fabric.

## What runs where
- **Lakehouse**: the catalog root in production (`ATLAS_ROOT=abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<lakehouse>` in `.env`). Working files under `Files/` (raw, bronze, silver, gold parquet, the manifest, the logs); serving tables as Delta under `Tables/` (`gold_sources`, `gold_edges`, `gold_people`, `gold_contributors`, `gold_chunks`, `gold_net_index`, `silver_entities`, `silver_redacted`), written by the load node with delta-rs, or by Spark when the pipeline runs on the mount.
- **Eventhouse**: retrieval. An external Delta table `gold_chunks` over the lakehouse table (`atlas/eventhouse.py`, idempotent); the hybrid query runs in KQL. No ingestion, no copy. Without `EVENTHOUSE_URI` the app's cache answers the same call.
- **`10_pipeline.ipynb`**, daily 02:00 UTC: the full pipeline on the lakehouse mount with the same wheel the app runs; extraction is incremental, so a quiet night costs nothing. A tagged parameters cell `FROM_STEP` lets one run resume (`deploy.py --run --from resolve`) without touching the schedule.
- **`20_retention.ipynb`**, daily 03:00 UTC: raw files past their deletion date removed, vault rows past expiry dropped, logged.
- **`30_queries.ipynb`**: the network and the gate in plain SQL over the Delta tables.
- **The app** reads the Delta tables into its cache, writes approvals, decisions and logs back to OneLake, and calls the Eventhouse. On a laptop it uses the signed-in CLI identity; in the container, the managed identity, a workspace Contributor.

## Deploy and run
```
uv run python fabric/deploy.py                       # code only: the wheel (uv build), conf, notebooks, schedules; removes notebooks no longer in the repo
uv run python fabric/deploy.py --data                # also seeds OneLake from data/ (first deploy or a deliberate reset; manifest, evals and logs only when absent)
uv run python fabric/deploy.py --run [--from resolve] [20_retention]   # runs a notebook and polls the job
```
`deploy.py` substitutes endpoints and keys from `.env` into the notebooks (`__ENV__`; Key Vault references are the production step). Production writes the working files, so a code deploy never uploads data. Editing a notebook: substitute inside parsed cells, never into the raw ipynb JSON.

## Gotchas learned
- OneLake is the ADLS Gen2 DFS API: blob-style block uploads fail (`IncorrectEndpointError`); `store.py` writes with create + append + flush, and delta-rs needs `use_fabric_endpoint`. A file created through the mount cannot be appended through DFS, so logs are per-writer partitions under `Files/<log>/`, merged on read.
- An API-triggered notebook run that reaches `input()` hangs for ever; a cell that raises fails the run and cancels the Spark session although earlier cells completed. Job status and cancel: `/workspaces/{ws}/items/{nb}/jobs/instances[/{id}/cancel]`; result files are read back over DFS.
- KQL: a column named `project` is written `['project']`; dates compare as `todatetime(doc_date)`, strings do not order. Kusto streaming ingest returned 520; the external Delta table is the answer.
- Never upgrade pandas or pyarrow inside the Spark runtime: the old compiled pyarrow stays loaded and every parquet call fails. The notebooks install the wheel `--no-deps` plus only what the runtime lacks (the Azure OpenAI client is plain REST, so no `openai` or `aiohttp`), force-reinstall `typing_extensions` and reload it; `store.write_table` lets Spark write the Delta tables on the mount through a parquet staged under `Files/tmp/`.
- The trial capacity runs one Spark session at a time; the first cell (Presidio and the spaCy model) takes minutes; `deploy.py --run` waits between notebooks.
- The app process must be restarted after a code deploy: modules imported lazily on first use (the Eventhouse client) will not match a store module already loaded.
- `deploy.py` builds the wheel (`uv build --wheel`) before uploading it. It used to upload whatever `dist/` held, so a deploy could ship a wheel days old without saying so, and the notebooks would run it that night.

## Modules
<!-- generated:modules fabric -->
- `fabric/deploy.py` (159 lines): Deploy the MVP to Microsoft Fabric with the public REST API. No portal clicking
<!-- /generated -->

## Not yet built
OneLake security scoping per engagement; Key Vault references for the keys; a Data Pipeline from SharePoint into `Files/raw`; the vault in its own workspace.
