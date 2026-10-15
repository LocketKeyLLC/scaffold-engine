"""§17.1551 — a runner-write MCP node parks for operator approval, never runs deterministically.

Live, Jellyfin metadata-pass job 263e920a, T4: a ``tool='MCP'`` node targeting the pve-runner's
write tool ``run_supervised`` with a refresh POST. It executed deterministically with no engine↔runner
``approval`` and hard-failed (``approval Field required``). Auto-minting the approval in execution was
refused by the security classifier — it would bypass the write-approval gate. So such a node now parks
for approval like any hands-on write (``pending_hands_on`` surfaces it, ``_pause_for_decision`` frames
it with ``mcp_write_runbook``), and the approved supervised path mints the approval and runs it.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.modules import supervised_runs as sr
from app.modules.assist_supervised import WRITE_TOOL


def _mcp(tool, command="curl -X POST http://127.0.0.1:8096/Items/x/Refresh", server="pve-runner"):
    return {"node_key": "T4", "title": "Trigger refresh", "tool": "MCP",
            "tool_config": json.dumps({"server": server, "tool": tool, "args": {"command": command}})}


def test_is_mcp_write_true_for_runner_write():
    assert sr.is_mcp_write(_mcp(WRITE_TOOL)) is True


def test_is_mcp_write_false_for_read_tool():
    assert sr.is_mcp_write(_mcp("run_readonly")) is False


def test_is_mcp_write_false_for_non_mcp_node():
    assert sr.is_mcp_write({"tool": "Shell", "title": "x", "description": "y"}) is False


def test_is_mcp_write_false_without_a_command():
    assert sr.is_mcp_write(_mcp(WRITE_TOOL, command="")) is False


def test_mcp_write_runbook_fences_the_command_under_run_this():
    rb = sr.mcp_write_runbook(_mcp(WRITE_TOOL, command="echo hi"), {}, {})
    assert rb is not None
    assert "## Run this" in rb and "```bash" in rb and "echo hi" in rb


def test_mcp_write_runbook_renders_an_upstream_placeholder():
    rb = sr.mcp_write_runbook(_mcp(WRITE_TOOL, command="for l in ${upstream.T2}; do :; done"),
                              {"T2": "lib1 lib2"}, {})
    assert rb is not None and "lib1 lib2" in rb and "${upstream" not in rb


def test_mcp_write_runbook_is_none_for_a_read_node():
    assert sr.mcp_write_runbook(_mcp("run_readonly"), {}, {}) is None


def test_is_hands_on_node_true_for_runner_write_mcp():
    # the frontier's hands-on predicate (peek + claim) must hold a runner-write MCP node for approval,
    # not run it deterministically.
    assert sr.is_hands_on_node(_mcp(WRITE_TOOL)) is True


def test_is_hands_on_node_delegates_for_a_read_mcp_node():
    # §17.1551 only short-circuits a runner-WRITE node to hands-on; a read MCP node's verdict is
    # whatever step_is_hands_on decides (unchanged). Compare to that, not a hardcoded value —
    # step_is_hands_on classifies by command content and is not this fix's concern.
    from app.modules.step_classify import step_is_hands_on
    node = _mcp("run_readonly", command="curl -s http://127.0.0.1:8096/Items")
    on, _ = step_is_hands_on(node)
    assert sr.is_hands_on_node(node) == bool(on)


@pytest.mark.asyncio
async def test_execute_mcp_node_refuses_a_runner_write_instead_of_calling_it(monkeypatch):
    # defense-in-depth: if a runner-write node ever reaches deterministic execution (the pause was
    # bypassed), it fails clearly and NEVER calls the tool unapproved.
    from app.modules import mcp_node, mcp_registry, mcp_client
    spec = MagicMock(); spec.enabled = True
    monkeypatch.setattr(mcp_registry, "get_server", AsyncMock(return_value=spec))
    called = {}

    async def _never(*a, **k):
        called["hit"] = True
        return MagicMock()
    monkeypatch.setattr(mcp_client, "call_tool", _never)

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    node = {"id": "00000000-0000-0000-0000-000000000001", "node_key": "T4", "title": "refresh",
            "tool_config": json.dumps({"server": "pve-runner", "tool": WRITE_TOOL,
                                       "args": {"command": "curl -X POST http://x/Refresh"}})}
    res = await mcp_node.execute_mcp_node(db, node=node, upstream_outputs={}, brief={}, job_id="j")
    assert res["status"] == "failed"
    assert "17.1551" in (res.get("error") or "")
    assert "hit" not in called            # the tool was never called unapproved


@pytest.mark.asyncio
async def test_execute_mcp_node_still_runs_a_read_node(monkeypatch):
    # a read MCP node is unaffected — it still dispatches deterministically.
    from app.modules import mcp_node, mcp_registry, mcp_client
    spec = MagicMock(); spec.enabled = True
    monkeypatch.setattr(mcp_registry, "get_server", AsyncMock(return_value=spec))
    result = MagicMock(); result.text = "ok"
    monkeypatch.setattr(mcp_client, "call_tool", AsyncMock(return_value=result))
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    node = {"id": "00000000-0000-0000-0000-000000000002", "node_key": "T3", "title": "enumerate",
            "tool_config": json.dumps({"server": "pve-runner", "tool": "run_readonly",
                                       "args": {"command": "curl -s http://x/Items"}})}
    res = await mcp_node.execute_mcp_node(db, node=node, upstream_outputs={}, brief={}, job_id="j")
    assert res["status"] == "done"


# ── §17.1573b — a step defined by "the table / the unit" inherits its dependencies' text (two levels) ─────────
@pytest.mark.asyncio
async def test_an_anaphoric_step_inherits_its_dependency_and_that_dependencys_dependency():
    rows = {
        "T2": {"node_key": "T2", "title": "Write nftables rule file", "description": "Dedicated bridge-family table 'block_ra'. "
               "prerouting chain drops RA frames forwarded to guests on vmbr0; input chain drops RA frames delivered to the host.",
               "prompt_template": "", "depends_on": ["T1"]},
        "T3": {"node_key": "T3", "title": "Write systemd unit file", "description": "Small oneshot unit that loads the dedicated table at boot.",
               "prompt_template": "", "depends_on": ["T2"]},
    }

    async def _execute(stmt, params):
        q = str(stmt)
        res = MagicMock()
        if "SELECT depends_on" in q:
            res.scalar = MagicMock(return_value=rows[params["k"]]["depends_on"])
            return res
        keys = params.get("keys") or []
        res.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[rows[k] for k in keys if k in rows])))
        return res

    db = MagicMock(); db.execute = AsyncMock(side_effect=_execute)
    # a Shell step with no command of its own: nothing but "the table" says what it is about
    t4 = {"node_key": "T4", "title": "Enable and start unit", "description": "Loads the table now and ensures it loads at boot.",
          "prompt_template": "", "depends_on": ["T3"], "tool": "Shell"}
    out = await sr.inherit_referenced_step_text(db, "job", t4)
    assert "[as in T3: Write systemd unit file" in out["description"]            # its dependency
    assert "[as in T2: Write nftables rule file" in out["description"]           # and the one that defines "the table"
    assert out["description"].startswith("Loads the table now")                 # own text first
    # a step that names its object inherits nothing
    named = dict(t4, description="Enable scaffold-rafilter.service now.")
    db2 = MagicMock(); db2.execute = AsyncMock(side_effect=_execute)
    assert (await sr.inherit_referenced_step_text(db2, "job", named))["description"] == "Enable scaffold-rafilter.service now."
    db2.execute.assert_not_called()
