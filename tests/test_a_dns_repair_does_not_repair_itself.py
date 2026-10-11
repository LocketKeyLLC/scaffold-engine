"""§17.1491 — a DNS repair is not inserted for a step that fixes DNS itself, for the repair, or twice.

Live (2026-10-11): CT 105 cannot resolve names (installing resolvconf emptied /etc/resolv.conf). The no-DNS rule
inserted "Give container 105 a working nameserver" for ADD160 — whose own text sets the resolvconf base nameserver
(fixture) — then, drafting that repair, measured no DNS again and inserted another for it, and again:
ADD162 → ADD163 → … → ADD168, each waiting on the next, ADD160 behind them all, until the run was stopped.
"""
import json
import pathlib

from app.modules import machine_truth as mt

ADD160 = json.loads(pathlib.Path("tests/fixtures/add160_dns_fix_node_2026_10_11.json").read_text())


def _truth():
    return mt.GuestTruth(gid="105", kind="ct", status="running", resolves=False, dns_hint="192.168.1.1")


def _no_dns(node, plan):
    return [r for r in mt.contradictions(node, _truth(), {"network"}, plan) if r["kind"] == "no_dns"]


def test_a_step_that_sets_the_resolver_itself_is_not_given_a_repair():
    assert _no_dns(ADD160, [ADD160]) == []


def test_the_repair_is_not_given_a_repair():
    repair = {"node_key": "ADD162", "title": "Give container 105 a working nameserver", "status": "pending",
              "description": "Set it on the host: `pct set 105 --nameserver \"192.168.1.1\"`."}
    assert _no_dns(repair, [repair]) == []


def test_a_second_step_waits_for_the_repair_already_in_the_plan():
    repair = {"node_key": "ADD162", "title": "Give container 105 a working nameserver", "status": "pending"}
    other = {"node_key": "ADD170", "title": "Install curl in CT 105", "description": "apt-get install curl", "status": "pending"}
    rows = _no_dns(other, [repair, other])
    assert len(rows) == 1 and "insert" not in rows[0] and rows[0]["waits_for"] == "ADD162" and rows[0]["waits"] == "ADD170"


def test_the_first_step_still_gets_one():
    other = {"node_key": "ADD170", "title": "Install curl in CT 105", "description": "apt-get install curl", "status": "pending"}
    rows = _no_dns(other, [other])
    assert len(rows) == 1 and rows[0]["insert"]["title"] == "Give container 105 a working nameserver"
