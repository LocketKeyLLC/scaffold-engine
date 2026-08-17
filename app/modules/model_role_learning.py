"""§17.803 — role→model learning: periodic golden re-A/B → staged swap proposals.

A closed loop over the model-role assignments. On an interval (scheduler,
default OFF) this re-runs the objective per-role golden A/B (``scripts.model_ab.
run_model_ab_task`` — the SAME deterministic scoring the runtime uses) for each
switchable role that has a golden task, and when a candidate model beats the
incumbent *clean* (≥ pass rate, zero errors, strictly faster) it STAGES an
``open`` proposal in ``model_role_proposals`` (migration 065).

Nothing auto-swaps. A proposal is a suggestion until the operator reviews it as
a §17.629 confirm card in chat and explicitly accepts — at which point
``accept_proposal`` applies the swap through the existing durable override path
(``app.modules.model_overrides.set_override``).

Scope note: ``model_ab`` has three golden tasks, so only three of the seven
switchable roles are learnable today (coder / verifier / research_extract). The
other roles have no objective golden signal and are intentionally not covered
until one is added — ``run_learning_cycle`` logs which roles it skipped.

Design lineage: this promotes the §17.578 scheduled re-A/B job — which only
emitted a ``model_ab_recommend`` LOG line (nobody reads logs) — into a durable,
human-gated proposal. The incumbent-vs-candidate decision rule (``select_winner``)
is shared with that job (app/scheduler.py) so there is ONE rule, two callers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from statistics import mean

from sqlalchemy import text

from app.config import settings, get_model, SWITCHABLE_ROLE_FIELDS
from app.database import async_session
from app.modules.model_overrides import set_override

logger = logging.getLogger("scaffold.model_role_learning")

# Which model_ab golden task scores each switchable role (§17.805 — all 8 now
# covered). Three roles have a clean, role-specific objective signal
# (codegen/verifier/extraction). The `routing` task (route_command intent-match)
# scores the two classification-shaped roles (router, general — general's real
# STRUCTURED job is classification; this does NOT measure its open-ended
# synthesis quality, which needs an LLM judge). The remaining three are
# substitute/availability roles with no distinct skill, so they map to a
# capability PROXY task — their proposals are lower-signal and the human confirm
# card remains the gate. model_fallback is a LOCAL resilience role: A/B it only
# against LOCAL candidates (a :cloud winner would break offline fallback — the
# cycle warns, see run_learning_cycle).
ROLE_TASKS: dict[str, str] = {
    "model_coder": "codegen",
    "model_verifier": "verifier",
    "model_research_extract": "extraction",
    "model_router": "routing",           # real job: route_command classification
    "model_general": "routing",          # real structured job: assist_classify/decide
    "model_cloud_heavy": "codegen",      # proxy: escalation = hard-node capability
    "model_cloud_alt": "codegen",        # proxy: alternate heavy-cloud capability
    "model_fallback": "routing",         # proxy: light capability; LOCAL candidates only
    "model_triage": "routing",           # §17.791 real job: conversational intent classification
}


# ── Decision rule (shared with app/scheduler.py's §17.578 log path) ──────────

def _rate(summary: dict, model: str) -> float:
    """Pass rate over SCORED trials (trials minus hard errors), 0 if none."""
    s = summary.get(model, {})
    scored = s.get("trials", 0) - s.get("errors", 0)
    return (s.get("passed", 0) / scored) if scored else 0.0


def _wall(summary: dict, model: str) -> float:
    """Mean wall-clock seconds over the model's trials, 0 if none recorded."""
    ws = summary.get(model, {}).get("wall_s", [])
    return mean(ws) if ws else 0.0


