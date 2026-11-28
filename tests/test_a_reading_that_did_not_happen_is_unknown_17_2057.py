"""§17.2057 — the engine's reading of its own VM was false.

Live (laptop engine, 2026-11-27T21:26:21, the start of server redeploy RD82), guest 110 -- the server engine VM,
serving ~20 Docker services, no QEMU guest agent:

    machine_truth guest=110 {... 'transmits': False, 'address': 'fe80::be24:11ff:feb4:af15', 'agent': False,
                             'resolves': True, 'units': '0 units', ...}
    service_truth_read guest=110 services=[]

Four minutes later the address was its LAN address, everything else the same. Each field was a verdict drawn from
a read that did not say it:

* `address` -- `ip neigh` had no IPv4 entry for the MAC, and the §17.1496 fallback took the first line anyway: the
  link-local one.
* `resolves` / `units` / `services` -- the runner reports no error for a command that exits non-zero, so
  `qm guest exec 110 -- …` answering "No QEMU guest agent configured" came back as a successful read: non-empty
  (resolves), no unit lines (0 units), no listeners (no services). The in-guest reads were gated on the runner
  having run `qm agent 110 ping` (`agent_ping[0]`), not on the agent answering it.
* `transmits` -- `bridge fdb show` is aged (300 s) and its whole dump runs past the runner's 20 000-character cap.

Every guest id, MAC and address below is made up; the texts are the shapes the host printed.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.modules import machine_truth as mt
from app.modules import mcp_client
from app.modules import service_truth as st
from app.modules.measured import Reading

GID = "707"
MAC = "bc:24:11:5e:21:07"
CONFIG = ("agent: 0\nboot: order=scsi0\nmemory: 16384\nname: engine-vm\n"
          "net0: virtio=BC:24:11:5E:21:07,bridge=vmbr0,firewall=1\nscsi0: local-lvm:vm-707-disk-0,size=100G\n")
INV = {"cts": {"712": "running"}, "vms": {GID: "running"}, "names": {GID: "engine-vm"}, "disks": {}}

#: `ip neigh show` with ONLY the link-local entry for the VM's MAC (the RD82 shape), among other machines' entries
NEIGH_LINK_LOCAL_ONLY = (
    "10.66.0.1 dev vmbr0 lladdr 4c:00:00:0a:00:01 REACHABLE \n"
    "10.66.0.43 dev vmbr0 lladdr 24:00:00:0a:00:2b STALE \n"
    f"fe80::be24:11ff:fe5e:2107 dev vmbr0 lladdr {MAC} STALE \n"
    "fe80::4e00:ff:fe0a:1 dev vmbr0 lladdr 4c:00:00:0a:00:01 router STALE \n")
#: four minutes later: the IPv4 entry has appeared, AFTER the link-local one
NEIGH_WITH_V4 = NEIGH_LINK_LOCAL_ONLY + f"10.66.0.29 dev vmbr0 lladdr {MAC} REACHABLE \n"

NO_AGENT = "No QEMU guest agent configured\n"


def _envelope(out: str = "", code: int = 0, err: str = "") -> str:
    """`qm guest exec`'s own answer, in the layout the host prints it."""
    parts = [f'   "exitcode" : {code}', '   "exited" : 1']
    if err:
        parts.append('   "err-data" : ' + __import__("json").dumps(err))
    if out:
        parts.append('   "out-data" : ' + __import__("json").dumps(out))
    return "{\n" + ",\n".join(parts) + "\n}\n"


UNITS = ("cron.service                 enabled enabled\n"
         "docker.service               enabled enabled\n"
         "ssh.service                  enabled enabled\n")
SS = ('LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:* users:(("docker-proxy",pid=2211,fd=7))\n'
      'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=801,fd=3))\n')


def _runner(answers: dict, asked: list):
    """`call_tool` as the runner answers it: never an error flag -- a non-zero exit is just text."""
    async def call_tool(spec, tool, args):
        cmd = args["command"]
        asked.append(cmd)
        for key, text in answers.items():
            if cmd.startswith(key):
                return SimpleNamespace(is_error=False, text=text, structured=None)
        return SimpleNamespace(is_error=False, text="", structured=None)
    return call_tool


def _no_agent_host():
    """The RD82 host: no agent, so every `qm guest exec` answers with qm's own words."""
    return {f"qm config {GID}": CONFIG, "ip neigh show": NEIGH_LINK_LOCAL_ONLY,
            f"cat /sys/class/net/tap{GID}i0/statistics/rx_packets": "5120344\n",
            f"qm agent {GID} ping": NO_AGENT, f"qm guest exec {GID} --": NO_AGENT}


# ───────────────────────────── 1. the address


def test_no_ipv4_entry_is_an_unknown_address_not_the_link_local_one():
    t = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, neigh=NEIGH_LINK_LOCAL_ONLY)
    assert t.address is None, t.address
    assert "no IPv4 entry" in t.reads["ip neigh"] and "fe80::be24:11ff:fe5e:2107" in t.reads["ip neigh"]


