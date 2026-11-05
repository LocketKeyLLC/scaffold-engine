"""§17.1905–1909 — a job that writes code for the engine delivers it into the engine before anything runs it; the run
the operator's keys are for is its own step; the three answers are the engine's to check; each outside API the
code calls is researched again and the code audited against it; and the wiring step starts the lab.

Live (follow-up 5b4880c0, 2026-11-04): T10 "Are the end-to-end paper trade keys stored?" -- whose text also said
"Execute one end-to-end trade on the real Alpaca paper account" -- was about to close on any answer, with none of the
job's 29 modules in the engine, no Alpaca key stored, the wiring module exposing four functions nothing called, and
the SEC/FINRA/Congress callers never compared with research this job had not run."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import labs
from app.modules import engine_plan as ep
from app.modules import engine_source as es
from app.modules.engine_contracts import secrets_read

KEYS_TEXT = (ep._KEYS_NOTE + " Execute one end-to-end trade on the real Alpaca paper account: a small proposal (~$100 "
             "notional, fractional shares), operator-approved, sent, filled, and visible in the audit trail and P&L. "
             "Use secrets-store keys ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY. Paper account only.")


def _plan() -> list[dict]:
    return [
        {"id": "T1", "name": "Build the Alpaca client", "tool": "CodeGen", "type": "action", "depends_on": [],
         "notes": "Python module `lab_alpaca_client.py`: the Alpaca paper client."},
        {"id": "T2", "name": "Wire engine entry module", "tool": "CodeGen", "type": "action", "depends_on": ["T1"],
         "notes": "Python module `lab_wire.py`. Add the commands to run the daily batch."},
        {"id": "T10", "name": "Are the end-to-end paper trade keys stored in the engine's secrets?", "tool": "LLM",
         "type": "decision", "depends_on": ["T2"], "notes": KEYS_TEXT},
        {"id": "T11", "name": "Validate end-to-end result", "tool": "LLM", "type": "validation",
         "depends_on": ["T2", "T10"], "notes": "Check the audit trail."},
    ]


SOURCES = {
    "lab_alpaca_client": 'BASE = "https://paper-api.alpaca.markets"\n',
    "lab_edgar": 'URL = "https://data.sec.gov/submissions/CIK{cik}.json"  # EDGAR\n',
    "lab_wire": "def run():\n    return 1\n",
}


def test_the_run_is_its_own_step_the_code_is_delivered_first_and_a_second_pass_adds_nothing():
    tasks = _plan()
    changes = ep.complete_engine_plan(tasks, {"targets_engine": True}, [], SOURCES)
    by = {t["id"]: t for t in tasks}
    run, deliver = by["RUN"], by["DELIVER"]
    assert run["name"] == "Has the end-to-end paper trade been made on the real account?" and run["type"] == "decision"
    assert "Execute one end-to-end trade" in run["notes"] and run["depends_on"] == ["T10"]
    assert "Execute" not in by["T10"]["notes"] and "`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`" in by["T10"]["notes"]
    assert deliver["name"] == "Is this job's code merged into the engine and running?" and deliver["type"] == "decision"
    assert {"T1", "T2", "API_ALPACA_AUDIT", "API_SEC_EDGAR_AUDIT"} <= set(deliver["depends_on"])
    assert "DELIVER" in by["T10"]["depends_on"] and "RUN" in by["T11"]["depends_on"]
    assert [t["id"] for t in tasks].index("DELIVER") < [t["id"] for t in tasks].index("T10")
    # the wiring step starts the lab
    assert "def install(app) -> None" in by["T2"]["notes"] and "does NOT write `app/labs.py`: the delivery adds `lab_wire` there" in by["T2"]["notes"]
    # an API the CODE calls (by its host) is researched and audited, though no step's text names it
    assert by["API_SEC_EDGAR"]["tool"] == "SearXNG" and by["API_SEC_EDGAR_AUDIT"]["depends_on"][0] == "API_SEC_EDGAR"
    assert "`lab_edgar`" in by["API_SEC_EDGAR_AUDIT"]["notes"] and "`lab_alpaca_client` (step T1)" in by["API_ALPACA_AUDIT"]["notes"]
    assert any(c.startswith("delivery_added: DELIVER") for c in changes)
    # the audit is not an outcome check (it must not wait on the keys), and is a review (its findings become fixes)
    assert not ep._checks_the_outcome(by["API_ALPACA_AUDIT"]) and ep._REVIEW_STEP_RE.match(by["API_ALPACA_AUDIT"]["name"])
    again = ep.complete_engine_plan(tasks, {"targets_engine": True}, [], SOURCES)
    assert again == [] and len(tasks) == len(by), again


def test_a_plain_execute_a_trade_is_split_out_and_a_keys_only_question_is_not():
    for text, split in (("Execute one trade on the paper account. Use ALPACA_API_KEY_ID.", True),
                        ("Use secrets-store keys ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.", False)):
        tasks = [{"id": "T9", "name": "Wire it", "tool": "CodeGen", "type": "action", "depends_on": [],
                  "notes": "Python module `lab_wire.py`."},
                 {"id": "T10", "name": "Are the Alpaca paper keys stored in the engine's secrets?", "tool": "LLM",
                  "type": "decision", "depends_on": ["T9"], "notes": ep._KEYS_NOTE + " " + text}]
        ep.complete_engine_plan(tasks, {"targets_engine": True}, [])
        assert ("RUN" in {t["id"] for t in tasks}) is split, text
        assert "DELIVER" in {t["id"] for t in tasks}                       # either way, delivered before the keys


def test_a_fix_step_whose_finding_names_a_migration_stays_its_module():
    tasks = [{"id": "T1F1", "name": "Fix lab_pnl (review T1 findings)", "tool": "CodeGen", "type": "action",
              "depends_on": [], "notes": "Python module `lab_pnl.py` -- edit it in place. Fix:\n- Migration 0010's "
                                         "`fill` table has no `ticker` column."}]
    changes = ep.complete_engine_plan(tasks, {"targets_engine": True}, [])
    assert not any(c.startswith(("ddl_made_a_migration", "schema_first")) for c in changes), changes
    assert tasks[0]["notes"].startswith("Python module `lab_pnl.py`")


def test_an_audits_findings_fix_the_jobs_own_modules_and_delivery_waits_on_them():
    tasks = [
        {"id": "T7", "name": "Build execution entry point", "tool": "CodeGen", "status": "done", "depends_on": [],
         "notes": "Python module `lab_exec.py`."},
        {"id": "API_ALPACA_AUDIT", "name": "Audit the code that calls the Alpaca paper trading API against its research",
         "tool": "LLM", "status": "done", "depends_on": ["T7"], "notes": ""},
        {"id": "DELIVER", "name": "Is this job's code merged into the engine and running?", "tool": "LLM",
         "status": "pending", "depends_on": ["API_ALPACA_AUDIT"], "notes": ""},
    ]
    review = ("- `lab_exec` `submit` posts `qty` as a float, but the documentation says fractional orders take a "
              "string `qty` (https://docs.alpaca.markets/…)\n- `lab_other` is fine and agrees with the research\n")
    changes = ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": review}, [], own=["lab_exec"])
    fix = tasks[-1]
    assert fix["id"] == "API_ALPACA_AUDITF1" and fix["name"] == "Fix lab_exec (review API_ALPACA_AUDIT findings)"
    assert "this job's module" in fix["notes"] and "string `qty`" in fix["notes"]
    assert "API_ALPACA_AUDITF1" in tasks[2]["depends_on"], changes     # T7 wrote it BEFORE the audit: no owner
    assert ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": review}, [], own=["lab_exec"]) == []
    # a second audit finding the same module adds to the fix still open, once
    tasks.append({"id": "API_SEC_AUDIT", "name": "Audit the code that calls the SEC EDGAR APIs against its research",
                  "tool": "LLM", "status": "done", "depends_on": ["T7"], "notes": ""})
    other = {"API_SEC_AUDIT": "- `lab_exec` sends no User-Agent, which the SEC fair-access rule requires of every request\n"}
    assert ep.add_review_fixes(tasks, other, [], own=["lab_exec"]) == [
        "review_fix_extended: API_ALPACA_AUDITF1 also fixes what API_SEC_AUDIT found in `lab_exec.py`"]
    fix = next(t for t in tasks if t["id"] == "API_ALPACA_AUDITF1")
    assert "Also fix what review step API_SEC_AUDIT found in it:\n- `lab_exec` sends no User-Agent" in fix["notes"]
    assert "API_SEC_AUDIT" in fix["depends_on"] and ep.add_review_fixes(tasks, other, [], own=["lab_exec"]) == []


def test_the_delivered_files_are_each_paths_last_writer_and_only_the_jobs_own():
    rows = [
        {"node_key": "f00:T3", "status": "done", "title": "", "prompt_template": "", "description": "",
         "output_text": "```python\n# alembic/versions/0010_lab.py\nop.create_table(\"fill\")\n```"},
        {"node_key": "T2", "status": "done", "title": "", "prompt_template": "", "description": "",
         "output_text": "```python\n# app/modules/lab_x.py\nA = 1\n```\n```python\n# tests/test_lab_x.py\ndef test(): pass\n```"
                        "\n```python\n# app/main.py\nboom = 1\n```"},
        {"node_key": "T5", "status": "done", "title": "", "prompt_template": "", "description": "",
         "output_text": "```python\n# app/modules/lab_x.py\nA = 2\n```"},
        {"node_key": "T6", "status": "failed", "title": "", "prompt_template": "", "description": "",
         "output_text": "```python\n# app/modules/lab_x.py\nA = 3\n```"},
    ]
    files = es.job_files(rows)
    assert list(files) == ["alembic/versions/0010_lab.py", "app/modules/lab_x.py", "tests/test_lab_x.py"]
    assert files["app/modules/lab_x.py"] == ("# app/modules/lab_x.py\nA = 2\n", "T5")      # its header kept
    assert ep.job_tables(files) == ["fill"]


def test_a_secret_list_bound_at_the_top_of_the_module_is_read():
    code = ('from app.modules.runner_secrets import values_for\n'
            'OPERATOR_SECRET_NAMES = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]\n'
            'async def ingest(db):\n    s = await values_for(db, OPERATOR_SECRET_NAMES)\n    return s\n')
    assert set(secrets_read({"m": code}, "m", "ingest")) == {"REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"}
    assert ep.secrets_the_code_reads({"app/modules/m.py": (code, "T1")}) == ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]


def test_a_lab_is_installed_once_listed_and_a_broken_one_does_not_stop_the_others(monkeypatch):
    import sys
    import types
    calls = []
    good = types.ModuleType("app.modules.lab_good")
    good.install = lambda app: calls.append(app)                     # type: ignore[attr-defined]
    bad = types.ModuleType("app.modules.lab_bad")

    def _boom(app):
        raise RuntimeError("no scheduler")
    bad.install = _boom                                              # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.modules.lab_good", good)
    monkeypatch.setitem(sys.modules, "app.modules.lab_bad", bad)
    monkeypatch.setitem(sys.modules, "app.modules.lab_bare", types.ModuleType("app.modules.lab_bare"))
    monkeypatch.setattr(labs, "INSTALLED", {})
    out = labs.install_labs("APP", ("lab_bad", "lab_bare", "lab_good"))
    assert out == {"lab_bad": "failed: RuntimeError('no scheduler')", "lab_bare": "no install(app)", "lab_good": "installed"}
    assert calls == ["APP"] and labs.INSTALLED == out
    assert labs.LABS == () or all(importlib.util.find_spec(f"app.modules.{m}") for m in labs.LABS)


def test_the_delivery_script_lists_the_lab_once():
    spec = importlib.util.spec_from_file_location("deliver_job", Path(__file__).parent.parent / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    src = 'X = 1\nLABS: tuple[str, ...] = ()\nY = 2\n'
    one = mod.list_lab(src, "lab_wire")
    assert 'LABS: tuple[str, ...] = ("lab_wire",)\n' in one and one.endswith("Y = 2\n")
    assert mod.list_lab(one, "lab_wire") == one
    assert 'LABS: tuple[str, ...] = ("lab_wire", "lab_two")' in mod.list_lab(one, "lab_two")
    assert mod._SAFE.match("app/modules/lab_x.py") and not mod._SAFE.match("app/main.py")
    assert 'LABS: tuple[str, ...] = (' in (Path(__file__).parent.parent / "app" / "labs.py").read_text()


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0][0]

    def mappings(self):
        return self


class _Db:
    def __init__(self, answers):
        self.answers, self.sql = answers, []

    async def execute(self, q, params=None):
        s = str(q)
        self.sql.append(s)
        for k, v in self.answers.items():
            if k in s:
                if isinstance(v, Exception):
                    raise v
                return _Res(v)
        return _Res([])

    def begin_nested(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False
        return _Ctx()


LAB = {"app/modules/lab_wire.py": ("def install(app):\n    pass\n", "T2"),
       "app/modules/lab_feed.py": ('from app.modules.runner_secrets import values_for\n'
                                   'async def go(db):\n    return await values_for(db, ["FEED_KEY", "FEED_SECRET"])\n', "T1"),
       "alembic/versions/0010_lab.py": ('op.create_table("lab_fill")\n', "T3")}


@pytest.fixture
def _lab(monkeypatch):
    async def _rows(db, job):
        return []
    monkeypatch.setattr(es, "code_rows", _rows)
    monkeypatch.setattr(es, "job_files", lambda rows: LAB)


async def test_the_keys_and_the_run_are_answered_by_what_the_engine_holds(_lab):
    q = "Are the end-to-end paper trade keys stored in the engine's secrets?"
    got = await ep.answer_unmet(_Db({"runner_secrets": [("FEED_KEY",)]}), "J", q)
    assert got == {"evidence": "secret `FEED_SECRET` is not stored (Machines → Secrets)",
                   "verdicts": ["secret `FEED_SECRET` is not stored (Machines → Secrets)"]}
    assert await ep.answer_unmet(_Db({"runner_secrets": [("FEED_KEY",), ("FEED_SECRET",)]}), "J", q) is None
    run = "Has the end-to-end paper trade been made on the real account?"
    assert (await ep.answer_unmet(_Db({'"lab_fill"': [(0,)]}), "J", run))["verdicts"] == [
        "table `lab_fill` holds no row -- the run has not written it"]
    assert await ep.answer_unmet(_Db({'"lab_fill"': [(1,)]}), "J", run) is None
    assert await ep.answer_unmet(_Db({}), "J", "Which signal source?") is None


async def test_delivery_is_answered_when_the_engine_runs_the_code(_lab, monkeypatch):
    q = "Is this job's code merged into the engine and running?"
    monkeypatch.setattr(labs, "INSTALLED", {})
    got = (await ep.answer_unmet(_Db({"information_schema": []}), "J", q))["verdicts"]
    assert got == ["`app.modules.lab_wire` is not in the running engine", "`app.modules.lab_feed` is not in the running engine",
                   "table `lab_fill` does not exist (the job's revision has not run)",
                   "`lab_wire` was not installed when the engine started (not listed in `app/labs.py`)"], got
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object() if name.startswith("app.modules.lab_") else real(name, *a))
    monkeypatch.setattr(labs, "INSTALLED", {"lab_wire": "installed"})
    assert await ep.answer_unmet(_Db({"information_schema": [("lab_fill",)]}), "J", q) is None


async def test_stale_research_is_read_again_before_delivery_and_its_audit_with_it(monkeypatch):
    from app.modules import node_editor
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    reset = []

    async def _reset(db, job, keys, edited_by=None):
        reset.append((keys, edited_by))
    monkeypatch.setattr(node_editor, "_reset_keys", _reset)

    class _D(_Db):
        async def commit(self):
            pass
    rows = [{"node_key": "API_ALPACA", "status": "done", "completed_at": now - timedelta(days=10)},
            {"node_key": "API_ALPACA_AUDIT", "status": "done", "completed_at": now - timedelta(days=1)},
            {"node_key": "API_REDDIT", "status": "done", "completed_at": now - timedelta(days=1)},
            {"node_key": "API_FINRA", "status": "pending", "completed_at": None}]
    tasks = [{"id": "DELIVER", "name": "Is this job's code merged into the engine and running?", "status": "pending"}]
    got = await ep.refresh_stale_research(_D({"dag_nodes": rows}), "J", tasks, edited_by="op", now=now)
    assert got == ["API_ALPACA", "API_ALPACA_AUDIT"] and reset == [(got, "engine §17.1908 for op")]
    tasks[0]["status"] = "done"                                          # delivered: research is not re-read
    assert await ep.refresh_stale_research(_D({"dag_nodes": rows}), "J", tasks, now=now) == []


def test_the_decision_pause_asks_the_engine_before_it_closes_one_of_these():
    import inspect

    from app.modules import decision_pause
    src = inspect.getsource(decision_pause.resolve_decision)
    assert src.index("engine_plan.answer_unmet(") < src.index("UPDATE dag_nodes SET status = 'done'")
    assert '"outcome": "not_yet"' in src


def test_new_steps_are_inserted_after_the_new_steps_they_depend_on():
    """§17.1910 — live (2026-11-04 21:31 UTC): "node DELIVER depends on unknown node API_SEC_EDGAR_AUDIT"."""
    tasks = _plan()
    ep.complete_engine_plan(tasks, {"targets_engine": True}, [], SOURCES)
    order = [t["id"] for t in ep.new_in_dependency_order(tasks, {"T1", "T2", "T10", "T11"})]
    assert order.index("DELIVER") > max(order.index("API_ALPACA_AUDIT"), order.index("API_SEC_EDGAR_AUDIT"))
    assert order.index("API_SEC_EDGAR_AUDIT") > order.index("API_SEC_EDGAR")
    assert sorted(order) == sorted(t["id"] for t in tasks if t["id"] not in {"T1", "T2", "T10", "T11"})
    assert [t["id"] for t in ep.new_in_dependency_order(
        [{"id": "A", "depends_on": ["B"]}, {"id": "B", "depends_on": ["A"]}], set())] == ["A", "B"]   # a cycle: plan order


def test_the_install_note_names_where_its_imports_live_and_replaces_an_older_one():
    """§17.1911 — live (5b4880c0 T9 a1, 22:01 UTC): "`from app.api.deps import …`: the engine has no module
    `app.api.deps`" -- the note said `Depends(get_principal)` and not where it lives."""
    import importlib
    for imp in ("from app.authz import get_principal", "from app.scheduler import get_scheduler"):
        assert imp in ep._INSTALL_NOTE
        mod, name = imp.removeprefix("from ").split(" import ")
        assert hasattr(importlib.import_module(mod), name)
    old = ("Python module `lab_wire.py`. Add the commands. The engine starts the lab through this module: it exposes "
           "`def install(app) -> None`, which registers … Its tests call `install` with a FastAPI app and a stand-in "
           "scheduler and read the routes and jobs it registered.")
    tasks = [{"id": "T9", "name": "Wire engine entry module", "tool": "CodeGen", "type": "action", "depends_on": [],
              "notes": old}]
    assert "wiring_installs_the_lab: T9 exposes install(app), listed in app/labs.py" in ep.complete_engine_plan(
        tasks, {"targets_engine": True}, [])
    notes = tasks[0]["notes"]
    assert notes.startswith("Python module `lab_wire.py`. Add the commands.") and notes.count("def install(app)") == 1
    assert "from app.authz import get_principal" in notes and "registers …" not in notes
    assert not [c for c in ep.complete_engine_plan(tasks, {"targets_engine": True}, []) if c.startswith("wiring")]


