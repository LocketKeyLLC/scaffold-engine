r"""§17.1353 — an in-place substitution whose pattern the file does not hold.

Live, 2026-10-04. ADD133 created two Jellyfin libraries and gave each one its
content type with

    sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' …/Movies/options.xml

over a copy of an existing library's `options.xml`. Measured on the machine
afterwards: that file holds no `ContentType` element at all — Jellyfin 10.11.11
keeps a library's type in a `<type>.collection` marker file — so the edit
matched nothing, exited 0, and changed nothing. The step reported success, its
check read only the two `.mblink` paths, and both libraries came out as mixed
content.

`sed -i` cannot fail for matching nothing, so the FILE settles it: one
`grep -c -F` through the runner.

The second half of this file is the reason the live run got that far: §17.1343's
sectioned-config rule unpacked `_read` as `(ok, text)` while `_read` returns the
listing itself, so every call raised into `unmet`'s warning and the rule never
fired once in production. All four of its tests stubbed `_read` with a 2-tuple
the real function never returns — a mocked read hiding an inert gate, which is
why nothing here stubs `_read`: the tests patch the TRANSPORT and let the real
`_read` run.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.modules import runbook_preconditions as rp

FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
           / "add133_jellyfin_libraries_2026_10_04.sh").read_text()
#: the real head of the file the live edit was aimed at, read from container 101
LIVE_OPTIONS = """<?xml version="1.0" encoding="utf-8"?>
<LibraryOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Enabled>true</Enabled>
  <PathInfos>
    <MediaPathInfo>
      <Path>/media</Path>
    </MediaPathInfo>
  </PathInfos>
  <MetadataCountryCode>US</MetadataCountryCode>
</LibraryOptions>
"""
#: the live qBittorrent config §17.1343 was measured on: 5 `[section]` headers
LIVE_CONF = ("[AutoRun]\nenabled=false\n[BitTorrent]\n"
             r"Session\DefaultSavePath=/media/downloads" "\n"
             "[Core]\nAutoDeleteAddedTorrentFile=Never\n[LegalNotice]\nAccepted=true\n"
             "[Preferences]\nWebUI\\Port=8080\n")


class _Spec:
    endpoint = "http://192.168.1.156:8790/mcp/"


class _Res:
    """What `mcp_client.call_tool` hands back: `.structured`, `.text`, `.is_error`."""

    def __init__(self, text: str, is_error: bool = False):
        self.structured = None
        self.text = text
        self.is_error = is_error


def _transport(monkeypatch, answer):
    """Patch the TRANSPORT, so the real `_read` — and its real contract — runs."""
    seen: list[str] = []

    async def fake_call_tool(spec, tool, args, **kw):
        assert tool == "run_readonly", tool
        seen.append(args["command"])
        out = answer(args["command"])
        return _Res(out if isinstance(out, str) else out[0],
                    False if isinstance(out, str) else out[1])

    import app.modules.mcp_client as mc
    monkeypatch.setattr(mc, "call_tool", fake_call_tool)
    return seen


# ---------------------------------------------------------------- the detector


def test_the_live_block_yields_both_edits_with_the_file_each_is_copied_from():
    found = rp.in_place_substitutions([FIXTURE])
    assert [p for _, p, _ in found] == [
        "/var/lib/jellyfin/root/default/Movies/options.xml",
        "/var/lib/jellyfin/root/default/Shows/options.xml"]
    assert {pat for _, _, pat in found} == {"<ContentType>.*</ContentType>"}
    for _, path, _ in found:
        assert rp.copied_from([FIXTURE], path) == \
            "/var/lib/jellyfin/root/default/Media/options.xml"


def test_a_backup_copy_is_not_the_source_a_file_came_from():
    """Every edit in the live block is preceded by `cp -a X X.bak.<stamp>`."""
    assert rp.copied_from(
        ['cp -a "/etc/x.conf" "/etc/x.conf.bak.$(date +%s)"'], "/etc/x.conf") == ""


def test_sed_without_in_place_and_a_comment_are_not_edits():
    assert rp.in_place_substitutions(["sed -n 's|a|b|p' /etc/x.conf"]) == []
    assert rp.in_place_substitutions(["sed -e 's|aaaa|b|' /etc/x.conf > /tmp/o"]) == []
    assert rp.in_place_substitutions(["# sed -i 's|aaaa|b|' /etc/x.conf"]) == []


def test_perl_in_place_counts_too():
    found = rp.in_place_substitutions([
        """perl -pi -e 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' /etc/x.xml"""])
    assert [(p, pat) for _, p, pat in found] == [
        ("/etc/x.xml", "<ContentType>.*</ContentType>")]


