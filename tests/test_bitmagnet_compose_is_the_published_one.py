"""§17.1671 — "write the official bitmagnet compose" is bitmagnet's published file (live, job 3cc24343 T12: the model's
compose had no `command:`, no port 3334, no volumes)."""
import base64
import os
import re
import subprocess
from unittest.mock import MagicMock

from app.modules import bitmagnet_compose as bc
from app.modules import supervised_runs as sr

T12 = {"node_key": "T12", "title": "Create bitmagnet compose file", "tool": "Shell",
       "prompt_template": ("Starts from T5 and T11. Write official bitmagnet docker-compose.yml (bitmagnet + PostgreSQL) "
                           "with port 3333 mapped. Does NOT start containers.")}
FILE = {"path": "/tmp/in_ct_109.sh", "content": "mkdir -p /opt/bitmagnet\ntee /opt/bitmagnet/docker-compose.yml <<'EOF'\n"
                                                "services: {}\nEOF\n"}
DRAFT = ["bash /tmp/in_ct_109.sh start", "bash /tmp/in_ct_109.sh last"]


def test_the_published_file_has_what_the_draft_lacked():
    y = bc.OFFICIAL_COMPOSE
    for must in ("- worker", "- run", "--keys=http_server", "--keys=queue_server", "--keys=dht_crawler",
                 '"3333:3333"', '"3334:3334/tcp"', '"3334:3334/udp"', "./config:/root/.config/bitmagnet",
                 "./data/postgres:/var/lib/postgresql/data", "shm_size: 1g", "PGUSER=postgres", "postgres:16-alpine"):
        assert must in y, must


def test_the_target_comes_from_the_draft_when_the_text_names_no_guest():
    files = [{"path": "/tmp/in_ct_109.sh", "content": "GID=109\npct push \"$GID\" /tmp/x /root/x\n"}]
    assert bc.target(T12, ["bash /tmp/in_ct_109.sh start"], files) == ("109", "/opt/bitmagnet")   # the template's form
    assert bc.target(T12, ["bash /tmp/in_ct_109.sh start"], []) == ("109", "/opt/bitmagnet")
    assert bc.target({**T12, "prompt_template": "Write it to /srv/bm/docker-compose.yml in CT 120."}) == ("120", "/srv/bm")
    assert bc.block({**T12, "title": "Start bitmagnet stack"}, ["pct exec 109 -- true"]) is None
    assert bc.block(T12, []) is None


def test_the_block_writes_the_exact_file_and_starts_nothing(tmp_path):
    cmds, verify, note = bc.block({**T12, "prompt_template": f"Write it to {tmp_path}/bm/docker-compose.yml in CT 109."})
    assert not any(re.search(r"\bup\b|\bstart\b", c) for c in cmds) and "nothing is started" in note
    inner = subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"", cmds[0]],
                           capture_output=True, text=True).stdout
    (tmp_path / "bm").mkdir()
    (tmp_path / "bm" / "docker-compose.yml").write_text("old\n")
    r = subprocess.run(["sh", "-c", inner], capture_output=True, text=True)
    assert r.returncode == 0 and "WROTE" in r.stdout, r
    assert (tmp_path / "bm" / "docker-compose.yml").read_text() == bc.OFFICIAL_COMPOSE
    assert [p.read_text() for p in (tmp_path / "bm").glob("docker-compose.yml.bak.*")] == ["old\n"]
    assert (tmp_path / "bm" / "config").is_dir() and (tmp_path / "bm" / "data" / "postgres").is_dir()


