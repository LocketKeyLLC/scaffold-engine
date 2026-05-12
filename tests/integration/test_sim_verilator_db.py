"""
Integration smoke for ``app.sim.verilator`` — exercises the real
``scaffold-verilator`` sidecar against a self-checking depth-4
synchronous FIFO. The testbench writes four byte values, reads them
back, asserts the FIFO order is preserved, and emits KPI lines that
this test then matches against expectations.

This is the §17.141 closure for "no HDL numeric claim without a
sim_run_id" — if this test passes, the orchestrator can use Verilator
as ground truth for digital-design verification.

Skips cleanly when the sidecar is unreachable (e.g. before
``docker compose up scaffold-verilator``).
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.sim.verilator import run_verilator
from app.utils import http_clients

FIFO_DEPTH4_SV = r"""
module fifo #(parameter DEPTH = 4, parameter WIDTH = 8) (
    input  logic clk,
    input  logic rst_n,
    input  logic wr_en,
    input  logic [WIDTH-1:0] din,
    input  logic rd_en,
    output logic [WIDTH-1:0] dout,
    output logic full,
    output logic empty
);
    localparam ADDR_W = $clog2(DEPTH);

    logic [WIDTH-1:0] mem [DEPTH-1:0];
    logic [ADDR_W:0]  wr_ptr;
    logic [ADDR_W:0]  rd_ptr;

    assign empty = (wr_ptr == rd_ptr);
    assign full  = (wr_ptr[ADDR_W-1:0] == rd_ptr[ADDR_W-1:0])
                && (wr_ptr[ADDR_W]     != rd_ptr[ADDR_W]);
    assign dout = mem[rd_ptr[ADDR_W-1:0]];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
            rd_ptr <= '0;
        end else begin
            if (wr_en && !full) begin
                mem[wr_ptr[ADDR_W-1:0]] <= din;
                wr_ptr <= wr_ptr + 1;
            end
            if (rd_en && !empty) begin
                rd_ptr <= rd_ptr + 1;
            end
        end
    end
endmodule

module tb;
    logic clk = 0;
    logic rst_n = 0;
    logic wr_en = 0;
    logic rd_en = 0;
    logic [7:0] din = 0;
    logic [7:0] dout;
    logic full, empty;
    int errors = 0;
    int writes = 0;
    int reads = 0;

    fifo #(.DEPTH(4), .WIDTH(8)) dut (
        .clk(clk), .rst_n(rst_n),
        .wr_en(wr_en), .din(din),
        .rd_en(rd_en), .dout(dout),
        .full(full), .empty(empty)
    );

    always #5 clk = ~clk;

    initial begin
        rst_n = 0;
        #20 rst_n = 1;

        // All testbench stimulus is driven at NEGEDGE so the FIFO's
        // always_ff samples stable inputs on the following POSEDGE.
        // Driving at posedge races the DUT — depending on event order,
        // the testbench may update wr_en/din before always_ff samples,
        // making the FIFO see the NEXT iteration's value instead of
        // the current one.
        @(negedge clk);
        if (!empty) begin $display("FAIL: empty=0 after reset"); errors++; end
        if (full)   begin $display("FAIL: full=1 after reset");  errors++; end

        // Writes: drive at negedge, FIFO samples at the following posedge.
        for (int i = 0; i < 4; i++) begin
            wr_en = 1;
            din = 8'(8'hA0 + i[7:0]);
            @(negedge clk);
            writes++;
        end
        wr_en = 0;

        if (!full) begin $display("FAIL: full=0 after 4 writes"); errors++; end

        // Reads: at each negedge, dout is the combinational read of the
        // current rd_ptr (which reflects the previous posedge's update).
        // Assert dout, set rd_en for the next posedge to advance.
        for (int i = 0; i < 4; i++) begin
            logic [7:0] expected;
            expected = 8'(8'hA0 + i[7:0]);
            if (dout !== expected) begin
                $display("FAIL: read[%0d] expected %0h got %0h", i, expected, dout);
                errors++;
            end
            rd_en = 1;
            @(negedge clk);
            reads++;
        end
        rd_en = 0;

        if (!empty) begin $display("FAIL: empty=0 after 4 reads"); errors++; end

        $display("KPI writes=%0d", writes);
        $display("KPI reads=%0d", reads);
        $display("KPI errors=%0d", errors);
        if (errors == 0) $display("PASS");
        else             $display("FAIL");
        $finish;
    end

    initial begin
        #10000 $display("WATCHDOG: simulation timeout");
        $finish;
    end
endmodule
"""


@pytest_asyncio.fixture
async def verilator_clients():
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


async def _sidecar_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.verilator_url}/health")
            r.raise_for_status()
            return bool(r.json().get("ok"))
    except Exception:
        return False


@pytest.mark.smoke
async def test_verilator_fifo_depth4_passes(verilator_clients):
    if not await _sidecar_reachable():
        pytest.skip(f"scaffold-verilator sidecar unreachable at {settings.verilator_url}")

    async with async_session() as db:
        result = await run_verilator(
            FIFO_DEPTH4_SV,
            top_module="tb",
            db=db,
            run_timeout_s=30.0,
            build_timeout_s=120.0,
        )

    assert result.ok, (
        f"verilator failed: build_failed={result.build_failed} "
        f"exit={result.exit_code} "
        f"build_stderr_tail={result.build_stderr[-500:]!r} "
        f"stderr={result.stderr!r} "
        f"stdout_tail={result.stdout[-500:]!r}"
    )
    assert result.sim_run_id is not None
    assert result.build_failed is False
    assert result.timed_out is False
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout.splitlines()[-3:][0] or "PASS" in result.stdout

    assert result.measurements.get("errors") == 0.0, result.measurements
    assert result.measurements.get("writes") == 4.0, result.measurements
    assert result.measurements.get("reads") == 4.0, result.measurements

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

    assert persisted["tool"] == "verilator"
    assert persisted["exit_code"] == 0
    assert persisted["timed_out"] is False
    assert persisted["netlist_sha256"] == result.netlist_sha256
    assert float(persisted["measurements"]["errors"]) == 0.0
    assert float(persisted["measurements"]["writes"]) == 4.0
    assert float(persisted["measurements"]["reads"]) == 4.0

    async with async_session() as db:
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": str(result.sim_run_id)},
        )
        await db.commit()


@pytest.mark.smoke
async def test_verilator_sidecar_unreachable_returns_failure_row(verilator_clients, monkeypatch):
    """Even when the sidecar is down, ``run_verilator`` must persist an
    audit row and return ``ok=False`` — never raise."""
    monkeypatch.setattr(settings, "verilator_url", "http://127.0.0.1:2")
    await http_clients.close_clients()
    http_clients.init_clients()

    async with async_session() as db:
        result = await run_verilator(
            "module m; endmodule\n",
            top_module="m",
            db=db,
        )

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
