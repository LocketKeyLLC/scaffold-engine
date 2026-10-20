"""§17.1691 — a read-only step whose own output was cut is not done (live, job e5e22b92 T1/T2, 2026-10-20)."""
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr
from app.modules.rehearsal import RUNNER_OUTPUT_CAP

READ_NODE = {"title": "Login qBittorrent list torrents",
             "description": "Read-only: authenticate with stored credentials and list all torrents. No changes."}
# the shape of the live T1 answer: one JSON line of torrents, returned as its first 20,000 characters
TORRENT = '{"added_on":1791807781,"category":"tv-sonarr","name":"Show.S01E01.1080p","progress":1,"state":"stalledUP"},'
CUT_LIST = ("[" + TORRENT * (RUNNER_OUTPUT_CAP // len(TORRENT) + 1))[:RUNNER_OUTPUT_CAP]


def test_a_list_the_runner_cut_is_named():
    why = sr.a_read_was_cut(READ_NODE, [{"command": "pct exec 105 -- sh -c 'curl …/torrents/info'", "output": CUT_LIST}])
    assert "20,000" in why and "summary" in why


def test_a_log_the_template_cut_is_named():
    out = "started scaffold-T2 inside 104\n[CUT: the step printed 3409 lines; only the last 300 are kept]\n  \"id\": 1\n"
    why = sr.a_read_was_cut({"title": "Read full Sonarr queue", "description": "Read-only: paginate the queue."},
                            [{"command": "bash /tmp/in_ct_104.sh start", "output": out}])
    assert "3409 lines" in why


def test_correct_misses():
    short = [{"command": "x", "output": "[" + TORRENT * 10 + "]"}]
    assert sr.a_read_was_cut(READ_NODE, short) == ""
    change = {"title": "Widen indexer categories", "description": "Change each indexer's categories."}
    assert sr.a_read_was_cut(change, [{"command": "x", "output": CUT_LIST}]) == ""   # a change is judged by its checks


def _tail_line(template) -> str:
    vals = {"GID": "104", "STEP": "T2", "REMOTE_COMMANDS": "true", "VERIFY_INSIDE": "", "GUEST_USER_NAME": "GUEST_USER"}
    body = rt.render(template, vals)
    return next(ln.strip() for ln in body.splitlines() if "[CUT: the step printed" in ln)


@pytest.mark.parametrize("lines,cut", [(350, True), (300, False), (10, False)])
def test_the_template_line_marks_a_long_log_in_bash(lines, cut):
    line = _tail_line(rt.RUN_IN_CONTAINER)
    assert line.startswith('ginfo "')
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "step.log")
        with open(log, "w") as f:
            f.write("".join(f"line {i}\n" for i in range(1, lines + 1)))
        script = "ginfo() { bash -c \"$1\"; }\n" + line.replace("/root/.scaffold_step.log", log) + "\n"
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20).stdout
    assert ("[CUT: the step printed %d lines; only the last 300 are kept]" % lines in out) is cut
    assert out.rstrip().endswith(f"line {lines}") and len([x for x in out.splitlines() if x.startswith("line ")]) == min(lines, 300)


SCRIPT = {"path": "/tmp/in_ct_105.sh",
          "content": "cat > /tmp/in_ct_105_remote.sh <<'REMOTE'\nset +e; trap 'exit 0' EXIT  # " + sr.READS_ONLY_MARKER
                     + "\ncurl …/torrents/info\nREMOTE\n"}


async def _resolve(output):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmd = "pct exec 105 -- sh -c 'curl -s -b /tmp/qb_cookies.txt \"http://127.0.0.1:8080/api/v2/torrents/info\"'"
    waiting = {"kind": "run", "node_key": "T1", **READ_NODE, "prompt_template": READ_NODE["description"],
               "runbook": "## Run this", "commands": [cmd], "verify": [], "refused": [], "files": [SCRIPT]}

    async def judge(claim_text, cmds, pasted, expects=None):
        return [{"id": "V1", "verdict": "confirmed", "reason": "the list came back"}]
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=([cmd], [])), \
         patch("app.modules.assist_supervised.run_block",
               new=AsyncMock(return_value=[{"command": cmd, "ok": True, "exit": 0, "output": output}])), \
         patch("app.modules.assist_supervised.write_files_on",
               new=AsyncMock(return_value=[{"command": "write /tmp/in_ct_105.sh", "ok": True, "exit": 0, "output": "wrote"}])), \
         patch.object(sr, "_verify_verdicts", side_effect=judge), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "j", "T1", "run", waiting)


@pytest.mark.asyncio
async def test_resolve_run_fails_the_cut_read_and_keeps_the_whole_one():
    cut = await _resolve(CUT_LIST)
    assert cut["node_status"] == "failed" and "20,000" in str(cut.get("reason")), cut.get("reason")
    whole = await _resolve("[" + TORRENT * 10 + "]")
    assert whole["node_status"] == "done", whole.get("reason")
