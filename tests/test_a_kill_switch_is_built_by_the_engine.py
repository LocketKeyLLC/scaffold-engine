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
    cmds, verify, _, _ = ks.block(T8, DRAFT)
    assert all(c.startswith("pct exec 109 -- ") for c in cmds)              # no CT 102, no pushed file
    assert "9696" not in " ".join(cmds) and verify == ["pct exec 109 -- iptables -S OUTPUT",
                                                    "pct exec 109 -- iptables -S SCAFFOLD-KS-FWD",
                                                    "pct exec 109 -- ip6tables -S OUTPUT"]
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
    cmds = ks.block(T8, DRAFT)[0]
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
    # §17.1670d — the local Docker bridges are open BEFORE the DROP (the host must reach its own published port)
    assert out.index("iptables -A OUTPUT -o br-+ -j ACCEPT") < out.index("iptables -P OUTPUT DROP")
    assert "iptables -A OUTPUT -o docker0 -j ACCEPT" in out

    et = tmp_path / "etc-iptables"
    save = _inner(cmds[4]).replace("/etc/wireguard/wg0.conf", str(conf)).replace("/etc/iptables", str(et))
    r = subprocess.run(["sh", "-c", save], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "SAVED for boot" in r.stdout, r
    v4f = (et / "rules.v4").read_text().splitlines()
    assert v4f[0] == "*filter" and v4f[-1] == "COMMIT" and ":OUTPUT DROP [0:0]" in v4f
    assert "-A OUTPUT -o eth0 -p udp --dport 1637 -j ACCEPT" in v4f and "-A SCAFFOLD-KS-FWD -o eth0 -j DROP" in v4f
    assert "-A OUTPUT -o br-+ -j ACCEPT" in v4f and "-A OUTPUT -o br-+ -j ACCEPT" in (et / "rules.v6").read_text()
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


T11 = {"node_key": "T11", "title": "Verify kill-switch blocks traffic", "tool": "Shell",
       "prompt_template": ("Bring wg0 down, attempt outbound connection, confirm blocked. Bring wg0 back up. Must pass "
                           "BEFORE bitmagnet starts.")}
T11_DRAFT = ["pct exec 109 -- wg-quick down wg0",
             "pct exec 109 -- timeout 10 curl -s -o /dev/null -w '%{http_code}' http://1.1.1.1",
             "pct exec 109 -- wg-quick up wg0"]


def test_the_proof_is_one_command_that_always_brings_the_tunnel_back():
    assert ks.block(T11, T11_DRAFT) is None
    cmds, verify, note, extra = ks.proof_block(T11, T11_DRAFT)
    assert extra["always_run"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 109 -- sh -c ")
    s = _inner(cmds[0])
    assert s.index("systemctl stop wg-quick@wg0") < s.index("curl") < s.index("systemctl start wg-quick@wg0")
    assert "§17.1670c" in note and ks.proof_block(T8, DRAFT) is None


def _proof_env(tmp_path, curl_ok: bool, comes_back: bool, goes_down: bool = True):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    st = tmp_path / "state"
    (b / "systemctl").write_text(f"#!/bin/sh\necho \"$1\" > {st}\n"); (b / "systemctl").chmod(0o755)
    (b / "ip").write_text(f"#!/bin/sh\n[ \"$(cat {st})\" = start ] || {'true' if not goes_down else 'false'}\n")
    (b / "ip").chmod(0o755)
    (b / "curl").write_text(f"#!/bin/sh\nexit {0 if curl_ok else 28}\n"); (b / "curl").chmod(0o755)
    hs = "1792400000" if comes_back else "0"
    (b / "wg").write_text(f"#!/bin/sh\nprintf 'peer\\t{hs}\\n'\n"); (b / "wg").chmod(0o755)
    (b / "sleep").write_text("#!/bin/sh\n"); (b / "sleep").chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}, st


def test_the_proof_passes_only_when_blocked_and_back(tmp_path):
    s = _inner(ks.proof_block(T11, T11_DRAFT)[0][0])
    for curl_ok, back, rc, says in ((False, True, 0, "BLOCKED"), (True, True, 1, "traffic got out"),
                                    (False, False, 1, "did not come back")):
        env, st = _proof_env(tmp_path, curl_ok, back)
        r = subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=env)
        assert r.returncode == rc and says in r.stdout, (curl_ok, back, r.stdout)
        assert st.read_text().strip() == "start"                  # the tunnel is always started again
    env, st = _proof_env(tmp_path, False, True, goes_down=False)
    r = subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "did not go down" in r.stdout


def test_the_frame_takes_the_proof():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = "## Run this\n\n```bash\n" + "\n".join(T11_DRAFT) + "\n```\n"
    frame = sr.frame_run(T11, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert len(frame["commands"]) == 1 and "systemctl start wg-quick@wg0" in frame["commands"][0]


def test_the_frame_states_what_a_whole_switch_shows():
    """§17.1670d — "already met" is judged against the full rule set, the Docker bridges included"""
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    frame = sr.frame_run(T8, "## Run this\n\n```bash\n" + "\n".join(DRAFT) + "\n```\n", spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert "-o br-+" in frame["expects"]["pct exec 109 -- iptables -S OUTPUT"]


def test_a_proof_is_never_already_met():
    """§17.1671d — the pre-read cannot show what the proof proves; already_met must stand aside without reading"""
    import asyncio
    from unittest.mock import patch
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    frame = sr.frame_run(T11, "## Run this\n\n```bash\n" + "\n".join(T11_DRAFT) + "\n```\n", spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert frame["always_run"]
    with patch.object(sr, "run_verify", side_effect=AssertionError("read the checks")):
        assert asyncio.run(sr.already_met(spec, T11, frame, {})) is None
