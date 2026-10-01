"""§17.1259 — a step marked finished having done nothing.

The honest answer to a complaint the operator has made all session. A step with
no shell backend writes instructions and the node still goes to `done`. The
`runbook_only` flag exists in the SSE payload; the STATUS does not know, so the
job's counts and every downstream dependency treat instructions as work.

Not cosmetic. ADD116 ("give the media-stack containers working DNS") was marked
done having produced 1,582 characters of prose. DNS stayed broken on all five
containers — and because it was `done`, ADD115 unblocked and ran straight into
the wall ADD116 existed to remove. ADD97 and ADD98 did the same earlier; ADD98
is "prove it end to end: ask for one film and watch it arrive", recorded
finished having proven nothing.

The discriminator is whose machine. Commands for a host the runner can reach
could have been run, and describing them instead is not done. Work elsewhere —
an app on the operator's phone, a router with no API — can only be prose, so the
check stays silent.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import supervised_runs as sr

HOST_PROSE = """## What to do

Set the nameserver on each container.

```bash
pct set 102 --nameserver 192.168.1.30
pct exec 102 -- getent hosts github.com
```
"""
PHONE_PROSE = """## What to do

Open the My Spectrum app, tap Services, then WiFi, and change the DNS field.
"""
RAN = HOST_PROSE + "\n## Executed on pve-runner\n$ pct set 102 --nameserver 192.168.1.30\n"
NODE = {"title": "Give the containers working DNS", "tool": "LLM",
        "description": "set the nameserver on each container"}


def _open_channel():
    return (SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]})


@pytest.mark.asyncio
async def test_prose_with_host_commands_is_not_done():
    with patch.object(sr, "channel", AsyncMock(return_value=_open_channel())), \
         patch.object(sr, "_unmet_for", AsyncMock(return_value=[])):
        r = await sr.wrote_instructions_instead_of_doing_it(HOST_PROSE, NODE, None)
    assert r
    assert "INSTRUCTIONS, not work" in r
    assert "pct set 102 --nameserver 192.168.1.30" in r
    assert "would start from a false premise" in r


@pytest.mark.asyncio
async def test_work_on_the_operators_phone_is_correctly_prose():
    """ADD112's Spectrum-app walkthrough is the right output for its step and
    must never be flagged."""
    with patch.object(sr, "channel", AsyncMock(return_value=_open_channel())):
        assert await sr.wrote_instructions_instead_of_doing_it(PHONE_PROSE, NODE, None) is None


@pytest.mark.asyncio
async def test_a_step_that_actually_ran_is_silent():
    with patch.object(sr, "channel", AsyncMock(return_value=_open_channel())):
        assert await sr.wrote_instructions_instead_of_doing_it(RAN, NODE, None) is None


@pytest.mark.asyncio
async def test_no_channel_means_prose_was_all_it_could_do():
    with patch.object(sr, "channel", AsyncMock(return_value=None)):
        assert await sr.wrote_instructions_instead_of_doing_it(HOST_PROSE, NODE, None) is None


@pytest.mark.asyncio
async def test_empty_or_commandless_output_is_silent():
    with patch.object(sr, "channel", AsyncMock(return_value=_open_channel())):
        for out in ("", "   ", "## Notes\n\nNothing to run here.\n"):
            assert await sr.wrote_instructions_instead_of_doing_it(out, NODE, None) is None


@pytest.mark.asyncio
async def test_a_contradicting_host_is_mentioned_when_known():
    with patch.object(sr, "channel", AsyncMock(return_value=_open_channel())), \
         patch.object(sr, "_unmet_for", AsyncMock(return_value=[{"why": "container 102 is stopped"}])):
        r = await sr.wrote_instructions_instead_of_doing_it(HOST_PROSE, NODE, None)
    assert "container 102 is stopped" in r


@pytest.mark.asyncio
async def test_it_fails_soft_when_the_channel_cannot_be_read():
    with patch.object(sr, "channel", AsyncMock(side_effect=RuntimeError("down"))):
        assert await sr.wrote_instructions_instead_of_doing_it(HOST_PROSE, NODE, None) is None


def test_the_discriminator_knows_a_host_command_from_a_phone_tap():
    assert sr._targets_this_host("pct set 102 --nameserver 192.168.1.30")
    assert sr._targets_this_host("systemctl restart prowlarr")
    assert not sr._targets_this_host("Open the My Spectrum app")
    assert not sr._targets_this_host("tap Services")


def test_the_executor_consults_it_before_the_verifiers():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea.execute_next_node)
    assert "wrote_instructions_instead_of_doing_it(" in src
    assert src.index("wrote_instructions_instead_of_doing_it(") < src.index("elif skip_verify:")
    assert "node_wrote_instructions_not_work" in src


# ── §17.1260: the engine researched the failure, then deleted the research ─


REPORT = """## Run this

stuff

## Executed on pve-runner
$ python3 /tmp/add_indexers.py
failed: Anidex - HTTP 400

## What went wrong, and what to try

Prowlarr validates an indexer by connecting to it. "Resource temporarily
unavailable (anidex.info:443)" is a DNS failure inside the container, not a bad
request body.

