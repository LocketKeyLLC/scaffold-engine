r"""§17.1508 — a "queue/search the missing episodes" step triggers Sonarr's per-series
search, holding a whole-series backfill above a threshold.

Live, job c75d77fa: the LLM "Consolidate missing episodes list" step returned a refusal,
so the queue behind it never ran. The completeness checks already know the gaps, so the
engine skips the flaky consolidation and makes Sonarr search each show's missing monitored
episodes — except a show whose gap exceeds the threshold (Power Rangers' ~972 is a
whole-series backfill the operator asks for by name; Scrubs 15, HIMYM 9, Robot Chicken 1,
Scream Queens 23 are filled). Proven on the live library before shipping.
"""
from __future__ import annotations

import json
import subprocess

from app.modules.supervised_runs import (
    rewrite_queue_missing,
    _QUEUE_MISSING_PY,
    _QUEUE_HOLD_THRESHOLD,
)
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("sonarr", "104", ("8989",))]
INV = {"names": {"104": "sonarr"}}
NODE = {"title": "Queue missing episodes in Sonarr",
        "prompt_template": "For each show with missing episodes/seasons, trigger Sonarr to search and download them."}

LIB = [
    {"id": 1, "title": "The Big Bang Theory", "statistics": {"episodeCount": 279, "episodeFileCount": 279}},
    {"id": 2, "title": "How I Met Your Mother", "statistics": {"episodeCount": 208, "episodeFileCount": 199}},
    {"id": 6, "title": "Power Rangers", "statistics": {"episodeCount": 972, "episodeFileCount": 0}},
]


def test_the_step_is_rewritten_to_a_per_series_search_in_the_guest():
    cmds, verify, notes = rewrite_queue_missing(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1508" in notes[0]["why"]
    run = cmds[0]
    assert run.startswith("pct exec 104 -- sh -c '")
    assert "SeriesSearch" in run and "<SONARR_API_KEY>" in run
    assert str(_QUEUE_HOLD_THRESHOLD) in run


def test_the_verify_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_queue_missing(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and "python3" not in v and "$(" not in v
    assert read_only_command(v)


def test_the_command_fills_the_key_and_parses():
    cmds, verify, _ = rewrite_queue_missing(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "/var/lib/sonarr/config.xml" in c[0] and "$SONARR_API_KEY" not in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_selection_searches_small_gaps_and_holds_a_large_one(monkeypatch):
    """The script fetches `/series` itself and POSTs SeriesSearch per under-threshold show.
    Exec it in-process with a stubbed urllib: a show over the threshold is HELD (no POST),
    a complete show is skipped, the rest get a search. Live proof on the real library:
    HIMYM 9 / Robot Chicken 1 / Scrubs 15 / Scream Queens 23 searched, Power Rangers 972 held."""
    import urllib.request as ur
    import contextlib
    import io
    posts = []

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps(LIB).encode())
        posts.append(json.loads(req.data))
        return io.BytesIO(b'{"id": 1}')

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["-c", "thekey", str(_QUEUE_HOLD_THRESHOLD)])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(_QUEUE_MISSING_PY, {"__name__": "__main__"})
    out = buf.getvalue()
    assert "COMPLETE: The Big Bang Theory" in out
    assert "SEARCHING: How I Met Your Mother (9 episodes)" in out
    assert "HELD: Power Rangers (972 missing" in out
    # only the under-threshold, incomplete show (HIMYM, id 2) was actually searched
    assert [p.get("seriesId") for p in posts] == [2]
    assert all(p.get("name") == "SeriesSearch" for p in posts)


def test_the_loop_survives_one_dead_party():
    """§17.1274 — one failing search must not abort the rest; the per-series call is guarded."""
    assert "try:" in _QUEUE_MISSING_PY and "except Exception" in _QUEUE_MISSING_PY
    from app.modules import supervised_runs as sr
    cmds, _, _ = rewrite_queue_missing(NODE, ["x"], ["y"], SERVICES, INV)
    c, _, _ = mv.read_on_the_machine(cmds, [], INV)
    assert sr.loop_dies_on_one_dead_party(c) == [], "the guarded loop must pass §17.1274"


def test_a_completeness_check_is_not_a_queue():
    node = {"title": "Check Scrubs completeness", "prompt_template": "Query Sonarr for Scrubs, list missing episodes"}
    out, _, notes = rewrite_queue_missing(node, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes, "a check is not a queue; no SeriesSearch"


def test_frame_run_runs_the_queue_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_queue_missing(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_queue_missing(node, cmds") < src.index("read_on_the_machine(cmds")
