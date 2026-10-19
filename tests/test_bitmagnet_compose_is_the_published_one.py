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
    files = [{"path": "/tmp/in_ct_109.sh", "content": "GID=109\npct push 109 /tmp/x /root/x\n"}]
    assert bc.target(T12, ["pct exec 109 -- true"], files) == ("109", "/opt/bitmagnet")
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
    draft = ("## Write these files\n\n### /tmp/in_ct_109.sh\n```bash\nGID=109\npct push 109 /tmp/in_ct_109_remote.sh /root/x\n"
             "tee /opt/bitmagnet/docker-compose.yml <<'EOF'\nservices: {}\nEOF\n```\n\n## Run this\n\n```bash\n"
             + "\n".join(DRAFT) + "\n```\n")
    frame = sr.frame_run(T12, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert not frame.get("files") and base64.b64encode(bc.OFFICIAL_COMPOSE.encode()).decode() in frame["commands"][0]
