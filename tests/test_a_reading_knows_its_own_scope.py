r"""§17.1363 — a reading knows its own scope.

Eleven fixes in one day (§17.1352–1362b), and the operator's reading of them was
right: *"it appears as though your fixes are not fixing the issue itself but each
individual fix. Look for the deeper issue."* Six of the eleven are the same bug:

    §17.1352  1 of 2 secret stores read        -> "nothing sets $MASS_PASSWORD"
    §17.1356  1 of 3 guests measured           -> "the services on these machines"
    §17.1357  1 guest's unit list              -> "`qbittorrent-nox` is not a unit …"
    §17.1359  the layer threw, read nothing    -> "nothing contradicts this block"
    §17.1360  the config was never read        -> "write the setting here"
    §17.1361  2 configs found, 1 named by `ls` -> "config = qBittorrent-data.conf"

Each was fixed by widening that one reading. What made all six possible is that a
narrow reading was allowed to present itself as a complete one. Measured:

* `ServiceTruth.reads` / `GuestTruth.reads` are written at ten sites and consumed
  NOWHERE — a grep for `.reads` outside the writers finds one unrelated UI field;
* the facts block asserts *"read just now; use these values, do not infer
  others"* and never says what was NOT read;
* the frame had 24 fields and exactly one shaped like a gap (`secrets_missing`);
* `_probe` returns three outcomes and every caller collapsed them to two.

So: one `Reading` per pause. Everything the engine read is noted, everything it
could not read is a gap, and the gaps travel with the facts, into the frame, and
onto the operator's page.

**The distinction this draws is between "I could not look" and "I looked and it
is not there."** The first version of this file's own implementation got that
wrong — it filed nine negative FINDINGS as gaps ("sonarr in guest 103 — systemctl
has no such unit there") — which is why the tests below pin it from both sides.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import service_truth as st
from app.modules.measured import UNREAD_RULE, Reading

SS = json.loads((pathlib.Path(__file__).parent / "fixtures"
                 / "ss_tlnp_every_guest_2026_10_04.json").read_text())
SHOW = json.loads((pathlib.Path(__file__).parent / "fixtures"
                   / "systemctl_show_names_2026_10_04.json").read_text())
CTS = {"101": "running", "102": "running", "103": "running", "104": "running",
       "105": "running", "111": "running", "120": "stopped", "130": "running"}


class _Spec:
    endpoint = "http://192.168.1.156:8790/mcp/"


# -------------------------------------------------------------- the record


def test_an_empty_reading_says_nothing():
    r = Reading()
    assert r.says() == "" and r.gaps() == [] and not r


def test_what_was_read_and_what_was_not_are_separate():
    r = Reading()
    r.note("what listens in guest(s) 103, 104, 105", "ss -tlnp")
    r.gap("guest 120", "stopped — nothing in it could be read")
    said = r.says()
    assert "- read: what listens in guest(s) 103, 104, 105 — ss -tlnp" in said
    assert "- NOT READ: guest 120 — stopped" in said
    assert r.gaps() == [{"what": "guest 120", "why": "stopped — nothing in it could be read"}]


def test_a_gap_carries_the_instruction_that_makes_it_behaviour():
    """A gap the drafter is not told what to do about is decoration."""
    r = Reading()
    r.note("x", "y")
    assert UNREAD_RULE not in r.says()
    r.gap("guest 120", "stopped")
    assert UNREAD_RULE in r.says()
    assert "do not write a value for it" in UNREAD_RULE


def test_the_provenance_that_was_written_and_never_read_is_absorbed():
    """`ServiceTruth.reads` — ten write sites, zero readers before this."""
    r = Reading()
    r.absorb({"systemctl show": "qbittorrent-nox.service",
              "config settings": "qBittorrent.conf=8, qBittorrent-data.conf=2"},
             prefix="qbittorrent-nox in guest 105 · ")
    said = r.says()
    assert "qbittorrent-nox in guest 105 · systemctl show — qbittorrent-nox.service" in said
    assert "qBittorrent.conf=8" in said


def test_nothing_is_recorded_twice():
    r = Reading()
    for _ in range(3):
        r.note("a", "b")
        r.gap("c", "d")
    assert len(r.saw) == 1 and len(r.gaps()) == 1


# ------------------------------- could not look, versus looked and not there


def _sweep_reads(monkeypatch):
    async def fake_probe(spec, command):
        gid = command.split()[2]
        if gid == "106":
            return None, ""              # the runner could not be asked
        return (bool(SS.get(gid)), SS.get(gid, ""))
    monkeypatch.setattr(st, "_probe", fake_probe)


@pytest.mark.asyncio
async def test_a_guest_that_is_not_running_is_a_gap(monkeypatch):
    _sweep_reads(monkeypatch)
    r = Reading()
    await st.guests_of_the_named_services(_Spec(), ["radarr"], CTS, reading=r)
    assert {"what": "guest 120", "why": "stopped — nothing in it could be read"} in r.gaps()


@pytest.mark.asyncio
async def test_a_guest_the_runner_could_not_be_asked_about_is_a_gap(monkeypatch):
    """The name must be one nothing has, or the sweep stops before reaching it."""
    _sweep_reads(monkeypatch)
    r = Reading()
    await st.guests_of_the_named_services(_Spec(), ["plex"], {**CTS, "106": "running"}, reading=r)
    assert any(g["what"] == "guest 106" and "could not read" in g["why"] for g in r.gaps()), r.gaps()


@pytest.mark.asyncio
async def test_a_guest_the_sweep_stopped_short_of_is_not_a_gap(monkeypatch):
    """An early exit is not ignorance: every service the step names was placed.
    The first implementation filed five of these as gaps."""
    _sweep_reads(monkeypatch)
    r = Reading()
    await st.guests_of_the_named_services(_Spec(), ["radarr"], CTS, reading=r)
    assert [g["what"] for g in r.gaps()] == ["guest 120"], r.gaps()
    assert "what listens in guest(s) 101, 102, 103" in r.says()


@pytest.mark.asyncio
async def test_a_service_looked_for_everywhere_and_absent_is_a_READING(monkeypatch):
    """The correction. `plex` is not a gap: the engine swept every guest it could
    read and nothing listens as it. Filing that as a gap is the same error as
    filing a gap as a fact."""
    _sweep_reads(monkeypatch)
    r = Reading()
    await st.guests_of_the_named_services(_Spec(), ["plex"], CTS, reading=r)
    assert all(g["what"] != "plex" for g in r.gaps()), r.gaps()
    assert "read: plex is not running" in r.says()


@pytest.mark.asyncio
async def test_the_sweep_names_every_guest_it_read(monkeypatch):
    _sweep_reads(monkeypatch)
    r = Reading()
    await st.guests_of_the_named_services(_Spec(), ["qbittorrent"], CTS, reading=r)
    said = r.says()
    assert "what listens in guest(s) 101, 102, 103, 104, 105" in said


@pytest.mark.asyncio
async def test_an_unreadable_guest_is_a_gap_not_an_empty_service_list(monkeypatch):
    """`read_services` returning `[]` used to be indistinguishable from "this
    guest runs nothing"."""
    async def fake_probe(spec, command):
        return None, ""
    monkeypatch.setattr(st, "_probe", fake_probe)
    r = Reading()
    assert await st.read_services(_Spec(), "105", mentioned=["qbittorrent"], reading=r) == []
    assert any(g["what"] == "the services in guest 105" for g in r.gaps()), r.gaps()


