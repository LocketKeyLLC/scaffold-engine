"""§17.1811 — a follow-up job's code is written against the modules its earlier job finished.

Live (lab f00ea97f -> its follow-up, 2026-10-24): 23 trading modules exist only as the finished steps of f00ea97f.
Every reader of "this job's code" -- the engine block in the prompt (§17.1749/1796), the name checks
(`engine_findings`), the module list (`job_code`) and the engine-test sandbox (§17.1765) -- read `WHERE job_id = :j`,
so a new job fixing the order executor would see none of them: it would rewrite them or guess at their names.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.modules import engine_source as es
from app.sandbox import engine_tests as et

PRIOR = "f00ea97f-0000-4000-8000-000000000000"
CLIENT = {"node_key": "T5", "title": "Implement Alpaca paper client", "description": "", "status": "done",
          "prompt_template": "Python module `paper_alpaca_paper_client.py`",
          "output_text": "```python\n# app/modules/paper_alpaca_paper_client.py\nasync def get_bars(symbol):\n    ...\n```"}
EXECUTOR = {"node_key": "T17", "title": "Implement order executor", "description": "", "status": "done",
            "prompt_template": "Python module `paper_order_executor.py`",
            "output_text": "```python\n# app/modules/paper_order_executor.py\nOLD = 'ticker'\n```"}


def _db(rows):
    """A session answering `code_rows`' one read with rows as the query returns them."""
    r = MagicMock()
    r.mappings.return_value.all.return_value = [{"src": 0, "ord": 1, **x} for x in rows]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=r)
    return db


CARRIED = [{**CLIENT, "node_key": "f00ea97f:T5"}, {**EXECUTOR, "node_key": "f00ea97f:T17"}]


@pytest.mark.integration
async def test_the_earlier_jobs_finished_steps_come_first_under_their_own_keys():
    from sqlalchemy import text

    from app.database import async_session
    prior, other, job = (str(uuid.uuid4()) for _ in range(3))
    ins = ("INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
           "execution_order) VALUES (:j, :k, :t, :s, '', :o, 'CodeGen', :n)")
    try:
        async with async_session() as db:
            for jid, meta in ((prior, {}), (other, {}), (job, {"builds_on": [prior]})):
                await db.execute(text("INSERT INTO jobs (id, title, status, metadata) VALUES (:id, 'x', 'completed', "
                                      "CAST(:m AS jsonb))"), {"id": jid, "m": json.dumps(meta)})
            for k, s_, o, n in (("T17", "done", "executor", 2), ("T5", "done", "client", 1),
                                ("T30", "skipped", "never", 3), ("T31", "done", None, 4)):
                await db.execute(text(ins), {"j": prior, "k": k, "t": k, "s": s_, "o": o, "n": n})
            await db.execute(text(ins), {"j": other, "k": "T9", "t": "T9", "s": "done", "o": "other job", "n": 1})
            await db.execute(text(ins), {"j": job, "k": "T1", "t": "T1", "s": "pending", "o": None, "n": 1})
            await db.commit()
        async with async_session() as db:
            rows = await es.code_rows(db, job)
            alone = await es.code_rows(db, prior)
        p8 = prior[:8]
        assert [r["node_key"] for r in rows] == [f"{p8}:T5", f"{p8}:T17", "T1"]     # done, written, in plan order
        assert rows[0]["output_text"] == "client" and set(rows[0]) == {
            "node_key", "title", "description", "prompt_template", "output_text", "status"}
        assert sorted(r["node_key"] for r in alone) == ["T17", "T30", "T31", "T5"]   # no link: only itself, as before
    finally:
        async with async_session() as db:
            for jid in (prior, other, job):
                await db.execute(text("DELETE FROM dag_nodes WHERE job_id = :j"), {"j": jid})
                await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.asyncio
