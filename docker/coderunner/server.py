"""
Code-runner sidecar — HTTP surface that executes UNTRUSTED, LLM-generated
code and its tests, returning a ground-truth pass/fail + output.

Runs in an isolated container (``scaffold-coderunner``) so generated code
never executes inside the orchestrator's process tree — the software-path
analog of the ngspice/Verilator/symbiyosys oracles for circuits (§17.140-142).
The orchestrator talks to this over the bridge via app/sandbox/client.py.

Contract:
  POST /run  {files: {name: content}, command: [argv...], timeout_s?,
              cpu_seconds?, mem_mb?, max_output_bytes?}
             -> {ok, exit_code, stdout, stderr, duration_ms, timed_out,
                 truncated}
  GET  /health  -> {ok}

Each /run:
  1. Validates every filename (no absolute paths, no ``..`` traversal).
  2. Writes the files into a fresh per-call temp dir.
  3. Runs ``command`` (argv list — NO shell, so the untrusted file contents
     can't inject into the command) in that dir with: a wall-clock timeout,
     per-process rlimits (CPU seconds, address space, process count, file
     size), and output capped to ``max_output_bytes``.
  4. Deletes the temp dir.

Isolation note: container-level hardening (cap_drop ALL, no-new-privileges,
read_only rootfs, noexec/exec tmpfs, mem/pids limits, NO published egress)
lives in docker-compose.yml; the strong kernel-isolation upgrade is running
this service under the gVisor ``runsc`` runtime (operator step — see the
§17.433 OVERVIEW entry). The rlimits here are defense-in-depth on top.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coderunner-sidecar")

app = FastAPI(title="scaffold-coderunner")


class RunRequest(BaseModel):
    # filename -> file content. Names are validated (relative, no traversal).
    files: dict[str, str] = Field(..., max_length=200)
    # argv list (no shell). e.g. ["pytest","-q"] or ["python","main.py"].
    command: list[str] = Field(..., min_length=1, max_length=64)
    timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)
    cpu_seconds: int = Field(default=30, ge=1, le=300)
    mem_mb: int = Field(default=512, ge=16, le=2048)
    max_output_bytes: int = Field(default=256_000, ge=1_000, le=5_000_000)


class RunResponse(BaseModel):
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


def _safe_relpath(name: str) -> Path | None:
    """Reject absolute paths and ``..`` traversal; return a safe relative Path."""
    if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
        return None
    p = Path(name)
    if p.is_absolute():
        return None
    return p


def _rlimits(cpu_seconds: int, mem_mb: int):
    """preexec_fn: cap CPU seconds, address space, process count, file size."""
    def _apply() -> None:
        mem = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        os.setsid()  # own process group so a timeout kill takes children too
    return _apply


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    workdir = Path(tempfile.mkdtemp(prefix="coderun-"))
    try:
        for name, content in req.files.items():
            rel = _safe_relpath(name)
            if rel is None:
                return RunResponse(
                    ok=False, exit_code=-1, stdout="",
                    stderr=f"invalid filename rejected: {name!r}",
                    duration_ms=0, timed_out=False, truncated=False,
                )
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *req.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                preexec_fn=_rlimits(req.cpu_seconds, req.mem_mb),
                env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                     "HOME": str(workdir), "PYTHONUNBUFFERED": "1"},
            )
        except FileNotFoundError as e:
            return RunResponse(
                ok=False, exit_code=-1, stdout="",
                stderr=f"command not found: {e}",
                duration_ms=0, timed_out=False, truncated=False,
            )

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=req.timeout_s
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            out_b, err_b = b"", b""

        duration_ms = int((time.monotonic() - t0) * 1000)
        cap = req.max_output_bytes
        out = out_b[:cap].decode("utf-8", errors="replace")
        err = err_b[:cap].decode("utf-8", errors="replace")
        truncated = len(out_b) > cap or len(err_b) > cap
        exit_code = (proc.returncode if proc.returncode is not None else -1)

        return RunResponse(
            ok=(exit_code == 0 and not timed_out),
            exit_code=exit_code,
            stdout=out,
            stderr=err if not timed_out else (err or f"timed out after {req.timeout_s}s"),
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=truncated,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
