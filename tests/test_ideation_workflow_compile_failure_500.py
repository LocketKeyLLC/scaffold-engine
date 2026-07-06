"""§17.290 — Phase 2 compile-failure path returns http_status=500.

§17.280-UX-4 audit-tail concern: the compile-failure branch of
``research_and_compile`` returned ``http_status: 502`` (Bad Gateway).
Defensible per HTTP semantics (the LLM is "upstream"), but inconsistent
with:

  - The generic-exception path in the same function (``raise`` →
    FastAPI's default 500).
  - The 404 (job-not-found) and 409 (status-conflict) paths above, which
    have genuine client-error semantics.
  - No remediation hint in ``recovery.py::NEXT_ACTIONS`` keys off 502;
    operators see the same `/confirm` retry advice either way.

§17.290 standardizes the compile-failure code at 500 — matches the
generic-exception path's effective default and removes the lone 502
in the codebase. The audit's alternative ("document the 502-means-LLM
convention") would have required wider classification work since the
generic-exception path covers many upstream failures too (SearXNG,
Milvus, etc.) but collapses to 500 via re-raise.
"""
from tests._ideation_workflow_shared import *  # noqa: F401, F403


def _wrap_async_session_no_op(mod) -> None:
    """Stub `_mod.async_session()` as an async context manager that
    yields a fresh AsyncMock DB. Also no-op `_fail_job` so the
    compile-failure branch's `async with async_session(): await
    _fail_job(...)` works without touching a real Postgres."""
    fake_fail_db = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_fail_db)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    mod.async_session = MagicMock(return_value=fake_session)
    mod._fail_job = AsyncMock()


@pytest.mark.smoke
class TestCompileFailureHttpStatus:
    """§17.290 — the load-bearing UX-4 fix."""

    async def test_compile_parse_failure_returns_500(self):
        """workflow=None from `read_tool_args(resp)` → 500 (§17.581: compile is
        now tool_call; success but no parseable tool args is the reasoning-model
        failure mode). Pre-§17.290 this returned 502; standardized so the
        operator-facing code matches the rest of Phase 2's in-band failures."""
        claimed = {
            "research_data": {
                "feasibility": {"recommended_research_queries": ["RAG"]},
                "brief": {"title": "test", "domain": "eng"},
            },
            "refined_brief": None,
        }
        db = _mock_db_for_claim(claimed)

        # SearXNG empty → no distill needed; compile is the only LLM call.
        # success=True but NO tool args (all redraws argless) → read_tool_args None.
        _mod.search_searxng = AsyncMock(return_value=[])
        _mod.model_router = MagicMock()
        _mod.model_router.tool_call = AsyncMock(
            return_value=_tool_response(None, success=True)
        )
        _wrap_async_session_no_op(_mod)

        result = await _mod.research_and_compile(job_id="job-compile-fail", db=db)

        assert result["status"] == "failed"
        assert result["http_status"] == 500, (
            "§17.290: compile-failure path must return 500, not 502, "
            "to match the rest of Phase 2's in-band failure semantics. "
            "Operator-facing remediation is identical either way "
            "(/confirm re-try), so the lone 502 created inconsistency "
            "with no UX gain."
        )
        assert "compile step failed" in result["error"]

    async def test_compile_empty_args_dict_is_fatal(self):
        """§17.582 — a native-tool provider coerces missing args to {} (not None;
        openai.py:347/anthropic.py:416), and read_tool_args returns that {}
        verbatim. The compile guard must treat {} as fatal (falsy check, was the
        weaker `is None`) rather than advance to 'planning' with an empty plan —
        the §17.290/§17.463 no-empty-workflow invariant."""
        claimed = {
            "research_data": {
                "feasibility": {"recommended_research_queries": ["RAG"]},
                "brief": {"title": "test", "domain": "eng"},
            },
            "refined_brief": None,
        }
        db = _mock_db_for_claim(claimed)

        _mod.search_searxng = AsyncMock(return_value=[])
        _mod.model_router = MagicMock()
        # success=True, one tool call, but EMPTY args dict → read_tool_args → {}.
        _mod.model_router.tool_call = AsyncMock(
            return_value=_tool_response({}, success=True)
        )
        _wrap_async_session_no_op(_mod)

        result = await _mod.research_and_compile(job_id="job-empty-args", db=db)

        assert result["status"] == "failed"
        assert result["http_status"] == 500, (
            "§17.582: an empty-args {} compile result must be fatal, not "
            "advanced to planning with an empty workflow."
        )

    async def test_compile_llm_unsuccess_returns_500(self):
        """Compile LLM call itself fails (resp.success=False, e.g. timeout
        or HTTP error from the model server) — same path, same 500.
        Pre-§17.290 also returned 502.
        """
        claimed = {
            "research_data": {
                "feasibility": {"recommended_research_queries": ["RAG"]},
                "brief": {"title": "test", "domain": "eng"},
            },
            "refined_brief": None,
        }
        db = _mock_db_for_claim(claimed)

        _mod.search_searxng = AsyncMock(return_value=[])
        _mod.model_router = MagicMock()
        # success=False → read_tool_args None → workflow stays None.
        bad_resp = _tool_response(None, success=False)
        bad_resp.error = "model timeout"
        _mod.model_router.tool_call = AsyncMock(return_value=bad_resp)
        _wrap_async_session_no_op(_mod)

        result = await _mod.research_and_compile(job_id="job-compile-timeout", db=db)

        assert result["status"] == "failed"
        assert result["http_status"] == 500
        # Error string carries enough context for the operator log.
        assert "llm_success=False" in result["error"]


@pytest.mark.smoke
class TestOtherPhase2StatusesUnchanged:
    """§17.290 must NOT regress the 404 / 409 paths — they have genuine
    client-error semantics and stay as-is. Pin those here alongside the
    500 change so a future refactor that "standardizes" them too gets
    a test failure.
    """

    async def test_job_not_found_still_returns_404(self):
        """The atomic claim fails + disambiguation finds nothing → 404.
        §17.290 leaves this path untouched."""
        db = _mock_db_for_claim(
            claimed_row=None, existing_row_after_fail=None,
        )
        result = await _mod.research_and_compile(job_id="missing", db=db)
        assert result["status"] == "failed"
        assert result["http_status"] == 404

    async def test_wrong_status_still_returns_409(self):
        """Atomic claim fails + job in wrong status → 409.
        §17.290 leaves this path untouched."""
        db = _mock_db_for_claim(
            claimed_row=None,
            existing_row_after_fail={"status": "researching"},
        )
        result = await _mod.research_and_compile(job_id="race", db=db)
        assert result["status"] == "conflict"
        assert result["http_status"] == 409


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.290 — anchor the change in the production source. A drive-by
    refactor that flips the explicit 500 back to 502 (or removes the
    explicit code entirely, relying on the router's `.get(..., 500)`
    default) should be visible at test review."""

    def test_compile_failure_branch_carries_500(self):
        from app.modules import ideation_workflow

        with open(ideation_workflow.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "\"http_status\": 502" not in src, (
            "§17.290 regression: the lone 502 in the codebase has "
            "reappeared in ideation_workflow.py. Standardize on 500 — "
            "the compile-failure path uses the same recovery semantics "
            "as the generic-exception path (`/confirm` re-try), so "
            "differentiating the HTTP code adds no operator value."
        )
        # The replacement value lives in the compile-failure branch with
        # a §17.290 comment naming the audit closeout.
        assert "§17.290" in src, (
            "§17.290 regression: the audit citation has been removed "
            "from ideation_workflow.py. Keep the comment so the next "
            "reader sees why this branch is 500 and not 502."
        )
