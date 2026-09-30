"""§17.1212 — if the engine can read the value off the machine, it must not ask.

Live, on the operator's job, the run pause for ADD94 asked for two values with
NO suggestions and NO prefill:

    <DISK_NAME>  "…as shown in `pvesm status`… You will discover this in Step 1."
    <STORAGE>    "the storage pool where the disk lives. Discovered in Step 1."

Step 1 of the runbook the engine drafted is `qm config 106`. It knew the command
that answers the question and asked the human to go and run it. The engine had a
read-only channel to that exact host and had been using it all evening.

    "there is, yet again no clear 'enter' to communicate to the engine that the
     name of the storage should be within its own information regarding the set
     up of that VM."
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import runbook_discovery as rd

# verbatim from the operator's host, through the engine's own runner
QM_CONFIG_106 = """agent: 1
boot: order=ide2
cores: 4
ide2: none,media=cdrom
memory: 8192
name: palworld-server
net0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0
scsihw: virtio-scsi-pci
unused0: local-lvm:vm-106-disk-0
vmgenid: cfc697d3-0b6a-411a-97f6-5fa9e9c4caa8"""

PVESM_STATUS = """Name             Type     Status     Total (KiB)      Used (KiB) Available (KiB)        %
local             dir     active        98497780        10013732        83434500   10.17%
local-lvm     lvmthin     active       449990656        11249766       438740889    2.50%
oasis         zfspool     active      5721030656       106510872      5614519784    1.86%"""

CMDS = ["qm config 106", "qm rescan --vmid 106",
        "qm set 106 --scsi0 <DISK_NAME>", "qm resize 106 scsi0 100G"]


# ── the parsers, on real output ──────────────────────────────────────────

def test_the_detached_disk_is_found_and_comes_first():
    vols = rd.volumes_from_config(QM_CONFIG_106)
    assert vols[0] == ("unused0", "local-lvm:vm-106-disk-0")


def test_an_empty_cdrom_is_not_a_disk():
    assert all(v != "none" for _s, v in rd.volumes_from_config(QM_CONFIG_106))


def test_an_attached_disk_is_found_too_but_ranked_after_a_detached_one():
    cfg = QM_CONFIG_106 + "\nscsi0: oasis:vm-106-disk-9,size=40G"
    slots = [s for s, _v in rd.volumes_from_config(cfg)]
    assert slots[0] == "unused0" and "scsi0" in slots


def test_storages_come_off_pvesm_status():
    assert rd.storages_from_status(PVESM_STATUS) == ["local", "local-lvm", "oasis"]


def test_the_vmid_comes_from_the_commands():
    assert rd.vmid_from(CMDS) == "106"


def test_two_different_guests_means_the_engine_must_not_choose():
    assert rd.vmid_from(["qm set 106 --scsi0 x", "qm set 110 --boot order=scsi0"]) is None


# ── the discovery itself ─────────────────────────────────────────────────

def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _runner(outputs: dict):
    async def fake(spec, tool, args):
        r = MagicMock(); r.structured = None; r.is_error = False
        r.text = outputs.get(args["command"], "")
        return r
    return fake


@pytest.mark.asyncio
async def test_the_operators_case_is_answered_without_asking():
    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []},
              {"name": "STORAGE", "value": "", "secret": False, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm config 106": QM_CONFIG_106})):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    disk = next(i for i in out if i["name"] == "DISK_NAME")
    store = next(i for i in out if i["name"] == "STORAGE")
    assert disk["value"] == "local-lvm:vm-106-disk-0", disk
    assert store["value"] == "local-lvm", "the storage is the half before the colon — no second command"
    assert "read from pve-runner just now" in disk["suggestions"][0]["source"]


@pytest.mark.asyncio
async def test_two_disks_are_offered_but_neither_is_prefilled():
    """Choosing for the operator is how the wrong disk gets resized."""
    cfg = QM_CONFIG_106 + "\nunused1: oasis:vm-106-disk-7"
    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm config 106": cfg})):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    assert out[0]["value"] == "", "an ambiguous answer must not be prefilled"
    assert len(out[0]["suggestions"]) == 2


@pytest.mark.asyncio
async def test_a_pin_or_an_existing_suggestion_wins_over_a_fresh_read():
    """§17.1188's sources are the operator's own decisions; this is evidence."""
    pinned = [{"name": "STORAGE", "value": "my-pool", "secret": False, "suggestions": []}]
    already = [{"name": "DISK_NAME", "value": "", "secret": False,
                "suggestions": [{"value": "x", "source": "a pin", "confidence": "pinned"}]}]
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm config 106": QM_CONFIG_106})):
        p = await rd.discover_inputs(pinned, CMDS, _spec())
        a = await rd.discover_inputs(already, CMDS, _spec())
    assert p[0]["value"] == "my-pool"
    assert a[0]["suggestions"][0]["confidence"] == "pinned"


@pytest.mark.asyncio
async def test_a_secret_is_never_read_off_the_machine():
    inputs = [{"name": "DISK_NAME", "value": "", "secret": True, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm config 106": QM_CONFIG_106})):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    assert out[0]["value"] == "" and not out[0]["suggestions"]


@pytest.mark.asyncio
async def test_an_unrelated_placeholder_is_not_guessed_at():
    """The table is narrow on purpose: a wrong value prefilled into a command
    that changes a machine is worse than an empty box."""
    inputs = [{"name": "HOSTNAME", "value": "", "secret": False, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm config 106": QM_CONFIG_106})):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    assert not out[0]["suggestions"]


@pytest.mark.asyncio
async def test_a_refusal_is_not_parsed_as_an_answer():
    """§17.1204 — the runner's excuses are not output."""
    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []}]
    refusal = {"qm config 106": "(the runner is UNPRIVILEGED and this command needs root — …)"}
    with patch("app.modules.mcp_client.call_tool", new=_runner(refusal)):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    assert not out[0]["suggestions"] and out[0]["value"] == ""