def test_an_included_router_read_for_its_path_is_named_and_the_note_says_where_the_paths_are():
    """§17.1911 — live (T9 a2+a3): "'_IncludedRouter' object has no attribute 'path'" twice, nothing said why."""
    from fastapi import APIRouter, FastAPI

    from app.sandbox import engine_tests as et
    app, r = FastAPI(), APIRouter(prefix="/labs/x")
    r.add_api_route("/run", lambda: 1, methods=["POST"])
    app.include_router(r)
    assert "/labs/x/run" in app.openapi()["paths"]                       # what the hint says to read
    rx, hint = et.KNOWN_CAUSES[11]                                        # stable index (§17.1911)
    assert rx.search("E   AttributeError: '_IncludedRouter' object has no attribute 'path'")
    assert 'app.openapi()["paths"]' in hint and 'app.openapi()[\"paths\"]' in ep._INSTALL_NOTE
    # the older note (§17.1911's first) is replaced whole, not appended to
    older = ("Python module `w.py`. The engine starts the lab through this module: x. Its tests call `install` with a "
             "FastAPI app and a stand-in scheduler, patch the stream consumer it starts, and read the routes and jobs "
             "it registered.")
    tasks = [{"id": "T9", "name": "Wire it", "tool": "CodeGen", "type": "action", "depends_on": [], "notes": older}]
    ep.complete_engine_plan(tasks, {"targets_engine": True}, [])
    assert tasks[0]["notes"].count("The engine starts the lab") == 1 and "app.openapi()" in tasks[0]["notes"]
    ep.complete_engine_plan(tasks, {"targets_engine": True}, [])
    assert tasks[0]["notes"].count("The engine starts the lab") == 1


