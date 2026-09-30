"""Deploy the MVP to Microsoft Fabric with the public REST API. No portal clicking.
Prereq: `az login` once. Then: uv run python fabric/deploy.py [--workspace "Atlas"] [--data] [--run [notebook ...]] [--from step]
Creates workspace + lakehouse + Eventhouse, uploads the wheel, conf and notebooks, creates the schedules. `--data` also uploads
the laptop's raw files and parquet layers (first deploy, or a deliberate reset: production writes those files, a code deploy must
not). `--run` runs notebooks and polls; `--from resolve` resumes that one run after extraction. Idempotent: re-run safely."""
import base64, json, subprocess, sys, time
from pathlib import Path
import requests

API = "https://api.fabric.microsoft.com/v1"
ROOT = Path(__file__).resolve().parent.parent
WS_NAME = sys.argv[sys.argv.index("--workspace") + 1] if "--workspace" in sys.argv else "Atlas"
LH_NAME = "atlas_lakehouse"


def token(resource):
    return subprocess.check_output(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"], text=True).strip()


class Fabric:
    def __init__(self):
        self.h = {"Authorization": f"Bearer {token('https://api.fabric.microsoft.com')}"}
        self.dfs = {"Authorization": f"Bearer {token('https://storage.azure.com')}"}

    def call(self, method, url, **kw):
        r = requests.request(method, url if url.startswith("http") else API + url, headers=self.h, **kw)
        if r.status_code == 202:   # long-running operation
            loc = r.headers.get("Location"); wait = int(r.headers.get("Retry-After", "3"))
            while True:
                time.sleep(wait); s = requests.get(loc, headers=self.h).json()
                if s.get("status") in ("Succeeded", "Failed", "Cancelled"):
                    if s["status"] != "Succeeded": raise RuntimeError(s)
                    res = requests.get(loc + "/result", headers=self.h)
                    return res.json() if res.ok and res.text else s
        if not r.ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code} {r.text[:400]}")
        return r.json() if r.text else {}

    def paged(self, url):
        out, u = [], url
        while u:
            j = self.call("GET", u); out += j.get("value", []); u = j.get("continuationUri")
        return out

    def workspace(self):
        for w in self.paged("/workspaces"):
            if w["displayName"] == WS_NAME: return w
        caps = [c for c in self.paged("/capacities") if c["state"] == "Active"]
        if not caps: raise SystemExit("No active capacity. Start the Fabric trial first (app.fabric.microsoft.com -> account -> start trial).")
        print("creating workspace on capacity", caps[0]["displayName"])
        return self.call("POST", "/workspaces", json={"displayName": WS_NAME, "capacityId": caps[0]["id"], "description": "Atlas: pseudonymised corpus, contributor network, research desk"})

    def item(self, ws, typ, name, definition=None):
        for it in self.paged(f"/workspaces/{ws}/items?type={typ}"):
            if it["displayName"] == name: return it
        body = {"displayName": name, "type": typ}
        if definition: body["definition"] = definition
        print("creating", typ, name)
        return self.call("POST", f"/workspaces/{ws}/items", json=body)

    def upload(self, ws, lh, rel, local: Path):
        # OneLake speaks the ADLS Gen2 DFS API: create, append, flush
        base = f"https://onelake.dfs.fabric.microsoft.com/{ws}/{lh}/Files/{rel}"
        data = local.read_bytes()
        requests.put(base + "?resource=file", headers=self.dfs).raise_for_status()
        requests.patch(base + "?action=append&position=0", headers={**self.dfs, "Content-Length": str(len(data))}, data=data).raise_for_status()
        requests.patch(base + f"?action=flush&position={len(data)}", headers=self.dfs).raise_for_status()

    def eventhouse(self, ws, name):
        """Fabric's vector store: an Eventhouse with its default KQL database. Returns (query_uri, database_name)."""
        eh = next((e for e in self.paged(f"/workspaces/{ws}/eventhouses") if e["displayName"] == name), None)
        if not eh:
            print("creating Eventhouse", name); eh = self.call("POST", f"/workspaces/{ws}/eventhouses", json={"displayName": name, "description": "vectors for the research desk"})
        eh = self.call("GET", f"/workspaces/{ws}/eventhouses/{eh['id']}")
        dbs = [d for d in self.paged(f"/workspaces/{ws}/kqlDatabases") if (d.get("properties") or {}).get("parentEventhouseItemId") == eh["id"]]
        return eh["properties"]["queryServiceUri"], (dbs[0]["displayName"] if dbs else name)

    def notebook(self, ws, lh, name, ipynb: Path, subst: dict | None = None):
        nb = json.loads(ipynb.read_text())
        for c in nb["cells"]:   # placeholders are replaced inside cell sources, so the substituted text never has to be JSON-safe
            src = "".join(c["source"])
            for k, v in (subst or {}).items(): src = src.replace(k, v)
            c["source"] = src
        nb.setdefault("metadata", {})["dependencies"] = {"lakehouse": {"default_lakehouse": lh, "default_lakehouse_name": LH_NAME, "default_lakehouse_workspace_id": ws}}
        payload = base64.b64encode(json.dumps(nb).encode()).decode()
        definition = {"format": "ipynb", "parts": [{"path": "notebook-content.ipynb", "payload": payload, "payloadType": "InlineBase64"}]}
        existing = next((it for it in self.paged(f"/workspaces/{ws}/items?type=Notebook") if it["displayName"] == name), None)
        if existing:
            self.call("POST", f"/workspaces/{ws}/notebooks/{existing['id']}/updateDefinition", json={"definition": definition}); return existing
        return self.item(ws, "Notebook", name, definition)

    def schedule(self, ws, nb_id, at: str):
        """One daily run of a notebook at HH:MM UTC; idempotent (an existing schedule on the item is left alone)."""
        if self.call("GET", f"/workspaces/{ws}/items/{nb_id}/jobs/RunNotebook/schedules").get("value"):
            return
        self.call("POST", f"/workspaces/{ws}/items/{nb_id}/jobs/RunNotebook/schedules",
                  json={"enabled": True, "configuration": {"type": "Daily", "times": [at], "startDateTime": "2026-09-13T00:00:00", "endDateTime": "2027-09-13T00:00:00", "localTimeZoneId": "UTC"}})
        print("scheduled", nb_id[:8], "daily at", at, "UTC")

    def run_notebook(self, ws, nb_id, attempts=4, parameters: dict | None = None):
        body = {"executionData": {"parameters": {k: {"value": v, "type": "string"} for k, v in parameters.items()}}} if parameters else None
        for attempt in range(attempts):
            r = requests.post(f"{API}/workspaces/{ws}/items/{nb_id}/jobs/instances?jobType=RunNotebook", headers=self.h, json=body)
            r.raise_for_status(); loc = r.headers["Location"]
            while True:
                time.sleep(20); s = requests.get(loc, headers=self.h).json()
                if s.get("status") in ("Completed", "Failed", "Cancelled"): break
            if s["status"] == "Completed" or "TooManyRequestsForCapacity" not in json.dumps(s.get("failureReason", "")):
                return s
            print("  capacity busy (trial SKU); waiting 120s before retry", flush=True); time.sleep(120)
        return s


