"""§17.915 — a step never commits without evidence.

Live (session 613dd1df, 2026-09-03 20:06:16): ADD5 "Install Ubuntu Server 22.04
on VM 106" — the step inserted precisely BECAUSE the operator could not install
the OS — was marked `done` with `evidence_kind` NULL while the OS was not
installed. Same shape that put T23 wrongly `done`: §17.911 repaired the
consequence (reopen on insert); nothing stopped the close.

§17.891 gated tracker retires on `has_advancement_signal`, which accepts three
things — and one of them, a bare next/continue/skip intent, means "move on",
not "this is done".
"""
from __future__ import annotations

import inspect

import pytest

from app.modules.assist_policy import has_advancement_signal, is_completion_evidence

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("msg", [
    "done",
    "I already installed it",
    "that's finished",
    "root@pve:~# qm config 106\nboot: order=scsi0\nmemory: 8192",
])
def test_claims_and_clean_pastes_are_evidence(msg):
    assert is_completion_evidence(msg)


@pytest.mark.parametrize("msg", ["next", "continue", "skip this", "move on"])
def test_bare_advance_intent_advances_but_does_not_evidence(msg):
    """The distinction `has_advancement_signal` collapses. `_retire_step_mirrored`'s
    own docstring draws it: "The Skip verb (deliberate skip, work NOT done)
    still writes 'skipped' ... the two are semantically different"."""
    assert has_advancement_signal(msg) is True
    assert is_completion_evidence(msg) is False


@pytest.mark.parametrize("msg", [
    # the four live messages around the wrongful ADD5 close
    "assist with the completion and implementation of the homelab",
    "I want to build a markdown linter",
    "I want to build something",
    "anything",
    # a paste that FAILED is not evidence of completion
    "root@pve:~# sudo lvextend -l +100%FREE\n-bash: sudo: command not found",
    "",
])
def test_noise_and_failures_are_never_evidence(msg):
    assert is_completion_evidence(msg) is False


def test_retire_sink_refuses_without_evidence():
    """The guard lives at the SINK every tracker retire funnels through, not at
    each caller — a new call site cannot forget it."""
    from app.routers import assist
    src = inspect.getsource(assist._retire_step_mirrored)
    assert "is_completion_evidence" in src
    assert "return False" in src
    assert "return True" in src
    # the veto must be loud (§17.882b)
    assert "assist_retire_vetoed_no_evidence" in src


def test_retire_records_what_justified_the_close():
    """ADD5 closed with evidence_kind NULL, so nothing downstream could tell a
    tracker-retire from a real submit and §17.899's denial path had nothing to
    check against."""
    from app.routers import assist
    src = inspect.getsource(assist._retire_step_mirrored)
    assert "evidence=COALESCE" in src
    assert "evidence_kind=COALESCE" in src
    assert "tracker_retire" in src


def test_both_call_sites_honour_the_refusal():
    """A retire that was vetoed must not be reported as retired — the pointer
    may still advance, but the step stays open."""
    from app.routers import assist
    src = inspect.getsource(assist)
    assert src.count("retire_vetoed_no_evidence") >= 3   # 1 log + 2 call sites
    assert "if await _retire_step_mirrored(" in src
    assert "_retired = await _retire_step_mirrored(" in src


def test_submit_path_still_requires_evidence():
    """The other commit path already enforced this; §17.915 brings the tracker
    path up to the same contract rather than loosening either."""
    from app.modules.assist_agent import submit_step
    assert "submit requires non-empty evidence" in inspect.getsource(submit_step)
