"""Runnable service-arbitrage automation pipeline built on ``scaffold_client``.

Drives a client request end-to-end through the orchestrator with no manual
gate:

    intake parse (Phase 1)  →  feasibility gate  →  Phase 2 + DAG  →
    verifier-gated execution with a QA remediation loop  →  compiled output

Why this module exists on top of the SDK
-----------------------------------------
``scaffold_client.AsyncClient`` already injects ``X-API-Key``, maps non-2xx
responses to typed ``ScaffoldError`` subclasses, and parses SSE frames into
``{"event", "data"}`` dicts. It deliberately does NOT:

  * retry transient failures (timeouts / 5xx / connection drops), and
  * inspect response *bodies* for the "HTTP 200 but ``status='failed'``"
    shape that ``/dag`` and the Phase-2/Phase-1 helpers can return.

This module adds exactly those two things, plus exact Pydantic models for the
four response shapes (verified against app source: ``ideation_workflow.py``,
``idea_refinement.py``, ``dag_generator.py``, ``execution_handler.py``,
``execution_agent.py``).

Two structural facts the design turns on
----------------------------------------
1. **No auto-chain off-pipeline.** The OWUI ``/confirm`` macro that chains
   Phase 2 → DAG → execute lives in ``pipelines/scaffold_router.py``, not the
   orchestrator. A scripted client must call ``confirm`` → (``dag.create``) →
   ``aiter_execute_all`` explicitly. ``/execute/all`` auto-generates the DAG if
   none exists, so the explicit ``dag.create`` is optional — kept here so we
   can inspect and validate the plan before spending execution tokens.
2. **``CancelledError`` is a ``BaseException``.** ``except Exception`` will not
   catch it, so the audit write on cancellation is wrapped in
   ``asyncio.shield`` to survive the cancellation.

Model-role tuning (cheap 4b for intake/DAG, accurate 7b for extract/verify) is
a pipeline-valve concern (``/model set``) and is applied out of band — the
orchestrator HTTP surface has no per-role setter, only per-request
``model_overrides`` which the SDK does not expose. See the skill's
``references/pipelines.md``.

Run
---
    python -m examples.service_arbitrage_pipeline "Parse this intake: ..."
    echo "client email text" | python -m examples.service_arbitrage_pipeline -

Env: ``SCAFFOLD_ORCHESTRATOR_URL`` (default http://localhost:8000),
``SCAFFOLD_API_KEY``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scaffold_client import (
    AsyncClient,
    AuthenticationError,
    ConnectionError as ScaffoldConnectionError,
    NotFoundError,
    OrchestratorError,
    RequestError,
    ScaffoldError,
    TimeoutError as ScaffoldTimeoutError,
)
from scaffold_client.schemas import JOB_STATUSES

logger = logging.getLogger("scaffold.arbitrage")

# Terminal job states. Validated against the SDK's canonical JobStatus set so a
# rename/removal upstream fails fast at import instead of making _await_terminal
# poll a finished job to its timeout. (The SDK exposes no terminal-only subset
# to import; this at least catches drift in the names we hardcode.)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "blocked"})
assert TERMINAL_STATES <= set(JOB_STATUSES), (
    f"TERMINAL_STATES drifted from SDK JobStatus: {TERMINAL_STATES - set(JOB_STATUSES)}"
)


# ---------------------------------------------------------------------------
# Exact response models — verified against app source (file:line in comments).
# Every field is defensively optional beyond what the server guarantees,
# because on a coaxing-fallback LLM provider the structured args are JSON-
# parsed rather than SDK-validated, so presence isn't guaranteed.
# ---------------------------------------------------------------------------


class RefinedBrief(BaseModel):
    """``emit_refined_brief`` tool args — idea_refinement.py:57-116, returned :285."""

    model_config = ConfigDict(extra="allow")

    title: str = ""
    description: str = ""
    domain: str = "eng"          # enum: eng|llm|rag|prompt|spec
    goals: list[str] = Field(default_factory=list)
    complexity: str = "medium"   # enum: low|medium|high
    constraints: list[str] = Field(default_factory=list)
    inputs_available: list[str] = Field(default_factory=list)
    outputs_expected: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class Feasibility(BaseModel):
    """ideation_workflow.py:256-267 (fallback shape :233-241)."""

    model_config = ConfigDict(extra="allow")

    feasible: bool = True
    confidence: float = 0.0
    risks: list[str] = Field(default_factory=list)
    clarifications_needed: list = Field(default_factory=list)
    recommended_research_queries: list[str] = Field(default_factory=list)
    summary: str = ""
    # True ⇒ the feasibility LLM pass FAILED and defaulted to proceed
    # (confidence=0.5, feasible=True). Never auto-confirm on this.
    fallback: bool = False


class IdeateResponse(BaseModel):
    """``POST /ideate`` success — ideation_workflow.py:256-267."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str                  # "awaiting_confirmation" on success
    refined_brief: RefinedBrief
    feasibility: Feasibility
    message: str = ""


