"""§17.1496 — a frame is drawn off the event loop.

`frame_run` is synchronous and forks `mypy` (up to 25 s) for every drafted program and
`bash -n` for every written script. The executor called it directly from `async def
_pause_for_decision` -- ten times across the redraft chain -- so every SSE stream and every
HTTP request the orchestrator was serving froze for the duration. The detached run is
in-process (`run_broker`), so nothing isolated it. Every call now goes through
`asyncio.to_thread`.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr


def _calls(tree):
    direct, threaded = [], []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == "frame_run" and isinstance(f.value, ast.Name) \
                and f.value.id == "supervised_runs":
            direct.append(n.lineno)
        if isinstance(f, ast.Attribute) and f.attr == "to_thread" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Attribute) and a.attr == "frame_run":
                threaded.append(n.lineno)
    return direct, threaded


def test_the_executor_never_calls_frame_run_on_the_loop():
    tree = ast.parse(pathlib.Path(ea.__file__).read_text(encoding="utf-8"))
    direct, threaded = _calls(tree)
    assert direct == [], f"frame_run called on the event loop at lines {direct}"
    assert len(threaded) >= 10, threaded


def test_frame_run_forks_a_subprocess_so_the_loop_must_not_run_it():
    """The reason the rule exists: the program checks fork. If this ever stops being true the
    gate above is merely redundant, never wrong."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    assert 'subprocess.run(["bash", "-n"]' in src
    from app.modules import program_check
    assert "subprocess.run(" in pathlib.Path(program_check.__file__).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_frame_drawn_in_a_thread_is_the_frame_drawn_inline():
    node = {"node_key": "T1", "title": "Restart caddy in CT 120"}
    runbook = ("## Run this\n```bash\npct exec 120 -- systemctl restart caddy\n```\n"
               "## Verify\n- caddy is up: `pct exec 120 -- systemctl is-active caddy`\n")
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["pct exec"], "sudo": False}
    inline = sr.frame_run(node, runbook, spec, policy)
    threaded = await asyncio.to_thread(sr.frame_run, node, runbook, spec, policy)
    assert threaded == inline