def test_a_met_verdict_line_is_not_a_finding_and_a_not_met_one_is():
    """§17.1912 — the coverage guard (§17.377) makes an audit write a MET line for a module that agrees."""
    out = ("- MET — `paper_reddit_ingestion` (T6U7): sends the User-Agent the Reddit rules require of each client\n"
           "- **MET** — `paper_reddit_ingestion` paginates with `after` exactly as the listing docs describe\n"
           "- NOT MET — `paper_reddit_ingestion` (T6U7): calls www.reddit.com without OAuth, which the Data API "
           "terms forbid for apps (https://support.reddithelp.com/…)\n"
           "- UNKNOWN — `paper_reddit_ingestion` (T6U7): the rate limit the research does not state for this path\n")
    got = ep.review_findings(out, ["paper_reddit_ingestion"])
    assert [ln.split(" — ")[0] for ln in got["paper_reddit_ingestion"]] == ["NOT MET"], got
    # §17.1928 — UNKNOWN (the research does not state it) is a question for the research, not a finding
    assert [ln.split(" — ")[0] for ln in ep.review_gaps(out, ["paper_reddit_ingestion"])["paper_reddit_ingestion"]] == ["UNKNOWN"]
    assert "`- NOT MET — <module> (<step>): …`" in ep._AUDIT_NOTE and "`- MET — <module> (<step>): …` (no fix)" in ep._AUDIT_NOTE


