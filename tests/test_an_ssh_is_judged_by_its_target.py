"""§17.1338 — an ssh is judged by the target it names, and an ssh inside a written
file is the service's problem, not the block's.

Live, 2026-10-04. ADD122 ("LXC 111: implement the Palworld settings capability")
writes a Node route into container 111 whose code reaches the PalWorld VM:

    const PALWORLD_IP = '192.168.1.106';
    function sshExec(args) {
      return execFileSync('ssh', [ … `${PALWORLD_USER}@${PALWORLD_IP}`, …

The key precondition attributed that ssh to the step's SUBJECT and parked the
frame with: *"nothing has put this host's key on guest 111 … Before the first
ssh: sshpass -e ssh-copy-id …"*. Wrong twice. The ssh runs FROM 111, TO 106, at
runtime, and there is no "first ssh" in the block to prefix: the block pushes a
file and runs npm install. Meanwhile the real problem — that the panel's own
request will fail with publickey the first time anyone opens the page, while this
step's checks pass because the file is on disk — was never said.

The fixture is that frame's file, as the engine drafted it.
"""
from __future__ import annotations

import pathlib

import pytest

from app.modules import runbook_preconditions as rp

FX = pathlib.Path(__file__).parent / "fixtures"
LIVE_FILE = (FX / "add122_in_ct_111_2026_10_04.sh").read_text(encoding="utf-8")
#: `pct list` that pause: the engine names the guests, this file does not
INV = {"cts": {"111": "running", "101": "running"}, "vms": {"106": "running"},
       "names": {"111": "control-panel", "106": "palworld-server", "101": "jellyfin"}}
NODE = {"node_key": "ADD122", "title": "LXC 111: implement the Palworld settings capability",
        "description": "On LXC 111, add a backend route that reads and writes the PalWorld server's own "
                       "settings file. On container 111."}


def test_the_live_file_really_sshs_out_of_the_container():
    assert "execFileSync('ssh'" in LIVE_FILE
    assert "const PALWORLD_IP = '192.168.1.106';" in LIVE_FILE


def test_the_ssh_line_alone_names_no_host_so_the_file_does():
    line = next(ln for ln in LIVE_FILE.split("\n") if "execFileSync('ssh'" in ln)
    assert rp.ssh_target_in(line) == ("", ""), "nothing to read on that line"
    assert rp.host_a_file_reaches(LIVE_FILE, line) == "192.168.1.106", "the file's own constant"


def test_a_plain_command_still_reads_its_own_target():
    assert rp.ssh_target_in("ssh aedefruscio@192.168.1.106 true") == ("aedefruscio", "192.168.1.106")
    assert rp.ssh_target_in("sshpass -e ssh root@192.168.1.25 ls")[1] == "192.168.1.25"
    assert rp.ssh_target_in("echo hello") == ("", "")


def test_two_addresses_in_one_file_decide_nothing():
    """A file naming several machines is not one target; no judgment beats a guess."""
    two = "const A = '192.168.1.106';\nconst B = '192.168.1.25';\nexecFileSync('ssh', [A]);"
    assert rp.host_a_file_reaches(two, "execFileSync('ssh', [A]);") == ""


def test_broadcast_and_network_addresses_are_not_targets():
    f = "const NET = '192.168.1.0';\nconst BC = '192.168.1.255';\nexecFileSync('ssh', [NET]);"
    assert rp.host_a_file_reaches(f, "execFileSync('ssh', [NET]);") == ""


def test_the_file_is_reported_against_the_host_it_reaches_not_the_subject():
    rows = rp._file_ssh_needs_its_own_credential(
        [{"path": "/opt/control-panel-backend/routes/palworld.js", "content": LIVE_FILE}],
        ["111"], INV, [])
    assert len(rows) == 1, rows
    why = rows[0]["why"]
    assert "192.168.1.106" in why and "from inside guest 111" in why
    assert "not a prefix for this block" in why
    assert "ssh-copy-id" not in why, "the old remedy aimed at a command the block does not run"
    assert "while this step's checks pass" in why, "it says why the step would look fine"


def test_one_judgment_per_file_however_many_ssh_lines():
    rows = rp._file_ssh_needs_its_own_credential(
        [{"path": "a.js", "content": LIVE_FILE}, {"path": "b.js", "content": LIVE_FILE}], ["111"], INV, [])
    assert [r["command"][:24] for r in rows] and len(rows) == 2, rows


def test_a_finished_key_step_settles_it():
    plan = [{"node_key": "ADD26", "status": "done",
             "title": "Install the control panel's public key on the PalWorld VM (192.168.1.106)"}]
    rows = rp._file_ssh_needs_its_own_credential(
        [{"path": "x.js", "content": LIVE_FILE}], ["111"], {**INV, "addresses": {"106": ["192.168.1.106"]}}, plan)
    assert rows == [], rows


@pytest.mark.asyncio
async def test_the_whole_rule_no_longer_refuses_the_block_for_a_files_ssh():
    """End to end through `unmet`: the file is reported, and nothing claims the
    block ssh's into container 111."""
    cmds = ["pct push 111 /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh",
            "pct exec 111 -- systemd-run --unit scaffold-ADD122 bash /root/.scaffold_step.sh"]
    rows = await rp.unmet(cmds, None, plan=[], node=NODE, inventory=INV,
                          files=[{"path": "/opt/control-panel-backend/routes/palworld.js", "content": LIVE_FILE}])
    key_rows = [r for r in rows if "key" in r["why"] or "credential" in r["why"]]
    assert len(key_rows) == 1, [r["why"][:90] for r in rows]
    assert "put this host's key on guest 111" not in key_rows[0]["why"], key_rows[0]["why"]
    assert "192.168.1.106" in key_rows[0]["why"]

def test_depth_says_who_runs_a_line():
    """Depth 0 is the script; depth 1 is a body it writes and then RUNS (the
    container template pushes `<<'REMOTE'` into the guest and runs it there);
    depth 2 is content that body writes to a file."""
    depths = rp.heredoc_depths(LIVE_FILE)
    lines = LIVE_FILE.split("\n")
    assert len(depths) == len(lines)
    i = next(j for j, ln in enumerate(lines) if "execFileSync('ssh'" in ln)
    assert depths[i] == 2, depths[i]
    j = next(k for k, ln in enumerate(lines) if ln.startswith("cat > /tmp/in_ct_111_remote.sh"))
    assert depths[j] == 0, "the opener is the script's own line"
    k = next(m for m, ln in enumerate(lines) if ln.startswith("export DEBIAN_FRONTEND"))
    assert depths[k] == 1, "the body the guest runs"


def test_a_plain_script_has_no_heredoc_depth():
    t = "#!/bin/bash\nset -e\nssh -o BatchMode=yes \"$USER@$IP\" true\n"
    assert rp.heredoc_depths(t)[2] == 0
    assert any("ssh -o BatchMode=yes" in ln for ln in rp.executed_lines(t))
    assert rp.written_content_lines(t) == []


def test_an_unterminated_heredoc_does_not_swallow_the_file():
    """A cut script (§17.1310) still reads: every line after the opener is body,
    and nothing claims the script runs an ssh it never reaches."""
    t = "cat > /tmp/x <<'EOF'\nssh root@192.168.1.9 true\n"
    assert rp.heredoc_depths(t) == [0, 1, 1]
    assert not [ln for ln in rp.executed_lines(t, max_depth=0) if "ssh" in ln]
