"""§17.1351 — a step touches the guests of the services it names.

Live, 2026-10-04. ADD132 ("Make Radarr and Sonarr actually drive qBittorrent, and
prove the connection") named only one guest id in its text, inside a check
(`pct exec 103 …`). So the step read as one guest's work, a container template
took it, and the draft tried to run qBittorrent's own commands inside RADARR's
container:

    systemctl stop qbittorrent.service

The unit gate (§17.1327) caught it — *"`qbittorrent` is not a unit anything the
engine holds names … Units the guest has: … control-panel.service …"* — and the
no-check gate (§17.1345) caught the second fault in the same frame. Both worked.
But the draft should never have been aimed at 103 at all, and the engine already
knew better: §17.1346 had MEASURED that radarr lives on 103, sonarr on 104 and
qbittorrent-nox on 105. The operator: *"the engine already knows the information
anyways."*

So the guests a step touches are the ids in its own text PLUS the guests of the
services it names, and a step that touches more than one can be drafted from no
single-guest template.
"""
from __future__ import annotations

from app.modules import runbook_templates as rt
from app.modules import service_truth as st

RADARR = st.ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",))
SONARR = st.ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", ports=("8989",))
QB = st.ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox", ports=("8080",))
JELLYFIN = st.ServiceTruth(guest="101", unit="jellyfin.service", name="jellyfin", ports=("8096",))
SERVICES = [RADARR, SONARR, QB, JELLYFIN]

#: ADD132 as it stood: three services in the title, one guest id in a check
NODE = {"node_key": "ADD132",
        "title": "Make Radarr and Sonarr actually drive qBittorrent, and prove the connection",
        "description": "Done when `pct exec 103 -- sh -c 'curl -s -X POST … /api/v3/downloadclient/test'` "
                       "returns success for Radarr, and the same call on 104 against 8989 for Sonarr."}


def test_the_text_alone_sees_one_guest():
    """The pre-image: why a container template took it."""
    assert rt.guests_the_host_is_told_to_touch(NODE) == {"103"}
    assert rt.spans_guests(NODE) is False


def test_the_measured_services_name_all_three():
    assert rt.guests_the_host_is_told_to_touch(NODE, SERVICES) == {"103", "104", "105"}
    assert rt.spans_guests_with(NODE, SERVICES) is True


def test_no_guest_template_claims_a_step_that_spans():
    assert rt.select_template(NODE, None, SERVICES) is None


def test_the_apps_own_word_matches_its_unit_name():
    """The step says "qBittorrent"; the unit is `qbittorrent-nox`."""
    node = {"node_key": "X", "title": "Point qBittorrent's downloads at /media/downloads",
            "description": "In its own container."}
    assert rt.guests_the_host_is_told_to_touch(node, SERVICES) == {"105"}


def test_one_service_is_not_a_span_and_keeps_its_template():
    node = {"node_key": "Y", "title": "Add Jellyfin libraries for /media/movies and /media/tv",
            "description": "Inside container 101, write the library directories."}
    assert rt.spans_guests_with(node, SERVICES) is False
    assert rt.guests_the_host_is_told_to_touch(node, SERVICES) == {"101"}


def test_without_services_nothing_changes():
    """Blindness invents nothing: with no measurement, the old reading stands."""
    assert rt.guests_the_host_is_told_to_touch(NODE, None) == {"103"}
    assert rt.spans_guests_with(NODE, None) is False
    assert rt.spans_guests_with(NODE, []) is False


def test_a_service_the_step_never_mentions_is_not_counted():
    node = {"node_key": "Z", "title": "Set Radarr's root folder to /media/movies",
            "description": "Radarr only."}
    assert rt.guests_the_host_is_told_to_touch(node, SERVICES) == {"103"}


def test_the_ids_in_the_text_still_count_on_their_own():
    node = {"node_key": "W", "title": "Make the machines start on boot",
            "description": "qm set 106 --onboot 1, pct set 111 --onboot 1, pct set 120 --onboot 1"}
    assert rt.guests_the_host_is_told_to_touch(node, SERVICES) == {"106", "111", "120"}


def test_the_lane_carries_the_services_to_the_selector():
    """A measurement the selector never receives decides nothing."""
    import inspect
    import pathlib
    from app.modules import supervised_runs as sr
    assert "rt.select_template(node, truth, services)" in inspect.getsource(sr.draft_runbook)
    ea = pathlib.Path(__import__("app.modules.execution_agent", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    i = ea.index("async def _pause_for_decision(")
    body = ea[i:ea.index("\nasync def ", i + 10)]
    assert body.count("services=_services") >= 9, body.count("services=_services")
