"""§17.1759–1761 — code written for the engine names tables, columns, revisions and argument kinds that exist, and
never ships a sample value or a note handing its unfinished work on.

Live (job 2772acae "Paper-trading research lab", 2026-10-22/23): T16 inserted columns T2's migration never made
(`risk_results`, `blocking_reasons`, `broker_response`) and a table nobody creates (`audit_events`); T2's revision was
a second Alembic root granting to a role that does not exist, "Set `down_revision` to your current Alembic head";
T3's SEC contact was `research@example.com`; T14 passed `upstream={…}` (a `str` parameter) and the string
`APPROVAL_NODE_KEY` where `park_awaiting_decision` takes the node dict. All passed every gate. The programs below are
cut down from those outputs; the names are the live ones.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import engine_schema as sch
from app.modules import engine_source as es

T2_MIGRATION = '''```python
"""paper trading lab schema"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202410080001"
down_revision = None
branch_labels = None
depends_on = None

APP_DB_ROLE = "scaffold_app"  # Replace with the actual role if it differs.

COMMON_COLUMNS = [
    sa.Column("model_name", sa.String(255), nullable=False),
    sa.Column("data_timestamp", sa.DateTime(timezone=True), nullable=False),
]


def _uuid_pk(name: str = "id") -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "risk_checks",
        _uuid_pk(),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("risk_passed", sa.Boolean, nullable=False),
        sa.Column("max_position_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        *COMMON_COLUMNS,
        sa.Index("ix_risk_checks_proposal_id", "proposal_id"),
    )
    op.execute(sa.text('GRANT INSERT, SELECT ON TABLE "risk_checks" TO "scaffold_app"'))
```

Set `down_revision` to your current Alembic head before running `alembic upgrade head`.'''

T16_WRITER = '''```python
from sqlalchemy import text
from app.database import async_session


async def record_risk_check(proposal_id, results, reasons):
    async with async_session() as db:
        await db.execute(text("""
            INSERT INTO risk_checks (proposal_id, model_name, data_timestamp, risk_results, blocking_reasons)
            VALUES (:p, :m, now(), :r, :b)
        """), {"p": proposal_id, "m": "x", "r": results, "b": reasons})
        await db.execute(text("INSERT INTO audit_events (event_type, payload) VALUES (:t, :p)"), {"t": "x", "p": "{}"})


async def good(proposal_id):
    async with async_session() as db:
        await db.execute(text("INSERT INTO risk_checks (proposal_id, risk_passed, max_position_pct, model_name, "
                              "data_timestamp) VALUES (:p, true, 5, 'm', now())"), {"p": proposal_id})
        rows = await db.execute(text("""
            WITH recent AS (SELECT * FROM risk_checks WHERE created_at > now() - interval '1 day')
            SELECT r.reason, EXTRACT(EPOCH FROM r.data_timestamp) FROM recent r
            JOIN jobs j ON true, LATERAL jsonb_array_elements(j.metadata) AS e
        """))
        return rows
```'''

T3_CLIENT = '''```python
EDGAR_USER_AGENT = "ScaffoldEngine research@example.com"
```'''

T14_APPROVAL = '''```python
from app.database import async_session
from app.modules.decision_pause import frame_decision, park_awaiting_decision

APPROVAL_NODE_KEY = "paper_trade_approval"


async def request_approval(proposal, job_id):
    frame = await frame_decision({"key": APPROVAL_NODE_KEY}, brief="b", upstream={"proposal": proposal})
    async with async_session() as db:
        await park_awaiting_decision(db, job_id, APPROVAL_NODE_KEY, frame)
```'''

LIVE = {"jobs": {"id", "metadata", "status"}}


# ── §17.1759 the SQL ─────────────────────────────────────────────────────────

def test_the_jobs_own_migration_is_read_column_by_column():
    t = sch.tables_in_code([T2_MIGRATION])
    assert t["risk_checks"] == {"id", "proposal_id", "risk_passed", "max_position_pct", "reason", "model_name",
                                "data_timestamp"}


def test_the_live_writer_is_refused_for_each_missing_column_and_table():
    found = sch.unknown_schema_refs(T16_WRITER, {**LIVE, **sch.tables_in_code([T2_MIGRATION])})
    text = "\n".join(found)
    assert "`risk_results`, `blocking_reasons`" in text
    assert "no table `audit_events`" in text
    assert len(found) == 2, found          # the good INSERT, the CTE, EXTRACT(… FROM …), LATERAL and jobs all pass


def test_without_the_migration_the_table_itself_is_missing():
    found = sch.unknown_schema_refs(T16_WRITER, LIVE)
    assert any("no table `risk_checks`" in f for f in found)


def test_a_table_the_step_creates_itself_is_known():
    out = T16_WRITER.replace("```python\n", "```python\n# CREATE TABLE audit_events (event_type text, payload jsonb)\n"
                             "DDL = 'CREATE TABLE IF NOT EXISTS audit_events (event_type TEXT, payload JSONB)'\n", 1)
    found = sch.unknown_schema_refs(out, {**LIVE, **sch.tables_in_code([T2_MIGRATION])})
    assert not any("audit_events" in f for f in found)


def test_prose_and_dynamic_sql_are_not_read():
    out = '''```python
def f(table, cols):
    """Update the row from the cache; select from a list."""
    return f"INSERT INTO {table} ({cols}) VALUES (1)"
