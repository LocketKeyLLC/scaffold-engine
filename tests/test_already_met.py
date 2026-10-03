"""§17.1302 — a step whose own verify checks already pass is already done.

Live, 2026-10-03 07:45 UTC: ADD84 "Grow the VM 106 filesystem to fill the
disk" was parked as `growpart /dev/sda 1` + `resize2fs /dev/sda1` while the
guest's cloud-init had grown the root partition on first boot (`df -h /` → 97G
of a 100G disk). `growpart` exits 1 on NOCHANGE; approving would have failed a
step the machine already showed met, with the proving read in the frame's own
verify list."""
from __future__ import annotations

import inspect
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
FRAME = json.loads((FX / "add84_frame_2026_10_03.json").read_text(encoding="utf-8"))
FS = (FX / "live_truth_2026_10_02" / "fs106_2026_10_03.txt").read_text(encoding="utf-8")
DF = FS.split("$ qm guest exec 106 -- df -h /\n")[1].split("\n$ ")[0]
NODE = {"node_key": "ADD84", "title": "Grow the VM 106 filesystem to fill the disk",
        "description": "Inside the palworld-server guest, grow the partition and filesystem so the OS sees the "
                       "full ~100GB (e.g. growpart + resize2fs). Done when `df -h` inside the guest reports the "
                       "root filesystem at ~100GB."}
ADD58_RUNBOOK = """## Run this

```bash
qm set 100 --ostype l26
```

## Verify

- `qm config 100 | grep -E 'ostype|hostpci0'` shows `ostype: l26` and `hostpci0: 83:00.0,pcie=1`
- `qm status 100` shows `status: running`
"""


def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _verify(answers: dict):
    async def fake(spec, cmds, env):
        pasted = "".join(f"== V{i} ==\n{answers[c]}\n" for i, c in enumerate(cmds, 1) if c in answers)
        ran = [{"id": f"V{i}", "command": c, "ok": c in answers, "ran": c in answers} for i, c in enumerate(cmds, 1)]
        return pasted, ran
    return fake


def _judge(kind: str):
    async def fake(claim, cmds, pasted, expects=None):
        fake.calls.append((claim, list(cmds), expects))
        return [{"id": f"V{i}", "verdict": kind, "reason": "the root filesystem is 97G of a 100G disk",
                 "claim": claim} for i, _ in enumerate(cmds, 1)]
    fake.calls = []
    return fake


@pytest.mark.asyncio
async def test_add84_live_the_grown_filesystem_is_already_met(monkeypatch):
    assert FRAME["commands"][0].startswith("qm guest exec 106") and "growpart" in FRAME["commands"][0]
    monkeypatch.setattr(sr, "run_verify", _verify({FRAME["verify"][0]: DF}))
    judge = _judge("confirmed"); monkeypatch.setattr(sr, "_verify_verdicts", judge)
    met = await sr.already_met(_spec(), NODE, FRAME, {})
    assert met and met["checks"] == 1 and "97G" in met["report"], met
    claim, cmds, _ = judge.calls[0]
    assert NODE["title"] in claim and "Done when" in claim, "the judge reads the goal AND the step's done-when text"
    assert cmds == FRAME["verify"]
    rec = sr.already_met_record(NODE, "pve-runner", met)
    assert "nothing was run" in rec.lower() and "97G" in rec and "pve-runner" in rec and NODE["title"] in rec


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown", "contradicted"])
async def test_anything_short_of_confirmed_parks_as_before(monkeypatch, kind):
    monkeypatch.setattr(sr, "run_verify", _verify({FRAME["verify"][0]: DF}))
    monkeypatch.setattr(sr, "_verify_verdicts", _judge(kind))
    assert await sr.already_met(_spec(), NODE, FRAME, {}) is None


@pytest.mark.asyncio
async def test_a_check_that_did_not_answer_is_not_evidence(monkeypatch):
    monkeypatch.setattr(sr, "run_verify", _verify({}))          # the channel said nothing
    judge = _judge("confirmed"); monkeypatch.setattr(sr, "_verify_verdicts", judge)
    assert await sr.already_met(_spec(), NODE, FRAME, {}) is None
    assert judge.calls == [], "nothing to judge: no marker arrived"


@pytest.mark.asyncio
async def test_every_check_must_answer(monkeypatch):
    two = {**FRAME, "verify": [FRAME["verify"][0], "qm guest exec 106 -- lsblk"]}
    monkeypatch.setattr(sr, "run_verify", _verify({FRAME["verify"][0]: DF}))   # the second never answered
    monkeypatch.setattr(sr, "_verify_verdicts", _judge("confirmed"))
    assert await sr.already_met(_spec(), NODE, two, {}) is None