def test_the_frame_takes_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    draft = ("## Write these files\n\n### /tmp/in_ct_109.sh\n```bash\nGID=109\npct push \"$GID\" /tmp/in_ct_109_remote.sh /root/x\n"
             "tee /opt/bitmagnet/docker-compose.yml <<'EOF'\nservices: {}\nEOF\n```\n\n## Run this\n\n```bash\n"
             + "\n".join(DRAFT) + "\n```\n")
    frame = sr.frame_run(T12, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert not frame.get("files") and base64.b64encode(bc.OFFICIAL_COMPOSE.encode()).decode() in frame["commands"][0]


T13 = {"node_key": "T13", "title": "Start bitmagnet stack", "tool": "Shell",
       "prompt_template": "docker compose up -d. Verify containers healthy and port 3333 listening."}


def test_the_start_is_judged_and_the_container_egress_is_read():
    cmds, verify, note = bc.start_block(T13, ["bash /tmp/in_ct_109.sh start"])
    assert [c.split(" -- ")[0] for c in cmds] == ["pct exec 109"] * 10
    assert "systemd-run --unit scaffold-bitmagnet-pull" in cmds[0] and "docker compose pull" in cmds[0]
    assert all("is-active" in c for c in cmds[1:7]) and "still running after every wait" in cmds[6]
    assert "docker compose up -d" in cmds[7] and "healthy" in cmds[8] and "docker exec bitmagnet" in cmds[9]
    assert verify[0].startswith("pct exec 109 -- curl") and bc.block(T13, ["bash /tmp/in_ct_109.sh start"]) is None
    for c in cmds:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True).returncode == 0, c


def test_the_frame_takes_the_start():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    draft = ("## Write these files\n\n### /tmp/in_ct_109.sh\n```bash\nGID=109\ncd /opt/bitmagnet && docker compose up -d\n"
             "```\n\n## Run this\n\n```bash\n" + "\n".join(DRAFT) + "\n```\n")
    frame = sr.frame_run(T13, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert any("docker compose up -d" in c for c in frame["commands"]) and frame["verify"]


def _egress_env(tmp_path, ct_ip, in_ip):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "curl").write_text(f"#!/bin/sh\necho {ct_ip}\n"); (b / "curl").chmod(0o755)
    (b / "docker").write_text(f"#!/bin/sh\n[ -n \"{in_ip}\" ] && echo {in_ip}\n"); (b / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}


def test_the_egress_check_both_ways(tmp_path):
    s = subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"",
                        bc.start_block(T13, ["bash /tmp/in_ct_109.sh start"])[0][9]], capture_output=True, text=True).stdout
    same = subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=_egress_env(tmp_path, "146.70.226.122", "146.70.226.122"))
    assert same.returncode == 0 and "THROUGH THE TUNNEL" in same.stdout
    leak = subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=_egress_env(tmp_path, "146.70.226.122", "67.240.32.243"))
    assert leak.returncode == 1 and "FAILED" in leak.stdout
    none = subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=_egress_env(tmp_path, "146.70.226.122", ""))
    assert none.returncode == 0 and "NOT TESTED" in none.stdout


def _pull_env(tmp_path, have: bool, active: bool):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "docker").write_text(f"#!/bin/sh\nexit {0 if have else 1}\n"); (b / "docker").chmod(0o755)
    (b / "systemctl").write_text(f"#!/bin/sh\nexit {0 if active else 3}\n"); (b / "systemctl").chmod(0o755)
    (b / "sleep").write_text("#!/bin/sh\n"); (b / "sleep").chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}"}


def test_the_waits_pass_hold_and_fail_as_they_should(tmp_path):
    cmds = bc.start_block(T13, ["bash /tmp/in_ct_109.sh start"])[0]
    inner = lambda c: subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"", c],
                                     capture_output=True, text=True).stdout
    mid, last = inner(cmds[1]), inner(cmds[6])
    run = lambda s, **k: subprocess.run(["sh", "-c", s], capture_output=True, text=True, env=_pull_env(tmp_path, **k))
    assert "PULLED" in run(mid, have=True, active=False).stdout
    r = run(mid, have=False, active=True); assert r.returncode == 0 and "STILL PULLING" in r.stdout
    r = run(last, have=False, active=True); assert r.returncode == 1 and "still running after every wait" in r.stdout
    r = run(mid, have=False, active=False); assert r.returncode == 1 and "ended without the images" in r.stdout


def test_the_wait_is_bounded_by_the_clock():
    """live: 75 × (2 s + a 3 s curl) outran the runner's 180 s call"""
    w = bc.start_block(T13, ["bash /tmp/in_ct_109.sh start"])[0][8]
    assert "end=$(( $(date +%s) + 150 ))" in w and "seq 1 75" not in w