```'''
    assert sch.unknown_schema_refs(out, LIVE) == []


def test_the_engines_own_sql_passes_against_its_own_database():
    """Measured: the engine's own source, read against the tables it creates (db/init.sql, db/migrations, alembic),
    gives no finding -- and the corpus is not empty (it has INSERTs the check reads)."""
    root = es.ENGINE_PKG.parent
    ddl = [p.read_text() for p in sorted((root / "db").rglob("*.sql"))]
    ddl += [p.read_text() for p in sorted((root / "alembic" / "versions").glob("*.py"))]
    tables = sch.tables_in_code(["```sql\n" + d + "\n```" for d in ddl])
    for t in ("procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers"):
        tables[t] = None                   # the task queue's own schema (procrastinate applies it, not the repo)
    assert len(tables) > 20
    names = {t for t in tables}
    for t in list(tables):
        tables[t] = None                   # columns added in later migrations are not all readable: names only
    bad, inserts = [], 0
    for p in sorted(es.ENGINE_PKG.rglob("*.py")):
        src = p.read_text()
        inserts += src.count("INSERT INTO ")
        for f in sch.unknown_schema_refs("```python\n" + src + "\n```", tables):
            bad.append((p.name, f[:120]))
    assert inserts > 30 and "dag_nodes" in names
    assert bad == []


@pytest.mark.asyncio
async def test_the_engines_tables_are_read_from_its_database():
    db = AsyncMock()
    res = MagicMock(); res.all.return_value = [("jobs", "id"), ("jobs", "status"), ("dag_nodes", "node_key")]
    db.execute = AsyncMock(return_value=res)
    assert await sch.live_tables(db) == {"jobs": {"id", "status"}, "dag_nodes": {"node_key"}}


# ── §17.1760 the migration, and values the step did not have ─────────────────

def test_the_live_migration_is_refused_for_its_root_name_and_role():
    with patch.object(es, "_database_role", return_value="scaffold"):
        found = es.migration_refs(T2_MIGRATION)
    head = es._latest_migration()
    assert any("down_revision = None" in f and f'down_revision = "{head}"' in f for f in found)
    assert any("revision = '202410080001'" in f for f in found)
    assert any("no role `scaffold_app`" in f for f in found)


def test_a_revision_chained_onto_the_head_or_a_sibling_passes():
    head = es._latest_migration()
    nxt = f"{int(head[:4]) + 1:04d}_paper_lab"
    ok = T2_MIGRATION.replace('"202410080001"', f'"{nxt}"').replace("down_revision = None", f'down_revision = "{head}"')
    ok = ok.replace('TO "scaffold_app"', 'TO "scaffold"')
    with patch.object(es, "_database_role", return_value="scaffold"):
        assert es.migration_refs(ok) == []
        second = ok.replace(f'"{nxt}"', f'"{int(head[:4]) + 2:04d}_paper_lab_more"').replace(
            f'down_revision = "{head}"', f'down_revision = "{nxt}"')
        assert es.migration_refs(second, {nxt}) == []
        assert es.migration_refs(second) != []


def test_a_revision_already_in_the_history_is_not_new():
    p = sorted((es.ENGINE_PKG.parent / "alembic" / "versions").glob("[0-9]*_*.py"))[-1]
    assert es.migration_refs("```python\n" + p.read_text() + "\n```") == []


def test_a_sample_value_and_a_handoff_are_refused():
    assert any("research@example.com" in f for f in es.placeholder_refs(T3_CLIENT))
    assert any("unfinished work" in f for f in es.placeholder_refs(T2_MIGRATION))


def test_a_test_may_use_sample_data_and_prose_may_say_never():
    test = '''```python
# tests/test_edgar.py
def test_agent():
    assert make("research@example.com")
```'''
    assert es.placeholder_refs(test) == []
    assert es.placeholder_refs("```python\nX = 1  # do not replace it with your own design\n```") == []


# ── §17.1761 argument kinds ──────────────────────────────────────────────────

def test_the_live_approval_calls_are_refused_for_the_kinds_they_pass():
    found = [f for f in es.unresolved_refs(T14_APPROVAL) if "given a" in f]
    assert any(f.startswith("`frame_decision(…)`: `upstream` is `str`; it was given a `dict`")
               and "the signature is `async frame_decision(node: dict, *, brief: str = …, upstream: str = …)`" in f for f in found)
    assert any("park_awaiting_decision" in f and "`node` is `dict`" in f and "APPROVAL_NODE_KEY" in f for f in found)


def test_the_engines_own_source_passes_its_own_kind_check():
    bad = [(p.name, f[:100]) for p in sorted(es.ENGINE_PKG.rglob("*.py"))
           for f in es.unresolved_refs("```python\n" + Path(p).read_text() + "\n```") if "it was given a `" in f]
    assert bad == []


# ── the production path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_findings_reads_the_jobs_siblings_and_the_database():
    db = AsyncMock()
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [
        {"node_key": "T2", "title": "Define Postgres audit schema", "description": "", "prompt_template": "",
         "output_text": T2_MIGRATION},
        {"node_key": "T16", "title": "Implement audit trail writer", "description": "", "prompt_template": "",
         "output_text": "an older draft"}]
    cols = MagicMock(); cols.all.return_value = [("jobs", "id"), ("jobs", "metadata"), ("jobs", "status")]
    db.execute = AsyncMock(side_effect=[nodes, cols])
    found = await es.engine_findings(db, "job-1", "T16", T16_WRITER)
    assert any("risk_results" in f for f in found) and any("audit_events" in f for f in found)
    assert not any("no table `risk_checks`" in f for f in found)       # T2, a sibling, creates it


# ── §17.1762 what the job's own modules define ───────────────────────────────

T10_STREAM = {"node_key": "T10", "title": "Implement Alpaca websocket stream", "description": "",
              "prompt_template": "Python module `alpaca_stream.py` with `start_stream(callback) -> None`.",
              "output_text": '''```python
import logging
from typing import Any, Callable
logger = logging.getLogger("scaffold")
MAX_BACKOFF_SECONDS = 60


async def start_stream(callback: Callable[[dict], Any]) -> None:
    ...
```'''}
T16_AUDIT = {"node_key": "T16", "title": "Implement audit trail writer", "description": "", "prompt_template": "",
             "output_text": '''```python
# app/modules/audit_writer.py
async def append_audit_event(*, event_type: str, payload: dict, model_name: str, data_timestamp=None) -> None:
    ...
```'''}
T12_KILL = '''```python
from app.modules.alpaca_stream import start_stream, get_open_orders, OrderState
from app.modules.audit_writer import append_audit_event


async def activate_kill_switch(runner_id):
    await append_audit_event(event_type="kill_switch", payload={}, model_name="x", runner_id=runner_id)
    await start_stream(print)
```'''


