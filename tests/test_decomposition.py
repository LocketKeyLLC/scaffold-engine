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

    async def test_happy_path_reaches_execute_then_rolls_up(self, monkeypatch):
        db = AsyncMock()
        monkeypatch.setattr(dc, "async_session", lambda: _ACM(db))
        monkeypatch.setattr(dc, "get_ideation_slot_sem", lambda: _ACM())
        monkeypatch.setattr(dc, "analyze_and_confirm",
                            AsyncMock(return_value={"status": "awaiting_confirmation"}))
        monkeypatch.setattr(dc, "research_and_compile",
                            AsyncMock(return_value={"status": "planning"}))
        called = {"n": 0}

        async def fake_ean(cid, **k):
            called["n"] += 1
            if False:        # async generator that yields nothing
                yield
        monkeypatch.setattr(dc, "execute_all_nodes", fake_ean)
        roll = AsyncMock()
        monkeypatch.setattr(dc, "_rollup_umbrella", roll)
        await dc.run_component_pipeline(
            "c", "idea", domain="eng", research_queries=["q"],
            model_overrides=None, umbrella_id="u",
        )
        assert called["n"] == 1      # execute reached on the happy path
        roll.assert_awaited()


@pytest.mark.smoke
class TestUmbrellaCompile:
    """§17.533 — on completion the umbrella stitches its children's outputs into
    one deliverable; a failed umbrella does not."""

    def _counts(self, total, terminal, done, awaiting=0):
        r = MagicMock()
        r.mappings.return_value.first.return_value = {
            "total": total, "terminal": terminal, "done": done,
            # §17.624 — roll-up now also counts children parked in awaiting_assist.
            "awaiting": awaiting,
        }
        return r

    async def test_completed_assembles_deliverable(self):
        counts = self._counts(2, 2, 2)
        title = MagicMock()
        title.scalar_one_or_none.return_value = "KM System"
        kids = MagicMock()
        kids.mappings.return_value.all.return_value = [
            {"component_index": 0, "title": "Indexer", "status": "completed",
             "compiled_output": "INDEXER OUT"},
            {"component_index": 1, "title": "Dashboard", "status": "completed",
             "compiled_output": "DASH OUT"},
        ]
        upd = MagicMock()
        upd.first.return_value = object()      # won the finalize race
        # §17.701 — _rollup_umbrella first refreshes the children snapshot; a
        # metadata SELECT whose .scalar() is None short-circuits that refresh.
        rmeta = MagicMock(); rmeta.scalar.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[rmeta, counts, title, kids, upd])
        await dc._rollup_umbrella(db, "u")
        params = db.execute.call_args_list[4].args[1]   # the UPDATE
        assert params["s"] == "completed"
        co = params["co"]
        assert "# KM System" in co
        assert "Component 1: Indexer" in co and "INDEXER OUT" in co
        assert "Component 2: Dashboard" in co and "DASH OUT" in co
        db.commit.assert_awaited()

    async def test_failed_skips_compile(self):
        counts = self._counts(2, 2, 0)        # all terminal, none completed
        upd = MagicMock()
        upd.first.return_value = object()
        rmeta = MagicMock(); rmeta.scalar.return_value = None  # §17.701 refresh short-circuit
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[rmeta, counts, upd])
        await dc._rollup_umbrella(db, "u")
        assert db.execute.await_count == 3    # refresh + counts + UPDATE; no compile SELECTs
        params = db.execute.call_args_list[2].args[1]
        assert params["s"] == "failed" and params["co"] is None

    async def test_lost_finalize_race_does_not_commit(self):
        counts = self._counts(1, 1, 1)
        title = MagicMock()
        title.scalar_one_or_none.return_value = "X"
        kids = MagicMock()
        kids.mappings.return_value.all.return_value = []
        upd = MagicMock()
        upd.first.return_value = None         # another caller already finalized
        rmeta = MagicMock(); rmeta.scalar.return_value = None  # §17.701 refresh short-circuit
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[rmeta, counts, title, kids, upd])
        await dc._rollup_umbrella(db, "u")
        db.commit.assert_not_awaited()        # we lost the race -> no commit


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
        db = self._db_returning({"total": 3, "terminal": 3, "done": 1, "awaiting": 0})
        await dc._rollup_umbrella(db, "umb")
        update = db.execute.call_args_list[-1]
        assert "UPDATE jobs SET status" in str(update.args[0])
        assert update.args[1]["s"] == "completed"
        db.commit.assert_awaited()

    async def test_failed_when_all_terminal_and_none_done(self):
        db = self._db_returning({"total": 2, "terminal": 2, "done": 0, "awaiting": 0})
        await dc._rollup_umbrella(db, "umb")
        assert db.execute.call_args_list[-1].args[1]["s"] == "failed"

    async def test_awaiting_assist_when_any_child_parked(self):
        # §17.624 — a parked child makes the whole umbrella awaiting_assist,
        # even if others completed; the deliverable is still compiled.
        db = self._db_returning({"total": 3, "terminal": 3, "done": 2, "awaiting": 1})
        await dc._rollup_umbrella(db, "umb")
        assert db.execute.call_args_list[-1].args[1]["s"] == "awaiting_assist"
        db.commit.assert_awaited()

    async def test_repromotes_from_awaiting_assist_when_last_component_done(self):
        # §17.701 — once every component is done (none still awaiting), a parked
        # umbrella re-completes. The UPDATE guard must accept 'awaiting_assist'
        # (not just 'aggregating') and skip the no-op self-transition.
        db = self._db_returning({"total": 2, "terminal": 2, "done": 2, "awaiting": 0})
        await dc._rollup_umbrella(db, "umb")
        upd = db.execute.call_args_list[-1]
        sql = str(upd.args[0])
        assert "awaiting_assist" in sql        # guard re-promotes a parked umbrella
        assert "status <> :s" in sql           # skips awaiting_assist -> awaiting_assist churn
        assert upd.args[1]["s"] == "completed"
        db.commit.assert_awaited()

    async def test_noop_while_children_still_running(self):
        db = self._db_returning({"total": 3, "terminal": 2, "done": 1, "awaiting": 0})
        await dc._rollup_umbrella(db, "umb")
        # §17.701 — refresh metadata SELECT + counts SELECT; still no UPDATE.
        assert db.execute.call_count == 2
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
