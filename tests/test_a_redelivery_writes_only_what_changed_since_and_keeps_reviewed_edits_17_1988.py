"""§17.1988 — a re-delivery writes only what the job's steps changed since its last delivery; a reviewed edit is kept.

Live (5b4880c0, 2026-11-21): re-delivering the paper lab to ship two new 13F fix steps (F13F1/F13F2) also re-wrote
`paper_entry_backtesting` (a dead `forward_days` back) and `paper_p_l_calculator` (the `ZERO` start of two Decimal
sums dropped) -- both edited by hand in the review of the first delivery (#1161), which the job's records never learn.
The export now says what each file did since the delivery question was answered, and the script keeps the rest."""
from __future__ import annotations

import importlib.util
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.modules import engine_source as es

T0 = datetime(2026, 11, 7, 4, 34, tzinfo=timezone.utc)          # the delivery question answered
_Q = "Is this job's code merged into the engine and running?"

BACKTEST_JOB = "def run(*, lookback_days=60,\n        forward_days=20):\n    return lookback_days\n"
BACKTEST_REVIEWED = "def run(*, lookback_days=60):\n    return lookback_days\n"
PNL_JOB = "def avg(lots):\n    q = sum(x for x, _ in lots)\n    return q\n"
PNL_REVIEWED = "def avg(lots):\n    q = sum((x for x, _ in lots), ZERO)\n    return q\n"
F13_OLD = "def holdings(doc):\n    return parse(doc.primary)\n"
F13_NEW = "def holdings(doc):\n    return parse(info_table(doc.index))\n"


def _row(key, files: dict[str, str], at, status="done", title="Fix a module"):
    body = "\n".join(f"```python\n# {p}\n{c}```" for p, c in files.items())
    return {"node_key": key, "status": status, "title": title, "prompt_template": "", "description": "",
            "output_text": body, "completed_at": at}


def _live_rows(delivered=True):
    rows = [
        _row("f00ea97f:T25E4", {"app/modules/lab_backtest.py": BACKTEST_JOB}, T0 - timedelta(days=20)),
        _row("T1F1", {"app/modules/lab_pnl.py": PNL_JOB}, T0 - timedelta(days=15)),
        _row("API_SEC_EDGAR_AUDITF1", {"app/modules/lab_13f.py": F13_OLD}, T0 - timedelta(days=2)),
        _row("DELIVER", {}, T0 if delivered else None, status="done" if delivered else "pending", title=_Q),
        _row("F13F1", {"app/modules/lab_13f.py": F13_NEW, "tests/test_lab_13f.py": "def test_x(): pass\n"},
             T0 + timedelta(days=14)),
    ]
    return rows


def test_the_live_shape_two_files_unchanged_since_the_delivery_and_the_13f_module_changed():
    at, since = es.since_delivery(_live_rows())
    assert at == T0
    assert since["app/modules/lab_backtest.py"] == ("unchanged", "")
    assert since["app/modules/lab_pnl.py"] == ("unchanged", "")
    assert since["app/modules/lab_13f.py"] == ("changed", "# app/modules/lab_13f.py\n" + F13_OLD)
    assert since["tests/test_lab_13f.py"] == ("new", "")


def test_a_job_never_delivered_has_no_delivery_and_another_jobs_question_is_not_its_own():
    assert es.since_delivery(_live_rows(delivered=False)) == (None, {})
    rows = _live_rows(delivered=False) + [_row("f00ea97f:DELIVER", {}, T0, title=_Q)]
    assert es.since_delivery(rows) == (None, {})


def test_a_step_reset_since_the_delivery_is_not_part_of_what_was_delivered():
    rows = _live_rows()
    rows[1] = {**rows[1], "completed_at": T0 + timedelta(days=1)}          # T1F1 re-ran after the delivery
    _at, since = es.since_delivery(rows)
    assert since["app/modules/lab_pnl.py"] == ("new", "")


# -- the script ---------------------------------------------------------------------------------------------------

