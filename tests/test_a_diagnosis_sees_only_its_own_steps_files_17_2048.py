"""§17.2048 — a failing step's diagnosis is directed at ITS OWN files only.

Live (job 0fb4ea7f, 2026-11-26 21:47:54 UTC): step RDV80, a read-only
read-back that writes no files, failed. The fix path logged

    assist_file_size_mismatch node_key=RDV80 [
      {'path': '/etc/pve/firewall/cluster.fw', 'expected': 45, 'observed': 872},
      {'path': '/tmp/Caddyfile', 'expected_lines': 17, 'observed_lines': 3}]

— files other, much older steps of the job wrote and measured — and handed
them to the fix system prompt as the FILE MISMATCH directive, the dominant
fact of the turn. The `file_writes` ledger is session-wide and keyed by path;
nothing recorded which step a write or a measurement belonged to.

These tests drive the PRODUCTION fix path (`generate_fix`, model mocked) and
read the system prompt it actually sends, with a ledger holding several files:
some the failing step wrote or measured, some other steps did, one from before
provenance existed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.assist_files import content_fingerprint


def _ctx(node_key: str):
    from app.modules.assist_guide import StepContext
    return StepContext(
        node_key=node_key, title="Read back the reverse proxy config", tool="Shell",
        domain=None, system_prompt="sys", base_prompt="read back the config",
        upstream_outputs={}, upstream_truncated_keys=[], grounding="",
        grounding_kind=None, assembled_prompt="## Task\nread back the config",
    )


def _ledger() -> dict:
    """Five files, three steps, and one entry from before provenance."""
    return {
        # Another step's firewall write, measured by that step long ago.
        "/etc/pve/firewall/cluster.fw": {
            "expected": 45, "lines": 3, "sha": "a" * 16, "observed": 872,
            "observed_sha": None, "node_key": "RDV12", "observed_node_key": "RDV12"},
        # Another step's Caddyfile, pasted back wrong in that step.
        "/tmp/Caddyfile": {
            "expected": 400, "lines": 17, "sha": content_fingerprint("x\n" * 17),
            "observed": None, "observed_sha": content_fingerprint("y\n" * 3),
            "observed_lines": 3, "node_key": "RDV31", "observed_node_key": "RDV31"},
        # A legacy entry: no recorded step at all.
        "/opt/legacy/app.conf": {
            "expected": 300, "lines": 9, "sha": "b" * 16, "observed": 12,
            "observed_sha": None},
        # The failing step's OWN write, clipped.
        "/opt/proxy/site.conf": {
            "expected": 2287, "lines": 60, "sha": "c" * 16, "observed": 1000,
            "observed_sha": None, "node_key": "RDV80", "observed_node_key": "RDV80"},
        # Written by an earlier step, but the FAILING step's own paste measured
        # it short — that measurement is this step's evidence (§17.965's case).
        "/opt/proxy/upstreams.conf": {
            "expected": 500, "lines": 12, "sha": "d" * 16, "observed": 120,
            "observed_sha": None, "node_key": "RDV40", "observed_node_key": "RDV80"},
    }


async def _fix_system_prompt(node_key: str, ledger: dict) -> str:
    from app.modules import assist_evidence, assist_guide
    draft = SimpleNamespace(text="## Fix\n```bash\ncat /opt/proxy/site.conf\n```",
                            success=True, error=None, model="m", raw={})
    with patch.object(assist_evidence.settings, "assist_answer_verification_enabled", False), \
            patch.object(assist_guide.model_router, "chat",
                         new=AsyncMock(return_value=draft)) as chat:
        await assist_guide.generate_fix(
            ctx=_ctx(node_key), error_text="curl: (7) Failed to connect",
            research=False, node_key=node_key,
            environment={"file_writes": ledger},
        )
    head = assist_guide.GUIDE_SYSTEM_FIX.strip()[:60]
    for call in chat.await_args_list:
        messages = call.args[0] if call.args else call.kwargs.get("messages") or []
        for m in messages:
            if m.get("role") == "system" and head in m.get("content", ""):
                return m["content"]
    raise AssertionError("generate_fix never sent its fix system prompt")


def _directive(system: str) -> str:
    """The FILE MISMATCH directive's text, or "" when it is absent."""
    from app.modules.assist_directives import apply_file_mismatch
    marker = apply_file_mismatch("", mismatches=[{"path": "/m", "expected": 2,
                                                  "observed": 1, "short": True,
                                                  "missing": 1}])
    head = marker.strip().split("\n")[0][:30]
    i = system.find(head)
    return system[i:] if i >= 0 else ""


@pytest.mark.asyncio
async def test_other_steps_mismatches_do_not_reach_the_fix_directive():
    system = await _fix_system_prompt("RDV80", _ledger())
    directive = _directive(system)
    assert directive, "the failing step's own clipped file must still drive the directive"
    # The failing step's OWN evidence reaches it …
    assert "/opt/proxy/site.conf" in directive
    assert "/opt/proxy/upstreams.conf" in directive
    # … and no other step's file does, nor the unattributed legacy entry.
    assert "/etc/pve/firewall/cluster.fw" not in directive
    assert "/tmp/Caddyfile" not in directive
    assert "/opt/legacy/app.conf" not in directive


