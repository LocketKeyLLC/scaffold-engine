"""
Integration smoke for ``app.sim.symbiyosys`` — exercises the real
``scaffold-symbiyosys`` sidecar against a 2-bit counter with a
trivially-true safety assertion (``count < 4`` over a 2-bit value).
The bmc engine should return PASS within a few cycles.

This is the §17.142 closure for formal-verification ground truth:
``run_symbiyosys`` writes a row with ``tool='symbiyosys'`` AND a
populated ``verdict`` column that downstream reports can join against.

Skips cleanly when the sidecar is unreachable (e.g. before
``docker compose up scaffold-symbiyosys``).
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.sim.symbiyosys import (
    VERDICT_PASS,
    run_symbiyosys,
)
from app.utils import http_clients

COUNTER_BMC_SV = r"""
module counter (
    input  logic clk,
    input  logic rst_n,
    output logic [1:0] count
);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) count <= '0;
        else        count <= count + 1;
    end

`ifdef FORMAL
    // Trivially true: a 2-bit value is always < 4. The bmc engine
    // should confirm this within `depth` cycles.
    always @(posedge clk) begin
        if (rst_n) assert (count < 4);
    end
`endif
endmodule
"""


@pytest_asyncio.fixture
async def symbiyosys_clients():
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


async def _sidecar_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.symbiyosys_url}/health")
            r.raise_for_status()
            return bool(r.json().get("ok"))
    except Exception:
        return False


@pytest.mark.smoke
async def test_symbiyosys_counter_bmc_passes(symbiyosys_clients):
    if not await _sidecar_reachable():
        pytest.skip(f"scaffold-symbiyosys sidecar unreachable at {settings.symbiyosys_url}")

    async with async_session() as db:
        result = await run_symbiyosys(
            COUNTER_BMC_SV,
            top_module="counter",
            db=db,
            mode="bmc",
            depth=10,
            engine="smtbmc z3",
            timeout_s=120.0,
        )

    assert result.verdict == VERDICT_PASS, (
        f"symbiyosys failed: verdict={result.verdict} "
        f"exit={result.exit_code} "
        f"stderr_tail={result.stderr[-500:]!r} "
        f"stdout_tail={result.stdout[-500:]!r}"
    )
    assert result.ok is True
    assert result.sim_run_id is not None
    assert result.timed_out is False
    assert result.exit_code == 0

    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT tool, tool_version, exit_code, timed_out,
                       verdict, netlist_sha256
                FROM sim_runs
                WHERE id = :id
                """
            ),
            {"id": str(result.sim_run_id)},
        )
        persisted = row.mappings().one()

    assert persisted["tool"] == "symbiyosys"
    assert persisted["verdict"] == VERDICT_PASS
    assert persisted["exit_code"] == 0
    assert persisted["timed_out"] is False
    assert persisted["netlist_sha256"] == result.netlist_sha256

    async with async_session() as db:
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        await db.commit()


@pytest.mark.smoke
async def test_symbiyosys_sidecar_unreachable_returns_failure_row(symbiyosys_clients, monkeypatch):
    """Sidecar-down path must still persist an audit row with
    ``verdict='ERROR'`` — never raise."""
    monkeypatch.setattr(settings, "symbiyosys_url", "http://127.0.0.1:3")
    await http_clients.close_clients()
    http_clients.init_clients()

    async with async_session() as db:
        result = await run_symbiyosys(
            "module m; endmodule\n",
            top_module="m",
            db=db,
        )

    assert result.ok is False
    assert result.verdict == "ERROR"
    assert result.exit_code == -1
    assert result.sim_run_id is not None
    assert "unreachable" in result.stderr.lower()

    async with async_session() as db:
        row = await db.execute(
            text("SELECT verdict FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        assert row.scalar_one() == "ERROR"
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        await db.commit()
