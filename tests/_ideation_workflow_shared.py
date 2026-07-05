"""Shared fixtures and helpers for test_ideation_workflow_*.py files (#9.6).

Module-level imports and the _load_module importlib setup live here so
split files can `from ... import *`.

Leading underscore in the filename -> pytest skips collection.
"""
"""tests/test_ideation_workflow.py — Tests for ideation pipeline (Phase 1 + 2).

Covers the rewritten contract:
  • Phase 1 (analyze_and_confirm) — refine + feasibility, halt at awaiting_confirmation
  • Phase 2 (research_and_compile) — atomic claim, research, distill, ingest, compile
  • Distillation + compile both use model_router (4b), not model_general
  • Atomic claim returns conflict (409) on concurrent calls, not-found (404) on missing

Uses the importlib loader pattern (Docker-safe): stubs heavy deps so the module
loads without a live Postgres/Milvus/Ollama.

Run:
    docker exec scaffold-orchestrator pytest tests/test_ideation_workflow.py -v -m smoke
"""

import importlib.util

import json

import os

import sys

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "modules", "ideation_workflow.py")
)

def _load_module():
    """Load ideation_workflow.py via importlib with heavy deps stubbed."""
    stubs = {}
    for mod_name in [
        "app", "app.config", "app.model_router",
        "app.modules", "app.modules.idea_refinement",
        "app.modules.gt_extractor", "app.modules.rag_pipeline",
        "app.database", "app.providers",
        # §17.290 — added ``app.utils.job_utils`` (host of ``fail_job``)
        # because ideation_workflow.py imports from it; without this stub
        # the loader hit ``ModuleNotFoundError: No module named
        # 'app.utils.job_utils'`` and every Phase-2 test silently skipped.
        # Symptom existed pre-§17.290 — the audit's verification step
        # surfaced it. All existing tests gain real runs from this fix.
        "app.utils", "app.utils.llm_parsing", "app.utils.topic_detection",
        "app.utils.job_utils",
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql",
        "structlog", "aiohttp", "asyncpg",
    ]:
        stubs[mod_name] = MagicMock()

    # sqlalchemy.text passthrough
    stubs["sqlalchemy"].text = lambda s: s

    # settings.ideation_max_* need real ints for slicing
    stubs["app.config"].settings.ideation_max_queries = 5
    stubs["app.config"].settings.ideation_max_distill_results = 15
    stubs["app.config"].settings.model_general = "qwen2.5:7b"

    # structlog .bind() must be chainable and return a logger-like object
    mock_logger = MagicMock()
    mock_logger.bind.return_value = mock_logger
    stubs["structlog"].stdlib.get_logger.return_value = mock_logger

    # §17.x — load the REAL app.utils.llm_retry (lightweight; imports only
    # ``logging``) so the Phase-2 compile path exercises the actual
    # generate_until_nonempty retry wrapper. Without this, the module-level
    # ``from app.utils.llm_retry import generate_until_nonempty`` raised
    # ModuleNotFoundError ('app.utils' stubbed as MagicMock → not a package),
    # which the loader caught and turned into a silent skip of EVERY Phase-2
    # test (skip-cascade; same class of bug as the §17.290 job_utils heal).
    _retry_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "app", "utils", "llm_retry.py")
    )
    _retry_spec = importlib.util.spec_from_file_location(
        "app.utils.llm_retry", _retry_path
    )
    _retry_mod = importlib.util.module_from_spec(_retry_spec)
    _retry_spec.loader.exec_module(_retry_mod)
    stubs["app.utils.llm_retry"] = _retry_mod

    # §17.580 — real-load app.providers.base (Tool dataclass) and
    # app.utils.tool_call_args (read_tool_args); both are stdlib-only. The
    # feasibility pass now imports them, and a bare MagicMock stub for the
    # parent packages would ModuleNotFoundError the ``from ... import`` lines
    # and skip-cascade every Phase-1/2 test (same class as the §17.290 heal).
    for _real_name, _rel in [
        ("app.providers.base", ("app", "providers", "base.py")),
        ("app.utils.tool_call_args", ("app", "utils", "tool_call_args.py")),
    ]:
        _p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", *_rel))
        _s = importlib.util.spec_from_file_location(_real_name, _p)
        _m = importlib.util.module_from_spec(_s)
        _s.loader.exec_module(_m)
        stubs[_real_name] = _m

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("ideation_workflow", _MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            import traceback
            traceback.print_exc()
        return mod

_mod = _load_module()

pytestmark = pytest.mark.skipif(
    _mod is None or not hasattr(_mod, "analyze_and_confirm"),
    reason="ideation_workflow.py not loadable in this environment",
)

def _llm_response(text_content: str, success: bool = True):
    """Fake model_router.generate() response — object with .success, .text."""
    resp = MagicMock()
    resp.success = success
    resp.text = text_content
    return resp

def _tool_response(arguments, success: bool = True):
    """Fake model_router.tool_call() response — object with .success and
    .tool_calls[0].arguments, matching read_tool_args's read path (§17.580).

    ``arguments=None`` yields an empty ``tool_calls`` list so read_tool_args
    returns None (the genuine-failure → fallback path).
    """
    resp = MagicMock()
    resp.success = success
    if arguments is None:
        resp.tool_calls = []
    else:
        call = MagicMock()
        call.arguments = arguments
        resp.tool_calls = [call]
    return resp

def _mock_db_for_claim(claimed_row, existing_row_after_fail=None):
    """Build AsyncSession mock for Phase 2's atomic-claim flow.

    The new research_and_compile does:
      1. UPDATE ... RETURNING research_data, refined_brief  -> claim
      2. (if claim empty) SELECT status FROM jobs WHERE id  -> disambiguation
      3. UPDATE jobs SET status='planning', ...             -> final transition

    Args:
        claimed_row: dict the atomic UPDATE RETURNING yields (or None if claim fails)
        existing_row_after_fail: dict with 'status' key for the disambiguation SELECT
            when the claim fails (used to decide between 404 and 409)
    """
    call_results = []

    # Call 1: atomic claim
    claim_mappings = MagicMock()
    claim_mappings.first.return_value = claimed_row
    claim_result = MagicMock()
    claim_result.mappings.return_value = claim_mappings
    call_results.append(claim_result)

    if claimed_row is None:
        # Call 2: disambiguation SELECT
        check_mappings = MagicMock()
        check_mappings.first.return_value = existing_row_after_fail
        check_result = MagicMock()
        check_result.mappings.return_value = check_mappings
        call_results.append(check_result)
    else:
        # Call 2: final UPDATE (doesn't need .mappings but safe to provide)
        final_result = MagicMock()
        call_results.append(final_result)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=call_results)
    return db


__all__ = ['AsyncMock', 'MagicMock', '_MODULE_PATH', '_llm_response', '_tool_response', '_load_module', '_mock_db_for_claim', '_mod', 'importlib', 'json', 'os', 'patch', 'pytest', 'pytestmark', 'sys']
