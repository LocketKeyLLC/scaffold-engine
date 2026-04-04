#!/usr/bin/env python3
"""Patch gt_browser.py: add api_key valve, inject auth headers into httpx _call()."""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "gt_browser.py"

with open(PATH, "r") as f:
    src = f.read()

original = src

# --- 1. Add api_key to Valves ---
OLD_VALVES = """    class Valves(BaseModel):
        orchestrator_url: str = Field(
            default="http://scaffold-orchestrator:8000",
            description="Scaffold orchestrator base URL",
        )
        timeout: int = Field(default=60, description="Request timeout seconds")"""
NEW_VALVES = """    class Valves(BaseModel):
        api_key: str = Field(default="", description="Scaffold Engine API key (X-API-Key header)")
        orchestrator_url: str = Field(
            default="http://scaffold-orchestrator:8000",
            description="Scaffold orchestrator base URL",
        )
        timeout: int = Field(default=60, description="Request timeout seconds")"""
if OLD_VALVES in src:
    src = src.replace(OLD_VALVES, NEW_VALVES, 1)

# --- 2. Inject auth headers into _call() — GET path ---
OLD_GET = """                if method == "GET":
                    resp = client.get(url, params=params)"""
NEW_GET = """                if method == "GET":
                    resp = client.get(url, params=params, headers={"X-API-Key": self.valves.api_key})"""
if OLD_GET in src:
    src = src.replace(OLD_GET, NEW_GET, 1)

# --- 3. Inject auth headers into _call() — POST path ---
OLD_POST = """                else:
                    resp = client.post(url, json=json_body)"""
NEW_POST = """                else:
                    resp = client.post(url, json=json_body, headers={"X-API-Key": self.valves.api_key})"""
if OLD_POST in src:
    src = src.replace(OLD_POST, NEW_POST, 1)

if src == original:
    print("SKIP: gt_browser.py already patched")
else:
    with open(PATH, "w") as f:
        f.write(src)
    print("OK: gt_browser.py patched")
