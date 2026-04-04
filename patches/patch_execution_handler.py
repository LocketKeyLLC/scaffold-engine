#!/usr/bin/env python3
"""Patch execution_handler.py: add api_key valve, inject auth headers,
fix retry URL (/exec/retry -> /retry), fix retry response check,
add missing status icons (blocked, completed, cancelled)."""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "execution_handler.py"

with open(PATH, "r") as f:
    src = f.read()

original = src

# --- 1. Add api_key to Valves ---
OLD_VALVES = """    class Valves(BaseModel):
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 310"""
NEW_VALVES = """    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 310"""
if OLD_VALVES in src:
    src = src.replace(OLD_VALVES, NEW_VALVES, 1)

# --- 2. Inject auth headers into all requests calls ---

# _status: requests.get
OLD_STATUS_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                timeout=30
            )"""
NEW_STATUS_GET = """            resp = requests.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )"""
if OLD_STATUS_GET in src:
    src = src.replace(OLD_STATUS_GET, NEW_STATUS_GET, 1)

# _approve: requests.post
OLD_APPROVE_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/execute",
                json={"job_id": job_id},
                timeout=self.valves.request_timeout
            )"""
NEW_APPROVE_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/execute",
                json={"job_id": job_id},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )"""
if OLD_APPROVE_POST in src:
    src = src.replace(OLD_APPROVE_POST, NEW_APPROVE_POST, 1)

# _skip: requests.post
OLD_SKIP_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/skip",
                json={"job_id": job_id, "node_key": node_key},
                timeout=30
            )"""
NEW_SKIP_POST = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/skip",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )"""
if OLD_SKIP_POST in src:
    src = src.replace(OLD_SKIP_POST, NEW_SKIP_POST, 1)

# --- 3. Fix retry URL: /exec/retry -> /retry ---
OLD_RETRY_URL = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/exec/retry",
                json={"job_id": job_id, "node_key": node_key},
                timeout=30
            )"""
NEW_RETRY_URL = """            resp = requests.post(
                f"{self.valves.orchestrator_url}/retry",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )"""
if OLD_RETRY_URL in src:
    src = src.replace(OLD_RETRY_URL, NEW_RETRY_URL, 1)

# --- 4. Fix retry response check: d.get("reset") -> d.get("status") == "reset" ---
OLD_RETRY_CHECK = """            d = resp.json()
            if d.get("reset"):
                return (
                    f"🔄 **Node `{node_key}` reset to pending.**\\n\\n"
                    f"Run `/exec approve {job_id}` to re-execute it."
                )
            else:
                return f"❌ {d.get('error', 'Unknown error')}\""""
NEW_RETRY_CHECK = """            d = resp.json()
            if d.get("status") == "reset":
                return (
                    f"🔄 **Node `{node_key}` reset to pending.**\\n\\n"
                    f"Run `/exec approve {job_id}` to re-execute it."
                )
            else:
                return f"❌ {d.get('error', 'Unknown error')}\""""
if OLD_RETRY_CHECK in src:
    src = src.replace(OLD_RETRY_CHECK, NEW_RETRY_CHECK, 1)

# --- 5. Add missing status icons: blocked, completed, cancelled ---
# In _status method
OLD_ICONS = """            status_icons = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⬜", "skipped": "⏭️", "executing": "🔄", "planning": "📋"}"""
NEW_ICONS = """            status_icons = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⬜", "skipped": "⏭️", "executing": "🔄", "planning": "📋", "blocked": "🚫", "completed": "✅", "cancelled": "🚫"}"""
if OLD_ICONS in src:
    src = src.replace(OLD_ICONS, NEW_ICONS, 1)

if src == original:
    print("SKIP: execution_handler.py already patched")
else:
    with open(PATH, "w") as f:
        f.write(src)
    print("OK: execution_handler.py patched")
