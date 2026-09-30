"""§17.1209 — opening a job is a read.

The console resolved a job's assist session with `POST /assist/start`, on the
comment that it was "idempotent, unique-per-job". It is idempotent about
*creating* a session and about nothing else: the resume path runs §17.1052's
stranded-session repair, which finalizes a session whose steps are all terminal,
and §17.1208 showed what that then did to the job behind it.

Live, on the operator's job — from the engine's own log, triggered by nothing
but a page load:

    assist_session_started … pending=0 reopened=False prev_status=blocked
    assist_session_completed …
    POST /assist/start status=200

`blocked` → `completed`, over 21 pending and 2 failed nodes. Looking at the job
finished it.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app/ui/static"


def test_the_hub_resolves_the_session_with_a_read():
    src = (STATIC / "views/job_hub.js").read_text()
    blk = src[src.index("function renderRun("):src.index("// ── The stage strip")]
    assert 'api.get(`/assist/for-job/${encodeURIComponent(jobId)}`)' in blk, blk
    assert "/assist/start" not in blk.replace("`POST /assist/start`", ""), \
        "mounting the Run tab must not POST"


def test_every_remaining_start_is_behind_a_click():
    """A POST that starts work is fine — from a button. The test is that none
    of them run on mount."""
    for name in ("views/job_hub.js", "views/assist.js", "views/theater.js",
                 "views/flow_guide.js", "exec_mode.js"):
        src = (STATIC / name).read_text()
        for m in re.finditer(r'post\("/assist/start"', src):
            # walk back to the enclosing function and require a click path
            head = src[max(0, m.start() - 1200):m.start()]
            assert ("addEventListener(\"click\"" in head or "onClick:" in head
                    or "function startAssist" in head or "export async function startAssistFor" in head), \
                f"{name}: a /assist/start with no click above it"


@pytest.mark.asyncio
async def test_the_resolver_does_not_write():
    """Its whole reason for existing. A SELECT and nothing else."""
    import app.routers.assist as ar
    src = inspect.getsource(ar.assist_session_for_job)
    low = src.lower()
    for forbidden in ("update ", "insert ", "delete ", "db.commit", "start_assist_session",
                      "_maybe_finalize_session"):
        assert forbidden not in low, f"{forbidden!r} in a route that only resolves"
    assert "select id, status, current_node_key from assist_sessions" in low


def test_the_resolver_is_declared_before_the_catch_all():
    """FastAPI matches in declaration order; after `/assist/{session_id}` this
    route would never be reached — the §17.626 trap."""
    src = (Path(__file__).resolve().parents[1] / "app/routers/assist.py").read_text()
    assert src.index('@router.get("/assist/for-job/{job_id}")') < \
        src.index('@router.get("/assist/{session_id}")')


def test_the_resolver_checks_ownership():
    import app.routers.assist as ar
    assert "assert_visible" in inspect.getsource(ar.assist_session_for_job)
