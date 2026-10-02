"""§17.1205 — the Machines page could connect a machine and test it, and nothing
else. No way to stop it, no way to ask it anything, no way to see what it had
been doing. *"there is no clear interaction to start, stop interact or watch the
runner like there should be."*

Three things, and one of them is not what it sounds like: "stop" cannot mean
`systemctl stop` on the target. The runner refuses commands touching its own
service by design (its denylist names `local-runner-mcp`) and the engine has no
other channel there, so stopping means the engine stops USING it — the registry
flag — and the real service commands are handed over to run where they can.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import runner_activity as ra


@pytest.fixture(autouse=True)
def _clean():
    ra.reset()
    yield
    ra.reset()


# ── the recorder ─────────────────────────────────────────────────────────

def test_only_the_tools_that_carry_a_command_are_recorded():
    """`write_policy` and the tool listings are the engine asking a runner about
    itself, and the probe calls them on every page load — they would bury the
    rows that are actually work."""
    import app.modules.mcp_client as mc
    assert mc._COMMAND_TOOLS == ra.COMMAND_TOOLS == ("run_readonly", "run_supervised", "write_file")  # §17.1274
    assert "write_policy" not in ra.COMMAND_TOOLS and "list_tools" not in ra.COMMAND_TOOLS


def test_the_buffer_is_newest_first_and_bounded():
    for i in range(ra.MAX_ENTRIES + 25):
        ra.record(runner="r", tool="run_readonly", command=f"cmd{i}", output="x")
    rows = ra.recent(runner="r", limit=ra.MAX_ENTRIES)
    assert len(rows) == ra.MAX_ENTRIES, "a panel about the recent past must not grow forever"
    assert rows[0]["command"] == f"cmd{ra.MAX_ENTRIES + 24}"


def test_a_refusal_is_not_listed_as_a_command_that_ran():
    """It reads the runner's own vocabulary through §17.1204's `not_evidence`,
    so the list cannot claim something ran when the machine would not run it."""
    out = MagicMock(); out.structured = None; out.is_error = False
    out.text = "(refused by the local runner: mutation verb)"
    ra.note_result(MagicMock(name="s"), "run_readonly", {"command": "qm stop 106"}, out, None)
    e = ra.recent()[0]
    assert e["ran"] is False and e["why"] == "the runner refused it"
    assert e["command"] == "qm stop 106"


def test_a_call_that_never_came_back_is_recorded_as_not_run():
    """§17.1201 — a dropped connection is not a command result and must not read
    as one."""
    ra.note_failure(MagicMock(), "run_readonly", {"command": "qm list"}, RuntimeError("boom"), None)
    e = ra.recent()[0]
    assert e["ran"] is False and "did not come back" in e["why"] and "RuntimeError" in e["why"]


def test_the_body_of_the_output_is_not_kept():
    """The list shows the first line and a count. Keeping the whole output would
    make this a second copy of the machine's state, which nothing asked for."""
    ra.record(runner="r", tool="run_readonly", command="pvesm status",
              output="Name  Type\nlocal dir\nlocal-lvm lvmthin\n" + "secret-looking\n" * 50)
    e = ra.recent()[0]
    assert e["first_line"] == "Name  Type" and e["lines"] == 53
    assert "secret-looking" not in repr(e)


def test_a_write_is_labelled_as_one():
    ra.record(runner="r", tool="run_supervised", command="qm set 106 --scsi0 x", output="ok")
    assert ra.recent()[0]["kind"] == "write"


def test_summary_says_where_the_count_starts_from():
    """In memory, so the surface must not imply a durable log."""
    ra.record(runner="r", tool="run_readonly", command="hostname", output="pve")
    s = ra.summary(runner="r")
    assert s["count"] == 1 and s["refused"] == 0 and s["since"]


def test_recording_never_breaks_the_call_it_describes():
    ra.record(runner="r", tool="run_readonly", command=None, output=None)   # type: ignore[arg-type]
    assert len(ra.recent()) == 1


def test_inflight_is_counted_so_running_is_not_guessed_from_a_timestamp():
    tok = ra.started("r")
    assert ra.summary(runner="r")["running"] == 1
    assert isinstance(ra.finished(tok), int)
    assert ra.summary(runner="r")["running"] == 0


# ── the endpoints ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_command_that_could_write_is_refused_before_it_is_sent():
    """The box is not a shell. The gate is the same deterministic
    `read_only_command` the state check uses, and it runs BEFORE the runner is
    even looked up — so a write cannot reach the machine through this door."""
    from fastapi import HTTPException
    import app.routers.machines as m
    look = AsyncMock()
    with patch("app.modules.assist_local_runner.runner_spec", new=look):
        with pytest.raises(HTTPException) as ex:
            await m.run_on_machine(m.RunInput(command="systemctl restart pveproxy"), db=MagicMock())
    assert ex.value.status_code == 422
    assert "not read-only" in ex.value.detail["error"]
    look.assert_not_awaited()          # refused before anything was resolved


@pytest.mark.asyncio
async def test_a_read_only_command_goes_through_and_comes_back():
    import app.routers.machines as m
    spec = MagicMock(); spec.name = "pve-runner"
    res = MagicMock(); res.structured = None; res.text = "pve"; res.is_error = False
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=spec)), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        out = await m.run_on_machine(m.RunInput(command="hostname"), db=MagicMock())
    assert out == {"command": "hostname", "ran": True, "why": "", "output": "pve"}


