r"""§17.1555/§17.1556/§17.1557 — the MCP machine-node seams the Jellyfin metadata job exposed.

Live (job 263e920a): T1 "Read Jellyfin API key" ran `grep … /var/lib/jellyfin/config/system.xml` through
the runner's read tool, the runner answered `grep: …: No such file or directory`, and the node was marked
DONE with that string as its output (§17.1556). T2/T3 spliced `${T1}`/`${T2}` into shell loops — bare
placeholders `render_args` did not know (§17.1557), so `for lib in ; do` ran as `/bin/sh: 1: Syntax error`
and was marked done too. The three verify steps then spliced THAT in as `${T3}`. A runner-read whose
command splices another node's output is not a deterministic call: it parks for approval like a write,
where the engine's capabilities and gates see it (§17.1555).
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.modules import supervised_runs as sr
from app.modules.assist_supervised import WRITE_TOOL
from app.modules.mcp_node import render_args, only_shell_errors


def _mcp(tool, command, key="T7"):
    return {"node_key": key, "title": "Verify: Name", "tool": "MCP", "prompt_template": "Verify: Name.",
            "tool_config": json.dumps({"server": "pve-runner", "tool": tool, "args": {"node": "101", "command": command}})}


SPLICED = "for item in ${T3}; do curl -s -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items/$item\"; done"
PLAIN = "curl -s http://127.0.0.1:8096/System/Info/Public"


# ── §17.1555 a spliced runner-read parks ───────────────────────────────────────────────────────────

def test_a_runner_read_that_splices_an_upstream_needs_the_pause():
    assert sr.mcp_splices_an_upstream(_mcp("run_readonly", SPLICED)) is True
    assert sr.mcp_splices_an_upstream(_mcp("run_readonly", "echo ${upstream.T2}")) is True
    assert sr.mcp_splices_an_upstream(_mcp("run_readonly", "echo ${brief.target}")) is True
    assert sr.mcp_needs_the_pause(_mcp("run_readonly", SPLICED)) is True


def test_a_plain_runner_read_and_a_non_runner_tool_do_not():
    assert sr.mcp_splices_an_upstream(_mcp("run_readonly", PLAIN)) is False
    assert sr.mcp_splices_an_upstream(_mcp("run_readonly", "echo $HOME ${PATH}")) is False   # shell vars, not nodes
    assert sr.mcp_splices_an_upstream(_mcp("search", SPLICED)) is False                       # not the runner's read
    assert sr.mcp_splices_an_upstream({"tool": "Shell", "title": "x"}) is False
    assert sr.mcp_needs_the_pause(_mcp("run_readonly", PLAIN)) is False


def test_a_runner_write_still_needs_the_pause_by_its_own_rule():
    assert sr.is_mcp_write(_mcp(WRITE_TOOL, PLAIN)) is True
    assert sr.mcp_splices_an_upstream(_mcp(WRITE_TOOL, SPLICED)) is False     # §17.1551's, not this rule's
    assert sr.mcp_needs_the_pause(_mcp(WRITE_TOOL, PLAIN)) is True


def test_the_frontier_holds_a_spliced_read_and_frames_its_rendered_command():
    node = _mcp("run_readonly", SPLICED)
    assert sr.is_hands_on_node(node) is True
    rb = sr.mcp_write_runbook(node, {"T3": ("id1 id2", 1.0), "T1": ("thekey", 1.0)}, {})
    assert rb is not None and "## Run this" in rb and "for item in id1 id2" in rb and "${T3}" not in rb


def test_a_plain_read_is_still_a_deterministic_call():
    from app.modules.step_classify import step_is_hands_on
    node = _mcp("run_readonly", PLAIN)
    on, _ = step_is_hands_on(node)
    assert sr.is_hands_on_node(node) == bool(on)          # §17.1551's delegation, unchanged
    assert sr.mcp_write_runbook(node, {}, {}) is None


# ── §17.1557 the bare ${T<n>} the planner writes ───────────────────────────────────────────────────

def test_a_bare_upstream_key_renders_and_an_unknown_one_stays_verbatim():
    up = {"T1": ("thekey", 1.0), "T2": ("lib1 lib2", 1.0), "T7v2": ("seven", 1.0)}
    out = render_args({"command": "for l in ${T2}; do x ${T1} ${T9} ${upstream.T2} ${brief.q} ${ T7v2 }; done"},
                      up, {"q": "Q"})
    assert out == {"command": "for l in lib1 lib2; do x thekey ${T9} lib1 lib2 Q seven; done"}


def test_shell_variables_that_are_not_node_keys_are_left_alone():
    assert render_args("echo $HOME ${PATH} ${T}", {"T1": "x"}, {}) == "echo $HOME ${PATH} ${T}"


# ── §17.1556 the runner's error is a failure, not a result ─────────────────────────────────────────

@pytest.mark.parametrize("text_out", [
    "grep: /var/lib/jellyfin/config/system.xml: No such file or directory\n",
    '/bin/sh: 1: Syntax error: "do" unexpected\n',
    "bash: line 1: jq: command not found",
    "curl: (7) Failed to connect to 127.0.0.1 port 8096 after 0 ms: Connection refused",
    "sh: 1: python3: not found\ngrep: x: No such file or directory",
])
def test_only_shell_errors_recognises_an_answer_that_is_nothing_but_complaints(text_out):
    assert only_shell_errors(text_out) is True


@pytest.mark.parametrize("text_out", [
    "", "   \n", "lib1 lib2", '{"Items": []}',
    "ok\ngrep: x: No such file or directory",          # a real result with one warning line is a result
    "No such file or directory is a common message",   # prose about the error, not the error
])
def test_only_shell_errors_leaves_a_real_result_alone(text_out):
    assert only_shell_errors(text_out) is False


def _db():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    return db


def _status_written(db):
    sql = " ".join(str(c.args[0]) for c in db.execute.await_args_list)
    return "failed" if "status = 'failed'" in sql else ("done" if "status = 'done'" in sql else None)


async def _execute(monkeypatch, node, *, text, structured=None, is_error=False):
    from app.modules import mcp_node, mcp_registry, mcp_client
    spec = MagicMock(); spec.enabled = True
    monkeypatch.setattr(mcp_registry, "get_server", AsyncMock(return_value=spec))
    res = MagicMock(); res.text = text; res.structured = structured; res.is_error = is_error
    monkeypatch.setattr(mcp_client, "call_tool", AsyncMock(return_value=res))
    db = _db()
    out = await mcp_node.execute_mcp_node(db, node={**node, "id": "nid"}, upstream_outputs={}, brief={}, job_id="j")
    return out, db


@pytest.mark.asyncio
async def test_a_read_that_came_back_as_shell_errors_fails_the_node(monkeypatch):
    err = "grep: /var/lib/jellyfin/config/system.xml: No such file or directory\n"
    out, db = await _execute(monkeypatch, _mcp("run_readonly", "grep x /etc/y", key="T1"),
                             text=json.dumps({"result": err}), structured={"result": err})
    assert out["status"] == "failed" and _status_written(db) == "failed"
    assert "shell errors" in out["error"] and "No such file" in out["error"] and "§17.1556" in out["error"]


@pytest.mark.asyncio
async def test_a_runner_refusal_or_tool_error_fails_the_node(monkeypatch):
    refused = "(refused by the local runner: writes)"
    out, db = await _execute(monkeypatch, _mcp("run_readonly", PLAIN), text=refused, structured={"result": refused})
    assert out["status"] == "failed" and "refused" in out["error"]
    out2, db2 = await _execute(monkeypatch, _mcp("search", "q"), text="boom", is_error=True)
    assert out2["status"] == "failed" and "error" in out2["error"]


@pytest.mark.asyncio
async def test_a_real_answer_is_done_and_stored_unwrapped(monkeypatch):
    body = '[{"Name":"Movies","ItemId":"aaa"}]'
    out, db = await _execute(monkeypatch, _mcp("run_readonly", PLAIN), text=json.dumps({"result": body}),
                             structured={"result": body})
    assert out["status"] == "done" and _status_written(db) == "done"
    assert out["output"] == body                                   # the shell output itself, not the envelope
