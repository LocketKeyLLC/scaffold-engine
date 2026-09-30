"""§17.1227–1231 — five defects the live home-lab job exposed in one evening.

§17.1227  A draft with NOTHING to run. ADD96 ("Add the search sources to
          Prowlarr and connect it to Radarr and Sonarr") drafted 3,961
          characters whose "## Run this" was nine steps of "Open the Prowlarr
          web UI", "go to Indexers → Add Indexer", "Click Test, then Save" —
          and whose "## Verify" called `curl …/api/v1/indexer` four times. The
          drafter knew the API and used it only to CHECK.
§17.1228  So the channel rules now say: if you can check it with a command, you
          can do it with a command.
§17.1229  The same step asked the operator to type ELEVEN values — a node name,
          a storage, three container ids, three IPs and three API keys — every
          one readable through the channel the engine already had open.
§17.1230  A job in `assisted_executing` whose only assist session was
          `completed` belonged to nobody: /assist would not drive it, /execute
          refused it, and 20 pending steps were reachable from no surface.
§17.1231  A decision node beat a hands-on approval unconditionally, so ADD50 was
          handed back twice while ADD99 — 49 steps later — was parked instead.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import execution_agent as ea
from app.modules import runbook_discovery as rd
from app.modules import supervised_runs as sr

# The shape of what ADD96 actually drafted.
UI_RUNBOOK = """## Run this

1. Open the Prowlarr web UI at `http://<PROWLARR_IP>:9696` and log in.
2. Navigate to **Settings → General** and copy the API key.
3. Go to **Indexers → Add Indexer** and add each of the following.
4. Click **Test**, then **Save**.

## Verify

```bash
curl -s http://<PROWLARR_IP>:9696/api/v1/indexer -H "X-Api-Key: <PROWLARR_API_KEY>" | grep -c '"enable":true'
```

```bash
curl -s http://<RADARR_IP>:7878/api/v3/indexer -H "X-Api-Key: <RADARR_API_KEY>" | grep -c '"enable":true'
```
"""


# ── §17.1227/1228 ────────────────────────────────────────────────────────

def test_the_note_quotes_the_api_the_draft_already_used_to_verify():
    note = sr.no_commands_retry_note("Add the search sources to Prowlarr", UI_RUNBOOK)
    assert "NOTHING TO RUN" in note
    assert "/api/v1/indexer" in note and "/api/v3/indexer" in note
    assert "curl" in note
    # and it leaves a door for the genuinely-manual part
    assert "## By hand" in note


def test_the_note_still_helps_when_verify_names_no_api():
    note = sr.no_commands_retry_note("do a thing", "## Run this\n\nOpen the UI and click Save.\n")
    assert "NOTHING TO RUN" in note and "HTTP API or a CLI" in note
    assert "/api/" not in note                 # nothing to quote, so nothing quoted


def test_an_empty_runbook_gets_no_note():
    """A draft that produced no TEXT is §17.1189(C)'s case, not this one."""
    assert sr.no_commands_retry_note("s", "") == ""
    assert sr.no_commands_retry_note("s", "   ") == ""


def test_verify_commands_are_read_from_the_verify_section_only():
    got = sr._verify_commands_in(UI_RUNBOOK)
    assert len(got) == 2 and all("curl" in c for c in got)


def test_the_channel_rules_forbid_describing_a_ui():
    assert "never by describing the web UI" in sr.CHANNEL_RULES
    assert "you can DO it with a command" in sr.CHANNEL_RULES


def test_the_executor_redrafts_when_there_is_nothing_to_run():
    import inspect
    src = inspect.getsource(ea._pause_for_decision)
    assert 'if not frame.get("commands"):' in src
    assert "no_commands_retry_note(" in src
    # only ever trade up
    assert 'if _apif.get("commands"):' in src
    i = src.index("no_commands_retry_note(")
    assert i < src.index("runbook_coverage")   # before the coverage pass


# ── §17.1229 ─────────────────────────────────────────────────────────────

