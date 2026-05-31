"""§17.184 — unit tests for app/modules/prompt_assembly.py.

The audit (AUDIT.md 3.2) flagged this 255-line module as having no
dedicated test file despite being the single source of truth for the
upstream-last prompt that BOTH the autonomous executor and Assist Mode
feed to the LLM. Bugs here are silent quality regressions (truncation
miscount, wrong context order, missing RAG block) that don't surface in
execution-agent tests because those mock the prompt builder.

These tests cover:

  * system_for_tool — CodeGen vs LLM branch
  * truncate_output — head/tail preserve + marker math
  * build_base_prompt — template path, no-template fallback, brief-shape variants
  * fetch_upstream_outputs — SQL helper against a mocked db
  * truncate_upstream_outputs — proportional truncation, min_chunk floor
  * render_upstream_block — header shape, empty short-circuit
  * assemble_step_context — full upstream-last pipeline (incl. grounding kinds)

Test isolation: ``db`` is a duck-typed mock — only ``execute(...).fetchall()``
is exercised by the production code, so an ``AsyncMock`` returning a fake
result object suffices. No DB, no live LLM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules import prompt_assembly as pa


# ---------------------------------------------------------------------------
# system_for_tool
# ---------------------------------------------------------------------------

class TestSystemForTool:
    def test_codegen_returns_codegen_prompt(self):
        out = pa.system_for_tool("CodeGen")
        assert "fenced block" in out
        assert out == pa.EXECUTION_SYSTEM_CODEGEN

    def test_llm_returns_llm_prompt(self):
        out = pa.system_for_tool("LLM")
        assert "Direct, focused prose" in out
        assert out == pa.EXECUTION_SYSTEM_LLM

    @pytest.mark.parametrize("tool", ["SearXNG", "Milvus", "", "unknown"])
    def test_non_codegen_tools_get_llm_prompt(self, tool):
        # Any non-CodeGen, non-Shell tool gets the generic LLM system
        # prompt. §17.359 adds Shell as a second specialized branch alongside
        # CodeGen — this list intentionally excludes "Shell".
        assert pa.system_for_tool(tool) == pa.EXECUTION_SYSTEM_LLM

    def test_shell_returns_runbook_prompt(self):
        # §17.359 — Shell mirror must match execution_agent._system_for_tool.
        assert pa.system_for_tool("Shell") == pa.EXECUTION_SYSTEM_RUNBOOK
        assert pa.system_for_tool("shell") == pa.EXECUTION_SYSTEM_RUNBOOK
        assert "Run this" in pa.EXECUTION_SYSTEM_RUNBOOK
        assert "past-tense" in pa.EXECUTION_SYSTEM_RUNBOOK.lower()

    def test_llm_prompt_mirror_has_no_fabrication_guard(self):
        # §17.360 — the assist-mode mirror must carry the same anti-
        # fabrication clauses as the autonomous executor's LLM prompt.
        # Without this mirror, an assist-mode operator sees a different
        # prompt than the autonomous run, breaking the W.10 invariant.
        assert "No-fabrication guard" in pa.EXECUTION_SYSTEM_LLM
        assert "preserve the placeholder verbatim" in pa.EXECUTION_SYSTEM_LLM

    def test_runbook_mirror_has_placeholder_first_rule(self):
        # §17.361 — assist-mode mirror must carry the placeholder-first
        # rule so assist-mode runbook output matches autonomous runs.
        assert "Placeholder-first rule" in pa.EXECUTION_SYSTEM_RUNBOOK
        assert "SCREAMING_SNAKE_CASE" in pa.EXECUTION_SYSTEM_RUNBOOK
        assert "<HOST_IP>" in pa.EXECUTION_SYSTEM_RUNBOOK

    def test_llm_mirror_has_brief_spec_and_validation_clauses(self):
        # §17.365 + §17.366 — mirror invariant. Without these the
        # assist-mode operator sees a different prompt than autonomous.
        assert "Brief-spec fidelity" in pa.EXECUTION_SYSTEM_LLM
        assert "Validation grounding" in pa.EXECUTION_SYSTEM_LLM
        assert "MET" in pa.EXECUTION_SYSTEM_LLM and "NOT MET" in pa.EXECUTION_SYSTEM_LLM

    def test_codegen_mirror_has_brief_spec_clause(self):
        # §17.365 — CodeGen mirror.
        assert "Brief-spec fidelity" in pa.EXECUTION_SYSTEM_CODEGEN
        assert "module-level constant" in pa.EXECUTION_SYSTEM_CODEGEN


# ---------------------------------------------------------------------------
# truncate_output — head/tail preserve + marker.
# ---------------------------------------------------------------------------

class TestTruncateOutput:
    def test_under_cap_returns_unchanged(self):
        text = "a" * 100
        assert pa.truncate_output(text, 200) == text

    def test_exactly_at_cap_returns_unchanged(self):
        text = "x" * 100
        assert pa.truncate_output(text, 100) == text

    def test_over_cap_inserts_truncation_marker(self):
        text = "A" * 5000 + "B" * 5000  # 10000 chars
        out = pa.truncate_output(text, 2000)  # 20% head, 20% tail = 400+400
        assert out.startswith("A" * 400)
        assert out.endswith("B" * 400)
        assert "[...truncated 9200 chars...]" in out

    def test_head_tail_each_get_20pct_of_cap(self):
        text = "z" * 1000
        out = pa.truncate_output(text, 100)
        # head=20, tail=20, marker between
        assert out[:20] == "z" * 20
        assert out[-20:] == "z" * 20
        assert "truncated" in out


# ---------------------------------------------------------------------------
# build_base_prompt — template vs no-template; brief shapes.
# ---------------------------------------------------------------------------

class TestBuildBasePrompt:
    def test_template_takes_precedence_over_fallback(self):
        node = {"prompt_template": "Do exactly X.", "title": "Step 1"}
        brief = {"description": "the goal"}
        out = pa.build_base_prompt(node, brief)
        assert out.startswith("Do exactly X.")
        assert "the goal" in out

    def test_no_template_falls_back_to_default_shape(self):
        node = {"title": "Build the thing"}
        brief = {"description": "deliver value"}
        out = pa.build_base_prompt(node, brief)
        assert "Execute this task: Build the thing" in out
        assert "Project goal: deliver value" in out
        assert "ground truth" in out.lower()

    def test_brief_missing_description_falls_back_to_first_goal(self):
        node = {"title": "T"}
        brief = {"goals": ["first goal", "second goal"]}
        out = pa.build_base_prompt(node, brief)
        assert "Project goal: first goal" in out

    def test_brief_with_neither_description_nor_goals_emits_empty_goal(self):
        node = {"title": "T"}
        brief = {}
        out = pa.build_base_prompt(node, brief)
        assert "Project goal:" in out

    def test_no_brief_at_all_does_not_crash(self):
        node = {"title": "T"}
        out = pa.build_base_prompt(node, None)
        assert "Execute this task: T" in out

    def test_no_title_no_template_still_returns_a_string(self):
        out = pa.build_base_prompt({}, {"description": "x"})
        assert "Execute this task:" in out


# ---------------------------------------------------------------------------
# fetch_upstream_outputs — SQL helper.
# ---------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, node_key: str, output_text: str | None):
        self.node_key = node_key
        self.output_text = output_text


def _fake_db(rows: list[_FakeRow]) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


class TestFetchUpstreamOutputs:
    async def test_empty_depends_on_returns_empty_dict_without_db_call(self):
        db = AsyncMock()
        out = await pa.fetch_upstream_outputs(db, "j1", [])
        assert out == {}
        db.execute.assert_not_awaited()

    async def test_returns_node_key_to_output_map(self):
        rows = [_FakeRow("T1", "first output"), _FakeRow("T2", "second output")]
        db = _fake_db(rows)
        out = await pa.fetch_upstream_outputs(db, "job-x", ["T1", "T2"])
        assert out == {"T1": "first output", "T2": "second output"}

    async def test_none_output_text_becomes_empty_string(self):
        """A node row with output_text NULL should map to '' so callers
        don't have to handle Optional[str] everywhere."""
        rows = [_FakeRow("T1", None)]
        db = _fake_db(rows)
        out = await pa.fetch_upstream_outputs(db, "j1", ["T1"])
        assert out == {"T1": ""}

    async def test_query_filters_to_done_status(self):
        """The WHERE clause must require status='done' so a still-running
        upstream doesn't leak partial state into the next node's prompt."""
        rows = []
        db = _fake_db(rows)
        await pa.fetch_upstream_outputs(db, "j1", ["T1"])
        # The first positional arg is the SQLAlchemy ``text()`` clause; the
        # second is the param dict.
        call = db.execute.await_args
        sql_str = str(call.args[0])
        assert "status = 'done'" in sql_str