def test_a_step_reads_which_module_it_writes():
    assert es.step_module(T10_STREAM) == "alpaca_stream"
    assert es.step_module(T16_AUDIT) == "audit_writer"
    assert es.step_module_path({"output_text": "```python\n# app/routers/paper_lab.py\nx = 1\n```"}) == "app.routers.paper_lab"


def test_the_live_kill_switch_is_refused_for_names_its_siblings_never_wrote():
    found = es.sibling_refs(T12_KILL, es.sibling_definitions([T10_STREAM, T16_AUDIT]))
    text = "\n".join(found)
    assert "`alpaca_stream` (step T10) has no `get_open_orders` (it has start_stream, MAX_BACKOFF_SECONDS)" in text
    assert "has no `OrderState`" in text
    assert "has no parameter `runner_id`" in text and "step T16" in text
    assert "start_stream`" not in text.replace("(it has start_stream, MAX_BACKOFF_SECONDS)", "")
    assert len(found) == 3, found


def test_a_sibling_not_yet_written_is_not_checked():
    assert es.sibling_refs(T12_KILL, es.sibling_definitions([{**T10_STREAM, "output_text": ""}, T16_AUDIT])) == [
        f for f in es.sibling_refs(T12_KILL, es.sibling_definitions([T16_AUDIT]))]


def test_a_module_named_like_an_engine_module_is_refused_unless_it_keeps_it():
    row = {"node_key": "T12", "prompt_template": "Python module `kill_switch.py` with `activate_kill_switch()`."}
    found = es.replaces_an_engine_module(T12_KILL, row)
    assert found and "app.modules.kill_switch" in found[0]
    assert es.replaces_an_engine_module(T12_KILL, {"prompt_template": "Python module `paper_kill_switch.py`."}) == []
    keeps = "```python\n# app/modules/kill_switch.py\n" + (es.ENGINE_PKG / "modules" / "kill_switch.py").read_text() + "\n```"
    assert es.replaces_an_engine_module(keeps, row) == []
    other_pkg = "```python\n# app/modules/metrics.py\nX = 1\n```"           # app.observability.metrics is not it
    assert es.replaces_an_engine_module(other_pkg, {}) == []


# ── §17.1763 closed value sets and tuple results ────────────────────────────

T13_PROPOSE = '''```python
from app.model_router import generate_json
from app.modules.runner_secrets import values_for

KEY = "alpaca_paper_api_key"


async def propose(prompt, schema, db):
    llm_output = await generate_json(prompt, schema, model="gpt-4o-mini", role="research", fallback=True)
    if not isinstance(llm_output, list):
        return []
    return await values_for(db, [KEY, "ALPACA_PAPER_SECRET"])
```'''


def test_the_live_proposal_call_is_refused_for_role_model_fallback_and_tuple():
    found = es.unresolved_refs(T13_PROPOSE)
    text = "\n".join(found)
    assert "never both" in text
    assert "'research' is not one" in text
    assert "`fallback` is `str | None`; it was given a `bool`" in text
    assert "`generate_json` returns a tuple" in text
    assert "'alpaca_paper_api_key' is not one" in text and "ALPACA_PAPER_SECRET" not in text


def test_the_right_shapes_pass():
    ok = '''```python
from app.model_router import generate_json
from app.modules.runner_secrets import values_for


async def propose(prompt, schema, db):
    parsed, resp = await generate_json(prompt, schema, role="model_general", as_array=True)
    whole = await generate_json(prompt, schema, role="model_general")
    first = whole[0]
    return parsed, first, await values_for(db, ["ALPACA_PAPER_KEY_ID"])
```'''
    assert es.unresolved_refs(ok) == []


def test_the_engines_own_source_passes_the_value_checks():
    bad = [(p.name, f[:100]) for p in sorted(es.ENGINE_PKG.rglob("*.py"))
           for f in es.unresolved_refs("```python\n" + Path(p).read_text() + "\n```")
           if "takes a model ROLE" in f or "secret name" in f or "never both" in f or "returns a tuple" in f]
    assert bad == []


@pytest.mark.asyncio
async def test_engine_findings_carries_every_check():
    db = AsyncMock()
    me = {"node_key": "T12", "title": "Implement kill switch", "description": "",
          "prompt_template": "Python module `kill_switch.py` with `activate_kill_switch() -> dict`.", "output_text": ""}
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [T10_STREAM, T16_AUDIT, me]
    cols = MagicMock(); cols.all.return_value = [("jobs", "id")]
    db.execute = AsyncMock(side_effect=[nodes, cols])
    out = T12_KILL.replace("    await start_stream(print)",
                           '    await start_stream(print)\n    return "ops@example.com"')
    found = "\n".join(await es.engine_findings(db, "job-1", "T12", out))
    assert "has no `get_open_orders`" in found                       # §17.1762 siblings
    assert "app.modules.kill_switch" in found                        # §17.1762 collision
    assert "ops@example.com" in found                                # §17.1760 placeholder


@pytest.mark.asyncio
async def test_engine_findings_checks_the_migration():
    db = AsyncMock()
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [
        {"node_key": "T2", "title": "Define Postgres audit schema", "description": "", "prompt_template": "",
         "output_text": ""}]
    cols = MagicMock(); cols.all.return_value = []
    db.execute = AsyncMock(side_effect=[nodes, cols])
    found = "\n".join(await es.engine_findings(db, "job-1", "T2", T2_MIGRATION))
    assert "down_revision = None" in found                            # §17.1760 migration