async def test_the_module_list_and_the_name_checks_see_the_earlier_jobs_modules():
    own = {"node_key": "T1", "title": "Fix the order executor", "description": "", "status": "pending",
           "prompt_template": "Python module `paper_order_executor.py`", "output_text": None}
    mods, names = await es.job_code(_db([CARRIED[0], own]), "job-2", except_node="T1")
    assert "paper_alpaca_paper_client" in mods and "get_bars" in names


@pytest.mark.asyncio
async def test_in_the_sandbox_this_jobs_file_replaces_the_earlier_jobs_copy():
    rows = await es.code_rows(_db(CARRIED), "job-2")
    me = {"node_key": "T1", "prompt_template": "Python module `paper_order_executor.py`"}
    mine = "```python\n# app/modules/paper_order_executor.py\nNEW = 'symbol'\n```"
    run = AsyncMock(return_value=SimpleNamespace(ok=True, error=None, database="ok", exit_code=0, stdout="", stderr="",
                                                 timed_out=False))
    with patch.object(et.settings, "engine_test_check_enabled", True), \
         patch.object(et.settings, "engine_test_url", "http://sandbox:8020"), \
         patch.object(et, "_sandbox_engine", new=AsyncMock(return_value="d")), \
         patch.object(et, "_local_digest", return_value="d"), \
         patch.object(et, "run_code", new=run):
        res = await et.check(mine, me, rows)
    files = run.await_args.args[0]
    assert res.verdict == "pass"
    assert "get_bars" in files["app/modules/paper_alpaca_paper_client.py"]        # the earlier job's module is there
    assert "NEW = 'symbol'" in files["app/modules/paper_order_executor.py"]       # and this job's fix replaces its own


# ── the production path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_executor_hands_the_sandbox_the_earlier_jobs_code():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    carried = [{**CLIENT, "node_key": "f00ea97f:T5"}]
    spy = AsyncMock(return_value=SimpleNamespace(verdict="pass", reason="ok", ran=[]))
    with patch("app.sandbox.engine_tests.check", new=spy), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=carried)), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    spy.assert_awaited()
    assert spy.await_args.args[2] == carried, captured.get("raised")


@pytest.mark.asyncio
async def test_the_executor_shows_the_prompt_the_earlier_jobs_modules():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    carried = [{**CLIENT, "node_key": "f00ea97f:T5"}]
    with patch("app.sandbox.engine_tests.check", new=AsyncMock(return_value=SimpleNamespace(
                verdict="pass", reason="ok", ran=[]))), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=carried)), \
         patch("app.modules.engine_source.job_code", new=AsyncMock(return_value=(set(), set()))), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "get_bars" in captured["calls"][0][-1]["content"], captured.get("raised")


# ── the endpoint ─────────────────────────────────────────────────────────────

def _endpoint_db(status):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=status)))
    db.commit = AsyncMock()
    return db


async def _start(db, builds_on):
    from app.routers import workflow as wf
    from app.schemas import IdeaInput
    body = IdeaInput(idea="Finish the lab", builds_on=builds_on)
    with patch.object(wf, "_require_valid_models", new=AsyncMock()), \
         patch.object(wf, "create_ideation_job", new=AsyncMock(return_value="job-2")), \
         patch.object(wf, "spawn_phase1_background") as spawn:
        out = await wf.ideate_start_endpoint(body, db=db, principal=SimpleNamespace(identity="op"))
    return out, spawn


@pytest.mark.asyncio
async def test_a_job_is_started_on_a_finished_job_and_keeps_the_link():
    db = _endpoint_db("completed")
    out, spawn = await _start(db, [PRIOR])
    assert out["job_id"] == "job-2" and spawn.called
    writes = [c for c in db.execute.await_args_list if "builds_on" in str(c.args[0])]
    assert writes and json.loads(writes[0].args[1]["b"]) == [PRIOR] and writes[0].args[1]["j"] == "job-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed", None])
async def test_a_job_cannot_build_on_one_that_did_not_finish(status):
    with pytest.raises(HTTPException) as e:
        await _start(_endpoint_db(status), [str(uuid.uuid4())])
    assert e.value.status_code == 422 and "not completed" in e.value.detail


