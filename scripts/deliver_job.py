#!/usr/bin/env python3
"""§17.1905 — write a job's code into this checkout, for the pull request that delivers it into the engine.

    python3 scripts/deliver_job.py <job_id> [--lab <wiring_module>] [--full] [--base-url http://localhost:8000]

Reads ``GET /jobs/{id}/code?content=true`` (the engine's own export: each file as its last finished step left it),
writes every file at its path, and lists the job's wiring module in ``app/labs.py`` so the engine starts the lab
(``install(app)``) when it starts. §17.1932 — its tests go under ``tests/labs/<lab>/`` with a ``conftest.py`` that
empties the job's tables before each test, as the engine's sandbox ran them (§17.1819). It writes files and nothing else -- branch, commit and pull request are the
operator's (or their agent's), and the review is theirs. §17.1988 — a job delivered before writes only what its steps
changed since that delivery (merged onto the reviewed copy here); every other file is kept as the review left it,
unless ``--full``. The API key is read from ``SCAFFOLD_API_KEY`` or ``.env``."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SAFE = re.compile(r"^(?:app/modules/[a-z_][a-z0-9_]*\.py|tests/test_[a-z0-9_]+\.py|alembic/versions/\w+\.py"
                   # §17.1985 — a static file beside the code: a page template, the lab's own asset, its vendor manifest
                   r"|app/(?:static|templates)/[a-z][a-z0-9_]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:html|css|js|json|svg))$")
#: §17.1985 — `app/static/<lab>/vendor.json`: {"chart.umd.min.js": "https://…/chart.js@4.5.1/dist/chart.umd.min.js"}
_VENDOR_MANIFEST = re.compile(r"^app/static/([a-z][a-z0-9_]*)/vendor\.json$")
_VENDOR_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:js|css|json|svg|woff2?|map)$")
#: a pinned release: `@4.5.1`, `/4.5.1/`, `-4.5.1.`, `v4.5.1` -- a floating `@latest` would change under the review
_PINNED = re.compile(r"[@/v-]\d+\.\d+\.\d+(?:[-+][\w.]+)?(?=[/.]|$)")


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


#: §17.1932 — the sandbox's guarantee, delivered with the lab's tests: each starts with the job's tables empty
_CONFTEST = '''"""The {lab} lab's tests, delivered by `scripts/deliver_job.py` (§17.1932).

Each test starts with the lab's own tables empty, as the engine's sandbox ran them (§17.1819): the tests insert the
rows they need and count what the code wrote. The tables are the job's (its Alembic revision's), never the engine's."""
from __future__ import annotations

import asyncio

import pytest

TABLES = {tables!r}


