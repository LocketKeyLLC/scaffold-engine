"""
Integration smoke for ``app.sim.ngspice`` — exercises the real
``scaffold-ngspice`` sidecar against an RC low-pass and asserts the
measured -3 dB corner matches the analytical value 1/(2*pi*R*C) to
within 1%. Also asserts a ``sim_runs`` row was persisted with the
measurements payload intact.

This is the closure for §17.140 invariant "no numeric claim without a
sim_run_id" — if this test passes, the orchestrator can use ngspice as
ground truth for circuit analysis.

Skips cleanly when the sidecar is unreachable (e.g. before
``docker compose up scaffold-ngspice``).
"""
from __future__ import annotations

import math

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.sim.ngspice import run_ngspice
from app.utils import http_clients

R_OHMS = 1_000.0
C_FARADS = 1e-6
ANALYTICAL_FC_HZ = 1.0 / (2.0 * math.pi * R_OHMS * C_FARADS)

RC_LOWPASS_NETLIST = """\
* RC low-pass — §17.140 ground-truth smoke
V1 in 0 AC 1
R1 in out 1k
C1 out 0 1u
.control
ac dec 100 1 100k
meas ac fc_3db when vdb(out)=-3 fall=1
.endc
.end
"""


@pytest_asyncio.fixture
async def ngspice_clients():
    """Initialize the shared httpx clients (orchestrator does this at
    lifespan startup); close them afterwards."""
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


async def _sidecar_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.ngspice_url}/health")
            r.raise_for_status()
            return bool(r.json().get("ok"))
    except Exception:
        return False


@pytest.mark.smoke
async def test_ngspice_rc_lowpass_fc_within_1pct(ngspice_clients):
    if not await _sidecar_reachable():
        pytest.skip(f"scaffold-ngspice sidecar unreachable at {settings.ngspice_url}")

    async with async_session() as db:
        result = await run_ngspice(RC_LOWPASS_NETLIST, db=db)

    assert result.ok, f"ngspice failed: exit={result.exit_code} stderr={result.stderr!r}"
    assert result.sim_run_id is not None
    assert "fc_3db" in result.measurements, (
        f"missing fc_3db; got measurements={result.measurements} "
        f"stdout_tail={result.stdout[-500:]!r}"
    )

    measured = result.measurements["fc_3db"]
    rel_err = abs(measured - ANALYTICAL_FC_HZ) / ANALYTICAL_FC_HZ
    assert rel_err < 0.01, (
        f"fc_3db {measured:.4f} Hz vs analytical {ANALYTICAL_FC_HZ:.4f} Hz "
        f"(rel_err={rel_err:.3%})"
    )

    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT tool, tool_version, exit_code, timed_out,
                       measurements, netlist_sha256
                FROM sim_runs
                WHERE id = :id
                """
            ),
            {"id": str(result.sim_run_id)},
        )
        persisted = row.mappings().one()

    assert persisted["tool"] == "ngspice"
    assert persisted["exit_code"] == 0
    assert persisted["timed_out"] is False
    assert persisted["netlist_sha256"] == result.netlist_sha256
    persisted_fc = float(persisted["measurements"]["fc_3db"])
    assert abs(persisted_fc - measured) < 1e-9

    async with async_session() as db:
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        await db.commit()


@pytest.mark.smoke
async def test_ngspice_sidecar_unreachable_returns_failure_row(ngspice_clients, monkeypatch):
    """Even when the sidecar is down, ``run_ngspice`` must persist an audit
    row and return ``ok=False`` — never raise. The invariant is that no
    verification attempt goes unrecorded."""
    monkeypatch.setattr(settings, "ngspice_url", "http://127.0.0.1:1")
    await http_clients.close_clients()
    http_clients.init_clients()

    async with async_session() as db:
        result = await run_ngspice("V1 1 0 1\n.end\n", db=db)

    assert result.ok is False
    assert result.exit_code == -1
    assert result.sim_run_id is not None
    assert "unreachable" in result.stderr.lower()

    async with async_session() as db:
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        await db.commit()
