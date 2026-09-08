"""§17.973 — what the fix path already tried, and what is left.

Operator: *"a larger issue with the fixing component … should research
troubleshooting possibilities and have its own record knowing what was tried or
not as well as what is left."*

Measured before writing any of it: T31–T35 produced 54 fix turns, 100% carrying
a `## Diagnosis` section, and EVERY committed step recorded `ruled_out=0`. The
engine states a cause, tests it, watches it fail, and forgets it.
"""
import inspect

import pytest

from app.modules.assist_hypotheses import (
    extract_diagnosis,
    find_retested_hypothesis,
    harvest,
    render_tested_hypotheses,
)

# The real T34 sequence, trimmed to its Diagnosis claims.
_T34 = [
    "## Diagnosis\nThe previous command failed because the shell tried to "
    "interpret the `${}` symbols inside the code as system variables.\n## Fix\nx",
    "## Diagnosis\nThe `cat` commands I gave you were truncated (cut off) by "
    "the system before they finished sending.\n## Fix\nx",
    "## Diagnosis\nThe browser is blank because the `App.jsx` file is "
    "corrupted.\n## Fix\nx",
    "## Diagnosis\nThe `App.jsx` file is actually complete and syntactically "
    "correct based on the `cat` output you provided.\n## Fix\nx",
    "## Diagnosis\nThe `App.jsx` file is corrupted or incomplete.\n## Fix\nx",
]


def _reply(diag):
    return f"## 👉 Do this next\n```bash\nls\n```\n## Diagnosis\n{diag}\n## Fix\nx"


# ── reading the engine's own record ───────────────────────────────────────


def test_the_diagnosis_section_is_extracted():
    assert extract_diagnosis(_T34[2]) == (
        "The browser is blank because the `App.jsx` file is corrupted.")


def test_only_the_claim_is_kept_not_the_elaboration():
    r = _reply("The file is truncated. This happened because the terminal "
               "dropped characters during the paste.")
    assert extract_diagnosis(r) == "The file is truncated."


def test_a_reply_without_a_diagnosis_yields_nothing():
    assert extract_diagnosis("## 👉 Do this next\n```bash\nls\n```") == ""
    assert extract_diagnosis("") == ""


# ── eliminated vs still under test ────────────────────────────────────────


def test_everything_but_the_newest_is_eliminated():
    """If a cause was diagnosed and another fix followed, it did not resolve
    the step — the same reasoning §17.881 uses for its failure streak."""
    led = harvest(_T34)
    assert len(led["eliminated"]) == 4
    assert led["current"] == "The `App.jsx` file is corrupted or incomplete."


def test_a_single_fix_eliminates_nothing():
    led = harvest([_T34[0]])
    assert led["eliminated"] == []
    assert led["current"].startswith("The previous command failed")


def test_identical_repeats_collapse():
    led = harvest([_reply("The file is corrupt.")] * 3)
    assert led["eliminated"] == [] and led["current"] == "The file is corrupt."


def test_no_fixes_is_empty_not_an_error():
    assert harvest([]) == {"eliminated": [], "current": ""}
    assert harvest(None) == {"eliminated": [], "current": ""}


# ── the gate: re-diagnosing a closed cause ────────────────────────────────


def test_the_live_re_diagnosis_is_caught():
    """T34 diagnosed "App.jsx is corrupted", concluded it was fine, then
    diagnosed it corrupted again. That third turn is the one to stop."""
    led = harvest(_T34[:4])
    hits = find_retested_hypothesis(_T34[4], led)
    assert hits
    assert "corrupted" in hits[0]["previous"]
    assert "corrupted" in hits[0]["shared"]


def test_the_opposite_claim_about_the_same_file_is_not_a_retest():
    """The pair that sets the threshold: "App.jsx is CORRUPTED" vs "App.jsx is
    actually COMPLETE and correct" share only the identifiers app/file/jsx. A
    gate that flagged this would stop the engine concluding the file was fine —
    which is the one conclusion that actually moved T34 forward."""
    led = harvest(_T34[:3])
    assert find_retested_hypothesis(_T34[3], led) == []