def select_winner(models: list[str], summary: dict) -> dict | None:
    """§17.578 rule: does a candidate beat the incumbent (``models[0]``) clean?

    A winner must have equal-or-better pass rate, zero errors, AND be strictly
    faster (incumbent wall > 0 so an unmeasured incumbent never yields a
    spurious speedup). Returns the decision dict or ``None`` when the incumbent
    holds. Pure function — no I/O — so it is trivially unit-testable and reused
    by the scheduler's ``_log_model_ab_recommendation``.
    """
    if not models:
        return None
    incumbent = models[0]
    inc_rate, inc_wall = _rate(summary, incumbent), _wall(summary, incumbent)
    best = max(models, key=lambda m: (_rate(summary, m), -_wall(summary, m)))
    best_wall = _wall(summary, best)
    if (
        best != incumbent
        and summary.get(best, {}).get("errors", 0) == 0
        and _rate(summary, best) >= inc_rate
        and best_wall < inc_wall
        and inc_wall > 0
    ):
        return {
            "incumbent": incumbent,
            "candidate": best,
            "incumbent_rate": round(inc_rate, 4),
            "candidate_rate": round(_rate(summary, best), 4),
            "speedup": round(inc_wall / best_wall, 3) if best_wall else 0.0,
        }
    return None


# ── Proposal persistence ─────────────────────────────────────────────────────

_SUPERSEDE_OPEN = text(
    "UPDATE model_role_proposals SET status = 'superseded', decided_at = now() "
    "WHERE role = :role AND status = 'open'"
)
_INSERT_OPEN = text(
    "INSERT INTO model_role_proposals "
    "(role, task, incumbent_model, candidate_model, incumbent_rate, "
    " candidate_rate, speedup, evidence, status) "
    "VALUES (:role, :task, :incumbent, :candidate, :inc_rate, :cand_rate, "
    "        :speedup, CAST(:evidence AS JSONB), 'open') "
    "RETURNING id"
)
_LIST_OPEN = text(
    "SELECT id, role, task, incumbent_model, candidate_model, incumbent_rate, "
    "       candidate_rate, speedup, created_at "
    "FROM model_role_proposals WHERE status = 'open' ORDER BY created_at DESC"
)
_GET = text(
    "SELECT id, role, task, incumbent_model, candidate_model, incumbent_rate, "
    "       candidate_rate, speedup, status, created_at, decided_at "
    "FROM model_role_proposals WHERE id = :id"
)
_MARK = text(
    "UPDATE model_role_proposals SET status = :status, decided_at = now() "
    "WHERE id = :id AND status = 'open'"
)


def _proposal_dict(row) -> dict:
    """Serialize a proposal row (RowMapping) to a JSON-friendly dict."""
    m = row._mapping if hasattr(row, "_mapping") else row
    out: dict = {}
    for k, v in m.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


async def _stage_proposal(
    db, role: str, task: str, decision: dict, summary: dict
) -> int:
    """Supersede any open proposal for ``role`` then insert the new one.

    One transaction: the partial-unique-on-open index (migration 065) makes the
    supersede-then-insert atomic-per-role — no window with two live proposals.
    """
    await db.execute(_SUPERSEDE_OPEN, {"role": role})
    # Keep only the two candidates' summaries as evidence (compact, auditable).
    evidence = {
        m: summary.get(m, {})
        for m in (decision["incumbent"], decision["candidate"])
    }
    res = await db.execute(
        _INSERT_OPEN,
        {
            "role": role,
            "task": task,
            "incumbent": decision["incumbent"],
            "candidate": decision["candidate"],
            "inc_rate": decision["incumbent_rate"],
            "cand_rate": decision["candidate_rate"],
            "speedup": decision["speedup"],
            "evidence": json.dumps(evidence),
        },
    )
    new_id = res.scalar()
    await db.commit()
    return new_id


# ── The learning cycle (scheduler entrypoint) ────────────────────────────────

