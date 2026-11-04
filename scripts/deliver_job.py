#!/usr/bin/env python3
"""§17.1905 — write a job's code into this checkout, for the pull request that delivers it into the engine.

    python3 scripts/deliver_job.py <job_id> [--lab <wiring_module>] [--base-url http://localhost:8000]

Reads ``GET /jobs/{id}/code?content=true`` (the engine's own export: each file as its last finished step left it),
writes every file at its path, and lists the job's wiring module in ``app/labs.py`` so the engine starts the lab
(``install(app)``) when it starts. It writes files and nothing else -- branch, commit and pull request are the
operator's (or their agent's), and the review is theirs. The API key is read from ``SCAFFOLD_API_KEY`` or ``.env``."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SAFE = re.compile(r"^(?:app/modules/[a-z_][a-z0-9_]*\.py|tests/test_[a-z0-9_]+\.py|alembic/versions/\w+\.py)$")


def _key() -> str:
    if os.environ.get("SCAFFOLD_API_KEY"):
        return os.environ["SCAFFOLD_API_KEY"]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("SCAFFOLD_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("no SCAFFOLD_API_KEY in the environment or .env")


def list_lab(labs_py: str, lab: str) -> str:
    """`app/labs.py` with `lab` in its LABS tuple (once; the tuple's other entries kept, in order)."""
    m = re.search(r"^LABS: tuple\[str, \.\.\.\] = \((.*?)\)$", labs_py, re.M | re.S)
    if not m:
        raise ValueError("app/labs.py has no `LABS: tuple[str, ...] = (...)` line")
    have = re.findall(r"[\"'](\w+)[\"']", m.group(1))
    if lab in have:
        return labs_py
    items = [*have, lab]
    body = "(" + ", ".join(f'"{x}"' for x in items) + ("," if len(items) == 1 else "") + ")"
    return labs_py[:m.start()] + f"LABS: tuple[str, ...] = {body}" + labs_py[m.end():]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("job_id")
    ap.add_argument("--lab", help="the wiring module to list in app/labs.py (default: the one the export names)")
    ap.add_argument("--base-url", default=os.environ.get("SCAFFOLD_URL", "http://localhost:8000"))
    a = ap.parse_args()
    req = urllib.request.Request(f"{a.base_url}/jobs/{a.job_id}/code?content=true", headers={"X-API-Key": _key()})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    for f in out["files"]:
        if not _SAFE.match(f["path"]):
            sys.exit(f"refusing to write {f['path']!r}: not a job module, test or revision")
        dest = ROOT / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"])
        print(f"wrote {f['path']}  ({f['lines']} lines, step {f['step']})")
    lab = a.lab or (out["wiring"][0] if len(out["wiring"]) == 1 else None)
    if lab:
        labs = ROOT / "app" / "labs.py"
        labs.write_text(list_lab(labs.read_text(), lab))
        print(f"listed {lab} in app/labs.py")
    else:
        print(f"no single wiring module (install(app)): {out['wiring'] or 'none'} -- app/labs.py left as it is; "
              "pass --lab once the job has one", file=sys.stderr)
    print(f"tables: {', '.join(out['tables']) or '-'}; secrets read: {', '.join(out['secrets']) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
