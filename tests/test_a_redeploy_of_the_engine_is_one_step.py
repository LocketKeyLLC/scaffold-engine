"""§17.1622 — a redeploy of the engine is drafted from the engine's deploy template and is never split.

Live (job 0fb4ea7f RD1, 2026-10-16): "Inside VM 110: redeploy the scaffold engine's services ... the chat UI and API
reachable from the LAN" matched no template (`\\bdeploy` misses "redeploy"), and the splitter read "write" + "UI" +
"API" as two deliverables: five steps writing a made-up compose stack under /opt/engine.
"""
from __future__ import annotations

from app.modules import runbook_templates as rt
from app.modules import step_decomposition as sd

RD1 = {"title": "VM 110: redeploy engine services", "prompt_template": (
    "Inside VM 110: redeploy the scaffold engine's services at the newest commit of its ref (it carries §17.1621: the "
    "pipelines container could not write its valves and restarted forever), keeping the .env secrets, using the Ollama "
    "already running there; the chat UI and API reachable from the LAN (ports 3000 and 8000). Settings:\n"
    "SCAFFOLD_LAN_UI=1\nMODEL_ROUTER=qwen3:4b\nDone when http://localhost:8000/health answers inside the VM.")}


def test_a_redeploy_names_the_engine_deploy():
    assert rt._DEPLOY_ENGINE_RE.search(rt._text(RD1))


def test_the_live_redeploy_is_not_split():
    assert sd.too_large({}, RD1) == ""


def test_a_build_of_two_things_still_splits():
    node = {"title": "CT 111: rebuild the control panel", "prompt_template": "Write the backend and the frontend of the panel."}
    assert sd.too_large({}, node)
