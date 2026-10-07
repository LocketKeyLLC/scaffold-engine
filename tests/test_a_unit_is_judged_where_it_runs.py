r"""§17.1357 — a unit measured on ANY of the step's guests is a unit the engine holds.

Live, 2026-10-04, the third ADD132 draft. With §17.1356's measurement in place
the draft was right: it reached guest 105 for the download client, read each
app's API key out of its own config, and checked the result in all three guests.
It was refused anyway:

    `qbittorrent-nox` is not a unit anything the engine holds names: the guest's
    own `systemctl list-unit-files` does not have it

`qbittorrent-nox.service` is real — measured the same evening on guest 105,
`LoadState=loaded`, `ActiveState=active`. The gate was handed `_truth.units`:
the unit list of the step's SUBJECT guest, 103. The engine refused a correct
draft for a fact it had just measured in a different container.

A unit used on the WRONG guest is §17.1346's refusal (`values_from_another_guest`),
not this one's — so widening this gate to every guest the step's services live in
loses nothing.
"""
from __future__ import annotations

import json
import pathlib

from app.modules import service_truth as st
from app.modules.supervised_runs import unsourced_service_name

#: the three services as §17.1356 measures them on the live machines
LIVE = [st.ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active"),
        st.ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active"),
        st.ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox",
                        state="active")]
#: what guest 103's own `systemctl list-unit-files` holds
UNITS_103 = ["radarr", "ssh", "cron", "postfix"]
NODE = {"title": "Make Radarr and Sonarr actually drive qBittorrent, and prove the connection",
        "description": ""}
#: the line the engine refused
LINE = "pct exec 105 -- systemctl is-active qbittorrent-nox.service"


def test_the_measured_units_are_collected_by_guest():
    assert st.measured_units_by_guest(LIVE) == {"103": ["radarr"], "104": ["sonarr"],
                                                "105": ["qbittorrent-nox"]}
    assert st.measured_units_by_guest([]) == {}
    assert st.measured_units_by_guest(None) == {}


def test_the_subject_guests_list_alone_refuses_the_correct_draft():
    """The defect, pinned: this is what the engine did."""
    out = unsourced_service_name([LINE], [], {}, NODE, "", UNITS_103)
    assert len(out) == 1, out
    assert "qbittorrent-nox" in out[0]["why"]


def test_the_guest_the_line_addresses_settles_it():
    by_guest = st.measured_units_by_guest(LIVE)
    assert unsourced_service_name([LINE], [], {}, NODE, "", UNITS_103, by_guest) == []


def test_an_invented_unit_is_still_refused():
    """The gate must keep biting: ADD66's `palworld-server` was the HOSTNAME."""
    by_guest = st.measured_units_by_guest(LIVE)
    out = unsourced_service_name(
        ["pct exec 105 -- systemctl is-active qbittorrent.service"], [], {}, NODE, "",
        UNITS_103, by_guest)
    assert len(out) == 1, out
    assert "qbittorrent-nox" in out[0]["why"], out[0]["why"]   # and it names the real one


def test_the_right_unit_in_the_wrong_guest_is_still_refused():
    """Measured, and the reason this is per-guest and not a pooled list:
    `values_from_another_guest` judges ports, data dirs and configs — NOT units —
    so nothing else catches this, and a union would have let it through."""
    cmds = ["pct exec 103 -- systemctl restart qbittorrent-nox"]
    assert st.values_from_another_guest(cmds, [], LIVE) == []      # not its business
    by_guest = st.measured_units_by_guest(LIVE)
    out = unsourced_service_name(cmds, [], {}, NODE, "", UNITS_103, by_guest)
    assert len(out) == 1, out


