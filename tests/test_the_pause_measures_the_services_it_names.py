r"""§17.1356 — a step's services are measured wherever they live, not only in its guest.

Live, 2026-10-04. ADD132 — *"Make Radarr and Sonarr actually drive qBittorrent,
and prove the connection"* — was measured in guest **103 only**, Radarr's,
because the pause took the step's SUBJECT guest and the guest ids its text
spells out. qBittorrent is in 105. Of the eleven drafter prompts that followed
(17–21 KB each), measured in `llm_traces`:

    carried `qbittorrent-nox`                  1 of 11
    carried `/var/lib/qbittorrent-nox`         0 of 11
    carried `MASS_PASSWORD`                   11 of 11

So the draft said `systemctl restart qbittorrent` — the unit is
`qbittorrent-nox` — against `/var/lib/qbittorrent/qBittorrent/qBittorrent.conf`,
when it is `/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf`. The
engine then refused its own block with *"`qbittorrent` is not a unit anything the
engine holds names"*: a fact it had measured in §17.1346 and never handed over.

One `ss -tlnp` per running container is enough, and it is the same read
`read_services` already opens with. No guest NAME would have found it —
qBittorrent's container is called `download-client`.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import service_truth as st

SS = json.loads((pathlib.Path(__file__).parent / "fixtures"
                 / "ss_tlnp_every_guest_2026_10_04.json").read_text())
#: what `pct list` says, the same evening
CTS = {"101": "running", "102": "running", "103": "running", "104": "running",
       "105": "running", "111": "running", "120": "stopped", "130": "running"}


class _Spec:
    endpoint = "http://192.168.1.156:8790/mcp/"


def _reads(monkeypatch, answers=None, fail=()):
    """Patch the module's one read with the REAL `ss -tlnp` from each guest."""
    seen: list[str] = []

    async def fake_probe(spec, command):
        seen.append(command)
        gid = command.split()[2]
        if gid in fail:
            return False, ""
        text = (answers or SS).get(gid, "")
        return (bool(text), text)

    monkeypatch.setattr(st, "_probe", fake_probe)
    return seen


# ------------------------------------------------------- what listens where


def test_the_listener_names_come_off_the_real_listings():
    assert st.listeners_in(SS["103"]) == ["Radarr"]
    assert st.listeners_in(SS["104"]) == ["Sonarr"]
    assert st.listeners_in(SS["105"]) == ["qbittorrent-nox"]
    assert st.listeners_in(SS["101"]) == ["jellyfin"]
    assert st.listeners_in(SS["130"]) == ["pihole-FTL"]
    assert st.listeners_in(SS["111"]) == ["node"]


def test_the_units_every_guest_has_name_no_app():
    """`sshd`, postfix's `master` and `systemd` are on all of them."""
    for gid in ("101", "102", "103", "104", "105", "111", "130"):
        assert not ({"sshd", "master", "systemd"} & set(st.listeners_in(SS[gid]))), gid


def test_a_stopped_guest_says_nothing():
    assert st.listeners_in(SS["120"]) == []        # "container '120' not running!"


def test_the_step_word_matches_the_process_name():
    assert st._same_service("qbittorrent", "qbittorrent-nox")
    assert st._same_service("radarr", "Radarr") and st._same_service("sonarr", "Sonarr")
    assert st._same_service("pihole", "pihole-FTL")
    assert not st._same_service("radarr", "Sonarr")
    assert not st._same_service("radarr", "node")


def test_a_name_too_short_to_be_evidence_matches_nothing():
    for short in ("n", "no", "nod"):
        assert not st._same_service(short, "node"), short


# ----------------------------------------------------------- the lookup


@pytest.mark.asyncio
async def test_the_live_step_finds_all_three_guests(monkeypatch):
    seen = _reads(monkeypatch)
    names = st.names_in({"title": "Make Radarr and Sonarr actually drive qBittorrent, "
                                  "and prove the connection", "description": ""})
    assert set(names) >= {"radarr", "sonarr", "qbittorrent"}
    found = await st.guests_of_the_named_services(_Spec(), names, CTS)
    assert found == {"radarr": "103", "sonarr": "104", "qbittorrent": "105"}
    assert all(c.startswith("pct exec ") and "ss -tlnp" in c for c in seen), seen


@pytest.mark.asyncio
async def test_it_stops_once_every_name_is_placed(monkeypatch):
    """105 is the fifth running guest, so the sixth is never read."""
    seen = _reads(monkeypatch)
    await st.guests_of_the_named_services(_Spec(), ["radarr", "sonarr", "qbittorrent"], CTS)
    assert [c.split()[2] for c in seen] == ["101", "102", "103", "104", "105"]


@pytest.mark.asyncio
async def test_a_stopped_guest_is_never_read(monkeypatch):
    seen = _reads(monkeypatch)
    await st.guests_of_the_named_services(_Spec(), ["caddy"], CTS)
    assert "120" not in [c.split()[2] for c in seen]


@pytest.mark.asyncio
async def test_an_unreadable_guest_is_skipped_not_fatal(monkeypatch):
    seen = _reads(monkeypatch, fail={"101", "102", "103"})
    found = await st.guests_of_the_named_services(_Spec(), ["qbittorrent"], CTS)
    assert found == {"qbittorrent": "105"}
    assert len(seen) >= 5


