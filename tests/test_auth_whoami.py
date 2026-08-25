"""§17.815 — GET /auth/whoami + server-derived node-edit attribution (plan 5.3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.authz import ADMIN_PRINCIPAL, Principal, ROLE_USER
from app.routers import auth_info, nodes


# ── /auth/whoami ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whoami_reports_scoped_identity():
    p = Principal(identity="alice", role=ROLE_USER, key_id=7)
    out = await auth_info.whoami(principal=p)
    assert out["identity"] == "alice"
    assert out["role"] == ROLE_USER
    assert out["is_admin"] is False
    assert out["key_id"] == 7
    assert isinstance(out["multi_user"], bool)


@pytest.mark.asyncio
async def test_whoami_admin_default():
    out = await auth_info.whoami(principal=ADMIN_PRINCIPAL)
    assert out["is_admin"] is True
    assert out["key_id"] is None


# ── node-edit attribution ────────────────────────────────────────────────────


def test_attributed_nonadmin_forces_real_identity():
    """A scoped key can't stamp edits as someone else (§17.810 audit trail)."""
    p = Principal(identity="alice", role=ROLE_USER, key_id=7)
    assert nodes._attributed(p, "operator") == "alice"
    assert nodes._attributed(p, None) == "alice"


def test_attributed_admin_keeps_surface_label():
    """Single-user installs keep 'web'/'cli'/'operator' provenance labels."""
    assert nodes._attributed(ADMIN_PRINCIPAL, "web") == "web"
    assert nodes._attributed(ADMIN_PRINCIPAL, "  ") == ADMIN_PRINCIPAL.identity
    assert nodes._attributed(ADMIN_PRINCIPAL, None) == ADMIN_PRINCIPAL.identity


@pytest.mark.asyncio
async def test_node_delete_threads_attribution():
    """The endpoint passes the ATTRIBUTED name to the editor, not the raw
    client value."""
    p = Principal(identity="alice", role=ROLE_USER, key_id=7)
    ed = AsyncMock(return_value={"ok": True})
    with patch.object(nodes.node_editor, "delete_node", new=ed):
        await nodes.node_delete(
            "91a94870-f38c-48e3-877a-225766039969", "T1",
            edited_by="operator", db=AsyncMock(), principal=p)
    assert ed.call_args.kwargs["edited_by"] == "alice"
