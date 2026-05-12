"""
SymbiYosys sidecar — HTTP surface over the sby CLI.

Third oracle on the engineering-design track. Unlike ngspice (numeric
.meas results) and verilator (KPI $display lines), sby's primary
output is a categorical verdict — PASS / FAIL / UNKNOWN / ERROR —
emitted both as exit code and as a summary line.

Contract:
  POST /run   {sv_source, top_module, mode?, depth?, engine?,
               timeout_s?, seed?}
              -> {ok, verdict, exit_code, stdout, stderr,
                  duration_ms, tool_version, timed_out, seed,
                  depth_reached, counterexample_vcd_b64}
  GET  /health                -> {ok, tool_version}

Pipeline per /run call:
  1. Write user-supplied SystemVerilog to ``design.sv`` in a fresh
     temp dir.
  2. Synthesize a small ``config.sby`` referencing it. The config is
     parameterized on mode (bmc / prove / cover), depth, engine,
     top_module, and the design file.
  3. ``sby -f -d work config.sby`` — ``-f`` overwrites any prior work
     dir; ``-d work`` pins the work-dir name so we know where to find
     counterexample traces. sby drives yosys + the chosen solver.
  4. Map sby's exit code to a verdict. (sby exit codes per upstream:
     0=PASS, 2=FAIL, 4=UNKNOWN, 8=TIMEOUT, 16=ERROR.)
  5. If FAIL, locate the counterexample VCD under work/engine_0/ and
     return its bytes base64-encoded. Caller can decode for display.

Determinism: ``seed`` is forwarded to the .sby config and echoed in
the response so audit rows can be reproduced.
"""
from __future__ import annotations

import asyncio
import base64
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
logger = logging.getLogger("symbiyosys-sidecar")

SBY_BIN = "sby"

# sby exit-code → verdict mapping. Reference: sby's `main.py` documents
# these as the only public exit codes; everything else (e.g. argparse
# error) bubbles up as 1 and we treat that as ERROR.
_VERDICT_BY_EXIT = {
    0:  "PASS",
    2:  "FAIL",
    4:  "UNKNOWN",
    8:  "TIMEOUT",
    16: "ERROR",
}

# Fallback regex over sby's stdout summary line:
#   "SBY 12:34:56 [work] DONE (PASS, rc=0)"
_VERDICT_LINE = re.compile(
    r"\bDONE\s*\(\s*(PASS|FAIL|UNKNOWN|TIMEOUT|ERROR)\b", re.IGNORECASE
)

# Tool version probe. sby --version emits e.g. "sby 0.40+85 (Yosys 0.50+91)".
_VERSION_RE = re.compile(r"\bsby\s+([\w.+-]+)", re.IGNORECASE)


class RunRequest(BaseModel):
    sv_source: str = Field(..., min_length=1, max_length=5_000_000)
    top_module: str = Field(..., min_length=1, max_length=128,
                            pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    mode: str = Field(default="bmc", pattern=r"^(bmc|prove|cover|live)$")
    depth: int = Field(default=20, ge=1, le=10_000)
    engine: str = Field(default="smtbmc z3", min_length=1, max_length=128)
    timeout_s: float = Field(default=120.0, gt=0.0, le=3600.0)
    seed: int | None = Field(default=None)


class RunResponse(BaseModel):
    ok: bool
    verdict: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    tool_version: str
    timed_out: bool
    seed: int | None
    depth_reached: int | None
    counterexample_vcd_b64: str | None


app = FastAPI(title="scaffold-symbiyosys")


def _detect_sby_version() -> str:
    try:
        out = subprocess.run(
            [SBY_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        m = _VERSION_RE.search(out.stdout + "\n" + out.stderr)
        if m:
            return f"sby-{m.group(1)}"
        return "unknown"
    except Exception as exc:
        logger.warning("sby version probe failed: %s", exc)
        return "unknown"


_TOOL_VERSION: str = _detect_sby_version()


def _build_sby_config(req: RunRequest) -> str:
    """Synthesize the .sby driver config from the request."""
    seed_line = f"seed {req.seed}\n" if req.seed is not None else ""
    return (
        "[options]\n"
        f"mode {req.mode}\n"
        f"depth {req.depth}\n"
        f"timeout {int(req.timeout_s)}\n"
        f"{seed_line}"
        "\n"
        "[engines]\n"
        f"{req.engine}\n"
        "\n"
        "[script]\n"
        "read -formal design.sv\n"
        f"prep -top {req.top_module}\n"
        "\n"
        "[files]\n"
        "design.sv\n"
    )


def _parse_verdict(exit_code: int, stdout: str) -> str:
    """Exit code is the authoritative source; the regex is a fallback
    when sby short-circuits before emitting a DONE line (rare, but
    e.g. an internal Python traceback)."""
    if exit_code in _VERDICT_BY_EXIT:
        return _VERDICT_BY_EXIT[exit_code]
    m = _VERDICT_LINE.search(stdout)
    if m:
        return m.group(1).upper()
    return "ERROR"


def _extract_counterexample(workdir: Path) -> str | None:
    """Locate the trace VCD under work/engine_0/ if sby produced one."""
    for engine_dir in sorted(workdir.glob("engine_*")):
        for vcd in sorted(engine_dir.glob("*.vcd")):
            try:
                return base64.b64encode(vcd.read_bytes()).decode("ascii")
            except OSError:
                continue
    return None


def _extract_depth_reached(stdout: str) -> int | None:
    """sby/yosys reports BMC progress as ``Reached k=N`` or
    ``Reached step N``. Capture the largest integer in any such line."""
    pattern = re.compile(r"\b(?:step|k)\s*=?\s*(\d+)\b")
    best: int | None = None
    for line in stdout.splitlines():
        if "Reached" not in line and "step" not in line.lower():
            continue
        m = pattern.search(line)
        if not m:
            continue
        n = int(m.group(1))
        if best is None or n > best:
            best = n
    return best


async def _run_sby(workdir: Path, timeout_s: float) -> tuple[int, str, str, bool]:
    """Execute sby with the given workdir. Returns (exit, stdout, stderr, timed_out)."""
    proc = await asyncio.create_subprocess_exec(
        SBY_BIN, "-f", "-d", "work", "config.sby",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workdir),
    )
    try:
        # +5 s grace over the sby-internal timeout so sby gets the
        # chance to emit a TIMEOUT verdict cleanly before we kill it.
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s + 5.0
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
        return (-1, "", f"sby timed out after {timeout_s}s (outer kill)", True)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": _TOOL_VERSION != "unknown", "tool_version": _TOOL_VERSION}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    workdir = Path(tempfile.mkdtemp(prefix="sby-"))
    (workdir / "design.sv").write_text(req.sv_source, encoding="utf-8")
    (workdir / "config.sby").write_text(_build_sby_config(req), encoding="utf-8")

    try:
        t0 = time.monotonic()
        exit_code, stdout, stderr, timed_out = await _run_sby(workdir, req.timeout_s)
        duration_ms = int((time.monotonic() - t0) * 1000)

        verdict = _parse_verdict(exit_code, stdout) if not timed_out else "TIMEOUT"
        depth_reached = _extract_depth_reached(stdout) if not timed_out else None
        cex = None
        if verdict == "FAIL":
            cex = _extract_counterexample(workdir / "work")

        return RunResponse(
            ok=(verdict == "PASS"),
            verdict=verdict,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            tool_version=_TOOL_VERSION,
            timed_out=timed_out,
            seed=req.seed,
            depth_reached=depth_reached,
            counterexample_vcd_b64=cex,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
