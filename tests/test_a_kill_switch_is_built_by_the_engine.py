"""§17.1670 — "configure the VPN kill-switch" is the engine's block: CT 105's measured shape plus Docker's FORWARD
(live, job 3cc24343 T8: an unseen nftables file, an asked-for address, and T15's Prowlarr call bundled in)."""
import os
import subprocess
from unittest.mock import MagicMock

from app.modules import kill_switch as ks
from app.modules import supervised_runs as sr

T8 = {"node_key": "T8", "title": "Configure nftables kill-switch", "tool": "Shell",
      "prompt_template": ("Starts from T7. Mirror CT 105 pattern: OUTPUT dropped except lo, wg0, established/related, "
                          "LAN 192.168.1.0/24, and WireGuard endpoint UDP port. Persist rules.")}
DRAFT = ["pct status 109", "pct exec 109 -- hostname -I", "pct exec 109 -- apt-get install -y nftables",
         "pct push 109 /tmp/bitmagnet-nftables.conf /etc/nftables.conf", "pct exec 109 -- nft -f /etc/nftables.conf",
         "pct exec 102 -- sh -c 'curl -s -X POST http://127.0.0.1:9696/api/v1/indexer'"]


def test_the_guest_is_the_one_protected_not_the_pattern():
    assert ks.guarded(T8, DRAFT) == ("109", "wg0", "192.168.1.0/24")       # "Mirror CT 105" names the pattern
    assert ks.guarded({**T8, "prompt_template": "Kill-switch for wg1 in CT 120."}, DRAFT) == ("120", "wg1", "")


def test_left_alone():
    assert ks.block({**T8, "title": "Verify kill-switch blocks traffic"}, DRAFT) is None   # T11 is a check
    assert ks.block({**T8, "title": "Configure WireGuard wg0"}, DRAFT) is None
    assert ks.block(T8, []) is None and ks.block({**T8, "prompt_template": "x"}, []) is None


def test_nothing_else_rides_along():
    cmds, verify, _ = ks.block(T8, DRAFT)
    assert all(c.startswith("pct exec 109 -- ") for c in cmds)              # no CT 102, no pushed file
    assert "9696" not in " ".join(cmds) and verify == ["pct exec 109 -- nft list ruleset"]
    for c in cmds:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True).returncode == 0, c


def test_the_frame_takes_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = "## Run this\n\n```bash\n" + "\n".join(DRAFT) + "\n```\n"
    frame = sr.frame_run(T8, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running", "102": "running"},
                                    "names": {"109": "bitmagnet", "102": "prowlarr"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert not frame.get("files") and "9696" not in " ".join(frame["commands"])
    assert any("SCAFFOLD-KS-FWD" in c for c in frame["commands"])


def _inner(cmd):
    return subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"", cmd],
                          capture_output=True, text=True).stdout


def _env(tmp_path):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    log = tmp_path / "calls"
    for t in ("iptables", "ip6tables"):
        (b / t).write_text(f"#!/bin/sh\necho \"{t} $*\" >> {log}\n"
                           "case \"$1\" in -C) exit 1;; -N) exit 0;; esac\n"); (b / t).chmod(0o755)
    for t in ("iptables-restore", "ip6tables-restore"):
        (b / t).write_text(f"#!/bin/sh\ncat > /dev/null; echo \"{t} $*\" >> {log}\n"); (b / t).chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}, log


def test_the_rules_go_in_allow_first_then_drop_and_the_boot_files_match(tmp_path):
    env, log = _env(tmp_path)
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\nAddress = 10.0.0.2/32\n\n[Peer]\nEndpoint = ch3.vpn.airdns.org:1637\n")
    cmds, _, _ = ks.block(T8, DRAFT)
    v4 = _inner(cmds[2]).replace("/etc/wireguard/wg0.conf", str(conf))
    r = subprocess.run(["sh", "-c", v4], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "udp/1637" in r.stdout, r
    calls = log.read_text().splitlines()
    out = [c for c in calls if " OUTPUT" in c]
    assert out[0] == "iptables -P OUTPUT ACCEPT" and out[-1] == "iptables -P OUTPUT DROP"     # never locked mid-way
    assert "iptables -A OUTPUT -o eth0 -d 192.168.1.0/24 -j ACCEPT" in out
    assert "iptables -A OUTPUT -o eth0 -p udp --dport 1637 -j ACCEPT" in out
    assert "iptables -A SCAFFOLD-KS-FWD -o eth0 -j DROP" in calls
    assert "iptables -I DOCKER-USER 1 -j SCAFFOLD-KS-FWD" in calls

    et = tmp_path / "etc-iptables"
    save = _inner(cmds[4]).replace("/etc/wireguard/wg0.conf", str(conf)).replace("/etc/iptables", str(et))
    r = subprocess.run(["sh", "-c", save], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "SAVED for boot" in r.stdout, r
    v4f = (et / "rules.v4").read_text().splitlines()
    assert v4f[0] == "*filter" and v4f[-1] == "COMMIT" and ":OUTPUT DROP [0:0]" in v4f
    assert "-A OUTPUT -o eth0 -p udp --dport 1637 -j ACCEPT" in v4f and "-A SCAFFOLD-KS-FWD -o eth0 -j DROP" in v4f
    assert ":FORWARD DROP [0:0]" in (et / "rules.v6").read_text().splitlines()


def test_no_endpoint_port_stops_before_any_rule(tmp_path):
    env, log = _env(tmp_path)
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\nAddress = 10.0.0.2/32\n")
    v4 = _inner(ks.block(T8, DRAFT)[0][2]).replace("/etc/wireguard/wg0.conf", str(conf))
    r = subprocess.run(["sh", "-c", v4], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "no Endpoint port" in r.stdout and not log.exists()


def test_a_second_run_adds_nothing_and_nothing_is_flushed(tmp_path):
    env, log = _env(tmp_path)
    b = tmp_path / "bin"
    (b / "iptables").write_text(f"#!/bin/sh\necho \"iptables $*\" >> {log}\nexit 0\n")      # every rule already there
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Peer]\nEndpoint = 1.2.3.4:1637\n")
    cmds = ks.block(T8, DRAFT)[0]
    assert not any(" -F" in c for c in cmds)
    v4 = _inner(cmds[2]).replace("/etc/wireguard/wg0.conf", str(conf))
    assert subprocess.run(["sh", "-c", v4], capture_output=True, text=True, env=env).returncode == 0
    calls = log.read_text().splitlines()
    assert not [c for c in calls if " -A " in c or " -I " in c]
