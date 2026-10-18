"""§17.1635 — a command or path the step QUOTES is not what it asks for.

Live (job 0fb4ea7f PLF1, 2026-10-18): "correct the stale lines of PLACEMENT.md", whose line-43 fix read "(the redeploy
command) becomes `cd /opt/scaffold-engine && git fetch origin && …`", was drafted with the engine's DEPLOY template and
EMPTY settings. Approving it would have redeployed the server engine and corrected nothing.
"""
from __future__ import annotations

from pathlib import Path

from app.modules import runbook_templates as rt
from app.modules.step_decomposition import too_large

PLF1 = {"node_key": "PLF1", "title": "VM 110: correct the stale lines of PLACEMENT.md",
        "description": "Operator request 2026-10-18: correct PLACEMENT.md through the engine.",
        "prompt_template": "Inside VM 110: correct the stale lines of the placement record /opt/scaffold-engine/PLACEMENT.md. PLR1 printed it (45 lines) on 2026-10-18. Back the file up next to itself first (PLACEMENT.md.bak.<timestamp>). Then replace ONLY these blocks, reading the values from the machine at run time where one is read (never type a value the machine holds), and keep every other line exactly as it is, in place:\n- Lines 6-7 (`Tesla P40, 23040 MiB, 125.00 W, 89` and the `Power cap:` line) become two lines: `GPU: none passed through -- the Tesla P40 (host 02:00.0) was detached from VM 110 on 2026-10-17 (GPU0, operator decision 3, after the host crashes); it stays in the host, unused` and `Ollama runs on the CPU from /opt/ollama/docker-compose.nogpu.yml; gpu-power-cap.service fails at boot until the P40 is back`.\n- Lines 8-16 (the MODEL_ lines) become the orchestrator's live values, in the same order (MODEL_CODER, MODEL_GENERAL, MODEL_VERIFIER, MODEL_RESEARCH_EXTRACT, MODEL_ROUTER, MODEL_TRIAGE, MODEL_CLOUD_HEAVY, MODEL_CLOUD_ALT, MODEL_FALLBACK), read with `docker exec scaffold-orchestrator printenv`; only those nine variables, nothing else from its environment.\n- Lines 18-22 (the table under `Ollama models:`) become the live `docker exec ollama ollama list`.\n- Lines 29-39 (the service status lines) become the live `docker compose ps --all --format '{{.Service}} {{.State}}'` run in /opt/scaffold-engine, one per line, sorted.\n- Line 43 (the redeploy command) becomes `cd /opt/scaffold-engine && git fetch origin && git checkout --detach origin/main && bash scripts/deploy_on_guest.sh /root/scaffold-deploy.env`.\nDone when `grep '^MODEL_FALLBACK=' /opt/scaffold-engine/PLACEMENT.md` prints `MODEL_FALLBACK=deepseek-v4-pro:cloud` and `grep -i 'P40' /opt/scaffold-engine/PLACEMENT.md` prints `was detached from VM 110` and `grep -c 'qwen3.5:397b-cloud\\|pipelines restarting\\|git pull' /opt/scaffold-engine/PLACEMENT.md` prints `0`, and `cat -n /opt/scaffold-engine/PLACEMENT.md` prints it.\n"}
RD4 = {"node_key": "RD4", "title": "VM 110: redeploy engine services",
       "description": "Redeploy at main 1cb00182.",
       "prompt_template": "Inside VM 110: redeploy the scaffold engine's services at the newest commit of its ref."}


def test_a_quoted_command_or_a_path_does_not_ask_for_a_deploy():
    assert not rt.asks_to_deploy_the_engine(PLF1)
    assert not rt.asks_to_deploy_the_engine({"prompt_template": "the deploy log of /opt/scaffold-engine/PLACEMENT.md"})


def test_the_asks_that_are_deploys_still_are():
    assert rt.asks_to_deploy_the_engine(RD4)
    for t in ("VM 110: redeploy engine services", "Deploy the scaffold engine in VM 110",
              "deploy the engine's stack inside VM 110"):
        assert rt.asks_to_deploy_the_engine({"title": t}), t


def test_the_split_rule_reads_the_ask_the_same_way():
    """The sibling call site (§17.1622's exemption) uses the same judgement: PLF1 is not exempted as a deploy."""
    import inspect
    assert "asks_to_deploy_the_engine(" in inspect.getsource(too_large)


def test_each_protection_holds_on_its_own():
    # only the masking: a quoted phrase with no path in it
    assert not rt.asks_to_deploy_the_engine({"prompt_template": "Done when the log prints `redeploy the scaffold engine`."})
    # only the path rule: an unquoted path after the verb
    assert not rt.asks_to_deploy_the_engine({"prompt_template": "Read the redeploy notes in /opt/scaffold-engine/README."})
