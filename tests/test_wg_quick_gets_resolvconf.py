"""§17.1668 — a block that installs WireGuard and runs `wg-quick` installs `resolvconf` too
(live, job 3cc24343 T7: AirVPN's `DNS =` line would have failed "resolvconf: command not found")."""
from unittest.mock import MagicMock

from app.modules import supervised_runs as sr
from app.modules import wg_quick_resolvconf as wr

T7_CMDS = ["pct exec 109 -- apt-get update", "pct exec 109 -- apt-get install -y wireguard-tools",
           "pct exec 109 -- cp /root/airvpn-bitmagnet.conf /etc/wireguard/wg0.conf",
           "pct exec 109 -- chmod 600 /etc/wireguard/wg0.conf", "pct exec 109 -- systemctl enable wg-quick@wg0",
           "pct exec 109 -- systemctl start wg-quick@wg0"]


def test_the_live_block_gets_resolvconf_once():
    c, v, f, notes = wr.repair(T7_CMDS, ["pct exec 109 -- wg show wg0"], [])
    assert c[1] == "pct exec 109 -- apt-get install -y wireguard-tools resolvconf"
    assert [x for i, x in enumerate(c) if i != 1] == [x for i, x in enumerate(T7_CMDS) if i != 1]
    assert "§17.1668" in notes[0]["why"]


def test_in_a_script_file_too():
    body = "set -e\napt-get install -y wireguard\nwg-quick up wg0\n"
    _, _, f, notes = wr.repair(["bash /tmp/x.sh"], [], [{"path": "/tmp/x.sh", "content": body}])
    assert "apt-get install -y wireguard resolvconf\n" in f[0]["content"] and notes


def test_left_alone():
    with_it = T7_CMDS[:1] + ["pct exec 109 -- apt-get install -y wireguard-tools openresolv"] + T7_CMDS[2:]
    assert wr.repair(with_it, [], [])[3] == []
    assert wr.repair(["apt-get install -y wireguard-tools", "wg show"], [], [])[3] == []      # no wg-quick
    assert wr.repair(["systemctl start wg-quick@wg0"], [], [])[3] == []                       # nothing installed here


def test_the_frame_carries_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    node = {"node_key": "T7", "title": "Configure WireGuard wg0", "tool": "Shell",
            "prompt_template": "Install wireguard-tools in CT 109, set up wg0 from /root/airvpn-bitmagnet.conf."}
    draft = "## Run this\n\n```bash\n" + "\n".join(T7_CMDS) + "\n```\n\n## Verify\n\n```bash\npct exec 109 -- wg show wg0\n```\n"
    frame = sr.frame_run(node, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert any("wireguard-tools resolvconf" in c for c in frame["commands"]), frame["commands"]
