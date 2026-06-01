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

    # ----- §17.365 — brief-spec fidelity -----

    def test_llm_prompt_has_brief_spec_fidelity_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The clause closes the CodeGen-retry's 2-of-9 language-map regression.
        assert "Brief-spec fidelity" in EXECUTION_SYSTEM_LLM
        # The "silent truncation" framing is the load-bearing phrase.
        assert "silently truncate" in EXECUTION_SYSTEM_LLM
        # Concrete anti-example markers — the model must see the actual
        # numbers from the regression so it can pattern-match.
        assert "9 supported languages" in EXECUTION_SYSTEM_LLM
        assert "./out" in EXECUTION_SYSTEM_LLM
        assert "Re-interpreting flag semantics" in EXECUTION_SYSTEM_LLM

    def test_codegen_prompt_has_brief_spec_fidelity_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        assert "Brief-spec fidelity" in EXECUTION_SYSTEM_CODEGEN
        # CodeGen-specific lift-to-constant rule
        assert "module-level constant" in EXECUTION_SYSTEM_CODEGEN
        # The concrete anti-example
        assert "9 language-to-extension mappings" in EXECUTION_SYSTEM_CODEGEN
        # "silent truncation" line-wraps in the prompt; collapse whitespace
        # before substring match so a future reflow doesn't break the test.
        assert "silent truncation" in (
            " ".join(EXECUTION_SYSTEM_CODEGEN.split()).lower()
        )

    # ----- §17.366 — validation grounding -----

    def test_llm_prompt_has_validation_grounding_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The clause closes the CodeGen-retry's T6 spec-restatement regression.
        assert "Validation grounding" in EXECUTION_SYSTEM_LLM
        # The MET / NOT MET / UNKNOWN format is the load-bearing pattern.
        assert "MET" in EXECUTION_SYSTEM_LLM
        assert "NOT MET" in EXECUTION_SYSTEM_LLM
        assert "UNKNOWN" in EXECUTION_SYSTEM_LLM
        # The validation-trigger keyword list
        assert "Validate" in EXECUTION_SYSTEM_LLM
        assert "Verify" in EXECUTION_SYSTEM_LLM

    def test_llm_prompt_validation_clause_forbids_silent_unknown_downgrade(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The specific failure mode — UNKNOWN getting silently flipped
        # to MET when evidence is missing — must be called out.
        assert "Do not silently downgrade UNKNOWN to MET" in EXECUTION_SYSTEM_LLM

    # ----- §17.368 — validation per-upstream evidence walk -----

    def test_llm_prompt_has_per_upstream_evidence_walk_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        # The §17.368 clause closes the retry's 13-MET-all-citing-T6 regression.
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Per-upstream evidence walk" in flat
        assert "Single-upstream-bias" in flat
        # The decision-rule phrasing the model needs to apply.
        assert "every upstream whose `name` or `outputs` field is relevant" in flat

    def test_llm_prompt_names_silently_passed_regression_as_worst_mode(self):
        """The §17.368 clause's load-bearing meta-claim — a wrong-upstream
        MET silently passes the regression the validation was supposed
        to catch. Pin so a future prompt edit can't soften this."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "silently passes a regression" in flat

    # ----- §17.369 — decision-output authority -----

    def test_llm_prompt_has_decision_authority_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Decision-output authority" in flat
        # The trigger condition phrasing.
        assert "`type` = `decision`" in flat
        # The Good/Bad framing — "advisory inspiration" vs verbatim.
        assert "advisory inspiration" in flat

    def test_codegen_prompt_has_decision_authority_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        flat = " ".join(EXECUTION_SYSTEM_CODEGEN.split())
        assert "Decision-output authority" in flat
        # The CodeGen-side framing — "decision is the authority; this
        # node is the encoder" — must be present so the model treats
        # the upstream as canonical, not advisory.
        assert "decision node is the authority" in flat
        # The transformation-vs-substitution distinction.
        assert "Transformation is fine; substitution is not" in flat

    # ----- §17.371 — decision-node tight scope -----

    def test_llm_prompt_has_decision_node_tight_scope_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Decision-node tight scope" in flat
        # The size-heuristic phrasing.
        assert "size heuristic" in flat.lower()
        # The §17.371 anti-example pin — the 35-pattern enumeration must
        # be explicit so the model has a concrete shape to avoid.
        assert "35-design-pattern enumeration" in flat
        # The cascade-to-downstream observation closes the loop.
        assert "decision-node scope explosion drove a downstream scope leak" in flat

    def test_decision_tight_scope_names_pattern_classes_explicitly(self):
        """The pattern dump and the downstream classes that came from it
        must be named so the model can pattern-match its own draft. Drop
        the concrete examples and the model regresses on the next
        adversarial brief."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        # Two of the 35-design-pattern enumeration's most distinctive
        # pattern names (high-confidence-it's-pattern-language signals).
        assert "Chain of Responsibility" in flat
        assert "Flyweight" in flat
        # The four classes that got implemented downstream because of
        # the pattern dump. Naming them ties cause to effect.
        assert "MarkdownProcessor" in flat
        assert "NullWriter" in flat
        assert "CodeBlockExtractor" in flat
        assert "FileWriter" in flat

    # ----- §17.372 — stay in the brief's domain -----

    def test_llm_prompt_has_stay_in_domain_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Stay in the brief's domain" in flat
        # The §17.372 anti-example pin — the oilfield content from this
        # retry must be named explicitly so the model has the failure
        # shape on record.
        assert "Drift Test Requirements" in flat
        assert "API 5CT" in flat
        assert "J55 BTC casing" in flat

    def test_stay_in_domain_clause_distinguishes_from_360(self):
        """§17.372 is upstream of §17.360 — fabricated values vs whole
        domain-wrong sections are different failure shapes. The clause
        must make the relationship explicit so the model doesn't
        conflate the two."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "upstream of §17.360" in flat
        # The crux distinction — quoting a plausible spec from training
        # data passes §17.360 (sourced, not invented) but fails §17.372
        # (wrong domain). Pin the phrase.
        assert "passed §17.360" in flat and "failed §17.372" in flat

    # ----- §17.373 — cite every code-bearing upstream -----

    def test_llm_prompt_has_cite_every_upstream_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Cite every code-bearing upstream" in flat
        # The mechanical-check phrasing — "every code-bearing upstream"
        # / "scan the report" — pins the operational shape.
        assert "every code-bearing upstream" in flat
        assert "scan the report" in flat
        # The §17.373 anti-example markers — the retry's 8 MET cluster
        # on T4/T5/T6.
        assert "T4 / T5 / T6" in flat or "T4/T5/T6" in flat

    # ----- §17.374 — no-runnable-script default -----

    def test_codegen_prompt_has_no_runnable_script_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        flat = " ".join(EXECUTION_SYSTEM_CODEGEN.split())
        assert "No-runnable-script default" in flat
        # The mechanical name-check phrasing — CLI vs module distinction.
        assert "name does NOT contain" in flat
        # The three forbidden constructs in non-CLI nodes must be named.
        assert 'if __name__ == "__main__":' in flat
        assert "def main()" in flat
        assert "argparse.ArgumentParser" in flat

    def test_codegen_no_runnable_script_names_anti_example_node(self):
        """The §17.374 clause must name the actual T3 ("Write filename
        generator") regression so the model has a concrete shape to
        pattern-match against. Drop the anti-example and the model
        regresses on the next retry."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        flat = " ".join(EXECUTION_SYSTEM_CODEGEN.split())
        assert "Write filename generator" in flat
        # The cascade observation — sibling nodes' outputs become
        # redundant or conflicting.
        assert "three competing CLIs" in flat

    def test_codegen_no_runnable_script_names_mechanical_check_keywords(self):
        """The §17.374 naming check is mechanical. The keyword list
        ("parser / generator / module / function / library / utility /
        helper / test / tests") must be present so the model has the
        exact triggers."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_CODEGEN
        flat = " ".join(EXECUTION_SYSTEM_CODEGEN.split())
        for kw in ("parser", "generator", "module", "function", "library",
                   "utility", "helper", "test"):
            assert kw in flat, f"missing keyword: {kw!r}"

    # ----- §17.378 — coverage section first -----

    def test_llm_prompt_has_coverage_section_first_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Coverage section first" in flat
        # The mandatory format markers — `## Coverage` + `## Verdicts`.
        assert "## Coverage" in flat
        assert "## Verdicts" in flat
        # The §17.376-references-the-substring-guard observation closes
        # the relationship between §17.378 and the runtime guard's
        # gaming failure.
        assert "substring guard passed on this shape" in flat

    def test_coverage_clause_calls_out_half_coverage_failure(self):
        """The §17.378 clause must explicitly forbid the half-coverage
        failure mode (Coverage section present but listing only some
        upstreams). Without it, the model regresses to the prior
        cluster shape but inside a Coverage section."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "list only 3 of 5 upstreams" in flat
        assert "Half-coverage is the same failure" in flat

    # ----- §17.379 — decision-node reference disambiguation -----

    def test_llm_prompt_has_decision_node_disambiguation_clause(self):
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        assert "Decision-node reference disambiguation" in flat
        # The actual T7-from-the-retry anti-example.
        assert "decision node (T2 or T3)" in flat
        # The corrective framing: name the specific T_N with type=decision.
        assert "name that T_N when you reference the decision" in flat

    def test_disambiguation_clause_names_decision_keyword_signals(self):
        """The §17.379 clause must list the keyword signals that
        identify a decision node by name ("Design", "Define",
        "Decide", "Choose", "Select"). Without these, the model has
        no way to pick the decision node from the upstream graph."""
        from app.modules.execution_agent import EXECUTION_SYSTEM_LLM
        flat = " ".join(EXECUTION_SYSTEM_LLM.split())
        for kw in ("Design", "Define", "Decide", "Choose", "Select"):
            assert kw in flat, f"missing decision keyword: {kw!r}"

