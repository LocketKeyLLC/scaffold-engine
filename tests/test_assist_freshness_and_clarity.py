"""§17.928-932 — the four operator-visible defects behind "the engine can't
figure out the current problem", "my messages vanish", and "it never tells me
to move on". Every case here is reconstructed from the live session
613dd1df (node T26, 2026-09-04/05), not invented input.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules.assist_agent import _fix_failure_streak, _looks_like_still_broken
from app.modules.assist_guide import (
    _primary_action_corpus,
    _repeats_in_primary_action,
    _with_advance_footer,
    advance_footer,
    find_repeated_failed,
    has_done_criterion,
)
from app.modules.assist_turns import list_turns


# ── §17.928 — the transcript window is the NEWEST turns ────────────────────


async def test_list_turns_window_is_the_newest_turns():
    """The window must be the most RECENT `limit` turns, not the oldest.

    The bug needs a session LONGER than the cap to show itself — with fewer
    turns than the limit both orderings return the same rows, which is why
    four green tests never caught it (the §17.906 lesson: a collection gate
    needs a multi-element input). Live cost: a 545-turn session served turns
    1-200, so the transcript and the model's history both froze six days in
    the past while the operator worked in the present.
    """
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=rows)

    await list_turns(session_id="s", limit=200, db=db)

    sql = " ".join(str(db.execute.call_args[0][0]).split())
    # The inner window must take the NEWEST rows...
    assert "ORDER BY created_at DESC, id DESC LIMIT :lim" in sql
    # ...and the result must still be handed back oldest-first.
    assert sql.rstrip().endswith("ORDER BY created_at ASC, id ASC")
    assert db.execute.call_args[0][1]["lim"] == 200


async def test_list_turns_returns_rows_oldest_first():
    """The contract every caller relies on is unchanged: oldest-first."""
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"id": 3, "node_key": "T26", "role": "operator", "kind": "message",
         "content": "third", "evidence_kind": None, "created_at": "2026-09-05"},
        {"id": 4, "node_key": "T26", "role": "assistant", "kind": "fix",
         "content": "fourth", "evidence_kind": None, "created_at": "2026-09-05"},
    ]
    db.execute = AsyncMock(return_value=rows)
    out = await list_turns(session_id="s", limit=2, db=db)
    assert [t["content"] for t in out] == ["third", "fourth"]


# ── §17.930 — "that didn't work", however the operator phrases it ──────────


@pytest.mark.parametrize("msg", [
    # THE live message that scored False and cost four repeated prescriptions.
    "neither the first or the 'if that fails' worked",
    "none of them worked",
    "nothing worked",
    "nothing helped",
    "that failed again",
    "it is not working",
    # §17.917 vocabulary must keep firing.
    "You still have not fixed the ubuntu server install",
    "same problem",
    "no change",
    "it didn't work",
])
def test_still_broken_recognises_real_failure_reports(msg):
    assert _looks_like_still_broken(msg) is True


@pytest.mark.parametrize("msg", [
    "It worked, moving on",
    "That worked perfectly",
    "ok next step",
    "root@pve:~# qm config 110",
    "The install completed and the network is working",
    "done",
])
def test_still_broken_does_not_fire_on_success(msg):
    """Over-firing retires good remedies and blocks legitimate re-prescription
    (the §17.916 failure mode). Both directions must hold."""
    assert _looks_like_still_broken(msg) is False


async def test_failure_report_retires_prescription_when_not_the_next_turn():
    """§17.930 — the live T26 shape, exactly.

    A fix at 23:35 prescribes `qm set 110 --delete hostpci0`; a `note`
    double-record lands at 23:36; the operator's "neither ... worked" arrives
    at 00:07 as later[1]. The §17.917 cut inspected later[0] ONLY, so the
    prescription was never retired and came back three more times — twice
    byte-identical.
    """
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"id": 1, "role": "assistant", "kind": "fix",
         "content": "## Fix\n```bash\nqm set 110 --delete hostpci0\n```"},
        # An intervening operator turn that is NOT a failure report.
        {"id": 2, "role": "operator", "kind": "note",
         "content": "It appears to be hung on the restart. How do we fix this?"},
        # The failure report, two turns later.
        {"id": 3, "role": "operator", "kind": "message",
         "content": "neither the first or the 'if that fails' worked"},
    ]
    db.execute = AsyncMock(return_value=rows)

    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T26", db=db)
    assert streak == 1
    assert "qm set 110 --delete hostpci0" in cmds

    # ...and the gate must actually BLOCK the re-prescription that followed.
    repeated_draft = (
        "## 👉 Do this next\n**Run this now:**\n"
        "```bash\nqm set 110 --delete hostpci0\n```\nthen tell me what it shows."
    )
    assert find_repeated_failed(repeated_draft, cmds) == [
        "qm set 110 --delete hostpci0"]


async def test_unreported_prescription_is_not_retired():
    """§17.916 must survive §17.930: a command the operator never ran and never
    complained about is NOT 'already tried'."""
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"id": 1, "role": "assistant", "kind": "fix",
         "content": "```bash\nqm start 110\n```"},
        {"id": 2, "role": "operator", "kind": "message",
         "content": "which VM id was that again?"},
    ]
    db.execute = AsyncMock(return_value=rows)
    _, cmds = await _fix_failure_streak(session_id="s", node_key="T26", db=db)
    assert cmds == ""


# ── §17.932 — every walkthrough states its finish line ─────────────────────


def test_live_closing_text_has_no_finish_line():
    """The actual close the operator saw for days is an instruction to REPORT,
    never one to ADVANCE."""
    assert has_done_criterion("then tell me what it shows.") is False


@pytest.mark.parametrize("text_out", [
    "## ✅ Done when\nYou see a login prompt.",
    "**Done when:** the service reports active",
    "### How you will know\nThe page loads.",
    "## Success looks like\nA shell prompt.",
])
def test_existing_finish_line_is_respected(text_out):
    """A model that phrased it itself must not be double-footered."""
    assert has_done_criterion(text_out) is True


def test_advance_footer_names_the_control_that_ends_the_step():
    foot = advance_footer("Install Ubuntu Server on the AI VM")
    assert "✓ Done → next step" in foot
    assert "`next`" in foot
    assert "Install Ubuntu Server on the AI VM" in foot
    # and it must tell them what to do when it did NOT work
    assert "paste what you see" in foot


def test_advance_footer_does_not_license_advancing_on_a_clean_exit_status():
    """§17.932 — the first cut defined done as "the command returned without an
    error", which is exactly the premature advance this whole arc is about: on
    the live T26 ("Install AI VM OS") `qm set 110 --hostpci0 …` returns cleanly
    in a second while the OS install is nowhere near finished. The footer must
    anchor on the step's GOAL and must never hand the operator a completion
    criterion the engine cannot actually check."""
    foot = advance_footer("Install AI VM OS")
    assert "NOT when the last command merely returned without an error" in foot
    assert "it is not the goal" in foot
    # the goal, not the command, is the gate
    assert "**Install AI VM OS** is actually true on your machine" in foot


def test_advance_footer_applied_and_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "assist_done_criterion_enabled", True)
    bare = "## 👉 Do this next\nRun it.\nthen tell me what it shows."
    once = _with_advance_footer(bare, "Some step")
    assert has_done_criterion(once)
    assert _with_advance_footer(once, "Some step") == once


def test_advance_footer_valve_off_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "assist_done_criterion_enabled", False)
    bare = "then tell me what it shows."
    assert _with_advance_footer(bare, "Some step") == bare


def test_advance_footer_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "assist_done_criterion_enabled", True)
    assert _with_advance_footer(None, "t") == ""
    assert _with_advance_footer("", "t") == ""


# ── §17.954 — a repeat that is not the IMMEDIATE action is not a cycle ─────
#
# Reconstructed from live session 613dd1df/T31 (2026-09-06 12:09), not invented.
# The fix was CORRECT: it read the real template filename out of the operator's
# own `pvesm list local` output and opened with a new `pct create …`. Its step 2
# re-listed `pct set 111 --net0 firewall=1`, which had failed exactly once,
# because container 111 did not exist yet — the thing step 1 creates. The banner
# told the operator to distrust a fix that was right.

_T31_FIX = """## 👉 Do this next

