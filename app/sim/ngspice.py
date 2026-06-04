"""
ngspice client — talks to the scaffold-ngspice sidecar, persists every
invocation to ``sim_runs``.

Design contract (§17.140):

  * Never raises on simulator failure. Transport, HTTP, timeout, and
    non-zero exit all surface as ``NgspiceResult(ok=False, ...)`` so
    verification loops treat failures as data, not exceptions.
  * Every call writes one row to ``sim_runs`` *before* returning, even
    when the sidecar is unreachable. The audit row is the proof that
    the orchestrator attempted verification; a missing row would let a
    downstream report cite a sim run that never happened.
  * ``netlist_sha256`` is computed over the exact bytes sent to the
    sidecar so an auditor can reproduce the run from the row alone.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.sim._measure import coerce_finite_measurements
from app.utils.http_clients import get_ngspice_client

logger = logging.getLogger("scaffold")

TOOL_NAME = "ngspice"


@dataclass
class NgspiceResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    measurements: dict[str, float] = field(default_factory=dict)
    duration_ms: int = 0
    tool_version: str = "unknown"
    timed_out: bool = False
    seed: int | None = None
    netlist_sha256: str = ""
    sim_run_id: uuid.UUID | None = None


def _sha256(text_in: str) -> str:
    return hashlib.sha256(text_in.encode("utf-8")).hexdigest()


async def _call_sidecar(
    client: httpx.AsyncClient,
    netlist: str,
    timeout_s: float,
    seed: int | None,
) -> dict[str, Any] | None:
    """Returns the sidecar JSON body, or None on transport failure."""
    try:
        resp = await client.post(
            "/run",
            json={"netlist": netlist, "timeout_s": timeout_s, "seed": seed},
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ngspice sidecar call failed: %s", exc)
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
            "measurements": _json_dumps(measurements),
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "job_id": str(job_id) if job_id else None,
            "dag_node_id": str(dag_node_id) if dag_node_id else None,
        },
    )
    sim_run_id = row.scalar_one()
    await db.commit()
    return sim_run_id


def _json_dumps(obj: dict[str, float]) -> str:
    import json
    return json.dumps(obj)


async def run_ngspice(
    netlist: str,
    *,
    db: AsyncSession,
    timeout_s: float | None = None,
    seed: int | None = None,
    job_id: uuid.UUID | None = None,
    dag_node_id: uuid.UUID | None = None,
) -> NgspiceResult:
    """Run ngspice on ``netlist`` via the sidecar; persist the audit row.

    ``timeout_s`` defaults to ``settings.ngspice_run_timeout_s`` and is
    forwarded to the sidecar, which enforces it on the subprocess.
    """
    if not netlist or not netlist.strip():
        raise ValueError("netlist must be non-empty")

    effective_timeout = (
        timeout_s if timeout_s is not None else settings.ngspice_run_timeout_s
    )
    netlist_sha = _sha256(netlist)
    client = get_ngspice_client()
    body = await _call_sidecar(client, netlist, effective_timeout, seed)

    if body is None:
        result = NgspiceResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="ngspice sidecar unreachable",
            measurements={},
            duration_ms=0,
            tool_version="unknown",
            timed_out=False,
            seed=seed,
            netlist_sha256=netlist_sha,
        )
    else:
        result = NgspiceResult(
            ok=bool(body.get("ok", False)),
            exit_code=int(body.get("exit_code", -1)),
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            measurements=coerce_finite_measurements(body.get("measurements")),
            duration_ms=int(body.get("duration_ms", 0)),
            tool_version=str(body.get("tool_version", "unknown")),
            timed_out=bool(body.get("timed_out", False)),
            seed=body.get("seed", seed),
            netlist_sha256=netlist_sha,
        )

    result.sim_run_id = await _insert_sim_run(
        db,
        tool_version=result.tool_version,
        netlist_sha256=result.netlist_sha256,
        seed=result.seed,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        measurements=result.measurements,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        job_id=job_id,
        dag_node_id=dag_node_id,
    )
    return result
