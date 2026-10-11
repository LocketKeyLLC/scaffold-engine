"""§17.1457 — a block may change what its step is about, and nothing it would lose on the way.

Live (ADD151, 2026-10-10; fixture: the drafted block): `… | tee /etc/pve/firewall/cluster.fw` with only a
`[group dmz]` section, `pct set N -net0 name=eth0,bridge=vmbr0,firewall=1,ip=dhcp` for every container (each
fixed address, gateway and MAC dropped), and `qm set 106 -net0 virtio=BC:24:11:00:00:06,…` (a made-up MAC).
The machine values below are what the host answered that evening.
"""
import asyncio
import json
import pathlib

import pytest

from app.modules import rewrite_guard as rg

BLOCK = json.loads(pathlib.Path("tests/fixtures/add151_block_2026_10_10.json").read_text())
CLUSTER_FW = """[OPTIONS]
enable: 1
policy_in: DROP
[group management]
IN ACCEPT -p tcp -dport 22 -source 192.168.1.0/24
IN ACCEPT -p tcp -dport 8006 -source 192.168.1.0/24
IN DROP
[group dmz]
IN ACCEPT -p tcp -dport 80 -source 192.168.1.0/24
IN ACCEPT -p tcp -dport 443 -source 192.168.1.0/24
IN DROP
"""
CONF = {
    "pct config 101": "hostname: jellyfin\nnet0: name=eth0,bridge=vmbr0,gw=192.168.1.1,hwaddr=BC:24:11:CD:66:98,ip=192.168.1.20/24,type=veth\n",
    "pct config 120": "net0: name=eth0,bridge=vmbr0,firewall=0,hwaddr=BC:24:11:AC:C9:06,ip=dhcp,type=veth\n",
    "qm config 106": "name: palworld-server\nnet0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0\n",
    "cat /etc/pve/firewall/cluster.fw": CLUSTER_FW,
    "cat /etc/pve/firewall/101.fw": "[OPTIONS]\nenable: 1\npolicy_in: DROP\n[RULES]\nGROUP media\n",
}


@pytest.fixture
def machine(monkeypatch):
    async def _probe(spec, command):
        return (True, CONF[command]) if command in CONF else (False, "")
    import app.modules.service_truth as st
    monkeypatch.setattr(st, "_probe", _probe)


def _why(cmds, files=None):
    return asyncio.run(rg.destructive_rewrites(cmds, object(), files))


def test_the_live_block_is_refused_where_it_would_lose_things(machine):
    out = _why(BLOCK)
    bad = {o["command"] for o in out}
    assert any("tee /etc/pve/firewall/cluster.fw" in c for c in bad)
    assert any(c.startswith("pct set 101 -net0") for c in bad)
    assert any(c.startswith("pct set 120 -net0") for c in bad)
    assert any(c.startswith("qm set 106 -net0") for c in bad)
    assert not any("101.fw" in c for c in bad), "the per-guest file keeps every line it had"
    why101 = next(o["why"] for o in out if o["command"].startswith("pct set 101"))
    assert "hwaddr" in why101 and "ip" in why101 and "gw" in why101 and rg.NET0_MARKER in why101


def test_only_the_firewall_flag_changing_is_allowed(machine):
    ok = ["pct set 101 -net0 name=eth0,bridge=vmbr0,firewall=1,gw=192.168.1.1,hwaddr=BC:24:11:CD:66:98,ip=192.168.1.20/24,type=veth",
          "qm set 106 --net0 virtio=BC:24:11:E8:9F:7A,bridge=vmbr0,firewall=1"]
    assert _why(ok) == []


def test_an_edited_line_is_not_a_lost_one(machine):
    full = ("printf '%s\\n' '[OPTIONS]' 'enable: 1' 'policy_in: DROP' '[group management]' "
            "'IN ACCEPT -p tcp -dport 22 -source 192.168.1.0/24' 'IN ACCEPT -p tcp -dport 8006 -source 192.168.1.0/24' "
            "'IN DROP' '[group dmz]' 'IN ACCEPT -p tcp -dport 80' 'IN ACCEPT -p tcp -dport 443' 'IN DROP' "
            "| tee /etc/pve/firewall/cluster.fw")
    assert _why([full]) == []


