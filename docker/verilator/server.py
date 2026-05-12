"""
Verilator sidecar — HTTP surface over the verilator CLI + the generated
C++ build step.

Runs in an isolated container (``scaffold-verilator``) so untrusted HDL
input never compiles inside the orchestrator's process tree. The
orchestrator talks to this service over the ai-network bridge via the
client in ``app/sim/verilator.py``.

Contract:
  POST /run   {sv_source, top_module, timeout_s?, seed?, build_timeout_s?}
              -> {ok, exit_code, stdout, stderr, build_stdout,
                  build_stderr, measurements, duration_ms,
                  build_duration_ms, tool_version, timed_out, seed,
                  build_failed}
  GET  /health                -> {ok, tool_version}

Pipeline per /run call:
  1. Write the user-supplied SystemVerilog to ``design.sv`` in a fresh
     temp dir.
  2. ``verilator --binary --timing --top-module <top> design.sv`` —
     compiles SV + generates a C++ harness + builds it via Make. The
     ``--binary`` flag tells Verilator to do the full pipeline in one
     invocation; ``--timing`` enables ``#delay`` / ``@event`` constructs
     in initial blocks, which clocked testbenches universally need.
  3. Run ``./obj_dir/V<top>`` with the per-run timeout. stdout/stderr
     captured separately from the build phase.
  4. Parse KPI lines (``KPI <name>=<value>``) from the run's stdout.

KPI protocol: lines emitted via ``$display("KPI errors=%0d", errors);``
become a ``measurements: {errors: 0.0}`` entry in the response. Lines
without the ``KPI`` prefix are ignored — the contract is opt-in so a
test that doesn't want to expose KPIs simply doesn't print them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verilator-sidecar")

VERILATOR_BIN = "verilator"

# Match lines emitted via $display("KPI name=value", ...). The value can
# be int, decimal, scientific, or signed. Anything that fails float()
# parse is dropped, so $display strings that happen to start with "KPI"
# but encode something else don't pollute the measurements dict.
_KPI_LINE = re.compile(
    r"^\s*KPI\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)

# Captures "Verilator 5.024 2024-09-04 rev v5.024" style output.
_VERSION_RE = re.compile(r"Verilator\s+(\d+(?:\.\d+){0,3})", re.IGNORECASE)


class RunRequest(BaseModel):
    sv_source: str = Field(..., min_length=1, max_length=5_000_000)
    top_module: str = Field(..., min_length=1, max_length=128,
                            pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_s: float = Field(default=60.0, gt=0.0, le=1800.0)
    build_timeout_s: float = Field(default=120.0, gt=0.0, le=1800.0)
    seed: int | None = Field(default=None)


class RunResponse(BaseModel):
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    build_stdout: str
    build_stderr: str
    measurements: dict[str, float]
    duration_ms: int
    build_duration_ms: int
    tool_version: str
    timed_out: bool
    build_failed: bool
    seed: int | None


app = FastAPI(title="scaffold-verilator")


def _detect_verilator_version() -> str:
    try:
        out = subprocess.run(
            [VERILATOR_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        m = _VERSION_RE.search(out.stdout + "\n" + out.stderr)
        if m:
            return f"verilator-{m.group(1)}"
        return "unknown"
    except Exception as exc:
        logger.warning("verilator version probe failed: %s", exc)
        return "unknown"


_TOOL_VERSION: str = _detect_verilator_version()


def _parse_kpis(stdout: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in stdout.splitlines():
        m = _KPI_LINE.match(raw)
        if not m:
            continue
        try:
            out[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
    return out


async def _exec(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: float,
) -> tuple[int, str, str, bool]:
    """Run a subprocess with timeout. Returns (exit, stdout, stderr, timed_out)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            False,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        return (-1, "", f"timed out after {timeout_s}s", True)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": _TOOL_VERSION != "unknown", "tool_version": _TOOL_VERSION}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    workdir = Path(tempfile.mkdtemp(prefix="verilator-"))
    design = workdir / "design.sv"
    design.write_text(req.sv_source, encoding="utf-8")

    try:
        # --- Build phase ---
        build_t0 = time.monotonic()
        build_exit, build_stdout, build_stderr, build_timed_out = await _exec(
            [
                VERILATOR_BIN,
                "--binary",
                "--timing",
                "-j", "0",
                "--top-module", req.top_module,
                "design.sv",
            ],
            cwd=workdir,
            timeout_s=req.build_timeout_s,
        )
        build_duration_ms = int((time.monotonic() - build_t0) * 1000)

        if build_exit != 0 or build_timed_out:
            return RunResponse(
                ok=False,
                exit_code=build_exit,
                stdout="",
                stderr="",
                build_stdout=build_stdout,
                build_stderr=build_stderr,
                measurements={},
                duration_ms=0,
                build_duration_ms=build_duration_ms,
                tool_version=_TOOL_VERSION,
                timed_out=build_timed_out,
                build_failed=True,
                seed=req.seed,
            )

        # --- Run phase ---
        sim_bin = workdir / "obj_dir" / f"V{req.top_module}"
        if not sim_bin.is_file():
            return RunResponse(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr=f"expected binary not found: {sim_bin.name}",
                build_stdout=build_stdout,
                build_stderr=build_stderr,
                measurements={},
                duration_ms=0,
                build_duration_ms=build_duration_ms,
                tool_version=_TOOL_VERSION,
                timed_out=False,
                build_failed=True,
                seed=req.seed,
            )

        run_t0 = time.monotonic()
        run_exit, run_stdout, run_stderr, run_timed_out = await _exec(
            [str(sim_bin)],
            cwd=workdir,
            timeout_s=req.timeout_s,
        )
        run_duration_ms = int((time.monotonic() - run_t0) * 1000)

        measurements = _parse_kpis(run_stdout) if not run_timed_out else {}

        return RunResponse(
            ok=(run_exit == 0 and not run_timed_out),
            exit_code=run_exit,
            stdout=run_stdout,
            stderr=run_stderr,
            build_stdout=build_stdout,
            build_stderr=build_stderr,
            measurements=measurements,
            duration_ms=run_duration_ms,
            build_duration_ms=build_duration_ms,
            tool_version=_TOOL_VERSION,
            timed_out=run_timed_out,
            build_failed=False,
            seed=req.seed,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