# ── §17.1775 a step is told its own file and may not write a sibling's (live job f00ea97f T6, T9) ──

T4_ROW = {"node_key": "T4", "title": "Implement audit trail writer", "description": "",
          "prompt_template": "Python module `paper_audit_trail_writer.py`. Exports write_signal().", "output_text":
          "```python\n# app/modules/paper_audit_trail_writer.py\nasync def write_signal(**kw):\n    ...\n```"}
T6_ROW = {"node_key": "T6", "title": "Implement risk control module", "description": "",
          "prompt_template": "Python module `paper_risk_control_module.py`. Max position 5% equity.", "output_text": ""}


def test_the_step_is_told_which_module_is_its_own():
    b = es.block_for(T6_ROW, {"paper_audit_trail_writer", "paper_risk_control_module", "paper_alpaca_paper_client"})
    assert "**This step writes `app/modules/paper_risk_control_module.py`**" in b
    assert "`paper_alpaca_paper_client.py`, `paper_audit_trail_writer.py` -- belong to other steps" in b


def test_writing_a_siblings_module_is_refused():
    live = "```python\n# app/modules/paper_audit_trail_writer.py\nasync def write_signal(**kw):\n    ...\n```"
    found = es.writes_a_siblings_module(live, T6_ROW, [T4_ROW])
    assert found and "step T4's" in found[0] and "`paper_risk_control_module.py`" in found[0]
    own = "```python\n# app/modules/paper_risk_control_module.py\nMAX = 0.05\n```\n```python\n# tests/test_paper_risk_control_module.py\ndef test_x():\n    assert True\n```"
    assert es.writes_a_siblings_module(own, T6_ROW, [T4_ROW]) == []


@pytest.mark.asyncio
async def test_engine_findings_refuses_a_siblings_file():
    db = AsyncMock()
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [T4_ROW, T6_ROW]
    cols = MagicMock(); cols.all.return_value = []
    db.execute = AsyncMock(side_effect=[nodes, cols])
    out = "```python\n# app/modules/paper_audit_trail_writer.py\nasync def write_signal(**kw):\n    ...\n```"
    found = "\n".join(await es.engine_findings(db, "job-1", "T6", out))
    assert "that module is step T4's" in found


def test_a_value_only_the_operator_has_is_read_from_the_engines_secrets():
    """§17.1776 — live (job f00ea97f T7/T8): told "a Settings field a step of this job adds", every attempt either
    shipped `contact@example.com` or read `settings.sec_user_agent_contact` (refused: no step adds settings) -- both
    steps failed 3/3. The way that can pass: read it with values_for when the code runs."""
    found = es.placeholder_refs('```python\nUA = "ScaffoldEngine contact@example.com"\n```')
    assert found and 'values_for(db, ["UPPER_SNAKE_NAME"])' in found[0] and "Machines page" in found[0]
    assert "app.config.Settings" not in found[0]
    s = [f for f in es.unresolved_refs('```python\nfrom app.config import settings\nX = settings.sec_user_agent_contact\n```')
         if "sec_user_agent_contact" in f]
    assert s and "values_for" in s[0] and "a step of this job adds" not in s[0]
    assert "values_for(db, [\\\"UPPER_SNAKE_NAME\\\"])" in es.CONVENTIONS or 'values_for(db, ["UPPER_SNAKE_NAME"])' in es.CONVENTIONS


# ── §17.1782 a one-table SELECT's columns; a starred column helper (live job f00ea97f T3, T13) ──

T3_HELPER = '''```python
import sqlalchemy as sa
from alembic import op


def _audit_columns() -> list[sa.Column]:
    return [sa.Column("model_ts", sa.DateTime(timezone=True)), sa.Column("data_ts", sa.DateTime(timezone=True))]


def upgrade() -> None:
    op.create_table(
        "ingested_item",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("published_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_ts", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("id", name="uq"),
    )
```'''


def test_a_starred_column_helper_is_read():
    assert sch.tables_in_code([T3_HELPER])["ingested_item"] == {"id", "published_ts", "fetched_ts", "model_ts", "data_ts"}


def test_the_live_point_in_time_read_is_refused_for_its_columns():
    t13 = '''```python
from sqlalchemy import text
Q = text("SELECT id, publication_time, fetched_time FROM ingested_item WHERE published_ts <= :as_of ORDER BY published_ts")
OK = text("SELECT id, published_ts AS t, COUNT(*) AS n FROM ingested_item WHERE fetched_ts <= :x ORDER BY n, t")
J = text("SELECT i.id, s.nope FROM ingested_item i JOIN signal s ON s.ingested_item_id = i.id")
```'''
    found = sch.unknown_schema_refs(t13, {**sch.tables_in_code([T3_HELPER]), "signal": {"id", "ingested_item_id"}})
    text = "\n".join(found)
    assert "no column `publication_time`" in text and "no column `fetched_time`" in text
    assert len(found) == 2, found          # the aliased ORDER BY passes; a JOIN is not read for columns


def test_the_engines_own_selects_pass_against_its_live_columns():
    """Measured 2026-10-24 against the live database's information_schema: 534 SELECT strings, 0 findings (the
    first version hit `ORDER BY calls`, an alias, twice). Here: the engine's own tables as its DDL makes them."""
    import re
    p = es.ENGINE_PKG / "modules" / "cost_rollup.py"
    src = p.read_text()
    assert "AS calls" in src
    tables = {"llm_call_logs": {"id", "job_id", "model", "call_kind", "cost_usd", "created_at", "latency_ms",
                                "prompt_tokens", "completion_tokens", "error", "node_key", "role", "provider",
                                "success", "fallback_used", "retries", "duration_ms", "tokens"}}
    found = [f for f in sch.unknown_schema_refs("```python\n" + src + "\n```", tables) if "SELECT …" in f
             and re.search(r"no column `calls`", f)]
    assert found == []



