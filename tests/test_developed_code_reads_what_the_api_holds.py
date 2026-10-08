"""§17.1416 — developed code is given what the APIs it calls hold NOW, as it is given the files.

Live, 2026-10-07, ADD123: the model saw the control panel's own files but never Radarr's state, and
filled `rootFolderPath: movie.rootFolderPath || '/movies'` and `qualityProfileId: … || 1`. Radarr's
only root folder is `/media/movies` (Sonarr's `/media/tv`): every new request would have been
refused. The engine now reads those endpoints inside each guest, with the key and port taken from
the app's own config.xml there, and puts the answers in the prompt.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import subprocess

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import service_truth as st

RADARR = st.ServiceTruth(guest="103", name="radarr", address="192.168.1.22")
SONARR = st.ServiceTruth(guest="104", name="sonarr", address="192.168.1.23")
PANEL = st.ServiceTruth(guest="111", name="control-panel")

# the *arr apps' own pretty-printing: objects at 2, their fields at 4, nested qualities deeper
ROOTS = json.dumps([{"path": "/media/movies", "accessible": True, "freeSpace": 1, "unmappedFolders": [], "id": 1}], indent=2)
PROFILES = json.dumps([
    {"name": "Any", "upgradeAllowed": False, "cutoff": 20,
     "items": [{"quality": {"id": 0, "name": "Unknown"}, "items": [], "allowed": False}], "id": 1},
    {"name": "HD-1080p", "upgradeAllowed": False, "cutoff": 7,
     "items": [{"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True}], "id": 4},
], indent=2)


def _filtered(body: str) -> str:
    """The guest-side filter, run for real by grep over the real shape."""
    cmd = dv.api_reads([RADARR])[0][2]
    pattern = cmd.split('grep -E "', 1)[1].split('"', 1)[0]
    return subprocess.run(["grep", "-E", pattern], input=body, text=True, capture_output=True).stdout


def test_reads_are_composed_inside_each_guest_for_the_arr_services_only():
    reads = dv.api_reads([RADARR, SONARR, PANEL])
    assert [(n, e) for n, e, _ in reads] == [("radarr", "rootfolder"), ("radarr", "qualityprofile"),
                                             ("sonarr", "rootfolder"), ("sonarr", "qualityprofile")]
    for n, _, cmd in reads:
        assert cmd.startswith(f"pct exec {'103' if n == 'radarr' else '104'} -- sh -c '")
        assert "<ApiKey>" in cmd and "<Port>" in cmd
        assert "'\\''" not in cmd, "the read channel refuses a quote-escape (measured)"


def test_the_reads_pass_the_read_only_classifier():
    from app.modules.assist_state_check import read_only_command
    for _, _, cmd in dv.api_reads([RADARR, SONARR]):
        assert read_only_command(cmd), cmd


def test_bash_level_the_filter_keeps_each_objects_own_fields_only():
    assert dv._summarise(_filtered(ROOTS)) == "id='1', path='/media/movies'"
    assert dv._summarise(_filtered(PROFILES)) == "id='1', name='Any'; id='4', name='HD-1080p'"


def test_the_prompt_section_names_the_real_values():
    answers = {"rootfolder": _filtered(ROOTS), "qualityprofile": _filtered(PROFILES)}

    async def probe(_spec, cmd):
        return True, answers["rootfolder" if cmd.split("/api/v3/")[1].startswith("rootfolder") else "qualityprofile"]

    orig = st._probe
    st._probe = probe
    try:
        text = asyncio.run(dv.read_apis(None, [RADARR]))
    finally:
        st._probe = orig
    assert "radarr (192.168.1.22) /api/v3/rootfolder: id='1', path='/media/movies'" in text
    assert "never invent a default" in text


def test_an_unreadable_api_says_so_and_a_step_with_none_adds_nothing():
    async def probe(_spec, _cmd):
        return False, ""

    orig = st._probe
    st._probe = probe
    try:
        assert "(could not be read just now)" in asyncio.run(dv.read_apis(None, [RADARR]))
        assert asyncio.run(dv.read_apis(None, [PANEL])) == ""
    finally:
        st._probe = orig


def test_the_develop_loop_puts_them_in_the_facts():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "await develop.read_apis(spec, _services, status)" in src
