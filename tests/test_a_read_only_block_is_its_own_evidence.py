"""§17.1680 — a template block the engine marked read-only is its own evidence (live, job c2518102 T2 "Fetch Sonarr wanted
missing": one read of Sonarr's missing list was refused "this block has no check at all")."""
from unittest.mock import MagicMock

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

READ = ('curl -s -H "X-Api-Key: $(sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" /var/lib/sonarr/config.xml)" '
        '"http://127.0.0.1:8989/api/v3/wanted/missing?page=1&pageSize=100" | jq ".totalRecords"')
WRITE = "sed -i 's/^Port=.*/Port=9999/' /var/lib/sonarr/config.xml"


def _frame(remote, title):
    vals = {"GID": "104", "STEP": "T2", "REMOTE_COMMANDS": remote, "VERIFY_INSIDE": "", "GUEST_USER_NAME": "GUEST_USER"}
    vals["STRICT"] = rt.strict_line(remote)
    rb = rt.render(rt.RUN_IN_CONTAINER, vals)
    rb = rb.replace('## Verify\n\n```bash\npct exec 104 -- bash -c ""\n```\n', "")
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    return sr.frame_run({"node_key": "T2", "title": title, "tool": "Shell", "prompt_template": "Read only."}, rb, spec,
                        policy, services=[], inventory={"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}})


def test_a_read_only_block_needs_no_separate_check():
    assert sr.READS_ONLY_MARKER in rt.strict_line(READ)
    f = _frame(READ, "Fetch Sonarr wanted missing")
    assert not [r for r in f["refused"] if "no check at all" in r["why"]], f["refused"]


def test_a_block_that_writes_still_needs_one():
    assert sr.READS_ONLY_MARKER not in rt.strict_line(WRITE)
    f = _frame(WRITE, "Change Sonarr's port")
    assert [r for r in f["refused"] if "no check at all" in r["why"]], f["refused"]


# --- §17.1680b: after the run, the block's own output is judged ---------------------------------------------------

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

SCRIPT = {"path": "/tmp/in_ct_104.sh",
          "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\nset +e; trap 'exit 0' EXIT  # " + sr.READS_ONLY_MARKER
                     + " -- a failed read is part of the answer\ncurl … | jq …\nREMOTE\n"}


async def _resolve(output):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmd = "bash /tmp/in_ct_104.sh start"
    waiting = {"kind": "run", "node_key": "T2", "title": "Fetch Sonarr wanted missing", "description": "Read only.",
               "prompt_template": "curl GET Sonarr wanted/missing. Read-only.", "runbook": "## Run this",
               "commands": [cmd], "verify": [], "refused": [], "files": [SCRIPT]}
    seen = []

    async def judge(claim_text, cmds, pasted, expects=None):      # decides from what it is HANDED
        seen.append(pasted)
        bad = "request failed" in pasted or "(it printed nothing)" in pasted
        return [{"id": "V1", "verdict": "contradicted" if bad else "confirmed",
                 "reason": "the read failed" if bad else "the list came back"}]
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=([cmd], [])), \
         patch("app.modules.assist_supervised.run_block",
               new=AsyncMock(return_value=[{"command": cmd, "ok": True, "exit": 0, "output": output}])), \
         patch("app.modules.assist_supervised.write_files_on",
               new=AsyncMock(return_value=[{"command": "write /tmp/in_ct_104.sh", "ok": True, "exit": 0,
                                            "output": "wrote /tmp/in_ct_104.sh"}])), \
         patch.object(sr, "_verify_verdicts", side_effect=judge), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "j", "T2", "run", waiting), seen


@pytest.mark.asyncio
async def test_a_read_that_failed_is_not_a_done_step():
    out, seen = await _resolve("Sonarr API request failed\nscaffold-T2 finished (exit 0)")
    assert out["node_status"] == "failed", out.get("reason")
    assert seen and "Sonarr API request failed" in seen[0]                 # the judge read the block's own output


@pytest.mark.asyncio
async def test_a_read_that_answered_is_done():
    out, _ = await _resolve("Show A\t1\t3\tPilot\t2026-01-01\ttrue\nscaffold-T2 finished (exit 0)")
    assert out["node_status"] == "done", out.get("reason")