def test_a_genuinely_new_cause_passes():
    led = harvest(_T34)
    fresh = _reply("The backend process is missing from PM2, so the frontend "
                   "receives no response from the status endpoint.")
    assert find_retested_hypothesis(fresh, led) == []


def test_a_short_diagnosis_is_never_matched():
    """Too few content words for overlap to mean anything."""
    led = harvest([_reply("The disk is full and the writes are failing now")])
    assert find_retested_hypothesis(_reply("It broke"), led) == []


def test_no_ledger_means_no_findings():
    assert find_retested_hypothesis(_T34[0], None) == []
    assert find_retested_hypothesis(_T34[0], {"eliminated": []}) == []


# ── what the model is actually shown ──────────────────────────────────────


def test_the_block_lists_the_closed_causes_and_demands_what_is_left():
    block = render_tested_hypotheses(harvest(_T34))
    assert "ALREADY TESTED ON THIS STEP AND ELIMINATED (4)" in block
    assert "browser is blank" in block
    assert "what is LEFT" in block
    assert "UPSTREAM" in block
    assert "this session itself created" in block


def test_nothing_eliminated_renders_nothing():
    assert render_tested_hypotheses(harvest([_T34[0]])) == ""
    assert render_tested_hypotheses(None) == ""


# ── wiring ────────────────────────────────────────────────────────────────


def test_the_ledger_reaches_the_prompt_and_the_gate():
    from app.modules import assist_guide

    assert "hypotheses" in inspect.signature(assist_guide.generate_fix).parameters
    src = inspect.getsource(assist_guide.generate_fix)
    assert "render_tested_hypotheses" in src      # the model sees it
    assert "find_retested_hypothesis" in src      # and cannot ignore it


def test_the_fix_path_derives_it_from_the_turns():
    """Derived on read, never stored — a second copy would drift from what
    actually happened."""
    from app.modules import assist_agent

    src = inspect.getsource(assist_agent)
    assert "assist_hypotheses import harvest" in src
    assert "kind = 'fix'" in src


# ── §17.974 — the ledger has to reach RESEARCH, not just the prompt ───────
#
# Verified before writing it: the fix prepass was fed the step prompt plus the
# operator's latest error, and only once escalated a generic "previous fixes did
# not resolve it". It never named WHICH causes were eliminated, so every turn
# re-grounded on the same symptom. That is why T34 re-diagnosed "App.jsx is
# corrupted" four times — the research behind each of those fixes was asking the
# same question.


def test_the_eliminated_causes_reach_the_research_prepass():
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    assert "_elim_block" in src
    # Built from the ledger, and consumed by the prepass that generates queries.
    assert src.index("_elim = list") < src.index("_research_prepass(")
    seg = src[src.index("_elim = list"):src.index("_research_prepass(")]
    assert "hypotheses" in seg
    assert "do NOT" in seg and "research these again" in seg


def test_research_is_pointed_at_what_remains():
    """Naming what is closed is only half — the query generator has to be told
    where to look instead."""
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    seg = src[src.index("_elim = list"):src.index("_research_prepass(")]
    assert "NOT in that list" in seg
    assert "UPSTREAM" in seg
    assert "session itself created" in seg


def test_the_prepass_still_carries_the_error_and_the_escalation_note():
    """§17.909 (research the operator's symptom) and §17.881 (escalation) must
    both survive — this adds to the task text, it does not replace it."""
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    i = src.index("_research_prepass(")
    window = src[i:i + 700]
    assert "Operator hit this error" in window
    assert "REPEATED failure" in window
    assert "_elim_block" in window


def test_fix_research_is_recorded():
    """§17.974b — generate_fix returns research_sources in guidance_meta and the
    caller drops it; assist_steps.guidance_meta holds the GUIDE's sources (empty
    on T34). 54 fix turns left no trace of what any of them looked up, so
    §17.909's triage instruction had nothing to read."""
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    assert "assist_fix_research" in src
    i = src.index("assist_fix_research")
    window = src[i:i + 260]
    assert "node_key" in window and "queries" in window
    assert "eliminated_known" in window