def test_a_siblings_test_file_is_not_part_of_its_module():
    """Live (job f00ea97f T20): T21's module "had" FakeResponse / FakeSecretStore -- the names of its test file."""
    t21 = {"node_key": "T21", "prompt_template": "Python module `paper_spy_benchmark_tracker.py`.", "output_text":
           "```python\n# app/modules/paper_spy_benchmark_tracker.py\ndef get_spy_benchmark():\n    ...\n```\n"
           "```python\n# tests/test_paper_spy_benchmark_tracker.py\nclass FakeSecretStore:\n    pass\n```"}
    d = es.sibling_definitions([t21])["paper_spy_benchmark_tracker"]
    assert "get_spy_benchmark" in d["names"] and "FakeSecretStore" not in d["names"]



def test_a_missing_table_is_told_which_tables_the_job_has():
    """§17.1784 — live (f00ea97f T24): four attempts each guessed a new table name; the refusal never said which exist."""
    out = "```python\nfrom sqlalchemy import text\nQ = text('SELECT * FROM positions')\n```"
    found = sch.unknown_schema_refs(out, {**LIVE, **sch.tables_in_code([T3_HELPER])}, {"ingested_item", "signal"})
    assert found and "this job's tables are `ingested_item`, `signal`" in found[0]



def test_a_call_refusal_carries_the_whole_signature():
    """§17.1785 — live (f00ea97f T17, three attempts on the same call): "takes at most 0 positional argument(s)",
    "missing `approval_id`" -- each attempt fixed one argument and broke another."""
    t4 = {"node_key": "T4", "prompt_template": "", "output_text": "```python\n# app/modules/paper_audit_trail_writer.py\n"
          "async def write_order(*, proposal_id, approval_id, broker_order_id, submitted_ts=None):\n    ...\n```"}
    out = "```python\nfrom app.modules.paper_audit_trail_writer import write_order\n\nasync def go(o):\n    await write_order(o)\n```"
    found = es.sibling_refs(out, es.sibling_definitions([t4]))
    assert found and all("the signature is `async write_order(*, proposal_id, approval_id, broker_order_id, "
                         "submitted_ts=…)`" in f for f in found)


# ── §17.1786 every code step is shown the job's own tables (live job f00ea97f T13, T20) ──

def test_the_block_shows_the_jobs_tables_first_and_says_they_win():
    tables = sch.tables_in_code([T3_HELPER])
    b = es.block_for(T6_ROW, {"paper_risk_control_module"}, tables=tables)
    assert "### This job's tables (from its migration -- exact names; where this step's text names a table or column " \
           "differently, these win)" in b
    assert "    ingested_item: data_ts, fetched_ts, id, model_ts, published_ts" in b
    assert b.index("This job's tables") < b.index("### Conventions")       # survives the budget cut
    assert "This job's tables" not in es.block_for(T6_ROW, set())


@pytest.mark.asyncio
async def test_the_executor_shows_the_steps_the_jobs_tables():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.modules.engine_schema.tables_in_code", return_value={"ingested_item": {"published_ts", "id"}}):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "    ingested_item: id, published_ts" in captured["prompt"]


# ── §17.1787 a module another step has not written yet is not importable (live job f00ea97f T20) ──

def test_importing_an_unwritten_siblings_module_is_refused():
    t22 = {"node_key": "T22", "prompt_template": "Python module `paper_performance_metrics_calculator.py`.",
           "output_text": ""}                                             # reset: nothing written
    out = ("```python\nfrom app.modules.paper_performance_metrics_calculator import get_performance_metrics\n"
           "from app.modules.paper_audit_trail_writer import write_signal\n```")
    others = [t22, T4_ROW]
    found = es.imports_an_unwritten_sibling(out, others, es.sibling_definitions(others))
    assert found == ["`app.modules.paper_performance_metrics_calculator`: that module is step T22's and is not "
                     "written yet -- this step cannot import it; use the modules that exist (listed above), or "
                     "compute what it needs here"]


def test_the_block_offers_only_written_modules():
    b = es.block_for(T6_ROW, {"paper_risk_control_module", "paper_audit_trail_writer", "paper_performance_metrics_calculator"},
                     written={"paper_audit_trail_writer"})
    assert "The other modules -- `paper_audit_trail_writer.py` -- belong to other steps" in b
    assert "Not written yet, so NOT importable here: `paper_performance_metrics_calculator.py`" in b


@pytest.mark.asyncio
async def test_engine_findings_refuses_an_unwritten_import():
    db = AsyncMock()
    t22 = {"node_key": "T22", "title": "Implement performance metrics calculator", "description": "",
           "prompt_template": "Python module `paper_performance_metrics_calculator.py`.", "output_text": ""}
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [t22, T6_ROW]
    cols = MagicMock(); cols.all.return_value = []
    db.execute = AsyncMock(side_effect=[nodes, cols])
    out = "```python\n# app/modules/paper_risk_control_module.py\nfrom app.modules.paper_performance_metrics_calculator import x\n```"
    found = "\n".join(await es.engine_findings(db, "job-1", "T6", out))
    assert "step T22's and is not written yet" in found


@pytest.mark.asyncio
async def test_the_executor_tells_the_step_which_modules_are_not_written_yet():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)          # its one sibling (T9, alpaca_client.py) has no output
    assert "Not written yet, so NOT importable here: `alpaca_client.py`" in captured["prompt"]


