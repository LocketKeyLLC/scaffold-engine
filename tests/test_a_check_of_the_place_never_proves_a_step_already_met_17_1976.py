"""§17.1976 — a check that shows only that the step's machine is up never proves the step already met, and the
step's own named checks (its "Check:" line) are the pre-read, run where the step runs.

Live (job 0fb4ea7f LM26, 2026-11-21 13:41 UTC): "VM 110: start the benchmark A/B again as a transient unit" was
recorded "## Already met — nothing was run" off one read, `qm status 110` → `status: running` (the in-guest template's
own verify); the judge wrote "the VM the benchmark A/B runs in is up again, so its effect is present". The step's text
said "Check: `systemctl is-active lm-ab3` and `tail -n 3 /root/lm_bench.log`. Done when lm-ab3 is active …" and lm-ab3
was not running."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.modules import supervised_runs as sr
from app.modules.operator_steps import checks

LM26 = {"node_key": "LM26", "title": "VM 110: start the benchmark A/B again as a transient unit", "description": "",
        "prompt_template": (
            "Inside VM 110: start the local-model benchmark A/B again. lm-ab3 was a transient unit (systemd-run) and "
            "did not survive the host going down; its script /root/lm_ab3.sh is still there.\n"
            "Run, as root inside the guest:\n"
            "`test -s /root/lm_ab/triage.partial.jsonl && test -x /root/lm_ab3.sh`\n"
            "`systemd-run --unit=lm-ab3 --collect bash /root/lm_ab3.sh`\n"
            "Check: `systemctl is-active lm-ab3` and `tail -n 3 /root/lm_bench.log`.\n"
            "Done when lm-ab3 is active and the log shows a new \"== ab3 start\".")}
_RUN = 'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<GUEST_USER_NAME>" bash /tmp/in_vm_110.sh {}'
LM26_FRAME = {"commands": [_RUN.format(p) for p in ("boot", "boot", "boot", "start", "wait", "last")],
              "verify": ["qm status 110"]}


def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _verify(answer):
    async def fake(spec, cmds, env):
        fake.calls.append(list(cmds))
        pasted = "".join(f"== V{i} ==\n{answer(c)}\n" for i, c in enumerate(cmds, 1))
        return pasted, [{"id": f"V{i}", "command": c, "ok": True, "ran": True} for i, c in enumerate(cmds, 1)]
    fake.calls = []
    return fake


def _judge(verdict_of):
    """The live judge's shape: one verdict per check, decided here from the command."""
    async def fake(claim, cmds, pasted, expects=None):
        fake.calls.append(list(cmds))
        return [{"id": f"V{i}", "verdict": verdict_of(c), "reason": "", "claim": claim} for i, c in enumerate(cmds, 1)]
    fake.calls = []
    return fake


def _lm26_answer(c):
    if c == "qm status 110":
        return "status: running"
    return "inactive" if "is-active" in c else "== ab routing already done"


def test_the_steps_check_line_names_its_checks():
    assert checks(LM26) == ["systemctl is-active lm-ab3", "tail -n 3 /root/lm_bench.log"]


def test_place_checks_hit_and_miss():
    for c in ("qm status 110", "pct status 109", "ping -c 2 10.20.30.40", "qm status 110 | grep -q running",
              "ssh admin@10.20.30.40 true", "pct exec 109 -- uptime", "qm guest ping 106",
              "qm agent 106 ping", "nc -z 10.20.30.40 22", _RUN.format("check 'hostname'")):
        assert sr.a_place_check(c), c
    for c in ("systemctl is-active lm-ab3", "pct exec 120 -- dpkg -s jq", "qm config 110 | grep -E '^scsi0:'",
              "ssh admin@10.20.30.40 'nvidia-smi'", "qm status 110; tail -n 3 /root/lm_bench.log",
              "pct exec 101 -- ls -la /media/movies", "df -h /", _RUN.format("check 'systemctl is-active lm-ab3'")):
        assert not sr.a_place_check(c), c


