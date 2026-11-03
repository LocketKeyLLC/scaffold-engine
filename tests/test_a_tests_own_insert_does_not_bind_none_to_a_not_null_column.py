"""§17.1895 — a test's own INSERT that binds None to a column the database refuses: None outright, or a helper
parameter defaulting to None that a caller leaves out (`metrics_json = json.dumps(metrics) if metrics is not None
else None` into `CAST(:metrics AS json)`). A column with a server default is still NOT NULL when the INSERT names it
and binds NULL -- the default applies only when the column is left out -- so the schema rules now carry `not_null`
beside `required`. A parameter the helper rebinds before the INSERT (`external_id = external_id or uuid4().hex`) is
not None by default.

Live (follow-up 5b4880c0 T5 a4, 2026-11-03 21:53 UTC): "null value in column \"metrics\" of relation \"risk_check\"
violates not-null constraint" -- four of nine failures through `_insert_risk_check`, which every chain test calls
through `_create_chain`, with nothing said before the sandbox."""
from __future__ import annotations

from app.modules import engine_schema
from app.modules.engine_contracts import null_inserts

PATH = "tests/test_paper_twelve_modules.py"

MIGRATION = '''# alembic/versions/0010_paper_audit_trail.py
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.create_table(
        "ingested_item",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
    )
    op.create_table(
        "risk_check",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("proposal_id", sa.Integer, sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("check_type", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metrics", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
    )
'''


def _rules():
    return engine_schema.column_rules_in_code([f"```python\n{MIGRATION}```"])


def test_a_not_null_column_with_a_default_is_recorded_beside_the_required_ones():
    rules = _rules()
    assert rules["risk_check"]["required"] == ["proposal_id", "check_type"]
    assert rules["risk_check"]["not_null"] == ["proposal_id", "check_type", "metrics"]
    assert rules["ingested_item"]["not_null"] == ["source", "external_id", "payload"]


def test_a_helper_parameter_left_out_binds_none_and_a_rebound_one_does_not():
    test = '''# tests/test_paper_twelve_modules.py
import json
from uuid import uuid4
from sqlalchemy import text

async def _insert_ingested_item(db, *, source="sec_edgar", external_id=None, payload=None, note=None):
    external_id = external_id or uuid4().hex
    result = await db.execute(
        text("INSERT INTO ingested_item (source, external_id, payload, note) "
             "VALUES (:source, :external_id, CAST(:payload AS json), :note) RETURNING id"),
        {"source": source, "external_id": external_id, "payload": json.dumps(payload) if payload is not None else None,
         "note": note})
    return result.scalar_one()

async def _insert_risk_check(db, *, proposal_id, check_type="allow_list", reason=None, metrics=None):
    metrics_json = json.dumps(metrics) if metrics is not None else None
    result = await db.execute(
        text("INSERT INTO risk_check (proposal_id, check_type, reason, metrics) "
             "VALUES (:proposal_id, :check_type, :reason, CAST(:metrics AS json)) RETURNING id"),
        {"proposal_id": proposal_id, "check_type": check_type, "reason": reason, "metrics": metrics_json})
    return result.scalar_one()

async def _create_chain(db):
    item_id = await _insert_ingested_item(db, source="sec_edgar")
    check_id = await _insert_risk_check(db, proposal_id=1)
    ok = await _insert_risk_check(db, proposal_id=1, metrics={"a": 1})
    return item_id

async def test_direct(db):
    await db.execute(text("INSERT INTO risk_check (proposal_id, check_type, metrics) VALUES (:p, :c, :m)"),
                     {"p": 1, "c": "allow_list", "m": None})
'''
    got = null_inserts({PATH: test}, {}, _rules())
    assert got == [
        "`tests/test_paper_twelve_modules.py:24` calls `_insert_ingested_item(…)` without `payload`, which its INSERT "
        "writes to `ingested_item.payload`, a column that has no default: the database refuses the row -- pass "
        "`payload=…`, or give `payload` a real default in `_insert_ingested_item`",
        "`tests/test_paper_twelve_modules.py:25` calls `_insert_risk_check(…)` without `metrics`, which its INSERT "
        "writes to `risk_check.metrics`, a column that is NOT NULL (its default applies only when the INSERT leaves "
        "the column out): the database refuses the row -- pass `metrics=…`, or give `metrics` a real default in "
        "`_insert_risk_check`",
        "`tests/test_paper_twelve_modules.py:31` binds None to `risk_check.metrics` in its INSERT, a column that is "
        "NOT NULL (its default applies only when the INSERT leaves the column out): the database refuses the row -- "
        "give it a value"], got
    # `external_id` is rebound before the INSERT; `note` is nullable; the call with `metrics=` is right
    assert null_inserts({"app/modules/x.py": test}, {}, _rules()) == []
    assert null_inserts({PATH: test}, {}, {}) == []