@pytest.mark.asyncio
async def test_without_builds_on_nothing_extra_is_written():
    db = _endpoint_db("completed")
    await _start(db, [])
    assert not [c for c in db.execute.await_args_list if "builds_on" in str(c.args[0])]


@pytest.mark.asyncio
async def test_the_name_checks_accept_a_call_to_the_earlier_jobs_module_and_refuse_a_guess():
    own = {"node_key": "T1", "title": "Fix the order executor", "description": "", "status": "pending",
           "prompt_template": "Python module `paper_order_executor.py`", "output_text": None}
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [{"src": 0, "ord": 1, **CARRIED[0]}, own]
    cols = MagicMock(); cols.all.return_value = []

    async def findings(out):
        db = AsyncMock(); db.execute = AsyncMock(side_effect=[nodes, cols])
        return "\n".join(await es.engine_findings(db, "job-2", "T1", out))
    head = "```python\n# app/modules/paper_order_executor.py\nfrom app.modules.paper_alpaca_paper_client import "
    assert "get_bars" not in await findings(head + "get_bars\n```")
    assert "get_price_history" in await findings(head + "get_price_history\n```")


# ── §17.1812 — a follow-up step is shown the earlier job's code, and may fix it ─────────────────────────────────

SPY = {"node_key": "f00ea97f:T21", "title": "Implement SPY benchmark tracker", "description": "", "status": "done",
       "prompt_template": "", "output_text": "```python\n# app/modules/paper_spy_benchmark_tracker.py\n"
       "URL = 'https://api.example.com/spy'\n```\n```python\n# tests/test_paper_spy_benchmark_tracker.py\n"
       "def test_url():\n    assert True\n```"}
MIGRATION = {"node_key": "f00ea97f:T3", "title": "Implement audit trail schema", "description": "", "status": "done",
             "prompt_template": "", "output_text": "```python\n# alembic/versions/0010_paper_audit_trail.py\n"
             "revision = '0010'\n```"}
LAB = [{**CLIENT, "node_key": "f00ea97f:T5"}, {**EXECUTOR, "node_key": "f00ea97f:T17"}, SPY, MIGRATION]


def _step(title, prompt=""):
    return {"node_key": "T2", "title": title, "description": "", "prompt_template": prompt}


def test_every_file_is_listed_with_whether_it_has_tests():
    out = es.carried_block(LAB, _step("Audit existing paper modules"))
    assert "app/modules/paper_order_executor.py (step f00ea97f:T17) -- NO test file" in out
    assert "app/modules/paper_spy_benchmark_tracker.py (step f00ea97f:T21) -- tests: " \
           "tests/test_paper_spy_benchmark_tracker.py" in out
    assert "alembic/versions/0010_paper_audit_trail.py (step f00ea97f:T3)\n" in out        # a migration has no tests
    assert "Every file (4 modules)" in out


def test_a_step_that_fixes_a_module_gets_its_current_code_first():
    out = es.carried_block(LAB, _step("Fix ticker field consistency", "Edit paper_order_executor in place."))
    assert "### `app/modules/paper_order_executor.py` (this step names it)\n```python\n" in out
    assert "OLD = 'ticker'" in out
    assert out.index("paper_order_executor.py` (this") < out.index("### `app/modules/paper_alpaca")
    assert "edits it in place: the whole file under the same path" in out


def test_a_module_is_named_by_its_words_and_brings_its_tests():
    out = es.carried_block(LAB, _step("Fix SPY benchmark tracker", "Replace the api.example.com URL."))
    assert "### `app/modules/paper_spy_benchmark_tracker.py` (this step names it)" in out
    assert "### `tests/test_paper_spy_benchmark_tracker.py` (this step names it)" in out
    assert "### `app/modules/paper_order_executor.py` (this" not in out            # one word ("tracker") is not a name
    mig = es.carried_block(LAB, _step("Audit", "Read the audit-trail migration."))
    assert "### `alembic/versions/0010_paper_audit_trail.py` (this step names it)" in mig   # the revision number aside