async def _empty() -> None:
    import asyncpg

    from app.config import settings
    conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("TRUNCATE " + ", ".join(f'"{{t}}"' for t in TABLES) + " RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _lab_tables_empty():
    if TABLES:
        asyncio.run(_empty())
    yield
'''


def lab_dir(modules: list[str], lab: str | None) -> str:
    """§17.1932 — the lab's tests directory name: the prefix its modules share (`paper_…` -> `paper`), else the lab."""
    heads = {m.split("_", 1)[0] for m in modules}
    if len(heads) == 1 and modules:
        return heads.pop()
    return re.sub(r"[^a-z0-9_]", "_", (lab or "lab").lower())


def vendored(manifest_path: str, content: str) -> list[tuple[str, str]]:
    """§17.1985 — what a lab's vendor manifest asks for: (destination, url) per third-party file, each at a pinned
    https release, written under `app/static/<lab>/vendor/`. The job's step cannot write a minified library; it names
    the file, and the delivery fetches it for the review. Raises ValueError on a name, URL or version it refuses."""
    m = _VENDOR_MANIFEST.match(manifest_path)
    if not m:
        return []
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path}: not an object of file name -> URL")
    out: list[tuple[str, str]] = []
    for name, url in data.items():
        if not _VENDOR_NAME.match(str(name)):
            raise ValueError(f"{manifest_path}: refusing file name {name!r}")
        if not isinstance(url, str) or not url.startswith("https://") or not _PINNED.search(url):
            raise ValueError(f"{manifest_path}: {name}: {url!r} is not an https URL of a pinned release")
        out.append((f"app/static/{m.group(1)}/vendor/{name}", url))
    return out


def place(path: str, where: str) -> str:
    """Where a delivered file goes: a test under `tests/labs/<lab>/`, anything else at its own path."""
    return f"tests/labs/{where}/{path[len('tests/'):]}" if path.startswith("tests/test_") else path


def merge3(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """§17.1988 — a line three-way merge: the reviewed copy (`ours`) with what the job's steps changed since the
    delivery (`base` -> `theirs`) carried onto it. Returns (text, clean); changes that touch the same lines of the
    delivered file are left between `<<<<<<< reviewed` / `>>>>>>> job` markers for the review."""
    import difflib
    b, sides = base.splitlines(keepends=True), (ours.splitlines(keepends=True), theirs.splitlines(keepends=True))
    changes = sorted((i1, i2, side, s[j1:j2]) for side, s in enumerate(sides)
                     for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, s, autojunk=False).get_opcodes()
                     if tag != "equal")
    out: list[str] = []
    pos, clean, k = 0, True, 0
    while k < len(changes):
        lo, hi, group = changes[k][0], changes[k][1], [changes[k]]
        k += 1
        while k < len(changes) and changes[k][0] <= hi:        # touching the same delivered lines: one region
            hi = max(hi, changes[k][1])
            group.append(changes[k])
            k += 1
        made = []
        for side in (0, 1):
            at, text = lo, []
            for i1, i2, s, lines in group:
                if s == side:
                    text += b[at:i1] + lines
                    at = i2
            made.append(text + b[at:hi] if any(g[2] == side for g in group) else None)
        out += b[pos:lo]
        if made[0] is None or made[1] is None or made[0] == made[1]:
            out += made[0] if made[1] is None else made[1]
        else:
            clean = False
            out += ["<<<<<<< reviewed\n", *made[0], "=======\n", *made[1], ">>>>>>> job\n"]
        pos = hi
    return "".join(out + b[pos:]), clean


def decide(f: dict, current: str | None, full: bool = False) -> tuple[str, str | None]:
    """§17.1988 — what a delivery does with one exported file, given the file in this checkout (None: absent):
    ("write" | "readded" | "merged" | "conflict", text) or ("kept" | "kept-deleted" | "same", None).

    A job delivered before (the export says what each file did since): a file no step changed since is the reviewed
    copy here, kept whatever the job holds -- live (5b4880c0, 2026-11-21) re-delivering for F13F1/F13F2 reverted
    two edits made by hand in the review of #1161 (a dead `forward_days` back, the `ZERO` start of two Decimal sums
    dropped). A file a step changed since is written, onto the reviewed copy by a three-way merge from the file as
    delivered. A first delivery, or `full`, writes every file as the job holds it.

    §17.1988b — a file the review deleted (delivered, absent here) and no step changed since stays deleted; one a step
    changed since is written back, said so, for the review to decide again."""
    new = f["content"]
    if current == new:
        return "same", None
    since = f.get("since_delivery")
    if full or since is None or since == "new":
        return "write", new
    if since == "unchanged":
        return ("kept-deleted" if current is None else "kept"), None
    if current is None:
        return "readded", new
    base = f.get("delivered_content")
    if base is None or current == base:              # no reviewed edit to carry (or no base to carry it from)
        return "write", new
    text, clean = merge3(base, current, new)
    return ("merged" if clean else "conflict"), text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("job_id")
    ap.add_argument("--lab", help="the wiring module to list in app/labs.py (default: the one the export names)")
    ap.add_argument("--base-url", default=os.environ.get("SCAFFOLD_URL", "http://localhost:8000"))
    ap.add_argument("--full", action="store_true",
                    help="write every file as the job holds it, over the reviewed copies (§17.1988)")
    a = ap.parse_args()
    req = urllib.request.Request(f"{a.base_url}/jobs/{a.job_id}/code?content=true", headers={"X-API-Key": _key()})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    return deliver(out, a.lab, a.full)


def deliver(out: dict, lab_arg: str | None = None, full: bool = False) -> int:
    """Write the export `out` into this checkout (`ROOT`); 1 when a merge left a conflict for the review."""
    lab = lab_arg or (out["wiring"][0] if len(out["wiring"]) == 1 else None)
    where = lab_dir([Path(f["path"]).stem for f in out["files"] if f["path"].startswith("app/modules/")], lab)
    if out.get("delivered_at") and not full:
        print(f"last delivered {out['delivered_at']}: writing what the job's steps changed since; the rest is kept "
              "(--full writes every file as the job holds it)")
    conflicts = []
    for f in out["files"]:
        if not _SAFE.match(f["path"]):
            sys.exit(f"refusing to write {f['path']!r}: not a job module, test or revision")
        dest = ROOT / place(f["path"], where)
        act, body = decide(f, dest.read_text() if dest.exists() else None, full)
        if act == "kept":
            print(f"kept {place(f['path'], where)}: changed in the engine since delivery (reviewed edit), "
                  "not rewritten")
            continue
        if act == "kept-deleted":
            print(f"kept deleted {place(f['path'], where)}: deleted in the engine since delivery (reviewed), "
                  "not re-written")
            continue
        if act == "same":
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)
        if act == "conflict":
            conflicts.append(place(f["path"], where))
        print({"write": "wrote", "readded": "re-added", "merged": "merged", "conflict": "CONFLICT in"}[act]
              + f" {place(f['path'], where)}  ({f['lines']} lines, step {f['step']})"
              + {"write": "", "readded": " -- deleted in review, but a step changed it since; review it again",
                 "merged": " -- the step's change carried onto the reviewed copy",
                 "conflict": " -- the step's change and the reviewed edit overlap; resolve the markers"}[act])
        try:
            fetches = vendored(f["path"], body)
        except ValueError as exc:
            sys.exit(f"refusing the vendor manifest: {exc}")
        for to, url in fetches:                           # §17.1985 — fetched once, here, for the review to read
            with urllib.request.urlopen(url, timeout=120) as v:
                body = v.read()
            (ROOT / to).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / to).write_bytes(body)
            print(f"vendored {to}  ({len(body)} bytes, sha256 {hashlib.sha256(body).hexdigest()}) from {url}")
    if any(f["path"].startswith("tests/test_") for f in out["files"]):
        for pkg in (ROOT / "tests" / "labs", ROOT / "tests" / "labs" / where):
            (pkg / "__init__.py").touch()
        (ROOT / "tests" / "labs" / where / "conftest.py").write_text(
            _CONFTEST.format(lab=where, tables=list(out["tables"])))
        print(f"wrote tests/labs/{where}/conftest.py (empties {', '.join(out['tables']) or 'no tables'} before each test)")
    if lab:
        labs = ROOT / "app" / "labs.py"
        labs.write_text(list_lab(labs.read_text(), lab))
        print(f"listed {lab} in app/labs.py")
    else:
        print(f"no single wiring module (install(app)): {out['wiring'] or 'none'} -- app/labs.py left as it is; "
              "pass --lab once the job has one", file=sys.stderr)
    print(f"tables: {', '.join(out['tables']) or '-'}; secrets read: {', '.join(out['secrets']) or '-'}")
    if conflicts:
        print(f"{len(conflicts)} file(s) left with merge markers: {', '.join(conflicts)}", file=sys.stderr)
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
