"""
conftest.py — Shared fixtures for Scaffold Engine test suite.

NOTE (#9.8/#9.9): These top-level imports are load-bearing. Removing them
causes every `from app.X import ...` in a test module to fail with
"'app' is not a package" because pytest's path finder needs `app` as a
resolved package before test modules reference submodules like
`app.utils.embedding`. Attempted removal on 2026-04-21 caused 16 collection
errors — see drift-findings.md. If you really need to drop these, first
convert the test suite to use `tests/` as a proper package (add
`tests/__init__.py` and configure rootdir). Until then, keep them.
"""
import os

import app  # noqa: F401  — load-bearing; see note above
import app.model_router  # noqa: F401  — load-bearing; see note above
import pytest
from unittest.mock import AsyncMock, MagicMock

# Cloud CI Tier 1 (`make ci-smoke`) installs only requirements-ci.txt — a
# strict subset of prod deps that omits redis, apscheduler, pypdf, the
# opentelemetry-* tree, sentence-transformers, etc. Test modules whose
# top-level imports transitively need any of those would fail at COLLECTION
# (pytest must import the module to discover its `-m smoke` markers). Gate
# them out here so the smoke tier can run; they still run unconditionally
# in `make test` / `make ci` inside the dev image where all deps are present.
#
# Maintenance: when a new test module surfaces an `ImportError` in cloud CI
# logs ("ERROR tests/test_foo.py ... ModuleNotFoundError: No module named X"),
# either (a) add `X` to requirements-ci.txt if it's lightweight, or (b) add
# `test_foo.py` here if its dep chain is heavy.
if os.environ.get("SCAFFOLD_CI_SMOKE_MODE"):
    collect_ignore = [
        "integration/test_execution_db.py",
        "integration/test_sim_ngspice_db.py",
        "integration/test_sim_verilator_db.py",
        "integration/test_sim_symbiyosys_db.py",
        "integration/test_spec_extractor_live.py",
        "integration/test_specs_router_db.py",
        "integration/test_topology_select_db.py",
        "test_assist_session_map.py",
        "test_config_endpoint.py",
        "test_cost_rollup.py",
        "test_embedding.py",
        "test_embedding_cache.py",
        "test_embedding_cache_warmup.py",
        "test_execution_verify_cache.py",
        "test_execute_all_concurrent_guard.py",
        "test_execution_agent_concurrency.py",
        "test_execution_agent_feedback.py",
        "test_execution_agent_retry.py",
        "test_github_ingest_cache.py",
        "test_gt_browser_module.py",
        "test_gt_extractor.py",
        "test_gt_extractor_module.py",
        "test_ideation_phase2_cancel.py",
        "test_integration.py",
        "test_main.py",
        "test_resume_endpoint.py",
        "test_observability_alerts.py",
        "test_observability_metrics.py",
        "test_observability_rollups.py",
        "test_pre_migration_sweep.py",
        "test_rag_pipeline_smoke.py",
        "test_reindex.py",
        "test_research_agent_core.py",
        "test_research_agent_extract_no_entries.py",
        "test_research_agent_helpers.py",
        "test_research_agent_ingestion.py",
        "test_research_agent_lifecycle.py",
        "test_research_pause_resume.py",
        "test_research_pdf_mode.py",
        "test_research_ssrf_guard.py",
        "test_research_url_mode.py",
        "test_retrieval_golden.py",
        "test_score_retrieval.py",
        "test_verify_extraction.py",
        "test_web_ui.py",
    ]