@pytest.mark.asyncio
async def test_an_unreachable_runner_leaves_the_ask_exactly_as_it_was():
    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=AsyncMock(side_effect=RuntimeError("down"))):
        out = await rd.discover_inputs(inputs, CMDS, _spec())
    assert out[0]["value"] == "" and not out[0]["suggestions"]


@pytest.mark.asyncio
async def test_no_runner_no_probes():
    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []}]
    assert (await rd.discover_inputs(inputs, CMDS, None))[0]["suggestions"] == []


@pytest.mark.asyncio
async def test_only_read_only_commands_are_ever_sent():
    sent: list[str] = []

    async def spy(spec, tool, args):
        sent.append(args["command"])
        r = MagicMock(); r.structured = None; r.is_error = False; r.text = QM_CONFIG_106
        return r

    inputs = [{"name": "DISK_NAME", "value": "", "secret": False, "suggestions": []}]
    with patch("app.modules.mcp_client.call_tool", new=spy):
        await rd.discover_inputs(inputs, CMDS, _spec())
    from app.modules.assist_state_check import read_only_command
    assert sent and all(read_only_command(c) for c in sent), sent


# ── it is wired where the pause is built ─────────────────────────────────

def test_the_pause_runs_discovery_after_the_redraft():
    """Before the redraft it would fill the commands that get thrown away."""
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    assert "discover_inputs(" in src, "the pause never asks the machine"
    assert src.index("shape_retry_note") < src.index("discover_inputs("), \
        "discovery must run on the commands actually parked"


def test_the_ui_offers_enter_and_says_why_run_is_disabled():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/theater.js").read_text()
    assert 'if (ev.key !== "Enter") return;' in src, "no Enter on a form full of text boxes"
    assert "to enable Run — or press Enter." in src, "a greyed button with no reason reads as broken"
    assert 'confidence === "measured"' in src, "the UI must say a value was read, not asked for"