class DagTask(BaseModel):
    """Item in ``/dag`` ``tasks`` — dag_generator.py:1077-1128.

    NOTE the key is ``id`` here ("T1"...), but the same node is ``node_key``
    everywhere it is read back (``/exec/status``, ``/dag/{job_id}``).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str = ""
    type: str = "action"         # research|action|output|decision|validation
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    tool: str = "LLM"            # LLM|SearXNG|Milvus|CodeGen|Shell
    domain: str | None = None
    assigned_model: str | None = None
    notes: str | None = None
    is_deliverable: bool = False


class DagEdge(BaseModel):
    """_build_edges — dag_generator.py:1219. ``from`` is a Python keyword."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class DagResponse(BaseModel):
    """``POST /dag`` success — dag_generator.py:961-974.

    ``status`` is ``"executing"`` on success (NOT "planning" — the docstring
    at dag_generator.py:708 is stale). There is no ``validation`` field.
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    strategy: str = ""
    task_count: int = 0
    tasks: list[DagTask] = Field(default_factory=list)
    edges: list[DagEdge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    mermaid_dag: str = ""


class ExecNode(BaseModel):
    """Node in ``/exec/status`` — execution_handler.py:78-96."""

    model_config = ConfigDict(extra="allow")

    node_key: str
    title: str | None = None
    status: str = "pending"
    execution_order: int = 0
    depends_on: list = Field(default_factory=list)
    deps_met: bool = False
    actionable: bool = False
    assigned_model: str | None = None
    failure_reason: str | None = None   # dag_nodes.last_verification_reason
    is_deliverable: bool = False
    confidence: float | None = None
    tool: str | None = None


class ExecStatus(BaseModel):
    """``GET /exec/status/{job_id}`` — execution_handler.py:138-163.

    Poll ``job_status`` (NOT ``status``) for the terminal state.
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    job_title: str | None = None
    job_status: str
    error_summary: str | None = None
    completed_at: str | None = None
    compiled_output: str | None = None
    synthesized: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    total_nodes: int = 0
    nodes: list[ExecNode] = Field(default_factory=list)


class NodeFailure(BaseModel):
    """``node_failed`` SSE ``data`` payload — execution_agent.py:2269-2277."""

    model_config = ConfigDict(extra="allow")

    job_id: str | None = None
    node_key: str
    title: str | None = None
    error: str | None = None
    verification_reason: str | None = None   # set ⇒ the verifier rejected
    model_used: str | None = None
    retries_exhausted: bool = False


# ---------------------------------------------------------------------------
# Errors + config
# ---------------------------------------------------------------------------


class PipelineFailure(ScaffoldError):
    """A phase returned a failed/conflict body (possibly HTTP 200)."""


class SchemaMismatch(ScaffoldError):
    """A response body did not match the verified contract."""


@dataclass
class PipelineConfig:
    feasibility_bar: float = 0.65
    # QA loop: how many times to re-pump /execute/all after remediating
    # failures before giving up and skipping anything still failing.
    max_qa_rounds: int = 2
    # Recovery poll after an SSE drop.
    poll_attempts: int = 720
    poll_interval_s: float = 5.0
    # Transient-retry policy for non-streaming calls.
    retry_attempts: int = 4
    retry_base_delay_s: float = 1.5
    retry_max_delay_s: float = 20.0


@dataclass
class PipelineResult:
    job_id: str | None
    outcome: str                       # "completed" | "failed" | "blocked" |
                                       # "cancelled" | "diverted"
    status: ExecStatus | None = None
    compiled_output: str | None = None
    diverted_reason: str | None = None
    skipped_nodes: list[str] = field(default_factory=list)


