"""Roots and catalog. Config (conf/) travels with the code; data lives under one root that is a laptop folder in
development and the Fabric lakehouse in production (ATLAS_ROOT=abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<lakehouse>,
or the /lakehouse/default mount inside a Fabric notebook). Working files sit under the data root (data/ or Files/), serving
tables are Delta tables under TABLES (data/tables/ or Tables/)."""
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")   # roots, endpoints and keys; ATLAS_ROOT here makes the lakehouse the default, ATLAS_ROOT=. on the command line is development
ROOT = os.environ.get("ATLAS_ROOT", str(REPO)).rstrip("/")
ONELAKE = ROOT.startswith("abfss://")
MOUNT = ROOT.startswith("/lakehouse/")                                   # inside a Fabric notebook: /lakehouse/default
DATA = f"{ROOT}/Files" if (ONELAKE or MOUNT) else f"{ROOT}/data"
TABLES = os.environ.get("ATLAS_TABLES", f"{ROOT}/Tables" if (ONELAKE or MOUNT) else f"{ROOT}/data/tables")
CONF = Path(os.environ.get("ATLAS_CONF", REPO / "conf"))
CATALOG = yaml.safe_load((CONF / "catalog.yaml").read_text())
PARAMS = yaml.safe_load((CONF / "parameters.yaml").read_text())


def path(name: str) -> str:
    """Catalog dataset name -> location under the data root (a local path string or an abfss URI). Use atlas.store to read and write it."""
    return f"{DATA}/{CATALOG[name]['path']}"
