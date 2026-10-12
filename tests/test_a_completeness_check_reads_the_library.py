r"""§17.1504 — a "check <show> completeness" step reads the *arr library and reports gaps.

Live, job c75d77fa, the five show steps ("Query Sonarr for <show>. Compare existing
episodes against all available seasons/episodes. List any missing episodes."). The model
drafted anything from a catalog `series/lookup` (which proves nothing about the library)
to an add-the-series POST needing an external id to a `true` check — and a weak self-check
let a hollow one pass (Big Bang Theory recorded done while HIMYM, also "done", was in fact
missing 9 episodes). The engine can do this exactly: GET `/series` with per-season
statistics, inside the guest (so the >20 KB JSON never hits the runner's 20 000-char cap),
and print `have X of Y` per season with a COMPLETE / MISSING verdict.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import rewrite_completeness_check, _COMPLETENESS_PY
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest):
        self.name, self.guest = name, guest


SERVICES = [_Svc("sonarr", "104"), _Svc("radarr", "103")]
INV = {"names": {"104": "sonarr", "103": "radarr"}}
NODE = {"title": "Check Scrubs completeness",
        "prompt_template": "Query Sonarr for Scrubs. Compare existing episodes against all "
                           "available seasons/episodes. List any missing episodes."}


def test_the_step_is_rewritten_to_read_the_library_in_the_guest():
    cmds, verify, notes = rewrite_completeness_check(NODE, ["<model draft>"], ["true"], SERVICES, INV)
    assert notes and "§17.1504" in notes[0]["why"]
    run = cmds[0]
    assert run.startswith("pct exec 104 -- sh -c '")
    assert "/api/v3/series" in run and "series/lookup" not in run, "the library, not the catalog"
    assert "<SONARR_API_KEY>" in run and "Scrubs" in run
    assert "-X POST" not in run and "/test" not in run, "a read, not an add or a validating POST"


def test_the_check_is_read_only_safe_and_not_a_no_op():
    """§17.1505 — the verify runs through the read-only channel, which refuses `python3`
    and command substitution (§17.1390). It is a plain authenticated library read whose
    HTTP code witnesses the read works — never a `true`, no interpreter, no `$( … )`."""
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_completeness_check(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and "python3" not in v and "$(" not in v
    assert "/api/v3/series" in v and "$SONARR_API_KEY" in v and "http_code" in v.lower()
    assert read_only_command(v), "the read-only verify channel must accept the check"


def test_the_key_fills_from_the_guest_and_the_command_parses():
    cmds, verify, _ = rewrite_completeness_check(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "$SONARR_API_KEY" not in c[0] and "/var/lib/sonarr/config.xml" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_parser_reports_completeness_on_realistic_series_json():
    """The parser logic, run on a minimal /series shape (the live proof fed it the real
    6-series library: Big Bang Theory COMPLETE, Scrubs MISSING 15, HIMYM MISSING 9)."""
    import json
    series = [{"title": "Scrubs", "seasons": [
        {"seasonNumber": 0, "statistics": {"episodeCount": 5, "episodeFileCount": 0}},   # specials ignored
        {"seasonNumber": 1, "statistics": {"episodeCount": 24, "episodeFileCount": 9}},
        {"seasonNumber": 2, "statistics": {"episodeCount": 22, "episodeFileCount": 22}},
    ]}]
    out = subprocess.run(["python3", "-c", _COMPLETENESS_PY, "Scrubs"],
                         input=json.dumps(series), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "SERIES: Scrubs" in out.stdout
    assert "Season 1: have 9 of 24 MISSING 15" in out.stdout
    assert "Season 2: have 22 of 22" in out.stdout and "Season 0" not in out.stdout
    assert "RESULT: MISSING 15 episode(s)" in out.stdout


def test_a_show_not_in_the_library_is_reported_not_failed():
    import json
    out = subprocess.run(["python3", "-c", _COMPLETENESS_PY, "Firefly"],
                         input=json.dumps([{"title": "Scrubs", "seasons": []}]), capture_output=True, text=True)
    assert out.returncode == 0 and "RESULT: NOT IN LIBRARY (Firefly)" in out.stdout


def test_a_non_completeness_step_is_left_to_the_model():
    node = {"title": "Install Radarr", "prompt_template": "apt-get install radarr"}
    out, _, notes = rewrite_completeness_check(node, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes


def test_it_does_not_fire_without_a_known_arr_guest():
    out, _, notes = rewrite_completeness_check(NODE, ["x"], ["y"], [], {"names": {}})
    assert out == ["x"] and not notes


def test_frame_run_rewrites_completeness_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_completeness_check(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_completeness_check(node, cmds") < src.index("read_on_the_machine(cmds")
