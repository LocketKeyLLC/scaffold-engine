"""§17.899 — an operator DENIAL reopens the step the engine wrongly closed.

The missing half of §17.890. That change let the operator's completion claim
outrank the verifier — correct, since the verifier cannot see their machine.
But it gave a claim about the WRONG THING the same power, and nothing could
take it back.

Live incident (HomeLab session, 2026-08-31 23:13): the operator wrote
"It worked Ubuntu Server is now downloading!" — a genuine claim, about the OS
ISO. It landed on T23 "Install PalWorld server" and closed it with that text as
the node's output. 62 seconds later: "But we have ONLY installed the ubuntu
server and have not installed anything else." That correction was correctly NOT
read as a claim — and then nothing listened for it. T23 stayed `done`, the
PalWorld install work silently migrated into T24 "Configure PalWorld service",
and T24 could never satisfy its own goal. It churned for 22 hours.

Two layers, tested here:
1. `assist_policy.looks_like_completion_denial` — the deterministic detector.
2. `assist_agent.reopen_denied_step` — the bounded plan mutation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent
from app.modules import assist_policy as P


# ── 1. the denial detector ───────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    # The verbatim live message that was ignored.
    "But we have ONLY installed the ubuntu server and have not installed anything else.",
    "that isn't done",
    "this isn't finished",
    "it wasn't installed",
    "This step was not finished.",
    "we haven't done that yet",
    "I haven't configured it",
    "nothing was installed",
    "nothing else has been done",
    "I did not configure it",
    "not done yet",
    "Not done yet.",
    "We only installed the OS.",
    "it's still not complete",
    "that step is not done",
])
def test_denial_positive(msg):
    assert P.looks_like_completion_denial(msg) is True


@pytest.mark.parametrize("msg", [
    "",
    # The claim that CAUSED the bug must never read as its own denial.
    "It worked Ubuntu Server is now downloading!",
    "done",
    "I installed it",
    # Questions are not denials.
    "did we install that?",
    "how do I know if it is done?",
    "is that step done",
    # Pasted evidence goes down the evidence path.
    "root@pve:~# qm start 106",
    # Error reports are the §17.874 fix path — the work HAPPENED and broke.
    "command not found: pct",
    "It failed with permission denied",
    "Traceback (most recent call last)",
    # Work in flight is a progress report about the CURRENT step, not a denial
    # that a CLOSED one happened.
    "the download is not finished yet, still going",
    "it is still downloading",
    "apt is currently installing",
    # Unrelated traffic.
    "what is next",
    "I want to build a markdown linter",
])
def test_denial_negative(msg):
    assert P.looks_like_completion_denial(msg) is False


def test_denial_and_claim_are_mutually_exclusive_on_the_live_pair():
    """The two messages that produced the incident, classified correctly."""
    claim = "It worked Ubuntu Server is now downloading!"
    denial = ("But we have ONLY installed the ubuntu server and have not "
              "installed anything else.")
    assert P.looks_like_completion_claim(claim) is True
    assert P.looks_like_completion_denial(claim) is False
    assert P.looks_like_completion_claim(denial) is False
    assert P.looks_like_completion_denial(denial) is True


# ── 2. the bounded reopen ────────────────────────────────────────────────────

_DENIAL = "we have not installed anything else"


def _db(*, session=("job-1", "active"), committed=("T23", 60.0),
        turns_since=1, node=("Install PalWorld server", "It worked")):
    """A db whose execute() returns each queried row in call order:
    session → most-recent-committed step → operator turns since → dag node."""
    db = MagicMock()
    results = []

    def _row(mapping):
        r = MagicMock()
        r.mappings.return_value.first.return_value = mapping
        r.scalar.return_value = mapping
        return r

    results.append(_row({"job_id": session[0], "status": session[1]} if session else None))
    results.append(_row(
        {"node_key": committed[0], "committed_at": "2026-08-31T23:13:18Z",
         "age_s": committed[1]} if committed else None))
    results.append(_row(turns_since))
    results.append(_row({"title": node[0], "output_text": node[1]} if node else None))
    # Any further calls are the UPDATEs.
    db.execute = AsyncMock(side_effect=results + [_row(None)] * 8)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_reopens_the_recently_committed_step():
    db = _db()
    out = await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db)
    assert out is not None
    assert out["node_key"] == "T23"
    assert out["title"] == "Install PalWorld server"
    db.commit.assert_awaited()
    # The mirror invariant: dag_nodes AND assist_steps AND the session pointer.
    sql = " ".join(str(c.args[0]) for c in db.execute.await_args_list)
    assert "UPDATE dag_nodes" in sql
    assert "UPDATE assist_steps" in sql
    assert "UPDATE assist_sessions" in sql
    # The bogus node output must be cleared, not left to poison the digest.
    assert "output_text=NULL" in sql.replace(" = ", "=")
    # Stale guidance is dropped so the reopened step regenerates (§17.894).
    assert "guidance_status='none'" in sql.replace(" = ", "=")


@pytest.mark.asyncio
async def test_non_denial_is_a_noop():
    db = _db()
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message="It worked!", db=db) is None
    db.execute.assert_not_awaited()   # short-circuits before touching the DB
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_commit_is_not_reopened():
    """A denial hours later is about something else — reopening is a mutation."""
    db = _db(committed=("T23", 99_999.0))
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db) is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_skipped_once_the_operator_has_moved_on():
    db = _db(turns_since=9)
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db) is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_committed_step_is_a_noop():
    db = _db(committed=None)
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db) is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_session_is_a_noop():
    db = _db(session=("job-1", "completed"))
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db) is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_valve_off_is_a_noop():
    db = _db()
    with patch("app.config.settings.assist_denial_reopen_enabled", False):
        assert await assist_agent.reopen_denied_step(
            session_id="s1", message=_DENIAL, db=db) is None
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_error_fails_soft():
    """A reopen that blows up must never trap the operator's turn."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    db.commit = AsyncMock()
    assert await assist_agent.reopen_denied_step(
        session_id="s1", message=_DENIAL, db=db) is None
