"""Tests for execution_agent — tool-specific error handling (SearXNG, Milvus) + skip_node return shape.

Split from the original test_execution_agent.py (#9.6). Shared imports
and helpers live in _execution_agent_shared.
"""
from tests._execution_agent_shared import *  # noqa: F401, F403

@pytest.mark.smoke
class TestSearXNGSearchErrorHandling:
    """_searxng_search graceful degradation on failures.

    _searxng_search lazy-imports get_searxng_client inside the function,
    so the correct patch target is its source module, not execution_agent.
    """
    @staticmethod
    def _mock_client(*, response=None, side_effect=None):
        client = AsyncMock()
        if side_effect is not None:
            client.get = AsyncMock(side_effect=side_effect)
        else:
            client.get = AsyncMock(return_value=response)
        return client

    async def test_http_error_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "failed" in result.lower()

    async def test_timeout_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        client = self._mock_client(side_effect=httpx.TimeoutException("timed out"))
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "failed" in result.lower()

    async def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "no search results" in result.lower()

    async def test_success_formats_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [
            {"title": "Result 1", "content": "Snippet 1", "url": "https://example.com"},
        ]}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "[1] Result 1" in result
        assert "Snippet 1" in result


@pytest.mark.smoke
class TestMilvusSearchErrorHandling:
    """_milvus_search graceful degradation on failures."""

    async def test_connection_error_returns_failure_string(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(side_effect=ConnectionError("Milvus unreachable"))

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "failed" in result.lower()

    async def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": []})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "no knowledge base results" in result.lower()

    async def test_success_formats_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": [
            {"title": "RAG Architecture", "content": "Retrieval-augmented generation..."},
            {"title": "Embeddings", "content": "Vector representations..."},
        ]})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "[1] RAG Architecture" in result
        assert "[2] Embeddings" in result
        assert "Retrieval-augmented" in result


@pytest.mark.smoke
class TestSkipNodeReturnShape:
    """#95: skip_node return dict conforms to ExecutionResult schema."""

    async def test_skipped_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = {"id": "node-uuid-1"}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)
        db.commit = AsyncMock()

        result = await skip_node("job-1", "T1", db)

        validated = ExecutionResult(**result)
        assert validated.status == "skipped"
        assert validated.node_key == "T1"

    async def test_not_found_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)

        result = await skip_node("job-1", "T99", db)

        validated = ExecutionResult(**result)
        assert validated.status == "error"
        assert validated.message is not None
        assert "not found" in validated.message.lower()