PCT_LIST = """VMID       Status     Lock         Name
       105 running                 prowlarr
       107 running                 radarr
       108 stopped                 sonarr
       111 running                 control-panel
"""


def test_guests_are_read_by_name_and_the_header_is_not_one():
    by = rd.guests_by_name(PCT_LIST)
    assert by == {"prowlarr": "105", "radarr": "107", "sonarr": "108", "control-panel": "111"}
    assert "name" not in by


def test_a_service_name_matches_its_guest():
    by = rd.guests_by_name(PCT_LIST)
    assert rd.match_guest("PROWLARR".lower(), by) == "105"
    assert rd.match_guest("sonarr", by) == "108"
    assert rd.match_guest("jellyfin", by) is None          # not on this host


def test_an_ambiguous_service_name_picks_nothing():
    """A wrong ctid runs a write against the wrong container."""
    by = {"media-radarr": "1", "radarr-4k": "2"}
    assert rd.match_guest("radarr", by) is None


def test_the_ip_and_api_key_are_parsed_but_loopback_is_not_an_ip():
    assert rd.first_ipv4(" 192.168.1.44 fd00::1 \n") == "192.168.1.44"
    assert rd.first_ipv4("127.0.0.1") is None
    assert rd.apikey_from_config("<Config><ApiKey>0a1b2c3d4e5f60718293a4b5</ApiKey></Config>") == "0a1b2c3d4e5f60718293a4b5"
    assert rd.apikey_from_config("<Config></Config>") is None


@pytest.mark.asyncio
async def test_the_host_answers_the_ctid_the_ip_and_the_node_name():
    reads = {
        "hostname": "pve\n",
        "pct list": PCT_LIST,
        "pct exec 105 -- hostname -I": "192.168.1.45 \n",
    }
    inputs = [{"name": "PROXMOX_NODE_NAME"}, {"name": "PROWLARR_CTID"}, {"name": "PROWLARR_IP"}]
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c: reads.get(c, ""))):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="pve-runner"))
    got = {i["name"]: i.get("value") for i in inputs}
    assert got == {"PROXMOX_NODE_NAME": "pve", "PROWLARR_CTID": "105", "PROWLARR_IP": "192.168.1.45"}
    assert all("pve-runner" in (i["suggestions"][0]["source"]) for i in inputs)


@pytest.mark.asyncio
async def test_a_discovered_api_key_is_stored_never_shown():
    """§17.1193 — reading a secret onto the screen to save typing would trade one
    problem for a worse one. It goes into the store; the command keeps $NAME."""
    reads = {
        "pct list": PCT_LIST,
        "pct exec 107 -- cat /config/config.xml": "<Config><ApiKey>abcdef0123456789abcdef</ApiKey></Config>",
    }
    inputs = [{"name": "RADARR_API_KEY"}]
    set_secret = AsyncMock()
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c: reads.get(c, ""))), \
         patch("app.modules.runner_secrets.set_secret", set_secret):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="pve-runner"))
    i = inputs[0]
    assert i["stored"] is True and i["secret"] is True
    assert i["value"] == ""                        # the value is NOT on the screen
    assert "you do not need to type it" in i["hint"]
    assert set_secret.await_args.args[1] == "RADARR_API_KEY"
    assert set_secret.await_args.args[2] == "abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_an_input_that_is_already_answered_is_left_alone():
    inputs = [{"name": "PROWLARR_CTID", "value": "999"},
              {"name": "RADARR_IP", "suggestions": [{"value": "10.0.0.9"}]}]
    read = AsyncMock(return_value="")
    with patch.object(rd, "_read", read):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="r"))
    assert inputs[0]["value"] == "999"
    read.assert_not_awaited()                      # nothing pending, nothing probed


@pytest.mark.asyncio
async def test_an_unreadable_host_changes_nothing():
    inputs = [{"name": "PROWLARR_CTID"}, {"name": "PROWLARR_IP"}]
    with patch.object(rd, "_read", AsyncMock(return_value="")):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="r"))
    assert not any(i.get("value") for i in inputs)


