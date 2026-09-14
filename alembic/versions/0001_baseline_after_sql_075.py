"""baseline — the schema as of db/migrations/075 (applied by app/migrations.py)

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-09-14

§17.1075 — an empty revision that marks where Alembic takes over. Everything
up to and including db/migrations/075_dag_nodes_evidence.sql is applied by
the custom SQL runner (which stays for that history); every schema change
after this point is an Alembic revision (`make migration m="…"`).
"""
from __future__ import annotations

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
