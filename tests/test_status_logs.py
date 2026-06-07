"""Tests for /status and /logs/{job_id} endpoints.

Uses importlib.util pattern for Docker compatibility (WORKDIR /app conflict).
Run: pytest tests/test_status_logs.py -v
"""

import importlib.util
import json
import pathlib
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# ── Bootstrap: stub out app.database + app.modules.recovery before
# loading the router ───────────────────────────────────────────────────

# Create fake app.database module so the router can import from it
_app_pkg = types.ModuleType("app")
_app_pkg.__path__ = []
sys.modules.setdefault("app", _app_pkg)

_db_mod = types.ModuleType("app.database")
_db_mod.get_db = lambda: None  # placeholder
sys.modules.setdefault("app.database", _db_mod)

# Stub app.modules.recovery only when the real module isn't importable.
# Tests run from the project root in the docker dev image have `app` as a
# real package, so the real recovery module loads cleanly. Standalone runs
# (no app on sys.path) get a stub. Either way: don't shadow the real
# module with a half-stub or you'll break test_recovery.py via a half-
# populated `app.modules.recovery` in sys.modules.
try:
    from app.modules.recovery import next_actions_for as _real_next_actions_for  # noqa: F401
except ImportError:
    _modules_pkg = types.ModuleType("app.modules")
    _modules_pkg.__path__ = []
    sys.modules["app.modules"] = _modules_pkg

    _recovery_mod = types.ModuleType("app.modules.recovery")
    _recovery_mod.NEXT_ACTIONS = {}
    _recovery_mod.next_actions_for = lambda status, job_id, **kw: [
        {"action": "stub", "command": f"/test {job_id}",
         "endpoint": None, "method": None, "description": "stubbed",
         "node_specific": False},
    ]
    _recovery_mod.all_known_statuses = lambda: ()
    sys.modules["app.modules.recovery"] = _recovery_mod

# Also stub structlog if not installed in test env
try:
    import structlog  # noqa: F401
except ImportError:
    _structlog = types.ModuleType("structlog")
    _fake_logger = MagicMock()
    _structlog.get_logger = lambda: _fake_logger
    sys.modules["structlog"] = _structlog

# ── Load the router module via importlib ───────────────────────────────

_router_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "routers" / "status.py"
_spec = importlib.util.spec_from_file_location("status_router", _router_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_status = _mod.get_status
get_logs = _mod.get_logs
StatusResponse = _mod.StatusResponse
LogsResponse = _mod.LogsResponse
StatusCounts = _mod.StatusCounts
JobSummary = _mod.JobSummary
NodeLog = _mod.NodeLog


# ── Fixtures ───────────────────────────────────────────────────────────


def _make_row(**kwargs):
    """Create a mock DB row with attribute access."""
    # §17.445 — get_logs now SELECTs last_verification_reason; default it so a
    # MagicMock attribute (which would fail NodeLog's str|None) isn't auto-made.
    kwargs.setdefault("last_verification_reason", None)
    obj = MagicMock()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _make_db(execute_side_effects):
    """Create a mock async DB session.

    execute_side_effects: list of return values for successive db.execute() calls.
    Each value should be a MagicMock with .first() or iteration support.
    """
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effects)
    db.commit = AsyncMock()
    return db


def _make_result(rows):
    """Create a mock result that iterates over rows."""
    result = MagicMock()
    result.__iter__ = lambda self: iter(rows)
    # New COUNT(*) call in get_logs expects .scalar() -> int
    result.scalar = MagicMock(return_value=len(rows))
    result.first = lambda: rows[0] if rows else None
    return result


def _make_scalar_result(value):
    """Create a mock result with .scalar() returning a value."""
    result = MagicMock()
    result.scalar = lambda: value
    return result


# ── Tests: GET /status ─────────────────────────────────────────────────