# Network-transient errors — always safe to retry (the request likely never
# reached the server). 5xx (OrchestratorError) is retried ONLY for idempotent
# calls (GET): retrying a non-idempotent POST duplicates work (ideate creates a
# job every call) or masks the real 5xx behind a follow-up conflict (confirm's
# atomic claim fails on the retry once the job is already failed).
_NETWORK_TRANSIENT = (ScaffoldTimeoutError, ScaffoldConnectionError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _retry_transient(cfg: PipelineConfig, label: str, coro_factory,
                           *, retry_5xx: bool = False):
    """Await ``coro_factory()`` with bounded exponential backoff on transient
    SDK errors. Contract errors (auth, 4xx, not-found) propagate immediately.

    ``retry_5xx`` opts a call into retrying server errors (OrchestratorError);
    pass it only for idempotent calls (GET). Non-idempotent POSTs leave it False
    so a deterministic 5xx isn't retried into duplicate work / a masked error.
    """
    retryable = _NETWORK_TRANSIENT + ((OrchestratorError,) if retry_5xx else ())
    last: Exception | None = None
    for attempt in range(1, cfg.retry_attempts + 1):
        try:
            return await coro_factory()
        except retryable as exc:
            last = exc
            delay = min(cfg.retry_base_delay_s * 2 ** (attempt - 1), cfg.retry_max_delay_s)
            logger.warning("retry %s attempt=%d/%d delay=%.1fs cause=%s",
                           label, attempt, cfg.retry_attempts, delay, exc)
            await asyncio.sleep(delay)
    raise last  # exhausted


def _check_failure(body: dict, label: str) -> dict:
    """Raise on the failed/conflict body shapes that can arrive as HTTP 200.

    ``/dag`` validation failures return ``{status:"failed", errors:[...]}``
    (plural ``errors``) with a 200, and re-entry guards return ``error`` +
    ``http_status`` — the SDK surfaces 4xx/5xx as exceptions, but a 200 body
    with an in-band failure slips through and must be caught here.
    """
    if not isinstance(body, dict):
        raise SchemaMismatch(f"{label}: expected object, got {type(body).__name__}")
    # Truthiness, not key-presence: a success body carrying error=null / errors=[]
    # must not trip this (else `detail` falls through to a nonsense status string).
    if body.get("status") == "failed" or body.get("error") or body.get("errors"):
        detail = body.get("error") or body.get("errors") or body.get("status")
        raise PipelineFailure(f"{label}: {detail}")
    return body


def _validate(model: type[BaseModel], body: dict, label: str):
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise SchemaMismatch(f"{label}: {e.error_count()} field mismatch(es): {e}") from e


# ---------------------------------------------------------------------------
# Phase A — intake parsing
# ---------------------------------------------------------------------------


async def parse_intake(client: AsyncClient, cfg: PipelineConfig,
                       raw_message: str, *, domain: str | None = None) -> IdeateResponse:
    """Phase 1: refine + feasibility. Lands the job in ``awaiting_confirmation``.

    A refinement failure comes back as an HTTP error (the endpoint raises on a
    dict with ``error``), so the SDK converts it to a ``ScaffoldError`` before
    we get here.
    """
    body = await _retry_transient(
        cfg, "ideate", lambda: client.ideate(raw_message, domain=domain),
    )
    _check_failure(body, "ideate")
    brief = _validate(IdeateResponse, body, "ideate")
    if brief.status != "awaiting_confirmation":
        raise SchemaMismatch(
            f"ideate: expected awaiting_confirmation, got {brief.status!r}"
        )
    logger.info("intake.parsed job=%s feasible=%s conf=%.2f fallback=%s",
                brief.job_id, brief.feasibility.feasible,
                brief.feasibility.confidence, brief.feasibility.fallback)
    return brief


# ---------------------------------------------------------------------------
# Phase B — feasibility gate + confirm + DAG
# ---------------------------------------------------------------------------


async def gated_confirm_and_plan(client: AsyncClient, cfg: PipelineConfig,
                                 brief: IdeateResponse) -> tuple[str | None, str | None]:
    """Return ``(job_id, None)`` to proceed, or ``(None, reason)`` to divert.

    Diverts to a human on: not feasible, low confidence, OR a fallback (the
    feasibility LLM failed and blindly defaulted to proceed).
    """
    f = brief.feasibility
    if f.fallback:
        return None, "feasibility_check_failed"
    if not f.feasible:
        return None, "infeasible"
    if f.confidence < cfg.feasibility_bar:
        return None, f"low_confidence({f.confidence:.2f}<{cfg.feasibility_bar})"

    # Scripted path does NOT auto-chain — confirm, then plan explicitly.
    confirm_body = await _retry_transient(
        cfg, "confirm", lambda: client.confirm(brief.job_id),
    )
    _check_failure(confirm_body, "confirm")

    # /execute/all would auto-generate the DAG; we build it explicitly so we
    # can validate the plan (and catch a 200-but-failed body) before spending
    # execution tokens.
    dag_body = await _retry_transient(
        cfg, "dag", lambda: client.dag.create(brief.job_id),
    )
    _check_failure(dag_body, "dag")
    dag = _validate(DagResponse, dag_body, "dag")
    logger.info("plan.built job=%s strategy=%s tasks=%d warnings=%d",
                dag.job_id, dag.strategy, dag.task_count, len(dag.warnings))
    return brief.job_id, None


# ---------------------------------------------------------------------------
# Phase C — execution with QA remediation loop
# ---------------------------------------------------------------------------


async def execute_with_qa(client: AsyncClient, cfg: PipelineConfig,
                          job_id: str) -> PipelineResult:
    """Stream ``/execute/all`` and remediate failures after each round.

    The orchestrator auto-retries each node up to ``max_retries`` server-side,
    so by the time a ``node_failed`` frame arrives its retry budget is spent.
    We therefore collect failures during the stream and act *after* it ends:
    a verifier false-negative (``verification_reason`` set) → ``skip``; a hard
    error → one ``/exec/retry``. Then we re-pump ``/execute/all`` to drain the
    reset nodes, capped at ``max_qa_rounds`` so an unfixable node can't spin
    forever.
    """
    skipped: list[str] = []
    tried_retry: set[str] = set()

    for round_no in range(1, cfg.max_qa_rounds + 2):  # +1 initial pass
        failures: dict[str, NodeFailure] = {}
        stream_ok = True
        try:
            async for evt in client.aiter_execute_all(job_id):
                if evt.get("event") != "node_failed":
                    continue
                data = evt.get("data")
                if not isinstance(data, dict):
                    continue
                try:
                    nf = NodeFailure.model_validate(data)
                except ValidationError:
                    logger.warning("qa.unparsable_node_failed data=%r", data)
                    continue
                failures[nf.node_key] = nf
        except (ScaffoldError, httpx.HTTPError) as e:
            # SSE dropped mid-flight; the orchestrator keeps running server-side.
            # Still remediate what we collected this round (don't discard the
            # verifier-false-negatives the module exists to fix), then stop —
            # re-pumping after an unreliable stream isn't safe; recover by polling.
            logger.warning("qa.stream_dropped job=%s round=%d cause=%r", job_id, round_no, e)
            stream_ok = False

        if not failures:
            break

        # Last allowed round: skip anything still failing rather than looping.
        # (The range bound + this break terminate the loop; no progress flag needed.)
        last_round = round_no > cfg.max_qa_rounds
        for node_key, nf in failures.items():
            verifier_reject = bool(nf.verification_reason)
            can_retry = (not verifier_reject and nf.error
                         and node_key not in tried_retry and not last_round)
            action = "retry" if can_retry else "skip"
            try:
                if can_retry:
                    await _retry_transient(
                        cfg, f"retry:{node_key}",
                        lambda nk=node_key: client.jobs.retry(job_id, nk),
                    )
                    tried_retry.add(node_key)
                    logger.info("qa.retry job=%s node=%s", job_id, node_key)
                else:
                    await _retry_transient(
                        cfg, f"skip:{node_key}",
                        lambda nk=node_key: client.skip(job_id, nk),
                    )
                    skipped.append(node_key)
                    logger.warning("qa.skip job=%s node=%s reason=%s",
                                   job_id, node_key,
                                   nf.verification_reason or nf.error or "unknown")
            except (RequestError, NotFoundError) as e:
                # A benign 4xx (node raced to terminal / not skippable) must not
                # crash the whole run — log it and handle the other failures.
                logger.warning("qa.%s_rejected job=%s node=%s cause=%r",
                               action, job_id, node_key, e)

        if not stream_ok:
            break

    status = await _await_terminal(client, cfg, job_id)
    return PipelineResult(
        job_id=job_id,
        outcome=status.job_status,
        status=status,
        compiled_output=status.compiled_output,
        skipped_nodes=skipped,
    )


async def _await_terminal(client: AsyncClient, cfg: PipelineConfig,
                          job_id: str) -> ExecStatus:
    """Poll ``/exec/status`` until the job reaches a terminal ``job_status``."""
    for _ in range(cfg.poll_attempts):
        body = await _retry_transient(
            cfg, "status", lambda: client.jobs.status(job_id),
            retry_5xx=True,  # GET is idempotent — safe to retry a transient 5xx
        )
        status = _validate(ExecStatus, body, "exec_status")
        if status.job_status in TERMINAL_STATES:
            return status
        await asyncio.sleep(cfg.poll_interval_s)
    raise ScaffoldTimeoutError(f"{job_id}: no terminal state within budget")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_client(client: AsyncClient, raw_message: str, *,
                     cfg: PipelineConfig | None = None,
                     domain: str | None = None) -> PipelineResult:
    """Drive one client request through the whole pipeline."""
    cfg = cfg or PipelineConfig()
    brief = await parse_intake(client, cfg, raw_message, domain=domain)
    job_id, reason = await gated_confirm_and_plan(client, cfg, brief)
    if job_id is None:
        logger.info("intake.diverted job=%s reason=%s", brief.job_id, reason)
        return PipelineResult(job_id=brief.job_id, outcome="diverted",
                              diverted_reason=reason)
    try:
        return await execute_with_qa(client, cfg, job_id)
    except asyncio.CancelledError:
        # CancelledError is a BaseException — an `except Exception` would skip
        # this. Shield the audit write so it survives the cancellation.
        await asyncio.shield(_mark_disconnect(job_id))
        raise


async def _mark_disconnect(job_id: str) -> None:
    """Audit hook mirroring the orchestrator's own client_disconnect handling."""
    logger.info("client.cancelled job=%s (server-side reaper will finalize)", job_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _amain(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("message", help="intake text, or '-' to read from stdin")
    ap.add_argument("--domain", default=None, help="knowledge domain override")
    ap.add_argument("--url", default=os.environ.get(
        "SCAFFOLD_ORCHESTRATOR_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.environ.get("SCAFFOLD_API_KEY"))
    ap.add_argument("--feasibility-bar", type=float, default=0.65)
    ap.add_argument("--stream-timeout", type=float, default=3600.0,
                    help="client timeout (s) — must cover long CPU execution")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    message = sys.stdin.read() if args.message == "-" else args.message
    if not message.strip():
        print("error: empty intake message", file=sys.stderr)
        return 2

    cfg = PipelineConfig(feasibility_bar=args.feasibility_bar)

    async with AsyncClient(args.url, api_key=args.api_key,
                           timeout=args.stream_timeout) as client:
        try:
            result = await run_client(client, message, cfg=cfg, domain=args.domain)
        except AuthenticationError as e:
            print(f"auth failed: {e}\n"
                  "Check SCAFFOLD_API_KEY matches the orchestrator env "
                  "(5-place sync: .env, valves.json, ~/.bashrc, both containers).",
                  file=sys.stderr)
            return 3
        except (PipelineFailure, SchemaMismatch, ScaffoldError) as e:
            print(f"pipeline error: {e}", file=sys.stderr)
            return 1

    print(f"\njob_id:  {result.job_id}")
    print(f"outcome: {result.outcome}")
    if result.diverted_reason:
        print(f"diverted: {result.diverted_reason}")
    if result.skipped_nodes:
        print(f"skipped nodes: {', '.join(result.skipped_nodes)}")
    if result.compiled_output:
        print("\n=== compiled output ===\n")
        print(result.compiled_output)
    return 0 if result.outcome in ("completed", "diverted") else 1


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
