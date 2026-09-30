"""§17.1240–1242 — three gaps this session hit head-on.

§17.1240  `pct start` on a running container exits non-zero, so a retry of a
          start that SUCCEEDED fails and finished work is recorded broken. Live:
          ADD50's start worked, its response was lost, and every retry after
          that could only fail because 111 was up.
§17.1241  A decision is recorded once and nothing could revise it. ADD99 was
          answered, the operator then corrected the answer (four capabilities to
          three), the record still said four, and ADD100 built four. Fixing the
          record took a reset, a full run to re-park the pause, and a second
          answer — for one sentence.
§17.1242  §17.1229 reads guests, addresses and API keys off the host; it did not
          read the HARDWARE, and the next step stopped to ask "what is the second
          graphics card?" — one `lspci` away, and one of the things the operator
          had answered "I am unsure" to.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import node_editor as ne
from app.modules import runbook_discovery as rd
from app.modules import runbook_preconditions as rp

JOB = "11111111-2222-3333-4444-555555555555"

PCT = """VMID       Status     Lock         Name
       111 running                 control-panel
       120 stopped                 caddy-proxy
"""
QM = """      VMID NAME                 STATUS
       106 palworld             running
"""


# ── §17.1240 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_starting_an_already_running_container_is_refused_with_the_guard_form():
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        got = await rp.unmet(["pct start 111"], SimpleNamespace(name="r"))
    assert len(got) == 1
    why = got[0]["why"]
    assert "ALREADY running" in why and "exits non-zero" in why
    assert "pct status 111 | grep -q running || pct start 111" in why


@pytest.mark.asyncio
async def test_starting_a_stopped_container_is_fine():
    """The vacuity check — this is the normal, correct case."""
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        assert await rp.unmet(["pct start 120"], SimpleNamespace(name="r")) == []


@pytest.mark.asyncio
async def test_stopping_an_already_stopped_container_is_refused_too():
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        got = await rp.unmet(["pct stop 120"], SimpleNamespace(name="r"))
    assert got and "ALREADY stopped" in got[0]["why"]


@pytest.mark.asyncio
async def test_the_existing_needs_running_refusal_is_unchanged():
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        got = await rp.unmet(["pct exec 120 -- true"], SimpleNamespace(name="r"))
    assert got and "is stopped" in got[0]["why"]


# ── §17.1242 ─────────────────────────────────────────────────────────────

LSPCI = """02:00.0 3D controller [0302]: NVIDIA Corporation GP102GL [Tesla P40] [10de:1b38] (rev a1)
08:01.0 VGA compatible controller [0300]: Matrox Electronics Systems Ltd. MGA G200eW WPCM450 [102b:0532] (rev 0a)
83:00.0 VGA compatible controller [0300]: NVIDIA Corporation GP106 [GeForce GTX 1060 3GB] [10de:1c02] (rev a1)
"""


def test_the_real_lspci_output_is_read_correctly():
    gpus = rd.gpus_from_lspci(LSPCI)
    assert [g["model"] for g in gpus] == ["Tesla P40", "MGA G200eW WPCM450", "GeForce GTX 1060 3GB"]
    assert [g["compute"] for g in gpus] == [True, False, True]


def test_a_pci_id_is_never_reported_as_a_model():
    """The Matrox line has no bracketed marketing name, so the first version read
    `[102b:0532]` as the model and would have told the operator their card was
    called "102b:0532"."""
    gpus = rd.gpus_from_lspci(LSPCI)
    assert all(":" not in g["model"] for g in gpus), [g["model"] for g in gpus]


def test_the_boards_console_video_is_not_offered_as_compute():
    for line in ("00:1f.0 VGA compatible controller [0300]: ASPEED Technology, Inc. ASPEED Graphics Family [1a03:2000]",
                 "08:01.0 VGA compatible controller [0300]: Matrox Electronics Systems Ltd. MGA G200eW [102b:0532]"):
        g = rd.gpus_from_lspci(line)
        assert g and g[0]["compute"] is False, line


@pytest.mark.asyncio
async def test_the_hardware_block_says_which_cards_can_run_models():
    with patch.object(rd, "_read", AsyncMock(return_value=LSPCI)):
        block = await rd.hardware_facts(SimpleNamespace(name="pve-runner"))
    assert "Tesla P40" in block and "GeForce GTX 1060 3GB" in block
    assert "2 cards that can run models" in block
    assert "do NOT ask the operator about it" in block
    assert "console video" in block


@pytest.mark.asyncio
async def test_an_unreadable_host_yields_no_hardware_block():
    with patch.object(rd, "_read", AsyncMock(return_value="")):
        assert await rd.hardware_facts(SimpleNamespace(name="r")) == ""
    assert await rd.hardware_facts(None) == ""


def test_the_drafter_is_given_the_hardware():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.draft_runbook)
    assert "hardware_facts(spec)" in src
    assert src.index("host_inventory(spec)") < src.index("hardware_facts(spec)")


# ── §17.1241 ─────────────────────────────────────────────────────────────


def _nodes(status="done", node_type="decision"):
    return [{"node_key": "ADD99", "status": status, "node_type": node_type,
             "title": "Decide: what should the panel do?", "depends_on": [], "execution_order": 1},
            {"node_key": "ADD100", "status": "done", "node_type": "task",
             "title": "Rebuild the panel", "depends_on": ["ADD99"], "execution_order": 2}]


def _db(prior="Decision (operator): four things"):
    db = AsyncMock()
    res = MagicMock(); res.scalar.return_value = prior
    db.execute = AsyncMock(return_value=res); db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_a_revision_rewrites_the_record_and_rebuilds_what_read_it():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes())), \
         patch.object(ne, "_reset_keys", AsyncMock()) as reset, \
         patch.object(ne, "_reopen_job", AsyncMock()), \
         patch.object(ne, "_audit", AsyncMock()) as audit:
        out = await ne.revise_decision(JOB, "ADD99", choice="THREE things, only the engine",
                                       note="they corrected it", db=db)
    assert out["status"] == "ok"
    assert "THREE things, only the engine" in out["decision"]
    assert out["rebuilt"] == ["ADD100"]
    assert reset.await_args.args[2] == ["ADD100"]
    # the old answer is kept, so the revision is reversible
    assert audit.await_args.args[4]["output_text"] == "Decision (operator): four things"
    assert audit.await_args.args[3] == "revise_decision"


@pytest.mark.asyncio
async def test_a_revision_needs_an_answer():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes())) as load:
        for bad in ("", "   "):
            out = await ne.revise_decision(JOB, "ADD99", choice=bad, db=db)
            assert out["http_status"] == 422
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_an_answered_decision_can_be_revised():
    db = _db()
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes(status="pending"))):
        out = await ne.revise_decision(JOB, "ADD99", choice="x", db=db)
    assert out["http_status"] == 409 and "not been answered" in out["error"]
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes(node_type="task"))):
        out = await ne.revise_decision(JOB, "ADD99", choice="x", db=db)
    assert out["http_status"] == 409 and "not a decision node" in out["error"]
    with patch.object(ne, "_load_nodes", AsyncMock(return_value=_nodes())):
        out = await ne.revise_decision(JOB, "NOPE", choice="x", db=db)
    assert out["http_status"] == 404


def test_the_revise_route_exists():
    import inspect
    from app.routers import nodes as r
    src = inspect.getsource(r)
    assert '@router.post("/nodes/{job_id}/{node_key}/revise")' in src
    assert "revise_decision(" in src and "choice=body.choice" in src


# ── §17.1243: a create is not blocked by the thing not existing yet ────────


@pytest.mark.asyncio
async def test_a_create_and_the_commands_after_it_are_not_refused():
    """Live ADD111: `pct create 130 …` was refused with "there is no guest 130 on
    this host", and `pct start 130` / `pct exec 130 -- …` LATER IN THE SAME BLOCK
    were refused for the same reason. A correct 11-command runbook was unrunnable
    and the frame fell back to "I'll do it myself"."""
    block = ["pct create 130 local:vztmpl/debian-12.tar.zst --hostname pihole",
             "pct start 130",
             "pct exec 130 -- bash -c 'apt update'"]
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        assert await rp.unmet(block, SimpleNamespace(name="r")) == []


@pytest.mark.asyncio
async def test_a_create_onto_a_taken_id_IS_refused():
    """The opposite reason, which is the real one — and Proxmox shares one id
    space, so a VM's id collides with `pct create` too. Live, the engine offered
    CTID 100 while 100 is the gpu-vm (here the fixture's VM is 106)."""
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        ct = await rp.unmet(["pct create 111 tmpl"], SimpleNamespace(name="r"))
        vm = await rp.unmet(["pct create 106 tmpl"], SimpleNamespace(name="r"))
    assert ct and "already taken" in ct[0]["why"] and "a container" in ct[0]["why"]
    assert vm and "already taken" in vm[0]["why"] and "a VM" in vm[0]["why"]
    assert "one id space" in vm[0]["why"]


@pytest.mark.asyncio
async def test_an_exec_on_a_guest_nobody_creates_is_still_refused():
    """The vacuity check: the fix must not make every missing guest acceptable."""
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        got = await rp.unmet(["pct exec 999 -- true"], SimpleNamespace(name="r"))
    assert got and "there is no guest 999" in got[0]["why"]


@pytest.mark.asyncio
async def test_order_matters_a_start_BEFORE_its_create_is_still_refused():
    """`made` is built in command order, so a block that starts a guest before
    creating it is still caught — that block really is broken."""
    with patch.object(rp, "_read", AsyncMock(side_effect=lambda spec, c: PCT if c == "pct list" else QM)):
        got = await rp.unmet(["pct start 130", "pct create 130 tmpl"], SimpleNamespace(name="r"))
    assert got and "there is no guest 130" in got[0]["why"]
