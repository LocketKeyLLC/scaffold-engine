"""§17.1967 — a test that ran and found faults is done, with its findings; a test whose own commands broke is failed.

Shapes from job 7cc2a7ac (2026-11-20): T15's Playwright run (10 of 12 sections passed, exit 1), T17's engine control
(HTTP 500 after the media control answered), T9's route sweep (`200` and `000` lines, refuted by its own check).
Addresses and names below are made up.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import conditional_steps as cs
from app.modules import supervised_runs as sr
from app.modules import found_faults as tf
from app.modules.execution_agent import _result_of

PANEL = "http://10.77.0.5:3001"
CURL = "curl -sS --fail-with-body -m 60 --config /dev/stdin -H 'Content-Type: application/json' -X POST"

T15 = {"title": "Browser-test page sections",
       "description": "Playwright headless: load each section, assert real data, no stuck 'Loading'."}
T15_RUN = [
    {"command": "mkdir -p /opt/pw/shots", "output": "", "exit": 0, "ok": True},
    {"command": f"bash -c 'curl -s -o /dev/null -w \"%{{http_code}}\\n\" {PANEL}/'", "output": "200", "exit": 0, "ok": True},
    {"command": f"bash -c 'cd /opt/pw && node sections.mjs {PANEL} panel /opt/pw/shots'", "exit": 1, "ok": False,
     "output": "PASS Overview  shot=/opt/pw/shots/overview.png\nPASS Health  shot=/opt/pw/shots/health.png\n"
               "FAIL Media Request  shot=/opt/pw/shots/media_request.png\n     - stuck on \"Loading\"\n"
               "PASS Network View  shot=/opt/pw/shots/network_view.png\n"
               "console/page errors seen (1):\n  Failed to load resource: the server responded with a status of 503\n"
               "=== 3 of 4 sections passed ==="},
]
T17 = {"title": "Proxmox host: use the control panel media and engine controls once and confirm each effect",
       "description": "Done when the media request answers and the engine request is listed."}
T17_RUN = [
    {"command": "bash -c 'test -n \"$PANEL_PASSWORD\" && echo set'", "output": "set", "exit": 0, "ok": True},
    {"command": f"bash -c '{CURL} --data '{{\"title\":\"Some Film\"}}' {PANEL}/api/media-request'",
     "output": '{"title":"Some Film","id":2}', "exit": 0, "ok": True},
    {"command": f"bash -c '{CURL} --data '{{\"brief\":\"x\"}}' {PANEL}/api/engine/request'", "exit": 22, "ok": False,
     "output": "curl: (22) The requested URL returned error: 500\n{\"error\":\"HTTP 404: {\\\"detail\\\":\\\"Not Found\\\"}\"}"},
]
T9 = {"title": "Curl API routes direct",
      "description": f"Same curl -sf -m 3 sweep directly against {PANEL} to isolate Caddy vs backend faults. Read-only."}


def _probe(route, code, rc=0):
    return {"command": f"bash -c 'curl -s -o /dev/null -w \"{route} %{{http_code}} %{{time_total}}s\\n\" -m 3 {PANEL}/api/{route}'",
            "output": f"{route} {code} {'3.002514' if code == '000' else '0.004'}s", "exit": rc, "ok": rc == 0,
            "informational": rc != 0}


T9_RUN = [_probe("capabilities", "200"), _probe("health", "000", 28), _probe("media-library", "000", 28),
          _probe("network-view", "200")]
T9_REFUTED = [{"verdict": "contradicted", "reason": "'via-caddy 000 3.002345s' — no HTTP status at all"}]


@pytest.mark.parametrize("title", [
    "Browser-test page sections", "Test media routes", "Proxmox host: test the control panel game-server controls",
    "Proxmox host: exercise the Palworld start/stop control and confirm the effect", T17["title"]])
def test_a_step_that_tests_is_named(title):
    assert tf.finds_faults({"title": title})


def test_a_step_that_says_it_finds_faults_is_named():
    assert tf.finds_faults(T9)
    assert tf.finds_faults({"title": "Verify control panel sections", "description": "Report pass/fail per section."})


@pytest.mark.parametrize("node", [
    {"title": "Verify runner can route to the panel"}, {"title": "Install Playwright toolchain"},
    {"title": "CT 111: give the media-library route a time limit so it never hangs"},
    {"title": "Check SSH access", "description": "Done when it reports no failures."},   # after Done when: not its purpose
])
def test_a_gate_or_a_change_is_not_a_test(node):
    assert not tf.finds_faults(node)


@pytest.mark.parametrize("node,run,refuted,n_fault", [(T15, T15_RUN, [], 2), (T17, T17_RUN, [], 1),
                                                      (T9, T9_RUN, T9_REFUTED, 2)])
def test_the_live_shapes_are_done_with_findings(node, run, refuted, n_fault):
    section, why = tf.findings_of_a_run(node, run, refuted)
    assert section.startswith("## Findings") and why.startswith(tf.FINDINGS_MARK)
    assert f"found {n_fault} fault(s)" in why, why


def _with(run, i, **kw):
    out = [dict(e) for e in run]
    out[i].update(kw)
    return out


@pytest.mark.parametrize("node,run,refuted", [
    # every probe failed: a broken test cannot be told from a broken target
    (T17, [T17_RUN[0], T17_RUN[2]], []),
    (T9, [_probe("health", "000", 28), _probe("overview", "000", 28)], T9_REFUTED),
    # the test's own tooling broke
    (T15, _with(T15_RUN, 2, output="PASS Overview\nError [ERR_MODULE_NOT_FOUND]: Cannot find package 'playwright'"), []),
    (T15, _with(T15_RUN, 2, exit=127, output="PASS Overview\nFAIL x\nbash: node: not here"), []),
    (T17, _with(T17_RUN, 2, output="curl: (22) The requested URL returned error: 401"), []),     # its own credential
    # a setup command, not the probe, stopped the block
    (T15, T15_RUN[:1] + [dict(T15_RUN[1], exit=1, ok=False, output="200")], []),
    # a check that printed nothing found nothing
    (T9, T9_RUN, [{"verdict": "contradicted", "kind": "blank", "reason": "printed nothing (§17.1738)"}]),
    # nothing said it failed
    (T9, T9_RUN, []),
    # the same run, by a step that does not test
    ({"title": "CT 111: send the panel's engine request to the engine's job route"}, T17_RUN, []),
])
def test_correct_misses(node, run, refuted):
    assert tf.findings_of_a_run(node, run, refuted) == ("", "")


async def _resolve(node, run):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmds = [e["command"] for e in run]
    waiting = {"kind": "run", "node_key": "T17", **node, "prompt_template": node.get("description", ""),
               "runbook": "## Run this", "commands": cmds, "verify": [], "refused": [], "files": []}
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(cmds, [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=[dict(e) for e in run])), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        res = await sr.resolve_run(db, "j", "T17", "run", waiting)
    final = db.execute.await_args_list[-1].args[1]
    return res, final


@pytest.mark.asyncio
async def test_resolve_run_records_the_test_done_with_findings_and_the_change_failed():
    res, row = await _resolve(T17, T17_RUN)
    assert res["node_status"] == "done", res
    assert row["why"].startswith(tf.FINDINGS_MARK) and "HTTP 500" in row["why"]
    assert "## Findings" in row["out"]
    assert "## Findings" in _result_of(row["out"])          # what the steps after it read
    res, row = await _resolve({"title": "CT 111: send the panel's engine request to the job route"}, T17_RUN)
    assert res["node_status"] == "failed", res


def test_a_fix_conditional_on_a_test_with_findings_still_runs():
    fix = {"prompt_template": "Only if T15 found failures: fix the media sections."}
    found = [{"node_key": "T15", "status": "done", "last_verification_reason": tf.FINDINGS_MARK + " — FAIL Media Request"}]
    clean = [{"node_key": "T15", "status": "done", "last_verification_reason": "supervised run: all exited 0"}]
    assert cs.unmet_condition(fix, found) is None
    assert cs.unmet_condition(fix, clean)
