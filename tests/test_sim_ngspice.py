"""Tests for app/sim/ngspice.py — pins the §17.140 audit invariant.

§17.279 closes the §17.273 test-gap: no test exercised the
sidecar-down + timeout scenarios, and the audit invariant ("every
call writes a sim_runs row even on transport failure") was unpinned.
The module's own docstring states it, but a regression that swapped
the early-return order would not have been caught by any existing test.

Covers:
  - Happy path: sidecar 200 + ok=True → result populated + sim_runs row
  - Sidecar unreachable (httpx.ConnectError) → ok=False + sim_runs row STILL written
  - Sidecar HTTP 5xx → ok=False + sim_runs row written
  - Sidecar returns timed_out=True → result.timed_out=True + sim_runs row
  - Sidecar non-zero exit → ok=False + exit_code preserved + sim_runs row
  - Sidecar returns malformed JSON → treated as transport failure + sim_runs row
  - Empty netlist → ValueError BEFORE any sidecar call or sim_runs write
  - netlist_sha256 invariant: same input → same hash; differs from any other input
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.sim.ngspice import NgspiceResult, _sha256, run_ngspice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_db_for_insert():
    """Mock AsyncSession whose .execute() returns a row with a fixed UUID."""
    sim_run_id = uuid.uuid4()
    row = MagicMock()
    row.scalar_one.return_value = sim_run_id
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    return db, sim_run_id


def _mock_httpx_response(body: dict | None = None, status: int = 200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=body or {})
    if status >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _ngspice_client_returning(resp_or_exc):
    """Build an AsyncMock httpx.AsyncClient whose .post() returns the response
    OR raises the given exception."""
    client = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(resp_or_exc, Exception):
        client.post = AsyncMock(side_effect=resp_or_exc)
    else:
        client.post = AsyncMock(return_value=resp_or_exc)
    return client


# ---------------------------------------------------------------------------
# Pure-function: netlist_sha256
# ---------------------------------------------------------------------------

def test_sha256_is_deterministic_and_sensitive():
    """Same input → same hash; one-byte diff → different hash."""
    a = "V1 1 0 5\nR1 1 0 1k\n.end\n"
    b = "V1 1 0 5\nR1 1 0 2k\n.end\n"  # one digit different
    assert _sha256(a) == _sha256(a)
    assert _sha256(a) != _sha256(b)
    # And matches stdlib (regression guard against accidental algorithm swap)
    assert _sha256(a) == hashlib.sha256(a.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Precondition: empty netlist → ValueError BEFORE any work
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\n\n\t  \n"])
@pytest.mark.asyncio
async def test_empty_netlist_raises_value_error_no_sim_run(bad):
    """ValueError fires from the explicit precondition check; sim_runs
    intentionally NOT written because the input is invalid by contract,
    not a runtime failure."""
    db, _ = _mock_db_for_insert()
    with patch("app.sim.ngspice.get_ngspice_client") as gc:
        with pytest.raises(ValueError, match="netlist must be non-empty"):
            await run_ngspice(bad, db=db)
    gc.assert_not_called()
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_writes_sim_run_and_returns_populated_result():
    """Sidecar returns ok=True → NgspiceResult populated + audit row written."""
    db, sim_run_id = _mock_db_for_insert()
    sidecar_body = {
        "ok": True,
        "exit_code": 0,
        "stdout": "Note: ngspice-44 ready\n",
        "stderr": "",
        "measurements": {"fc_3db": 158.78},
        "duration_ms": 42,
        "tool_version": "ngspice-44.2",
        "timed_out": False,
        "seed": 7,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    netlist = "* RC LPF\nR1 in out 1k\nC1 out 0 1u\n.end\n"
    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice(netlist, db=db, seed=7)

    assert isinstance(result, NgspiceResult)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.measurements == {"fc_3db": 158.78}
    assert result.duration_ms == 42
    assert result.tool_version == "ngspice-44.2"
    assert result.timed_out is False
    assert result.seed == 7
    assert result.netlist_sha256 == _sha256(netlist)
    assert result.sim_run_id == sim_run_id

    # Audit invariant: sim_runs INSERT fired (exactly once).
    assert db.execute.await_count == 1
    db.commit.assert_awaited_once()
    insert_params = db.execute.await_args.args[1]
    assert insert_params["tool"] == "ngspice"
    assert insert_params["netlist_sha256"] == _sha256(netlist)
    assert insert_params["exit_code"] == 0
    assert insert_params["timed_out"] is False


# ---------------------------------------------------------------------------
# Failure paths — every one MUST still write the audit row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sidecar_unreachable_writes_sim_run_with_ok_false():
    """httpx.ConnectError → NgspiceResult(ok=False, stderr="...unreachable"),
    sim_runs row STILL written. This is the canonical 'sidecar container
    down' scenario."""
    db, sim_run_id = _mock_db_for_insert()
    client = _ngspice_client_returning(httpx.ConnectError("connection refused"))

    netlist = "V1 1 0 5\n.end\n"
    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice(netlist, db=db)

    # Failure-mode shape
    assert result.ok is False
    assert result.exit_code == -1
    assert "unreachable" in result.stderr
    assert result.measurements == {}
    assert result.tool_version == "unknown"
    assert result.netlist_sha256 == _sha256(netlist)
    assert result.sim_run_id == sim_run_id

    # AUDIT INVARIANT: row still written, even though sidecar was down.
    assert db.execute.await_count == 1, (
        "§17.140 invariant: sim_runs row must be written even when sidecar is unreachable"
    )
    insert_params = db.execute.await_args.args[1]
    assert insert_params["exit_code"] == -1
    assert "unreachable" in insert_params["stderr"]