def test_an_audit_stands_on_each_modules_current_writer_only():
    """§17.1913 — live API_ALPACA_AUDIT a1: the coverage guard wanted T3 cited, an earlier writer of the SPY tracker."""
    tasks = [{"id": "T3", "name": "Fix SPY tracker", "tool": "CodeGen", "type": "action", "depends_on": [],
              "notes": "Python module `lab_spy.py`."},
             {"id": "T5U4", "name": "Fix lab_spy (found by T5's tests)", "tool": "CodeGen", "type": "action",
              "depends_on": ["T3"], "notes": "Python module `lab_spy.py`."}]
    sources = {"lab_spy": 'URL = "https://data.alpaca.markets/v2/stocks/bars"  # alpaca\n'}
    ep.complete_engine_plan(tasks, {"targets_engine": True}, [], sources)
    audit = next(t for t in tasks if t["id"] == "API_ALPACA_AUDIT")
    assert audit["depends_on"] == ["API_ALPACA", "T5U4"] and "`lab_spy` (step T5U4)" in audit["notes"]
    audit["depends_on"] = ["API_ALPACA", "T3", "T5U4"]                       # as an earlier pass left it
    assert "api_audit_stands_on: API_ALPACA_AUDIT on API_ALPACA and the current writers T5U4" in ep.complete_engine_plan(
        tasks, {"targets_engine": True}, [], sources)
    assert audit["depends_on"] == ["API_ALPACA", "T5U4"]


async def test_the_code_verifier_is_shown_the_modules_that_already_exist(monkeypatch):
    """§17.1914 — live (T9, 23:28 UTC): tests passed; the verifier refused an import of the earlier job's
    `paper_fill_tracker` as "a module/symbol that no upstream block or plan step defines"."""
    from app.modules import execution_verify as ev
    seen = {}

    async def _run(messages, role, overrides):
        seen["user"] = messages[1]["content"]
        return "pass", "", 1.0
    monkeypatch.setattr(ev, "_run_verification", _run)
    await ev._verify_codegen_output("Wire it", "code", job_modules=["`app.modules.paper_fill_tracker` (step f00ea97f:T18): track_fills(db)"])
    assert "MODULES THAT ALREADY EXIST" in seen["user"] and "`app.modules.paper_fill_tracker` (step f00ea97f:T18)" in seen["user"]
    await ev._verify_codegen_output("Wire it", "code")
    assert "MODULES THAT ALREADY EXIST" not in seen["user"]
