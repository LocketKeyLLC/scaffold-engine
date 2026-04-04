#!/usr/bin/env python3
"""Patch dag_viewer.py: add api_key + orchestrator_url valves, add self.id,
replace hardcoded URL, inject auth headers."""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "dag_viewer.py"

with open(PATH, "r") as f:
    src = f.read()

original = src

# --- 1. Add api_key and orchestrator_url to Valves ---
OLD_VALVES = """class Pipeline:
    class Valves(BaseModel):
        pass
    def __init__(self):
        self.name = "DAG Viewer"
        self.valves = self.Valves()"""
NEW_VALVES = """class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
    def __init__(self):
        self.id = "dag_viewer"
        self.name = "DAG Viewer"
        self.valves = self.Valves()"""
if OLD_VALVES in src:
    src = src.replace(OLD_VALVES, NEW_VALVES, 1)

# --- 2. Remove module-level ORCHESTRATOR_URL ---
OLD_CONST = 'ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"\n'
if OLD_CONST in src:
    src = src.replace(OLD_CONST, "", 1)

# --- 3. Replace remaining ORCHESTRATOR_URL refs with self.valves.orchestrator_url ---
if "ORCHESTRATOR_URL" in src:
    src = src.replace("ORCHESTRATOR_URL", "self.valves.orchestrator_url")

# --- 4. Inject auth headers into requests.get call ---
OLD_GET = """            r = requests.get(
                f"{self.valves.orchestrator_url}/dag/{job_id}",
                timeout=15,
            )"""
NEW_GET = """            r = requests.get(
                f"{self.valves.orchestrator_url}/dag/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=15,
            )"""
if OLD_GET in src:
    src = src.replace(OLD_GET, NEW_GET, 1)

# --- 5. Update connection error message ---
OLD_CONN = """            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}.\""""
NEW_CONN = """            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. Is it running?\""""
# Only apply if the shorter version exists (without "Is it running?")
if OLD_CONN in src and "Is it running?" not in src.split("Cannot reach")[1].split("\n")[0]:
    src = src.replace(OLD_CONN, NEW_CONN, 1)

if src == original:
    print("SKIP: dag_viewer.py already patched")
else:
    with open(PATH, "w") as f:
        f.write(src)
    print("OK: dag_viewer.py patched")
