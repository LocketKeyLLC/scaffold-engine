"""§17.1690 — the pause asks a guest which tools it has; a block that pipes into one it lacks is refused."""
import asyncio

from app.modules import guest_tools as gt
from app.modules.machine_truth import truth_from_texts, GuestTruth
from app.modules.runbook_preconditions import unmet

# live: `pct exec 104 -- which jq python3 curl wget` (runner read channel, 2026-10-20)
WHICH_104 = "/bin/python3\n/bin/curl\n/bin/wget\n"

# live, job e5e22b92 T2 "Read full Sonarr queue": the template's in-guest body
T2_FILE = {"path": "/tmp/in_ct_104.sh", "content": (
    "#!/usr/bin/env bash\nGID=104\ncat > /tmp/in_ct_104_remote.sh <<'REMOTE'\n"
    "set +e; trap 'exit 0' EXIT  # §17.1608: this block only reads\n"
    "export DEBIAN_FRONTEND=noninteractive\n"
    "curl -H \"X-Api-Key: $(cat /config/config.xml | sed -n \"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p\")\" -s "
    "\"http://localhost:8989/api/v3/queue?page=1&pageSize=100\" | jq -r '.totalRecords, .records[] | [.id, .title] | @tsv'\n"
    "REMOTE\n")}
T2_CMDS = ["bash /tmp/in_ct_104.sh start", "bash /tmp/in_ct_104.sh last"]
TOOLS_104 = {"jq": False, "python3": True, "curl": True, "wget": True}


def test_which_is_read_into_tools():
    assert gt.parse_which(WHICH_104, False) == TOOLS_104       # `which` exits 1 when one is missing: still an answer
    assert gt.parse_which("", False) is None                  # blind, not "everything missing"
    assert gt.parse_which(WHICH_104, None) is None             # the runner could not be asked


def test_the_truth_carries_the_tools_and_shows_the_read():
    t = truth_from_texts("104", inventory={"cts": {"104": "running"}}, tools_read=(False, WHICH_104))
    assert t.tools == TOOLS_104
    assert "jq MISSING" in t.reads["which"]
    assert truth_from_texts("104", inventory={"cts": {"104": "running"}}).tools is None


def test_the_live_draft_is_refused_with_python3_named():
    hit = gt.calls_a_missing_tool(T2_CMDS, [T2_FILE], "104", TOOLS_104)
    assert len(hit) == 1 and "jq -r" in hit[0]["command"]
    assert "python3" in hit[0]["why"] and "not in guest 104" in hit[0]["why"]


def test_a_wrapped_guest_pipe_is_judged_and_a_host_pipe_is_not():
    inside = ["pct exec 104 -- sh -c 'curl -s http://localhost:8989/api/v3/queue | jq .totalRecords'"]
    outside = ["pct exec 104 -- curl -s http://localhost:8989/api/v3/queue | jq .totalRecords"]
    assert gt.calls_a_missing_tool(inside, [], "104", TOOLS_104)
    assert gt.calls_a_missing_tool(outside, [], "104", TOOLS_104) == []
    assert gt.calls_a_missing_tool(inside, [], "105", TOOLS_104) == []


def test_correct_misses():
    py = dict(T2_FILE, content=T2_FILE["content"].replace("jq -r '.totalRecords, .records[] | [.id, .title] | @tsv'",
                                                          "python3 -c 'import sys,json;print(json.load(sys.stdin)[\"totalRecords\"])'"))
    assert gt.calls_a_missing_tool(T2_CMDS, [py], "104", TOOLS_104) == []
    installs = dict(T2_FILE, content=T2_FILE["content"].replace("export DEBIAN", "apt-get install -y jq\nexport DEBIAN"))
    assert gt.calls_a_missing_tool(T2_CMDS, [installs], "104", TOOLS_104) == []
    assert gt.calls_a_missing_tool(T2_CMDS, [T2_FILE], "104", dict(TOOLS_104, jq=True)) == []
    assert gt.calls_a_missing_tool(T2_CMDS, [T2_FILE], "104", None) == []
    # "jq" as a word in a URL or a name is not a call
    assert gt.calls_a_missing_tool(["pct exec 104 -- sh -c 'curl -s http://x/jq-docs'"], [], "104", TOOLS_104) == []


def test_unmet_raises_it_from_the_measured_truth():
    truth = GuestTruth(gid="104", kind="ct", status="running", tools=TOOLS_104)
    node = {"title": "Read full Sonarr queue", "description": "Read-only."}
    out = asyncio.run(unmet(T2_CMDS, None, files=[T2_FILE], node=node,
                            inventory={"cts": {"104": "running"}, "vms": {}}, truth=truth))
    assert any("not in guest 104" in r["why"] for r in out), out
