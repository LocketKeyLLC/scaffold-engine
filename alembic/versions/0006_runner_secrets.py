"""runner_secrets: named values the engine holds for a machine (§17.1193)

Revision ID: 0006_runner_secrets
Revises: 0005_awaiting_decision_status
Create Date: 2026-09-28

§17.1191 refused to hold a password at all and asked the operator to write it
into a file on the target by hand. That is stricter than every tool this
engine is measured against — Ansible Vault, GitHub Actions secrets, Jenkins
credentials and Vault all STORE the value centrally, encrypted, and inject it
at run time — and it pushed manual file editing onto the operator for
something a form should do.

The security property §17.1191 actually bought is separable from storage: the
value must never enter the command string, the approval, the log line or the
process table. It is delivered to the runner OUT OF BAND and injected as an
environment variable, exactly as Actions does. So the engine may hold it.

Encrypted at rest with the same Fernet derivation as provider_connections
(app/utils/secrets.py). The plaintext is never selected back into any API
response: `value_enc` leaves this table only on the path to a runner.
"""
from __future__ import annotations

from alembic import op

revision = "0006_runner_secrets"
down_revision = "0005_awaiting_decision_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS runner_secrets (
            name         TEXT PRIMARY KEY
                         CHECK (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),
            value_enc    TEXT NOT NULL,
            runner       TEXT,
            hint         TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            owner        TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_runner_secrets_runner ON runner_secrets (runner)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS runner_secrets")
