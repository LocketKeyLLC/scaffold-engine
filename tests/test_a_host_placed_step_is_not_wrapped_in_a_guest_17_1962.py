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