def test_a_written_file_that_drops_lines_is_refused(machine):
    out = _why([], files=[{"path": "/etc/pve/firewall/cluster.fw", "content": "[group dmz]\nIN ACCEPT -p tcp -dport 80\n"}])
    assert out and rg.FILE_MARKER in out[0]["why"]


def test_blindness_refuses_nothing(monkeypatch):
    async def _probe(spec, command):
        return (None, "")
    import app.modules.service_truth as st
    monkeypatch.setattr(st, "_probe", _probe)
    assert _why(BLOCK) == []


def test_it_is_wired_at_both_ends_and_registered():
    pre = pathlib.Path("app/modules/runbook_preconditions.py").read_text()
    run = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert "out.extend(await destructive_rewrites(commands or [], spec, files))" in pre
    assert "_lost = await destructive_rewrites(runnable, spec" in run
    from app.modules import supervised_runs as sr
    for m in (rg.NET0_MARKER, rg.FILE_MARKER):
        assert m in sr._SHAPE_REFUSALS and sr._WHOSE_GAP[m] == "drafter"


# §17.1460 — the third live ADD151 block: host-side heredoc writes, and a cluster.fw whose content was a note.
BLOCK3 = json.loads(pathlib.Path("tests/fixtures/add151_block3_2026_10_11.json").read_text())
CONF.update({
    "pct config 102": "net0: name=eth0,bridge=vmbr0,gw=192.168.1.1,hwaddr=BC:24:11:D1:0B:06,ip=192.168.1.21/24,type=veth\n",
    "qm config 110": "net0: virtio=BC:24:11:B4:AF:15,bridge=vmbr0\n",
    "cat /etc/pve/firewall/120.fw": "[OPTIONS]\nenable: 1\n[RULES]\nGROUP dmz\n",
})


def test_the_note_in_cluster_fw_is_refused_and_the_rest_passes(machine):
    out = _why(BLOCK3)
    bad = [o for o in out if "cluster.fw" in o["command"]]
    assert bad, "the cluster.fw write carrying <PASTE THE FULL cluster.fw HERE…> must be refused"
    assert any(rg.NOTE_MARKER in o["why"] for o in bad) and any(rg.FILE_MARKER in o["why"] for o in bad)
    assert not [o for o in out if "101.fw" in o["command"] or "120.fw" in o["command"]], \
        "a per-guest file that keeps every line it had is fine"
    assert not [o for o in out if o["command"].startswith(("pct set", "qm set"))], "every net0 now matches the machine"


def test_a_placeholder_name_is_not_a_note():
    assert rg.drafter_notes(["ssh aedefruscio@<HOST_IP> nvidia-smi", "curl http://<CURRENT_IP>:3001/"]) == []
    assert rg.drafter_notes(["cat > /etc/x <<'EOF'\n<INSERT THE EXISTING LINES>\nEOF"])


def test_the_note_marker_is_registered():
    from app.modules import supervised_runs as sr
    assert rg.NOTE_MARKER in sr._SHAPE_REFUSALS and sr._WHOSE_GAP[rg.NOTE_MARKER] == "drafter"


def test_a_new_file_has_nothing_to_lose(monkeypatch):
    """§17.1461 — live, every NEW guest .fw was refused: the `cat` error was read as its content."""
    async def _probe(spec, command):
        if command.startswith("cat /etc/pve/firewall/102.fw"):
            return True, "cat: /etc/pve/firewall/102.fw: No such file or directory"
        return False, ""
    import app.modules.service_truth as st
    monkeypatch.setattr(st, "_probe", _probe)
    cmd = "cat > /etc/pve/firewall/102.fw <<\"EOF\"\n[OPTIONS]\nenable: 1\nEOF"
    assert _why([cmd]) == []
