"""§17.1647 — an *arr test call sends the whole object (job 5db1b088 T10, 2026-10-18).

`POST /api/v3/downloadclient/test` with `{"id": 1}` answered 400 ("'Implementation' must not be empty") and T10 failed as
if qBittorrent were unreachable."""
from __future__ import annotations

import subprocess

from app.modules.arr_test_calls import send_the_whole_object

LIVE = ("pct exec 103 -- sh -c 'curl -s --fail-with-body -X POST -H \"X-Api-Key: $(cat /var/lib/radarr/config.xml "
        "/config/config.xml 2>/dev/null | sed -n \"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p\" | head -n 1)\" -H \"Content-Type: "
        "application/json\" -d \"{\\\"id\\\": 1}\" http://127.0.0.1:7878/api/v3/downloadclient/test'")


def test_the_live_call_gets_the_object_and_posts_it_back():
    (cmd,), _, notes = send_the_whole_object([LIVE], [])
    payload = cmd.split("sh -c ", 1)[1]
    get, post = payload.split(" | curl", 1)
    assert "http://127.0.0.1:7878/api/v3/downloadclient/1" in get and get.count("X-Api-Key: $(cat /var/lib/radarr") == 1
    assert "-X POST" in post and "-d @-" in post and post.rstrip("'").endswith("/api/v3/downloadclient/test")
    assert '{\\"id\\": 1}' not in cmd and notes
    assert subprocess.run(["bash", "-n", "-c", cmd], capture_output=True).returncode == 0


def test_other_calls_are_left_alone():
    same = ["pct exec 103 -- curl -s http://127.0.0.1:7878/api/v3/downloadclient/1",
            "pct exec 103 -- sh -c 'curl -s -X POST -H \"X-Api-Key: k\" -d \"{\\\"id\\\": 1}\" http://127.0.0.1:7878/api/v3/command'",
            "pct exec 102 -- sh -c 'curl -s -X POST -H \"X-Api-Key: k\" -d @/tmp/x.json http://127.0.0.1:9696/api/v1/indexer/test'"]
    out, _, notes = send_the_whole_object(same, [])
    assert out == same and notes == []


def test_a_single_quoted_body_and_a_verify_line_too():
    v = "pct exec 104 -- sh -c \"curl -s -X POST -H 'X-Api-Key: k' -d '{\\\"id\\\":3}' http://127.0.0.1:8989/api/v3/indexer/test\""
    line = "curl -s -X POST -H 'X-Api-Key: k' -d '{\"id\":3}' http://127.0.0.1:8989/api/v3/indexer/test"
    _, (fixed,), _ = send_the_whole_object([], [line])
    assert fixed.startswith("curl -s --fail-with-body -H 'X-Api-Key: k' http://127.0.0.1:8989/api/v3/indexer/3 | curl")
    assert v  # the escaped-in-double-quotes spelling is left to the draft (no false rewrite)


# ── §17.1648: `pct exec … | tee /tmp/x` writes on the HOST ──────────────────

def test_a_file_teed_on_the_host_and_read_in_the_guest_is_refused():
    """Live (T10's redraft): the GET was teed to /tmp/radarr_dc.json OUTSIDE the `sh -c '…'` quotes (the host's
    shell runs `tee`), and the POST read `-d @/tmp/radarr_dc.json` INSIDE guest 103, where no such file exists."""
    from app.modules.service_truth import a_file_read_on_another_machine
    cmds = ["pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: k\" http://127.0.0.1:7878/api/v3/downloadclient/1' | tee /tmp/radarr_dc.json",
            "pct exec 103 -- sh -c 'curl -s -X POST -H \"X-Api-Key: k\" -d @/tmp/radarr_dc.json http://127.0.0.1:7878/api/v3/downloadclient/test'"]
    assert a_file_read_on_another_machine(cmds, [])
    inside = ["pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/api/v3/downloadclient/1 | tee /tmp/radarr_dc.json'",
              cmds[1]]
    assert a_file_read_on_another_machine(inside, []) == []          # teed inside the guest: same machine