def test_the_ipv4_entry_is_the_address_wherever_it_is_listed():
    t = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, neigh=NEIGH_WITH_V4)
    assert t.address == "10.66.0.29"
    other = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG.replace("5E:21:07", "5E:21:08"),
                                neigh=NEIGH_WITH_V4)
    assert other.address is None and other.reads["ip neigh"] == "(no entry for the MAC)"


@pytest.mark.asyncio
async def test_an_unknown_address_refuses_no_ssh_target():
    """The §17.1303 gate judges every ssh target against `truth.address`: with the link-local address it refuses the
    guest's real LAN address as "not VM 707's address". Unknown refuses nothing."""
    from app.modules.runbook_preconditions import unmet
    node = {"node_key": "RD1", "title": f"Redeploy the engine on VM {GID}", "description": ""}
    cmds = ["ssh -o BatchMode=yes ops@10.66.0.29 docker ps"]

    async def refused_for_address(t):
        return [r for r in await unmet(cmds, None, plan=[], node=node, inventory=INV, truth=t) if "is not VM" in r["why"]]

    blind = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, neigh=NEIGH_LINK_LOCAL_ONLY)
    seen = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, neigh=NEIGH_WITH_V4)
    assert blind.address is None and await refused_for_address(blind) == []
    assert seen.address == "10.66.0.29" and await refused_for_address(seen) == []
    blind.address = "fe80::be24:11ff:fe5e:2107"          # the value main measured from the same text
    assert await refused_for_address(blind), \
        "the gate bites on a wrong measured address -- which is why a link-local one must never be measured"


# ───────────────────────────── 2. reads through an agent that is not there


@pytest.mark.asyncio
async def test_a_vm_without_an_agent_is_not_read_through_it(monkeypatch):
    asked: list = []
    monkeypatch.setattr(mcp_client, "call_tool", _runner(_no_agent_host(), asked))
    t = await mt.read_guest_truth(object(), GID, INV, [])
    assert t.agent is False
    assert (t.resolves, t.units, t.tools) == (None, None, None), (t.resolves, t.units, t.tools)
    assert not any(c.startswith(f"qm guest exec {GID}") for c in asked), asked
    assert t.address is None and t.transmits is True


@pytest.mark.asyncio
async def test_a_guest_exec_that_qm_answers_itself_is_not_the_guest(monkeypatch):
    """Belt and braces: the ping said the agent answered, but each `qm guest exec` came back in qm's own words."""
    host = _no_agent_host() | {f"qm agent {GID} ping": ""}
    asked: list = []
    monkeypatch.setattr(mcp_client, "call_tool", _runner(host, asked))
    t = await mt.read_guest_truth(object(), GID, INV, [])
    assert t.agent is True and any(c.startswith(f"qm guest exec {GID}") for c in asked)
    assert (t.resolves, t.units, t.tools) == (None, None, None), (t.resolves, t.units, t.tools)


@pytest.mark.asyncio
async def test_a_vm_whose_agent_answers_is_read(monkeypatch):
    host = {f"qm config {GID}": CONFIG, "ip neigh show": NEIGH_WITH_V4,
            f"cat /sys/class/net/tap{GID}i0/statistics/rx_packets": "5120344\n",
            f"qm agent {GID} ping": "",
            f"qm guest exec {GID} -- timeout 5 getent": _envelope("151.101.2.132   deb.debian.org\n"),
            f"qm guest exec {GID} -- systemctl": _envelope(UNITS),
            f"qm guest exec {GID} -- which": _envelope("/usr/bin/python3\n/usr/bin/curl\n", code=1)}
    monkeypatch.setattr(mcp_client, "call_tool", _runner(host, []))
    t = await mt.read_guest_truth(object(), GID, INV, [])
    assert t.agent is True and t.resolves is True and t.units == ["cron", "docker", "ssh"]
    assert t.tools == {"jq": False, "python3": True, "curl": True, "wget": False}
    assert t.address == "10.66.0.29"


@pytest.mark.asyncio
async def test_a_failed_lookup_inside_the_vm_does_not_resolve(monkeypatch):
    """Through `qm guest exec` a failed `getent` is `(guest exit code 2)` -- non-empty, and main read it as resolving."""
    host = {f"qm config {GID}": CONFIG, f"qm agent {GID} ping": "",
            f"qm guest exec {GID} -- timeout 5 getent": _envelope(code=2),
            f"qm guest exec {GID} -- cat /proc/uptime": _envelope("86400.12 170000.55\n"),
            f"qm guest exec {GID} -- systemctl": _envelope(UNITS)}
    monkeypatch.setattr(mcp_client, "call_tool", _runner(host, []))
    t = await mt.read_guest_truth(object(), GID, INV, [])
    assert t.resolves is False and t.units == ["cron", "docker", "ssh"]


