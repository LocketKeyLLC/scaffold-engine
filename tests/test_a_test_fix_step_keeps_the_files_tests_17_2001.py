"""§17.2001 — a step that fixes a module's test file keeps every test the file holds; a round of a step that had no
tool_config keeps its history.

Live (dashboard job 69fe61c4, 2026-11-22): REACH finished `tests/test_paper_wire_engine_entry_module.py` with 28 tests
(the /dashboard route, the static mount, the read-only session). Run with the job's other tests one failed, so
§17.1942 added T12L1 "Fix test_paper_wire_engine_entry_module (its tests fail with the job's)" -- "every test kept".
Its attempts wrote 16 tests, then §17.1923 opened a round (21:08:39 UTC): the round's edit took tool_config from null
to `{converging_rounds, converging_from}`, which the §17.1997 window read as a real change (JSON null vs `{}`), so the
round's attempts were held to nothing they had written. The round passed with 6 tests -- 27 of REACH's 28 gone, three of the 6 named for
`load_series` and testing `install` -- and the delivery took T12L1's file: none of the dashboard's tests shipped.
§17.1992's keep-list held only for a step that edits a MODULE in place; this step edits the test file.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.modules import engine_contracts, engine_source
from app.modules import execution_agent as xa
from app.modules import lab_together as lt

PATH = "tests/test_paper_wire_engine_entry_module.py"
MOD = "paper_wire_engine_entry_module"
#: REACH's tests, by name, as it finished them (dag_nodes.output_text, 69fe61c4 REACH)
REACH_TESTS = [
    "test_install_registers_daily_batch_job",
    "test_install_registers_routes",
    "test_install_no_scheduler_does_not_schedule",
    "test_install_starts_stream_consumer_task",
    "test_start_stream_consumer_outside_a_loop_returns_none",
    "test_install_does_not_read_secrets",
    "test_install_mounts_the_labs_static_bundle",
    "test_install_mounts_static_without_losing_the_lab_routes",
    "test_execution_route_passes_identity_timestamp_and_session",
    "test_execution_route_opens_its_own_session",
    "test_execution_route_uses_the_principal_from_the_engine_dependency",
    "test_router_has_dashboard_route",
    "test_existing_routes_unchanged",
    "test_dashboard_requires_auth",
    "test_dashboard_route_returns_200_html",
    "test_dashboard_renders_html_without_the_template_file",
    "test_dashboard_empty_data_returns_200",
    "test_dashboard_all_valid_spans_return_200",
    "test_invalid_span_raises_400",
    "test_dashboard_invalid_span_returns_400",
    "test_dashboard_route_invalid_span",
    "test_existing_view_route_preserved",
    "test_dashboard_reads_through_the_read_only_session",
    "test_load_series_reads_committed_pnl_rows_oldest_first",
    "test_valid_span_builds_context_and_renders",
    "test_missing_view_keys_default_to_empty",
    "test_dashboard_with_pnl_data_returns_200",
    "test_dashboard_passes_the_series_window_to_the_spy_benchmark",
]
#: T12L1's answer, as recorded done (dag_nodes.output_text, 69fe61c4 T12L1)
T12L1_OUT = '```python\n# tests/test_paper_wire_engine_entry_module.py\nfrom unittest.mock import MagicMock\n\nfrom app.modules.paper_wire_engine_entry_module import install\n\n\ndef _router_from(app):\n    """Return the router object install passed to include_router."""\n    return app.include_router.call_args[0][0]\n\n\ndef _prefix_from(app):\n    """Return the /labs/paper prefix install wired, wherever it lives."""\n    args = app.include_router.call_args.args\n    kwargs = app.include_router.call_args.kwargs\n    if "prefix" in kwargs:\n        return kwargs["prefix"]\n    if args and hasattr(args[0], "prefix"):\n        return args[0].prefix\n    return ""\n\n\ndef test_install_includes_router():\n    app = MagicMock()\n    result = install(app)\n    assert result is None\n    app.include_router.assert_called_once()\n    router = app.include_router.call_args[0][0]\n    assert router is not None\n\n\ndef test_install_does_not_mutate_existing_routes():\n    app = MagicMock()\n    install(app)\n    assert app.include_router.call_count == 1\n\n\ndef test_install_uses_paper_prefix():\n    app = MagicMock()\n    install(app)\n    prefix = _prefix_from(app)\n    assert prefix.startswith("/labs/paper")\n\n\ndef test_load_series_reads_committed_pnl_rows_oldest_first():\n    app = MagicMock()\n    result = install(app)\n    assert result is None\n    app.include_router.assert_called_once()\n    router = app.include_router.call_args[0][0]\n    assert router is not None\n\n\ndef test_load_series_returns_empty_for_unknown_series():\n    app = MagicMock()\n    install(app)\n    app.include_router.assert_called_once()\n\n\ndef test_load_series_ignores_uncommitted_rows():\n    app = MagicMock()\n    install(app)\n    assert _prefix_from(app).startswith("/labs/paper")\n```'
T12L1_TITLE = "Fix test_paper_wire_engine_entry_module (its tests fail with the job's)"
T12L1_PROMPT = ("Python test module `tests/test_paper_wire_engine_entry_module.py` -- step REACH's tests: edit it in "
                "place (the whole file, every test kept). Run with the job's other tests before delivery (1 failed, "
                "193 passed in 3.30s), these fail:\n- `test_load_series_reads_committed_pnl_rows_oldest_first`\n"
                "Each passes alone or did when its step ran. " + lt._TEST_FIX_NOTE)
T0 = datetime(2026, 11, 22, 20, 59, tzinfo=timezone.utc)


ASYNC = {"test_dashboard_reads_through_the_read_only_session", "test_load_series_reads_committed_pnl_rows_oldest_first"}


def _reach_file() -> str:
    body = "".join(f"\n\n{'async ' if n in ASYNC else ''}def {n}():\n    assert True\n" for n in REACH_TESTS)
    return f"```python\n# {PATH}\nfrom app.modules.{MOD} import install\n{body}```\n"


def _rows(t12l1_cfg=None):
    return [
        {"node_key": "REACH", "title": "Use T9, T10 from the lab's entry", "tool": "CodeGen", "status": "done",
         "completed_at": T0, "output_text": _reach_file(),
         "tool_config": {"fixes_module": MOD, "reaches": ["paper_serve_static_asset_under"]}},
        {"node_key": "T12L1", "title": T12L1_TITLE, "tool": "CodeGen", "status": "running", "completed_at": None,
         "prompt_template": T12L1_PROMPT, "output_text": None, "tool_config": t12l1_cfg},
    ]


def test_reach_wrote_28_tests_and_t12l1_kept_6():
    assert len(REACH_TESTS) == 28
    assert len(engine_contracts.written_tests(T12L1_OUT).get(PATH) or []) == 6


@pytest.mark.parametrize("cfg", [{"fixes_tests_of": MOD}, None], ids=["engine-marked", "by-its-title"])
def test_a_test_fix_step_inherits_the_files_tests(cfg):
    rows = _rows(cfg)
    mt = engine_source.module_tests(rows, rows[1])
    assert mt["module"] == MOD and mt["path"] == PATH and mt["key"] == "REACH" and mt.get("tests_only") is True
    assert mt["tests"] == REACH_TESTS and mt["retired"] == []
    block = engine_source.module_tests_block(mt)
    assert f"This step fixes `{PATH}` in place" in block and "test_dashboard_requires_auth" in block


def test_the_live_answer_is_refused_for_the_tests_it_dropped():
    rows = _rows({"fixes_tests_of": MOD})
    found = engine_contracts.dropped_module_tests(T12L1_OUT, engine_source.module_tests(rows, rows[1]))
    assert len(found) == 1
    assert f"dropped 27 of the 28 tests it already held for `{MOD}`, the file this step fixes in place" in found[0]
    for gone in ("test_dashboard_requires_auth", "test_install_mounts_static_without_losing_the_lab_routes",
                 "test_dashboard_reads_through_the_read_only_session"):
        assert f"`{gone}`" in found[0]


def test_an_answer_that_keeps_every_test_is_not_refused():
    rows = _rows({"fixes_tests_of": MOD})
    assert engine_contracts.dropped_module_tests(_reach_file(), engine_source.module_tests(rows, rows[1])) == []


def test_the_kept_tests_are_put_back_when_an_answer_drops_them():
    rows = _rows({"fixes_tests_of": MOD})
    mt = engine_source.module_tests(rows, rows[1])
    keep = engine_source.with_module_tests({}, {}, mt)
    assert sorted(mt["inherited"]) == sorted(REACH_TESTS)
    spliced = engine_source.inherited_kept(T12L1_OUT, keep, mt)[PATH]
    assert "test_dashboard_requires_auth" in spliced and "test_install_uses_paper_prefix" not in spliced


@pytest.mark.parametrize("title", ["Write tests/test_paper_wire_engine_entry_module.py",
                                   "Fix test_paper_no_such_module (its tests fail with the job's)"])
def test_a_step_that_fixes_no_known_test_file_inherits_nothing(title):
    rows = _rows()
    rows[1]["title"] = title
    rows[1]["prompt_template"] = "Write the tests."
    assert engine_source.fixed_tests_of(rows[1], {"REACH"}) == ""
    assert engine_source.module_tests(rows, rows[1]) == {}


def test_a_module_fix_step_is_unchanged():
    rows = _rows({"fixes_module": MOD})
    mt = engine_source.module_tests(rows, rows[1])
    assert mt["module"] == MOD and "tests_only" not in mt
    assert f"This step edits `{MOD}` in place" in engine_source.module_tests_block(mt)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        rows = self.rows

        class _R:
            def all(self):
                return rows
        return _R()


@pytest.mark.asyncio
async def test_the_engines_test_fix_step_is_marked_with_the_module_whose_tests_it_fixes(monkeypatch):
    insert = AsyncMock(return_value="T12L1")
    monkeypatch.setattr(lt, "_insert_fix_step", insert)
    monkeypatch.setattr(lt, "async_session", lambda: _Session([("REACH", "Use T9, T10 from the lab's entry")]))
    got = await lt.failing_test_fix_steps(
        "j", "T12", [("REACH", PATH, ["test_load_series_reads_committed_pnl_rows_oldest_first"])], "1 failed")
    assert got == ["T12L1"]
    assert insert.await_args.args[3] == T12L1_TITLE
    assert insert.await_args.kwargs["tool_config"]["fixes_tests_of"] == MOD


@pytest.mark.asyncio
async def test_a_round_of_a_step_with_no_tool_config_keeps_its_history():
    """The live edits: T12L1 had no tool_config; the round's edit set only round bookkeeping. A real edit still cuts."""
    from app.database import async_session
    text = xa.text
    jid = str(uuid.uuid4())
    log = ("INSERT INTO execution_logs (job_id, node_id, log_level, message, details, created_at) VALUES "
           "(:j, :n, 'warning', 'Node x -> failed', CAST(:d AS jsonb), now() + make_interval(secs => :t))")
    edit = ("INSERT INTO dag_node_edits (job_id, node_key, op, before, after, edited_by, created_at) VALUES "
            "(:j, :k, :op, CAST(:b AS jsonb), CAST(:a AS jsonb), :by, now() + make_interval(secs => :t))")
    round1 = {"converging_from": ["19 failed / 0 passed", "19 failed / 0 passed", "4 failed / 15 passed",
                                  "1 failed / 18 passed"], "converging_rounds": 1}
    tests = {PATH: REACH_TESTS[:16]}
    reason = "§17.1765 engine-test sandbox: its tests failed\n1 failed, 15 passed in 2.10s"
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'round memory', 'running')"),
                             {"id": jid})
            ids = {}
            for k in ("T12L1", "X1", "X2"):
                ids[k] = str((await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                    "VALUES (:j, :k, 'x', 'pending', 0) RETURNING id"), {"j": jid, "k": k})).scalar())
                await db.execute(text(log), {"j": jid, "n": ids[k], "t": 1,
                                             "d": json.dumps({"reason": reason, "tests": tests})})
            for k, before, after, by in (
                    # the live pair: null (as dag_node_edits recorded it) to the round's bookkeeping
                    ("T12L1", {"tool_config": None}, {"tool_config": round1}, "engine §17.1923"),
                    # a key absent before counts the same as null
                    ("X1", {}, {"tool_config": round1}, "engine §17.1923"),
                    # a real config set on a step that had none still cuts
                    ("X2", {"tool_config": None}, {"tool_config": {"fixes_module": MOD, **round1}}, "admin")):
                await db.execute(text(edit), {"j": jid, "k": k, "op": "edit", "b": json.dumps(before),
                                              "a": json.dumps(after), "by": by, "t": 2})
                await db.execute(text(edit), {"j": jid, "k": k, "op": "reset", "b": json.dumps({"status": "failed"}),
                                              "a": json.dumps({"status": "pending"}),
                                              "by": "node_editor:reset by engine §17.1923", "t": 2})
            await db.commit()
        async with async_session() as db:
            for k in ("T12L1", "X1"):
                assert await xa.prior_tests(db, jid, ids[k]) == tests, k
                assert await xa.prior_refusals(db, jid, ids[k]) == [reason], k
            assert await xa.prior_tests(db, jid, ids["X2"]) == {}
            assert await xa.prior_refusals(db, jid, ids["X2"]) == []
    finally:
        async with async_session() as db:
            for tbl in ("execution_logs", "dag_node_edits", "dag_nodes"):
                await db.execute(text(f"DELETE FROM {tbl} WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
