"""Tests for execution_agent — _compile_output 3-strategy priority chain + partial/cache/marker variants.

Split from the original test_execution_agent.py (#9.6). Shared imports
and helpers live in _execution_agent_shared.
"""
from tests._execution_agent_shared import *  # noqa: F401, F403


@pytest.fixture(autouse=True)
def _bypass_synthesis_override_db_read(monkeypatch):
    """Sprint X.16 — bypass `_resolve_synthesis_enabled`'s DB-read in
    every test in this file.

    Background: X.6 introduced ``_resolve_synthesis_enabled(job_id, db)``
    which SELECTs ``jobs.compile_synthesis_override`` and falls through
    to ``settings.compile_synthesis_enabled`` when the column is NULL.
    Pre-X.16 tests in this file use ``make_mock_db([{...row dicts...}])``,
    whose ``scalar()`` inference returns the first row dict as a "scalar"
    when the dict has multiple keys — that dict is truthy, so
    ``_resolve_synthesis_enabled`` returns True, forcing synthesis ON
    even when the test set ``settings.compile_synthesis_enabled=False``.

    The bypass replaces the resolver with one that reads
    ``settings.compile_synthesis_enabled`` directly. Synthesis-enabled
    tests still work because they patch the setting via
    ``patch.object(settings, "compile_synthesis_enabled", True)`` —
    that patch is observed by the bypass.

    Tests that need to exercise the override-resolution semantics
    explicitly belong in ``tests/test_compile_synthesis_override.py``,
    where this fixture isn't applied.
    """
    from app.config import settings
    from app.modules import execution_compile

    async def _bypass(job_id, db):
        return settings.compile_synthesis_enabled

    monkeypatch.setattr(
        execution_compile, "_resolve_synthesis_enabled", _bypass,
    )


@pytest.mark.smoke
class TestCompileOutputStrategy2:
    """Strategy 2: last CodeGen terminal node is the deliverable."""

    async def test_last_codegen_selected(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan text"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "def hello(): pass"},
            {"node_key": "T3", "title": "Refactor", "tool": "CodeGen",
             "status": "done", "output_text": "def hello():\n    print('hi')"},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result == "def hello():\n    print('hi')"

    async def test_codegen_not_last_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "code here"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "done", "output_text": "summary here"},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "## T1:" in result
        assert "## T2:" in result


