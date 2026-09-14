#!/usr/bin/env python3
"""§17.1057 — evaluate `ast-grep scan --json`: errors fail the gate, warnings are
listed as advisory. Fixture hits under tests/fixtures/ are ignored (the pytest
half, tests/test_ast_grep_rules.py, asserts those). Usage: make check-ast-grep."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

BIN = shutil.which("ast-grep") or shutil.which("sg")
if not BIN:
    print("\033[1;31m✗ ast-grep not installed — pip install ast-grep-cli==0.45.3 (or the release binary)\033[0m")
    sys.exit(1)
out = subprocess.run([BIN, "scan", "--json"], capture_output=True, text=True)
if out.returncode not in (0, 1):
    print(out.stderr[-800:])
    sys.exit(1)
hits = [h for h in json.loads(out.stdout or "[]") if not str(h.get("file", "")).startswith("tests/fixtures/")]
errors = [h for h in hits if h.get("severity") == "error"]
warnings = sum(1 for h in hits if h.get("severity") == "warning")
for h in errors:
    print(f"  {h['file']}:{h['range']['start']['line'] + 1} {h['ruleId']} — {h.get('message', '')}")
if errors:
    print(f"\033[1;31m✗ ast-grep: {len(errors)} rule error(s)\033[0m")
    sys.exit(1)
tail = f"\033[2m · {warnings} advisory warning(s) (ast-grep scan)\033[0m" if warnings else ""
print(f"\033[1;32m✓ ast-grep: no rule errors\033[0m{tail}")