def test_a_call_to_an_api_path_is_not_a_route_inventory():
    """§17.1682 — live, "read Sonarr's /api/v3/wanted/missing" became an inventory of Sonarr's code"""
    t = ("Inside CT 104, read Sonarr's /api/v3/wanted/missing (includeSeries=true). Print one line per series and "
         "season. Read only, change nothing, one endpoint only.")
    assert not sr._ROUTE_INVENTORY_RE.search(t)
    for still in ("Inventory the control panel's endpoints", "Read the backend's routes and handlers",
                  "List every API the panel serves", "enumerate the routes of the panel"):
        assert sr._ROUTE_INVENTORY_RE.search(still), still


def test_a_read_only_step_is_never_a_search():
    """§17.1683 — live, "Fetch Sonarr wanted missing" (Read only, change nothing) became SeriesSearch POSTs"""
    node = {"node_key": "T2", "title": "Fetch Sonarr wanted missing",
            "prompt_template": ("Inside CT 104, read Sonarr's /api/v3/wanted/missing. Print one line per series and season: "
                                "missing episodes. Read only, change nothing.")}
    inv = {"cts": {"104": "running"}, "vms": {}, "names": {"104": "sonarr"}}
    from app.modules.service_truth import ServiceTruth
    son = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", argv="/opt/Sonarr/Sonarr", ports=("8989",))
    c, v, notes = sr.rewrite_queue_missing(node, ["x"], [], [son], inv)
    assert notes == [] and c == ["x"]
    search = {**node, "title": "Search the missing episodes for every season",
              "prompt_template": "Queue a search for the missing episodes of each season."}
    assert sr.rewrite_queue_missing(search, ["x"], [], [son], inv)[2]      # a real search step still is one


def test_the_template_trust_ends_when_its_commands_are_replaced():
    node = {"title": "Fetch Sonarr wanted missing", "prompt_template": "Read only, change nothing."}
    rb = "<!-- runbook-template: run_in_container -->\n## Run commands inside container 104\n"
    assert sr.writes_in_a_read_only_step(["bash /tmp/in_ct_104.sh start", "bash /tmp/in_ct_104.sh last"], node, rb) == []
    post = ("pct exec 104 -- sh -c 'python3 -c \"api(\\\"/api/v3/command\\\",\\\"POST\\\",{})\"'")
    assert sr.writes_in_a_read_only_step([post], node, rb)


def _block(prog, extra=""):
    return [{"path": "/tmp/in_ct_104.sh", "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\nset -e\n"
             "export DEBIAN_FRONTEND=noninteractive\n" + extra + "python3 - <<'EOF'\n" + prog + "\nEOF\nREMOTE\n"}]


READ_PROG = ("import json,urllib.request\nreq=urllib.request.Request('http://localhost:8989/api/v3/wanted/missing',"
             "headers={'X-Api-Key':k})\ndata=json.loads(urllib.request.urlopen(req).read())\nprint(len(data['records']))")
RO = {"title": "Fetch Sonarr wanted missing", "prompt_template": "Read only, change nothing."}


def test_a_read_only_program_is_recognised_and_a_writing_one_is_not():
    """§17.1680c — live, T2's all-pages python read was refused "no check at all\""""
    assert sr.a_read_only_program(RO, _block(READ_PROG))
    for w in ("urllib.request.urlopen(urllib.request.Request(u, data=b'{}', method='POST'))",
              "open('/var/lib/sonarr/x','w').write('1')", "import subprocess; subprocess.run(['rm','x'])",
              "import os; os.remove('/x')", "c.execute('DELETE FROM Episodes')"):
        assert not sr.a_read_only_program(RO, _block(READ_PROG + "\n" + w)), w
    assert not sr.a_read_only_program(RO, _block(READ_PROG, extra="systemctl restart sonarr\n"))   # shell work beside it
    assert not sr.a_read_only_program({"title": "Fetch Sonarr wanted missing"}, _block(READ_PROG))  # step never said so


def test_reading_a_packaged_apps_api_is_not_a_route_inventory():
    """§17.1682b — live, T6 "read qBittorrent's torrent list from its local Web API" became an inventory of /opt/qbittorrent*"""
    inv = {"cts": {"105": "running", "111": "running"}, "vms": {}, "names": {"105": "download-client", "111": "control-panel"}}
    t6 = {"title": "Fetch qBittorrent torrent status",
          "prompt_template": "Inside CT 105, read qBittorrent's torrent list from its local Web API (/api/v2/torrents/info)."}
    assert sr.rewrite_inventory_service_routes(t6, ["x"], [], [], inv)[2] == []
    panel = {"title": "Read the control panel backend's routes", "prompt_template": "Read the panel's routes in CT 111."}
    assert sr._ROUTE_INVENTORY_RE.search(panel["title"]) and sr._ROUTE_SERVICE_RE.search(panel["prompt_template"])