def test_the_anchor_is_the_longest_run_of_plain_text():
    assert rp.literal_anchor("<ContentType>.*</ContentType>") == "</ContentType>"
    assert rp.literal_anchor("^bridge-ports .*") == "bridge-ports "
    assert rp.literal_anchor("^Session.DefaultSavePath=.*$") == "DefaultSavePath="


def test_a_pattern_the_engine_cannot_resolve_yields_no_anchor():
    """A shell expansion, an escape, or nothing literal to find: no judgment."""
    assert rp.literal_anchor("^${KEY}=.*") == ""
    assert rp.literal_anchor(r"^Session\\DefaultSavePath=.*") == ""
    assert rp.literal_anchor(".*") == ""
    assert rp.literal_anchor("^a=.*") == ""           # too short to be evidence


# ------------------------------------------------------------------- the gate


@pytest.mark.asyncio
async def test_the_live_edit_is_refused_because_the_file_has_no_such_element(monkeypatch):
    """0 matches in the file the block copies from: the edit is inert."""
    def answer(cmd):
        assert cmd.startswith("pct exec 101 -- grep -c -F -- "), cmd
        path = cmd.rsplit(" ", 1)[1]
        if "/Media/" not in path:
            return f"grep: {path}: No such file or directory\n"   # not created yet
        return str(LIVE_OPTIONS.count("</ContentType>")) + "\n"
    _transport(monkeypatch, answer)
    out = await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [FIXTURE], "101")
    assert len(out) == 2, out
    why = out[0]["why"]
    assert "changes NOTHING" in why and "`</ContentType>`" in why
    assert "/var/lib/jellyfin/root/default/Media/options.xml" in why   # the file READ
    assert "read the setting back out" in why


@pytest.mark.asyncio
async def test_an_edit_whose_pattern_the_file_holds_is_left_alone(monkeypatch):
    def answer(cmd):
        return str(LIVE_OPTIONS.count("</MetadataCountryCode>")) + "\n"
    _transport(monkeypatch, answer)
    script = ("cp /var/lib/jellyfin/root/default/Media/options.xml /tmp/x.xml\n"
              "sed -i 's|<MetadataCountryCode>.*</MetadataCountryCode>"
              "|<MetadataCountryCode>GB</MetadataCountryCode>|' /tmp/x.xml\n")
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [script], "101") == []


@pytest.mark.asyncio
async def test_a_file_nothing_can_read_is_not_judged(monkeypatch):
    """Neither the target nor a source answers with a count: no refusal."""
    def answer(cmd):
        return "grep: no such file\n"
    seen = _transport(monkeypatch, answer)
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [FIXTURE], "101") == []
    assert seen, "the gate must actually have tried to read"


@pytest.mark.asyncio
async def test_a_refusal_from_the_runner_is_not_a_judgment(monkeypatch):
    """The read-only channel refuses the command: `_read` returns '' and nothing
    is refused out of blindness. Measured live — a shell assignment inside
    `sh -c` is "not a known read-only command", which is why the read is a bare
    `grep`."""
    def answer(cmd):
        return "(refused by the local runner: x is not a known read-only command)"
    _transport(monkeypatch, answer)
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [FIXTURE], "101") == []


@pytest.mark.asyncio
async def test_the_read_is_the_shape_the_runner_accepts(monkeypatch):
    r"""No assignment, no `sh -c`, no `|| true`: one bare `grep -c -F`.

    Measured against the live runner: `sh -c 'f=…; …'` came back "(refused by
    the local runner: … options.xml is not a known read-only command)", while
    `pct exec 101 -- grep -c -F -- "</ContentType>" <path>` returned `0`.
    """
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [FIXTURE], "101")
    assert seen
    for cmd in seen:
        assert re.fullmatch(r'pct exec 101 -- grep -c -F -- "[^"]+" /\S+', cmd), cmd