@pytest.mark.asyncio
async def test_lm26_a_running_vm_alone_is_not_the_step_already_met(monkeypatch):
    """Part 1 alone: no named check, only the template's `qm status 110`, and a judge that confirms everything."""
    node = {**LM26, "prompt_template": LM26["prompt_template"].split("Check:")[0]}
    assert checks(node) == []
    rv = _verify(_lm26_answer); monkeypatch.setattr(sr, "run_verify", rv)
    judge = _judge(lambda c: "confirmed"); monkeypatch.setattr(sr, "_verify_verdicts", judge)
    assert await sr.already_met(_spec(), node, LM26_FRAME, {}) is None
    assert rv.calls == [] and judge.calls == [], "a pre-read of the place is never taken as evidence"


@pytest.mark.asyncio
async def test_lm26_live_the_named_checks_run_inside_the_guest_and_say_not_met(monkeypatch):
    rv = _verify(_lm26_answer); monkeypatch.setattr(sr, "run_verify", rv)
    judge = _judge(lambda c: "contradicted" if "is-active" in c else "confirmed")
    monkeypatch.setattr(sr, "_verify_verdicts", judge)
    assert await sr.already_met(_spec(), LM26, LM26_FRAME, {}) is None
    ran = rv.calls[0]
    assert ran[0] == "qm status 110"
    assert _RUN.format("check 'systemctl is-active lm-ab3'") in ran, ran          # in the guest, not on the host
    assert _RUN.format("check 'tail -n 3 /root/lm_bench.log'") in ran, ran


@pytest.mark.asyncio
async def test_a_named_check_the_pre_read_cannot_carry_means_not_met(monkeypatch):
    """The drafted verify reads another guest and the step's bare check has no way in: the pre-read is not the step's."""
    node = {"node_key": "X1", "title": "CT 111: restart the panel",
            "prompt_template": "Restart it. Done when `systemctl is-active control-panel.service` prints active."}
    frame = {"commands": ["pct exec 111 -- systemctl restart control-panel.service"],
             "verify": ["pct exec 120 -- systemctl is-active caddy"]}
    monkeypatch.setattr(sr, "run_verify", _verify(lambda c: "active"))
    monkeypatch.setattr(sr, "_verify_verdicts", _judge(lambda c: "confirmed"))
    assert "control-panel.service" in sr.own_checks_not_read(node, frame["verify"])
    assert await sr.already_met(_spec(), node, frame, {}) is None


@pytest.mark.asyncio
async def test_correct_miss_a_package_already_installed_is_already_met(monkeypatch):
    node = {"node_key": "P1", "title": "CT 120: install jq",
            "description": "Install jq inside CT 120. Done when `pct exec 120 -- dpkg -s jq` shows Status: install ok installed."}
    frame = {"commands": ["pct exec 120 -- apt-get install -y jq"], "verify": ["pct exec 120 -- dpkg -s jq"]}
    monkeypatch.setattr(sr, "run_verify", _verify(lambda c: "Package: jq\nStatus: install ok installed"))
    monkeypatch.setattr(sr, "_verify_verdicts", _judge(lambda c: "confirmed"))
    met = await sr.already_met(_spec(), node, frame, {})
    assert met and met["checks"] == 1 and "install ok installed" in met["report"]


@pytest.mark.asyncio
async def test_correct_miss_a_step_that_only_starts_the_guest_is_proven_by_its_status(monkeypatch):
    """3cc24343 T4 "Start CT 109": the block's whole effect IS the place."""
    node = {"node_key": "T4", "title": "Start CT 109", "description": "pct start 109. Verify it boots and is reachable."}
    frame = {"commands": ["pct start 109"], "verify": ["pct status 109", "ping -c 2 10.20.30.40"]}
    monkeypatch.setattr(sr, "run_verify", _verify(lambda c: "status: running" if "status" in c else "2 received"))
    monkeypatch.setattr(sr, "_verify_verdicts", _judge(lambda c: "confirmed"))
    assert await sr.already_met(_spec(), node, frame, {})


@pytest.mark.asyncio
async def test_a_restart_is_not_proven_by_the_guest_being_up(monkeypatch):
    node = {"node_key": "G9", "title": "Restart VM 110 to reset its GPU", "description": ""}
    frame = {"commands": ["qm reboot 110"], "verify": ["qm status 110"]}
    monkeypatch.setattr(sr, "run_verify", _verify(lambda c: "status: running"))
    monkeypatch.setattr(sr, "_verify_verdicts", _judge(lambda c: "confirmed"))
    assert await sr.already_met(_spec(), node, frame, {}) is None
