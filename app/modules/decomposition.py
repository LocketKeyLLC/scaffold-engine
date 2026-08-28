"""§17.526 — triage-time task decomposition.

One multi-part idea becomes an *umbrella* job (a thin grouping row, no DAG, never
executes) plus N *component* child jobs, each of which runs the ordinary
pipeline autonomously (Phase 1 → Phase 2 grounded research → DAG → execute).

Flow (server-side, kicked once by ``POST /decompose``):
  1. ``extract_components`` — native tool-call splits the idea into components.
  2. ``create_and_run_decomposition`` — insert umbrella + children, then spawn
     one background ``run_component_pipeline`` per child.
  3. each child drives itself to a terminal state; on finish it rolls the
     umbrella up (``_rollup_umbrella``): ``completed`` once all children are
     terminal with ≥1 completed, else ``failed``.

The umbrella status ``aggregating`` is deliberately absent from every cleanup
reaper whitelist (it is inert to the normal sweep); a dedicated sweep in
``cleanup.py`` finalizes an umbrella whose last child was reaped.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import settings
from app.database import async_session
from app.modules.execution_agent import execute_all_nodes
from app.modules.idea_refinement import LLM_SELECTABLE_DOMAINS, _truncate_title
from app.modules.ideation_workflow import (
    analyze_and_confirm,
    get_ideation_slot_sem,
    research_and_compile,
)
from app.providers.base import Tool
from app.utils.job_utils import fail_job as _fail_job
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.decompose")

# Strong refs to in-flight child pipelines (asyncio.create_task holds only a
# weak ref; mirror ideation_workflow._PHASE1_BACKGROUND_TASKS).
_COMPONENT_TASKS: set[asyncio.Task] = set()

# §17.574 — bound how many component pipelines execute concurrently. Components
# already spawn all-at-once (fire-and-forget, below) and each child's DAG now
# runs node-parallel by default (§17.571), so an N-component umbrella could put
# N×inflight inference calls on the host at once. This semaphore caps concurrent
# components; the rest queue (still spawned, just gated on entry). Lazy + resettable
# (settings.decompose_component_max_concurrent), mirroring get_ideation_slot_sem.
_component_sem: asyncio.Semaphore | None = None


def get_component_sem() -> asyncio.Semaphore:
    global _component_sem
    if _component_sem is None:
        _component_sem = asyncio.Semaphore(settings.decompose_component_max_concurrent)
    return _component_sem


def _reset_component_sem() -> None:
    """Drop the cached semaphore so a settings change (or a test) re-reads the cap."""
    global _component_sem
    _component_sem = None

# A build must split into at least this many parts to be worth decomposing;
# below it, /decompose declines and the caller uses the normal single-job path.
MIN_COMPONENTS = 2
# Hard ceiling on components: each becomes its own full pipeline (Phase 1+2 +
# DAG + execute), so an unbounded split would fan out N heavy concurrent runs
# and pin N DB connections. The extractor is asked for 2-5; this enforces it
# even if the model over-splits.
MAX_COMPONENTS = 5
# §17.531 — cap on a component's description (it becomes the child's input_text
# and feeds refine/DAG prompts). Generous but bounded.
MAX_COMPONENT_DESC_LEN = 4000

DECOMPOSE_SYSTEM = (
    "You split a multi-part software/engineering build into independent "
    "components, each buildable on its own as a separate workflow. Return 2-5 "
    "components only when the idea genuinely has separable parts; if it is a "
    "single focused build, return a single component. Each component is "
    "self-contained: its description must stand alone without the others. "
    "Do not invent scope the user did not state or imply."
    # §17.698 — the preserve-existing-system guard that §17.649/§17.694/§17.695
    # added to refine (idea_refinement), synthesis, and DAG generation never
    # reached THIS path. A component's `description` becomes a child's
    # `input_text`, which is the FIRST thing the child's synthesis sees — so if
    # decomposition inverts "clean the reachable Proxmox in place" into "fresh
    # bare-metal reinstall", the child's (now §17.695-aware) synthesis has
    # nothing left to preserve; the destructive premise is already baked in.
    # Production repro: umbrella idea "Configure the reachable Proxmox
    # environment by wiping former user data and resetting services" decomposed
    # into a component titled "Proxmox VE Bare-Metal Rebuild / install a fresh
    # Proxmox VE hypervisor / wipe all disks", and the operator had to fight it
    # step-by-step in assist mode. Removing DATA is not wiping DISKS or
    # reinstalling an OS — keep them distinct here too.
    "\n\nPRESERVE an already-present system. If the idea says the target system "
    "is EXISTING / reachable / accessible / already-running / the user can log "
    "into it (e.g. 'my existing Proxmox', 'the reachable Proxmox environment', "
    "'i can log in'), it is INSTALLED and REACHABLE — do NOT emit a component "
    "that reinstalls the OS, wipes all disks, reformats, or rebuilds from bare "
    "metal. 'Wipe former user data / reset services / start fresh' on such a "
    "system means clean the DATA and redeploy SERVICES in place — NOT a fresh OS "
    "install or a disk wipe (removing data is not the same as wiping disks or "
    "reinstalling an OS). Only emit a from-scratch reinstall / bare-metal-rebuild "
    "component when the idea UNAMBIGUOUSLY asks to rebuild from scratch (e.g. "
    "'bare-metal rebuild', the OS \"won't boot / needs reinstalling\", 'reformat "
    "everything') AND gives no sign the system is already reachable."
    # §17.717 — a component's `description` becomes a child's input_text; if a
    # prerequisite the operator already prepared is dropped here, the child
    # re-acquires it (the reported miss: operator had the Proxmox installer on a
    # USB already plugged in, and the component still planned download + write).
    "\n\nPRESERVE operator-provided PREREQUISITES. If the idea says a required "
    "resource is ALREADY acquired / prepared / on-hand (e.g. 'the Proxmox "
    "installer is already on a USB plugged into the server', 'the ISO is already "
    "downloaded'), carry that fact into the description of the component it "
    "applies to and frame that component as STARTING from the ready resource — do "
    "NOT emit a component or scope that re-acquires, re-downloads, or re-prepares "
    "what the operator already has."
)

DECOMPOSE_COMPONENTS_TOOL = Tool(
    name="emit_task_components",
    description=(
        "Record the independent components a build idea splits into, each "
        "buildable as its own workflow."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "Concise component name (max 6 words).",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One self-contained paragraph: what this "
                                "component must do, standing alone."
                            ),
                        },
                        "domain": {
                            "type": "string",
                            "enum": sorted(LLM_SELECTABLE_DOMAINS),
                            "description": "Knowledge domain for grounding; default 'eng'.",
                        },
                        "research_queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2-4 grounded-standards search queries for this component.",
                        },
                    },
                    "required": ["label", "description"],
                },
            },
        },
        "required": ["components"],
    },
)


async def extract_components(
    idea_text: str, *, model_overrides: dict | None = None,
) -> list[dict]:
    """Split ``idea_text`` into normalized component dicts via native tool-call.

    Returns ``[{label, description, domain, research_queries}]`` (possibly a
    single component, or ``[]`` on any soft failure — the caller decides whether
    to decompose based on the count).
    """
    resp = await model_router.tool_call(
        messages=[
            {"role": "system", "content": DECOMPOSE_SYSTEM},
            {"role": "user", "content": idea_text},
        ],
        tools=[DECOMPOSE_COMPONENTS_TOOL],
        temperature=0.2,
        max_tokens=2048,
        role=settings.ideation_model_role,
        overrides=model_overrides,
    )
    if not resp.success:
        logger.warning("extract_components: llm_failed err=%s", resp.error)
        return []
    args = read_tool_args(resp)
    if args is None or not isinstance(args.get("components"), list):
        logger.warning("extract_components: parse_failed raw=%r", (resp.text or "")[:200])
        return []

    out: list[dict] = []
    for c in args["components"]:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        desc = str(c.get("description") or "").strip()
        if not label or not desc:
            continue
        domain = c.get("domain")
        out.append({
            "label": label[:80],
            # §17.531 — bound the description: it becomes a child's input_text
            # and flows into refine/DAG prompts. Cap to MAX_COMPONENT_DESC_LEN so
            # an over-long (or injection-padded) field can't bloat downstream
            # prompts. Mirrors the MAX_LLM_TEXT_LEN bounds on schema input fields.
            "description": desc[:MAX_COMPONENT_DESC_LEN],
            "domain": domain if domain in LLM_SELECTABLE_DOMAINS else "eng",
            "research_queries": [
                q for q in (c.get("research_queries") or []) if isinstance(q, str)
            ][:4],
        })
    if len(out) > MAX_COMPONENTS:
        logger.warning(
            "extract_components: model emitted %d components; capping to %d",
            len(out), MAX_COMPONENTS,
        )
    return out[:MAX_COMPONENTS]


async def _compile_umbrella_deliverable(db: AsyncSession, umbrella_id: str) -> str:
    """§17.533 — stitch the component children's compiled outputs into one
    umbrella-level markdown deliverable (ordered by component_index)."""
    title = (await db.execute(
        text("SELECT title FROM jobs WHERE id = :u"), {"u": umbrella_id},
    )).scalar_one_or_none() or "Decomposed build"
    rows = (await db.execute(
        text("""
            SELECT component_index, title, status, compiled_output
            FROM jobs WHERE parent_job_id = :u
            ORDER BY component_index
        """),
        {"u": umbrella_id},
    )).mappings().all()
    parts = [f"# {title}", "", f"_Assembled from {len(rows)} components._", ""]
    for r in rows:
        idx = (r["component_index"] or 0) + 1
        parts.append(f"## Component {idx}: {r['title']} [{r['status']}]")
        body = (r["compiled_output"] or "").strip()
        parts.append(body if body else "_(no output produced)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


async def _refresh_umbrella_children_snapshot(
    db: AsyncSession, umbrella_id: str,
) -> None:
    """§17.701 — keep ``metadata.children[].status`` in sync with the live child
    job statuses. The snapshot was written ONCE at decomposition time (all
    'refining') and never updated, so raw metadata inspection was misleading.
    Every USER-FACING surface (``/results``, ``/status``, assist's umbrella
    guard) already reads child status LIVE via ``parent_job_id`` — this is
    housekeeping so the denormalized snapshot doesn't lie. Preserves each entry's
    label / job_id / component_index; only refreshes ``status``. Commits itself."""
    meta = (await db.execute(
        text("SELECT metadata FROM jobs WHERE id = :u"), {"u": umbrella_id},
    )).scalar()
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = None
    if not isinstance(meta, dict):
        return
    kids = meta.get("children")
    if not isinstance(kids, list) or not kids:
        return
    live = {
        str(r["id"]): r["status"]
        for r in (await db.execute(
            text("SELECT id, status FROM jobs WHERE parent_job_id = :u"),
            {"u": umbrella_id},
        )).mappings().all()
    }
    changed = False
    for k in kids:
        if (isinstance(k, dict) and k.get("job_id") in live
                and k.get("status") != live[k["job_id"]]):
            k["status"] = live[k["job_id"]]
            changed = True
    if changed:
        meta["children"] = kids
        # §17.811 — refresh the umbrella's progress + ETA over component children
        # (single writer; folded into the same metadata write, no race). Children
        # are heterogeneous concurrent pipelines, so report count/pct without a
        # time ETA rather than a misleading one.
        if settings.progress_eta_enabled:
            _terminal = {"completed", "failed", "cancelled"}
            _total = len(kids)
            _done = sum(
                1 for k in kids
                if isinstance(k, dict) and k.get("status") in _terminal
            )
            _pct = int(round(100.0 * _done / _total)) if _total else None
            meta["progress"] = {
                "phase": "aggregating",
                "label": "Building components",
                "unit": "components",
                "completed": _done,
                "total": _total,
                "pct": _pct,
                "eta_ms": None,
                "eta_human": None,
                "current_item": None,
                "summary": f"{_done}/{_total} components · {_pct}%",
                "soft": False,
            }
        await db.execute(
            text("UPDATE jobs SET metadata = CAST(:m AS jsonb), updated_at = NOW() "
                 "WHERE id = :u"),
            {"m": json.dumps(meta), "u": umbrella_id},
        )
        await db.commit()


async def _rollup_umbrella(db: AsyncSession, umbrella_id: str) -> None:
    """Recompute an umbrella's status from its children. Idempotent + safe to
    call repeatedly; only promotes to a terminal state once every child is
    terminal. On 'completed', also assembles the unified deliverable from the
    children's outputs (§17.533).

    §17.701 — re-promotes from ``awaiting_assist`` too, not just ``aggregating``.
    A decomposition parks the umbrella at ``awaiting_assist`` once its children
    reach the hands-on gate; when the operator then FINISHES those components via
    assist mode, this must fire again to carry the umbrella awaiting_assist →
    completed and assemble the unified deliverable. Pre-§17.701 the UPDATE guard
    was ``status = 'aggregating'``, so a parked umbrella could never re-complete
    (and the cleanup safety net only catches 'aggregating'), stranding the
    whole-project deliverable forever once every component was done via assist."""
    # Keep the denormalized child-status snapshot honest on every rollup tick.
    await _refresh_umbrella_children_snapshot(db, umbrella_id)
    row = (await db.execute(
        text("""
            SELECT count(*) AS total,
                   count(*) FILTER (
                       WHERE status IN ('completed','failed','cancelled',
                                        'blocked','awaiting_assist')
                   ) AS terminal,
                   count(*) FILTER (WHERE status = 'completed') AS done,
                   count(*) FILTER (WHERE status = 'awaiting_assist') AS awaiting
            FROM jobs WHERE parent_job_id = :u
        """),
        {"u": umbrella_id},
    )).mappings().first()
    if not row or not row["total"] or row["terminal"] != row["total"]:
        return
    # §17.624 — a component parked by the hands-on assist gate is terminal for
    # roll-up (autonomous processing is done) but the umbrella as a whole still
    # needs the operator. Surface awaiting_assist rather than a misleading
    # 'completed' when ANY child is parked; otherwise the prior completed/failed
    # rule stands.
    if row["awaiting"] > 0:
        new_status = "awaiting_assist"
    elif row["done"] > 0:
        new_status = "completed"
    else:
        new_status = "failed"
    compiled = (
        await _compile_umbrella_deliverable(db, umbrella_id)
        if new_status in ("completed", "awaiting_assist") else None
    )
    # COALESCE keeps any prior compiled_output if a racing finalizer already set
    # it; the status guard makes the finalize single-winner. §17.701 — accept
    # 'awaiting_assist' too so a parked umbrella re-completes when its last
    # component finishes via assist, and `status <> :s` skips the no-op
    # awaiting_assist → awaiting_assist churn while other components remain.
    result = await db.execute(
        text("""
            UPDATE jobs SET status = :s,
                compiled_output = COALESCE(:co, compiled_output)
            WHERE id = :u
              AND status IN ('aggregating', 'awaiting_assist')
              AND status <> :s
            RETURNING id
        """),
        {"s": new_status, "co": compiled, "u": umbrella_id},
    )
    if result.first() is None:
        return  # lost the finalize race; another caller already rolled up
    await db.commit()
    logger.info(
        "umbrella_rollup: umbrella=%s status=%s (%d/%d children completed)",
        umbrella_id, new_status, row["done"], row["total"],
    )


async def run_component_pipeline(
    child_id: str,
    idea_text: str,
    *,
    domain: str | None,
    research_queries: list[str] | None,
    model_overrides: dict | None,
    umbrella_id: str,
) -> None:
    """Drive one component child through the full pipeline on its own session,
    then roll its umbrella up. Any failure marks the child ``failed`` (so it
    never strands in a non-terminal status) and still triggers the rollup."""
    # §17.574 — bound concurrent component pipelines; beyond the cap they queue
    # here (already spawned, gated on entry). Released in the finally below.
    sem = get_component_sem()
    await sem.acquire()
    try:
        async with async_session() as db:
            async with get_ideation_slot_sem():
                phase1 = await analyze_and_confirm(
                    idea_text, db,
                    domain=domain, model_overrides=model_overrides, job_id=child_id,
                )
            # §17.530 — STOP if Phase 1 failed. analyze_and_confirm RETURNS a
            # {"status":"failed"} dict (it does not raise), so without this gate
            # the child falls through to execute_all_nodes, whose guard
            # (status NOT IN running/completed) RESURRECTS the failed job back to
            # 'running' — it could then complete and corrupt the umbrella rollup.
            if isinstance(phase1, dict) and phase1.get("status") == "failed":
                return  # finally still rolls the umbrella up; child stays failed
            # Honor the decomposition tool's curated research queries: they
            # drive Phase 2's grounded search (read at ideation_workflow's
            # recommended_research_queries). Phase 1 left the row in
            # awaiting_confirmation with research_data set.
            if research_queries:
                await db.execute(
                    # CAST(:q AS jsonb), NOT :q::jsonb — SQLAlchemy text() treats
                    # a bind param immediately followed by ``::`` as ambiguous and
                    # leaves :q unbound (PostgresSyntaxError "syntax error at :").
                    text("""
                        UPDATE jobs SET research_data = jsonb_set(
                            coalesce(research_data, '{}'::jsonb),
                            '{feasibility,recommended_research_queries}',
                            CAST(:q AS jsonb), true)
                        WHERE id = :id
                    """),
                    {"q": json.dumps(research_queries), "id": child_id},
                )
                await db.commit()
            phase2 = await research_and_compile(
                child_id, db, model_overrides=model_overrides,
            )
            # §17.530 — same resurrection guard for Phase 2: a 'failed' or
            # 'conflict' (lost the atomic claim, e.g. reaper-cancelled mid-flight)
            # must NOT fall through to execute_all_nodes.
            if isinstance(phase2, dict) and phase2.get("status") in ("failed", "conflict"):
                return

        # execute_all_nodes auto-generates the DAG and self-manages sessions;
        # consume the SSE generator to completion to run the child autonomously.
        async for _ in execute_all_nodes(child_id, model_overrides=model_overrides):
            pass
    except Exception as e:  # noqa: BLE001 — best-effort; never strand a child
        logger.exception("component_pipeline_failed: child=%s", child_id)
        try:
            async with async_session() as fdb:
                await _fail_job(fdb, child_id, f"component pipeline error: {e}")
        except Exception:
            logger.exception("component_pipeline_fail_mark_failed: child=%s", child_id)
    finally:
        # §17.530 — shield the rollup so a CancelledError landing in this finally
        # can't skip the umbrella update mid-await. The Stage-7 reaper sweep is
        # the backstop, but shielding keeps the common case correct.
        async def _do_rollup() -> None:
            async with async_session() as rdb:
                await _rollup_umbrella(rdb, umbrella_id)
        # §17.854 (audit A8) — sem.release() MUST run in its own finally. A
        # CancelledError delivered to this task while awaiting the shield
        # re-raises out of the await (CancelledError is BaseException, not caught
        # by `except Exception`), which previously skipped the release below and
        # permanently shrank decompose_component_max_concurrent by one slot until
        # process restart. The shielded rollup still completes; the release is
        # now unconditional.
        try:
            await asyncio.shield(_do_rollup())
        except Exception:
            logger.exception("component_pipeline_rollup_failed: umbrella=%s", umbrella_id)
        finally:
            sem.release()  # §17.574 — free the component slot for a queued child


def _spawn_component(
    child_id: str,
    idea_text: str,
    *,
    domain: str | None,
    research_queries: list[str] | None,
    model_overrides: dict | None,
    umbrella_id: str,
) -> asyncio.Task:
    """Fire-and-forget a child pipeline with a strong ref so it survives GC."""
    task = asyncio.create_task(
        run_component_pipeline(
            child_id, idea_text,
            domain=domain, research_queries=research_queries,
            model_overrides=model_overrides, umbrella_id=umbrella_id,
        )
    )
    _COMPONENT_TASKS.add(task)
    task.add_done_callback(_COMPONENT_TASKS.discard)
    return task


async def create_and_run_decomposition(
    idea_text: str,
    db: AsyncSession,
    *,
    components: list[dict],
    model_overrides: dict | None = None,
    owner: str | None = None,
) -> dict:
    """Insert the umbrella + one child per component, then spawn each child's
    pipeline. Returns the umbrella id and the child roll-up immediately.

    §17.810 — the whole tree (umbrella + every component) is stamped with the
    same ``owner`` as the caller, so a user sees their decomposition and all its
    children, and nobody else's.
    """
    umbrella_id = (await db.execute(
        text("""
            INSERT INTO jobs (title, input_text, status, job_type, owner)
            VALUES (:title, :input_text, 'aggregating', 'umbrella', :owner)
            RETURNING id
        """),
        {"title": _truncate_title(idea_text), "input_text": idea_text, "owner": owner},
    )).scalar_one()

    children: list[dict] = []
    for idx, comp in enumerate(components):
        child_id = (await db.execute(
            text("""
                INSERT INTO jobs
                    (title, input_text, status, job_type, parent_job_id, component_index, metadata, owner)
                VALUES
                    (:title, :input_text, 'refining', 'component', :parent, :idx, :meta, :owner)
                RETURNING id
            """),
            {
                "title": _truncate_title(comp["label"]),
                "input_text": comp["description"],
                "parent": umbrella_id,
                "idx": idx,
                "meta": json.dumps({"component": {
                    "label": comp["label"],
                    "domain": comp.get("domain", "eng"),
                }}),
                "owner": owner,
            },
        )).scalar_one()
        children.append({
            "job_id": str(child_id),
            "component_index": idx,
            "label": comp["label"],
            "status": "refining",
        })

    # Record the child map on the umbrella for /results rollup convenience.
    await db.execute(
        text("UPDATE jobs SET metadata = :m WHERE id = :u"),
        {"m": json.dumps({"children": children}), "u": str(umbrella_id)},
    )
    await db.commit()

    for child, comp in zip(children, components):
        _spawn_component(
            child["job_id"], comp["description"],
            domain=comp.get("domain"),
            research_queries=comp.get("research_queries"),
            model_overrides=model_overrides,
            umbrella_id=str(umbrella_id),
        )

    logger.info(
        "decomposition_started: umbrella=%s children=%d", umbrella_id, len(children),
    )
    return {
        "umbrella_job_id": str(umbrella_id),
        "status": "aggregating",
        "children": children,
    }
