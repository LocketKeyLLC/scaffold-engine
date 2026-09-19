"""§17.1107 — job_state.transition(): the one guarded job-status write.

Phase 1 ledger S-2..S-5. Unit tests drive the statement builder with a fake
session (same shape as test_execution_resume); the ratchet test at the bottom
counts raw ``UPDATE jobs … status =`` sites outside ``job_state.py`` and only
ever lets that number go DOWN.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.modules import job_state
from app.modules.job_state import (
    ERROR_SUMMARY_MAX,
    JOB_STATUSES,
    NODE_STATUSES,
    TERMINAL_JOB_STATUSES,
    sql_status_list,
    transition,
)

JID = "00000000-0000-0000-0000-0000000000aa"


class FakeSession:
    """Records every (sql, params); ``first_row`` is what the UPDATE returns,
    ``current`` is what the refusal SELECT returns."""

    def __init__(self, first_row=("running",), current="cancelled"):
        self.calls: list[tuple[str, dict]] = []
        self.first_row = first_row
        self.current = current

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
        outer = self

        class R:
            def first(self_inner):
                return outer.first_row

            def scalar(self_inner):
                return outer.current

        return R()

    async def commit(self):
        self.calls.append(("COMMIT", {}))


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


# ── vocabulary ───────────────────────────────────────────────────────────────

def test_vocabulary_matches_the_schema_literal():
    """schemas.JobStatus is the API's view of the same 16 values."""
    from typing import get_args
    from app.schemas import JobStatus
    assert set(get_args(JobStatus)) == JOB_STATUSES
    assert TERMINAL_JOB_STATUSES <= JOB_STATUSES
    assert "blocked" not in TERMINAL_JOB_STATUSES, "blocked is re-enterable (retry/reopen/assist)"
    assert NODE_STATUSES == {"pending", "running", "done", "failed", "skipped"}


def test_sql_status_list_validates_and_sorts():
    assert sql_status_list(["failed", "completed"]) == "'completed', 'failed'"
    with pytest.raises(ValueError):
        sql_status_list(["completed", "blocked'; DROP TABLE jobs; --"])
    with pytest.raises(ValueError):
        sql_status_list([])
    with pytest.raises(ValueError):
        sql_status_list(["done"])                      # node word, job vocabulary
    assert sql_status_list(["done"], vocabulary=NODE_STATUSES) == "'done'"


# ── transition ───────────────────────────────────────────────────────────────

async def test_default_guard_refuses_terminal_rows_in_sql():
    db = FakeSession(first_row=("planning",))
    prior = await transition(db, JID, to="executing", reason="t")
    sql, params = db.calls[0]
    one = _one_line(sql)
    assert prior == "planning"
    assert "SET status = :to, updated_at = NOW()" in one
    assert "jobs.status NOT IN ('cancelled', 'completed', 'failed')" in one
    assert "FOR UPDATE" in one, "prior status must be read under the row lock"
    assert "RETURNING prior.prior_status" in one
    assert params == {"id": JID, "to": "executing"}
    assert len(db.calls) == 1, "success path is exactly one statement"


async def test_expected_from_renders_an_in_list():
    db = FakeSession(first_row=("running",))
    await transition(db, JID, to="failed", expected_from=("running",))
    assert "jobs.status IN ('running')" in _one_line(db.calls[0][0])


async def test_allow_terminal_drops_the_guard():
    db = FakeSession(first_row=("completed",))
    await transition(db, JID, to="executing", allow_terminal=True)
    one = _one_line(db.calls[0][0])
    assert "NOT IN" not in one and "AND TRUE" in one


async def test_set_columns_are_whitelisted_and_ride_the_same_statement():
    db = FakeSession(first_row=("planning",))
    await transition(
        db, JID, to="executing", set_columns={"dag_input_hash": "abc", "title": "T"},
    )
    one, params = _one_line(db.calls[0][0]), db.calls[0][1]
    assert "dag_input_hash = :set_dag_input_hash" in one and "title = :set_title" in one
    assert params["set_dag_input_hash"] == "abc" and params["set_title"] == "T"
    with pytest.raises(ValueError):
        await transition(FakeSession(), JID, to="executing", set_columns={"owner": "x"})


async def test_error_summary_is_truncated():
    db = FakeSession(first_row=("running",))
    await transition(db, JID, to="failed", set_columns={"error_summary": "x" * 5000})
    assert len(db.calls[0][1]["set_error_summary"]) == ERROR_SUMMARY_MAX


async def test_unknown_target_status_raises_before_any_sql():
    db = FakeSession()
    with pytest.raises(ValueError):
        await transition(db, JID, to="finished")
    assert db.calls == []