**Run this now:**
```bash
pct create 111 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst --hostname control-panel --rootfs local-lvm:8
```
then tell me what it shows.

## Diagnosis
The previous `pct create` used a generic template name instead of the exact
filename on disk.

## Fix
2. **Enable the Firewall:**
   ```bash
   pct set 111 --net0 firewall=1
   ```
3. **Start the Container:**
   ```bash
   pct start 111
   ```
"""


def test_a_downstream_prerequisite_repeat_is_not_flagged():
    hits = ["pct set 111 --net0 firewall=1"]
    assert _repeats_in_primary_action(_T31_FIX, hits) == []


def test_the_primary_action_is_still_flagged_when_it_repeats():
    """§17.906's live cost — `qm destroy 106 --purge` prescribed four times as
    the immediate action — must stay flagged. This narrows the gate, not the
    thing the gate was built for."""
    draft = (
        "## 👉 Do this next\n**Run this now:**\n"
        "```bash\nqm destroy 106 --purge\n```\nthen tell me what it shows.\n"
    )
    hits = ["qm destroy 106 --purge"]
    assert _repeats_in_primary_action(draft, hits) == ["qm destroy 106 --purge"]


def test_both_are_judged_independently_in_one_draft():
    """A draft whose immediate action repeats AND whose step 2 repeats keeps
    only the immediate one — the operator is warned about what they run now."""
    draft = _T31_FIX.replace(
        "pct create 111 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst "
        "--hostname control-panel --rootfs local-lvm:8",
        "qm destroy 106 --purge")
    hits = ["qm destroy 106 --purge", "pct set 111 --net0 firewall=1"]
    assert _repeats_in_primary_action(draft, hits) == ["qm destroy 106 --purge"]


def test_repeated_urls_are_never_demoted():
    """§17.883's version-guess skeleton match is the reason the URL half of this
    gate exists — a re-prescribed failing endpoint is wrong wherever it sits."""
    draft = _T31_FIX + "\n```bash\nwget https://example.test/r/v1.2.3/x.tar.gz\n```\n"
    hits = ["https://example.test/r/v1.2.3/x.tar.gz"]
    assert _repeats_in_primary_action(draft, hits) == hits


def test_an_unlocatable_action_stays_loud():
    """No fenced block means the immediate action can't be identified. Silence
    would be a gate that fails open, so the hits are kept."""
    hits = ["pct set 111 --net0 firewall=1"]
    assert _repeats_in_primary_action("just prose, no commands", hits) == hits
    assert _primary_action_corpus("just prose, no commands") == set()


def test_falls_back_to_the_first_fenced_block_without_a_callout():
    """Not every draft carries the §17.741 👉 heading; the first fenced block is
    the immediate action in that case."""
    draft = "**Run this now:**\n```bash\nqm destroy 106 --purge\n```\n"
    assert _primary_action_corpus(draft) == {"qm destroy 106 --purge"}


def test_the_gate_applies_the_scoping():
    """Pin the wiring, not just the helper — the demotion has to be reachable
    from the gate that builds the operator-facing banner."""
    import inspect

    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    assert "_repeats_in_primary_action" in src
