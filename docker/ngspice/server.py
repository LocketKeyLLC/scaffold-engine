"""
ngspice sidecar — HTTP surface over the ngspice CLI.

Runs in an isolated container (`scaffold-ngspice`) so untrusted SPICE
input never executes inside the orchestrator's process tree. The
orchestrator talks to this service over the ai-network bridge via the
client in app/sim/ngspice.py.

Contract:
  POST /run   {netlist, timeout_s, seed?} -> {ok, exit_code, stdout,
                                              stderr, measurements,
                                              duration_ms, tool_version,
                                              timed_out}
  GET  /health                            -> {ok, tool_version}

Determinism:
  - ngspice batch mode (-b) is deterministic for non-MC analyses.
  - `seed` is echoed in the response for audit/joining with sim_runs
    rows; it is NOT injected into the netlist. Callers that need MC
    reproducibility must place ``.options seed=N`` in their own netlist.
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
logger = logging.getLogger("ngspice-sidecar")

NGSPICE_BIN = "ngspice"

# Parses lines like:
#   fc_3db              =  1.587775e+02
#   gain_dc             =  1.000000e+00 FROM=  0.000000e+00 TO=  1.000000e-03
#   t_settle            =  4.621e-04 targ=  4.621e-04 trig=  0.000000e+00
# The trailing group is REQUIRED so noise lines like ``Stack = 0 bytes.``,
# ``Maximum ngspice program size =   21.777 MB.``, or
# ``Total elapsed time (seconds) = 0.004`` are rejected — they all carry
# a unit suffix that isn't one of ngspice's measurement keywords.
_MEAS_SUFFIXES = (
    r"targ|trig|TARG|TRIG|FROM|TO|from|to|fall|rise|cross|FALL|RISE|CROSS|AT|at"
)
_MEASURE_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
    rf"(?:\s*$|\s+(?:{_MEAS_SUFFIXES})\b)"
)

# Stop measurement parsing once ngspice's batch-mode stats footer starts;
# the footer carries lines that vaguely look like KPIs (Stack, Library, etc.)
_FOOTER_MARKERS = (
    "Total analysis time",
    "Total elapsed time",
    "Total DRAM",
    "Maximum ngspice program",
    "Current ngspice program",
    "Shared ngspice pages",
    "Text (code) pages",
    "Stack",
    "Library pages",
)

_MEASURE_SKIP = ("Note:", "Warning:", "Error:", "Circuit:")


class RunRequest(BaseModel):
    netlist: str = Field(..., min_length=1, max_length=2_000_000)
    timeout_s: float = Field(default=30.0, gt=0.0, le=600.0)
    seed: int | None = Field(default=None)


class RunResponse(BaseModel):
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    measurements: dict[str, float]
    duration_ms: int
    tool_version: str
    timed_out: bool
    seed: int | None


app = FastAPI(title="scaffold-ngspice")


_VERSION_RE = re.compile(r"ngspice[- ](\d+(?:\.\d+){0,3})", re.IGNORECASE)


def _detect_ngspice_version() -> str:
    """Capture once at startup; ngspice doesn't change under our feet.

    Output across packaged ngspice versions varies — Debian's 44.x prints
    a starred banner ``** ngspice-44.2 : Circuit level simulation
    program``, so we extract by regex over the full text rather than
    line-by-line startswith.
    """
    try:
        out = subprocess.run(
            [NGSPICE_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        m = _VERSION_RE.search(out.stdout + "\n" + out.stderr)
        if m:
            return f"ngspice-{m.group(1)}"
        return "unknown"
    except Exception as exc:
        logger.warning("ngspice version probe failed: %s", exc)
        return "unknown"


_TOOL_VERSION: str = _detect_ngspice_version()


def _parse_measurements(stdout: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in stdout.splitlines():
        stripped = raw.lstrip()
        if any(stripped.startswith(p) for p in _FOOTER_MARKERS):
            break
        if any(stripped.startswith(p) for p in _MEASURE_SKIP):
            continue
        m = _MEASURE_LINE.match(raw)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


async def _run_ngspice(netlist: str, timeout_s: float) -> tuple[int, str, str, bool]:
    """Execute ngspice -b on the supplied netlist. Returns (exit, stdout, stderr, timed_out)."""
    workdir = Path(tempfile.mkdtemp(prefix="ngspice-"))
    netlist_path = workdir / "netlist.cir"
    netlist_path.write_text(netlist, encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            NGSPICE_BIN, "-b", str(netlist_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
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
            return (-1, "", f"ngspice timed out after {timeout_s}s", True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": _TOOL_VERSION != "unknown", "tool_version": _TOOL_VERSION}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    t0 = time.monotonic()
    exit_code, stdout, stderr, timed_out = await _run_ngspice(
        req.netlist, req.timeout_s
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    measurements = _parse_measurements(stdout) if not timed_out else {}
    return RunResponse(
        ok=(exit_code == 0 and not timed_out),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        measurements=measurements,
        duration_ms=duration_ms,
        tool_version=_TOOL_VERSION,
        timed_out=timed_out,
        seed=req.seed,
    )
