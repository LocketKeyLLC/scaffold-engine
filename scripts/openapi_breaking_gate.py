#!/usr/bin/env python3
"""§17.1070 — the OpenAPI breaking-change gate (wraps oasdiff).

Runs `oasdiff breaking` with `docs/openapi-severity.txt` and then applies
`docs/openapi-breaking-allow.json`: a list of {"id", "text_contains",
"reason", "expires"} entries for changes that are DELIBERATE contract
corrections. An entry is honoured only until its `expires` date (ISO) so a
waiver cannot silently outlive the PR it was written for. Anything else at
ERR fails the gate. Usage: openapi_breaking_gate.py <base.json> <head.json>
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEVERITY = ROOT / "docs" / "openapi-severity.txt"
ALLOW = ROOT / "docs" / "openapi-breaking-allow.json"


def main() -> int:
    base, head = sys.argv[1], sys.argv[2]
    cmd = ["oasdiff", "breaking", base, head, "--format", "json"]
    if SEVERITY.exists():
        cmd += ["--severity-levels", str(SEVERITY)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode not in (0, 1) and not out.stdout.strip():
        print(out.stderr.strip()[:800] or f"oasdiff exit {out.returncode}")
        return 1
    changes = json.loads(out.stdout or "[]") or []
    allow = []
    if ALLOW.exists():
        today = dt.date.today().isoformat()
        for a in json.loads(ALLOW.read_text()):
            if a.get("expires", "0000") >= today:
                allow.append(a)
    errors, waived = [], []
    for c in changes:
        if c.get("level") != 3:  # oasdiff: 3 = ERR, 2 = WARN, 1 = INFO
            continue
        text = c.get("text") or ""
        if any(a["id"] == c.get("id") and a.get("text_contains", "") in text for a in allow):
            waived.append(c)
        else:
            errors.append(c)
    for c in errors:
        print(f"  ERR [{c.get('id')}] {c.get('operation')} {c.get('path')}: {c.get('text')}")
    if errors:
        print(f"\033[1;31m✗ openapi: {len(errors)} breaking change(s) vs origin/main\033[0m")
        return 1
    tail = f" \033[2m· {len(waived)} waived by docs/openapi-breaking-allow.json\033[0m" if waived else ""
    print(f"\033[1;32m✓ openapi: no breaking changes vs origin/main\033[0m{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
