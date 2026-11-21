"""§17.1988b — after a delivery the engine's tree holds the reviewed copy: a step that edits a delivered module starts
from it, a re-delivery leaves a file the review deleted deleted, and a delivery question asked again keeps the delivery.

Live (5b4880c0): the review of #1161 edited `paper_p_l_calculator` and `paper_entry_backtesting` by hand; the job's
records hold the pre-review copies, so a fix step shown the job's copy would draft on code the review had changed."""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.modules import engine_source as es

T0 = datetime(2026, 11, 7, 4, 34, tzinfo=timezone.utc)
_Q = "Is this job's code merged into the engine and running?"
PNL_JOB = "def avg(lots):\n    return sum(x for x, _ in lots)\n"
PNL_REVIEWED = "def avg(lots):\n    return sum((x for x, _ in lots), ZERO)\n"


def _row(key, files, at, status="done", title="Fix a module", **extra):
    body = "\n".join(f"```python\n# {p}\n{c}```" for p, c in files.items())
    return {"node_key": key, "status": status, "title": title, "prompt_template": "", "description": "",
            "output_text": body, "completed_at": at, **extra}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "lab_pnl.py").write_text(PNL_REVIEWED)
    monkeypatch.setattr(es, "ENGINE_PKG", tmp_path)
    return tmp_path


# -- (1) a step that edits a delivered module is shown the reviewed copy ------------------------------------------

def test_a_module_delivered_and_not_rewritten_since_is_shown_as_the_engines_tree_holds_it(tree):
    rows = [_row("T1F1", {"app/modules/lab_pnl.py": PNL_JOB}, T0 - timedelta(days=15)),
            _row("DELIVER", {}, T0, title=_Q)]
    block = es.edited_module_block(rows, "lab_pnl")
    assert "ZERO" in block and "T1F1, as delivered and reviewed into the engine" in block


def test_a_module_a_step_rewrote_after_the_delivery_is_shown_as_that_step_left_it(tree):
    rows = [_row("DELIVER", {}, T0, title=_Q),
            _row("F1", {"app/modules/lab_pnl.py": PNL_JOB}, T0 + timedelta(days=1))]
    block = es.edited_module_block(rows, "lab_pnl")
    assert "ZERO" not in block and "(step F1)" in block


def test_a_job_never_delivered_is_shown_its_own_copy(tree):
    rows = [_row("T1F1", {"app/modules/lab_pnl.py": PNL_JOB}, T0),
            _row("DELIVER", {}, None, status="pending", title=_Q)]
    assert "ZERO" not in es.edited_module_block(rows, "lab_pnl")


def test_an_earlier_jobs_delivered_module_is_its_reviewed_copy_too(tree):
    rows = [_row("f00ea97f:T5", {"app/modules/lab_pnl.py": PNL_JOB}, T0 - timedelta(days=3)),
            _row("f00ea97f:DELIVER", {}, T0, title=_Q)]
    assert "ZERO" in es.edited_module_block(rows, "lab_pnl")
    assert "ZERO" not in es.edited_module_block(rows[:1], "lab_pnl")          # that job was never delivered


def test_an_earlier_jobs_module_delivered_by_this_job_is_its_reviewed_copy(tree):
    """Live: `paper_entry_backtesting` is f00ea97f:T25E4's; f00ea97f asked no delivery question, 5b4880c0's export
    carried the file and its review edited it."""
    rows = [_row("f00ea97f:T25E4", {"app/modules/lab_pnl.py": PNL_JOB}, T0 - timedelta(days=20)),
            _row("DELIVER", {}, T0, title=_Q)]
    assert "ZERO" in es.edited_module_block(rows, "lab_pnl")


# -- (4) a delivery question asked again keeps the delivery ---------------------------------------------------------

def test_a_delivery_question_reset_to_pending_keeps_its_delivery_mark():
    rows = [_row("T1F1", {"app/modules/lab_pnl.py": PNL_JOB}, T0 - timedelta(days=15)),
            _row("DELIVER", {}, None, status="pending", title=_Q, delivered_at=T0),
            _row("F1", {"app/modules/lab_13f.py": "A = 1\n"}, T0 + timedelta(days=14))]
    at, since = es.since_delivery(rows)
    assert at == T0
    assert since["app/modules/lab_pnl.py"] == ("unchanged", "") and since["app/modules/lab_13f.py"] == ("new", "")


