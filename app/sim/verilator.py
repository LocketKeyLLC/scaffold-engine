"""
Verilator client — talks to the scaffold-verilator sidecar, persists
every invocation to ``sim_runs`` with ``tool='verilator'``.

Design contract (§17.141, mirrors §17.140's ngspice contract):

  * Never raises on simulator failure. Transport, HTTP, timeout, build
    failure, and non-zero run exit all surface as
    ``VerilatorResult(ok=False, ...)`` so verification loops treat
    failures as data, not exceptions.
  * Every call writes one row to ``sim_runs`` *before* returning, even
    when the sidecar is unreachable. Missing audit row would let a
    downstream report cite a sim run that never happened.
  * ``netlist_sha256`` is computed over the exact SV bytes sent to the
    sidecar so an auditor can reproduce the run from the row alone.

Build-vs-run distinction: Verilator's pipeline has two phases
(``verilator --binary`` compiles SV + emits + builds C++, then the
generated binary runs). The wrapper exposes both phases on the result
dataclass; the ``sim_runs.stderr`` column captures whichever phase
failed (so an auditor reading the row sees the relevant error first).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.http_clients import get_verilator_client

logger = logging.getLogger("scaffold")

TOOL_NAME = "verilator"


@dataclass
class VerilatorResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    build_stdout: str = ""
    build_stderr: str = ""
    measurements: dict[str, float] = field(default_factory=dict)
    duration_ms: int = 0
    build_duration_ms: int = 0
    tool_version: str = "unknown"
    timed_out: bool = False
    build_failed: bool = False
    seed: int | None = None
    netlist_sha256: str = ""
    sim_run_id: uuid.UUID | None = None


def _sha256(text_in: str) -> str:
    return hashlib.sha256(text_in.encode("utf-8")).hexdigest()


async def _call_sidecar(
    client: httpx.AsyncClient,
    sv_source: str,
    top_module: str,
    run_timeout_s: float,
    build_timeout_s: float,
    seed: int | None,
) -> dict[str, Any] | None:
    try:
        resp = await client.post(
            "/run",
            json={
                "sv_source": sv_source,
                "top_module": top_module,
                "timeout_s": run_timeout_s,
                "build_timeout_s": build_timeout_s,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("verilator sidecar call failed: %s", exc)
        return None


async def _insert_sim_run(
    db: AsyncSession,
    *,
    tool_version: str,
    netlist_sha256: str,
    seed: int | None,
    exit_code: int,
    stdout: str,
    stderr: str,
    measurements: dict[str, float],
    duration_ms: int,
    timed_out: bool,
    job_id: uuid.UUID | None,
    dag_node_id: uuid.UUID | None,
) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            INSERT INTO sim_runs (
                tool, tool_version, netlist_sha256, seed,
                exit_code, stdout, stderr, measurements,
                duration_ms, timed_out, job_id, dag_node_id
            )
            VALUES (
                :tool, :tool_version, :netlist_sha256, :seed,
                :exit_code, :stdout, :stderr, CAST(:measurements AS JSONB),
                :duration_ms, :timed_out, :job_id, :dag_node_id
            )
            RETURNING id
            """
        ),
        {
            "tool": TOOL_NAME,
            "tool_version": tool_version,
            "netlist_sha256": netlist_sha256,
            "seed": seed,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "measurements": json.dumps(measurements),
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "job_id": str(job_id) if job_id else None,
            "dag_node_id": str(dag_node_id) if dag_node_id else None,
        },
    )
    sim_run_id = row.scalar_one()
    await db.commit()
    return sim_run_id


async def run_verilator(
    sv_source: str,
    *,
    top_module: str,
    db: AsyncSession,
    run_timeout_s: float | None = None,
    build_timeout_s: float | None = None,
    seed: int | None = None,
    job_id: uuid.UUID | None = None,
    dag_node_id: uuid.UUID | None = None,
) -> VerilatorResult:
    """Build + run ``sv_source`` via the Verilator sidecar; persist the audit row.

    ``run_timeout_s`` / ``build_timeout_s`` default to settings values
    and are forwarded to the sidecar, which enforces them on the
    respective subprocesses.
    """
    if not sv_source or not sv_source.strip():
        raise ValueError("sv_source must be non-empty")
    if not top_module:
        raise ValueError("top_module must be non-empty")

    run_to = run_timeout_s if run_timeout_s is not None else settings.verilator_run_timeout_s
    build_to = build_timeout_s if build_timeout_s is not None else settings.verilator_build_timeout_s

    sv_sha = _sha256(sv_source)
    client = get_verilator_client()
    body = await _call_sidecar(client, sv_source, top_module, run_to, build_to, seed)

    if body is None:
        result = VerilatorResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="verilator sidecar unreachable",
            measurements={},
            duration_ms=0,
            tool_version="unknown",
            timed_out=False,
            build_failed=False,
            seed=seed,
            netlist_sha256=sv_sha,
        )
    else:
        result = VerilatorResult(
            ok=bool(body.get("ok", False)),
            exit_code=int(body.get("exit_code", -1)),
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            build_stdout=str(body.get("build_stdout", "")),
            build_stderr=str(body.get("build_stderr", "")),
            measurements={
                k: float(v) for k, v in (body.get("measurements") or {}).items()
            },
            duration_ms=int(body.get("duration_ms", 0)),
            build_duration_ms=int(body.get("build_duration_ms", 0)),
            tool_version=str(body.get("tool_version", "unknown")),
            timed_out=bool(body.get("timed_out", False)),
            build_failed=bool(body.get("build_failed", False)),
            seed=body.get("seed", seed),
            netlist_sha256=sv_sha,
        )

    # On build failure, surface the build stderr in the audit row's
    # ``stderr`` column — an auditor opening the row should see the
    # phase that actually broke without having to dig into build_stderr.
    audit_stderr = result.stderr
    if result.build_failed and result.build_stderr:
        audit_stderr = f"BUILD FAILED:\n{result.build_stderr}"

    result.sim_run_id = await _insert_sim_run(
        db,
        tool_version=result.tool_version,
        netlist_sha256=result.netlist_sha256,
        seed=result.seed,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=audit_stderr,
        measurements=result.measurements,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        job_id=job_id,
        dag_node_id=dag_node_id,
    )
    return result
