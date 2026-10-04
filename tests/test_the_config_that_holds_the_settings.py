r"""§17.1361 — the config that holds the settings, and an append that lands nowhere.

ADD132 ran and failed honestly: `LOGIN FAILED: Fails.`. It had hashed the
password correctly, stopped the service, restarted it, and registered the client
through each app's API — and written the WebUI keys into the wrong file.

Measured on guest 105 afterwards:

    qBittorrent.conf        5 `[sections]`, holds [Preferences] and WebUI\Port=8080
    qBittorrent-data.conf   1 section, no [Preferences] anywhere

`config_candidates` globs `…/.config/*/*.conf`, `ls` returns both alphabetically,
and `-` (0x2D) sorts before `.` (0x2E) — so `configs[0]`, the file the facts
name, was the one with no settings in it. The draft believed the facts, its

    sed -i '/^\[Preferences\]/a WebUI\\Username=admin…' …/qBittorrent-data.conf

matched nothing, wrote nothing, exited 0, and only the step's own login check
caught it.

Two fixes, both measurable:

* which file holds the settings is a count — `[section]` headers and `key=`
  lines — so the candidates are scored and the richest leads. Live:
  `qBittorrent.conf=8, qBittorrent-data.conf=2`.
* §17.1353 judged a substitution's PATTERN; an append's ADDRESS is the same
  question, and neither can fail for not matching.
"""
from __future__ import annotations

import pytest

from app.modules import runbook_preconditions as rp

#: the line that failed, verbatim
LIVE = (r"pct exec 105 -- sed -i '/^\[Preferences\]/a WebUI\\Username=admin\n"
        r"WebUI\\Password_PBKDF2='" '"$HASH" '
        "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf")
DATA = "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf"
REAL = "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf"


class _Spec:
    endpoint = "http://192.168.1.156:8790/mcp/"


class _Res:
    def __init__(self, text):
        self.structured, self.text, self.is_error = None, text, False


def _transport(monkeypatch, answer):
    seen: list[str] = []

    async def fake_call_tool(spec, tool, args, **kw):
        seen.append(args["command"])
        return _Res(answer(args["command"]))

    import app.modules.mcp_client as mc
    monkeypatch.setattr(mc, "call_tool", fake_call_tool)
    return seen


# ----------------------------------------------- an address is a pattern too


def test_the_live_append_is_detected_with_its_target_and_address():
    found = rp.in_place_substitutions([LIVE])
    assert len(found) == 1, found
    _, path, pat = found[0]
    assert path == DATA
    assert pat == r"^\[Preferences\]"


def test_the_escaped_brackets_of_a_sed_address_are_unwrapped():
    """They are the regex's own punctuation, not a shell escape."""
    assert rp.literal_anchor(r"^\[Preferences\]") == "Preferences"
    assert rp.literal_anchor(r"^\[BitTorrent\]") == "BitTorrent"


def test_a_real_backslash_in_a_key_still_yields_no_anchor():
    """qBittorrent's `Session\\DefaultSavePath` — the engine does not guess at it."""
    assert rp.literal_anchor(r"^Session\\DefaultSavePath=.*") == ""
    assert rp.literal_anchor("^${KEY}=.*") == ""


def test_the_substitution_shapes_still_work():
    found = rp.in_place_substitutions(
        ["sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' /x/options.xml"])
    assert [(p, pat) for _, p, pat in found] == [("/x/options.xml", "<ContentType>.*</ContentType>")]
    assert rp.literal_anchor("<ContentType>.*</ContentType>") == "</ContentType>"


def test_each_address_verb_counts():
    for verb in ("a", "i", "c"):
        found = rp.in_place_substitutions(
            [f"sed -i '/^\\[Preferences\\]/{verb} Key=1' {DATA}"])
        assert found, verb
        assert found[0][2] == r"^\[Preferences\]"


# ----------------------------------------------------------- the gate


@pytest.mark.asyncio
async def test_the_live_append_is_refused_on_the_file_with_no_such_section(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    out = await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [LIVE], "103")
    assert len(out) == 1, out
    assert "`Preferences` does not appear in " + DATA in out[0]["why"]
    # read in guest 105, which the LINE names -- not 103, the step's subject
    assert all(c.startswith("pct exec 105 -- grep -c -F") for c in seen), seen


@pytest.mark.asyncio
async def test_the_same_append_on_the_right_file_is_not_refused(monkeypatch):
    _transport(monkeypatch, lambda cmd: "5\n")
    good = f"pct exec 105 -- sed -i '/^\\[Preferences\\]/a WebUI\\\\Username=admin' {REAL}"
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [good], "103") == []


@pytest.mark.asyncio
async def test_the_read_goes_to_the_guest_the_line_names(monkeypatch):
    """The step's subject was 103; the path only exists in 105. Reading 103 would
    have answered "No such file" and judged nothing."""
    seen = _transport(monkeypatch, lambda cmd: ("0\n" if "pct exec 105" in cmd
                                                else "grep: No such file or directory\n"))
    out = await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [LIVE], "103")
    assert len(out) == 1, out
    assert [c.split()[2] for c in seen] == ["105"]


@pytest.mark.asyncio
async def test_a_line_naming_no_guest_falls_back_to_the_subject(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    bare = f"sed -i '/^\\[Preferences\\]/a Key=1' {DATA}"
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [bare], "105")
    assert [c.split()[2] for c in seen] == ["105"]


@pytest.mark.asyncio
async def test_no_guest_anywhere_judges_nothing(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    bare = f"sed -i '/^\\[Preferences\\]/a Key=1' {DATA}"
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [bare], "") == []
    assert seen == []


# --------------------------------------------- which config the facts name


def test_the_candidate_scoring_is_read_from_the_file(monkeypatch):
    """The measurement that decides it, as the live guest answered:

        qBittorrent.conf=8   qBittorrent-data.conf=2
    """
    import inspect

    from app.modules import service_truth as st
    src = inspect.getsource(st.read_services)
    assert "if len(paths) > 1:" in src
    assert 'grep -c -E' in src and '^\\\\[|^[A-Za-z][A-Za-z0-9_.-]*=' in src
    assert "key=lambda q: -score.get(q, 0)" in src
    assert 's.reads["config settings"]' in src


def test_a_single_config_needs_no_second_read():
    """Radarr has one `config.xml`; nothing extra is read for it."""
    import inspect

    from app.modules import service_truth as st
    src = inspect.getsource(st.read_services)
    i = src.index("if len(paths) > 1:")
    j = src.index("if paths:", i)
    assert "_probe" in src[i:j]          # the extra read lives inside that branch