@pytest.mark.asyncio
async def test_sidecar_http_5xx_writes_sim_run_with_ok_false():
    """HTTP 503 from sidecar → ok=False + audit row written."""
    db, _ = _mock_db_for_insert()
    client = _ngspice_client_returning(_mock_httpx_response(status=503))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice("V1 1 0 5\n.end\n", db=db)

    assert result.ok is False
    assert result.exit_code == -1
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_sidecar_timeout_writes_sim_run_with_timed_out_true():
    """Sidecar returns timed_out=True → result.timed_out=True + audit row.
    NOTE: distinct from httpx-level timeout (which would be a transport error)."""
    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": "timeout after 60s",
        "measurements": {},
        "duration_ms": 60000,
        "tool_version": "ngspice-44.2",
        "timed_out": True,
        "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice("V1 1 0 5\n.end\n", db=db)

    assert result.ok is False
    assert result.timed_out is True
    assert result.duration_ms == 60000
    assert db.execute.await_count == 1
    insert_params = db.execute.await_args.args[1]
    assert insert_params["timed_out"] is True


@pytest.mark.asyncio
async def test_sidecar_non_zero_exit_writes_sim_run_with_exit_code_preserved():
    """Sidecar runs ngspice but ngspice exits 1 (e.g. netlist syntax error).
    ok=False, exit_code=1 preserved, audit row carries the diagnostic."""
    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": "Error: unrecognized device 'X1'\n",
        "measurements": {},
        "duration_ms": 8,
        "tool_version": "ngspice-44.2",
        "timed_out": False,
        "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice("bogus netlist\n.end\n", db=db)

    assert result.ok is False
    assert result.exit_code == 1
    assert "unrecognized device" in result.stderr
    assert db.execute.await_count == 1
    insert_params = db.execute.await_args.args[1]
    assert insert_params["exit_code"] == 1
    assert "unrecognized device" in insert_params["stderr"]


@pytest.mark.asyncio
async def test_sidecar_returns_malformed_json_writes_sim_run_with_ok_false():
    """resp.json() raises ValueError → treated as transport failure
    (same as sidecar-unreachable). Audit row still written."""
    db, _ = _mock_db_for_insert()
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(side_effect=ValueError("Expecting value: line 1 column 1 (char 0)"))
    client = _ngspice_client_returning(resp)

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        result = await run_ngspice("V1 1 0 5\n.end\n", db=db)

    assert result.ok is False
    assert "unreachable" in result.stderr
    assert db.execute.await_count == 1


# ---------------------------------------------------------------------------
# Job + dag_node ID forwarding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_id_and_dag_node_id_forwarded_to_sim_runs_row():
    """When the caller provides job_id and dag_node_id, those land in
    the sim_runs row so downstream reports can join back."""
    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
        "measurements": {}, "duration_ms": 1, "tool_version": "ngspice-44.2",
        "timed_out": False, "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    job_id = uuid.uuid4()
    dag_node_id = uuid.uuid4()
    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        await run_ngspice(
            "V1 1 0 5\n.end\n",
            db=db,
            job_id=job_id,
            dag_node_id=dag_node_id,
        )

    insert_params = db.execute.await_args.args[1]
    assert insert_params["job_id"] == str(job_id)
    assert insert_params["dag_node_id"] == str(dag_node_id)


@pytest.mark.asyncio
async def test_no_job_or_dag_node_id_writes_nulls():
    """Defaults: job_id/dag_node_id None → NULL in sim_runs row."""
    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
        "measurements": {}, "duration_ms": 1, "tool_version": "ngspice-44.2",
        "timed_out": False, "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        await run_ngspice("V1 1 0 5\n.end\n", db=db)

    insert_params = db.execute.await_args.args[1]
    assert insert_params["job_id"] is None
    assert insert_params["dag_node_id"] is None


# ---------------------------------------------------------------------------
# Default timeout from settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_defaults_to_settings_when_unspecified(monkeypatch):
    """If timeout_s=None, settings.ngspice_run_timeout_s is forwarded to sidecar."""
    from app.config import settings
    monkeypatch.setattr(settings, "ngspice_run_timeout_s", 33.0)

    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
        "measurements": {}, "duration_ms": 1, "tool_version": "ngspice-44.2",
        "timed_out": False, "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        await run_ngspice("V1 1 0 5\n.end\n", db=db)

    # client.post called with timeout_s=33.0 in the JSON body.
    post_kwargs = client.post.await_args.kwargs
    assert post_kwargs["json"]["timeout_s"] == 33.0


@pytest.mark.asyncio
async def test_timeout_explicit_overrides_settings(monkeypatch):
    """Explicit timeout_s wins over settings.ngspice_run_timeout_s."""
    from app.config import settings
    monkeypatch.setattr(settings, "ngspice_run_timeout_s", 33.0)

    db, _ = _mock_db_for_insert()
    sidecar_body = {
        "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
        "measurements": {}, "duration_ms": 1, "tool_version": "ngspice-44.2",
        "timed_out": False, "seed": None,
    }
    client = _ngspice_client_returning(_mock_httpx_response(sidecar_body))

    with patch("app.sim.ngspice.get_ngspice_client", return_value=client):
        await run_ngspice("V1 1 0 5\n.end\n", db=db, timeout_s=120.0)

    post_kwargs = client.post.await_args.kwargs
    assert post_kwargs["json"]["timeout_s"] == 120.0
