# alembic/versions/0010_paper_audit_trail.py
"""paper_audit_trail

Revision ID: 0010_paper_audit_trail
Revises: 0009_runner_secrets_for_operator
Create Date: 2026-10-23 20:20:00.000000+00:00
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010_paper_audit_trail"
down_revision: Union[str, None] = "0009_runner_secrets_for_operator"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("model_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("data_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'append-only table: %', TG_TABLE_NAME;
        END;
        $$;
    """)

    op.create_table(
        "ingested_item",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("published_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("payload", sa.JSON, nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("source", "external_id", name="uq_ingested_item_source_external_id"),
        sa.CheckConstraint(
            "source IN ('sec_edgar','congress','finra','news','reddit')",
            name="ck_ingested_item_source",
        ),
    )

    op.create_table(
        "signal",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ingested_item_id", sa.BigInteger, sa.ForeignKey("ingested_item.id"), nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("signal_type", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "signal_type IN ('form4_purchase','13f_holding','congress_trade','finra_context','news_context','reddit_context')",
            name="ck_signal_signal_type",
        ),
        sa.CheckConstraint("direction IN ('long','flat')", name="ck_signal_direction"),
    )

    op.create_table(
        "proposal",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.BigInteger, sa.ForeignKey("signal.id"), nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("order_type", sa.Text, nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("rationale", sa.Text, nullable=False),
        *_audit_columns(),
        sa.CheckConstraint("side IN ('buy','sell')", name="ck_proposal_side"),
        sa.CheckConstraint("quantity > 0", name="ck_proposal_quantity_positive"),
        sa.CheckConstraint("order_type IN ('market','limit')", name="ck_proposal_order_type"),
    )

    op.create_table(
        "risk_check",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger, sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("check_type", sa.Text, nullable=False),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metrics", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        *_audit_columns(),
    )

    op.create_table(
        "approval",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger, sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("operator_id", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        *_audit_columns(),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_approval_decision"),
    )

    op.create_table(
        "broker_order_id",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("approval_id", sa.BigInteger, sa.ForeignKey("approval.id"), nullable=False, unique=True),
        sa.Column("proposal_id", sa.BigInteger, sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("broker_order_id", sa.Text, nullable=False, unique=True),
        sa.Column("submitted_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        *_audit_columns(),
    )

    op.create_table(
        "fill",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("broker_order_id", sa.BigInteger, sa.ForeignKey("broker_order_id.id"), nullable=False),
        sa.Column("fill_id", sa.Text, nullable=False),
        sa.Column("side", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("commission", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("filled_ts", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
        sa.CheckConstraint("side IN ('buy','sell')", name="ck_fill_side"),
    )

    op.create_table(
        "pnl",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("as_of_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_value", sa.Numeric(18, 2), nullable=False),
        sa.Column("cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("equity", sa.Numeric(18, 2), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("unrealized_pnl", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("daily_return", sa.Numeric(12, 8), nullable=True),
        sa.Column("cumulative_return", sa.Numeric(12, 8), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(12, 8), nullable=True),
        sa.Column("sharpe_ratio", sa.Numeric(12, 8), nullable=True),
        sa.Column("turnover", sa.Numeric(12, 8), nullable=True),
        sa.Column("spy_benchmark_return", sa.Numeric(12, 8), nullable=True),
        *_audit_columns(),
    )

    for table in (
        "ingested_item",
        "signal",
        "proposal",
        "risk_check",
        "approval",
        "broker_order_id",
        "fill",
        "pnl",
    ):
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;")
        op.execute(f"""
            CREATE TRIGGER trg_no_update_delete_{table}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
        """)


def downgrade() -> None:
    for table in (
        "pnl",
        "fill",
        "broker_order_id",
        "approval",
        "risk_check",
        "proposal",
        "signal",
        "ingested_item",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_no_update_delete_{table} ON {table};")
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation();")
