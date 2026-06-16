"""Tests for app.modules.decomposition — triage-time task decomposition.

Covers the pure logic: component extraction/normalization, umbrella roll-up,
and umbrella+children creation with per-child spawn. The full child pipeline
(Phase 1 → Phase 2 → DAG → execute) is exercised by a live smoke, not here.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import decomposition as dc


def _resp(args: dict, success: bool = True):
    r = MagicMock()
    r.success = success
    r.text = json.dumps(args)
    r.error = None if success else "boom"
    return r


class _ACM:
    """Minimal async context manager for mocking async_session()/slot-sem."""
    def __init__(self, val=None):
        self.val = val
    async def __aenter__(self):
        return self.val
    async def __aexit__(self, *a):
        return False


@pytest.mark.smoke
class TestExtractComponents:
    async def test_normalizes_filters_and_caps(self, monkeypatch):
        payload = {"components": [
            {"label": "Auth service", "description": "Build auth",
             "domain": "eng", "research_queries": ["a", "b", "c", "d", "e"]},
            {"label": "", "description": "no label"},          # dropped — no label
            {"label": "Billing", "description": ""},            # dropped — no description
            {"label": "NLP parser", "description": "parse text", "domain": "bogus"},
            "not-a-dict",                                        # dropped — not a dict
        ]}
        monkeypatch.setattr(dc.model_router, "tool_call",
                            AsyncMock(return_value=_resp(payload)))
        monkeypatch.setattr(dc, "read_tool_args", lambda r: payload)

        out = await dc.extract_components("idea")
        assert [c["label"] for c in out] == ["Auth service", "NLP parser"]
        assert out[0]["research_queries"] == ["a", "b", "c", "d"]   # capped at 4
        assert out[1]["domain"] == "eng"                            # bogus -> default

    async def test_llm_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(dc.model_router, "tool_call",
                            AsyncMock(return_value=_resp({}, success=False)))
        assert await dc.extract_components("idea") == []

    async def test_parse_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(dc.model_router, "tool_call",
                            AsyncMock(return_value=_resp({})))
        monkeypatch.setattr(dc, "read_tool_args", lambda r: None)
        assert await dc.extract_components("idea") == []

    async def test_caps_at_max_components(self, monkeypatch):
        # §17.530 — over-split must be clamped so the fan-out stays bounded.
        payload = {"components": [
            {"label": f"C{i}", "description": f"build part {i}"} for i in range(9)
        ]}
        monkeypatch.setattr(dc.model_router, "tool_call",
                            AsyncMock(return_value=_resp(payload)))
        monkeypatch.setattr(dc, "read_tool_args", lambda r: payload)
        out = await dc.extract_components("idea")
        assert len(out) == dc.MAX_COMPONENTS

    async def test_caps_description_length(self, monkeypatch):
        # §17.531 — a padded/over-long description must be truncated.
        long_desc = "x" * (dc.MAX_COMPONENT_DESC_LEN + 500)
        payload = {"components": [
            {"label": "A", "description": long_desc},
            {"label": "B", "description": "short"},
        ]}
        monkeypatch.setattr(dc.model_router, "tool_call",
                            AsyncMock(return_value=_resp(payload)))
        monkeypatch.setattr(dc, "read_tool_args", lambda r: payload)
        out = await dc.extract_components("idea")
        assert len(out[0]["description"]) == dc.MAX_COMPONENT_DESC_LEN


@pytest.mark.smoke
class TestDecomposeEndpointGuards:
    """§17.531 — server-side kill switch + global fan-out cap on /decompose."""

    async def test_killswitch_short_circuits_no_llm(self, monkeypatch):
        from app.routers import workflow as wf
        from app.schemas import IdeaInput
        monkeypatch.setattr(wf.settings, "decompose_enabled", False)
        ec = AsyncMock()
        monkeypatch.setattr(wf, "extract_components", ec)
        out = await wf.decompose_endpoint(IdeaInput(idea="a multi-part build"), db=AsyncMock())
        assert out == {"decomposed": False, "reason": "disabled"}
        ec.assert_not_called()   # no LLM work when disabled

    async def test_fanout_cap_rejects_429(self, monkeypatch):
        from app.routers import workflow as wf
        from app.schemas import IdeaInput
        monkeypatch.setattr(wf.settings, "decompose_enabled", True)
        monkeypatch.setattr(wf.settings, "decompose_max_inflight_components", 20)
        monkeypatch.setattr(wf, "_require_valid_models", AsyncMock())
        monkeypatch.setattr(wf, "get_ideation_slot_sem", lambda: _ACM())
        monkeypatch.setattr(wf, "extract_components", AsyncMock(return_value=[
            {"label": "a", "description": "d"},
            {"label": "b", "description": "d"},
            {"label": "c", "description": "d"},
        ]))
        cr = AsyncMock()
        monkeypatch.setattr(wf, "create_and_run_decomposition", cr)
        # 19 in flight + 3 new = 22 > cap 20 -> reject
        res = MagicMock()
        res.scalar_one.return_value = 19
        db = AsyncMock()
        db.execute = AsyncMock(return_value=res)
        with pytest.raises(wf.HTTPException) as ei:
            await wf.decompose_endpoint(IdeaInput(idea="a multi-part build idea"), db=db)
        assert ei.value.status_code == 429
        cr.assert_not_called()   # nothing created when over cap


@pytest.mark.smoke
class TestResurrectionGuard:
    """§17.530 — a child whose Phase 1/2 returns a failure dict must NOT fall
    through to execute_all_nodes (whose guard would resurrect the failed job),
    but must still roll the umbrella up."""

    def _wire(self, monkeypatch, *, phase1, phase2=None):
        db = AsyncMock()
        monkeypatch.setattr(dc, "async_session", lambda: _ACM(db))
        monkeypatch.setattr(dc, "get_ideation_slot_sem", lambda: _ACM())
        monkeypatch.setattr(dc, "analyze_and_confirm", AsyncMock(return_value=phase1))
        rac = AsyncMock(return_value=phase2)
        monkeypatch.setattr(dc, "research_and_compile", rac)
        ean = MagicMock()  # must NOT be called on the failure paths
        monkeypatch.setattr(dc, "execute_all_nodes", ean)
        roll = AsyncMock()
        monkeypatch.setattr(dc, "_rollup_umbrella", roll)
        return rac, ean, roll

    async def test_phase1_failure_skips_execute_but_rolls_up(self, monkeypatch):
        rac, ean, roll = self._wire(monkeypatch, phase1={"status": "failed"})
        await dc.run_component_pipeline(
            "c0", "idea", domain="eng", research_queries=None,
            model_overrides=None, umbrella_id="u",
        )
        rac.assert_not_called()      # Phase 2 not reached
        ean.assert_not_called()      # execute_all_nodes NOT reached → no resurrection
        roll.assert_awaited()        # umbrella still rolled up

    async def test_phase2_conflict_skips_execute_but_rolls_up(self, monkeypatch):
        rac, ean, roll = self._wire(
            monkeypatch,
            phase1={"status": "awaiting_confirmation"},
            phase2={"status": "conflict"},
        )
        await dc.run_component_pipeline(
            "c1", "idea", domain="eng", research_queries=None,
            model_overrides=None, umbrella_id="u",
        )
        rac.assert_awaited()         # Phase 2 ran
        ean.assert_not_called()      # but execute_all_nodes was NOT reached
        roll.assert_awaited()


@pytest.mark.smoke
class TestRollupUmbrella:
    def _db_returning(self, row):
        mappings = MagicMock()
        mappings.first.return_value = row
        result = MagicMock()
        result.mappings.return_value = mappings
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    async def test_completed_when_all_terminal_and_one_done(self):
        db = self._db_returning({"total": 3, "terminal": 3, "done": 1})
        await dc._rollup_umbrella(db, "umb")
        update = db.execute.call_args_list[-1]
        assert "UPDATE jobs SET status" in str(update.args[0])
        assert update.args[1]["s"] == "completed"
        db.commit.assert_awaited()

    async def test_failed_when_all_terminal_and_none_done(self):
        db = self._db_returning({"total": 2, "terminal": 2, "done": 0})
        await dc._rollup_umbrella(db, "umb")
        assert db.execute.call_args_list[-1].args[1]["s"] == "failed"

    async def test_noop_while_children_still_running(self):
        db = self._db_returning({"total": 3, "terminal": 2, "done": 1})
        await dc._rollup_umbrella(db, "umb")
        assert db.execute.call_count == 1          # SELECT only; no UPDATE
        db.commit.assert_not_awaited()


@pytest.mark.smoke
class TestCreateAndRun:
    async def test_inserts_umbrella_children_and_spawns(self, monkeypatch):
        def result_with(val):
            r = MagicMock()
            r.scalar_one.return_value = val
            return r
        # umbrella insert, child0 insert, child1 insert, metadata update
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            result_with("umb"), result_with("c0"), result_with("c1"), MagicMock(),
        ])
        spawned = []
        monkeypatch.setattr(dc, "_spawn_component",
                            lambda *a, **k: spawned.append((a, k)))

        comps = [
            {"label": "A", "description": "desc-a", "domain": "eng",
             "research_queries": ["q1"]},
            {"label": "B", "description": "desc-b", "domain": "rag",
             "research_queries": []},
        ]
        out = await dc.create_and_run_decomposition("big idea", db, components=comps)

        assert out["umbrella_job_id"] == "umb"
        assert out["status"] == "aggregating"
        assert [c["job_id"] for c in out["children"]] == ["c0", "c1"]
        assert [c["component_index"] for c in out["children"]] == [0, 1]
        assert all(c["status"] == "refining" for c in out["children"])
        assert len(spawned) == 2
        # each spawn carries the component description + its umbrella id
        assert spawned[0][0] == ("c0", "desc-a")
        assert spawned[0][1]["umbrella_id"] == "umb"
        assert spawned[0][1]["research_queries"] == ["q1"]
        db.commit.assert_awaited()