# ── §17.977 — the project, not just the node ─────────────────────────────
#
# Operator: *"is there a way to fix the older logs … correcting the OVERALL
# project instead of just the singular node."*
#
# Measured: harvesting every node's fix turns in the live session recovers 124
# eliminated causes across 19 nodes. The project playbook holds 3. There is no
# migration because these are DERIVED on read — every existing session recovers
# its whole history the moment the code ships.

from app.modules.assist_hypotheses import (  # noqa: E402
    cross_step_eliminated,
    harvest_session,
    render_cross_step_eliminated,
)

_ROWS = [
    ("T33", _reply("The heredoc terminator never arrived.")),
    ("T33", _reply("The file was written to the wrong path entirely.")),
    ("T34", _reply("The App.jsx file is corrupted beyond recovery.")),
    ("T34", _reply("The backend process is missing from the PM2 manager.")),
]


def test_every_node_is_harvested_not_just_the_current_one():
    led = harvest_session(_ROWS)
    assert set(led) == {"T33", "T34"}
    assert len(led["T33"]["eliminated"]) == 1
    assert len(led["T34"]["eliminated"]) == 1


def test_the_current_step_is_excluded_from_its_own_cross_step_view():
    """The same-step ledger already covers it, and it is the half that binds."""
    pairs = cross_step_eliminated(harvest_session(_ROWS), "T34")
    assert pairs == [("T33", "The heredoc terminator never arrived.")]


def test_no_current_node_still_works():
    assert len(cross_step_eliminated(harvest_session(_ROWS), None)) == 2


def test_rows_without_a_node_are_ignored():
    rows = _ROWS + [(None, _reply("Something with no step attached at all."))]
    assert set(harvest_session(rows)) == {"T33", "T34"}


def test_the_cross_step_view_is_bounded():
    rows = [(f"T{i}", _reply(f"Cause number {i} which failed on that step."))
            for i in range(40)]
    rows += [(f"T{i}", _reply(f"Second cause {i} on that same step.")) for i in range(40)]
    assert len(cross_step_eliminated(harvest_session(rows), "T1")) <= 12


def test_it_is_framed_as_context_and_never_as_a_prohibition():
    """The design decision this entry turns on: an eliminated CAUSE is not a
    ruled-out METHOD. "App.jsx is corrupted" was disproved on T34 and would be
    actively wrong as a standing project-wide ban — the file WAS corrupted twice
    before it wasn't."""
    block = render_cross_step_eliminated(
        cross_step_eliminated(harvest_session(_ROWS), "T34"))
    assert "context, not a rule" in block
    assert "does NOT mean it cannot be the cause here" in block
    assert "[T33]" in block                       # attributed to its step
    # No prohibition language — that belongs to the same-step ledger alone.
    assert "Do NOT propose" not in block


def test_nothing_disproved_elsewhere_renders_nothing():
    assert render_cross_step_eliminated([]) == ""
    assert render_cross_step_eliminated(None) == ""


def test_the_same_step_ledger_still_comes_first_and_still_binds():
    """Ordering is the safety property: the binding list is read before the
    informational one, and the trailer stays last."""
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    i = src.index("parts.append(_hyp_block)")
    j = src.index("parts.append(_cross_block)")
    assert i < j < src.index("_FIX_USER_TRAILER")


def test_the_project_view_is_derived_from_history_not_stored():
    """The 'update path' is that there isn't one — no migration, no backfill,
    and no stored copy that can disagree with the transcript."""
    from app.modules import assist_agent

    src = inspect.getsource(assist_agent)
    assert "harvest_session" in src
    i = src.index("harvest_session")
    window = src[i - 900:i + 200]
    assert "FROM assist_turns" in window
    assert "node_key IS NOT NULL" in window
