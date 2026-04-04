#!/usr/bin/env python3
"""Patch prompt_inspector.py: add api_key valve, inject auth headers."""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "prompt_inspector.py"

with open(PATH, "r") as f:
    src = f.read()

original = src

# --- 1. Add api_key to Valves ---
OLD_VALVES = """    class Valves(BaseModel):
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 30"""
NEW_VALVES = """    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 30"""
if OLD_VALVES in src:
    src = src.replace(OLD_VALVES, NEW_VALVES, 1)

# --- 2. Inject headers into _list() GET ---
OLD_LIST_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}",
                timeout=self.valves.request_timeout
            )"""
NEW_LIST_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )"""
if OLD_LIST_GET in src:
    # This pattern appears in both _list and _view — replace first occurrence (_list)
    src = src.replace(OLD_LIST_GET, NEW_LIST_GET, 1)

# --- 3. Inject headers into _view() GET ---
# After first replacement, the _view one still matches the OLD pattern
OLD_VIEW_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                timeout=self.valves.request_timeout
            )"""
NEW_VIEW_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )"""
if OLD_VIEW_GET in src:
    src = src.replace(OLD_VIEW_GET, NEW_VIEW_GET, 1)

# --- 4. Inject headers into _save() POST ---
OLD_SAVE_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                json={"prompt": new_prompt},
                timeout=self.valves.request_timeout
            )"""
NEW_SAVE_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                json={"prompt": new_prompt},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )"""
if OLD_SAVE_POST in src:
    src = src.replace(OLD_SAVE_POST, NEW_SAVE_POST, 1)

if src == original:
    print("SKIP: prompt_inspector.py already patched")
else:
    with open(PATH, "w") as f:
        f.write(src)
    print("OK: prompt_inspector.py patched")
