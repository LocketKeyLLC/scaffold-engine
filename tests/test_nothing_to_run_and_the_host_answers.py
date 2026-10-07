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
    assert 'if not frame.get("commands") and not _developed:' in src
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


# ── §17.1229b: a multi-word name, and the host's own address ──────────────


def test_a_multi_word_prefix_finds_its_service_and_a_generic_one_finds_nothing():
    by = rd.guests_by_name(PCT_LIST)
    assert rd.match_guest("PROWLARR_CONTAINER", by) == "105"
    assert rd.match_guest("MEDIA_SONARR_LXC", by) == "108"
    # nothing in these names is a service — they are about the host
    assert rd.match_guest("PROXMOX_NODE", by) is None
    assert rd.match_guest("HOST", by) is None
    assert rd.match_guest("CONTAINER", by) is None


def test_service_words_strips_the_generic_ones():
    assert rd.service_words("PROWLARR_CONTAINER") == ["prowlarr"]
    assert rd.service_words("PROXMOX_NODE") == []
    assert rd.service_words("") == []


@pytest.mark.asyncio
async def test_the_hosts_own_address_is_read_and_not_guessed_when_there_are_several():
    """Live: PROXMOX_NODE_IP came back empty with two ledger guesses. `pct list`
    cannot answer it — the host itself can, and with several addresses the
    operator still picks."""
    inputs = [{"name": "PROXMOX_NODE_IP"}]
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c:
                      "192.168.1.26 192.168.1.127 127.0.0.1\n" if c == "hostname -I" else "")):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="pve-runner"))
    i = inputs[0]
    assert not i.get("value")                          # two candidates → no guess
    assert [s["value"] for s in i["suggestions"]] == ["192.168.1.26", "192.168.1.127"]
    assert "hostname -I" in i["suggestions"][0]["source"]


@pytest.mark.asyncio
async def test_a_single_host_address_is_prefilled():
    inputs = [{"name": "HOST_IP"}]
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c:
                      "10.0.0.5\n" if c == "hostname -I" else "")):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="r"))
    assert inputs[0]["value"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_a_host_shaped_name_never_probes_a_guest():
    """The whole point of the generic-word list: no `pct exec` against a guest
    that happens to share a word with 'node'."""
    calls: list[str] = []

    async def _rec(spec, c):
        calls.append(c)
        return "10.0.0.5\n" if c == "hostname -I" else PCT_LIST if c == "pct list" else ""

    with patch.object(rd, "_read", _rec):
        await rd._discover_guest_inputs([{"name": "PROXMOX_NODE_IP"}], SimpleNamespace(name="r"))
    assert not any(c.startswith("pct exec") for c in calls), calls


def test_loopback_is_never_offered():
    assert rd._all_ipv4("127.0.0.1 10.1.1.1 10.1.1.1") == ["10.1.1.1"]
    assert rd._all_ipv4("") == []


# ── §17.1229c: the drafter names the same value _HOST or _IP ──────────────


@pytest.mark.asyncio
async def test_a_service_HOST_placeholder_is_answered_with_the_guests_address():
    """Live, on the redraft after §17.1229b: the drafter called them
    `PROWLARR_HOST`/`RADARR_HOST`/`SONARR_HOST`, which matched no pattern, and
    the ledger offered the bare name 'prowlarr' as if it resolved on this LAN."""
    reads = {
        "pct list": PCT_LIST,
        "pct exec 105 -- hostname -I": "192.168.1.45\n",
        "pct exec 107 -- hostname -I": "192.168.1.47\n",
    }
    inputs = [{"name": "PROWLARR_HOST"}, {"name": "RADARR_HOSTNAME"}]
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c: reads.get(c, ""))):
        await rd._discover_guest_inputs(inputs, SimpleNamespace(name="pve-runner"))
    assert inputs[0]["value"] == "192.168.1.45"
    assert inputs[1]["value"] == "192.168.1.47"