@pytest.mark.integration
async def test_code_rows_reads_the_mark_from_the_recorded_answer_and_the_resets_pre_image():
    from sqlalchemy import text

    from app.database import async_session
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ins = ("INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
           "execution_order, completed_at) VALUES (:j, :k, :t, :s, '', NULL, 'LLM', 1, :c)")
    try:
        async with async_session() as db:
            # a: answered on 11-07 (the decision record), then reset -- its node holds no finish any more
            await db.execute(text("INSERT INTO jobs (id, title, status, metadata) VALUES (:id, 'x', 'completed', "
                                  "CAST(:m AS jsonb))"),
                             {"id": a, "m": json.dumps({"decisions": {"DELIVER": {"at": T0.isoformat(),
                                                                                  "by": "operator"}}})})
            await db.execute(text(ins), {"j": a, "k": "DELIVER", "t": _Q, "s": "pending", "c": None})
            # b: no decision record (answered by another path); the reset wrote its finish down
            await db.execute(text("INSERT INTO jobs (id, title, status, metadata) VALUES (:id, 'x', 'completed', "
                                  "'{}'::jsonb)"), {"id": b})
            await db.execute(text(ins), {"j": b, "k": "DELIVER", "t": _Q, "s": "pending", "c": None})
            await db.execute(text(ins), {"j": b, "k": "T1", "t": "Write a module", "s": "done", "c": T0})
            await db.execute(text(
                "INSERT INTO dag_node_edits (job_id, node_key, op, before, after, edited_by) VALUES "
                "(:j, 'DELIVER', 'reset', CAST(:b AS jsonb), '{\"status\": \"pending\"}'::jsonb, 'node_editor:reset')"),
                {"j": b, "b": json.dumps({"status": "done", "completed_at": (T0 + timedelta(hours=2)).isoformat()})})
            await db.commit()
        async with async_session() as db:
            ra, rb = await es.code_rows(db, a), await es.code_rows(db, b)
        assert es.delivered_at(ra) == T0
        assert es.delivered_at(rb) == T0 + timedelta(hours=2)
        assert next(r for r in rb if r["node_key"] == "T1")["delivered_at"] is None        # only the question's
    finally:
        async with async_session() as db:
            for jid in (a, b):
                await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
                await db.execute(text("DELETE FROM dag_nodes WHERE job_id = :j"), {"j": jid})
                await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


# -- (2) a file the review deleted stays deleted -------------------------------------------------------------------

@pytest.fixture
def dj(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deliver_job_1988b",
                                                  Path(__file__).parent.parent / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    (tmp_path / "app" / "modules").mkdir(parents=True)
    (tmp_path / "app" / "labs.py").write_text("LABS: tuple[str, ...] = ()\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _f(path, content, since=None, base=None):
    return {"path": path, "step": "T1", "lines": content.count("\n"), "content": content,
            **({"since_delivery": since} if since else {}), **({"delivered_content": base} if base is not None else {})}


def test_a_file_the_review_deleted_is_not_written_again(dj, tmp_path, capsys):
    rc = dj.deliver({"files": [_f("app/modules/lab_old.py", "A = 1\n", "unchanged")], "tables": [], "secrets": [],
                     "wiring": [], "delivered_at": T0.isoformat()}, "lab_wire")
    assert rc == 0 and not (tmp_path / "app/modules/lab_old.py").exists()
    assert ("kept deleted app/modules/lab_old.py: deleted in the engine since delivery (reviewed), not re-written"
            in capsys.readouterr().out)


def test_a_deleted_file_a_step_changed_since_is_written_back_and_said_so_and_a_new_one_is_written(dj, tmp_path,
                                                                                                  capsys):
    dj.deliver({"files": [_f("app/modules/lab_old.py", "A = 2\n", "changed", base="A = 1\n"),
                          _f("app/modules/lab_new.py", "B = 1\n", "new")],
                "tables": [], "secrets": [], "wiring": [], "delivered_at": T0.isoformat()}, "lab_wire")
    assert (tmp_path / "app/modules/lab_old.py").read_text() == "A = 2\n"
    assert (tmp_path / "app/modules/lab_new.py").read_text() == "B = 1\n"
    out = capsys.readouterr().out
    assert "re-added app/modules/lab_old.py" in out and "wrote app/modules/lab_new.py" in out


def test_a_first_delivery_and_full_still_write_an_absent_file(dj, tmp_path):
    dj.deliver({"files": [_f("app/modules/lab_a.py", "A = 1\n")], "tables": [], "secrets": [], "wiring": [],
                "delivered_at": None}, "lab_wire")
    dj.deliver({"files": [_f("app/modules/lab_b.py", "B = 1\n", "unchanged")], "tables": [], "secrets": [],
                "wiring": [], "delivered_at": T0.isoformat()}, "lab_wire", full=True)
    assert (tmp_path / "app/modules/lab_a.py").exists() and (tmp_path / "app/modules/lab_b.py").exists()
