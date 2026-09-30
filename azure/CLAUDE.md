# Hosting and models: Azure

One resource group, `rg-atlas`, in your own subscription, all created by CLI. `az group delete -n rg-atlas` removes everything; the always-on app costs about $150 a month.

## The app on Azure Container Apps (UK South)
`uv run python azure/deploy_app.py [--no-build] [--tag <tag>]` is idempotent. It builds the linux/amd64 image locally with `docker buildx` (Colima with Rosetta; ACR Tasks were not permitted on the subscription it was built on, so `az containerapp up --source` failed) and pushes it to the resource group's registry (`atlas<hex>acr`, created on first run); creates or updates Container App `atlas-app` in environment `atlas-env` (2 vCPU / 4 GiB, one replica: the cache and research threads live in the process); stores the four secrets (`ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `ENTRA_CLIENT_SECRET`, `SESSION_SECRET`) as Container Apps secrets and the rest as environment variables, with `APP_URL` = the FQDN; gives the app a system-assigned managed identity placed in the Entra group `atlas-fabric-sp`, for which the Fabric tenant setting "Service principals can use Fabric APIs" is enabled, and makes it a Contributor on the workspace; adds `https://<fqdn>/auth/callback` to the Entra app `atlas-app` and restricts sign-in to assigned users. `--no-build` re-applies settings only. `.env` also needs `FABRIC_WORKSPACE_ID` (the workspace `fabric/deploy.py` created). The script prints the app's URL; logs `az containerapp logs show -n atlas-app -g rg-atlas --follow`.

The image (`Dockerfile`): the uv base image, `uv sync --frozen --no-dev`, the spaCy model, the reranker and DuckDB's fts extension baked in so the container needs no egress to become ready, a final `uv sync --inexact` (an exact sync would remove the spaCy model), a non-root user that owns nothing under `/app` (every write goes to OneLake, DuckDB spills to `/tmp`), and `ATLAS_REQUIRE_AUTH=1` so a revision without Entra settings fails closed. One identity path inside the container: delta-rs and the DFS API with the azure-identity token; DuckDB never talks to storage.

## CI deploy (every merge to main)
The `deploy` job in `.github/workflows/ci.yml` builds on the runner, pushes the image tagged with the commit, runs `az containerapp update --image`, and fails unless a Healthy active revision runs that image within three minutes. Identity: an Entra app registration (`atlas-github-deploy`) with a federated credential whose subject is `repo:<owner>@<owner id>/<repo>@<repo id>:ref:refs/heads/main` (GitHub stamps the owner and repository ids into the subject; the plain form fails with AADSTS700213); roles AcrPush on the registry and Container Apps Contributor on the one app. GitHub holds one secret, `AZURE_CLIENT_ID`, and five repository variables: `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`, `RESOURCE_GROUP`, `CONTAINER_APP`. Without `AZURE_SUBSCRIPTION_ID` the job is skipped, so a fork runs the tests only. CI never touches settings or secrets, and pushes that change only Markdown or `docs/` skip it.

## Models in the tenancy
| resource | region | purpose |
|---|---|---|
| an AI Services account | UK South | `atlas-embed` = text-embedding-3-small (index embeddings); `atlas-chat` = gpt-5-mini (answers, planning, the gate's second model) |
| a Foundry resource | Sweden Central | Claude Sonnet 5 and Opus 5 deployments `atlas-sonnet`, `atlas-opus` (`deploy_sonnet.json`, `deploy_opus.json`), blocked on Anthropic quota (0 TPM in every region checked on 13 Sep 2026) |

`atlas/llm_azure.py` is plain REST (no `openai` SDK: its `aiohttp` could not be upgraded in the Fabric runtime). `.env` holds `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_CHAT`, `AZURE_OPENAI_EMBED`, `AZURE_REGION` and `AZURE_OPENAI_FOR`, the purposes routed to Azure (`detect` was tried: recall 86.7% against 93 to 95%, one transcript blocked by the content filter; reverted). Extraction stays on the first-party Anthropic API until Foundry quota arrives; then `ANTHROPIC_FOUNDRY_RESOURCE`, `ANTHROPIC_FOUNDRY_API_KEY` and `FOUNDRY_DEPLOYMENTS=claude-opus-5=atlas-opus` switch it with no code change. Quota request: the Foundry portal (Operate → Quota → Request quota) or https://aka.ms/oai/stuquotarequest, one model per submission, 40 capacity units. Limits: `atlas-chat` is 100 requests and 100k tokens a minute; `store._Retry` caps a Retry-After at 30 seconds.

"No training" rests on Microsoft product terms for Azure OpenAI (regional processing, nothing shared with OpenAI), Anthropic commercial terms through the Marketplace for Claude on Foundry, and commercial terms on the first-party API. The provider is stamped on every ledger row and shown on the Compliance and Quality pages. Azure AI Search was built, measured at 13 to 18 of 20 agreement, and deleted (ADR 22).

## Modules
<!-- generated:modules azure -->
- `azure/deploy_app.py` (97 lines): Deploy the app to Azure Container Apps in UK South, by script. Idempotent: re-run to roll a new image or change settings
<!-- /generated -->