@pytest.mark.asyncio
async def test_a_step_that_wrote_and_measured_nothing_gets_no_file_directive():
    """The live shape exactly: RDV80 wrote no file and measured none."""
    ledger = {k: v for k, v in _ledger().items() if not k.startswith("/opt/proxy/")}
    system = await _fix_system_prompt("RDV80", ledger)
    assert _directive(system) == "", _directive(system)[:300]


@pytest.mark.asyncio
async def test_the_other_step_still_gets_its_own_mismatch():
    """Scoping is not suppression: the step that wrote the firewall file is
    still told, when IT is the one being diagnosed."""
    directive = _directive(await _fix_system_prompt("RDV12", _ledger()))
    assert "/etc/pve/firewall/cluster.fw" in directive
    assert "/opt/proxy/site.conf" not in directive


def test_the_ledger_records_which_step_wrote_and_which_measured():
    """Provenance is recorded where entries are written (§17.2048)."""
    from app.modules.assist_files import merge_file_writes
    led = merge_file_writes({}, written={"/opt/a.conf": {"expected": 10, "lines": 1,
                                                         "sha": "s", "body": "x"},
                                         "/opt/b.conf": {"expected": 20, "lines": 2,
                                                         "sha": "t", "body": "y"}},
                            node_key="S1")
    led = merge_file_writes(led, observed={"/opt/a.conf": 4}, node_key="S2")
    led = merge_file_writes(led, contents={"/opt/b.conf": "z\n"}, node_key="S3")
    assert led["/opt/a.conf"]["node_key"] == "S1"
    assert led["/opt/a.conf"]["observed_node_key"] == "S2"
    assert led["/opt/b.conf"]["observed_node_key"] == "S3"
    # A rewrite moves ownership and clears the stale measurement's owner.
    led = merge_file_writes(led, written={"/opt/a.conf": {"expected": 11, "lines": 1,
                                                          "sha": "u", "body": "q"}},
                            node_key="S4")
    assert led["/opt/a.conf"]["node_key"] == "S4"
    assert led["/opt/a.conf"]["observed_node_key"] is None


@pytest.mark.asyncio
async def test_capture_records_the_writing_step():
    """The production write site passes the reply's node_key through."""
    from app.config import settings
    from app.modules import assist_environment, assist_turns
    reply = "```bash\ncat > /opt/proxy/site.conf <<'EOF'\nlisten 8080\nEOF\n```"
    with patch.object(assist_environment, "set_environment",
                      new=AsyncMock(return_value={})) as se, \
            patch.object(settings, "assist_unified_memory_enabled", False):
        await assist_turns.capture_assistant_reply(
            session_id="s", node_key="RDV80", kind="guide", content=reply,
            db=AsyncMock())
    kw = se.await_args.kwargs
    assert kw["file_writes"] and kw["file_node_key"] == "RDV80"


def test_the_prompt_ledger_does_not_order_a_rewrite_of_another_steps_file():
    """The memory renderer (ask / verify / fix user prompt) agrees with the
    directive: another step's file is context, not an instruction."""
    from app.modules.assist_render import _file_ledger_blocks
    block = "\n".join(_file_ledger_blocks({"file_writes": _ledger()}, "RDV80"))
    lines = {ln.split("`")[1]: ln for ln in block.splitlines() if ln.startswith("- `")}
    assert "rewrite it in pieces" in lines["/opt/proxy/site.conf"]
    assert "rewrite it in pieces" not in lines["/etc/pve/firewall/cluster.fw"]
    assert "step RDV12" in lines["/etc/pve/firewall/cluster.fw"]
    assert "Rewrite it in pieces" not in lines["/tmp/Caddyfile"]
    assert "not recorded which" in lines["/opt/legacy/app.conf"]
    # Session-level (no step): byte-identical to before.
    from app.modules.assist_files import render_file_writes
    assert _file_ledger_blocks({"file_writes": _ledger()}, None)[0] == \
        render_file_writes(_ledger())


def test_a_contradiction_reaches_only_a_step_that_owns_one_side():
    from app.modules.assist_files import conflicts_for_step
    ledger = {"/srv/api/server.js": {"node_key": "S1"},
              "/srv/web/App.jsx": {"node_key": "S2"},
              "/srv/other.conf": {"node_key": "S9"}}
    conflicts = [{"kind": "shape", "client": "/srv/web/App.jsx",
                  "server": "/srv/api/server.js", "detail": "d"},
                 {"kind": "placeholder", "client": "/srv/other.conf", "detail": "p"}]
    assert [c["kind"] for c in conflicts_for_step(conflicts, ledger, "S1")] == ["shape"]
    assert [c["kind"] for c in conflicts_for_step(conflicts, ledger, "S9")] == ["placeholder"]
    assert conflicts_for_step(conflicts, ledger, "S5") == []
    assert len(conflicts_for_step(conflicts, ledger, None)) == 2