async def run_learning_cycle(db) -> dict:
    """Score candidate models per role and stage proposals for clean winners.

    Candidates come from ``settings.model_role_learning_candidates`` (per-role
    extra tags); a role with no configured candidates is skipped (the incumbent
    alone can't be A/B'd). Fail-soft per role — one role's harness error never
    aborts the others. Returns a summary of what happened for logging/tests.
    """
    candidates_cfg: dict = settings.model_role_learning_candidates or {}
    staged: list[dict] = []
    skipped: list[str] = []

    # Import here (not at module load) so the LLM/HTTP stack is only pulled in
    # when the cycle actually runs — mirrors _execute_model_ab_job.
    from app.utils.http_clients import init_clients
    from scripts.model_ab import run_model_ab_task

    for role, task in ROLE_TASKS.items():
        if role not in SWITCHABLE_ROLE_FIELDS:      # defensive; ROLE_TASKS is curated
            continue
        extras = [c.strip() for c in (candidates_cfg.get(role) or []) if c and c.strip()]
        # §17.805 — model_fallback is a LOCAL offline-resilience role; a cloud
        # winner would defeat its purpose. Warn (don't block — the confirm card
        # is the gate and the operator may have a reason), so a resilience-
        # breaking swap can never slip in silently.
        if role == "model_fallback":
            cloud = [c for c in extras if ":cloud" in c]
            if cloud:
                logger.warning(
                    'event="model_role_learning_fallback_cloud_candidate" '
                    'role=model_fallback candidates=%s '
                    'note="a cloud winner breaks offline resilience"', cloud,
                )
        incumbent = get_model(role)
        models = [incumbent] + [c for c in extras if c != incumbent]
        if len(models) < 2:
            logger.info(
                'event="model_role_learning_skip" role=%s reason="no_candidates"', role
            )
            skipped.append(role)
            continue
        try:
            init_clients()
            result = await asyncio.wait_for(
                run_model_ab_task(
                    task, models, repeat=settings.model_role_learning_repeat
                ),
                timeout=settings.scheduler_job_timeout,
            )
            decision = select_winner(models, result["summary"])
            if decision is None:
                logger.info(
                    'event="model_role_learning_no_change" role=%s task=%s '
                    'incumbent=%s', role, task, incumbent
                )
                continue
            pid = await _stage_proposal(db, role, task, decision, result["summary"])
            logger.warning(
                'event="model_role_proposal_staged" id=%s role=%s task=%s '
                'incumbent=%s candidate=%s inc_rate=%.2f cand_rate=%.2f speedup=%.2fx',
                pid, role, task, decision["incumbent"], decision["candidate"],
                decision["incumbent_rate"], decision["candidate_rate"],
                decision["speedup"],
            )
            staged.append({"id": pid, "role": role, "candidate": decision["candidate"]})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-soft governance job
            logger.exception(
                'event="model_role_learning_failed" role=%s err=%s', role, exc
            )

    logger.info(
        'event="model_role_learning_cycle_done" staged=%d skipped=%d',
        len(staged), len(skipped),
    )
    return {"staged": staged, "skipped": skipped}


async def tick() -> None:
    """APScheduler entrypoint — one learning cycle. Fail-soft (governance)."""
    try:
        async with async_session() as db:
            await run_learning_cycle(db)
    except asyncio.CancelledError:
        logger.warning('event="model_role_learning_tick_cancelled"')
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception('event="model_role_learning_tick_failed" err=%s', exc)


# ── Review / apply helpers (called by app/routers/model_proposals.py) ────────

async def list_open_proposals(db) -> list[dict]:
    res = await db.execute(_LIST_OPEN)
    return [_proposal_dict(r) for r in res.fetchall()]


async def get_proposal(pid: int, db) -> dict | None:
    res = await db.execute(_GET, {"id": pid})
    row = res.fetchone()
    return _proposal_dict(row) if row is not None else None


async def accept_proposal(pid: int, db) -> dict | None:
    """Apply an open proposal's swap and mark it accepted.

    Applies FIRST via ``set_override`` (the durable, restart-surviving path that
    also mutates the live settings singleton), THEN marks the row accepted. If
    the mark fails after the override took, the swap still stands (re-accepting
    is idempotent). Returns ``None`` if the proposal is missing or not open.
    """
    prop = await get_proposal(pid, db)
    if prop is None or prop.get("status") != "open":
        return None
    role, model = prop["role"], prop["candidate_model"]
    await set_override(role, model, db)     # validates, persists, commits, mutates settings
    await db.execute(_MARK, {"id": pid, "status": "accepted"})
    await db.commit()
    logger.info(
        'event="model_role_proposal_accepted" id=%s role=%s model=%s', pid, role, model
    )
    return {"id": pid, "role": role, "model": model, "applied": True}


async def dismiss_proposal(pid: int, db) -> dict | None:
    """Mark an open proposal dismissed (no override written)."""
    res = await db.execute(_MARK, {"id": pid, "status": "dismissed"})
    await db.commit()
    if res.rowcount == 0:
        return None
    logger.info('event="model_role_proposal_dismissed" id=%s', pid)
    return {"id": pid, "dismissed": True}
