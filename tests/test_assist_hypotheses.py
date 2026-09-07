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
