"""Sprint W.5 — tests for dag_generator.regenerate_subgraph and the
assist_replan.apply_selective_replan integration.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _result(rows: list[dict] | None = None, scalar=None):
    """Build a SQLAlchemy result-shaped mock from a list of mapping rows."""
    rows = rows or []
    mappings_obj = MagicMock()
    mappings_obj.all.return_value = rows
    result_obj = MagicMock()
    result_obj.mappings.return_value = mappings_obj
    result_obj.fetchall.return_value = rows
    result_obj.first.return_value = rows[0] if rows else None
    result_obj.scalar.return_value = (
        scalar if scalar is not None else (rows[0] if rows else None)
    )
    return result_obj


def _llm_response(text: str, success: bool = True, error: str | None = None):
    resp = MagicMock()
    resp.success = success
    resp.text = text
    resp.error = error
    resp.model = "fake-model"
    resp.total_duration_ms = 0
    return resp


def _subgraph_rows(brief: dict | None = None):
    """Two-row subgraph: T2 depends on T1 (root), T3 depends on T2."""
    brief = brief if brief is not None else {"description": "Build a parser"}
    return [
        {
            "refined_brief": brief,
            "node_key": "T2",
            "title": "Write parser",
            "prompt_template": "Implement Python parser using pyparsing",
            "depends_on": ["T1"],
        },
        {
            "refined_brief": brief,
            "node_key": "T3",
            "title": "Document parser",
            "prompt_template": "Write README documenting the Python API",
            "depends_on": ["T2"],
        },
    ]


@pytest.mark.smoke
class TestRegenerateSubgraph:
    """Direct tests for dag_generator.regenerate_subgraph."""

    async def test_empty_affected_returns_zero_no_llm_call(self):
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        mock_gen = AsyncMock()
        with patch(
            "app.modules.dag_generator.model_router.generate", new=mock_gen,
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="evidence",
                affected_keys=[], db=db,
            )
        assert result == {"regenerated": 0, "errors": []}
        mock_gen.assert_not_called()
        db.execute.assert_not_called()

    async def test_kill_switch_disabled(self):
        from app.modules.dag_generator import regenerate_subgraph
        from app.config import settings

        db = AsyncMock()
        mock_gen = AsyncMock()
        with patch.object(settings, "assist_replan_regen_enabled", False), \
             patch(
                 "app.modules.dag_generator.model_router.generate", new=mock_gen,
             ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="evidence",
                affected_keys=["T2", "T3"], db=db,
            )
        assert result["regenerated"] == 0
        assert "regen_disabled" in result["errors"]
        mock_gen.assert_not_called()

    async def test_happy_path_two_updates_persisted(self):
        """LLM returns 2 valid updates → 2 UPDATEs + commit."""
        from app.modules.dag_generator import regenerate_subgraph

        # Sequential execute returns:
        # 1. subgraph fetch
        # 2. root title fetch
        # 3. UPDATE T2
        # 4. UPDATE T3
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
            _result(),  # UPDATE T2
            _result(),  # UPDATE T3
        ])
        db.commit = AsyncMock()

        payload = {
            "updates": [
                {"node_key": "T2", "new_template": "Implement Rust parser using nom"},
                {"node_key": "T3", "new_template": "Write README documenting the Rust API"},
            ]
        }
        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1",
                root_evidence="Use Rust + nom (not Python)",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 2
        assert result["errors"] == []
        # Two UPDATE statements + one commit fired.
        assert db.commit.await_count == 1
        # Verify the UPDATE binds carry the new templates.
        update_calls = [
            c for c in db.execute.await_args_list
            if "UPDATE dag_nodes" in str(c.args[0])
        ]
        assert len(update_calls) == 2
        bind_templates = {c.args[1]["nk"]: c.args[1]["tpl"] for c in update_calls}
        assert "Rust" in bind_templates["T2"]
        assert "Rust" in bind_templates["T3"]

    async def test_llm_call_failure_fails_open(self):
        """RuntimeError from model_router.generate → return 0 + error, no DB writes."""
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
        ])
        db.commit = AsyncMock()

        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(side_effect=RuntimeError("ollama down")),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="x",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 0
        assert any("ollama down" in e for e in result["errors"])
        # No commit (no UPDATE executed).
        db.commit.assert_not_awaited()

    async def test_malformed_json_fails_open(self):
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
        ])
        db.commit = AsyncMock()

        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(return_value=_llm_response("not json {{{")),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="x",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 0
        assert "json_parse_failed" in result["errors"]
        db.commit.assert_not_awaited()

    async def test_unaffected_node_keys_ignored(self):
        """LLM hallucinates an update for T99 (not in subgraph) → skipped."""
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
            _result(),  # UPDATE T2
        ])
        db.commit = AsyncMock()

        payload = {
            "updates": [
                {"node_key": "T2", "new_template": "Implement Rust parser"},
                {"node_key": "T99", "new_template": "Phantom task — not in DAG"},
            ]
        }
        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(return_value=_llm_response(json.dumps(payload))),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="x",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 1
        # Phantom node logged in errors.
        assert any("ignored_unaffected_nodes" in e for e in result["errors"])
        assert any("T99" in e for e in result["errors"])
        # Only one UPDATE.
        update_calls = [
            c for c in db.execute.await_args_list
            if "UPDATE dag_nodes" in str(c.args[0])
        ]
        assert len(update_calls) == 1

    async def test_unsuccessful_response_fails_open(self):
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
        ])
        db.commit = AsyncMock()

        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(return_value=_llm_response("", success=False, error="rate-limited")),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="x",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 0
        assert any("rate-limited" in e for e in result["errors"])
        db.commit.assert_not_awaited()

    async def test_schema_mismatch_fails_open(self):
        """Parseable JSON but missing 'updates' key → 0 regen + error."""
        from app.modules.dag_generator import regenerate_subgraph

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result(_subgraph_rows()),
            _result([{"title": "Pick libraries"}]),
        ])
        db.commit = AsyncMock()

        with patch(
            "app.modules.dag_generator.model_router.generate",
            new=AsyncMock(return_value=_llm_response('{"foo": "bar"}')),
        ):
            result = await regenerate_subgraph(
                job_id="job-1", root_node_key="T1", root_evidence="x",
                affected_keys=["T2", "T3"], db=db,
            )

        assert result["regenerated"] == 0
        assert "schema_mismatch" in result["errors"]
        db.commit.assert_not_awaited()


@pytest.mark.smoke
class TestApplySelectiveReplanCallsRegen:
    """apply_selective_replan must invoke regenerate_subgraph with the
    BFS-affected node list and the human evidence."""

    async def test_calls_regenerate_with_affected_keys_and_evidence(self):
        from app.modules import assist_replan

        # Mock the BFS to return a deterministic subgraph.
        async def fake_downstream(*, db, job_id, root_node_key):
            return ["T2", "T3"]

        # Mock regenerate_subgraph to assert the call shape.
        captured = {}
        async def fake_regen(*, job_id, root_node_key, root_evidence,
                             affected_keys, db, model_overrides=None):
            captured["job_id"] = job_id
            captured["root_node_key"] = root_node_key
            captured["root_evidence"] = root_evidence
            captured["affected_keys"] = affected_keys
            return {"regenerated": 2, "errors": []}

        # Mock DB so the UPDATEs in the reset block don't matter.
        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        with patch.object(assist_replan, "downstream_node_keys", fake_downstream), \
             patch(
                 "app.modules.dag_generator.regenerate_subgraph",
                 fake_regen,
             ):
            result = await assist_replan.apply_selective_replan(
                db=db, session_id="sid", job_id="jid",
                root_node_key="T1",
                root_evidence="Use Rust instead of Python",
                divergence={"severity": "major", "reason": "lib pivot"},
                model_overrides=None,
            )

        assert captured["affected_keys"] == ["T2", "T3"]
        assert captured["root_evidence"] == "Use Rust instead of Python"
        assert captured["root_node_key"] == "T1"
        assert result["regenerated_count"] == 2
        assert result["regen_errors"] == []
        assert result["affected_nodes"] == ["T2", "T3"]
        assert result["scope"] == "selective"

    async def test_empty_subgraph_skips_regen(self):
        """No dependents → no_dependents return path; regen NOT called."""
        from app.modules import assist_replan

        async def fake_downstream(*, db, job_id, root_node_key):
            return []

        regen_mock = AsyncMock()
        db = AsyncMock()

        with patch.object(assist_replan, "downstream_node_keys", fake_downstream), \
             patch("app.modules.dag_generator.regenerate_subgraph", regen_mock):
            result = await assist_replan.apply_selective_replan(
                db=db, session_id="sid", job_id="jid",
                root_node_key="T1", root_evidence="x",
                divergence={"severity": "major"},
            )

        assert result["affected_nodes"] == []
        assert result.get("details") == "no_dependents"
        regen_mock.assert_not_called()
