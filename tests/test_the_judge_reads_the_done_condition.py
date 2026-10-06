"""§17.1396 — a step's checks are judged against its done-condition, not its title alone.

Live, 2026-10-06. ADD135, "Make the machines that serve the operator's goals start
on boot", carried the operator's decision in its description: onboot on VM 110
ai-vm ONLY, never VM 100 gpu-vm, "Done when 110.conf carries onboot: 1 and
100.conf still has no onboot line." The engine drafted exactly that —
`qm set 110 --onboot 1` — and the check on VM 100 answered "no onboot line
(correct)". The post-run judge saw only the title, reasoned "this machine does not
start on boot", returned CONTRADICTED, and the step that did what the operator
chose was recorded failed.

The already-met path had judged `title — description` since §17.1302. The two
others passed the title alone.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import supervised_runs as sr
from app.modules.supervised_runs import goal_claim

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD135 = json.loads((FIX / "add135_names_ten_guests_acts_on_none_2026_10_06.json").read_text())


def test_the_claim_carries_the_done_condition():
    claim = goal_claim(ADD135["title"], ADD135["description"])
    assert claim.startswith(ADD135["title"])
    assert "Do NOT set it on VM 100" in claim
    assert "100.conf still has no onboot line" in claim


def test_no_description_is_the_title_alone():
    assert goal_claim("Install X", "") == "Install X"
    assert goal_claim("Install X", "   \n ") == "Install X"


def test_the_description_is_bounded():
    assert len(goal_claim("T", "x " * 2000)) <= len("T — ") + sr._CLAIM_CAP + 5


def test_the_done_condition_survives_a_long_description():
    """The 400 cap cut ADD135 at "OPERATOR DECISION 2026-10-06: s". A description
    longer than any cap still hands the judge its done-condition whole."""
    desc = "Measured state. " * 200 + "Done when 110.conf carries onboot: 1 and 100.conf has none."
    claim = goal_claim("T", desc)
    assert claim.endswith("Done when 110.conf carries onboot: 1 and 100.conf has none.")
    assert len(claim) <= len("T — ") + sr._CLAIM_CAP + 5


def test_the_old_cap_would_have_cut_the_live_decision():
    """Vacuity guard: the live description really is longer than the old cap, and
    its decision really sits past it."""
    d = " ".join(ADD135["description"].split())
    assert len(d) > 400 and d.index("Do NOT set it on VM 100") > 400


def test_every_judge_path_builds_its_claim_the_same_way():
    """The drift was three call sites, one of them right. Asserted by source so a
    fourth cannot be added the old way."""
    src = inspect.getsource(sr)
    assert src.count("goal_claim(") >= 4          # the def + three call sites
    assert 'contradicted(await _verify_verdicts(\n                    str(waiting.get("title")' not in src
    assert '_goal_confirmed(\n                        str(waiting.get("title")' not in src
    assert "goal_claim(title, " in inspect.getsource(sr.already_met) if hasattr(sr, "already_met") else True


@pytest.mark.asyncio
async def test_the_judge_receives_the_done_condition(monkeypatch):
    """What the JUDGE is handed, not what a variable holds ([[feedback_declared_but_never_carried]])."""
    seen: list[dict] = []

    async def fake_judge(probes, pasted, **kw):
        seen.extend(probes)
        return [{"id": p["id"], "verdict": "confirmed", "command": p["command"]} for p in probes]

    import app.modules.assist_state_check as sc
    monkeypatch.setattr(sc, "judge_outputs", fake_judge)
    checks = ["grep onboot /etc/pve/qemu-server/110.conf",
              'grep onboot /etc/pve/qemu-server/100.conf || echo "no onboot line (correct)"']
    pasted = "== V1 ==\nonboot: 1\n== V2 ==\nno onboot line (correct)\n"
    await sr._verify_verdicts(goal_claim(ADD135["title"], ADD135["description"]), checks, pasted)
    assert seen and all("100.conf still has no onboot line" in p["claim"] for p in seen)


def test_the_parked_frame_carries_the_description():
    """No extra query in resolve_run: the question carries its own done-condition."""
    from app.modules import decision_pause
    src = inspect.getsource(decision_pause.park_awaiting_decision)
    assert '"description": node.get("description")' in src
    rr = inspect.getsource(sr.resolve_run)
    assert rr.count('str(waiting.get("description") or "")') == 2