def test_a_siblings_coroutine_used_as_a_value_is_refused():
    """§17.1787 — live (f00ea97f T18): "'coroutine' object is not subscriptable" in the sandbox, every attempt."""
    t5 = {"node_key": "T5", "prompt_template": "", "output_text": "```python\n# app/modules/paper_alpaca_paper_client.py\n"
          "async def get_order(order_id):\n    ...\n```"}
    bad = ("```python\nfrom app.modules.paper_alpaca_paper_client import get_order\n\n"
           "async def qty(oid):\n    return get_order(oid)['filled_qty']\n```")
    ok = ("```python\nfrom app.modules.paper_alpaca_paper_client import get_order\n\n"
          "async def qty(oid):\n    return (await get_order(oid))['filled_qty']\n```")
    defs = es.sibling_definitions([t5])
    found = es.sibling_refs(bad, defs)
    assert found and "is `async def` in `paper_alpaca_paper_client`" in found[0] and "step T5" in found[0]
    assert es.sibling_refs(ok, defs) == []


# ── §17.1789 the job's foreign keys are shown (live job f00ea97f T18) ──

T3_FK = '''```python
import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table("approval", sa.Column("id", sa.BigInteger, primary_key=True))
    op.create_table(
        "broker_order_id",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("approval_id", sa.BigInteger, sa.ForeignKey("approval.id"), nullable=False, unique=True),
    )
    op.execute("CREATE TABLE IF NOT EXISTS fill (id BIGSERIAL PRIMARY KEY, broker_order_id BIGINT NOT NULL REFERENCES broker_order_id (id))")
```'''


def test_foreign_keys_are_read_and_shown():
    fks = sch.foreign_keys_in_code([T3_FK])
    assert fks == {"broker_order_id": {"approval_id": "approval.id"}, "fill": {"broker_order_id": "broker_order_id.id"}}
    b = es.block_for(T6_ROW, set(), tables=sch.tables_in_code([T3_FK]), fks=fks)
    assert "    broker_order_id: approval_id→approval.id, id" in b
    assert "insert the parent row first -- in tests too" in b


@pytest.mark.asyncio
async def test_the_executor_passes_the_foreign_keys():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.modules.engine_schema.tables_in_code", return_value={"fill": {"id", "broker_order_id"}}), \
         patch("app.modules.engine_schema.foreign_keys_in_code", return_value={"fill": {"broker_order_id": "broker_order_id.id"}}):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "fill: broker_order_id→broker_order_id.id, id" in captured["prompt"]


def test_a_refused_column_says_where_it_lives_and_how_to_reach_it():
    """§17.1791 — live (f00ea97f T24): `SELECT … ticker … FROM fill`, refused round after round."""
    tables = {"fill": {"id", "broker_order_id", "price"}, "broker_order_id": {"id", "proposal_id"},
              "proposal": {"id", "ticker", "signal_id"}, "signal": {"id", "ticker"}}
    fks = {"fill": {"broker_order_id": "broker_order_id.id"}, "broker_order_id": {"proposal_id": "proposal.id"},
           "proposal": {"signal_id": "signal.id"}}
    out = "```python\nfrom sqlalchemy import text\nQ = text('SELECT ticker, price FROM fill WHERE id = :i')\n```"
    found = sch.unknown_schema_refs(out, tables, set(tables), fks)
    assert found and ("`ticker` is on `proposal` -- reach it from `fill` with a JOIN along "
                      "fill.broker_order_id → broker_order_id.id , broker_order_id.proposal_id → proposal.id") in found[0]


def test_a_refused_plural_column_points_to_its_singular():
    """§17.1793 — live (f00ea97f T24): `SELECT … sources … FROM proposal`; the job keeps `ingested_item.source`."""
    tables = {"proposal": {"id", "signal_id", "rationale"}, "signal": {"id", "ingested_item_id"},
              "ingested_item": {"id", "source", "payload"}}
    fks = {"proposal": {"signal_id": "signal.id"}, "signal": {"ingested_item_id": "ingested_item.id"}}
    out = "```python\nfrom sqlalchemy import text\nQ = text('SELECT rationale, sources FROM proposal')\n```"
    found = sch.unknown_schema_refs(out, tables, set(tables), fks)
    assert found and "`source` (not `sources`) is on `ingested_item` -- reach it from `proposal` with a JOIN" in found[0]


# ── §17.1794 JSON columns and CHECK constraints are shown (live job f00ea97f T24D) ──

T3_RULES = '''```python
import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "ingested_item",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("fetched_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("slug", sa.Text, unique=True),
        sa.CheckConstraint("source IN ('sec_edgar','reddit')", name="ck_source"),
        sa.UniqueConstraint("source", "external_id", name="uq_source_external_id"),
    )
    op.create_table("plain", sa.Column("id", sa.BigInteger, primary_key=True))
```'''


def test_json_columns_and_checks_are_read_and_shown():
    rules = sch.column_rules_in_code([T3_RULES])
    assert rules == {"ingested_item": {"json": ["payload (json)"], "checks": ["source IN ('sec_edgar','reddit')"],
                                       "required": ["source", "external_id", "payload"],     # §17.1800
                                       "unique": ["(slug)", "(source, external_id)"]}}
    b = es.block_for(T6_ROW, set(), tables=sch.tables_in_code([T3_RULES]), rules=rules)
    assert ("required: source, external_id, payload; UNIQUE (slug); UNIQUE (source, external_id); "
            "JSON: payload (json); CHECK source IN ('sec_edgar','reddit')") in b
    assert "each test makes its own UNIQUE values -- a text one from `uuid4().hex`" in b
    assert "an id is never made up: it is the `RETURNING id` of the row the test inserted" in b   # §17.1802
    assert "`CAST(:p AS json)` / `CAST(:p AS jsonb)` -- its own type" in b


@pytest.mark.asyncio
async def test_the_executor_passes_the_column_rules():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.modules.engine_schema.tables_in_code", return_value={"ingested_item": {"id", "payload"}}), \
         patch("app.modules.engine_schema.column_rules_in_code",
               return_value={"ingested_item": {"json": ["payload"], "checks": []}}):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "JSON: payload" in captured["prompt"]


