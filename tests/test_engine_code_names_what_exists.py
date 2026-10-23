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
    assert "`frame_decision(…)`: `upstream` is `str`; it was given a `dict`" in found
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
    assert "`alpaca_stream` (step T10) has no `get_open_orders` (it has MAX_BACKOFF_SECONDS, start_stream)" in text
    assert "has no `OrderState`" in text
    assert "has no parameter `runner_id`" in text and "step T16" in text
    assert "start_stream`" not in text.replace("(it has MAX_BACKOFF_SECONDS, start_stream)", "")
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
    assert "`fallback` is `None | str`; it was given a `bool`" in text
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