@pytest.mark.asyncio
async def test_a_service_the_guest_does_not_have_is_not_a_gap(monkeypatch):
    """Nine of these were filed as gaps by the first implementation. `systemctl
    show` answering `LoadState=not-found` is a reading, and the sweep already
    says which guest each service lives in."""
    async def fake_probe(spec, command):
        if "ss -tlnp" in command:
            return True, SS["105"]
        if "systemctl show" in command:
            name = command.rstrip("'").split()[-1]
            return True, SHOW.get(f"105:{name}", "LoadState=not-found\nActiveState=inactive\n")
        return False, ""
    monkeypatch.setattr(st, "_probe", fake_probe)
    r = Reading()
    out = await st.read_services(_Spec(), "105", mentioned=["radarr", "qbittorrent"], reading=r)
    assert [s.name for s in out] == ["qbittorrent-nox"]
    assert all("radarr" not in g["what"] for g in r.gaps()), r.gaps()


@pytest.mark.asyncio
async def test_the_config_scores_reach_the_facts(monkeypatch):
    """§17.1361's measurement was recorded in `reads` and read by nobody."""
    async def fake_probe(spec, command):
        if "ss -tlnp" in command:
            return True, SS["105"]
        if "systemctl show" in command:
            name = command.rstrip("'").split()[-1]
            return True, SHOW.get(f"105:{name}", "LoadState=not-found\n")
        if command.startswith("pct exec 105 -- sh -c 'id "):
            return True, "uid=999(qbittorrent-nox) gid=996(qbittorrent-nox)"
        if "ls -1d" in command:
            return True, ("/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf\n"
                          "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf\n")
        if "grep -c -E" in command:
            return True, ("/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf:8\n"
                          "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf:2\n")
        if "stat -c" in command:
            return True, ("qbittorrent-nox:qbittorrent-nox 664 "
                          "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf")
        return False, ""
    monkeypatch.setattr(st, "_probe", fake_probe)
    r = Reading()
    out = await st.read_services(_Spec(), "105", mentioned=["qbittorrent"], reading=r)
    assert out and out[0].config.endswith("qBittorrent.conf")
    assert "qBittorrent.conf=8" in r.says() and "qBittorrent-data.conf=2" in r.says()