class TestSystemPromptRouting:
    """_system_for_tool routes LLM/CodeGen tool nodes to the right system prompt.

    The execution agent now sends a system message constraining model output
    style. LLM nodes get strict prose rules; CodeGen nodes get code-friendly
    rules that allow fenced blocks. This class verifies the routing.
    """

    def test_llm_tool_gets_strict_prompt(self):
        from app.modules.execution_agent import _system_for_tool, EXECUTION_SYSTEM_LLM
        assert _system_for_tool("LLM") is EXECUTION_SYSTEM_LLM

    def test_codegen_tool_gets_code_prompt(self):
        from app.modules.execution_agent import (
            _system_for_tool,
            EXECUTION_SYSTEM_CODEGEN,
        )
        assert _system_for_tool("CodeGen") is EXECUTION_SYSTEM_CODEGEN

    def test_unknown_tool_falls_back_to_llm_prompt(self):
        from app.modules.execution_agent import _system_for_tool, EXECUTION_SYSTEM_LLM
        # Milvus/SearXNG/anything else -> default to strict LLM rules
        assert _system_for_tool("Milvus") is EXECUTION_SYSTEM_LLM
        assert _system_for_tool("SearXNG") is EXECUTION_SYSTEM_LLM
        assert _system_for_tool("") is EXECUTION_SYSTEM_LLM
        assert _system_for_tool("nonsense") is EXECUTION_SYSTEM_LLM

    def test_llm_prompt_forbids_markdown_chrome(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # Spot-check the constraint surface
        assert "No markdown tables" in EXECUTION_SYSTEM_LLM
        assert "No emoji" in EXECUTION_SYSTEM_LLM
        assert "No fenced code blocks" in EXECUTION_SYSTEM_LLM

    def test_codegen_prompt_allows_code_blocks(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        # CodeGen should NOT forbid fenced blocks (the LLM rule)
        assert "No fenced code blocks" not in EXECUTION_SYSTEM_CODEGEN
        # And SHOULD lead with the code
        assert "Lead with the code" in EXECUTION_SYSTEM_CODEGEN

    def test_both_prompts_warn_against_emoji(self):
        from app.modules.execution_agent import (
            EXECUTION_SYSTEM_LLM,
            EXECUTION_SYSTEM_CODEGEN,
        )
        assert "No emoji" in EXECUTION_SYSTEM_LLM
        assert "No emoji" in EXECUTION_SYSTEM_CODEGEN

    def test_both_prompts_handle_upstream_context(self):
        from app.modules.execution_agent import (
            EXECUTION_SYSTEM_LLM,
            EXECUTION_SYSTEM_CODEGEN,
        )
        # Both prompts must instruct the model how to treat upstream node outputs
        for prompt in (EXECUTION_SYSTEM_LLM, EXECUTION_SYSTEM_CODEGEN):
            assert "upstream" in prompt.lower()

    # ----- §17.359 — Shell tool routing + no-fake-execution clause -----

    def test_shell_tool_gets_runbook_prompt(self):
        from app.modules.execution_agent import (
            _system_for_tool,
            EXECUTION_SYSTEM_RUNBOOK,
        )
        assert _system_for_tool("Shell") is EXECUTION_SYSTEM_RUNBOOK
        # Case-insensitive: hand-edited lowercase row must land the same way.
        assert _system_for_tool("shell") is EXECUTION_SYSTEM_RUNBOOK

    def test_runbook_prompt_forbids_past_tense_narration(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_RUNBOOK
        # Spot-check the anti-fake-execution surface — these substrings are
        # the closing the OVERVIEW §17.359 trial regression hit.
        assert "past-tense" in EXECUTION_SYSTEM_RUNBOOK.lower()
        assert "Run this" in EXECUTION_SYSTEM_RUNBOOK
        assert "Verify" in EXECUTION_SYSTEM_RUNBOOK
        assert "Rollback" in EXECUTION_SYSTEM_RUNBOOK

    def test_runbook_prompt_calls_out_destructive_action_section(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_RUNBOOK
        # The "Risk" header is what prevents an unmarked rm/dd/format step.
        assert "Risk" in EXECUTION_SYSTEM_RUNBOOK

    def test_llm_prompt_forbids_fake_execution_narration(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The capability-boundary clause is the surface that closes the
        # homelab regression: LLM nodes claimed "Created the file" /
        # "tcpdump shows..." without having done anything.
        assert "cannot run commands" in EXECUTION_SYSTEM_LLM
        assert "past-tense" in EXECUTION_SYSTEM_LLM.lower()

    def test_codegen_prompt_forbids_fake_execution_narration(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        assert "NOT running it" in EXECUTION_SYSTEM_CODEGEN

    def test_shell_in_valid_tools(self):
        from app.config import VALID_TOOLS
        assert "Shell" in VALID_TOOLS

    # ----- §17.360 — LLM nodes must not fabricate concrete values -----

    def test_llm_prompt_has_no_fabrication_guard(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The §17.360 clause closes the homelab T9 (documentation, tool=LLM)
        # regression that invented `192.168.10.100`, `tskey-abc123...`,
        # `pve01.internal`, `0000:01:00.0` instead of preserving the
        # upstream Shell nodes' <PLACEHOLDER> tokens.
        assert "No-fabrication guard" in EXECUTION_SYSTEM_LLM
        for marker in ("IPs", "auth keys", "hostnames", "PCI"):
            assert marker in EXECUTION_SYSTEM_LLM, f"missing {marker!r}"

    def test_llm_prompt_requires_placeholder_preservation(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        assert "preserve the placeholder verbatim" in EXECUTION_SYSTEM_LLM
        # Anti-example markers — model should see what fabrication looks like
        # so it can pattern-match against its own draft.
        assert "192.168.10.100" in EXECUTION_SYSTEM_LLM
        assert "tskey-" in EXECUTION_SYSTEM_LLM

    # ----- §17.361 — runbook placeholder-first rule -----

    def test_runbook_prompt_has_placeholder_first_rule(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_RUNBOOK
        # §17.361 closes the §17.360 retry's residual: Shell runbooks were
        # emitting `e.g., 192.168.1.10/24` example values in commands
        # instead of routing operator-supplied values through
        # <SCREAMING_SNAKE> placeholders.
        assert "Placeholder-first rule" in EXECUTION_SYSTEM_RUNBOOK
        # SCREAMING_SNAKE_CASE convention named (model needs the canonical
        # name to recall the convention).
        assert "SCREAMING_SNAKE_CASE" in EXECUTION_SYSTEM_RUNBOOK
        # Good/Bad anti-example pair must both be present so the model has
        # a concrete contrast to pattern-match against.
        assert "Bad:" in EXECUTION_SYSTEM_RUNBOOK
        assert "Good:" in EXECUTION_SYSTEM_RUNBOOK
        assert "<HOST_IP>" in EXECUTION_SYSTEM_RUNBOOK
        assert "<PROXMOX_HOSTNAME>" in EXECUTION_SYSTEM_RUNBOOK

    def test_runbook_prompt_names_conventional_concrete_exemptions(self):
        """The rule must allow conventional shell variables (`/dev/sdX`,
        `/path/to/<FILE>`, fixed deployment flags like `bs=4M`) — otherwise
        the model over-corrects and starts placeholder-wrapping things
        that are NOT operator-supplied. Spot-check the exemption list."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_RUNBOOK
        assert "/dev/sdX" in EXECUTION_SYSTEM_RUNBOOK
        assert "bs=4M" in EXECUTION_SYSTEM_RUNBOOK
        # The decision rule the model applies.
        assert "does this value vary per" in EXECUTION_SYSTEM_RUNBOOK.lower()