def test_over_the_limit_a_file_is_named_not_cut():
    step = _step("Fix SPY benchmark tracker")
    whole = es.carried_block(LAB, step, budget=100000)
    last = whole.rsplit("### `", 1)[1].split("`")[0]                     # the file the full block ends on
    out = es.carried_block(LAB, step, budget=len(whole) - 1)
    assert "paper_spy_benchmark_tracker.py` (this step names it)" in out
    assert f"### `{last}`" not in out and f"Not shown here (over the size limit): `{last}`" in out


def test_the_rest_comes_by_the_words_few_files_hold():
    def mod(key, name, body):
        return {"node_key": f"f00ea97f:{key}", "title": key, "description": "", "status": "done", "prompt_template": "",
                "output_text": f"```python\n# app/modules/{name}.py\n{body}\n```"}
    big = mod("T9", "paper_congress", "# every proposal and every order\n" + "X = 1\n" * 400)
    lab = [big, mod("T1", "paper_a", "# proposal order"), mod("T2", "paper_b", "# proposal order"), SPY]
    out = es.carried_block(lab, _step("Audit", "Find each proposal order and the made-up example URL."),
                           budget=100000)
    # two words every file holds lose to the one word only the SPY tracker holds; the biggest file comes last
    assert out.index("### `app/modules/paper_spy_benchmark") < out.index("### `app/modules/paper_congress")


def test_no_files_no_block():
    assert es.carried_block([{**CLIENT, "node_key": "f00ea97f:T1", "output_text": "a decision"}], _step("x")) == ""


def test_a_followup_step_may_rewrite_the_earlier_jobs_module_but_not_a_siblings():
    me = {"node_key": "T2", "title": "Fix ticker field consistency", "prompt_template": "Edit paper_order_executor."}
    fix = "```python\n# app/modules/paper_order_executor.py\nNEW = 'symbol'\n```"
    assert es.writes_a_siblings_module(fix, me, [{**EXECUTOR, "node_key": "f00ea97f:T17"}]) == []
    own_sibling = {**EXECUTOR, "node_key": "T7"}
    assert "step T7's" in es.writes_a_siblings_module(fix, me, [own_sibling])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["LLM", "CodeGen"])
async def test_the_executor_shows_a_followup_step_the_earlier_jobs_code(tool):
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.sandbox.engine_tests.check", new=AsyncMock(return_value=SimpleNamespace(
                verdict="pass", reason="ok", ran=[]))), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=LAB)), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF, node_extra={
            "tool": tool, "title": "Audit existing paper modules",
            "prompt_template": "Read the paper_order_executor and report every field name."})
    prompt = captured["calls"][0][-1]["content"]
    assert "## The code this job builds on (job f00ea97f" in prompt, captured.get("raised")
    assert "OLD = 'ticker'" in prompt


@pytest.mark.asyncio
async def test_a_job_that_builds_on_nothing_gets_no_block():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.sandbox.engine_tests.check", new=AsyncMock(return_value=SimpleNamespace(
                verdict="pass", reason="ok", ran=[]))), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=[{**CLIENT, "node_key": "T5"}])), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF, node_extra={"tool": "LLM"})
    assert "The code this job builds on" not in captured["calls"][0][-1]["content"], captured.get("raised")


# ── §17.1816 — a module the output rewrites is checked against its new definitions ──────────────────────────

OLD_EXECUTOR = {"node_key": "f00ea97f:T17", "title": "Implement order executor", "description": "", "status": "done",
                "prompt_template": "Python module `paper_order_executor.py`",
                "output_text": "```python\n# app/modules/paper_order_executor.py\nasync def execute_order(approval):\n"
                               "    return approval\n```"}
REWRITE = ("```python\n# app/modules/paper_order_executor.py\nALPACA_PAPER_BASE_URL = 'https://paper-api.alpaca.markets'\n"
           "async def execute_order(*, db, client, approval):\n    return approval\n```\n"
           "```python\n# tests/test_paper_order_executor.py\n"
           "from app.modules.paper_order_executor import ALPACA_PAPER_BASE_URL, execute_order\n"
           "async def test_it():\n    await execute_order(db=1, client=2, approval=3)\n{call}\n```")