# ------------------------------------------------------------- the surfaces


def test_the_facts_carry_the_scope():
    svc = st.ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox",
                          state="active", ports=("8080",))
    r = Reading()
    r.gap("guest 120", "stopped")
    out = st.table([svc], r)
    assert "SERVICES MEASURED ON THESE MACHINES" in out
    assert "WHAT WAS READ, AND WHAT WAS NOT" in out
    assert "NOT READ: guest 120" in out
    # and without a reading the block is exactly what it always was
    assert "WHAT WAS READ" not in st.table([svc])


def test_the_frame_carries_the_gaps():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert '"not_measured": (reading.gaps() if reading is not None else [])' in src


def test_the_pause_builds_one_reading_and_hands_it_everywhere():
    """Verify the lane: a record nothing is given to records nothing."""
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_reading = Reading()" in src
    assert src.count("reading=_reading") >= 10, src.count("reading=_reading")
    assert "guests_of_the_named_services(spec, _named, _cts, reading=_reading, vms=_vms" in src   # §17.1402
    assert "_st2.table(_services, _reading)" in src
    # the inventory, the step's own guest, and the precondition layer
    assert "the host's own inventory" in src
    assert "what the machine contradicts" in src


def test_the_operator_page_renders_the_gaps():
    """§17.1096 — a behaviour the engine performs is not done until the UI says
    so. The gaps sit beside `refused`, styled as a scope note, not a refusal."""
    js = (pathlib.Path(__file__).resolve().parent.parent
          / "app/ui/static/views/theater.js").read_text()
    assert "d.not_measured" in js
    assert "could not read for this step" in js
    css = (pathlib.Path(__file__).resolve().parent.parent
           / "app/ui/static/app.css").read_text()
    assert ".decision-unread" in css


def test_the_preconditions_record_what_they_could_not_read():
    import inspect

    from app.modules import runbook_preconditions as rp
    for fn in (rp.a_bare_append_lands_in_the_last_section,
               rp.an_in_place_edit_the_file_cannot_match,
               rp.writes_where_the_check_does_not_read):
        src = inspect.getsource(fn)
        assert "reading" in inspect.signature(fn).parameters, fn.__name__
        assert "reading.gap(" in src, fn.__name__