@pytest.mark.asyncio
async def test_no_checks_or_no_commands_means_nothing_to_read(monkeypatch):
    called = []

    async def never(*a, **k):
        called.append(1); return "", []
    monkeypatch.setattr(sr, "run_verify", never)
    assert await sr.already_met(_spec(), NODE, {**FRAME, "verify": []}, {}) is None
    assert await sr.already_met(_spec(), NODE, {**FRAME, "commands": []}, {}) is None
    assert called == []


def test_verify_expectations_reads_the_shows_bullets():
    exp = sr.verify_expectations(ADD58_RUNBOOK)
    assert exp == {"qm config 100 | grep -E 'ostype|hostpci0'": "ostype: l26", "qm status 100": "status: running"}
    assert sr.verify_expectations(FRAME["runbook"]) == {}, "ADD84's bullet names no literal: nothing deterministic"


@pytest.mark.asyncio
async def test_an_expectation_settles_the_check_without_a_model():
    """The judge's deterministic pre-pass: the runbook said what the output
    shows when the goal holds, and it does — no model draw."""
    from app.modules import assist_state_check as sc
    import app.model_router as mr
    drew = []

    async def no_model(*a, **k):
        drew.append(1); raise AssertionError("the model must not be asked")
    import unittest.mock as um
    with um.patch.object(mr, "tool_call", new=no_model):
        verdicts = await sr._verify_verdicts("Configure VM 100", ["qm status 100"], "== V1 ==\nstatus: running\n",
                                             expects={"qm status 100": "status: running"})
    assert [v["verdict"] for v in verdicts] == ["confirmed"] and drew == []
    assert "expected" in verdicts[0]["reason"]
    assert sc.judge_outputs  # the one judge both paths use


def test_the_pause_reads_the_checks_before_it_parks():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("supervised_runs.already_met(") == 1
    assert body.index("supervised_runs.already_met(") < body.index('logger.warning("supervised_run_parked'), \
        "read the machine BEFORE parking the question"
    assert body.index("runbook_discovery") < body.index("supervised_runs.already_met("), \
        "after every redraft, so the checks read are the ones that would be parked"
    assert "_pause_for_decision(job_id, _depth + 1)" in body, "the NEXT step may be hands-on too: ask about it, never claim it unasked"
    assert "status = 'pending'" in body[body.index("supervised_runs.already_met("):], "only a never-claimed step is recorded this way"
    assert inspect.signature(ea._pause_for_decision).parameters["_depth"].default == 0


# ── §17.1302b — the judge never read a run's checks ──────────────────────────

def test_the_run_path_marker_and_the_judge_splitter_never_matched():
    """The defect, as a fact about two regexes: `run_probes` writes `== V1 ==`,
    `attribute_sections` splits on `== <L>:<id> ==`."""
    from app.modules.assist_state_check import attribute_sections
    assert attribute_sections("== V1 ==\nstatus: running\n") == {}
    assert attribute_sections("== V:1 ==\nstatus: running\n") == {"V:1": "status: running"}


@pytest.mark.asyncio
async def test_verify_verdicts_translates_the_ids_both_ways(monkeypatch):
    from app.modules import assist_state_check as sc
    seen = {}

    async def fake_judge(probes, pasted, **kw):
        seen["probes"], seen["pasted"] = probes, pasted
        return [{"id": p["id"], "verdict": "confirmed", "reason": "", "claim": p["claim"], "kind": "state"} for p in probes]
    monkeypatch.setattr(sc, "judge_outputs", fake_judge)
    out = await sr._verify_verdicts("t", ["qm status 100", "qm config 100"],
                                    "== V1 ==\nstatus: running\n== V2 ==\nostype: l26\n")
    assert [p["id"] for p in seen["probes"]] == ["V:1", "V:2"], "ids the splitter can match"
    assert "== V:1 ==" in seen["pasted"] and "== V:2 ==" in seen["pasted"] and "== V1 ==" not in seen["pasted"]
    assert [v["id"] for v in out] == ["V1", "V2"], "callers (`contradicted`, the drop path) keep seeing V1/V2"


@pytest.mark.asyncio
async def test_the_drop_path_can_now_confirm_from_the_runbooks_own_expectations():
    """§17.1225 end to end on the real judge, deterministic branch: the step's
    checks read back as the runbook said they would → confirmed → done."""
    import unittest.mock as um
    import app.model_router as mr

    async def no_model(*a, **k):
        raise AssertionError("the model must not be asked")
    with um.patch.object(mr, "tool_call", new=no_model):
        ok = await sr._goal_confirmed("Configure VM 100", ["qm status 100"], "== V1 ==\nstatus: running\n")
        assert ok is False, "no expectation given: the pre-pass cannot settle it and the model is not asked here"
        verdicts = await sr._verify_verdicts("Configure VM 100", ["qm status 100"], "== V1 ==\nstatus: running\n",
                                             expects={"qm status 100": "status: running"})
    assert [v["verdict"] for v in verdicts] == ["confirmed"]
