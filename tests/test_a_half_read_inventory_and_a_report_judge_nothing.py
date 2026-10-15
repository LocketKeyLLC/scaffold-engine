r"""§17.1575/1576 — two ways the engine failed steps over its own reading on 2026-10-15, 90 s after the host rebooted.

1575  `read_inventory` read `pct list` while the cluster service was still coming up: the read hung for its 15 s and
      came back as nothing, `qm list` answered, and the host "had" three VMs and no containers. The probe of
      container 111 (c2792d4b T2) was refused "there is no guest 111 on this host" three drafts running. A listing
      the host would not give is UNREAD, not empty: it is read once more, and if still silent its family is marked
      `unread` and nothing is refused for being absent from it.
1576  T12 "Compile verification report" (9e1c2c68) quoted `systemctl is-active` from T7's evidence and was failed
      three times "produced INSTRUCTIONS, not work"; the job ended blocked with every check met. A report's
      commands are its content (as a document's are, §17.1484).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.modules import runbook_preconditions as pc
from app.modules import supervised_runs as sr
from app.modules.machine_truth import known_guest

PCT_LIST = """VMID       Status     Lock         Name
101        running                 media
111        running                 panel"""
QM_LIST = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       100 gpu-vm               stopped    16384             40.00 0
       110 ai-vm                running    16384            100.00 4186"""
REFUSAL = ""        # `_read` already turns a privilege note / timeout into ""


def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _host(answers: dict[str, list[str]]):
    """`answers[cmd]` is the sequence of outputs; the last one repeats."""
    calls: list[str] = []

    async def fake(spec, tool, args):
        cmd = args["command"]; calls.append(cmd)
        seq = answers.get(cmd, [""]); n = sum(1 for c in calls if c == cmd)
        r = MagicMock(); r.structured = None; r.is_error = False
        r.text = seq[min(n, len(seq)) - 1]
        return r
    fake.calls = calls
    return fake


PROBE = "pct exec 111 -- sh -c 'ls /opt'"


@pytest.mark.asyncio
async def test_a_listing_that_hung_once_is_read_again_and_the_guest_is_known():
    host = _host({"pct list": [REFUSAL, PCT_LIST], "qm list": [QM_LIST]})
    with patch("app.modules.mcp_client.call_tool", new=host):
        inv = await pc.read_inventory(_spec())
        assert host.calls.count("pct list") == 2 and host.calls.count("qm list") == 1
        assert inv["cts"]["111"] == "running" and inv["names"]["111"] == "panel" and inv["unread"] == []
        out = await pc.unmet([PROBE], _spec(), inventory=inv)
    assert not any("there is no guest" in o["why"] for o in out)


@pytest.mark.asyncio
async def test_a_listing_still_silent_is_unread_and_nothing_is_refused_for_being_absent_from_it():
    host = _host({"pct list": [REFUSAL], "qm list": [QM_LIST]})
    with patch("app.modules.mcp_client.call_tool", new=host):
        inv = await pc.read_inventory(_spec())
        assert inv is not None and inv["unread"] == ["cts"] and inv["vms"]["110"] == "running" and inv["cts"] == {}
        out = await pc.unmet([PROBE], _spec(), inventory=inv, node={"title": "Probe container 111", "description": ""})
    assert not any("there is no guest" in o["why"] for o in out), out
    assert known_guest("111", inv) and known_guest("111", inv, "pct exec 111 -- ls")
    # the symmetric case: VMs unread, a `qm` command is not refused either
    host2 = _host({"pct list": [PCT_LIST], "qm list": [REFUSAL]})
    with patch("app.modules.mcp_client.call_tool", new=host2):
        inv2 = await pc.read_inventory(_spec())
        assert inv2["unread"] == ["vms"] and host2.calls.count("qm list") == 2
        out2 = await pc.unmet(["qm guest exec 110 -- ls"], _spec(), inventory=inv2)
    assert not any("there is no guest" in o["why"] for o in out2), out2


@pytest.mark.asyncio
async def test_a_full_listing_still_refuses_a_guest_the_host_does_not_have():
    host = _host({"pct list": [PCT_LIST], "qm list": [QM_LIST]})
    with patch("app.modules.mcp_client.call_tool", new=host):
        inv = await pc.read_inventory(_spec())
        assert inv["unread"] == [] and host.calls.count("pct list") == 1
        out = await pc.unmet(["pct exec 999 -- ls"], _spec(), inventory=inv)
    assert any(o["why"].startswith("there is no guest 999") for o in out), out
    assert not known_guest("999", inv)
    # both silent: no inventory at all, as before
    with patch("app.modules.mcp_client.call_tool", new=_host({})):
        assert await pc.read_inventory(_spec()) is None


# ── §17.1576 ─────────────────────────────────────────────────────────────────────────────────────────────────────
REPORT = ("This node is the aggregation report. The verification commands were already executed in upstream steps "
          "T5–T11; no new commands are run here.\n\n- **Unit active/enabled — MET.** T7 output reports "
          "`systemctl is-active scaffold-rafilter.service` printed `active` and `systemctl is-enabled` printed `enabled`.\n"
          "- **Guests untouched — MET.** T10 is a read-only listing (`pct list`) showing the guests unchanged.\n")


def test_a_report_quoting_the_commands_that_ran_is_not_instructions():
    for title in ("Compile verification report", "Aggregate the evidence", "Summarize results", "Final status report"):
        assert asyncio.run(sr.wrote_instructions_instead_of_doing_it(REPORT, {"title": title, "tool": "LLM"}, None)) is None, title
    import inspect
    src = inspect.getsource(sr.wrote_instructions_instead_of_doing_it)
    assert src.index("_REPORT_TITLE_RE.search(") < src.index("runbook_commands(text_value)")


def test_the_report_exemption_is_tight():
    for title in ("Give the media-stack containers working DNS", "Report the Jellyfin library count", "Verify endpoint responses",
                  "Restart backend service", "Fix UI data rendering", "Compile the kernel module"):
        hit = sr._REPORT_TITLE_RE.search(title)
        assert bool(hit) == (title == "Compile the kernel module"), title     # the one false friend is named, and known
