"""§17.1765 — the engine-test sandbox: a job's code, tested against the engine it extends.

Live (job 2772acae, read-only review): 24 of ~80 defects in the lab's code were logic that only shows when it runs --
a frozen dataclass mutated, EDGAR filings read from the wrong key (always []), a market order's notional 0 so the 5%
check always passed -- and the integration-test step (T22) failed three times on test-design bugs nobody could see,
because nothing ever ran a test. The coderunner (§17.434) cannot: it has python + pytest and nothing of the engine,
so every `import app.…` is a skip.

This image is the engine's own dev image (its packages, its test tooling, its `app/` at build time). Each /run:
  1. copies the image's `app/` into a fresh temp dir,
  2. writes the job's files over it (relative paths under `app/`, `tests/` or `alembic/`; no traversal),
  3. runs ``command`` (argv, no shell) there with PYTHONPATH at the copy, rlimits and a wall-clock timeout,
  4. deletes the dir.
It sits on the internal `rehearsal` network (no route out) with no database and no secrets: a test that needs a
live service fails, by design. GET /health reports the digest of the `app/` it holds, so the orchestrator can tell an
image built from another engine.

Contract (same shape as the coderunner's):
  POST /run  {files: {path: content}, command: [argv...], timeout_s?, cpu_seconds?, mem_mb?, max_output_bytes?}
             -> {ok, exit_code, stdout, stderr, duration_ms, timed_out, truncated}
  GET  /health -> {ok, engine}
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import resource
import shutil
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("enginetest-sidecar")

ENGINE = Path(os.environ.get("ENGINE_SOURCE", "/code/app"))
ROOTS = ("app", "tests", "alembic")

app = FastAPI(title="scaffold-enginetest")


class RunRequest(BaseModel):
    files: dict[str, str] = Field(..., max_length=200)
    command: list[str] = Field(..., min_length=1, max_length=64)
    timeout_s: float = Field(default=120.0, gt=0.0, le=600.0)
    cpu_seconds: int = Field(default=120, ge=1, le=600)
    mem_mb: int = Field(default=1536, ge=64, le=4096)
    max_output_bytes: int = Field(default=256_000, ge=1_000, le=5_000_000)


class RunResponse(BaseModel):
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


def engine_digest(root: Path) -> str:
    """sha256 over every .py under ``root`` (relative path + bytes): which engine this is."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


@lru_cache(maxsize=1)
def _digest() -> str:
    return engine_digest(ENGINE)


def safe_relpath(name: str) -> Path | None:
    """A relative path under one of ROOTS, with no traversal."""
    if not name or name.startswith(("/", "\\")):
        return None
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] not in ROOTS:
        return None
    return p


def _rlimits(cpu_seconds: int, mem_mb: int):
    def _apply() -> None:
        mem = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        os.setsid()
    return _apply


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "engine": _digest()}


def _fail(msg: str) -> RunResponse:
    return RunResponse(ok=False, exit_code=-1, stdout="", stderr=msg, duration_ms=0, timed_out=False, truncated=False)


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    workdir = Path(tempfile.mkdtemp(prefix="enginetest-"))
    try:
        shutil.copytree(ENGINE, workdir / "app", ignore=shutil.ignore_patterns("__pycache__"))
        (workdir / "tests").mkdir(exist_ok=True)
        for name, content in req.files.items():
            rel = safe_relpath(name)
            if rel is None:
                return _fail(f"invalid path rejected: {name!r} (relative, under {', '.join(ROOTS)})")
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *req.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(workdir),
                preexec_fn=_rlimits(req.cpu_seconds, req.mem_mb),
                env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(workdir),
                     "PYTHONPATH": str(workdir), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                     "HF_HUB_OFFLINE": "1", "SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP": "false", "LOG_FILE": ""},
            )
        except FileNotFoundError as e:
            return _fail(f"command not found: {e}")
        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_s)
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
        cap = req.max_output_bytes
        exit_code = proc.returncode if proc.returncode is not None else -1
        return RunResponse(
            ok=(exit_code == 0 and not timed_out), exit_code=exit_code,
            stdout=out_b[:cap].decode("utf-8", errors="replace"),
            stderr=(err_b[:cap].decode("utf-8", errors="replace") if not timed_out
                    else f"timed out after {req.timeout_s}s"),
            duration_ms=int((time.monotonic() - t0) * 1000), timed_out=timed_out,
            truncated=len(out_b) > cap or len(err_b) > cap)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
