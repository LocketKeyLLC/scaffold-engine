"""§17.1981 — a backticked Done-when text that only NAMES one of the step's own commands is not a check of its own,
and a check that walks a tree with no path never runs.

Live (2026-11-21, job 0fb4ea7f CL1 "Proxmox host: read the Playwright browsers"): the step ran
`du -sh /opt/…/browsers/* 2>&1` and said "Done when it prints the `du -sh` lines." The fragment `du -sh` became a
verify command; it ran bare on the host with cwd `/` and walked the whole root filesystem
(`du: cannot access './proc/…'`).
"""
from __future__ import annotations

from app.modules import operator_steps as ops
from app.modules import supervised_runs as sr

# the live CL1 shape (paths made up)
CL1 = {"node_key": "CL1", "tool": "Shell", "title": "Proxmox host: read the browser toolchain", "description": "",
       "prompt_template": (
           "Proxmox host: read what the browser toolchain holds; change nothing.\n"
           "Run on the Proxmox host:\n"
           "`ls -la /opt/browser-kit /opt/browser-kit/browsers 2>&1 | head -n 40`\n"
           "`du -sh /opt/browser-kit/browsers/* 2>&1`\n"
           "`grep -rn \"BROWSERS_PATH\" /root/*.sh /usr/local/bin 2>/dev/null | head -n 5`\n"
           "Done when it prints the `du -sh` lines.")}
# the live LM4 / LM19 shapes: a program named without its operands, a strict prefix of the step's own command
LM4 = {"node_key": "LM4", "tool": "Shell", "title": "Inside VM 120: why the bench stopped", "prompt_template": (
    "Run, as root inside the guest:\n`journalctl -u bench-unit --no-pager -o short-iso | tail -n 40`\n"
    "`wc -l /root/bench.log`\nDone when it prints the `journalctl -u bench-unit` lines.")}
LM19 = {"node_key": "LM19", "tool": "Shell", "title": "Inside VM 120: restart the A/B", "prompt_template": (
    "1. `sed -i 's/^run a$/run b/' /root/ab.sh && grep -n '^run ' /root/ab.sh`\n"
    "2. `systemd-run --unit=ab3 --collect bash /root/ab.sh`\n"
    "Done when `systemctl is-active ab3` prints `active` and the `grep -n '^run '` line shows `run b`.")}


def test_the_live_fragment_is_not_a_check():
    assert "du -sh" not in ops.checks(CL1)
    assert ops.checks(CL1) == []


def test_a_program_named_without_its_operands_is_not_a_check():
    assert ops.checks(LM4) == []


def test_a_strict_prefix_is_not_a_check_but_the_real_check_beside_it_stays():
    assert ops.checks(LM19) == ["systemctl is-active ab3"]


def test_real_done_when_checks_stay():
    """Correct misses: complete commands with operands, and the step's own command repeated whole."""
    node = {"prompt_template": (
        "Run `qm config 130 | grep scsi0` and `du -sh /opt/kit/browsers/* 2>&1`.\n"
        "Done when `qm config 130` shows the disk, `du -sh /opt/kit/browsers` prints a size, `df -h /` shows space "
        "and `du -sh /opt/kit/browsers/* 2>&1` lists each browser.")}
    assert ops.checks(node) == ["qm config 130", "du -sh /opt/kit/browsers", "df -h /",
                                "du -sh /opt/kit/browsers/* 2>&1"]


def test_a_bare_walker_is_never_a_check_even_with_no_own_command():
    node = {"prompt_template": "Read the sizes. Done when `du -sh` prints them and `find -name '*.log'` lists them."}
    assert ops.checks(node) == []


def test_walks_the_cwd_hits_and_misses():
    for hit in ("du -sh", "du -sh 2>&1", "find -name x", "find", "grep -rn TOKEN", "grep -r -e TOKEN", "ls -laR",
                "tree", "rg TOKEN", "du -sh | sort -h"):
        assert ops.walks_the_cwd(hit), hit
    for miss in ("du -sh /opt/x", "du -sh /opt/x/* 2>&1", "find /etc -name x", "grep -rn TOKEN /etc",
                 "grep -n x /root/a.sh", "ls -la", "ls -R /opt/x", "cd /opt/x && du -sh", "df -h", "rg TOKEN /srv",
                 "qm config 130", "systemctl is-active ab3"):
        assert not ops.walks_the_cwd(miss), miss


def test_the_run_never_adds_the_fragment():
    """with_own_checks (§17.1470) adds the step's own checks to the drafted list; the fragment is not one."""
    verify = ["du -sh /opt/browser-kit/browsers/* 2>&1"]
    assert sr.with_own_checks(CL1, verify, ["du -sh /opt/browser-kit/browsers/* 2>&1"]) == verify


def test_the_pre_read_does_not_wait_on_the_fragment():
    """§17.1976's own-check guard no longer calls the pre-read blind for leaving out `du -sh`."""
    assert sr.own_checks_not_read(CL1, ["du -sh /opt/browser-kit/browsers/* 2>&1"]) == ""


POLICY = {"allow": ["ANY"], "sudo": True, "secrets": [], "can_write_files": True}
DRAFT = """## Run this

```bash
ls -la /opt/browser-kit/browsers 2>&1 | head -n 40
du -sh /opt/browser-kit/browsers/* 2>&1
```

## Verify

```bash
du -sh
du -sh /opt/browser-kit/browsers/* 2>&1
```
"""


def _frame(runbook):
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://10.9.8.7:8790/mcp/"})()
    return sr.frame_run(CL1, runbook, spec, POLICY, env={"profile": "root@pve"},
                        inventory={"cts": {}, "vms": {}, "names": {}})


def test_a_drafted_walker_check_is_dropped_from_the_frame():
    """The live frame's Verify carried `du -sh` itself: the drafter copied the Done-when. It is dropped, said why."""
    frame = _frame(DRAFT)
    assert "du -sh" not in frame["verify"], frame["verify"]
    assert "du -sh /opt/browser-kit/browsers/* 2>&1" in frame["verify"]
    assert any("du -sh" in w and "root filesystem" in w for w in frame["engine_fixed"]), frame["engine_fixed"]


def test_a_drafted_check_with_its_path_stays_in_the_frame():
    frame = _frame(DRAFT.replace("du -sh\ndu -sh /opt", "du -sh /opt/browser-kit\ndu -sh /opt"))
    assert "du -sh /opt/browser-kit" in frame["verify"]
    assert not any("root filesystem" in w for w in frame["engine_fixed"])