@pytest.mark.smoke
class TestCompileOutputStrategy3:
    """Strategy 3: concatenate all passed outputs with headers."""

    async def test_concatenation_format(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "SearXNG",
             "status": "done", "output_text": "research data"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "done", "output_text": "analysis results"},
            {"node_key": "T3", "title": "Review", "tool": "LLM",
             "status": "done", "output_text": "review notes"},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "## T1: Research" in result
        assert "## T2: Analyze" in result
        assert "## T3: Review" in result
        assert "---" in result

    async def test_failed_nodes_excluded(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "good stuff"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Blocked task", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "## T1: Research" in result
        assert "T2" not in result
        assert "T3" not in result

    async def test_no_done_nodes_returns_none(self):
        """W.2: returns None (not "") so caller stores NULL — semantically
        cleaner than an empty string ("we never produced output" vs. "we
        produced an empty string")."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is None


@pytest.mark.smoke
class TestCompileOutputW2Strategy3:
    """W.2 additions: preamble + truncation + diagnostic warning."""

    async def test_strategy3_prepends_partial_deliverable_preamble(self):
        """When Strategy 3 fires (no leaf done, last not CodeGen), the
        output starts with a 'Partial deliverable' banner so consumers
        can tell this is a fallback rather than a clean Strategy-0 result."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "research data"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "done", "output_text": "analysis"},
            # T3 is the leaf but it's still pending → no Strategy 0 hit
            {"node_key": "T3", "title": "Synthesize", "tool": "LLM",
             "status": "pending", "output_text": None,
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is not None
        assert "Partial deliverable" in result
        assert "2 of 3" in result
        # And the actual section content still appears below the preamble
        assert "## T1: Research" in result

    async def test_strategy3_truncates_when_total_exceeds_cap(self, monkeypatch):
        """Pathological case: many verbose nodes blow past the storage cap.
        Each section is truncated proportionally so the artifact stays
        readable rather than ballooning into a multi-MB blob."""
        from app.config import settings
        monkeypatch.setattr(settings, "compile_output_max_chars", 2_000)

        big = "x" * 5_000
        db = make_mock_db([
            {"node_key": f"T{i}", "title": f"Node {i}", "tool": "LLM",
             "status": "done", "output_text": big}
            for i in range(1, 5)
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is not None
        # Truncation marker should appear in each section.
        assert result.count("[...truncated") >= 1
        # Result still includes a section per node.
        for i in range(1, 5):
            assert f"## T{i}: Node {i}" in result

    async def test_strategy3_logs_warning_with_done_nodes(self, caplog):
        """Diagnostic: Strategy 3 firing with done output is a hint that
        the dag_generator's leaf-set logic missed this DAG shape (or the
        true leaves failed). Warning is the trail teams follow."""
        import logging
        caplog.set_level(logging.WARNING, logger="scaffold.execution_compile")

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "a"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "b"},
        ])
        from app.modules.execution_agent import _compile_output
        await _compile_output("job-1", db)
        assert any(
            "compile_strategy3_fallback" in r.message
            for r in caplog.records
        ), "expected a strategy-3 fallback warning"


@pytest.mark.smoke
class TestCompileOutputPartial:
    """Partial compile behavior — _compile_output returns what it can;
    caller adds [PARTIAL] prefix."""

    async def test_partial_with_some_done(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "Milvus",
             "status": "done", "output_text": "kb results"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Compare", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "kb results" in result
        assert "T2" not in result
        partial = "[PARTIAL — some nodes failed or blocked]\n\n" + result
        assert partial.startswith("[PARTIAL")

    async def test_all_failed_returns_none(self):
        """W.2 contract change: no done nodes → return None (was "" pre-W.2).
        Caller stores NULL, which is the semantically correct state."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is None


@pytest.mark.smoke
class TestCompileOutputCache:
    """#22: blocked job with cached compiled_output skips recompute."""

    async def test_cached_compiled_output_skips_recompute(self):
        """When jobs.compiled_output is populated, _compile_output is NOT called."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = "CACHED COMPILED OUTPUT"
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> cache hit
            status_update_result,  # UPDATE status = 'blocked' (idempotent guard)
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock()  # MUST NOT be called

        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.modules.execution_agent.async_session", mock_session_factory), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = await execute_next_node("job-1")

        assert result["status"] == "blocked"
        mock_compile.assert_not_called()

    async def test_uncached_blocked_job_recomputes(self):
        """When compiled_output is NULL, _compile_output IS called and result stored."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = None  # cache miss
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> None
            status_update_result,  # UPDATE compiled_output + status
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock(return_value="FRESHLY COMPILED")

        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.modules.execution_agent.async_session", mock_session_factory), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = await execute_next_node("job-1")

        assert result["status"] == "blocked"
        mock_compile.assert_called_once()


@pytest.mark.smoke
class TestCompileOutputExplicitMarker:
    """#97: is_output_node=TRUE takes precedence over title heuristics."""

    async def test_is_output_node_overrides_title_heuristic(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research topic", "tool": "SearXNG",
             "status": "done", "output_text": "should NOT be used",
             "is_output_node": False},
            {"node_key": "T2", "title": "Summarize output", "tool": "LLM",
             "status": "done", "output_text": "legacy heuristic winner",
             "is_output_node": False},
            {"node_key": "T3", "title": "Final deliverable", "tool": "LLM",
             "status": "done", "output_text": "EXPLICIT WINNER",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result == "EXPLICIT WINNER"
    async def test_explicit_marker_but_not_done_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "fallback content",
             "is_output_node": False},
            {"node_key": "T2", "title": "Compose output", "tool": "LLM",
             "status": "failed", "output_text": None,
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "fallback content" in result
        assert "T2" not in result

    async def test_multiple_output_nodes_joined(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Part A", "tool": "LLM",
             "status": "done", "output_text": "alpha",
             "is_output_node": True},
            {"node_key": "T2", "title": "Part B", "tool": "LLM",
             "status": "done", "output_text": "beta",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "alpha" in result
        assert "beta" in result
        assert "---" in result


@pytest.mark.smoke
class TestCompileOutputDominantLeaf:
    """§17.473 — dominant-leaf preference. dag_generator marks EVERY
    structural leaf is_output_node, so a DAG with a dead-end side branch
    (nothing consumes it) marks that branch as a co-deliverable alongside
    the real convergence node. Strategy 0 now drops a dead-end leaf when a
    dominant leaf both covers its upstream and is _DOMINANT_LEAF_FACTOR×
    larger — but preserves genuinely co-equal deliverables."""

    async def test_dead_end_branch_dropped_for_dominant_leaf(self):
        # MAIN's closure (MAIN,M3,M2,M1,B = 5) dominates D's (D,B = 2); D's
        # only unique contribution is itself → dead-end branch, dropped. One
        # survivor → single-leaf path emits its raw output (no section header).
        # D is a Shell runbook (Proxmox's Tailscale shape) — §17.482 only drops
        # action-tool dead-ends; an LLM dead-end would now be kept (see
        # test_select_dominant_leaves_protects_llm_dead_end).
        db = make_mock_db([
            {"node_key": "B", "title": "Base", "tool": "LLM", "status": "done",
             "output_text": "base", "is_output_node": False, "depends_on": []},
            {"node_key": "M1", "title": "Mid 1", "tool": "LLM", "status": "done",
             "output_text": "m1", "is_output_node": False, "depends_on": ["B"]},
            {"node_key": "M2", "title": "Mid 2", "tool": "LLM", "status": "done",
             "output_text": "m2", "is_output_node": False, "depends_on": ["M1"]},
            {"node_key": "M3", "title": "Mid 3", "tool": "LLM", "status": "done",
             "output_text": "m3", "is_output_node": False, "depends_on": ["M2"]},
            {"node_key": "D", "title": "Dead-end branch", "tool": "Shell",
             "status": "done", "output_text": "DEAD_END_BRANCH",
             "is_output_node": True, "depends_on": ["B"]},
            {"node_key": "MAIN", "title": "Synthesis", "tool": "LLM",
             "status": "done", "output_text": "REAL_DELIVERABLE",
             "is_output_node": True, "depends_on": ["M3"]},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        # §17.506 — D is a done Shell node, so a plan-only banner is prepended;
        # assert content selection (the test's concern) via membership.
        assert "REAL_DELIVERABLE" in result
        assert "DEAD_END_BRANCH" not in result

    async def test_coequal_leaves_both_kept(self):
        # config + README: equal closures ({CFG,B} / {DOC,B}) sharing a base;
        # neither is _DOMINANT_LEAF_FACTOR× the other → both preserved.
        db = make_mock_db([
            {"node_key": "B", "title": "Build", "tool": "LLM", "status": "done",
             "output_text": "build", "is_output_node": False, "depends_on": []},
            {"node_key": "CFG", "title": "Config", "tool": "LLM", "status": "done",
             "output_text": "CONFIG_OUT", "is_output_node": True, "depends_on": ["B"]},
            {"node_key": "DOC", "title": "README", "tool": "LLM", "status": "done",
             "output_text": "README_OUT", "is_output_node": True, "depends_on": ["B"]},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "CONFIG_OUT" in result
        assert "README_OUT" in result
        assert "---" in result

    async def test_no_depends_on_preserves_legacy_concat(self):
        # Backward-compat: leaves with no depends_on (closure size 1 each)
        # have no dominant leaf → legacy multi-leaf join is unchanged.
        db = make_mock_db([
            {"node_key": "T1", "title": "Part A", "tool": "LLM", "status": "done",
             "output_text": "alpha", "is_output_node": True},
            {"node_key": "T2", "title": "Part B", "tool": "LLM", "status": "done",
             "output_text": "beta", "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "alpha" in result and "beta" in result and "---" in result

    async def test_codegen_leaf_not_dropped_by_dominant_doc_leaf(self):
        # §17.473 refinement — a CodeGen "write tests" leaf is structurally
        # dominated by a "document everything" LLM leaf (closure 4 ≥ 2×2),
        # but CodeGen is a code deliverable and must survive → both joined.
        # Mirrors the real mdsplit job (CodeGen unit-tests vs LLM usage-docs).
        db = make_mock_db([
            {"node_key": "B", "title": "Base", "tool": "LLM", "status": "done",
             "output_text": "base", "is_output_node": False, "depends_on": []},
            {"node_key": "M1", "title": "Mid 1", "tool": "LLM", "status": "done",
             "output_text": "m1", "is_output_node": False, "depends_on": ["B"]},
            {"node_key": "M2", "title": "Mid 2", "tool": "LLM", "status": "done",
             "output_text": "m2", "is_output_node": False, "depends_on": ["M1"]},
            {"node_key": "TESTS", "title": "Write unit tests", "tool": "CodeGen",
             "status": "done", "output_text": "def test_x(): assert True",
             "is_output_node": True, "depends_on": ["B"]},
            {"node_key": "DOCS", "title": "Document usage", "tool": "LLM",
             "status": "done", "output_text": "usage docs",
             "is_output_node": True, "depends_on": ["M2"]},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "def test_x()" in result      # CodeGen deliverable preserved
        assert "usage docs" in result


@pytest.mark.smoke
class TestDominantLeafHelpers:
    """§17.473 — pure-function coverage for the closure + selection logic."""

    def test_dependency_closure_transitive(self):
        from app.modules.execution_compile import _dependency_closure
        deps = {"C": ["B"], "B": ["A"], "A": []}
        assert _dependency_closure("C", deps) == {"C", "B", "A"}

    def test_dependency_closure_cycle_safe(self):
        from app.modules.execution_compile import _dependency_closure
        deps = {"A": ["B"], "B": ["A"]}  # malformed cycle must not hang
        assert _dependency_closure("A", deps) == {"A", "B"}

    def test_select_dominant_leaves_drops_dead_end(self):
        from app.modules.execution_compile import _select_dominant_leaves
        all_nodes = [
            {"node_key": "B", "depends_on": []},
            {"node_key": "M1", "depends_on": ["B"]},
            {"node_key": "M2", "depends_on": ["M1"]},
            {"node_key": "M3", "depends_on": ["M2"]},
            {"node_key": "D", "depends_on": ["B"]},
            {"node_key": "MAIN", "depends_on": ["M3"]},
        ]
        explicit = [n for n in all_nodes if n["node_key"] in ("D", "MAIN")]
        survivors, dropped = _select_dominant_leaves(explicit, all_nodes)
        assert [n["node_key"] for n in survivors] == ["MAIN"]
        assert dropped == ["D"]

    def test_select_dominant_leaves_keeps_coequal(self):
        from app.modules.execution_compile import _select_dominant_leaves
        all_nodes = [
            {"node_key": "B", "depends_on": []},
            {"node_key": "CFG", "depends_on": ["B"]},
            {"node_key": "DOC", "depends_on": ["B"]},
        ]
        explicit = [n for n in all_nodes if n["node_key"] in ("CFG", "DOC")]
        survivors, dropped = _select_dominant_leaves(explicit, all_nodes)
        assert {n["node_key"] for n in survivors} == {"CFG", "DOC"}
        assert dropped == []

    def test_select_dominant_leaves_protects_codegen(self):
        # §17.473 refinement — a dominated CodeGen leaf is NOT dropped.
        from app.modules.execution_compile import _select_dominant_leaves
        all_nodes = [
            {"node_key": "B", "depends_on": [], "tool": "LLM"},
            {"node_key": "M1", "depends_on": ["B"], "tool": "LLM"},
            {"node_key": "M2", "depends_on": ["M1"], "tool": "LLM"},
            {"node_key": "M3", "depends_on": ["M2"], "tool": "LLM"},
            {"node_key": "CODE", "depends_on": ["B"], "tool": "CodeGen"},
            {"node_key": "DOC", "depends_on": ["M3"], "tool": "LLM"},
        ]
        explicit = [n for n in all_nodes if n["node_key"] in ("CODE", "DOC")]
        survivors, dropped = _select_dominant_leaves(explicit, all_nodes)
        assert dropped == []
        assert {n["node_key"] for n in survivors} == {"CODE", "DOC"}

    def test_select_dominant_leaves_drops_shell_dead_end(self):
        # Contrast: a Shell runbook leaf (Proxmox's Tailscale shape) stays
        # droppable — only action-tool leaves are; CodeGen/LLM are protected.
        from app.modules.execution_compile import _select_dominant_leaves
        all_nodes = [
            {"node_key": "B", "depends_on": [], "tool": "LLM"},
            {"node_key": "M1", "depends_on": ["B"], "tool": "LLM"},
            {"node_key": "M2", "depends_on": ["M1"], "tool": "LLM"},
            {"node_key": "M3", "depends_on": ["M2"], "tool": "LLM"},
            {"node_key": "SH", "depends_on": ["B"], "tool": "Shell"},
            {"node_key": "MAIN", "depends_on": ["M3"], "tool": "LLM"},
        ]
        explicit = [n for n in all_nodes if n["node_key"] in ("SH", "MAIN")]
        survivors, dropped = _select_dominant_leaves(explicit, all_nodes)
        assert dropped == ["SH"]
        assert [n["node_key"] for n in survivors] == ["MAIN"]

    def test_select_dominant_leaves_protects_llm_dead_end(self):
        # §17.482 — an LLM leaf structurally dominated by a larger LLM leaf is
        # NOT dropped. This is the exact mis-fire shape the §17.473 rule hit:
        # closure(SUB)=2, closure(MAIN)=4 (≥ 2×), SUB's only unique node is
        # itself — so the size+subset test would drop it, but SUB is LLM text
        # (a parallel deliverable: Homelab's "validate directory structure",
        # AI-Research's "set up network security node"), so it must survive.
        # Same topology as drops_shell, but the dead-end tool is LLM not Shell.
        from app.modules.execution_compile import _select_dominant_leaves
        all_nodes = [
            {"node_key": "B", "depends_on": [], "tool": "LLM"},
            {"node_key": "M1", "depends_on": ["B"], "tool": "LLM"},
            {"node_key": "M2", "depends_on": ["M1"], "tool": "LLM"},
            {"node_key": "M3", "depends_on": ["M2"], "tool": "LLM"},
            {"node_key": "SUB", "depends_on": ["B"], "tool": "LLM"},
            {"node_key": "MAIN", "depends_on": ["M3"], "tool": "LLM"},
        ]
        explicit = [n for n in all_nodes if n["node_key"] in ("SUB", "MAIN")]
        survivors, dropped = _select_dominant_leaves(explicit, all_nodes)
        assert dropped == []
        assert {n["node_key"] for n in survivors} == {"SUB", "MAIN"}


# ---------------------------------------------------------------------------
# Sprint W.7 — opt-in LLM synthesis pass
# ---------------------------------------------------------------------------


def _synthesis_response(text: str = "synthesized prose"):
    """Build a model_router.tool_call response with a render_summary call."""
    from app.providers.base import ModelResponse, ToolCall
    return ModelResponse(
        text="", model="fake", success=True,
        tool_calls=[ToolCall(
            id="t0", name="render_summary",
            arguments={"summary": text},
        )],
    )


def _synthesis_failure():
    from app.providers.base import ModelResponse
    return ModelResponse(
        text="", model="fake", success=False, error="ollama down",
    )


def _make_db_with_brief(rows, brief_description="Build a parser"):
    """make_mock_db returns a db whose every .execute() yields the same rows.
    For W.7 we also need the brief lookup to return a refined_brief dict.
    Using AsyncMock with side_effect lets us route the second call (brief
    SELECT) to a separate result."""
    from unittest.mock import AsyncMock, MagicMock

    nodes_result = MagicMock()
    nodes_mappings = MagicMock()
    nodes_mappings.all.return_value = rows
    nodes_result.mappings.return_value = nodes_mappings

    brief_result = MagicMock()
    brief_mappings = MagicMock()
    brief_mappings.first.return_value = {
        "refined_brief": {"description": brief_description},
    }
    brief_result.mappings.return_value = brief_mappings

    db = AsyncMock()
    # First call: SELECT dag_nodes ... (compile path)
    # Second call: SELECT refined_brief ... (synthesis path)
    db.execute = AsyncMock(side_effect=[nodes_result, brief_result])
    return db


@pytest.mark.smoke
class TestCompileOutputSynthesis:
    """W.7 — opt-in LLM synthesis. Default OFF; when ON, runs on all
    strategies except CodeGen-source paths (CodeGen guard preserves code)."""

    async def test_synthesis_disabled_returns_heuristic_no_llm_call(self):
        """Default behavior: synthesis off → heuristic returned, no LLM call."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
        ])
        synth_mock = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", False), \
             patch(
                 "app.modules.execution_compile._synthesize_compiled_output",
                 new=synth_mock,
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # Strategy 3 heuristic shape
        assert "## T1: Plan" in result
        assert "## T2: Build" in result
        # No synthesis call.
        synth_mock.assert_not_called()

    async def test_synthesis_enabled_strategy3_uses_llm_output(self):
        """Strategy 3 + synthesis ON → LLM-rewritten narrative replaces
        heuristic body."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_response(
                     "Coherent narrative covering both Plan and Build.",
                 )),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert result == "Coherent narrative covering both Plan and Build."

    async def test_synthesis_fail_open_returns_heuristic(self):
        """LLM call raises → fall back to heuristic body."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(side_effect=RuntimeError("ollama down")),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # Heuristic shape preserved.
        assert "## T1: Plan" in result
        assert "## T2: Build" in result

    async def test_synthesis_unsuccessful_response_falls_back(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_failure()),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert "## T1: Plan" in result

    async def test_codegen_guard_skips_synthesis_strategy_2(self):
        """Strategy 2 (last CodeGen): synthesis ON but CodeGen guard fires
        → heuristic returned verbatim, no LLM call."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        code_payload = "def hello():\n    print('hi')"
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": code_payload},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=synth_call,
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # CodeGen output preserved verbatim.
        assert result == code_payload
        # CodeGen guard ran inside _synthesize_compiled_output before the
        # LLM call would have fired.
        synth_call.assert_not_called()

    async def test_synthesis_enabled_strategy0_single_leaf(self):
        """Strategy 0 single-leaf with non-CodeGen tool → synthesis fires."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "raw research dump"},
            {"node_key": "T2", "title": "Synthesize", "tool": "LLM",
             "status": "done", "output_text": "explicit leaf body",
             "is_output_node": True},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_response(
                     "Polished narrative version.",
                 )),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert result == "Polished narrative version."

    async def test_synthesis_strategy0_codegen_leaf_skipped(self):
        """Strategy 0 single-leaf with tool=CodeGen → guard fires, raw code returned."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        code = "fn main() { println!(\"hi\"); }"
        db = make_mock_db([
            {"node_key": "T1", "title": "Write the binary", "tool": "CodeGen",
             "status": "done", "output_text": code,
             "is_output_node": True},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=synth_call,
             ):
            result, _was_syn = await _compile_output("job-1", db)
        assert result == code
        synth_call.assert_not_called()


# ---------------------------------------------------------------------------
# Sprint X.2 — synthesized flag + skipped-verify banner
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCompileOutputSynthesizedFlag:
    """X.2 — _compile_output returns (text, was_synthesized: bool)."""

    async def test_synthesis_disabled_returns_false(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", False):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert was_syn is False

    async def test_synthesis_enabled_and_succeeded_returns_true(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output
        from app.providers.base import ModelResponse, ToolCall

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        ok_resp = ModelResponse(
            text="", model="fake", success=True,
            tool_calls=[ToolCall(id="t0", name="render_summary",
                                  arguments={"summary": "Polished narrative."})],
        )
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(return_value=ok_resp)):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value == "Polished narrative."
        assert was_syn is True

    async def test_synthesis_enabled_but_fail_open_returns_false(self):
        """LLM call fails → heuristic returned + was_synthesized=False so the
        caller persists compiled_output_synthesized=False on jobs."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(side_effect=RuntimeError("ollama down"))):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert was_syn is False
        assert "## T1" in text_value  # heuristic shape

    async def test_codegen_guard_returns_false(self):
        """Strategy 2 (last CodeGen) is guarded — synthesis never fires →
        was_synthesized stays False even with synthesis enabled."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "def hi(): pass"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock()):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value == "def hi(): pass"
        assert was_syn is False

    async def test_empty_result_returns_none_false(self):
        """No done nodes → (None, False) — caller stores NULL + synthesized=False."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is None
        assert was_syn is False


@pytest.mark.smoke
class TestSynthesisPromptGuards:
    """§17.360 — SYNTHESIS_SYSTEM + SYNTHESIS_TOOL must enforce no-fabrication
    + placeholder-preservation + capability-boundary. Closes the homelab
    retry's compiled_output regression (synthesizer filled in placeholders
    like `<PROXMOX_HOST_IP>` with fabricated `192.168.1.10` / `tskey-abc...`
    values; see OVERVIEW §17.360)."""

    def test_synthesis_system_forbids_value_fabrication(self):
        from app.modules.execution_compile import SYNTHESIS_SYSTEM
        assert "DO NOT invent" in SYNTHESIS_SYSTEM
        # Spot-check several value classes the clause names explicitly.
        for marker in ("IPs", "auth keys", "API tokens", "file paths", "version"):
            assert marker in SYNTHESIS_SYSTEM, f"missing {marker!r}"

    def test_synthesis_system_requires_placeholder_preservation(self):
        from app.modules.execution_compile import SYNTHESIS_SYSTEM
        assert "Preserve placeholders" in SYNTHESIS_SYSTEM
        assert "verbatim" in SYNTHESIS_SYSTEM.lower()
        # An anti-example the clause flags must appear so the model sees
        # what fabrication looks like.
        assert "192.168" in SYNTHESIS_SYSTEM
        assert "tskey-" in SYNTHESIS_SYSTEM

    def test_synthesis_system_has_capability_boundary(self):
        from app.modules.execution_compile import SYNTHESIS_SYSTEM
        assert "Capability boundary" in SYNTHESIS_SYSTEM
        assert "past-tense" in SYNTHESIS_SYSTEM.lower()

    def test_synthesis_tool_description_mirrors_placeholder_rule(self):
        from app.modules.execution_compile import SYNTHESIS_TOOL
        desc = SYNTHESIS_TOOL.description
        summary_desc = SYNTHESIS_TOOL.input_schema["properties"]["summary"]["description"]
        # Tool-call-level reminder so the model sees the constraint at the
        # tool boundary even if the system prompt is truncated.
        assert "Do not invent" in desc
        assert "preserve placeholders" in desc.lower()
        assert "verbatim" in summary_desc.lower()


@pytest.mark.smoke
class TestShellGuardForSynthesis:
    """§17.360 — Shell-tagged source nodes short-circuit synthesis the same
    way CodeGen does. Runbooks are deliverables-as-instructions; rewriting
    them as narrative prose silently corrupts the output (LLM helpfully
    fills in `<PLACEHOLDER>` tokens, drops "## Run this" structure)."""

    async def test_shell_leaf_skips_synthesis_strategy_0_single(self):
        """Single-leaf with tool=Shell → guard fires, runbook returned verbatim."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        runbook = (
            "## Run this\n```bash\nssh root@<PROXMOX_HOST_IP>\n```\n\n"
            "## Verify\n- `pveversion`\n"
        )
        db = make_mock_db([
            {"node_key": "T1", "title": "Install Proxmox VE host",
             "tool": "Shell", "status": "done", "output_text": runbook,
             "is_output_node": True},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call", new=synth_call):
            result, was_syn = await _compile_output("job-1", db)
        # Runbook preserved verbatim — placeholders intact, no rewriting.
        # §17.506 — a plan-only banner is prepended (T1 is a done Shell node),
        # so assert the runbook body is present rather than exact-equal.
        assert runbook in result
        assert "<PROXMOX_HOST_IP>" in result
        assert was_syn is False
        synth_call.assert_not_called()

    async def test_shell_only_multi_leaf_skips_synthesis(self):
        """All-Shell multi-leaf set → homogeneous-tool detection passes
        source_tool='Shell' so the guard short-circuits. Closes the
        homelab retry's compile-step regression."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "Install host",
             "tool": "Shell", "status": "done",
             "output_text": "## Run this\nssh root@<HOST>\n",
             "is_output_node": True},
            {"node_key": "T2", "title": "Configure VLANs",
             "tool": "Shell", "status": "done",
             "output_text": "## Run this\npvesh create /nodes/<NODE>/network\n",
             "is_output_node": True},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call", new=synth_call):
            result, was_syn = await _compile_output("job-1", db)
        # Both runbooks present, placeholders intact, no LLM rewrite.
        assert "<HOST>" in result
        assert "<NODE>" in result
        assert was_syn is False
        synth_call.assert_not_called()

    async def test_mixed_shell_llm_multi_leaf_still_synthesizes(self):
        """Heterogeneous leaf set (Shell + LLM) → no homogeneous-tool
        short-circuit; synthesis runs but is constrained by the new
        SYNTHESIS_SYSTEM clauses (covered by TestSynthesisPromptGuards)."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Install host",
             "tool": "Shell", "status": "done",
             "output_text": "## Run this\nssh root@<HOST>\n",
             "is_output_node": True},
            {"node_key": "T2", "title": "Document setup",
             "tool": "LLM", "status": "done",
             "output_text": "Documentation prose about the install.",
             "is_output_node": True},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_response(
                     "Synthesized narrative covering install and documentation.",
                 )),
             ):
            result, was_syn = await _compile_output("job-1", db)
        # §17.506 — T1 is a done Shell node → plan-only banner prepended;
        # synthesis still ran (was_syn) and its narrative is present.
        assert "Synthesized narrative covering install and documentation." in result
        assert was_syn is True


@pytest.mark.smoke
class TestSkippedVerifyBanner:
    """X.2 — when N nodes were skipped during execution, prepend an
    operational banner so consumers can tell the deliverable doesn't
    cover the full DAG. Sits AFTER synthesis on the call path so it
    survives any LLM rewriting."""

    async def test_no_skipped_no_banner(self):
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "beta"},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert "Note:" not in text_value
        assert "skipped" not in text_value.lower()

    async def test_one_skipped_banner_singular(self):
        """skipped_count=1 → singular wording ('1 of 3 task were skipped' would
        be ungrammatical; banner uses 'task' singular)."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "beta"},
            {"node_key": "T3", "title": "C", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 1 of 3 task were skipped")
        # Body still present after the banner
        assert "alpha" in text_value
        assert "beta" in text_value

    async def test_multiple_skipped_banner_plural(self):
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "skipped", "output_text": None},
            {"node_key": "T3", "title": "C", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 2 of 3 tasks were skipped")
        assert "alpha" in text_value

    async def test_banner_survives_synthesis(self):
        """Banner is prepended AFTER synthesis, so even if the LLM rewrites
        the heuristic into a clean narrative, the banner stays at the top."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output
        from app.providers.base import ModelResponse, ToolCall

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
            {"node_key": "T3", "title": "Polish", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        ok_resp = ModelResponse(
            text="", model="fake", success=True,
            tool_calls=[ToolCall(id="t0", name="render_summary",
                                  arguments={"summary": "Clean narrative output."})],
        )
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(return_value=ok_resp)):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 1 of 3 task were skipped")
        # Synthesized body follows the banner
        assert "Clean narrative output." in text_value
        assert was_syn is True

    async def test_empty_result_no_banner(self):
        """When _compile_output returns None, banner doesn't fire (no text to prepend to)."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "skipped", "output_text": None},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is None
        assert was_syn is False


@pytest.mark.smoke
class TestCompileExplicitDeliverable:
    """§17.475 — is_deliverable is the PRIMARY Strategy-0 signal; the
    is_output_node + dominant-leaf path is demoted to the no-marker fallback."""

    async def test_explicit_deliverable_picked_over_leaves(self):
        # The deliverable (T3, a NON-leaf CodeGen node) is marked; the two
        # is_output_node leaves (T4 docs, T5 validation) are NOT. Compile must
        # pick T3 verbatim and ignore the leaves / dominant-leaf entirely —
        # the exact mdsplit shape that the old leaf logic got wrong.
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM", "status": "done",
             "output_text": "plan", "is_output_node": False,
             "is_deliverable": False, "depends_on": []},
            {"node_key": "T3", "title": "Write CLI", "tool": "CodeGen",
             "status": "done", "output_text": "def main(): return 0",
             "is_output_node": False, "is_deliverable": True, "depends_on": ["T1"]},
            {"node_key": "T4", "title": "Docs", "tool": "LLM", "status": "done",
             "output_text": "usage docs", "is_output_node": True,
             "is_deliverable": False, "depends_on": ["T3"]},
            {"node_key": "T5", "title": "Validate", "tool": "LLM", "status": "done",
             "output_text": "validation report", "is_output_node": True,
             "is_deliverable": False, "depends_on": ["T3"]},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result == "def main(): return 0"      # CodeGen deliverable, raw
        assert "usage docs" not in result
        assert "validation report" not in result

    async def test_multiple_deliverables_joined(self):
        # Two genuine artifacts (library + README) both marked → both rendered.
        db = make_mock_db([
            {"node_key": "T1", "title": "Library", "tool": "CodeGen",
             "status": "done", "output_text": "LIB_CODE", "is_output_node": True,
             "is_deliverable": True, "depends_on": []},
            {"node_key": "T2", "title": "README", "tool": "LLM", "status": "done",
             "output_text": "README_TEXT", "is_output_node": True,
             "is_deliverable": True, "depends_on": []},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "LIB_CODE" in result and "README_TEXT" in result

    async def test_no_marker_falls_back_to_is_output_node(self):
        # No is_deliverable anywhere (pre-§17.475 job) → fallback to
        # is_output_node + dominant-leaf, identical to prior behavior. Two
        # co-equal leaves (closure size 1) → both joined.
        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM", "status": "done",
             "output_text": "alpha", "is_output_node": True,
             "is_deliverable": False},
            {"node_key": "T2", "title": "B", "tool": "LLM", "status": "done",
             "output_text": "beta", "is_output_node": True,
             "is_deliverable": False},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert "alpha" in result and "beta" in result and "---" in result
