"""
SymbiYosys client — talks to the scaffold-symbiyosys sidecar, persists
every invocation to ``sim_runs`` with ``tool='symbiyosys'`` AND a
populated ``verdict`` column (PASS / FAIL / UNKNOWN / TIMEOUT / ERROR).

Design contract (§17.142, mirrors §17.140 ngspice + §17.141 verilator):

  * Never raises on simulator failure. Transport, HTTP, timeout, and
    any sby exit surface as ``SymbiYosysResult(ok=False, verdict=…)``.
    ``ok`` is True ONLY when verdict == "PASS".
  * Every call writes one row to ``sim_runs`` *before* returning, even
    when the sidecar is unreachable — the audit row is proof the
    orchestrator attempted verification.
  * ``netlist_sha256`` is computed over the exact SV bytes sent to the
    sidecar so an auditor can reproduce the run from the row alone.
  * Counterexample VCD (if sby produced one on FAIL) comes back
    base64-encoded; the wrapper does not persist it to ``sim_runs``
    in v1 (waveform artifact storage is deferred — see §17.140's
    "out of scope" list).
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
from app.sim._measure import coerce_finite_measurements
from app.utils.http_clients import get_symbiyosys_client

logger = logging.getLogger("scaffold")

TOOL_NAME = "symbiyosys"

# Verdicts that the sidecar may return. Kept here as a constant so
# call sites can pattern-match against the same set.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNKNOWN = "UNKNOWN"
VERDICT_TIMEOUT = "TIMEOUT"
VERDICT_ERROR = "ERROR"
VALID_VERDICTS = frozenset({
    VERDICT_PASS, VERDICT_FAIL, VERDICT_UNKNOWN,
    VERDICT_TIMEOUT, VERDICT_ERROR,
})


@dataclass
class SymbiYosysResult:
    ok: bool
    verdict: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    tool_version: str = "unknown"
    timed_out: bool = False
    seed: int | None = None
    depth_reached: int | None = None
    counterexample_vcd_b64: str | None = None
    netlist_sha256: str = ""
    sim_run_id: uuid.UUID | None = None


def _sha256(text_in: str) -> str:
    return hashlib.sha256(text_in.encode("utf-8")).hexdigest()


async def _call_sidecar(
    client: httpx.AsyncClient,
    sv_source: str,
    top_module: str,
    mode: str,
    depth: int,
    engine: str,
    timeout_s: float,
    seed: int | None,
) -> dict[str, Any] | None:
    try:
        resp = await client.post(
            "/run",
            json={
                "sv_source": sv_source,
                "top_module": top_module,
                "mode": mode,
                "depth": depth,
                "engine": engine,
                "timeout_s": timeout_s,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("symbiyosys sidecar call failed: %s", exc)
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
    verdict: str | None,
    job_id: uuid.UUID | None,
    dag_node_id: uuid.UUID | None,
) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            INSERT INTO sim_runs (
                tool, tool_version, netlist_sha256, seed,
                exit_code, stdout, stderr, measurements,
                duration_ms, timed_out, verdict, job_id, dag_node_id
            )
            VALUES (
                :tool, :tool_version, :netlist_sha256, :seed,
                :exit_code, :stdout, :stderr, CAST(:measurements AS JSONB),
                :duration_ms, :timed_out, :verdict, :job_id, :dag_node_id
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
            "verdict": verdict,
            "job_id": str(job_id) if job_id else None,
            "dag_node_id": str(dag_node_id) if dag_node_id else None,
        },
    )
    sim_run_id = row.scalar_one()
    await db.commit()
    return sim_run_id


async def run_symbiyosys(
    sv_source: str,
    *,
    top_module: str,
    db: AsyncSession,
    mode: str = "bmc",
    depth: int = 20,
    engine: str = "smtbmc z3",
    timeout_s: float | None = None,
    seed: int | None = None,
    job_id: uuid.UUID | None = None,
    dag_node_id: uuid.UUID | None = None,
) -> SymbiYosysResult:
    """Run sby on ``sv_source`` via the sidecar; persist the audit row.

    ``mode`` is one of ``bmc`` / ``prove`` / ``cover`` / ``live``.
    ``engine`` is forwarded to the .sby ``[engines]`` section verbatim
    (e.g. ``smtbmc z3``, ``smtbmc boolector``, ``abc bmc3``).
    """
    if not sv_source or not sv_source.strip():
        raise ValueError("sv_source must be non-empty")
    if not top_module:
        raise ValueError("top_module must be non-empty")

    effective_timeout = (
        timeout_s if timeout_s is not None else settings.symbiyosys_run_timeout_s
    )
    sv_sha = _sha256(sv_source)
    client = get_symbiyosys_client()
    body = await _call_sidecar(
        client, sv_source, top_module, mode, depth, engine,
        effective_timeout, seed,
    )

    if body is None:
        result = SymbiYosysResult(
            ok=False,
            verdict=VERDICT_ERROR,
            exit_code=-1,
            stdout="",
            stderr="symbiyosys sidecar unreachable",
            duration_ms=0,
            tool_version="unknown",
            timed_out=False,
            seed=seed,
            netlist_sha256=sv_sha,
        )
    else:
        raw_verdict = str(body.get("verdict", VERDICT_ERROR)).upper()
        verdict = raw_verdict if raw_verdict in VALID_VERDICTS else VERDICT_ERROR
        result = SymbiYosysResult(
            ok=(verdict == VERDICT_PASS),
            verdict=verdict,
            exit_code=int(body.get("exit_code", -1)),
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            duration_ms=int(body.get("duration_ms", 0)),
            tool_version=str(body.get("tool_version", "unknown")),
            timed_out=bool(body.get("timed_out", False)),
            seed=body.get("seed", seed),
            depth_reached=body.get("depth_reached"),
            counterexample_vcd_b64=body.get("counterexample_vcd_b64"),
            netlist_sha256=sv_sha,
        )

    # depth_reached is the only numeric KPI we have for symbiyosys; the
    # rest of the verification semantics live in the verdict column.
    measurements: dict[str, float] = {}
    if result.depth_reached is not None:
        measurements.update(
            coerce_finite_measurements({"depth_reached": result.depth_reached})
        )

    result.sim_run_id = await _insert_sim_run(
        db,
        tool_version=result.tool_version,
        netlist_sha256=result.netlist_sha256,
        seed=result.seed,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        measurements=measurements,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        verdict=result.verdict,
        job_id=job_id,
        dag_node_id=dag_node_id,
    )
    return result