def test_a_unit_listing_with_no_unit_in_it_is_not_zero_units():
    for text in ("No QEMU guest agent configured", "\n(guest exit code 1)", "sh: 1: systemctl: not found"):
        t = mt.truth_from_texts(GID, inventory=INV, units_text=text)
        assert t.units is None, (text, t.units)
    assert mt.truth_from_texts(GID, inventory=INV, units_text=UNITS).units == ["cron", "docker", "ssh"]


@pytest.mark.asyncio
async def test_the_services_of_a_vm_without_an_agent_are_unread_not_none(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _runner(_no_agent_host(), []))
    r = Reading()
    out = await st.read_services(object(), GID, mentioned=["scaffold-orchestrator", "postgres"], reading=r, vm=True)
    assert out is None, out
    gap = next(g for g in r.gaps() if g["what"] == f"the services in guest {GID}")
    assert "No QEMU guest agent configured" in gap["why"], gap


@pytest.mark.asyncio
async def test_the_services_of_a_vm_whose_agent_answers_are_read(monkeypatch):
    show = ("Id=docker.service\nUser=\nGroup=\nFragmentPath=/lib/systemd/system/docker.service\n"
            "ActiveState=active\nLoadState=loaded\nWorkingDirectory=\n")
    host = {f"qm guest exec {GID} -- sh -c 'ss -tlnp'": _envelope(SS),
            f"qm guest exec {GID} -- hostname -I": _envelope("10.66.0.29 172.17.0.1 \n"),
            f"qm guest exec {GID} -- sh -c 'systemctl show": _envelope(show)}
    monkeypatch.setattr(mcp_client, "call_tool", _runner(host, []))
    out = await st.read_services(object(), GID, mentioned=["docker"], reading=Reading(), vm=True)
    assert out and out[0].unit == "docker.service" and out[0].address == "10.66.0.29"


@pytest.mark.asyncio
async def test_the_sweep_records_a_vm_without_an_agent_as_a_gap(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _runner(_no_agent_host(), []))
    r = Reading()
    found = await st.guests_of_the_named_services(object(), ["postgres"], {}, reading=r, vms={GID: "running"})
    assert found == {}
    assert any(g["what"] == f"guest {GID}" and "No QEMU guest agent configured" in g["why"] for g in r.gaps()), r.gaps()


# ───────────────────────────── 3. transmits


def test_transmits_is_the_guests_own_counter():
    assert mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, tx="5120344\n").transmits is True
    assert mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, tx="0\n").transmits is False
    missing = f"cat: /sys/class/net/tap{GID}i0/statistics/rx_packets: No such file or directory\n"
    assert mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, tx=missing).transmits is None
    assert mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG,
                               tx="(refused by the local runner: x)").transmits is None


@pytest.mark.asyncio
async def test_transmits_is_read_from_the_guests_host_port(monkeypatch):
    asked: list = []
    monkeypatch.setattr(mcp_client, "call_tool", _runner(_no_agent_host(), asked))
    await mt.read_guest_truth(object(), GID, INV, [])
    assert f"cat /sys/class/net/tap{GID}i0/statistics/rx_packets" in asked and "bridge fdb show" not in asked
    asked.clear()
    ct = {"pct config 712": "hostname: panel\nnet0: name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:5E:21:12,type=veth\n"}
    monkeypatch.setattr(mcp_client, "call_tool", _runner(ct, asked))
    await mt.read_guest_truth(object(), "712", INV, [])
    assert "cat /sys/class/net/veth712i0/statistics/rx_packets" in asked


def test_a_vm_that_sent_nothing_is_flagged_and_one_that_sent_is_not():
    node = {"node_key": "X1", "title": f"ssh into VM {GID} and check docker", "description": ""}
    silent = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, tx="0\n")
    sent = mt.truth_from_texts(GID, inventory=INV, qm_config=CONFIG, tx="5120344\n")
    rows = mt.contradictions(node, silent, {"reachable"}, [])
    row = next(r for r in rows if r["kind"] == "not_transmitting")
    assert f"tap{GID}i0" in row["evidence"] and "0 frames" in row["evidence"]
    assert not any(r["kind"] == "not_transmitting" for r in mt.contradictions(node, sent, {"reachable"}, []))


# ───────────────────────────── 4. a routine read is not a warning


@pytest.mark.asyncio
async def test_the_two_reading_lines_log_at_info(monkeypatch, caplog):
    monkeypatch.setattr(mcp_client, "call_tool", _runner(_no_agent_host(), []))
    with caplog.at_level(logging.INFO, logger="scaffold"):
        await mt.read_guest_truth(object(), GID, INV, [])
        await st.read_services(object(), GID, vm=True)
    lines = {r.getMessage().split(" ", 1)[0]: r.levelno for r in caplog.records
             if r.getMessage().startswith(("machine_truth guest=", "service_truth_read guest="))}
    assert lines == {"machine_truth": logging.INFO, "service_truth_read": logging.INFO}, lines