@pytest.fixture
def dj(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deliver_job_1988",
                                                  Path(__file__).parent.parent / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    (tmp_path / "app" / "modules").mkdir(parents=True)
    (tmp_path / "app" / "labs.py").write_text("LABS: tuple[str, ...] = ()\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _f(path, content, since=None, base=None, step="T1"):
    return {"path": path, "step": step, "lines": content.count("\n"), "content": content,
            **({"since_delivery": since} if since else {}), **({"delivered_content": base} if base is not None else {})}


def _export(files, delivered=True):
    return {"files": files, "tables": [], "secrets": [], "wiring": ["lab_wire"],
            "delivered_at": T0.isoformat() if delivered else None}


def _checkout(root: Path, files: dict[str, str]):
    for p, c in files.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(c)


def test_a_redelivery_keeps_the_reviewed_files_and_writes_the_13f_module(dj, tmp_path, capsys):
    _checkout(tmp_path, {"app/modules/lab_backtest.py": BACKTEST_REVIEWED, "app/modules/lab_pnl.py": PNL_REVIEWED,
                         "app/modules/lab_13f.py": F13_OLD})
    rc = dj.deliver(_export([
        _f("app/modules/lab_backtest.py", BACKTEST_JOB, "unchanged", step="f00ea97f:T25E4"),
        _f("app/modules/lab_pnl.py", PNL_JOB, "unchanged", step="T1F1"),
        _f("app/modules/lab_13f.py", F13_NEW, "changed", base=F13_OLD, step="F13F1"),
        _f("app/modules/lab_wire.py", "def install(app):\n    pass\n", "unchanged", step="T9"),
    ]), "lab_wire")
    assert rc == 0
    assert (tmp_path / "app/modules/lab_backtest.py").read_text() == BACKTEST_REVIEWED
    assert (tmp_path / "app/modules/lab_pnl.py").read_text() == PNL_REVIEWED
    assert (tmp_path / "app/modules/lab_13f.py").read_text() == F13_NEW
    out = capsys.readouterr().out
    assert ("kept app/modules/lab_backtest.py: changed in the engine since delivery (reviewed edit), not rewritten"
            in out)
    assert "kept app/modules/lab_pnl.py" in out and "wrote app/modules/lab_13f.py" in out
    assert "kept deleted app/modules/lab_wire.py" in out       # §17.1988b: absent here, unchanged: the review deleted it


def test_a_first_delivery_writes_everything(dj, tmp_path):
    _checkout(tmp_path, {"app/modules/lab_pnl.py": PNL_REVIEWED})
    assert dj.deliver(_export([_f("app/modules/lab_pnl.py", PNL_JOB), _f("app/modules/lab_13f.py", F13_NEW)],
                              delivered=False), "lab_wire") == 0
    assert (tmp_path / "app/modules/lab_pnl.py").read_text() == PNL_JOB
    assert (tmp_path / "app/modules/lab_13f.py").read_text() == F13_NEW


def test_full_writes_every_file_as_the_job_holds_it(dj, tmp_path):
    _checkout(tmp_path, {"app/modules/lab_pnl.py": PNL_REVIEWED})
    dj.deliver(_export([_f("app/modules/lab_pnl.py", PNL_JOB, "unchanged")]), "lab_wire", full=True)
    assert (tmp_path / "app/modules/lab_pnl.py").read_text() == PNL_JOB


def test_a_file_a_new_step_changed_carries_the_reviewed_edit_by_a_three_way_merge(dj, tmp_path, capsys):
    base = "A = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n"
    reviewed = base.replace("A = 1", "A = 1  # reviewed")
    job = base.replace("return 2", "return 3")
    _checkout(tmp_path, {"app/modules/lab_x.py": reviewed})
    assert dj.deliver(_export([_f("app/modules/lab_x.py", job, "changed", base=base, step="F2")]), "lab_wire") == 0
    assert (tmp_path / "app/modules/lab_x.py").read_text() == "A = 1  # reviewed\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 3\n"
    assert "merged app/modules/lab_x.py" in capsys.readouterr().out


def test_the_merge_carries_insertions_and_deletions_from_both_sides_and_one_change_made_twice_once(dj):
    base = "".join(f"line{i}\n" for i in range(10))
    ours = base.replace("line1\n", "").replace("line5\n", "line5\nreviewed\n")           # delete + insert
    theirs = base.replace("line8\n", "line8 job\n").replace("line1\n", "")              # edit + the same delete
    merged, clean = dj.merge3(base, ours, theirs)
    assert clean and merged == "line0\nline2\nline3\nline4\nline5\nreviewed\nline6\nline7\nline8 job\nline9\n"
    assert dj.merge3(base, base, theirs) == (theirs, True) and dj.merge3(base, ours, base) == (ours, True)


def test_an_overlapping_change_is_left_marked_and_the_delivery_says_so(dj, tmp_path):
    _checkout(tmp_path, {"app/modules/lab_x.py": "A = 2\n"})
    rc = dj.deliver(_export([_f("app/modules/lab_x.py", "A = 3\n", "changed", base="A = 1\n")]), "lab_wire")
    assert rc == 1
    text = (tmp_path / "app/modules/lab_x.py").read_text()
    assert "<<<<<<< reviewed" in text and "A = 2" in text and "A = 3" in text


# -- the export ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_export_says_what_each_file_did_since_the_delivery(monkeypatch):
    from app.routers import jobs

    async def _visible(*a, **k):
        return None

    async def _rows(db, job_id):
        return _live_rows()

    class _Db:
        async def execute(self, *a, **k):
            return types.SimpleNamespace(mappings=lambda: types.SimpleNamespace(first=lambda: {"refined_brief": {}}))

    monkeypatch.setattr(jobs, "assert_visible", _visible)
    monkeypatch.setattr(es, "code_rows", _rows)
    monkeypatch.setattr(es, "brief_for_the_engine", lambda b, t: b)
    monkeypatch.setattr(es, "targets_the_engine", lambda b: True)
    out = await jobs.job_code_endpoint("5b4880c0-0000-4000-8000-000000000000", True, _Db(), None)
    assert out["delivered_at"] == T0.isoformat()
    by = {f["path"]: f for f in out["files"]}
    assert by["app/modules/lab_pnl.py"]["since_delivery"] == "unchanged"
    assert "delivered_content" not in by["app/modules/lab_pnl.py"]
    assert by["app/modules/lab_13f.py"]["since_delivery"] == "changed"
    assert by["app/modules/lab_13f.py"]["delivered_content"].endswith(F13_OLD)
