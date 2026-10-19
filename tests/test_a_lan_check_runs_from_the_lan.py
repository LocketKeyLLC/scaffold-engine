"""§17.1679 — a check the step says to make from the LAN runs on the host (live, job 3cc24343 T14: CT 109 curled its own
address)."""
from unittest.mock import MagicMock

from app.modules import lan_checks as lc
from app.modules import supervised_runs as sr

T14 = {"node_key": "T14", "title": "Verify bitmagnet web UI", "tool": "Shell",
       "prompt_template": "curl http://192.168.1.29:3333 from LAN. Confirm HTTP 200."}
LIVE = 'pct exec 109 -- bash -c "curl -s -o /dev/null -w \\"%{http_code}\\" http://192.168.1.29:3333"'


def test_the_live_check_moves_to_the_host():
    c, v, notes = lc.from_the_host(T14, [], [LIVE])
    assert v == ['curl -s -o /dev/null -w "%{http_code}" http://192.168.1.29:3333'] and "§17.1679" in notes[0]["why"]


def test_left_alone():
    loop = 'pct exec 109 -- bash -c "curl -s http://127.0.0.1:3333/"'
    assert lc.from_the_host(T14, [], [loop])[2] == []                                      # loopback: in the guest
    assert lc.from_the_host({**T14, "prompt_template": "curl it."}, [], [LIVE])[2] == []   # the step does not say LAN
    assert lc.from_the_host(T14, [], ['pct exec 109 -- bash -c "ss -tln"'])[2] == []       # not a curl
    chained = 'pct exec 109 -- bash -c "curl -s http://192.168.1.29:3333; rm -rf /x"'
    assert lc.from_the_host(T14, [], [chained])[2] == []                                   # one call only


def test_the_frame_carries_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = "## Run this\n\n```bash\npct exec 109 -- ss -tln\n```\n\n## Verify\n\n```bash\n" + LIVE + "\n```\n"
    frame = sr.frame_run(T14, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert any(v.startswith("curl ") and "192.168.1.29:3333" in v for v in frame["verify"]), frame["verify"]