# ── §17.1231 ─────────────────────────────────────────────────────────────

def test_plan_order_decides_which_pause_is_asked_first():
    assert ea._order_of({"execution_order": 50}) < ea._order_of({"execution_order": 99})
    # a missing order never jumps a step that has one
    assert ea._order_of({}) == float("inf")
    assert ea._order_of({"execution_order": None}) == float("inf")
    assert ea._order_of({"execution_order": "nope"}) == float("inf")


def test_the_pause_compares_both_candidates():
    import inspect
    src = inspect.getsource(ea._pause_for_decision)
    assert "_order_of(node), _order_of(run_node)" in src
    assert "if _r < _d:" in src
    # the hands-on look-up no longer hides behind `node is None`
    assert src.index("pending_hands_on(db, job_id)") < src.index("if node is None and run_node is None:")


def test_both_candidate_queries_select_the_order_they_are_compared_on():
    """A gate goes blind when the column is not there: without execution_order
    every comparison would be inf vs inf and the decision would always win."""
    from app.modules import decision_pause as dp
    import inspect
    assert "n.execution_order" in inspect.getsource(dp.pending_decision)
    assert "n.execution_order" in inspect.getsource(sr.pending_hands_on)


# ── §17.1230 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_assist_branch_with_no_live_session_is_handed_to_the_executor():
    from contextlib import asynccontextmanager
    db = AsyncMock()

    @asynccontextmanager
    async def _s():
        yield db

    jobs = [{"id": "j", "status": "assisted_executing"}, {"id": "j", "status": "executing"}]
    with patch.object(ea, "async_session", lambda: _s()), \
         patch.object(ea, "_get_job", AsyncMock(side_effect=jobs)), \
         patch.object(ea, "_assist_session_live", AsyncMock(return_value=False)), \
         patch.object(ea, "transition", AsyncMock(return_value=True)) as tr, \
         patch.object(ea, "enforce_job_budget", AsyncMock(return_value=None)), \
         patch.object(ea, "_peek_next_node", AsyncMock(return_value=None)), \
         patch.object(ea, "_get_next_node", AsyncMock(return_value=None)), \
         patch.object(ea, "_all_nodes_done", AsyncMock(return_value=True)), \
         patch.object(ea, "_flip_job_completed", AsyncMock(return_value=True)), \
         patch.object(ea, "_compile_output", AsyncMock(return_value=(None, False))):
        out = await ea.execute_next_node("j")
    assert tr.await_args.kwargs["to"] == "executing"
    assert out.get("status") != "error", out


@pytest.mark.asyncio
async def test_a_LIVE_assist_session_keeps_its_job():
    from contextlib import asynccontextmanager
    db = AsyncMock()

    @asynccontextmanager
    async def _s():
        yield db

    with patch.object(ea, "async_session", lambda: _s()), \
         patch.object(ea, "_get_job", AsyncMock(return_value={"id": "j", "status": "assisted_running"})), \
         patch.object(ea, "_assist_session_live", AsyncMock(return_value=True)), \
         patch.object(ea, "transition", AsyncMock(return_value=True)) as tr:
        out = await ea.execute_next_node("j")
    tr.assert_not_awaited()
    assert out["status"] == "error" and "not executable" in out["message"]


@pytest.mark.asyncio
async def test_liveness_fails_soft_to_the_assist_branch_keeping_it():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("no table"))
    assert await ea._assist_session_live(db, "j") is True


@pytest.mark.asyncio
async def test_a_finished_session_is_not_live():
    db = AsyncMock()
    res = MagicMock(); res.first.return_value = None
    db.execute = AsyncMock(return_value=res)
    assert await ea._assist_session_live(db, "j") is False
    sql = str(db.execute.await_args.args[0])
    for done in ("completed", "handed_off", "cancelled", "failed"):
        assert done in sql