# ── §17.1796 an index of the job's written modules (live job f00ea97f T25) ──

def test_the_block_indexes_the_jobs_modules_with_signatures():
    defs = es.sibling_definitions([T4_ROW, T10_STREAM])
    b = es.block_for(T6_ROW, set(), modules=defs)
    assert "### This job's modules (written -- their public functions, exact signatures; `p:a|b` means `p` takes only " \
           "`a` or `b` -- the function refuses the rest; `p: dict` is the kind `p` takes -- a call that hands it a literal of another kind is refused; `p: dict{k, q: Decimal}` are the keys it reads from `p`, each with the kind it takes; `p{get()->{json(): dict}}` is what it does with `p` -- a fake must do the same; `[secrets: A, B]` means the function reads those operator secrets through `values_for` -- a test patches `values_for` on that module to return `{\"A\": …, \"B\": …}`; `[own session]` means it opens its own `async_session()` and sees only rows the test has committed -- `await db.commit()` after inserting the parent rows, before calling it)" in b
    assert "    alpaca_stream: async start_stream(callback)" in b
    assert "    paper_audit_trail_writer: async write_signal(**kwargs)" in b
    assert b.index("This job's modules") < b.index("### Conventions")


def test_the_it_has_list_shows_functions_before_constants():
    t9 = {"node_key": "T9", "prompt_template": "", "output_text": "```python\n# app/modules/paper_congress.py\n"
          + "".join(f"ALLOWED_{i} = {i}\n" for i in range(30)) + "async def fetch_disclosures(client=None):\n    ...\n```"}
    found = es.sibling_refs("```python\nfrom app.modules.paper_congress import ingest_trades\n```",
                            es.sibling_definitions([t9]))
    assert found and "(it has fetch_disclosures, ALLOWED_0" in found[0]


@pytest.mark.asyncio
async def test_the_executor_passes_the_module_index():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    with patch("app.modules.engine_source.sibling_definitions",
               return_value={"paper_x": {"node": "T1", "names": {"go"}, "defined": {"go"},
                                         "funcs": {"go": es._call_shape(__import__("ast").parse("def go(a, *, b=1): pass").body[0])}}}):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "    paper_x: go(a, *, b=…)" in captured["prompt"]


# ── §17.1798 the module index shows public classes (live job f00ea97f T25: AlpacaPaperClient invisible) ──

T5_CLIENT = {"node_key": "T5", "prompt_template": "", "output_text": """```python
# app/modules/paper_alpaca_paper_client.py
from dataclasses import dataclass


class AlpacaPaperClientError(Exception):
    pass


@dataclass
class AlpacaAccount:
    equity: str
    cash: str
    _raw: dict | None = None


class AlpacaPaperClient:
    def __init__(self, *, base_url: str = "x") -> None:
        self._c = None

    async def _request(self, m):
        ...

    async def get_account(self) -> AlpacaAccount:
        ...

    async def get_latest_trade(self, symbol: str) -> dict:
        ...

    @staticmethod
    def build(symbol, qty):
        ...

    @property
    def ready(self) -> bool:
        return True
```"""}


def test_the_index_shows_a_modules_public_classes():
    r = es._render_job_modules(es.sibling_definitions([T5_CLIENT]), "other")
    line = [x for x in r.splitlines() if "paper_alpaca_paper_client" in x][0]
    assert "class AlpacaAccount(equity: str, cash: str, _raw: dict | None = …)" in line     # §17.1865 its fields' kinds                              # §17.1803 its constructor
    assert ("class AlpacaPaperClient(*, base_url: str = …) {build(symbol, qty), async get_account(), "
            "async get_latest_trade(symbol: str); read-only: ready}") in line                    # §17.1803
    assert "AlpacaPaperClientError" not in line and "_request" not in line


# ── §17.1801 a left-out test file is asked for alone (live job f00ea97f, the entry-point steps) ──

ENTRY_STEP = ("Python module `paper_entry_execution.py`: the lab's `execution` entry point. In this same step also write "
              "`tests/test_paper_entry_execution.py`: pytest tests of what `paper_entry_execution.py` must do.")
MODULE_ONLY = "```python\n# app/modules/paper_entry_execution.py\nasync def run_execution():\n    return 1\n```"
TESTS_ONLY = ("```python\n# tests/test_paper_entry_execution.py\nimport pytest\n\n\n@pytest.mark.asyncio\n"
              "async def test_run():\n    from app.modules.paper_entry_execution import run_execution\n"
              "    assert await run_execution() == 1\n```")


def test_a_required_test_file_left_out_is_named():
    assert es.missing_test_file(MODULE_ONLY, ENTRY_STEP) == "tests/test_paper_entry_execution.py"
    assert es.missing_test_file(MODULE_ONLY + "\n\n" + TESTS_ONLY, ENTRY_STEP) is None
    assert es.missing_test_file(MODULE_ONLY, "Python module `paper_x.py`.") is None       # no tests required
    other = TESTS_ONLY.replace("test_paper_entry_execution.py", "test_other.py")
    assert es.missing_test_file(MODULE_ONLY + "\n\n" + other, ENTRY_STEP) == "tests/test_paper_entry_execution.py"


# ── §17.1860 a required test path is exact (live 5b4880c0 T5, the kimi round: `tests/test_test_paper_twelve_modules.py`)

