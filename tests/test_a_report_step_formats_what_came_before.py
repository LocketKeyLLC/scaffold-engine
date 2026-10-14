r"""§17.1565 — a report/format step consumes the upstream results; it does not redo the job in prose.

Live (job 263e920a, T8 "Produce missing metadata report", the final deliverable, depends_on T7/T7v2/T7v3): with
the job goal ("run a full metadata and image refresh … verify … report") in its context the step took the goal
as its task, wrote "## Required operator actions" with six commands for container 101, and called the census it
had been handed a "pre-refresh baseline". §17.1259 refused it as instructions-not-work, three retries, and the
job blocked with the report's every input in hand.
"""
from __future__ import annotations

from app.modules import execution_agent as ea

BRIEF = {"description": "Run a full metadata and image refresh for all Jellyfin libraries on container 101, wait "
                        "for the scan, then verify every item has a Name, Overview and Primary image and report any that lack them."}
T8 = {"node_key": "T8", "title": "Produce missing metadata report", "tool": "LLM", "depends_on": ["T7", "T7v2", "T7v3"],
      "prompt_template": "Format the verification results into a human-readable report. This is the final deliverable."}


def test_a_report_step_with_upstream_is_told_its_inputs_already_happened():
    p = ea._build_prompt(T8, BRIEF)
    assert "THIS STEP FORMATS WHAT CAME BEFORE" in p
    assert "Do not write commands for anyone to run" in p and "do not doubt that the upstream steps ran" in p


def test_a_step_with_no_upstream_or_that_does_work_gets_no_such_block():
    first = {**T8, "depends_on": []}
    assert "FORMATS WHAT CAME BEFORE" not in ea._build_prompt(first, BRIEF)
    doer = {"node_key": "T4", "title": "Trigger metadata refresh", "tool": "LLM", "depends_on": ["T2"],
            "prompt_template": "POST refresh with ReplaceAllMetadata=true for each library."}
    assert "FORMATS WHAT CAME BEFORE" not in ea._build_prompt(doer, BRIEF)
    shell = {**T8, "tool": "Shell"}
    assert ea.formats_what_came_before(shell) == ""


def test_upstream_sections_are_ordered_and_stamped_by_when_they_finished():
    """§17.1566 — live, the report called T7v3 'the most recent pass' by its KEY while T7 had run an hour
    later, after the library rebuild; the block now orders by completed_at and says the later one wins."""
    from datetime import datetime, timezone
    up = {"T7": ("Shows/Episode: 902, overview 0, poster 279", 0.9),
          "T7v2": ("Shows/Movie: 902, overview 902, poster 321", 0.9),
          "T7v3": ("Shows/Movie: 902, overview 902, poster 321", 0.9)}
    fin = {"T7": datetime(2026, 10, 14, 22, 6, tzinfo=timezone.utc),
           "T7v2": datetime(2026, 10, 14, 20, 43, tzinfo=timezone.utc),
           "T7v3": datetime(2026, 10, 14, 20, 44, tzinfo=timezone.utc)}
    block = ea._format_upstream_block(up, "T8", finished=fin)
    assert block.index("### T7v2") < block.index("### T7v3") < block.index("### T7 ")
    assert "### T7 (confidence: 0.90) (finished 2026-10-14 22:06 UTC)" in block
    assert "order they FINISHED, oldest first" in block and "the later one is the current state" in block
    # without times: plan order, no claim about recency
    plain = ea._format_upstream_block(up, "T8")
    assert "FINISHED" not in plain and plain.index("### T7 ") < plain.index("### T7v2")


def test_the_upstream_block_says_the_results_already_happened():
    block = ea._format_upstream_block({"T7": ("Shows/Episode: 902, missing overview 0, poster 279\nRESULT: 281 item(s)", 0.9)}, "T8")
    assert "ALREADY RAN before this one" in block and "a RESULT the machine produced, not a plan" in block
    assert "### T7 (confidence: 0.90)" in block and "## YOUR TASK" in block
