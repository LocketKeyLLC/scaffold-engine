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