def test_a_line_naming_no_guest_keeps_the_subjects_list():
    """Nothing else changes: the host-side and in-guest-script lines are judged
    exactly as before."""
    by_guest = st.measured_units_by_guest(LIVE)
    assert unsourced_service_name(["systemctl restart radarr"], [], {}, NODE, "",
                                  UNITS_103, by_guest) == []
    assert unsourced_service_name(["systemctl restart qbittorrent-nox"], [], {}, NODE, "",
                                  UNITS_103, by_guest)


def test_an_unmeasured_guest_is_judged_as_before():
    by_guest = st.measured_units_by_guest(LIVE)
    assert unsourced_service_name(["pct exec 111 -- systemctl restart radarr"], [], {},
                                  NODE, "", UNITS_103, by_guest) == []


def test_the_pause_computes_the_union_once_and_passes_it_everywhere():
    """Verify the lane. Eight `frame_run` call sites read this; eight copies of
    the expression is how sibling call sites drift (§17.854/975/976/979/984)."""
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_st3.measured_units_by_guest(_services)" in src
    # §17.1411 — nine: the rehearsal repair loop's frame; §17.1412 — ten: the develop loop's frame
    assert src.count("units=_units, units_by_guest=_units_by_guest") == 10
    assert src.count("units=_units, units_by_guest=_units_by_guest") == src.count("supervised_runs.frame_run(")
    assert "units=(_truth.units if _truth is not None else None)" not in src
    # the union is built after the truth is read, so it can include it
    assert (src.index("_truth = await machine_truth.read_guest_truth")
            < src.index("_st3.measured_units_by_guest"))


def test_a_failure_to_widen_falls_back_to_the_subject_guest():
    """Fail-soft: the gate keeps working on the subject guest's list."""
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert 'logger.warning("measured_units_failed' in src
    # the subject guest's list is the starting point, so a failure to build the
    # map leaves the gate exactly as it was
    assert "_units = (_truth.units if _truth is not None else None)" in src
    assert "_units_by_guest: dict = {}" in src


# ------------------------------------ the `.service` suffix broke it both ways


def test_the_suffix_is_not_subtracted_from_the_name():
    r"""`unit` and `suffix` are separate groups in `_SYSTEMCTL_RE`, so `unit`
    never holds the `.service`. Subtracting its length chopped the name:

        qbittorrent-nox.service  ->  'qbittor'   (refused for a name nobody has)
        radarr.service           ->  ''          (skipped: the gate went blind)

    Measured on the regex itself, which is why ADD132's correct draft was refused
    and why `systemctl restart <short>.service` had never been judged at all.
    """
    import re

    from app.modules.supervised_runs import _SYSTEMCTL_RE
    for line, expect in (("pct exec 105 -- systemctl is-active qbittorrent-nox.service",
                          "qbittorrent-nox"),
                         ("systemctl restart radarr.service", "radarr"),
                         ("systemctl restart radarr", "radarr")):
        m = _SYSTEMCTL_RE.search(line)
        assert m, line
        unit = (m.group("unit") or "").strip().strip("'\"")
        assert unit == expect, (line, unit)
        # the old computation, kept here so the defect cannot come back unnoticed
        old = unit[:-len(m.group("suffix"))] if m.group("suffix") else unit
        if m.group("suffix"):
            assert old != expect


def test_a_suffixed_unit_the_guest_has_is_accepted():
    by_guest = st.measured_units_by_guest(LIVE)
    for line in ("pct exec 105 -- systemctl is-active qbittorrent-nox.service",
                 "pct exec 103 -- systemctl restart radarr.service"):
        assert unsourced_service_name([line], [], {}, NODE, "", UNITS_103, by_guest) == [], line


def test_a_suffixed_unit_nobody_has_is_now_judged():
    """The blind spot: a short name with `.service` used to vanish."""
    by_guest = st.measured_units_by_guest(LIVE)
    out = unsourced_service_name(["pct exec 103 -- systemctl restart plex.service"],
                                 [], {}, NODE, "", UNITS_103, by_guest)
    assert len(out) == 1, out
    assert "`plex`" in out[0]["why"]
