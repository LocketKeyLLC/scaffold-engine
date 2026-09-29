"""§17.1204 — a refusal is not an answer about the operator's system.

`run_probes` wrote whatever the runner returned under the claim's `== id ==`
marker, and `judge_outputs` reads a section body as that command's output. So a
runner that could not run the command handed the judge a sentence ABOUT THE
RUNNER and asked it to rule on a claim about the machine. `ok` was computed and
only ever logged, and it was wrong anyway: a refusal comes back as an ordinary
string, so `not res.is_error` was True for it.

The live shape is §17.1202's: an unprivileged runner on a Proxmox host answered
every `qm`/`pct` check with `ipcc_send_rec[1] failed … Unable to load access
control list`. The helper's own note calls that "a permission refusal, not a
statement about your system" — and it was going to the judge as the state of
VM 106.

An absent marker is the one shape that means "unknown", which is the truth when
nothing ran. A present-but-EMPTY marker will not do: `judge_outputs` reads that
as "the command printed nothing", which is evidence, and for a `grep` probe it
is evidence the thing is missing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_local_runner as lr
from app.modules import assist_state_check as sc

PRIVILEGE = ("(the runner is UNPRIVILEGED and this command needs root — the output below is a "
             "permission refusal, not a statement about your system. To let this one through, "
             "add it to the runner's --sudo-allow list and the matching sudoers rule.)\n"
             "ipcc_send_rec[1] failed: Unknown error -1\nUnable to load access control list")

NOT_EVIDENCE = [
    ("(refused by the local runner: mutation verb)", "the runner refused it"),
    ("(timed out after 20s)", "it timed out on the machine"),
    ("(runner error: SSE stream ended without a response)", "the call to the runner did not come back"),
    (PRIVILEGE, "the runner is not allowed to read this"),
]


@pytest.mark.parametrize("text_out,why", NOT_EVIDENCE)
def test_the_shapes_that_are_not_output_are_recognised(text_out, why):
    assert lr.not_evidence(text_out) == why


@pytest.mark.parametrize("text_out", ["status: running", "", "  ", "exit 1", "no such container"])
def test_real_output_is_evidence_including_nothing_at_all(text_out):
    """A command that printed nothing IS an answer (`grep` found no rows), and
    a non-zero exit is an answer too (§17.1201). Only the runner's own excuses
    are excluded."""
    assert lr.not_evidence(text_out) == ""


async def _probe(out: str, *, is_error: bool = False):
    spec = MagicMock(); spec.name = "pve-runner"

    async def fake_call(spec_, tool, args):
        r = MagicMock(); r.text = out; r.is_error = is_error; r.structured = None
        return r

    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        return await lr.run_probes(spec, [{"id": "S:T1", "command": "qm status 106"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("text_out,why", NOT_EVIDENCE)
async def test_a_probe_that_did_not_run_writes_no_section(text_out, why):
    pasted, executed = await _probe(text_out)
    assert "== S:T1 ==" not in pasted, pasted
    assert executed[0]["ran"] is False and executed[0]["ok"] is False
    assert executed[0]["why"] == why
    assert executed[0]["output"] == text_out.rstrip()      # kept, for the operator


@pytest.mark.asyncio
async def test_a_probe_that_ran_still_writes_its_section():
    pasted, executed = await _probe("status: running")
    assert "== S:T1 ==\nstatus: running" in pasted
    assert executed[0]["ran"] is True and executed[0]["why"] == ""


@pytest.mark.asyncio
async def test_an_empty_marker_is_never_the_way_a_refusal_is_recorded():
    """The trap: `judge_outputs` treats a present-but-empty section as "the
    command printed nothing", which is evidence. A refusal must leave NO
    marker, not an empty one."""
    pasted, _ = await _probe("(refused by the local runner: mutation verb)")
    assert sc.attribute_sections(pasted) == {}, sc.attribute_sections(pasted)


@pytest.mark.asyncio
async def test_the_claim_comes_out_unknown_rather_than_ruled_on():
    """End of the lane, no model involved: with nothing pasted, `judge_outputs`
    returns before it ever calls one, and the verdict is `unknown`."""
    pasted, _ = await _probe(PRIVILEGE)
    probes = [{"id": "S:T1", "command": "qm status 106", "claim": "VM 106 is running",
               "kind": "step", "node_key": "n1", "expect": "status: running"}]
    verdicts = await sc.judge_outputs(probes, pasted)
    assert [v["verdict"] for v in verdicts] == ["unknown"], verdicts


@pytest.mark.asyncio
async def test_the_privilege_refusal_would_otherwise_have_been_judged():
    """The counterfactual, so this test proves a behaviour rather than an
    absence: the same text UNDER a marker is a section the judge is asked to
    rule on. That is what the fix removes."""
    probes = [{"id": "S:T1", "command": "qm status 106", "claim": "VM 106 is running",
               "kind": "step", "node_key": "n1", "expect": "status: running"}]
    as_output = f"== S:T1 ==\n{PRIVILEGE}\n"
    assert sc.attribute_sections(as_output).get("S:T1"), "the marker form really does carry a body"


@pytest.mark.asyncio
async def test_the_transcript_names_what_could_not_be_checked():
    """Left out of the output silently, a blocked probe reads as one that
    passed. The operator turn has to say which, why, and in the runner's own
    words — it is the only place they can see it."""
    pasted, executed = await _probe(PRIVILEGE)
    rec = lr.transcript_record(executed, pasted)
    assert "ran 0 read-only probe(s)" in rec
    assert "could not be checked through the runner, so they stay unknown" in rec
    assert "S:T1: qm status 106 — the runner is not allowed to read this" in rec
    assert "Unable to load access control list" in rec


@pytest.mark.asyncio
async def test_a_mixed_batch_keeps_the_good_sections_only():
    spec = MagicMock(); spec.name = "pve-runner"
    answers = {"qm status 106": "status: running",
               "pct status 111": "(refused by the local runner: mutation verb)"}

    async def fake_call(spec_, tool, args):
        r = MagicMock(); r.text = answers[args["command"]]; r.is_error = False; r.structured = None
        return r

    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        pasted, executed = await lr.run_probes(
            spec, [{"id": "S:T1", "command": "qm status 106"}, {"id": "S:T2", "command": "pct status 111"}])
    assert set(sc.attribute_sections(pasted)) == {"S:T1"}
    assert [(e["id"], e["ran"]) for e in executed] == [("S:T1", True), ("S:T2", False)]


# ── a probe the runner cannot run goes to the operator, not to nobody ─────

def test_the_continuation_message_does_not_blame_the_paste():
    """The old sentence explained every missing marker as a paste problem. For
    a probe the ENGINE could not run, that sends the operator looking for a
    dropped `== … ==` line that was never there."""
    from app.modules import assist_state_check as sc
    remaining = [{"id": "S:T1", "command": "qm status 106", "claim": "VM 106 is running"}]
    msg = sc.render_continuation_message(
        [], remaining, missing_ids=["S:T1", "S:T2"], deferred=0, total=2,
        blocked={"S:T1": "the runner is not allowed to read this"})
    assert "Your local runner could not run: `S:T1`" in msg, msg
    assert "the runner is not allowed to read this" in msg, msg
    assert "Those need a shell on the machine itself." in msg, msg
    # the genuinely absent one keeps the original explanation, and only it
    assert "did not cover: `S:T2`" in msg and "`S:T1`, `S:T2`" not in msg, msg


@pytest.mark.asyncio
async def test_an_unprivileged_runner_asks_the_operator_instead_of_finalising():
    """The §17.1202 machine: the runner refuses EVERY probe, so nothing is
    pasted. `sections_present` is 0, the partial branch used to be skipped, and
    the whole check finalised `unknown` without ever asking anyone. A blocked
    probe now holds the check open on its own."""
    from app.modules import assist_state_check as sc
    probes = [{"id": "S:T1", "command": "qm status 106", "claim": "VM 106 is running",
               "kind": "step", "node_key": "n1"},
              {"id": "S:T2", "command": "pct status 111", "claim": "CT 111 is running",
               "kind": "step", "node_key": "n2"}]
    pending = {"probes": probes, "deferred": [], "verdicts": [], "probes_total": 2, "node_key": "n1"}
    db = MagicMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    blocked = {"S:T1": "the runner is not allowed to read this",
               "S:T2": "the runner is not allowed to read this"}
    with patch.object(sc, "get_pending_state_check", new=AsyncMock(return_value=pending)):
        out = await sc.resolve_state_check(db=db, session_id="sid", pasted="", blocked=blocked)
    assert out.get("pending") is True, out
    assert "Your local runner could not run" in out["message"], out["message"]
    assert "qm status 106" in out["message"], "the script to paste must be there"


@pytest.mark.asyncio
async def test_with_no_runner_reason_an_empty_paste_still_finalises():
    """The guard that keeps this from hanging forever: when nothing was pasted
    and the engine has no reason of its own, the check completes as it always
    did rather than waiting on a paste that is not coming."""
    from app.modules import assist_state_check as sc
    probes = [{"id": "S:T1", "command": "qm status 106", "claim": "VM 106 is running",
               "kind": "step", "node_key": "n1"}]
    pending = {"probes": probes, "deferred": [], "verdicts": [], "probes_total": 1, "node_key": "n1"}
    db = MagicMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    with patch.object(sc, "get_pending_state_check", new=AsyncMock(return_value=pending)):
        out = await sc.resolve_state_check(db=db, session_id="sid", pasted="", blocked={})
    assert not out.get("pending"), out


def test_the_loop_stops_re_sending_what_the_runner_refused():
    """A refused probe will not run better next pass. Without this the engine
    burns its twelve batches re-asking a runner that has already said no."""
    import inspect
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn)
    assert '_todo = [p for p in _pend["probes"] if p["id"] not in _blocked]' in src, src
    assert "if not _todo:" in src
    assert "run_probes(_spec, _todo" in src, "the next batch must be the filtered list"
    assert "blocked=_blocked" in src, "the reasons must reach resolve_state_check"
