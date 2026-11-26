r"""§17.2032 — a bare append is judged against the file in the guest its LINE addresses.

§17.1343's sectioned-config rule read the file in `subjects[0]` -- the first guest the step's title names -- while
§17.1361 had already taught its sibling (the inert-edit rule) to read the guest a `pct exec N --` line addresses.
A step titled for container 103 that appends with `pct exec 105 -- sh -c '… >> qBittorrent.conf'` read 103's copy
of that path: "No such file", a gap, never a judgment -- so the key landing in the wrong `[section]` went unsaid.
The 2026-10-12 audit ledger recorded it; nothing fixed it.

Driven through the real `_read` (the transport is patched, §17.1353's lesson), with two guests that answer
differently.
"""
from __future__ import annotations

import pytest

from app.modules import runbook_preconditions as rp

CONF = "/var/lib/qbittorrent/.config/qBittorrent/qBittorrent.conf"


class _Res:
    def __init__(self, text: str, is_error: bool = False):
        self.structured = None
        self.text = text
        self.is_error = is_error


class _Spec:
    endpoint = "http://192.0.2.156:8790/mcp/"


def _machines(monkeypatch, sections: dict[str, str]) -> list[str]:
    """Each guest answers for its OWN copy of the file: a section count, or grep's "No such file"."""
    seen: list[str] = []

    async def fake_call_tool(spec, tool, args, **kw):
        cmd = args["command"]
        seen.append(cmd)
        parts = cmd.split()
        gid = parts[2] if len(parts) > 2 and parts[:2] == ["pct", "exec"] else ""
        if gid in sections:
            return _Res(sections[gid] + "\n")
        return _Res(f"grep: {CONF}: No such file or directory\n", True)

    import app.modules.mcp_client as mc
    monkeypatch.setattr(mc, "call_tool", fake_call_tool)
    return seen


IN_105 = f"pct exec 105 -- sh -c \"printf '%s\\n' 'Session\\DefaultSavePath=/media/downloads' >> {CONF}\""
IN_104 = "pct exec 104 -- sh -c \"echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf\""


@pytest.mark.asyncio
async def test_the_append_is_read_in_its_own_guest_not_the_steps_first(monkeypatch):
    seen = _machines(monkeypatch, {"105": "5", "104": "0"})
    out = await rp.a_bare_append_lands_in_the_last_section(_Spec(), [IN_105, IN_104], "103")
    assert [c.split()[2] for c in seen] == ["105", "104"], seen
    assert len(out) == 1 and CONF in out[0]["why"] and "5 `[section]` headers" in out[0]["why"], out
    assert out[0]["command"].startswith("pct exec 105")


@pytest.mark.asyncio
async def test_before_the_fix_the_subject_was_read_and_nothing_judged(monkeypatch):
    """The shape of the defect, kept as a measurement: guest 103 has no such file, so reading IT says nothing."""
    seen = _machines(monkeypatch, {"105": "5"})
    out = await rp.a_bare_append_lands_in_the_last_section(_Spec(), [IN_105.replace("pct exec 105 -- ", "")], "103")
    assert out == [] and seen and seen[0].split()[2] == "103"


@pytest.mark.asyncio
async def test_a_line_naming_no_guest_still_falls_back_to_the_subject(monkeypatch):
    seen = _machines(monkeypatch, {"105": "5"})
    out = await rp.a_bare_append_lands_in_the_last_section(
        _Spec(), [f"printf '%s\\n' 'Session\\X=1' >> {CONF}"], "105")
    assert len(out) == 1 and seen[0].split()[2] == "105"


@pytest.mark.asyncio
async def test_through_unmet_the_step_titled_for_another_guest_is_judged(monkeypatch):
    """End to end: a step whose title names container 103, a block that appends inside 105 and 104."""
    _machines(monkeypatch, {"105": "5", "104": "0"})
    inv = {"cts": {"103": "running", "104": "running", "105": "running"}, "vms": {},
           "names": {"103": "sonarr", "104": "radarr", "105": "qbittorrent"}}
    node = {"node_key": "ADD140", "title": "LXC 103: point downloads at /media/downloads",
            "description": "Container 103 hands downloads to qBittorrent."}
    rows = await rp.unmet([IN_105, IN_104], _Spec(), plan=[], node=node, inventory=inv)
    sect = [r for r in rows if "SECTIONED config" in r["why"]]
    assert len(sect) == 1 and sect[0]["command"].startswith("pct exec 105"), [r["why"][:80] for r in rows]
