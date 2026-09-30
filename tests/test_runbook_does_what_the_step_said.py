"""§17.1215 — a drafted block must do what its step said it would do.

The gate checks a command's SHAPE (§17.1187). §17.1213 checks the host can carry
it. Nothing checked the block against the step's own intent.

ADD94 spells its work out in its own description. The engine drafted four
commands and dropped three of them — no stop, no boot order, no start — on a
RUNNING VM whose `boot: order=ide2` points at an empty CD-ROM. That block runs
clean, reports success, and leaves the server exactly as down as it was. Every
existing check passes it.
"""
from __future__ import annotations

import pytest

from app.modules import runbook_coverage as rc

STEP = """RECOVERY — VM 106's disk is currently DETACHED.

Re-attach it, then grow it:

```bash
qm stop 106
qm set 106 --scsi0 local-lvm:vm-106-disk-0
qm disk resize 106 scsi0 100G
qm set 106 --boot order=scsi0
qm start 106
```

Done when `qm config 106` shows `scsi0: …,size=100G`."""

BAD_DRAFT = ["qm config 106", "qm rescan --vmid 106",
             "qm set 106 --scsi0 <DISK_NAME>", "qm resize 106 scsi0 100G"]
GOOD_DRAFT = ["qm stop 106", "qm status 106", "qm set 106 --scsi0 local-lvm:vm-106-disk-0",
              "qm resize 106 scsi0 100G", "qm set 106 --boot order=scsi0", "qm start 106"]


def test_the_step_s_own_commands_are_read_off_its_description():
    assert rc.commands_in(STEP) == [
        "qm stop 106", "qm set 106 --scsi0 local-lvm:vm-106-disk-0",
        "qm disk resize 106 scsi0 100G", "qm set 106 --boot order=scsi0", "qm start 106"]


def test_prose_only_steps_name_nothing_and_require_nothing():
    """Most steps describe intent in words. This must never invent a
    requirement out of a step that stated none."""
    assert rc.commands_in("Start the container and confirm it answers on 3001.") == []
    assert rc.uncovered("Start the container and confirm it answers.", ["anything"]) == []


# ── the real miss ────────────────────────────────────────────────────────

def test_the_draft_that_nearly_left_the_server_down_is_caught():
    missing = rc.uncovered(STEP, BAD_DRAFT)
    assert missing == ["qm stop 106", "qm set 106 --boot order=scsi0", "qm start 106"], missing


def test_the_corrected_draft_passes():
    assert rc.uncovered(STEP, GOOD_DRAFT) == []


def test_two_flags_on_the_same_verb_are_not_confused():
    """A head-only match would see `qm set 106` and call `--boot` covered by
    `--scsi0`. That is precisely the command that was missing."""
    assert rc.signature("qm set 106 --scsi0 x") != rc.signature("qm set 106 --boot order=scsi0")
    assert "--boot" in rc.signature("qm set 106 --boot order=scsi0")


def test_a_placeholder_satisfies_the_value_the_step_named():
    """The step names a literal disk; the draft carries `<DISK_NAME>`. Same
    instruction — the value is filled at approval (§17.1212)."""
    assert rc.uncovered(STEP, GOOD_DRAFT[:2] + ["qm set 106 --scsi0 <DISK_NAME>"] + GOOD_DRAFT[3:]) == []


def test_a_spelling_synonym_is_not_a_gap():
    """`qm disk resize` and `qm resize` are one instruction; redrafting over a
    synonym would be the engine arguing with itself."""
    assert rc.uncovered("```bash\nqm disk resize 106 scsi0 100G\n```", ["qm resize 106 scsi0 100G"]) == []


def test_extra_commands_are_allowed():
    """One-directional on purpose: a draft may add a `qm status` between steps."""
    assert rc.uncovered(STEP, GOOD_DRAFT + ["qm config 106"]) == []


def test_an_empty_draft_is_entirely_uncovered():
    assert len(rc.uncovered(STEP, [])) == 5


# ── the note that goes back for the redraft ──────────────────────────────

def test_the_note_quotes_what_was_dropped():
    note = rc.coverage_retry_note(rc.uncovered(STEP, BAD_DRAFT))
    for c in ("qm stop 106", "qm set 106 --boot order=scsi0", "qm start 106"):
        assert c in note, note
    assert "qm rescan" not in note, "only the omissions, not a rewrite of the draft"
    assert "report success while the" in note, "say WHY a silent omission is worse than a failure"


def test_no_gap_no_note():
    assert rc.coverage_retry_note([]) == ""


# ── wired, and safe ──────────────────────────────────────────────────────

def test_the_draft_path_checks_coverage_and_redrafts_once():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    assert "runbook_coverage import coverage_retry_note, uncovered" in src
    assert "coverage_retry_note(_missing)" in src, "the omissions must reach the redraft"


def test_a_redraft_never_replaces_a_block_with_an_empty_one():
    """§17.1211's lesson: a regeneration that comes back empty must not be
    written over something that worked."""
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    blk = src[src.index("runbook_coverage_redraft job="):][:900]
    assert '_third.get("commands") and not uncovered(' in blk, blk


def test_the_replacement_must_itself_be_covered():
    """Swapping one incomplete draft for another incomplete draft is not a fix."""
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    assert 'not uncovered(_step_text, _third["commands"])' in src
