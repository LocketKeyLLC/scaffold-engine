"""§17.803 — /models/proposals router: delegation + 404 mapping.

The endpoints are thin wrappers over app.modules.model_role_learning; we call
them directly with a mocked module + AsyncMock db so we assert the HTTP-shape
(envelope, 404 on missing/not-open) without booting the full app/TestClient.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers import model_proposals as rt


@pytest.mark.smoke
async def test_list_endpoint_wraps_count():
    db = AsyncMock()
    with patch.object(rt._mrl, "list_open_proposals",
                      AsyncMock(return_value=[{"id": 1}, {"id": 2}])):
        out = await rt.list_proposals_endpoint(db)
    assert out == {"proposals": [{"id": 1}, {"id": 2}], "count": 2}


@pytest.mark.smoke
async def test_accept_endpoint_passthrough():
    db = AsyncMock()
    payload = {"id": 5, "role": "model_coder", "model": "cand:cloud", "applied": True}
    with patch.object(rt._mrl, "accept_proposal", AsyncMock(return_value=payload)):
        out = await rt.accept_proposal_endpoint(5, db)
    assert out == payload


@pytest.mark.smoke
async def test_accept_endpoint_404_when_not_open():
    db = AsyncMock()
    with patch.object(rt._mrl, "accept_proposal", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await rt.accept_proposal_endpoint(5, db)
    assert ei.value.status_code == 404


@pytest.mark.smoke
async def test_dismiss_endpoint_404_when_not_open():
    db = AsyncMock()
    with patch.object(rt._mrl, "dismiss_proposal", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await rt.dismiss_proposal_endpoint(9, db)
    assert ei.value.status_code == 404
