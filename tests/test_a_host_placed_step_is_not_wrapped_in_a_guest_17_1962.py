"""§17.1962 — a step whose title places it on the Proxmox host is never wrapped in an in-guest template."""
from types import SimpleNamespace
from app.modules import runbook_templates as rt

_TRUTH = SimpleNamespace(gid="111", kind="ct")
_BODY = ("On the Proxmox host shell (root@pve), call the stop control through the front door and confirm with "
         "`pct exec 111 -- curl -sS -m 5 http://192.168.1.44:8212`. On container 111.")


def test_the_live_split_step_gets_no_guest_template():
    node = {"title": "Proxmox host: call the stop control through the Caddy front door", "prompt_template": _BODY}
    assert rt.select_template(node, _TRUTH) is None


def test_the_same_body_under_a_guest_title_still_gets_the_container_template():
    node = {"title": "CT 111: call the stop control through the front door", "prompt_template": _BODY}
    assert rt.select_template(node, _TRUTH) is rt.RUN_IN_CONTAINER


def test_host_work_under_a_host_title_is_not_refused_for_missing_the_guest():
    from app.modules.supervised_runs import commands_never_reach_the_guest
    body = "Read the request body from /opt/control-panel-ui/src/App.jsx in CT 111, then send it once."
    cmds = ["curl -s -X POST --fail http://192.168.1.25:3001/api/media-request -d @/tmp/body.json"]
    host = {"title": "Proxmox host: use the control panel media control once", "prompt_template": body}
    guest = {"title": "Use the control panel media control once on CT 111", "prompt_template": body}
    assert commands_never_reach_the_guest(cmds, host) == []
    assert commands_never_reach_the_guest(cmds, guest)
