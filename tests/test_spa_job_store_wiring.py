"""§17.1115 (Phase 1 ledger U-4) — views read a job through the store.

Before: no client store; the plan tab's first paint issued FOUR
`GET /jobs/{id}` (hub, plan ×2, brief panel), the overview two. `store.js`
coalesces and caches; this gate keeps every view on it and keeps the one
invalidation hook (api.js dispatching `scaffold:mutated` after a non-GET) in
place. Static, ci-tier-0.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"
_DIRECT_JOB_GET = re.compile(r"api\.get\(\s*`/jobs/\$\{[^}]+\}`\s*[,)]")


def test_no_view_fetches_a_job_row_directly():
    offenders = []
    for js in sorted((_STATIC / "views").glob("*.js")):
        for lineno, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            if _DIRECT_JOB_GET.search(line):
                offenders.append(f"{js.name}:{lineno}: {line.strip()[:80]}")
    assert not offenders, "views must read a job through jobStore.get(id) (store.js), not api.get:\n  " + "\n  ".join(offenders)


def test_the_store_exists_and_the_api_funnel_announces_mutations():
    store = (_STATIC / "store.js").read_text(encoding="utf-8")
    assert "export function createStore" in store and "export const jobStore" in store
    assert 'addEventListener("scaffold:mutated"' in store and 'addEventListener("scaffold:job-status"' in store
    api = (_STATIC / "api.js").read_text(encoding="utf-8")
    assert 'new CustomEvent("scaffold:mutated"' in api
    assert 'method !== "GET"' in api, "only mutations announce"


def test_the_gate_regex_hits_the_shapes_it_replaced():
    for s in ("api.get(`/jobs/${jobId}`)", "const job = await api.get(`/jobs/${id}`);", "    api.get(`/jobs/${jobId}`),"):
        assert _DIRECT_JOB_GET.search(s), s
    assert not _DIRECT_JOB_GET.search("api.get(`/jobs/${id}/costs`)"), "sub-resources are not job rows"
    assert not _DIRECT_JOB_GET.search("jobStore.get(jobId)")


# ── §17.1122 — shell reads go through the memoized helpers ───────────────────

_DIRECT_SHELL_GET = re.compile(r'api\.get\(\s*"/(status|meta/first-run)"')


def test_no_code_fetches_a_shell_read_directly():
    offenders = []
    for js in sorted(list(_STATIC.glob("*.js")) + list((_STATIC / "views").glob("*.js"))):
        if js.name == "api.js":
            continue
        for lineno, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            if _DIRECT_SHELL_GET.search(line):
                offenders.append(f"{js.name}:{lineno}: {line.strip()[:80]}")
    assert not offenders, "use api.status() / api.firstRun() (memoized, shared across callers):\n  " + "\n  ".join(offenders)


def test_the_api_funnel_memoizes_shell_reads_and_clears_on_writes():
    api = (_STATIC / "api.js").read_text(encoding="utf-8")
    assert "export function memoized(" in api and "export function clearMemo(" in api
    for helper in ("export function health()", "export function status()", "export function firstRun()", "export function accountStatus()"):
        assert helper in api, helper
    body = api[api.index("export async function req("):api.index("export const get =")]
    assert "clearMemo();" in body and 'method !== "GET"' in body, "a write must clear the memo"