def main():
    f = Fabric()
    ws = f.workspace()["id"]; print("workspace", WS_NAME, ws)
    lh = f.item(ws, "Lakehouse", LH_NAME)["id"]; print("lakehouse", lh)
    if "--data" in sys.argv:   # the laptop's data as the starting state; never on a code deploy (production writes these files)
        for p in sorted((ROOT / "data/raw").iterdir()):
            if p.suffix in (".docx", ".pdf", ".csv"):
                f.upload(ws, lh, f"raw/{p.name}", p)
        for layer in ("bronze", "silver", "gold"):
            for p in (ROOT / "data" / layer).glob("*.parquet"):
                f.upload(ws, lh, f"{layer}/{p.name}", p)
        seeded = []
        for p in sorted((ROOT / "data").glob("*.json")) + sorted((ROOT / "data").glob("*.jsonl")):   # manifest, evals, logs: seeded once; production writes them and a deploy never overwrites
            if not requests.head(f"https://onelake.dfs.fabric.microsoft.com/{ws}/{lh}/Files/{p.name}", headers=f.dfs).ok:
                f.upload(ws, lh, p.name, p); seeded.append(p.name)
        print("uploaded raw files and parquet layers; seeded", seeded or "nothing (production files kept)")
    for p in (ROOT / "conf").glob("*.yaml"):
        f.upload(ws, lh, f"conf/{p.name}", p)
    subprocess.run(["uv", "build", "--wheel", "-q"], cwd=ROOT, check=True)   # build it here: uploading whatever dist/ happened to hold shipped a two-day-old wheel once, silently
    whl = sorted((ROOT / "dist").glob("atlas-*.whl"), key=lambda p: p.stat().st_mtime)
    if whl: f.upload(ws, lh, f"lib/{whl[-1].name}", whl[-1]); print("uploaded wheel", whl[-1].name)
    uri, db = f.eventhouse(ws, "atlas_eventhouse"); print("eventhouse", uri, db)
    env = {k: v for k, v in (l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
           if k.startswith(("AZURE_", "ANTHROPIC_", "EVENTHOUSE_", "FOUNDRY_"))}   # ponytail: keys travel in the notebook definition; Key Vault references in production
    env.update(EVENTHOUSE_URI=uri, EVENTHOUSE_DB=db)
    nbs = {}
    for nb in sorted((ROOT / "fabric").glob("*.ipynb")):
        nbs[nb.stem] = f.notebook(ws, lh, nb.stem, nb, subst={"__ENV__": json.dumps(env)})["id"]; print("notebook", nb.stem, nbs[nb.stem])
    for it in f.paged(f"/workspaces/{ws}/items?type=Notebook"):
        if it["displayName"] not in nbs:
            f.call("DELETE", f"/workspaces/{ws}/items/{it['id']}"); print("removed notebook", it["displayName"])
    for name, at in [("10_pipeline", "02:00"), ("20_retention", "03:00")]:
        f.schedule(ws, nbs[name], at)
    print(f"open: https://app.fabric.microsoft.com/groups/{ws}")
    if "--run" in sys.argv:
        names = []
        for tok in sys.argv[sys.argv.index("--run") + 1:]:
            if tok.startswith("--"): break
            names.append(tok)
        params = {"FROM_STEP": sys.argv[sys.argv.index("--from") + 1]} if "--from" in sys.argv else None   # this run only: the notebook's parameters cell, not its definition
        for name in names or ["10_pipeline"]:
            print("running", name, flush=True); s = f.run_notebook(ws, nbs[name], parameters=params if name == "10_pipeline" else None); print("  ", name, s.get("status"), (s.get("failureReason") or {}).get("message", "")[:300], flush=True)


if __name__ == "__main__":
    main()