def test_a_near_miss_of_the_required_test_path_is_not_it_and_is_named():
    from app.modules.engine_contracts import drop_file_block
    doubled = TESTS_ONLY.replace("# tests/test_paper_entry_execution.py", "# tests/test_test_paper_entry_execution.py")
    out = MODULE_ONLY + "\n\n" + doubled
    assert es.missing_test_file(out, ENTRY_STEP) == "tests/test_paper_entry_execution.py"      # was accepted before
    assert es.misnamed_test_file(out, ENTRY_STEP) == "tests/test_test_paper_entry_execution.py"
    short = TESTS_ONLY.replace("# tests/test_paper_entry_execution.py", "# tests/test_paper_entry.py")
    assert es.misnamed_test_file(MODULE_ONLY + "\n\n" + short, ENTRY_STEP) == "tests/test_paper_entry.py"
    bare = TESTS_ONLY.replace("# tests/test_paper_entry_execution.py", "# test_paper_entry_execution.py")
    assert es.missing_test_file(MODULE_ONLY + "\n\n" + bare, ENTRY_STEP) is None               # the bare file name
    assert es.misnamed_test_file(MODULE_ONLY + "\n\n" + TESTS_ONLY, ENTRY_STEP) is None         # exact: nothing
    other = TESTS_ONLY.replace("test_paper_entry_execution.py", "test_other.py")
    assert es.misnamed_test_file(MODULE_ONLY + "\n\n" + other, ENTRY_STEP) is None             # not a near miss
    assert es.misnamed_test_file(MODULE_ONLY, "Python module `paper_x.py`.") is None
    assert es.misnamed_required_tests(out, ENTRY_STEP) == [
        "This step requires its tests at `tests/test_paper_entry_execution.py` -- that exact path -- and the output "
        "wrote `tests/test_test_paper_entry_execution.py` instead: the block's first line is "
        "`# tests/test_paper_entry_execution.py`"]
    assert es.misnamed_required_tests(MODULE_ONLY + "\n\n" + TESTS_ONLY, ENTRY_STEP) == []
    # the misnamed block is dropped once the required one is in hand; the module stays
    dropped = drop_file_block(out, "tests/test_test_paper_entry_execution.py")
    assert MODULE_ONLY in dropped and "test_test_" not in dropped
    assert drop_file_block(out, "tests/nope.py") == out
    # the wiring: the names gate refuses it, and the §17.1801 ask names both paths and drops the near miss
    import inspect

    from app.modules import execution_agent
    assert "misnamed_required_tests(output" in inspect.getsource(es.engine_findings)
    src = inspect.getsource(execution_agent)
    assert "_mis = _es_t.misnamed_test_file(output" in src and "output = drop_file_block(output, _mis)" in src


@pytest.mark.asyncio
async def test_the_executor_asks_for_the_missing_tests_and_checks_the_whole():
    from tests.test_codegen_for_the_engine_reads_the_engine import LIVE_BRIEF, _run
    checked = AsyncMock(return_value=SimpleNamespace(verdict="pass", reason="", ran=[]))
    with patch("app.sandbox.engine_tests.check", new=checked), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(MODULE_ONLY, LIVE_BRIEF, outputs=[MODULE_ONLY, TESTS_ONLY],
                                    node_extra={"prompt_template": ENTRY_STEP})
    asks = [c for c in captured["calls"] if "Your answer left out `tests/test_paper_entry_execution.py`" in
            c[-1]["content"]]
    assert len(asks) == 1 and asks[0][-2] == {"role": "assistant", "content": MODULE_ONLY}
    checked.assert_awaited()
    assert "# tests/test_paper_entry_execution.py" in checked.await_args.args[0]      # the sandbox runs both


@pytest.mark.asyncio
async def test_a_complete_output_is_not_asked_again():
    from tests.test_codegen_for_the_engine_reads_the_engine import LIVE_BRIEF, _run
    with patch("app.sandbox.engine_tests.check",
               new=AsyncMock(return_value=SimpleNamespace(verdict="pass", reason="", ran=[]))), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(MODULE_ONLY + "\n\n" + TESTS_ONLY, LIVE_BRIEF,
                                    node_extra={"prompt_template": ENTRY_STEP})
    assert not [c for c in captured["calls"] if "Your answer left out" in c[-1]["content"]]


# ── §17.1803 a data class is its constructor; a property is read-only (live job f00ea97f T25E3) ──

T6_STATE = {"node_key": "T6", "prompt_template": "", "output_text": """```python
# app/modules/paper_risk_control_module.py
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(frozen=True)
class RiskState:
    LIMIT: ClassVar[int] = 3
    equity: float
    day_start_equity: float
    now: float = field(default=0.0)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity
```"""}


def test_a_data_class_renders_as_its_constructor_with_properties_read_only():
    r = es._render_job_modules(es.sibling_definitions([T6_STATE]), "other")
    assert "class RiskState(equity: float, day_start_equity: float, now: float = …) {read-only: daily_pnl}" in r


def test_a_data_class_built_with_a_property_or_without_a_field_is_refused():
    defs = es.sibling_definitions([T6_STATE])
    head = "```python\nfrom app.modules.paper_risk_control_module import RiskState\n"
    bad = es.sibling_refs(head + "s = RiskState(equity=1, day_start_equity=1, daily_pnl=0)\n```", defs)
    assert bad and "`RiskState(…)` has no parameter `daily_pnl`" in bad[0]
    missing = es.sibling_refs(head + "s = RiskState(equity=1)\n```", defs)
    assert missing and "missing required argument(s) `day_start_equity`" in missing[0]
    assert es.sibling_refs(head + "s = RiskState(equity=1, day_start_equity=1)\nprint(s.daily_pnl)\n```", defs) == []


# ── §17.1805 a subdomain of a sample domain is a sample (live job f00ea97f T21) ──

def test_a_sample_subdomain_url_is_refused_and_a_real_one_is_not():
    t21 = ('```python\n# app/modules/paper_spy_benchmark_tracker.py\n'
           'SPY_PRICE_URL = "https://api.example.com/v1/spy/history"\n```')
    found = es.placeholder_refs(t21)
    assert found and "`api.example.com` is a sample, not a value" in found[0]
    real = '```python\n# app/modules/paper_spy_benchmark_tracker.py\nDATA_URL = "https://data.alpaca.markets/v2"\n```'
    assert es.placeholder_refs(real) == []
