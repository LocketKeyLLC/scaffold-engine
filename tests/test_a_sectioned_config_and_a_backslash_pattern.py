r"""§17.1343 — a key appended to a sectioned config, and a backslash in a grep pattern.

Both from one live step. ADD130 ("point qBittorrent's downloads at /media/downloads")
failed, was fixed, reported success, and was still not done.

1. Its own check was `grep -E "Session\\DefaultSavePath" <conf>`. Inside double
   quotes the shell halves the escape, so grep receives `Session\DefaultSavePath`,
   where an expression reads `\D` as a plain D. Measured against a file holding
   exactly that line: the `-E` form exits 1, the `-F` form matches. Under `set -e`
   that exit ended a step whose edit had worked.

2. The second run appended the key with `>>`, which writes to the END of the file.
   The key landed under `[Preferences]`, while every other `Session\` key in that
   file sits under `[BitTorrent]` and the app reads it as
   `BitTorrent/Session\DefaultSavePath`. The run reported success, the key was in
   the file, the check found it, and nothing read it.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from app.modules import runbook_preconditions as rp
from app.modules import runbook_templates as rt

#: the live config, as the machine holds it
LIVE_CONF = ("[BitTorrent]\nSession\\Port=61661\nSession\\QueueingSystemEnabled=false\n\n"
             "[LegalNotice]\nAccepted=true\n\n[Meta]\nMigrationVersion=4\n\n"
             "[Network]\nCookies=@Invalid()\n\n[Preferences]\nWebUI\\Port=8080\n")


def _grep(pattern_line: str, text: str) -> int:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "qBittorrent.conf"
        f.write_text(text, encoding="utf-8")
        return subprocess.run(["bash", "-c", pattern_line.replace("FILE", str(f))],
                              capture_output=True, text=True).returncode


def test_bash_and_grep_settle_the_backslash_question():
    """Measured, not reasoned — this is the whole reason for the repair."""
    body = LIVE_CONF + "Session\\DefaultSavePath=/media/downloads\n"
    assert _grep('grep -E "Session\\\\DefaultSavePath" FILE', body) == 1, "the -E form finds nothing"
    assert _grep('grep -F "Session\\\\DefaultSavePath" FILE', body) == 0, "the -F form matches"
    assert _grep("grep -E 'Session\\\\DefaultSavePath' FILE", body) == 0, "single quotes also work"


def test_the_repair_turns_that_grep_into_a_fixed_string():
    out, repairs = rt.grep_a_backslash_as_a_fixed_string(
        ['grep -E "Session\\\\DefaultSavePath" /var/lib/x/qBittorrent.conf'])
    assert out[0].startswith("grep -F "), out[0]
    assert len(repairs) == 1 and "fixed-string" in repairs[0]["why"]


def test_the_repair_keeps_the_other_flags_and_drops_only_the_expression_one():
    out, _ = rt.grep_a_backslash_as_a_fixed_string(['grep -qE "a\\\\b" f'])
    assert "-F" in out[0] and "-q" in out[0] and "-E" not in out[0], out[0]


def test_the_repair_leaves_alone_what_is_already_right():
    same = ["grep -F \"a\\\\b\" f", "grep -E 'a\\\\b' f", 'grep -E "^WebUI" f']
    out, repairs = rt.grep_a_backslash_as_a_fixed_string(same)
    assert out == same and repairs == []


def test_the_rendered_script_carries_the_repair():
    """The grep that ended the step was a line INSIDE the pushed script."""
    import inspect
    src = inspect.getsource(rt.fill_free_params) if hasattr(rt, "fill_free_params") else ""
    whole = Path(rt.__file__).read_text(encoding="utf-8")
    assert "grep_a_backslash_as_a_fixed_string(_text_in.split" in whole, "wired into the rendered text"
    assert "§17.1343" in whole


def test_the_live_append_is_found_with_its_target():
    script = ("set -e\nCONF=/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf\n"
              "grep -q '^Session\\\\DefaultSavePath=' \"$CONF\" || printf '%s\\n' "
              "'Session\\DefaultSavePath=/media/downloads' >> \"$CONF\"\n")
    hits = rp.appends_a_key([script])
    assert len(hits) == 1 and hits[0][1] == "$CONF", hits


def test_a_comment_and_a_non_append_are_not_appends():
    assert rp.appends_a_key(["# echo 'a=b' >> /etc/x", "grep -q a /etc/hosts", "echo hello >> /tmp/log"]) == []


def test_a_section_header_or_a_section_aware_tool_is_recognised():
    assert rp._SECTION_AWARE_RE.search("printf '[BitTorrent]\\n' >> f")
    assert rp._SECTION_AWARE_RE.search(r"sed -i '/^\[BitTorrent\]/a Session\DefaultSavePath=/x' f")
    assert rp._SECTION_AWARE_RE.search("crudini --set f BitTorrent k v")
    assert not rp._SECTION_AWARE_RE.search("echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf")


class _Spec:
    name = "pve-runner"
    headers: dict = {}
    endpoint = "http://192.168.1.156:8790/mcp/"


@pytest.mark.asyncio
async def test_a_sectioned_file_refuses_the_bare_append(monkeypatch):
    r"""The FILE settles it: `grep -c '^\['` on the real config returns 5."""
    async def fake_read(spec, command):
        assert "grep -c" in command and "qBittorrent.conf" in command, command
        return True, str(LIVE_CONF.count("\n[") + LIVE_CONF.startswith("["))
    monkeypatch.setattr(rp, "_read", fake_read)
    script = ("CONF=/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf\n"
              "printf '%s\\n' 'Session\\DefaultSavePath=/media/downloads' >> \"$CONF\"\n")
    out = await rp.a_bare_append_lands_in_the_last_section(_Spec(), [script], "105")
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "SECTIONED config" in why and "[Preferences]" in why and "[BitTorrent]" in why
    assert "read the SECTION, not just the key" in why


@pytest.mark.asyncio
async def test_a_flat_file_is_left_alone(monkeypatch):
    async def fake_read(spec, command):
        return True, "0"
    monkeypatch.setattr(rp, "_read", fake_read)
    out = await rp.a_bare_append_lands_in_the_last_section(
        _Spec(), ["echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf"], "105")
    assert out == []


@pytest.mark.asyncio
async def test_an_unreadable_file_is_not_judged(monkeypatch):
    async def fake_read(spec, command):
        return False, ""
    monkeypatch.setattr(rp, "_read", fake_read)
    script = "printf '%s\\n' 'a=b' >> /etc/some.conf\n"
    assert await rp.a_bare_append_lands_in_the_last_section(_Spec(), [script], "105") == []


@pytest.mark.asyncio
async def test_a_block_that_places_the_key_in_its_section_is_not_refused(monkeypatch):
    async def fake_read(spec, command):
        return True, "5"
    monkeypatch.setattr(rp, "_read", fake_read)
    script = (r"sed -i '/^\[BitTorrent\]/a Session\DefaultSavePath=/media/downloads' "
              "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf\n"
              "printf '%s\\n' 'Session\\DefaultSavePath=/media/downloads' >> /tmp/other.conf\n")
    assert await rp.a_bare_append_lands_in_the_last_section(_Spec(), [script], "105") == []