@pytest.mark.asyncio
async def test_asking_a_paused_machine_says_so_rather_than_failing_oddly():
    from fastapi import HTTPException
    import app.routers.machines as m
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ex:
            await m.run_on_machine(m.RunInput(command="hostname"), db=MagicMock())
    assert ex.value.status_code == 409 and "paused" in ex.value.detail


@pytest.mark.asyncio
async def test_pause_flips_the_registry_flag_and_clears_the_caches():
    """Pausing must not forget the machine — the token stays, which is the whole
    difference from Forget. And the caches have to go, or the engine keeps
    answering from what it learned before."""
    import app.routers.machines as m
    spec = MagicMock(); spec.name = "pve-runner"; spec.enabled = True
    ups, clear_t, clear_p = AsyncMock(), MagicMock(), MagicMock()
    db = MagicMock(); db.commit = AsyncMock()
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=spec)), \
         patch("app.modules.mcp_registry.upsert_server", new=ups), \
         patch("app.modules.mcp_client.clear_tool_cache", new=clear_t), \
         patch("app.modules.assist_supervised.clear_policy_cache", new=clear_p):
        out = await m.pause_machine(paused=True, db=db)
    assert out == {"name": "pve-runner", "enabled": False, "paused": True}
    assert ups.await_args.args[1].enabled is False
    clear_t.assert_called_once_with("pve-runner")
    clear_p.assert_called_once_with("pve-runner")


@pytest.mark.asyncio
async def test_pause_resolves_a_runner_that_is_already_paused():
    """The one caller that needs `include_disabled`: without it, resume could
    never find the row it is meant to re-enable, and pausing would be a one-way
    door out of the only page that can undo it."""
    import inspect
    import app.routers.machines as m
    src = inspect.getsource(m.pause_machine)
    assert "runner_spec(db, include_disabled=True)" in src, src
    assert "include_disabled=True" in inspect.getsource(m.list_machines), "the page must still show it"


@pytest.mark.asyncio
async def test_a_paused_machine_is_still_reported_so_it_can_be_resumed():
    import app.routers.machines as m
    spec = MagicMock(); spec.name = "pve-runner"; spec.enabled = False
    spec.endpoint = "http://10.0.0.9:8790/mcp/"
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=spec)), \
         patch("app.modules.assist_supervised.cached_policy", return_value=(False, None)), \
         patch("app.modules.assist_supervised.cached_setup", return_value=(False, None)), \
         patch("app.modules.engine_setup.recipe_context", new=AsyncMock(return_value={"token": "T"})), \
         patch("app.modules.engine_setup.prefixes_needed", new=AsyncMock(return_value=[])), \
         patch("app.modules.engine_setup.read_prefixes_needed", new=AsyncMock(return_value=[])), \
         patch("app.modules.runner_secrets.list_secrets", new=AsyncMock(return_value=[])):
        out = await m.list_machines(db=MagicMock())
    assert out["paused"] is True and out["connected"] is False
    assert out["runner"]["name"] == "pve-runner", "hidden here, the pause could not be undone"


@pytest.mark.asyncio
async def test_activity_reports_what_was_recorded_for_this_runner_only():
    import app.routers.machines as m
    ra.record(runner="pve-runner", tool="run_readonly", command="hostname", output="pve")
    ra.record(runner="other-box", tool="run_readonly", command="uname", output="Linux")
    spec = MagicMock(); spec.name = "pve-runner"
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=spec)):
        out = await m.machine_activity(limit=40, db=MagicMock())
    assert out["runner"] == "pve-runner"
    assert [e["command"] for e in out["entries"]] == ["hostname"]
    assert out["count"] == 1 and out["since"]


# ── the surface renders what the payload carries ─────────────────────────

def test_the_view_renders_the_new_fields_and_actions():
    """§17.1007c — an operator-facing field nothing renders is not shipped."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/machines.js").read_text()
    for needed in ("/setup/machines/pause", "/setup/machines/run", "/setup/machines/activity",
                   "controlCard", "activityCard", "d.paused",
                   "systemctl stop local-runner-mcp"):
        assert needed in src, needed
    # §17.1181 — CSP: no inline style attributes anywhere in the view
    assert "style:" not in src and "style=" not in src, "CSP forbids inline styles"


def test_the_paused_state_is_not_reported_as_not_connected():
    """"no machine connected yet" would send the operator to re-connect a
    machine that is merely paused."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/machines.js").read_text()
    blk = src[src.index("const status = el(\"p\""):src.index("const result = el(\"p\"")]
    assert "d.paused" in blk and "still there" in blk, blk


def test_a_paused_machine_is_not_offered_a_connect_button():
    """Caught in a screenshot of the paused state, not by a test: the primary
    button keys off `connected`, which is false while paused, so it read
    "Connect and test" on a machine whose address was already saved — and the
    Test button vanished with it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/machines.js").read_text()
    blk = src[src.index("const known ="):src.index("function say(")]
    assert "const known = d.connected || d.paused;" in blk, blk
    assert 'known ? "Re-point and test" : "Connect and test"' in blk, blk
    assert "known ? test : null" in blk, "the Test button must stay while paused"
