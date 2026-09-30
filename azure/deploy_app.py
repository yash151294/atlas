"""Deploy the app to Azure Container Apps in UK South, by script. Idempotent: re-run to roll a new image or change settings.
Prereq: `az login` (the subscription) and the Fabric workspace. Steps: build the linux/amd64 image locally with
docker buildx and push it to Azure Container Registry (ACR Tasks were not permitted on the subscription it was built on), create or update the Container App with the environment and secrets from .env, give it a
system-assigned managed identity, make that identity a Contributor on the Fabric workspace (OneLake, Eventhouse), set the
app's public URL as APP_URL and add it to the Entra app registration's redirect URIs.
Usage: uv run python azure/deploy_app.py [--no-build] [--tag <image tag>]"""
import json, os, subprocess, sys
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
RG, LOC, ENV_NAME, APP, GROUP = "rg-atlas", "uksouth", "atlas-env", "atlas-app", "atlas-fabric-sp"
CONF = dotenv_values(ROOT / ".env")
WS = CONF.get("FABRIC_WORKSPACE_ID")   # the workspace fabric/deploy.py created
SECRETS = ["ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "ENTRA_CLIENT_SECRET", "SESSION_SECRET"]
PLAIN = ["ATLAS_ROOT", "EVENTHOUSE_URI", "EVENTHOUSE_DB", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT", "AZURE_OPENAI_EMBED", "AZURE_REGION", "AZURE_OPENAI_FOR",
         "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID"]
OPTIONAL = ["AZURE_OPENAI_REASONING", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL", "FOUNDRY_DEPLOYMENTS", "FOUNDRY_REGION"]   # forwarded when set in .env


def az(*args, check=True, capture=True):
    r = subprocess.run(["az", *args], text=True, capture_output=capture)
    if check and r.returncode:
        raise SystemExit(f"az {' '.join(args[:3])} failed:\n{((r.stderr or r.stdout) or '')[-1500:]}")
    return (r.stdout or "").strip()


def main():
    missing = [k for k in SECRETS + PLAIN + ["FABRIC_WORKSPACE_ID"] if not CONF.get(k)]
    if missing: raise SystemExit(f".env is missing {missing}")
    # registry and environment (created once); the image is built locally for linux/amd64 and pushed: the subscription it was built on
    # was not permitted to run ACR Tasks (remote builds), so 'az containerapp up --source' cannot be used
    acr = az("acr", "list", "-g", RG, "--query", "[0].name", "-o", "tsv") or az("acr", "create", "-g", RG, "-n", f"atlas{os.urandom(3).hex()}acr", "--sku", "Basic", "--location", LOC, "--query", "name", "-o", "tsv")
    server = f"{acr}.azurecr.io"
    if az("containerapp", "env", "show", "-n", ENV_NAME, "-g", RG, "--query", "name", "-o", "tsv", check=False) != ENV_NAME:
        az("containerapp", "env", "create", "-n", ENV_NAME, "-g", RG, "--location", LOC, "-o", "none")
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else __import__("time").strftime("%Y%m%d-%H%M")
    image = f"{server}/atlas:{tag}"
    if "--no-build" not in sys.argv:
        print("building linux/amd64 image locally and pushing to", server, "(several minutes)...", flush=True)
        az("acr", "login", "-n", acr, "-o", "none")
        builder = subprocess.check_output(["docker", "context", "show"], text=True).strip()   # the daemon's own builder (named after the context), where Rosetta runs amd64 stages
        r = subprocess.run(["docker", "buildx", "build", "--builder", builder, "--platform", "linux/amd64", "-t", image, "--push", str(ROOT)], text=True)
        if r.returncode: raise SystemExit("image build or push failed")
    exists = az("containerapp", "show", "-n", APP, "-g", RG, "--query", "name", "-o", "tsv", check=False) == APP
    if not exists:
        # the app is created with its identity and pulls the image with that identity (the CLI grants AcrPull on the registry)
        az("containerapp", "create", "-n", APP, "-g", RG, "--environment", ENV_NAME, "--image", image, "--registry-server", server, "--registry-identity", "system",
           "--system-assigned", "--ingress", "external", "--target-port", "8000", "--cpu", "2", "--memory", "4Gi", "--min-replicas", "1", "--max-replicas", "1", "-o", "none")
    elif "--no-build" not in sys.argv:
        az("containerapp", "update", "-n", APP, "-g", RG, "--image", image, "-o", "none")
    fqdn = az("containerapp", "show", "-n", APP, "-g", RG, "--query", "properties.configuration.ingress.fqdn", "-o", "tsv")
    url = f"https://{fqdn}"
    # identity and Fabric access before the revision that serves production data
    principal = az("containerapp", "identity", "assign", "-n", APP, "-g", RG, "--system-assigned", "--query", "principalId", "-o", "tsv")
    print("app", url, "| image", image, "| managed identity", principal, flush=True)
    # Fabric admits service principals only where the tenant setting allows: a security group holding just this identity,
    # and the setting 'Service principals can use Fabric APIs' enabled for that group (least privilege, reversible)
    group = az("ad", "group", "list", "--display-name", GROUP, "--query", "[0].id", "-o", "tsv") or az("ad", "group", "create", "--display-name", GROUP, "--mail-nickname", GROUP, "--description", "Service principals allowed to use Fabric APIs and OneLake for Atlas", "--query", "id", "-o", "tsv")
    if az("ad", "group", "member", "check", "-g", group, "--member-id", principal, "--query", "value", "-o", "tsv") != "true":
        az("ad", "group", "member", "add", "-g", group, "--member-id", principal, "-o", "none"); print("managed identity added to group", GROUP)
    sys.path.insert(0, str(ROOT / "fabric")); sys.argv = ["deploy.py"]; import deploy
    f = deploy.Fabric()
    setting = next((t for t in f.call("GET", "/admin/tenantsettings").get("tenantSettings", []) if t["settingName"] == "ServicePrincipalAccessGlobalAPIs"), {})
    groups = setting.get("enabledSecurityGroups") or []
    if not setting.get("enabled") or not any(g.get("graphId") == group for g in groups):
        f.call("POST", "/admin/tenantsettings/ServicePrincipalAccessGlobalAPIs/update", json={"enabled": True, "enabledSecurityGroups": [{"graphId": group, "name": GROUP}] + [g for g in groups if g.get("graphId") != group]})
        print("tenant setting 'Service principals can use Fabric APIs' enabled for group", GROUP, "(propagation can take a few minutes)")
    # the identity must be able to read the lakehouse and query the Eventhouse: a Contributor on the Fabric workspace
    if not any((a.get("principal") or {}).get("id") == principal for a in f.paged(f"/workspaces/{WS}/roleAssignments")):
        try:
            f.call("POST", f"/workspaces/{WS}/roleAssignments", json={"principal": {"id": principal, "type": "ServicePrincipal"}, "role": "Contributor"}); print("workspace role: Contributor granted to the managed identity")
        except Exception as e:
            raise SystemExit(f"workspace role assignment failed; the deployed app would not be able to read the lakehouse: {str(e)[:400]}")
    # the app's public callback on the Entra registration, and sign-in limited to users assigned to the app (you, to start)
    app_id = CONF["ENTRA_CLIENT_ID"]
    uris = json.loads(az("ad", "app", "show", "--id", app_id, "--query", "web.redirectUris", "-o", "json") or "[]") or []
    cb = f"{url}/auth/callback"
    if cb not in uris:
        az("ad", "app", "update", "--id", app_id, "--web-redirect-uris", *uris, cb, "-o", "none"); print("redirect URI added:", cb)
    sp = az("ad", "sp", "show", "--id", app_id, "--query", "id", "-o", "tsv", check=False) or az("ad", "sp", "create", "--id", app_id, "--query", "id", "-o", "tsv")
    az("ad", "sp", "update", "--id", sp, "--set", "appRoleAssignmentRequired=true", "-o", "none")
    me = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    assigned = json.loads(az("rest", "--method", "GET", "--url", f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp}/appRoleAssignedTo", "-o", "json"))["value"]
    if not any(a.get("principalId") == me for a in assigned):
        az("rest", "--method", "POST", "--url", f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp}/appRoleAssignedTo", "--headers", "Content-Type=application/json",
           "--body", json.dumps({"principalId": me, "resourceId": sp, "appRoleId": "00000000-0000-0000-0000-000000000000"}), "-o", "none")
        print("sign-in restricted to assigned users; the signed-in account assigned")
    # now the settings and secrets: this update creates the revision that serves production data
    env = [f"{k}={CONF[k]}" for k in PLAIN + OPTIONAL if CONF.get(k)] + [f"{k}=secretref:{k.lower().replace('_', '-')}" for k in SECRETS] + [f"APP_URL={url}"]
    az("containerapp", "secret", "set", "-n", APP, "-g", RG, "--secrets", *[f"{k.lower().replace('_', '-')}={CONF[k]}" for k in SECRETS], "-o", "none")
    az("containerapp", "update", "-n", APP, "-g", RG, "--cpu", "2", "--memory", "4Gi", "--min-replicas", "1", "--max-replicas", "1", "--set-env-vars", *env, "-o", "none")
    print("done:", url)


if __name__ == "__main__":
    main()