async def test_refusal_returns_none_and_logs_the_current_status(caplog):
    db = FakeSession(first_row=None, current="cancelled")
    with caplog.at_level(logging.WARNING, logger="scaffold.job_state"):
        prior = await transition(db, JID, to="failed", reason="fail_job")
    assert prior is None
    assert len(db.calls) == 2 and "SELECT status FROM jobs" in db.calls[1][0]
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "job_transition_refused" in msg and "current=cancelled" in msg and "reason=fail_job" in msg


# ── the migrated callers (S-2 / S-4 / S-5) ───────────────────────────────────

async def test_fail_job_refuses_a_cancelled_job_and_reports_it():
    from app.utils.job_utils import fail_job
    db = FakeSession(first_row=None, current="cancelled")
    moved = await fail_job(db, JID, "late DAG failure")
    assert moved is False
    assert "NOT IN ('cancelled', 'completed', 'failed')" in _one_line(db.calls[0][0])
    assert db.calls[-1][0] == "COMMIT"


async def test_fail_job_moves_a_live_job():
    from app.utils.job_utils import fail_job
    db = FakeSession(first_row=("planning",))
    assert await fail_job(db, JID, "boom") is True
    assert db.calls[0][1]["set_error_summary"] == "boom"


async def test_set_node_status_accepts_a_tuple_guard_and_logs_refusal(caplog):
    import app.modules.execution_agent as ea

    class NodeSession:
        def __init__(self):
            self.calls = []

        async def execute(self, stmt, params=None):
            self.calls.append((str(stmt), dict(params or {})))

            class R:
                def fetchone(self_inner):
                    return None
            return R()

        async def commit(self):
            self.calls.append(("COMMIT", {}))

    db = NodeSession()
    with caplog.at_level(logging.WARNING, logger="scaffold"):
        ok = await ea._set_node_status(db, "n1", "skipped", expected_status=("pending", "running", "failed"))
    assert ok is False
    one = _one_line(db.calls[0][0])
    assert "AND status IN ('failed', 'pending', 'running')" in one
    assert "expected" not in db.calls[0][1], "tuple guard is inlined, no :expected bind"
    assert "node_status_write_refused" in " ".join(r.getMessage() for r in caplog.records)


# ── the ratchet ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
RAW_STATUS_WRITE = re.compile(
    r"UPDATE\s+jobs\b(?:(?!\bWHERE\b)[\s\S]){0,400}?\bstatus\s*=\s*(?:'|:)"
)
# 2026-09-18 §17.1107: 37 sites → 28 after S-2..S-5. This number only goes DOWN.
# A new raw `UPDATE jobs … SET status` belongs in job_state.transition().
RAW_STATUS_WRITE_CEILING = 27   # §17.1119: design_pipeline._set_job_status migrated


def _raw_sites() -> dict[str, int]:
    out: dict[str, int] = {}
    for f in sorted((ROOT / "app").rglob("*.py")):
        if f.name == "job_state.py":
            continue
        n = len(RAW_STATUS_WRITE.findall(f.read_text(encoding="utf-8")))
        if n:
            out[str(f.relative_to(ROOT))] = n
    return out


def test_ratchet_regex_hits_every_raw_shape_and_ignores_payload_only_updates():
    hits = [
        "UPDATE jobs SET status = 'failed', error_summary = :e WHERE id = :id",
        "UPDATE jobs\n   SET status = :s,\n       updated_at = NOW()\n WHERE id = :jid",
        'text("""\n  UPDATE jobs\n  SET title = :t,\n      refined_brief = :b,\n      status = :target\n  WHERE id = :id\n""")',
    ]
    for h in hits:
        assert RAW_STATUS_WRITE.search(h), h
    assert not RAW_STATUS_WRITE.search("UPDATE jobs SET metadata = :m, updated_at = NOW() WHERE id = :id")
    assert not RAW_STATUS_WRITE.search("UPDATE jobs SET compiled_output = :c WHERE id = :id AND status = 'running'")


def test_raw_job_status_writes_only_go_down():
    sites = _raw_sites()
    total = sum(sites.values())
    assert total <= RAW_STATUS_WRITE_CEILING, (
        f"{total} raw job-status writes (ceiling {RAW_STATUS_WRITE_CEILING}). New status writes go "
        f"through app.modules.job_state.transition(); if you migrated some, LOWER the ceiling. Sites: {sites}"
    )


def test_ratchet_ceiling_is_tight():
    """When sites are migrated the ceiling must follow, or the ratchet stops biting."""
    total = sum(_raw_sites().values())
    assert total == RAW_STATUS_WRITE_CEILING, (
        f"raw sites = {total}, ceiling = {RAW_STATUS_WRITE_CEILING}; set the ceiling to {total}"
    )