# ---------------------------------------------------------------------------
# truncate_upstream_outputs — proportional truncation.
# ---------------------------------------------------------------------------

class TestTruncateUpstreamOutputs:
    def test_empty_in_empty_out(self):
        out, trunc = pa.truncate_upstream_outputs({})
        assert out == {}
        assert trunc == []

    def test_under_cap_returns_copy_unchanged(self):
        inp = {"T1": "short", "T2": "also short"}
        out, trunc = pa.truncate_upstream_outputs(inp, max_total_chars=1000)
        assert out == inp
        assert trunc == []
        # Copy invariant — same content, distinct object.
        assert out is not inp

    def test_over_cap_truncates_oversized_nodes(self):
        inp = {
            "T1": "a" * 500,    # 50% of total
            "T2": "b" * 500,    # 50% of total
        }
        out, trunc = pa.truncate_upstream_outputs(
            inp, max_total_chars=200, min_chunk=20,
        )
        # Each gets max(20, 200*0.5)=100 chars share; both exceed → both truncated.
        assert set(trunc) == {"T1", "T2"}
        assert "truncated" in out["T1"]
        assert "truncated" in out["T2"]

    def test_small_node_passes_through_when_within_min_chunk(self):
        """A node within the min_chunk floor is left alone even though the
        total exceeds the cap. T1 is shorter than the min_chunk → its share
        is floored at min_chunk (20), and since 15 < 20 it survives untouched."""
        inp = {
            "T1": "a" * 15,     # smaller than min_chunk → survives untouched
            "T2": "b" * 5000,   # dominant blob → still truncated
        }
        out, trunc = pa.truncate_upstream_outputs(
            inp, max_total_chars=200, min_chunk=20,
        )
        assert out["T1"] == "a" * 15
        assert "T1" not in trunc
        assert "T2" in trunc

    def test_min_chunk_floor_applied(self):
        """If a node's proportional share falls below min_chunk, min_chunk wins."""
        inp = {"T1": "a" * 100, "T2": "b" * 99_900}  # T2 dominates ratio
        out, trunc = pa.truncate_upstream_outputs(
            inp, max_total_chars=1000, min_chunk=50,
        )
        # T1's proportional share would be 1 char; the floor lifts it to 50.
        # 100-char input >50, so it gets truncated (>= min_chunk path).
        assert "T1" in trunc

    def test_settings_used_when_caller_passes_none(self, monkeypatch):
        """Default cap/min_chunk come from settings — not hardcoded."""
        monkeypatch.setattr(settings, "max_upstream_chars", 50)
        monkeypatch.setattr(settings, "compile_output_min_chunk", 10)
        inp = {"T1": "x" * 100}
        out, trunc = pa.truncate_upstream_outputs(inp)
        assert "T1" in trunc
        assert len(out["T1"]) <= 100  # truncated