class TestGetStatus:

    @pytest.mark.asyncio
    async def test_returns_status_counts(self):
        """Status counts are populated from GROUP BY query."""
        count_rows = [
            _make_row(status="completed", cnt=10),
            _make_row(status="failed", cnt=3),
            _make_row(status="planning", cnt=1),
        ]
        jobs_rows = [
            _make_row(
                id="abc-123",
                title="A completed job",
                status="completed",
                node_count=4,
                created_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc),
            ),
        ]

        db = _make_db([_make_result(count_rows), _make_result(jobs_rows)])

        resp = await get_status(limit=20, status_filter=None, db=db)

        assert isinstance(resp, StatusResponse)
        assert resp.status_counts.completed == 10
        assert resp.status_counts.failed == 3
        assert resp.status_counts.planning == 1
        assert resp.status_counts.running == 0  # not in result → defaults to 0
        assert resp.total_jobs == 14

    @pytest.mark.asyncio
    async def test_recent_jobs_populated(self):
        """Recent jobs list contains correct fields."""
        count_rows = [_make_row(status="completed", cnt=5)]
        jobs_rows = [
            _make_row(
                id="job-1",
                title="First job",
                status="completed",
                node_count=3,
                created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
            ),
            _make_row(
                id="job-2",
                title="Second job",
                status="failed",
                node_count=5,
                created_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
            ),
        ]

        db = _make_db([_make_result(count_rows), _make_result(jobs_rows)])

        resp = await get_status(limit=20, status_filter=None, db=db)

        assert len(resp.recent_jobs) == 2
        assert resp.recent_jobs[0].id == "job-1"
        assert resp.recent_jobs[0].title == "First job"
        assert resp.recent_jobs[0].node_count == 3
        assert resp.recent_jobs[1].id == "job-2"
        assert resp.recent_jobs[1].title == "Second job"

    @pytest.mark.asyncio
    async def test_recent_jobs_carry_title_and_next_actions(self):
        """Sprint U.7: every recent job exposes its human title and a populated
        next_actions list. The endpoint used to return bare UUIDs; this asserts
        the regression fix for the visible UX gap."""
        count_rows = [_make_row(status="awaiting_confirmation", cnt=1)]
        jobs_rows = [
            _make_row(
                id="job-with-title",
                title="Build a markdown linter",
                status="awaiting_confirmation",
                node_count=0,
                created_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
                updated_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
            ),
        ]
        db = _make_db([_make_result(count_rows), _make_result(jobs_rows)])

        resp = await get_status(limit=20, status_filter=None, db=db)

        assert resp.recent_jobs[0].title == "Build a markdown linter"
        assert resp.recent_jobs[0].next_actions, \
            "next_actions must be populated; the recovery registry stub returns at least one"
        first = resp.recent_jobs[0].next_actions[0]
        assert "command" in first or "endpoint" in first

    @pytest.mark.asyncio
    async def test_null_title_renders_empty_string(self):
        """A job with NULL title (legacy data) returns '' rather than crashing."""
        count_rows = [_make_row(status="completed", cnt=1)]
        jobs_rows = [
            _make_row(
                id="legacy",
                title=None,
                status="completed",
                node_count=0,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ]
        db = _make_db([_make_result(count_rows), _make_result(jobs_rows)])

        resp = await get_status(limit=20, status_filter=None, db=db)
        assert resp.recent_jobs[0].title == ""

    @pytest.mark.asyncio
    async def test_empty_jobs_table(self):
        """No jobs returns zero counts and empty list."""
        db = _make_db([_make_result([]), _make_result([])])

        resp = await get_status(limit=20, status_filter=None, db=db)

        assert resp.total_jobs == 0
        assert resp.recent_jobs == []
        assert resp.status_counts.completed == 0

    @pytest.mark.asyncio
    async def test_status_filter_passed_to_query(self):
        """When status filter is provided, it's included in the query params."""
        db = _make_db([_make_result([]), _make_result([])])

        await get_status(limit=10, status_filter="failed", db=db)

        # Second call is the jobs query — check params include filter
        call_args = db.execute.call_args_list[1]
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
        assert params.get("status_filter") == "failed"

    @pytest.mark.asyncio
    async def test_timestamp_is_utc_iso(self):
        """Response timestamp is valid ISO format."""
        db = _make_db([_make_result([]), _make_result([])])

        resp = await get_status(limit=20, status_filter=None, db=db)

        # Should parse without error
        parsed = datetime.fromisoformat(resp.timestamp)
        assert parsed.tzinfo is not None

    @pytest.mark.asyncio
    async def test_null_timestamps_handled(self):
        """Jobs with NULL created_at/updated_at don't crash."""
        count_rows = [_make_row(status="planning", cnt=1)]
        jobs_rows = [
            _make_row(
                id="55555555-5555-4555-8555-555555555555",
                title="planning-job",
                status="planning",
                node_count=0,
                created_at=None,
                updated_at=None,
            ),
        ]

        db = _make_db([_make_result(count_rows), _make_result(jobs_rows)])

        resp = await get_status(limit=20, status_filter=None, db=db)

        assert resp.recent_jobs[0].created_at is None
        assert resp.recent_jobs[0].updated_at is None


# ── Tests: GET /logs/{job_id} ──────────────────────────────────────────


class TestGetLogs:

    @pytest.mark.asyncio
    async def test_returns_node_details(self):
        """Logs endpoint returns per-node execution details."""
        job_row = _make_row(status="completed", compiled_output="Final result here")
        node_rows = [
            _make_row(
                node_key="T1",
                title="Research",
                tool="Milvus",
                status="done",
                domain="eng",
                output_text="Found 5 results about engineering",
                confidence=None,
                updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            ),
            _make_row(
                node_key="T2",
                title="Synthesize",
                tool="LLM",
                status="done",
                domain=None,
                output_text="Synthesis of findings...",
                confidence=None,
                updated_at=datetime(2026, 4, 4, 10, 5, tzinfo=timezone.utc),
            ),
        ]

        job_result = _make_result([job_row])
        nodes_result = _make_result(node_rows)
        count_result = _make_result(node_rows)
        db = _make_db([job_result, count_result, nodes_result])

        resp = await get_logs(job_id="11111111-1111-4111-8111-111111111111", include_output=False, include_compiled=True, db=db, limit=100, offset=0)

        assert isinstance(resp, LogsResponse)
        assert resp.job_id == "11111111-1111-4111-8111-111111111111"
        assert resp.job_status == "completed"
        assert resp.node_count == 2
        assert resp.compiled_output == "Final result here"
        assert resp.nodes[0].node_key == "T1"
        assert resp.nodes[0].tool == "Milvus"
        assert resp.nodes[0].domain == "eng"
        assert resp.nodes[1].node_key == "T2"

    @pytest.mark.asyncio
    async def test_job_not_found(self):
        """Returns 404 HTTPException when job_id doesn't exist."""
        empty_result = _make_result([])
        db = _make_db([empty_result])

        with pytest.raises(HTTPException) as exc_info:
            await get_logs(job_id="22222222-2222-4222-8222-222222222222", include_output=False, db=db, limit=100, offset=0)

        assert exc_info.value.status_code == 404
        assert "Job not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_output_truncated_by_default(self):
        """Output text is truncated to 500 chars when include_output=False."""
        long_output = "x" * 1000
        job_row = _make_row(status="completed", compiled_output=None)
        node_rows = [
            _make_row(
                node_key="T1",
                title="Big output",
                tool="LLM",
                status="done",
                domain=None,
                output_text=long_output,
                confidence=None,
                updated_at=None,
            ),
        ]

        db = _make_db([_make_result([job_row]), _make_result(node_rows), _make_result(node_rows)])

        resp = await get_logs(job_id="33333333-3333-4333-8333-333333333333", include_output=False, db=db, limit=100, offset=0)

        assert len(resp.nodes[0].output_preview) == 501  # 500 + "…"
        assert resp.nodes[0].output_preview.endswith("…")

    @pytest.mark.asyncio
    async def test_full_output_when_requested(self):
        """Output text is NOT truncated when include_output=True."""
        long_output = "x" * 1000
        job_row = _make_row(status="completed", compiled_output=None)
        node_rows = [
            _make_row(
                node_key="T1",
                title="Big output",
                tool="LLM",
                status="done",
                domain=None,
                output_text=long_output,
                confidence=None,
                updated_at=None,
            ),
        ]

        db = _make_db([_make_result([job_row]), _make_result(node_rows), _make_result(node_rows)])

        resp = await get_logs(job_id="44444444-4444-4444-8444-444444444444", include_output=True, db=db, limit=100, offset=0)

        assert resp.nodes[0].output_preview == long_output

    @pytest.mark.asyncio
    async def test_null_output_handled(self):
        """Nodes with NULL output_text return None preview."""
        job_row = _make_row(status="running", compiled_output=None)
        node_rows = [
            _make_row(
                node_key="T1",
                title="Pending",
                tool="LLM",
                status="pending",
                domain=None,
                output_text=None,
                confidence=None,
                updated_at=None,
            ),
        ]

        db = _make_db([_make_result([job_row]), _make_result(node_rows), _make_result(node_rows)])

        resp = await get_logs(job_id="55555555-5555-4555-8555-555555555555", include_output=False, db=db, limit=100, offset=0)

        assert resp.nodes[0].output_preview is None

    @pytest.mark.asyncio
    async def test_no_nodes_returns_empty_list(self):
        """Job with no DAG nodes returns empty node list."""
        job_row = _make_row(status="planning", compiled_output=None)

        db = _make_db([_make_result([job_row]), _make_result([]), _make_result([])])

        resp = await get_logs(job_id="66666666-6666-4666-8666-666666666666", include_output=False, db=db, limit=100, offset=0)

        assert resp.node_count == 0
        assert resp.nodes == []

    @pytest.mark.asyncio
    async def test_nodes_ordered_by_key(self):
        """Nodes should be returned in node_key order."""
        job_row = _make_row(status="completed", compiled_output="done")
        node_rows = [
            _make_row(node_key="T1", title="A", tool="LLM", status="done",
                      domain=None, output_text=None, confidence=None, updated_at=None),
            _make_row(node_key="T2", title="B", tool="Milvus", status="done",
                      domain="rag", output_text=None, confidence=None, updated_at=None),
            _make_row(node_key="T3", title="C", tool="CodeGen", status="failed",
                      domain=None, output_text="error", confidence=None, updated_at=None),
        ]

        db = _make_db([_make_result([job_row]), _make_result(node_rows), _make_result(node_rows)])

        resp = await get_logs(job_id="77777777-7777-4777-8777-777777777777", include_output=False, db=db, limit=100, offset=0)

        keys = [n.node_key for n in resp.nodes]
        assert keys == ["T1", "T2", "T3"]


# ── Tests: Pydantic model validation ──────────────────────────────────


class TestModels:

    def test_status_counts_defaults(self):
        """All status counts default to 0."""
        sc = StatusCounts()
        assert sc.planning == 0
        assert sc.running == 0
        assert sc.completed == 0
        assert sc.failed == 0

    def test_job_summary_minimal(self):
        """JobSummary works with just id and status."""
        js = JobSummary(id="abc", status="completed")
        assert js.node_count == 0
        assert js.created_at is None

    def test_node_log_all_fields(self):
        """NodeLog accepts all fields."""
        nl = NodeLog(
            node_key="T1",
            title="Research",
            tool="Milvus",
            status="done",
            domain="eng",
            output_preview="some text",
            confidence=0.95,
            updated_at="2026-04-04T10:00:00+00:00",
        )
        assert nl.domain == "eng"
        assert nl.confidence == 0.95
