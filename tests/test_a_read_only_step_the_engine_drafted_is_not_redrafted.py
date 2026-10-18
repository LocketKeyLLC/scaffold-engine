r"""§17.1577 — hands-on is not "changes"; a frame a capability drafted is final.

Live (c2792d4b T2 "Probe each endpoint authenticated … record status code, response body … Do not modify anything",
tool Shell, 2026-10-15): the engine's own read-only probe (§17.1574c) claimed the step, then §17.1254 read the
planner's tool (`tool:shell`) as "this step must CHANGE something", sent the frame back as "EVERY COMMAND ONLY LOOKS
AT THINGS", and asked the model for a write. The model's cookie draft came back, the rewrite claimed it again, and
the operator was shown the cookie prose above a block that would run the engine's probe.
"""
from __future__ import annotations

import inspect

from app.modules import supervised_runs as sr

LIVE_T2 = {"node_key": "T2", "title": "Probe each endpoint authenticated", "description": "", "tool": "Shell",
           "prompt_template": "Using the panel user's session cookie, curl each endpoint on container 111 and record "
                              "status code, response body, and whether it hangs. Do not modify anything."}
INV = {"cts": {"111": "running"}, "vms": {}, "names": {"111": "control-panel"}, "unread": []}
LOGINS = [{"name": "PANEL_PASSWORD", "user": "panel", "hint": "the control panel's login password (user `panel`)"}]
READS = ["pct exec 111 -- sh -c 'curl -s http://127.0.0.1:3001/api/health'", "pct exec 111 -- ss -tlnp"]


def test_a_shell_step_whose_words_only_read_is_not_asked_to_change():
    probe = sr.rewrite_probe_service_endpoints(LIVE_T2, ["x"], [], [], INV, logins=LOGINS)[0]
    assert sr.all_reads_for_a_changing_step(probe, LIVE_T2) == ""
    assert sr.all_reads_for_a_changing_step(READS, LIVE_T2) == ""
    assert not sr.step_says_it_changes(LIVE_T2)
    for title in ("Verify endpoint responses authenticated", "Inventory panel endpoints", "Check container 107 route",
                  "Measure the panel's response times"):
        node = {"title": title, "tool": "Shell", "description": "on container 111"}
        assert sr.all_reads_for_a_changing_step(READS, node) == "", title


def test_a_shell_step_whose_words_change_is_still_caught_when_its_block_only_reads():
    for title in ("Restart backend service", "Fix Radarr endpoint backend", "Install curl in container 111",
                  "Configure the panel's Radarr key"):
        node = {"title": title, "tool": "Shell", "description": "on container 111, the control panel"}
        assert sr.step_says_it_changes(node), title
        note = sr.all_reads_for_a_changing_step(READS, node)
        assert note and "ONLY LOOKS AT THINGS" in note, title
    # a command that writes in the step's own words says so, whatever the title
    body = {"title": "Panel backend: /api/media-library", "tool": "Shell",
            "prompt_template": "Run `curl -X POST http://127.0.0.1:3001/api/reload` after editing the config"}
    assert sr.step_says_it_changes(body)
    # the step's own "do not modify" / "read-only" outranks a change-looking title
    assert not sr.step_says_it_changes({"title": "Update the inventory", "tool": "Shell", "description": "read-only: list what is there"})
    assert not sr.step_says_it_changes({"title": "Fix the count", "tool": "Shell", "description": "Do not change anything; report the number."})


def test_the_executor_never_redrafts_a_frame_a_capability_drafted():
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    # §17.1641b — and a frame the engine composed as a check (api_route_check)
    assert '_ro = "" if (_developed or frame.get("expects") or _engine_check) else supervised_runs.all_reads_for_a_changing_step(frame.get("commands") or [], run_node)' in src
    assert '_nv = "" if (frame.get("expects") or frame.get("refused") or _developed)' in src
    assert '_missing = [] if (_developed or frame.get("expects") or _engine_check) else uncovered(' in src


COOKIE_DRAFT = ("## Inputs needed\n\n- `<PANEL_COOKIE>` — the panel user's session cookie value\n\n## Run this\n\n```bash\n"
                "pct exec 111 -- sh -c 'curl -s -b \"connect.sid=<PANEL_COOKIE>\" http://127.0.0.1:3001/api/health'\n```\n\n"
                "## Verify\n\n- `pct exec 111 -- sh -c 'curl -s -o /dev/null -w \"%{http_code}\" http://127.0.0.1:3001/api/health'` shows `200`\n")


def test_the_operator_reads_the_block_that_will_run_first_when_a_capability_replaced_the_draft():
    from types import SimpleNamespace
    policy = {"allow": ["ANY"], "held": ["PANEL_PASSWORD"], "held_logins": LOGINS}
    frame = sr.frame_run(LIVE_T2, COOKIE_DRAFT, SimpleNamespace(name="pve-runner"), policy, inventory=INV)
    assert frame["commands"] and frame["commands"][0].startswith("pct exec 111 -- sh -c 'cpw=$(cat)") and frame["suggested"] == "run"
    rb = frame["runbook"]
    assert rb.startswith("## Run this\n```bash\npct exec 111 -- sh -c 'cpw=$(cat)"), rb[:120]
    assert "## The engine drafted this" in rb and "§17.1574c" in rb
    assert rb.index("## The model's draft, replaced by the block above") < rb.index("connect.sid=<PANEL_COOKIE>")
    assert rb.endswith(sr._runbook_for_display(COOKIE_DRAFT, frame["commands"]))          # the rehearsal path still finds the draft
    # a frame no capability touched reads as before
    plain = sr.frame_run({"node_key": "X", "title": "t"}, "## Run this\n\n```bash\npct exec 111 -- ss -tlnp\n```\n\n## Verify\n\n- `pct exec 111 -- ss -tlnp` shows `3001`\n",
                         SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]})
    assert plain["runbook"].startswith("## Run this\n\n```bash\npct exec 111 -- ss -tlnp") and "replaced by the block above" not in plain["runbook"]
