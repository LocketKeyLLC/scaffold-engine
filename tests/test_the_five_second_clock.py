"""§17.1244 — three supervised runs died on our own five-second clock.

Each failure read as a flaky helper:

    the connection to pve-runner dropped while `pct start 111` was running
    (MCPError: SSE stream ended without a response)

`pct start 111` (ADD50), `pct start 120` (ADD110) and `pveam update && pveam
available --section system | grep debian-12` (ADD111) all died that way in one
evening. Everything that succeeded was fast: `pct list`, `pct status`, `curl`
against a local API, `pm2 list`.

`streamable_http_client` in this SDK takes no timeout of its own — it uses the
httpx client it is handed, or builds one with httpx's defaults, and httpx2's
default is **5 seconds**. Nothing set it. So the transport's read timeout was 5s
while `ClientSession` was told 60, and any answer slower than five seconds was
lost.

And underneath that, a second mismatch: `run_block` tells the runner it may spend
180 seconds on one approved command, while the engine abandoned the call at
`mcp_call_timeout + 10` = 70 seconds — walking away from a write still running on
the machine.
"""
from __future__ import annotations

import inspect

from app.config import settings
from app.modules import assist_supervised as sup
from app.modules import supervised_runs as sr
from app.modules import mcp_client


def test_the_transport_gets_an_explicit_timeout():
    """The bug was an ABSENT argument, so the test has to be about presence."""
    src = inspect.getsource(mcp_client._open_session)
    assert "httpx2.Timeout(" in src
    assert "read=timeout + 30.0" in src, "the read timeout must follow the call timeout"
    assert "http_client=http_client" in src


def test_both_transport_branches_share_one_client():
    """It was wrong in two places — a headers branch with a bare AsyncClient and a
    no-headers branch with none at all. One client now, so they cannot drift."""
    src = inspect.getsource(mcp_client._open_session)
    assert src.count("streamable_http_client(") == 1, "two call sites drift"
    assert src.count("httpx2.AsyncClient(") == 1
    assert "spec.headers or None" in src, "the headers branch must still pass headers"


def test_no_call_site_relies_on_httpx_defaults():
    """httpx2's default is 5s; a client built without `timeout=` is the bug."""
    src = inspect.getsource(mcp_client._open_session)
    for line in src.splitlines():
        if "httpx2.AsyncClient(" in line:
            assert "timeout=" in line, line


def test_the_engine_allows_at_least_what_it_asks_the_runner_for():
    """The mismatch that cut a 180-second write off at 70. If either number moves,
    this fails — which is the point."""
    assert settings.mcp_call_timeout >= sup.RUN_COMMAND_TIMEOUT_S, (
        f"the engine abandons at {settings.mcp_call_timeout}s a command it lets the "
        f"runner spend {sup.RUN_COMMAND_TIMEOUT_S}s on")


def test_the_outer_wait_for_still_bounds_the_call():
    """Raising the ceiling must not remove the ceiling — a hung server still ends."""
    src = inspect.getsource(mcp_client)
    assert "asyncio.wait_for(_do(), timeout=settings.mcp_call_timeout + 10.0)" in src
    assert settings.mcp_call_timeout <= 600.0


def test_the_runner_timeout_is_named_not_inlined():
    """It was the literal 180 buried in a payload dict, which is why nothing
    noticed it disagreed with the engine's own ceiling."""
    src = inspect.getsource(sup.run_block)
    assert "RUN_COMMAND_TIMEOUT_S" in src
    assert '"timeout_s": 180' not in src


# ── §17.1245: an approval must not be consumed by an attempt that failed ──

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _row(updated_at):
    r = MagicMock()
    r.mappings.return_value.first.return_value = {"updated_at": updated_at}
    return r


@pytest.mark.asyncio
async def test_a_decision_is_stale_once_the_node_has_been_written_since():
    answered = datetime.now(timezone.utc)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_row(answered + timedelta(seconds=5)))
    assert await sr.decision_is_stale(db, "j", "ADD111", {"at": answered.isoformat()}) is True


@pytest.mark.asyncio
async def test_a_decision_still_belongs_to_the_attempt_it_authorised():
    answered = datetime.now(timezone.utc)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_row(answered - timedelta(seconds=5)))
    assert await sr.decision_is_stale(db, "j", "ADD111", {"at": answered.isoformat()}) is False


@pytest.mark.asyncio
async def test_anything_unreadable_counts_as_stale():
    """Asking once more costs a click; the other way round marks a
    machine-changing step done without running it."""
    db = AsyncMock(); db.execute = AsyncMock(return_value=_row(None))
    assert await sr.decision_is_stale(db, "j", "n", {"at": "2026-01-01T00:00:00+00:00"}) is True
    assert await sr.decision_is_stale(db, "j", "n", {}) is True
    assert await sr.decision_is_stale(db, "j", "n", {"at": "not a date"}) is True
    missing = MagicMock(); missing.mappings.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=missing)
    assert await sr.decision_is_stale(db, "j", "n", {"at": datetime.now(timezone.utc).isoformat()}) is True


def test_pending_hands_on_checks_staleness_not_just_presence():
    """Live ADD111: approved, the run died on the five-second clock, the step was
    reset — and it could never be offered for approval again, because the
    approval from the FAILED attempt was still on the job."""
    src = inspect.getsource(sr.pending_hands_on)
    assert "decision_is_stale(" in src
    assert 'if node["node_key"] in decided:' not in src, "presence alone must not skip a node"


def test_there_is_exactly_one_staleness_rule():
    """It lived in execution_agent and only that file used it — which is how
    pending_hands_on came to disagree with it."""
    from app.modules import execution_agent as ea
    assert "supervised_runs import decision_is_stale" in inspect.getsource(ea._decision_is_stale)
    body = open("app/modules/supervised_runs.py").read()
    assert body.count("def decision_is_stale") == 1
    assert "fromisoformat" not in inspect.getsource(ea._decision_is_stale), "the rule must live in one place"