def test_every_shape_the_drafter_has_actually_used_is_covered():
    """The two live draws named the same value differently; both must match."""
    for name in ("PROWLARR_IP", "PROWLARR_HOST", "PROWLARR_HOSTNAME",
                 "PROWLARR_ADDRESS", "PROWLARR_CONTAINER_IP"):
        assert rd._IP_NAMES.match(name), name
        assert not rd._HOST_IP_NAMES.match(name), name
    for name in ("PROXMOX_NODE_IP", "HOST_IP", "PVE_ADDR", "NODE_ADDRESS"):
        assert rd._HOST_IP_NAMES.match(name), name
    # a bare generic word is nobody's service and nobody's host address
    assert not rd._IP_NAMES.match("HOST")
    assert not rd._HOST_IP_NAMES.match("HOST")


# ── §17.1232: the drafter is told which machines exist ────────────────────


QM_LIST = """      VMID NAME                 STATUS     MEM(MB)
       106 palworld             running    16384
       110 ubuntu-builder       stopped    4096
"""


@pytest.mark.asyncio
async def test_the_inventory_names_every_guest_and_how_to_address_it():
    """Live, ADD96's third draw: `curl http://<PROXMOX_HOST_IP>:9696/api/v1/…`
    while Prowlarr is container 102. The drafter did not know a guest existed."""
    real = """VMID       Status     Lock         Name
101        running                 jellyfin
102        running                 prowlarr
103        running                 radarr
"""
    with patch.object(rd, "_read", AsyncMock(side_effect=lambda spec, c:
                      real if c == "pct list" else QM_LIST if c == "qm list" else "")):
        block = await sr.host_inventory(SimpleNamespace(name="pve-runner"))
    assert "container 102 — prowlarr" in block
    assert "container 101 — jellyfin" in block and "container 103 — radarr" in block
    assert "VM 106 — palworld (running)" in block
    guest_lines = [l for l in block.splitlines() if l.startswith("  ")]
    assert len(guest_lines) == 5                    # 3 containers + 2 VMs
    assert not any("NAME" in l or "VMID" in l for l in guest_lines), guest_lines
    # and the rule that makes the inventory actionable
    assert "<NAME_IP>" in block and "pct exec" in block
    assert "`pct` cannot address a VM" in block


@pytest.mark.asyncio
async def test_an_unreadable_host_yields_no_inventory_rather_than_a_wrong_one():
    with patch.object(rd, "_read", AsyncMock(return_value="")):
        assert await sr.host_inventory(SimpleNamespace(name="r")) == ""
    assert await sr.host_inventory(None) == ""
    with patch.object(rd, "_read", AsyncMock(side_effect=RuntimeError("down"))):
        assert await sr.host_inventory(SimpleNamespace(name="r")) == ""


def test_the_channel_rules_say_where_a_service_lives():
    assert "not the host's" in sr.CHANNEL_RULES
    assert "<PROXMOX_HOST_IP>" in sr.CHANNEL_RULES     # named as the thing NOT to write


@pytest.mark.asyncio
async def test_the_drafter_only_gets_the_inventory_when_the_engine_may_run_it():
    """A runbook the OPERATOR will carry out by hand is not addressed by the
    engine, so it does not need the machine list — and must not pay for it."""
    inv = AsyncMock(return_value="\n\nGUESTS")
    with patch.object(sr, "host_inventory", inv), \
         patch.object(sr, "stored_values_block", AsyncMock(return_value="")), \
         patch("app.utils.llm_retry.generate_until_nonempty", new=AsyncMock(return_value="rb")), \
         patch("app.modules.prompt_assembly.build_base_prompt", return_value="p"):
        await sr.draft_runbook({"node_key": "X"}, {}, for_channel=False, spec=SimpleNamespace(name="r"))
    inv.assert_not_awaited()


def test_every_draft_call_site_passes_the_runner():
    """A gate goes blind when code moves: a new redraft that forgets `spec`
    would silently go back to drafting without the machine list. Enumerated off
    the AST, not grepped, so a call split over lines still counts."""
    import ast as _ast
    src = open("app/modules/execution_agent.py").read()
    calls = [n for n in _ast.walk(_ast.parse(src))
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
             and n.func.attr == "draft_runbook"]
    # §17.1254 added a fifth site and this gate caught it, which is the point.
    # The property is what matters, not the count: EVERY site must pass the
    # runner, so a new redraft cannot go back to drafting blind.
    assert len(calls) >= 4, f"expected at least the 4 known draft sites, found {len(calls)}"
    for c in calls:
        assert "spec" in [k.arg for k in c.keywords], f"draft_runbook at line {c.lineno} drops spec"