@pytest.mark.asyncio
async def test_a_block_with_no_guest_is_not_judged(monkeypatch):
    """The host-side `sed -i` over /etc/network/interfaces in the corpus: the
    read is `pct exec <gid>`, so with no guest there is nothing to read."""
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    host = "sed -i 's/bridge-ports .*/bridge-ports enp5s0f3/' /etc/network/interfaces\n"
    assert await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [host], "") == []
    assert seen == []


@pytest.mark.asyncio
async def test_one_judgment_per_file_and_pattern(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    twice = ("sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' /tmp/a.xml\n"
             "sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' /tmp/a.xml\n")
    out = await rp.an_in_place_edit_the_file_cannot_match(_Spec(), [twice], "101")
    assert len(out) == 1 and len(seen) == 1


# ------------------------------------------- the contract the mocks hid (§17.1343)


@pytest.mark.asyncio
async def test_the_sectioned_config_rule_fires_through_the_real_read(monkeypatch):
    r"""§17.1343's rule, driven through the real `_read`.

    It never fired in production: the call site unpacked `ok, out_text = await
    _read(...)` while `_read` returns the listing, so every call raised
    ValueError into `unmet`'s `append_section_check_failed` warning. Its four
    tests stubbed `_read` itself with a 2-tuple, so none of them could see it.
    """
    def answer(cmd):
        assert "grep -c" in cmd and "qBittorrent.conf" in cmd, cmd
        return str(len(re.findall(r"^\[", LIVE_CONF, re.M))) + "\n"
    _transport(monkeypatch, answer)
    script = ("CONF=/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf\n"
              "printf '%s\\n' 'Session\\DefaultSavePath=/media/downloads' >> \"$CONF\"\n")
    out = await rp.a_bare_append_lands_in_the_last_section(_Spec(), [script], "105")
    assert len(out) == 1, out
    assert "5 `[section]` headers" in out[0]["why"]


@pytest.mark.asyncio
async def test_a_flat_config_is_still_left_alone_through_the_real_read(monkeypatch):
    _transport(monkeypatch, lambda cmd: "0\n")
    out = await rp.a_bare_append_lands_in_the_last_section(
        _Spec(), ["echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf"], "105")
    assert out == []


@pytest.mark.asyncio
async def test_an_unreadable_section_count_is_not_judged_through_the_real_read(monkeypatch):
    _transport(monkeypatch, lambda cmd: ("", True))
    out = await rp.a_bare_append_lands_in_the_last_section(
        _Spec(), ["printf '%s\\n' 'a=b' >> /etc/some.conf\n"], "105")
    assert out == []


def test_no_read_call_site_unpacks_the_listing_as_a_pair():
    """The ratchet: `_read` returns one string, and a caller that takes it as a
    pair cannot work. This is the defect that made §17.1343 inert.

    Read from the AST, not from the text: `a, b = await _read(x), await _read(y)`
    is two calls and two values, and is fine.
    """
    import ast

    tree = ast.parse(pathlib.Path(rp.__file__).read_text())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.targets[0], (ast.Tuple, ast.List)):
            continue
        val = node.value
        if (isinstance(val, ast.Await) and isinstance(val.value, ast.Call)
                and isinstance(val.value.func, ast.Name) and val.value.func.id == "_read"):
            bad.append(node.lineno)
    assert bad == [], f"_read unpacked as a tuple at line(s) {bad}"


def test_the_refusal_asks_the_drafter_again():
    """§17.1353's marker was never put in `_SHAPE_REFUSALS`, so even once the
    layer ran (§17.1359) this refusal would have PARKED the frame instead of
    redrafting it. Verify the lane, not just the assertion."""
    from app.modules.supervised_runs import refusal_kinds
    refused = [{"why": (
        "this edit changes NOTHING: `</ContentType>` does not appear in /x/options.xml "
        "-- read just now, 0 matches.")}]
    assert refusal_kinds({"refused": refused}) == {"this edit changes NOTHING"}