@pytest.mark.asyncio
async def test_a_name_nothing_listens_as_is_simply_absent(monkeypatch):
    _reads(monkeypatch)
    assert await st.guests_of_the_named_services(_Spec(), ["plex"], CTS) == {}


@pytest.mark.asyncio
async def test_no_names_means_no_reads_at_all(monkeypatch):
    seen = _reads(monkeypatch)
    assert await st.guests_of_the_named_services(_Spec(), [], CTS) == {}
    assert seen == []


@pytest.mark.asyncio
async def test_no_channel_reads_nothing():
    assert await st.guests_of_the_named_services(None, ["radarr"], CTS) == {}


# ------------------------------------------------------------- the lane


def test_the_pause_asks_where_the_named_services_are():
    """Verify the lane: a lookup the pause never calls changes no draft."""
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "guests_of_the_named_services(spec, _named, _cts, reading=_reading)" in src
    assert "_st.read_services(spec, _g, mentioned=_named,\n" in src \
        or "read_services(spec, _g, mentioned=_named," in src
    # the subject guest stays first, and more than one guest is measured
    assert "_touch[:4]" in src
    assert "units=list" not in src, "the dead `units` argument is gone"


def test_read_services_uses_the_one_listener_parser():
    """Two copies of the same parse is how they drift (§17.1356)."""
    import inspect
    src = inspect.getsource(st.read_services)
    assert "listeners_in(ss_text)" in src
    assert "_PROC_RE.finditer" not in src


# ------------------------------- a name is a candidate, never a unit name

SHOW = json.loads((pathlib.Path(__file__).parent / "fixtures"
                   / "systemctl_show_names_2026_10_04.json").read_text())


def test_the_three_spellings_really_do_disagree():
    """Read off the live guests. `ss` reports the PROCESS, the step uses an
    English word, and only a third spelling is the unit:

        103:Radarr          LoadState=not-found
        103:radarr          LoadState=loaded    ActiveState=active
        105:qbittorrent     LoadState=not-found
        105:qbittorrent-nox LoadState=loaded    ActiveState=active
    """
    assert "LoadState=not-found" in SHOW["103:Radarr"]
    assert "LoadState=loaded" in SHOW["103:radarr"]
    assert "LoadState=not-found" in SHOW["105:qbittorrent"]
    assert "LoadState=loaded" in SHOW["105:qbittorrent-nox"]
    # and the not-found answer still claims a state, which is the trap
    assert "ActiveState=inactive" in SHOW["105:qbittorrent"]


def _guest_reads(monkeypatch, gid: str):
    """`ss` and `systemctl show` for one guest, from the live captures."""
    asked: list[str] = []

    async def fake_probe(spec, command):
        asked.append(command)
        if "ss -tlnp" in command:
            return True, SS[gid]
        if "systemctl show" in command:
            name = command.rstrip("'").split()[-1]
            return (True, SHOW.get(f"{gid}:{name}", "LoadState=not-found\nActiveState=inactive\n"))
        return False, ""        # id/ls/stat: fail-soft, not this test's business

    monkeypatch.setattr(st, "_probe", fake_probe)
    return asked


@pytest.mark.asyncio
async def test_the_listeners_capital_name_does_not_lose_the_unit(monkeypatch):
    """Resolving to the listener alone loses Radarr: `systemctl show Radarr` is
    not `radarr.service`."""
    asked = _guest_reads(monkeypatch, "103")
    out = await st.read_services(_Spec(), "103", mentioned=["radarr"])
    assert [s.unit for s in out] == ["radarr.service"]
    assert out[0].state == "active" and out[0].user == "radarr"
    tried = [c.rstrip("'").split()[-1] for c in asked if "systemctl show" in c]
    assert tried[:2] == ["Radarr", "radarr"], tried


@pytest.mark.asyncio
async def test_the_steps_word_does_not_become_the_unit(monkeypatch):
    """ADD132's word is `qbittorrent`; the unit is `qbittorrent-nox.service`."""
    asked = _guest_reads(monkeypatch, "105")
    out = await st.read_services(_Spec(), "105", mentioned=["qbittorrent"])
    assert [s.unit for s in out] == ["qbittorrent-nox.service"]
    assert out[0].state == "active"
    tried = [c.rstrip("'").split()[-1] for c in asked if "systemctl show" in c]
    assert "qbittorrent-nox" in tried


@pytest.mark.asyncio
async def test_a_service_the_guest_does_not_have_is_no_fact(monkeypatch):
    """The six phantom rows: `radarr.service (inactive)` on the download client.
    `LoadState=not-found` is the difference."""
    _guest_reads(monkeypatch, "105")
    out = await st.read_services(_Spec(), "105", mentioned=["radarr", "sonarr"])
    assert [s.name for s in out] == ["qbittorrent-nox"], [s.name for s in out]


@pytest.mark.asyncio
async def test_one_row_per_service_however_many_names_reach_it(monkeypatch):
    _guest_reads(monkeypatch, "105")
    out = await st.read_services(_Spec(), "105",
                                 mentioned=["qbittorrent", "qbittorrent-nox"])
    assert [s.unit for s in out] == ["qbittorrent-nox.service"]