## Something else
ignored
"""


def test_the_diagnosis_section_is_extracted_cleanly():
    """`diagnose_failure` writes the RESEARCH under this heading, and
    `attempt_feedback` skipped it entirely — so the engine paid for a web search
    and told the next draft only the headline."""
    d = sr.diagnosis_of(REPORT)
    assert d.startswith("## What went wrong")
    assert "DNS failure inside the container" in d
    assert "Something else" not in d, "it must stop at the next heading"
    assert sr.diagnosis_of("## Run this\nx") == ""
    assert sr.diagnosis_of("") == ""


@pytest.mark.asyncio
async def test_a_reset_node_recovers_its_record_from_the_pre_image():
    """`_reset_keys` NULLs output_text and last_verification_reason — the two
    fields §17.1247 reads. So the feedback loop worked after a `reask` and was
    INERT after a `reset`, which is the ordinary way to retry. ADD115 had seven
    reset pre-images, the largest holding 23,201 bytes."""
    from unittest.mock import MagicMock
    db = AsyncMock()
    res = MagicMock()
    res.scalar.return_value = {"output_text": REPORT, "last_verification_reason": "exited 1"}
    db.execute = AsyncMock(return_value=res)
    got = await sr.recover_prior_attempt(
        db, "j", {"node_key": "ADD115", "output_text": None, "last_verification_reason": None})
    assert "## Executed on" in got["output_text"]
    assert got["last_verification_reason"] == "exited 1"


@pytest.mark.asyncio
async def test_a_live_record_is_never_overwritten():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=AssertionError("must not query when the row still has it"))
    kept = await sr.recover_prior_attempt(
        db, "j", {"node_key": "X", "output_text": "live", "last_verification_reason": "r"})
    assert kept["output_text"] == "live"


@pytest.mark.asyncio
async def test_recovery_fails_soft():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("no such table"))
    blank = {"node_key": "X", "output_text": None, "last_verification_reason": None}
    assert await sr.recover_prior_attempt(db, "j", blank) == blank


def test_the_feedback_carries_the_research_not_just_the_headline():
    fb = sr.attempt_feedback({"last_verification_reason": "exited 1", "output_text": REPORT})
    assert "ALREADY RESEARCHED THIS FAILURE" in fb
    assert "DNS failure inside the container" in fb


def test_pending_hands_on_recovers_before_handing_the_node_over():
    import inspect
    src = inspect.getsource(sr.pending_hands_on)
    assert "recover_prior_attempt(db, job_id, node)" in src


# ── §17.1261: what happened has to reach the caller ───────────────────────


@pytest.mark.asyncio
async def test_a_successful_run_reports_what_it_executed():
    """Live: ADD116 did the work and the API answered `executed: 0 | ok: 0`. The
    early-return path splatted the run detail; the FINAL return dropped it, so
    errors carried detail and successes did not."""
    from unittest.mock import MagicMock
    from app.modules import decision_pause as dp

    waiting = {"kind": "run", "node_key": "ADD116", "runbook": "r",
               "commands": ["pct set 102 --nameserver \"192.168.1.30 1.1.1.1\""],
               "verify": [], "refused": []}
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}}
    upd = MagicMock(); upd.rowcount = 1
    db.execute = AsyncMock(side_effect=[row, upd, upd])
    ran = {"outcome": "ran", "node_status": "done",
           "executed": [{"command": "pct set 102 …", "ok": True, "exit": 0, "output": ""}],
           "verify": "$ pct exec 102 -- getent hosts github.com\n140.82.113.4"}
    with patch("app.modules.supervised_runs.resolve_run", new=AsyncMock(return_value=ran)), \
         patch.object(dp, "transition", new=AsyncMock(return_value=True)):
        out = await dp.resolve_decision(db, "j", "ADD116", choice="run")
    assert out["outcome"] == "ran"
    assert len(out["executed"]) == 1, "the executed list must reach the caller"
    assert "140.82.113.4" in out["verify"]
    assert out["record"]["result"] == "ran"


@pytest.mark.asyncio
async def test_a_failed_run_reports_its_reason_and_diagnosis():
    """A plain `failed` is not in the early-return list either, so it fell through
    to the same lossy return — no reason, no verify, and none of the research."""
    from unittest.mock import MagicMock
    from app.modules import decision_pause as dp

    waiting = {"kind": "run", "node_key": "ADD116", "runbook": "r",
               "commands": ["pct set 102 --nameserver 192.168.1.30 1.1.1.1"],
               "verify": [], "refused": []}
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}}
    upd = MagicMock(); upd.rowcount = 1
    db.execute = AsyncMock(side_effect=[row, upd, upd])
    failed = {"outcome": "failed", "node_status": "failed",
              "executed": [{"command": "pct set 102 …", "ok": False, "exit": 255,
                            "output": "400 too many arguments"}],
              "reason": "`pct set …` exited 255: 400 too many arguments",
              "diagnosis": "`--nameserver` takes one quoted space-separated list."}
    with patch("app.modules.supervised_runs.resolve_run", new=AsyncMock(return_value=failed)), \
         patch.object(dp, "transition", new=AsyncMock(return_value=True)):
        out = await dp.resolve_decision(db, "j", "ADD116", choice="run")
    assert out["outcome"] == "failed"
    assert "400 too many arguments" in out["reason"]
    assert "one quoted space-separated list" in out["diagnosis"]
    assert out["executed"][0]["exit"] == 255


@pytest.mark.asyncio
async def test_a_plain_decision_carries_no_run_detail():
    """A `decision` node has no run, so there is nothing to splat and the shape
    must stay exactly as it was."""
    from unittest.mock import MagicMock
    from app.modules import decision_pause as dp

    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "status": "awaiting_decision", "metadata": {"awaiting_decision": {"node_key": "ADD99"}}}
    upd = MagicMock(); upd.rowcount = 1
    db.execute = AsyncMock(side_effect=[row, upd, upd])
    with patch.object(dp, "transition", new=AsyncMock(return_value=True)):
        out = await dp.resolve_decision(db, "j", "ADD99", choice="Status & control")
    assert out["outcome"] == "resolved"
    assert set(out) == {"outcome", "node_key", "record"}
