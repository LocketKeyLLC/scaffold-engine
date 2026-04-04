#!/usr/bin/env python3
"""Patch scaffold_router.py: add api_key valve, move URL to valve, inject auth headers."""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "scaffold_router.py"

with open(PATH, "r") as f:
    src = f.read()

original = src

# --- 1. Add api_key and orchestrator_url to Valves ---
OLD_VALVES = """    class Valves(BaseModel):
        pass"""
NEW_VALVES = """    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000\""""
if OLD_VALVES in src:
    src = src.replace(OLD_VALVES, NEW_VALVES, 1)

# --- 2. Add Field import if not present ---
if "from pydantic import BaseModel" in src and "Field" not in src:
    src = src.replace("from pydantic import BaseModel", "from pydantic import BaseModel, Field", 1)

# --- 3. Remove module-level ORCHESTRATOR_URL ---
OLD_CONST = '\nORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"\n'
if OLD_CONST in src:
    src = src.replace(OLD_CONST, "\n", 1)

# --- 4. Replace all ORCHESTRATOR_URL references with self.valves.orchestrator_url ---
if "ORCHESTRATOR_URL" in src:
    src = src.replace("ORCHESTRATOR_URL", "self.valves.orchestrator_url")

# --- 5. Inject auth headers into all requests calls ---
# POST calls: add headers param
for endpoint in ["/ideas", "/dag", "/execute", "/skip", "/optimize", "/rag"]:
    # Pattern: json=...,\n                    timeout=...
    # We need to add headers= after json=
    pass

# Use a more targeted approach: inject headers dict before each requests call block
# Since all POST calls follow the pattern: requests.post(\n    f"{...}...",\n    json=...,\n    timeout=...,\n)
# And GET call: requests.get(f"{...}/status", timeout=10)

# POST calls - add headers after json= line
OLD_POST_PATTERN = """                    json={"idea": text},
                    timeout=310,"""
NEW_POST_PATTERN = """                    json={"idea": text},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=310,"""
if OLD_POST_PATTERN in src and 'headers={"X-API-Key"' not in src.split("/ideas")[0]:
    src = src.replace(OLD_POST_PATTERN, NEW_POST_PATTERN, 1)

OLD_DAG = """                    json={"job_id": parts[1]},
                    timeout=310,"""
NEW_DAG = """                    json={"job_id": parts[1]},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=310,"""
if OLD_DAG in src:
    src = src.replace(OLD_DAG, NEW_DAG, 1)

OLD_EXECUTE = """                    json={"job_id": parts[1], "skip_verify": False},
                    timeout=310,"""
NEW_EXECUTE = """                    json={"job_id": parts[1], "skip_verify": False},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=310,"""
if OLD_EXECUTE in src:
    src = src.replace(OLD_EXECUTE, NEW_EXECUTE, 1)

OLD_SKIP = """                    json={"job_id": parts[1], "node_key": parts[2]},
                    timeout=30,"""
NEW_SKIP = """                    json={"job_id": parts[1], "node_key": parts[2]},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=30,"""
if OLD_SKIP in src:
    src = src.replace(OLD_SKIP, NEW_SKIP, 1)

OLD_OPTIMIZE = """                    json={"prompt": text, "skip_verify": False},
                    timeout=310,"""
NEW_OPTIMIZE = """                    json={"prompt": text, "skip_verify": False},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=310,"""
if OLD_OPTIMIZE in src:
    src = src.replace(OLD_OPTIMIZE, NEW_OPTIMIZE, 1)

OLD_RAG = """                    json={"query": text, "top_k": 5},
                    timeout=60,"""
NEW_RAG = """                    json={"query": text, "top_k": 5},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=60,"""
if OLD_RAG in src:
    src = src.replace(OLD_RAG, NEW_RAG, 1)

# GET /status
OLD_STATUS = """                r = requests.get(f"{self.valves.orchestrator_url}/status", timeout=10)"""
NEW_STATUS = """                r = requests.get(f"{self.valves.orchestrator_url}/status", headers={"X-API-Key": self.valves.api_key}, timeout=10)"""
if OLD_STATUS in src and "X-API-Key" not in src.split("/status")[0].split("requests.get")[-1]:
    src = src.replace(OLD_STATUS, NEW_STATUS, 1)

# Connection error message - update to use valve URL
OLD_CONN_ERR = """            return f"⚠️ Cannot reach orchestrator at {ORCHESTRATOR_URL}. Is it running?\""""
NEW_CONN_ERR = """            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. Is it running?\""""
# This may already have been replaced by step 4, but just in case
if OLD_CONN_ERR in src:
    src = src.replace(OLD_CONN_ERR, NEW_CONN_ERR, 1)

if src == original:
    print("SKIP: scaffold_router.py already patched")
else:
    with open(PATH, "w") as f:
        f.write(src)
    print("OK: scaffold_router.py patched")
