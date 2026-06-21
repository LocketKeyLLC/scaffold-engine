"""§17.573 — quality_rollup helper + GET /observability/quality endpoint.

Aggregates dag_nodes (per tool/node_type) + jobs.metadata.grounding into a
tuning view. Two queries (node + grounding) → SQL-discriminating mock db.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.modules.observability_rollups import quality_rollup


def _mock_db(node_rows: list[dict], grounding_row: dict | None):
    async def _exec(sql, params=None):
        s = str(sql)
        result = MagicMock()
        m = MagicMock()
        if "FROM dag_nodes" in s:
            m.all.return_value = node_rows
        else:  # grounding distribution
            m.first.return_value = grounding_row
        result.mappings.return_value = m
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_exec)
    return db


@pytest.mark.smoke
async def test_quality_rollup_aggregates():
    db = _mock_db(
        [{"tool": "CodeGen", "node_type": "task", "total": 10, "done": 8,
          "failed": 2, "skipped": 0, "avg_confidence": 0.82, "avg_retry_count": 0.5}],
        {"jobs_scored": 5, "avg_score": 0.84, "min_score": 0.6,
         "corrected": 2, "below_threshold": 1},
    )
    out = await quality_rollup(window_minutes=1440, db=db)
    assert out["data_source"] == "ok"
    nt = out["by_node_type"][0]
    assert nt["pass_rate"] == 0.8                 # 8 / (8+2)
    assert nt["avg_confidence"] == 0.82
    g = out["grounding"]
    assert g["jobs_scored"] == 5 and g["corrected"] == 2 and g["below_threshold"] == 1
    assert g["threshold"] == 0.7


@pytest.mark.smoke
async def test_quality_rollup_pass_rate_none_when_undecided():
    # all skipped → no done/failed → pass_rate None (not a div-by-zero)
    db = _mock_db(
        [{"tool": "Shell", "node_type": "task", "total": 3, "done": 0,
          "failed": 0, "skipped": 3, "avg_confidence": 0.0, "avg_retry_count": 0.0}],
        {"jobs_scored": 0, "avg_score": 0.0, "min_score": 0.0,
         "corrected": 0, "below_threshold": 0},
    )
    out = await quality_rollup(window_minutes=60, db=db)
    assert out["by_node_type"][0]["pass_rate"] is None


@pytest.mark.smoke
async def test_quality_rollup_fail_open():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    out = await quality_rollup(window_minutes=60, db=db)
    assert out["data_source"] == "error"
    assert out["by_node_type"] == []
    assert out["grounding"]["jobs_scored"] == 0


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_quality_endpoint_200(client):
    payload = {"window_minutes": 60, "by_node_type": [], "grounding": {}, "data_source": "ok"}
    with patch("app.routers.observability.observability_rollups.quality_rollup",
               new=AsyncMock(return_value=payload)):
        r = client.get("/observability/quality?window_minutes=60")
    assert r.status_code == 200
    assert r.json()["data_source"] == "ok"


def test_quality_endpoint_validates_window(client):
    r = client.get("/observability/quality?window_minutes=99999")  # > 10080 max
    assert r.status_code == 422
