"""§17.1421 — "Pi-hole" is Pi-hole: the step's service is measured, and the model is told which API answers.

Live, 2026-10-08, ADD125 ("read-only Pi-hole API"): `names_in` knew `pihole` and `pihole-FTL` but not the
spelling the step used, so Pi-hole was never measured for it. Round 1 wrote `192.168.1.130` (guest 130's
ID as an address); refused, round 2 wrote `http://<PIHOLE_IP>` and Pi-hole v5's `/admin/api.php?summary`.
Measured: CT 130 is 192.168.1.30, Pi-hole v6.4.3, `/api/stats/summary` 200 without a key, the v5 path 400.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

from app.modules import develop as dv
from app.modules import service_truth as st

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD125 = json.loads((FIX / "add125_node_2026_10_08.json").read_text())
SS_130 = ('LISTEN 0      200          0.0.0.0:80        0.0.0.0:*    users:(("pihole-FTL",pid=200,fd=35))\n'
          'LISTEN 0      32           0.0.0.0:53        0.0.0.0:*    users:(("pihole-FTL",pid=200,fd=22))\n')
PIHOLE = st.ServiceTruth(guest="130", name="pihole-FTL", address="192.168.1.30")


def test_the_step_names_pihole():
    assert st.names_in(ADD125) == ["pihole"]
    assert st.names_in({"title": "PiHole and pi-hole", "description": "pihole-FTL"}) == ["pihole"]


def test_the_named_service_is_found_in_its_guest(monkeypatch):
    async def probe(_spec, cmd):
        return (True, SS_130) if "pct exec 130" in cmd else (True, "")

    monkeypatch.setattr(st, "_probe", probe)
    got = asyncio.run(st.guests_of_the_named_services(object(), st.names_in(ADD125), {"111": "running", "130": "running"}))
    assert got.get("pihole") == "130"


def test_status_probes_are_read_only_and_inside_the_guest():
    from app.modules.assist_state_check import read_only_command
    reads = dv.status_reads([PIHOLE])
    assert [p for _, p, _ in reads] == ["/api/stats/summary", "/admin/api.php?summary"]
    for _, _, cmd in reads:
        assert cmd.startswith("pct exec 130 -- curl -s -m 10 -o /dev/null -w %{http_code} ") and read_only_command(cmd)


def test_the_prompt_says_which_api_answers(monkeypatch):
    async def probe(_spec, cmd):
        return True, ("200" if "/api/stats/summary" in cmd else "400")

    monkeypatch.setattr(st, "_probe", probe)
    text = asyncio.run(dv.read_apis(None, [PIHOLE]))
    assert "pihole (192.168.1.30) GET /api/stats/summary without a key: HTTP 200" in text
    assert "GET /admin/api.php?summary without a key: HTTP 400" in text


def test_a_service_with_no_probe_adds_nothing():
    assert dv.status_reads([st.ServiceTruth(guest="111", name="control-panel")]) == []


def test_the_classifiers_vocabulary_is_unchanged():
    """The full suite caught it: widening `_NAME_RE` turned "Point the router's DNS at Pi-hole" (phone-app
    work, §17.1253) hands-on. The alias is for measuring only."""
    from app.modules.step_classify import step_is_hands_on
    assert not st._NAME_RE.search("Pi-hole")
    on, _ = step_is_hands_on({"title": "Point the router's DNS at Pi-hole", "tool": "LLM",
                              "description": "Never write `pct destroy 130` in this step."})
    assert on is False
