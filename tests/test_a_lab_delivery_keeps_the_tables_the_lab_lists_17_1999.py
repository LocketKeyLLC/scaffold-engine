"""§17.1999 — a lab delivery keeps the tables the lab's conftest already empties, joined with the job's own.

Live (69fe61c4, 2026-11-22): `deliver_job.py … --lab paper_wire_engine_entry_module` re-wrote
`tests/labs/paper/conftest.py` from the job's export alone -- a follow-up that creates no table -- as `TABLES = []`
over the paper lab's 8, so no lab test would start with the lab's tables empty again."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAB_CONFTEST = REPO / "tests" / "labs" / "paper" / "conftest.py"
PAPER_TABLES = ["ingested_item", "signal", "proposal", "risk_check", "approval", "broker_order_id", "fill", "pnl"]


@pytest.fixture
def dj(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deliver_job_1999", REPO / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    (tmp_path / "app" / "modules").mkdir(parents=True)
    (tmp_path / "app" / "labs.py").write_text("LABS: tuple[str, ...] = ()\n")
    (tmp_path / "app" / "main.py").write_text("from app.modules import paper_dashboard\n")   # §17.2000: it is reached
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _export(tables: list[str]) -> dict:
    test = "def test_page():\n    assert True\n"
    return {"files": [{"path": "tests/test_paper_dashboard.py", "step": "T8", "lines": 2, "content": test},
                      {"path": "app/modules/paper_dashboard.py", "step": "T6", "lines": 1, "content": "X = 1\n"}],
            "tables": tables, "secrets": [], "wiring": ["paper_wire_engine_entry_module"], "delivered_at": None}


def test_the_real_conftest_keeps_its_tables_for_a_job_with_none(dj):
    assert dj.lab_tables(LAB_CONFTEST.read_text(), []) == PAPER_TABLES


def test_a_jobs_new_tables_join_after_the_labs_in_a_stable_order(dj):
    conf = LAB_CONFTEST.read_text()
    assert dj.lab_tables(conf, ["dash_view", "signal", "dash_cache"]) == [*PAPER_TABLES, "dash_view", "dash_cache"]
    assert dj.lab_tables(None, ["b", "a"]) == ["b", "a"]                 # a first delivery: the job's, as it lists them


def test_the_live_delivery_keeps_the_conftest_and_its_tables(dj, tmp_path, capsys):
    lab = tmp_path / "tests" / "labs" / "paper"
    lab.mkdir(parents=True)
    reviewed = LAB_CONFTEST.read_text() + "\n# a reviewed note\n"
    (lab / "conftest.py").write_text(reviewed)
    assert dj.deliver(_export([]), "paper_wire_engine_entry_module") == 0
    assert (lab / "conftest.py").read_text() == reviewed                # nothing to add: left as the review has it
    assert f"empties {', '.join(PAPER_TABLES)} before each test" in capsys.readouterr().out
    dj.deliver(_export(["dash_view"]), "paper_wire_engine_entry_module")
    assert (lab / "conftest.py").read_text() == reviewed.replace(repr(PAPER_TABLES), repr([*PAPER_TABLES, "dash_view"]))


def test_a_first_delivery_writes_the_jobs_tables(dj, tmp_path):
    assert dj.deliver(_export(["dash_view"]), "paper_wire_engine_entry_module") == 0
    text = (tmp_path / "tests" / "labs" / "paper" / "conftest.py").read_text()
    assert "TABLES = ['dash_view']" in text and "_lab_tables_empty" in text