# ---------------------------------------------------------------------------
# render_upstream_block — header shape.
# ---------------------------------------------------------------------------

class TestRenderUpstreamBlock:
    def test_empty_short_circuits_to_empty_string(self):
        assert pa.render_upstream_block({}) == ""

    def test_renders_each_node_with_header(self):
        out = pa.render_upstream_block({"T1": "first", "T2": "second"})
        assert "### T1\nfirst" in out
        assert "### T2\nsecond" in out

    def test_block_is_upstream_last_invariant(self):
        """The header must say 'YOUR TASK ... above' so the LLM/human knows
        the upstream block precedes the literal task. Without this header
        the model could mistake the upstream context for the task itself."""
        out = pa.render_upstream_block({"T1": "x"})
        assert "Upstream Node Outputs (MANDATORY CONTEXT" in out
        assert "YOUR TASK" in out
        assert "build on the upstream outputs above" in out


# ---------------------------------------------------------------------------
# assemble_step_context — full pipeline.
# ---------------------------------------------------------------------------

class TestAssembleStepContext:
    async def test_no_upstream_no_grounding_minimal_path(self):
        node = {"node_key": "T1", "title": "First step", "tool": "LLM",
                "domain": None, "depends_on": []}
        brief = {"description": "the goal"}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node, brief=brief,
        )
        assert isinstance(ctx, pa.StepContext)
        assert ctx.node_key == "T1"
        assert ctx.tool == "LLM"
        assert ctx.upstream_outputs == {}
        assert ctx.grounding == ""
        assert ctx.grounding_kind is None
        # The assembled prompt is just the base prompt — no upstream prepend.
        assert ctx.assembled_prompt == ctx.base_prompt

    async def test_with_upstream_prepends_block(self):
        """assembled_prompt = render_upstream_block(upstream) + base prompt."""
        node = {"node_key": "T2", "title": "Step 2", "tool": "LLM",
                "domain": None, "depends_on": ["T1"]}
        rows = [_FakeRow("T1", "upstream output here")]
        db = _fake_db(rows)
        ctx = await pa.assemble_step_context(
            db=db, job_id="j1", node=node, brief={"description": "goal"},
        )
        assert "T1" in ctx.upstream_outputs
        # Critical invariant: upstream MUST appear BEFORE the base prompt.
        assert ctx.assembled_prompt.index("upstream output here") < \
               ctx.assembled_prompt.index("Execute this task: Step 2")

    async def test_codegen_node_gets_codegen_system_prompt(self):
        node = {"node_key": "T1", "title": "Implement X", "tool": "CodeGen",
                "domain": None, "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node, brief={"description": "g"},
        )
        assert ctx.system_prompt == pa.EXECUTION_SYSTEM_CODEGEN

    async def test_grounding_milvus_kind_uses_kb_header(self):
        """grounding_kind='milvus' renders as '## Knowledge Base Results'."""
        async def fake_grounding(*, tool, title, node_key, domain, brief):
            return ("KB excerpt #1\nKB excerpt #2", "milvus")

        node = {"node_key": "T1", "title": "T", "tool": "LLM",
                "domain": None, "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node,
            brief={"description": "g"}, fetch_grounding=fake_grounding,
        )
        assert "Knowledge Base Results" in ctx.assembled_prompt
        assert "KB excerpt #1" in ctx.assembled_prompt
        assert ctx.grounding_kind == "milvus"

    async def test_grounding_searxng_kind_uses_web_header(self):
        async def fake_grounding(*, tool, title, node_key, domain, brief):
            return ("search result 1", "searxng")

        node = {"node_key": "T1", "title": "T", "tool": "SearXNG",
                "domain": None, "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node,
            brief={"description": "g"}, fetch_grounding=fake_grounding,
        )
        assert "Web Search Results" in ctx.assembled_prompt
        assert ctx.grounding_kind == "searxng"

    async def test_grounding_generic_rag_uses_ground_truth_header(self):
        """Unknown grounding_kind ('rag' or any other) gets the generic
        'GROUND TRUTH ... authoritative' phrasing, not the typed headers."""
        async def fake_grounding(*, tool, title, node_key, domain, brief):
            return ("rag chunk", "rag")

        node = {"node_key": "T1", "title": "T", "tool": "LLM",
                "domain": "eng", "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node,
            brief={"description": "g"}, fetch_grounding=fake_grounding,
        )
        assert "GROUND TRUTH" in ctx.assembled_prompt
        assert "rag chunk" in ctx.assembled_prompt
        assert "Knowledge Base Results" not in ctx.assembled_prompt
        assert "Web Search Results" not in ctx.assembled_prompt

    async def test_grounding_empty_string_skipped(self):
        """When the grounding callable returns '', no grounding block is added."""
        async def fake_grounding(*, tool, title, node_key, domain, brief):
            return ("", None)

        node = {"node_key": "T1", "title": "T", "tool": "LLM",
                "domain": None, "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node,
            brief={"description": "g"}, fetch_grounding=fake_grounding,
        )
        assert "Knowledge Base Results" not in ctx.assembled_prompt
        assert "GROUND TRUTH" not in ctx.assembled_prompt
        # base prompt is the entire output.
        assert ctx.assembled_prompt == ctx.base_prompt

    async def test_truncated_upstream_keys_surface_on_context(self):
        """assemble_step_context exposes which upstream nodes were truncated
        so Assist Mode can flag the gap to the human."""
        big = "X" * 50_000  # forces truncation against the 8k default
        node = {"node_key": "T2", "title": "T", "tool": "LLM",
                "domain": None, "depends_on": ["T1"]}
        db = _fake_db([_FakeRow("T1", big)])
        ctx = await pa.assemble_step_context(
            db=db, job_id="j1", node=node, brief={"description": "g"},
        )
        assert "T1" in ctx.upstream_truncated_keys

    async def test_step_context_is_frozen_dataclass(self):
        """StepContext is immutable so it can be safely passed across async
        boundaries without surprise mutation by intermediate code."""
        node = {"node_key": "T1", "title": "T", "tool": "LLM",
                "domain": None, "depends_on": []}
        ctx = await pa.assemble_step_context(
            db=AsyncMock(), job_id="j1", node=node,
            brief={"description": "g"},
        )
        with pytest.raises((AttributeError, Exception)):
            ctx.node_key = "different"  # type: ignore[misc]
