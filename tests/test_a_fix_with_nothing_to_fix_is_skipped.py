"""§17.1644 — a fix step that runs "only if" named checks found failures is skipped when every one passed.

Live (job 5db1b088 T4, 2026-10-18): "Only if T1/T2/T3 found failures: fix library types, paths, or series resolution
via Jellyfin API." T1–T3 had passed, and T4 was drafted anyway -- a POST re-adding the Movies library that was already
there.
"""
from __future__ import annotations

import inspect

from app.modules import conditional_steps as cs
from app.modules import execution_agent

T4 = {"node_key": "T4", "title": "Fix Jellyfin library issues", "description": "",
      "prompt_template": ("Only if T1/T2/T3 found failures: fix library types, paths, or series resolution via "
                          "Jellyfin API. In-place config changes only. Do NOT delete media.")}
#: the live plan's record when T4 was drafted
ROWS = [{"node_key": "T1", "status": "done"}, {"node_key": "T1v2", "status": "skipped"},
        {"node_key": "T2", "status": "done"}, {"node_key": "T2v2", "status": "done"},
        {"node_key": "T3", "status": "skipped"}, {"node_key": "T4", "status": "pending"}]


def _rows(**over):
    return [{**r, "status": over.get(r["node_key"], r["status"])} for r in ROWS]


def test_the_live_t4_has_nothing_to_fix():
    why = cs.unmet_condition(T4, ROWS)
    assert why and "T1 done" in why and "T2v2 done" in why and "T3 skipped" in why, why


def test_a_failed_check_or_a_failed_split_child_leaves_it_to_its_draft():
    assert cs.unmet_condition(T4, _rows(T3="failed")) is None
    assert cs.unmet_condition(T4, _rows(T2v2="failed")) is None      # "T2" carries its split child


def test_an_unsettled_or_unknown_check_leaves_it_to_its_draft():
    assert cs.unmet_condition(T4, _rows(T2="pending")) is None
    assert cs.unmet_condition(T4, [r for r in ROWS if r["node_key"] != "T3"]) is None
    every_skipped = _rows(T1="skipped", T2="skipped", T2v2="skipped")
    assert cs.unmet_condition(T4, every_skipped) is None             # nothing actually checked


def test_the_condition_shapes_it_reads_and_the_steps_it_leaves_alone():
    assert cs.named_checks({"prompt_template": "Only if T6 found failures: fix or disable failing indexers"}) == ["T6"]
    assert cs.named_checks({"description": "only if T8, T10 or T12 report problems, fix them"}) == ["T8", "T10", "T12"]
    for text in ("Fix control panel API routes.", "Fix the indexers only if needed", "If T6 found failures, fix them",
                 "Only if the operator agrees: restart Jellyfin"):
        assert cs.named_checks({"prompt_template": text}) == [], text


def test_wired_before_the_draft():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i, j = src.index("unmet_condition(run_node, _plan_rows)"), src.index("from app.modules import develop\n")
    assert i < j and "SET status = 'skipped'" in src[i:j] and "return await _pause_for_decision(job_id, _depth + 1)" in src[i:j]


# ── §17.1661: a skipped check carries the operator's verdict ─────────────────

T25 = {"node_key": "T25", "title": "Fix shared storage paths",
       "prompt_template": "Only if T24 found failures: fix mount points or path mappings."}


def test_a_skip_with_a_passed_verdict_settles_the_fix():
    rows = [{"node_key": "T24", "status": "skipped", "last_verification_reason": "skipped by the operator -- verdict passed"}]
    assert cs.unmet_condition(T25, rows)


def test_a_failed_verdict_or_no_verdict_leaves_it_to_its_draft():
    failed = [{"node_key": "T24", "status": "skipped", "last_verification_reason": "skipped by the operator -- verdict failed"}]
    bare = [{"node_key": "T24", "status": "skipped", "last_verification_reason": None}]
    assert cs.unmet_condition(T25, failed) is None and cs.unmet_condition(T25, bare) is None


def test_the_verdict_reaches_the_record():
    import inspect
    from app.modules import supervised_runs as sr
    from app.routers import jobs
    assert "verdict=body.verdict" in inspect.getsource(jobs)
    src = inspect.getsource(sr.resolve_run)
    assert "last_verification_reason = COALESCE(:why, last_verification_reason)" in src and "verdict {_verdict}" in src
