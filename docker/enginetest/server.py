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
#: §17.1783 — the sandbox's own scratch Postgres (compose `scaffold-enginetest-db`, internal network, tmpfs); empty = none
DB_DSN = os.environ.get("SANDBOX_DB_DSN", "").rstrip("/")      # postgresql://sandbox:sandbox@scaffold-enginetest-db:5432
_base_lock = asyncio.Lock()
_base: dict[str, Any] = {"ok": None, "why": ""}

app = FastAPI(title="scaffold-enginetest")


class RunRequest(BaseModel):
    files: dict[str, str] = Field(..., max_length=200)
    command: list[str] = Field(..., min_length=1, max_length=64)
    timeout_s: float = Field(default=120.0, gt=0.0, le=600.0)
    cpu_seconds: int = Field(default=120, ge=1, le=600)
    mem_mb: int = Field(default=1536, ge=64, le=4096)
    max_output_bytes: int = Field(default=256_000, ge=1_000, le=5_000_000)
    #: §17.1783 — run against a fresh database: the engine's schema plus the job's own Alembic revisions
    database: bool = False


class RunResponse(BaseModel):
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    database: str = ""            # "" (not asked) | "ok" | "unavailable: …"


def _asyncpg_url(db: str) -> str:
    return DB_DSN.replace("postgresql://", "postgresql+asyncpg://", 1) + "/" + db


async def _admin():
    import asyncpg
    return await asyncpg.connect(DB_DSN + "/postgres")


async def _ensure_base() -> None:
    """Build the template database once per process (docker/enginetest/build_base.py)."""
    async with _base_lock:
        if _base["ok"] is not None:
            return
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "PYTHONPATH": "/code", "LOG_FILE": "",
               "HF_HUB_OFFLINE": "1", "DATABASE_URL": _asyncpg_url("engine_base")}
        proc = await asyncio.create_subprocess_exec(
            "python", "/srv/enginetest/build_base.py", cwd="/code", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            _base.update(ok=False, why="building the template database timed out")
            return
        if proc.returncode == 0:
            _base.update(ok=True, why="")
            logger.info("enginetest_base_ready %s", out.decode(errors="replace").strip()[-200:])
        else:
            _base.update(ok=False, why=(err or out).decode(errors="replace").strip()[-600:])
            logger.error("enginetest_base_failed %s", _base["why"])


async def _fresh_db() -> str:
    name = "t_" + os.urandom(6).hex()
    conn = await _admin()
    try:
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE engine_base')
    finally:
        await conn.close()
    return name


async def _drop_db(name: str) -> None:
    try:
        conn = await _admin()
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()
    except Exception as exc:                               # noqa: BLE001 — a leftover db dies with the tmpfs
        logger.warning("enginetest_drop_failed db=%s err=%r", name, exc)


_UPGRADE = ("from alembic.config import Config\nfrom alembic import command\n"
            "c = Config('alembic.ini'); c.set_main_option('script_location', 'alembic')\n"
            "command.upgrade(c, 'head')\n")


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
    dbname = ""
    db_state = ""
    try:
        shutil.copytree(ENGINE, workdir / "app", ignore=shutil.ignore_patterns("__pycache__"))
        (workdir / "tests").mkdir(exist_ok=True)
        if req.database:                                   # the job's revisions land over the engine's own
            shutil.copytree(ENGINE.parent / "alembic", workdir / "alembic", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(ENGINE.parent / "alembic.ini", workdir / "alembic.ini")
        for name, content in req.files.items():
            rel = safe_relpath(name)
            if rel is None:
                return _fail(f"invalid path rejected: {name!r} (relative, under {', '.join(ROOTS)})")
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(workdir),
               "PYTHONPATH": str(workdir), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "HF_HUB_OFFLINE": "1", "SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP": "false", "LOG_FILE": ""}
        if req.database:
            if not DB_DSN:
                db_state = "unavailable: no sandbox database configured"
            else:
                await _ensure_base()
                if not _base["ok"]:
                    db_state = f"unavailable: {_base['why'][:300]}"
                else:
                    dbname = await _fresh_db()
                    env["DATABASE_URL"] = _asyncpg_url(dbname)
                    up = await asyncio.create_subprocess_exec(
                        "python", "-c", _UPGRADE, cwd=str(workdir), env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        uo, ue = await asyncio.wait_for(up.communicate(), timeout=120)
                    except asyncio.TimeoutError:
                        up.kill()
                        uo, ue = b"", b"timed out"
                    if up.returncode != 0:
                        # the job's own migration does not apply on top of the engine's: that is the finding
                        tail = "\n".join((ue or uo).decode(errors="replace").strip().splitlines()[-25:])
                        return RunResponse(ok=False, exit_code=3, stdout="", database="ok",
                                           stderr="the job's Alembic revision does not apply to the engine's "
                                                  "database:\n" + tail, duration_ms=0, timed_out=False,
                                           truncated=False)
                    db_state = "ok"
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *req.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(workdir),
                preexec_fn=_rlimits(req.cpu_seconds, req.mem_mb), env=env,
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
            truncated=len(out_b) > cap or len(err_b) > cap, database=db_state)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if dbname:
            await _drop_db(dbname)