async def _findings_for(output, rows):
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [{"src": 0, "ord": 1, **r} for r in rows]
    cols = MagicMock(); cols.all.return_value = []
    db = AsyncMock(); db.execute = AsyncMock(side_effect=[nodes, cols])
    return "\n".join(await es.engine_findings(db, "job-2", "T2", output))


@pytest.mark.asyncio
async def test_a_rewritten_module_is_checked_against_what_the_output_now_defines():
    me = {"node_key": "T2", "title": "Fix ticker field consistency", "description": "", "status": "pending",
          "prompt_template": "Python module `paper_order_executor.py` -- the earlier job's module: edit it in place",
          "output_text": None}
    ok = await _findings_for(REWRITE.format(call=""), [OLD_EXECUTOR, me])
    assert "has no parameter" not in ok and "ALPACA_PAPER_BASE_URL" not in ok, ok
    bad = await _findings_for(REWRITE.format(call="    await execute_order(approval=3, size=9)"), [OLD_EXECUTOR, me])
    assert "has no parameter `size`" in bad and "(it takes *, db, client, approval)" in bad, bad     # the NEW one


@pytest.mark.asyncio
async def test_a_step_that_does_not_rewrite_the_module_is_still_held_to_it():
    me = {"node_key": "T2", "title": "Build execution", "description": "", "status": "pending",
          "prompt_template": "Python module `paper_execution_entry_point.py`", "output_text": None}
    out = ("```python\n# app/modules/paper_execution_entry_point.py\n"
           "from app.modules.paper_order_executor import execute_order\n"
           "async def run():\n    await execute_order(db=1, client=2, approval=3)\n```")
    found = await _findings_for(out, [OLD_EXECUTOR, me])
    assert "has no parameter `db`, `client`" in found and "(it takes approval)" in found, found


# ── §17.1822 — each file is its own module; an earlier module is rewritten only by the step that names it ─────

TWO_FILES = {"node_key": "T6D", "title": "Build daily signal-to-proposal batch -- data", "description": "",
             "status": "done", "prompt_template": "Python module `paper_batch_data.py`: the DATA part of this task.",
             "output_text": "```python\n# app/modules/paper_batch_data.py\nasync def get_batch_proposals(db):\n"
                            "    return []\n```\n```python\n# app/modules/paper_audit_trail_writer.py\n"
                            "async def write_approval(*, proposal_id):\n    return 1\n```"}


def test_each_file_is_credited_with_its_own_names():
    defs = es.sibling_definitions([TWO_FILES])
    assert "get_batch_proposals" in defs["paper_batch_data"]["defined"]
    assert "write_approval" not in defs["paper_batch_data"]["names"]              # live: credited here
    assert defs["paper_audit_trail_writer"]["node"] == "T6D" and "write_approval" in defs["paper_audit_trail_writer"]["funcs"]


def test_a_step_that_does_not_name_an_earlier_module_may_not_rewrite_it():
    writer = {"node_key": "f00ea97f:T4", "title": "Implement audit trail writer", "description": "", "status": "done",
              "prompt_template": "Python module `paper_audit_trail_writer.py`",
              "output_text": "```python\n# app/modules/paper_audit_trail_writer.py\nX = 1\n```"}
    me = {k: TWO_FILES[k] for k in ("node_key", "title", "description", "prompt_template")}
    found = es.writes_a_siblings_module(TWO_FILES["output_text"], me, [writer])
    assert found and "paper_audit_trail_writer.py`: that module is step f00ea97f:T4's" in found[0]
    fixer = {**me, "prompt_template": "Fix paper_audit_trail_writer: write_approval takes proposal_id."}
    assert es.writes_a_siblings_module(TWO_FILES["output_text"], fixer, [writer]) == []