@pytest.fixture(autouse=True, scope="session")
def _capture_thread_exceptions():
    """Log full tracebacks for any unhandled non-main-thread exceptions.

    pytest emits ``PytestUnhandledThreadExceptionWarning`` when a thread
    raises during a test session, but the warning summary doesn't carry
    the traceback — making the bug hard to diagnose (see §17.106/§17.108/
    §17.110 occurrences). This session-autouse hook installs
    ``threading.excepthook`` so any future occurrence writes
    ``timestamp + thread_name + full_traceback`` to a known log file an
    investigator can grep. Restores the previous hook on teardown.

    Log path: ``/tmp/.pytest_thread_exceptions.log`` (matches
    ``cache_dir = /tmp/.pytest_cache`` from pyproject.toml).
    """
    import threading
    import time
    import traceback as tb

    log_path = "/tmp/.pytest_thread_exceptions.log"
    prev_hook = threading.excepthook

    def _hook(args):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n=== THREAD EXCEPTION === "
                    f"ts={time.strftime('%Y-%m-%dT%H:%M:%S')} "
                    f"thread={args.thread.name if args.thread else '<unknown>'}\n"
                )
                tb.print_exception(
                    args.exc_type, args.exc_value, args.exc_traceback, file=f,
                )
        except Exception:
            # Never let the hook itself crash a test session.
            pass

    threading.excepthook = _hook
    yield
    threading.excepthook = prev_hook


def make_mock_db(rows: list[dict] | None = None, *, scalar=None, rowcount=None):
    """
    Build a mock AsyncSession whose .execute() returns a result object
    compatible with the common SQLAlchemy access patterns (#9.10):

      result.mappings().all()  -> rows (list of dicts)
      result.fetchall()        -> rows
      result.scalar()          -> `scalar` (or rows[0] if single-col row)
      result.scalar_one()      -> same as scalar()
      result.scalar_one_or_none() -> same as scalar()
      result.first()           -> rows[0] (or None if empty)
      result.rowcount          -> `rowcount` (default: len(rows))

    Args:
        rows: List of dicts representing the result set.
        scalar: Explicit scalar return value (overrides row-based inference).
        rowcount: Explicit rowcount override.
    """
    rows = rows or []

    mappings_obj = MagicMock()
    mappings_obj.all.return_value = rows
    # §17.145 — many modules use ``result.mappings().first()`` (e.g.
    # app/scheduler.py, app/modules/execution_agent.py, app/sim/spec_store.py).
    # Mirror the existing ``result.first()`` semantics: first row or None.
    # ``one()`` mirrors SQLAlchemy's "exactly one row" — returns rows[0]
    # when present (we don't raise here; tests use empty-list mocks to
    # exercise the not-found branch via the caller's own check).
    mappings_obj.first.return_value = rows[0] if rows else None
    mappings_obj.one.return_value = rows[0] if rows else None

    result_obj = MagicMock()
    result_obj.mappings.return_value = mappings_obj
    result_obj.fetchall.return_value = rows
    result_obj.first.return_value = rows[0] if rows else None
    # scalar() / scalar_one() / scalar_one_or_none() all return the same mock value
    inferred_scalar = scalar
    if inferred_scalar is None and rows:
        first = rows[0]
        # For single-column rows represented as {"col": value} or (value,) tuples
        if isinstance(first, dict) and len(first) == 1:
            inferred_scalar = next(iter(first.values()))
        else:
            inferred_scalar = first
    result_obj.scalar.return_value = inferred_scalar
    result_obj.scalar_one.return_value = inferred_scalar
    result_obj.scalar_one_or_none.return_value = inferred_scalar
    result_obj.rowcount = rowcount if rowcount is not None else len(rows)

    db = AsyncMock()
    db.execute.return_value = result_obj
    return db


# ---------------------------------------------------------------------------
# http_clients eager-init for the test suite.
# The module no longer lazy-creates clients; modules that call get_*()
# require init_clients() to have run. We re-seed the registry before every
# test so fixtures in other modules that close/reset clients don't leave
# a hole for the next test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _init_shared_http_clients():
    """Re-seed the http_clients registry per test so each httpx client is
    bound to the current event loop (pytest-asyncio gives each test its own
    loop; reusing a client across loops raises 'Event loop is closed')."""
    from app.utils import http_clients
    # Drop any registry entries from the previous test without awaiting
    # aclose() (previous loop is gone). These are leftover Python objects
    # — GC will finalize them.
    http_clients._clients.clear()
    http_clients.init_clients()
    yield
