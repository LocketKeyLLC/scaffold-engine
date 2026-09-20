"""§17.1081 — the engine's own optional capabilities, as recipes it walks
the operator through.

Every optional capability (local runner, queue worker, reranker sidecar,
strict step rulebook, sudo on the runner) shipped OFF with a written reason
and a hand-written page telling the operator how to turn it on. That page
was the engine's job done by someone else: assist mode IS the walkthrough
product — plan → per-step guidance → paste verification → state check — and
a page beside it rots, bypasses the verification, and hides every gap the
engine would have exposed on its own setup.

So the recipes live HERE, as briefs the engine turns into a job through the
same door as any operator idea (`create_ideation_job` → Phase 1 refine →
approve with Assist → walkthrough). Each recipe also knows how to DETECT
whether it is already on, so the console shows the truth, not a checklist.
Developer-side chores (dependency PRs, CVE re-scans, CI) are deliberately
not here — they are the maintainer's, not the engine's.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text

from app.modules.job_state import TERMINAL_JOB_STATUSES

logger = logging.getLogger("scaffold")

# Statuses the console renders. "on"/"off" come from detection; "blocked"
# means a prerequisite recipe is off; "in_progress" means a job for it is
# still open; "manual" means the engine cannot observe it (it lives on the
# operator's other machine) and only the walkthrough can confirm it.
STATUSES = ("on", "off", "blocked", "in_progress", "manual")

# (name is historical — these are the statuses a job is NOT open in)
_OPEN_JOB_STATUSES = tuple(sorted(TERMINAL_JOB_STATUSES))  # §17.1107 — one vocabulary


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    summary: str            # one plain sentence: what you get
    why_off: str            # why it ships off, in plain words
    effort: str             # "15 min"
    brief: str              # the idea the engine refines into a plan
    requires: tuple[str, ...] = ()
    detect: Optional[Callable[..., Awaitable[tuple[str, str]]]] = field(default=None, compare=False)


# ---------------------------------------------------------------------------
# Detection — (status, detail). Each reads the live settings + DB; none of
# them call out to the operator's other machines.
# ---------------------------------------------------------------------------

async def _detect_local_runner(db) -> tuple[str, str]:
    from app.config import settings
    name = (settings.assist_local_runner_server or "").strip()
    if not name:
        return "off", "ASSIST_LOCAL_RUNNER_SERVER is empty — the state check asks you to paste."
    try:
        from app.modules.mcp_registry import get_server
        spec = await get_server(db, name)
    except Exception as exc:
        return "off", f"registry lookup failed: {exc}"
    if spec is None or not spec.enabled:
        return "off", f"ASSIST_LOCAL_RUNNER_SERVER={name} but no enabled MCP server of that name is registered."
    return "on", f"probes run through '{name}' at {spec.endpoint or spec.command}."


async def _detect_runner_sudo(db) -> tuple[str, str]:
    st, _ = await _detect_local_runner(db)
    if st != "on":
        return "blocked", "Turn on the local runner first."
    return "manual", "Lives on the runner's machine (sudoers + --sudo-allow); the walkthrough confirms it with `sudo -n`."


async def _detect_queue(db) -> tuple[str, str]:
    from app.config import settings
    if settings.queue_enabled:
        return "on", f"the queue worker owns the background chores (cleanup on {settings.queue_cleanup_cron})."
    return "off", "background chores run inside the engine process."


async def _detect_reranker(db) -> tuple[str, str]:
    from app.config import settings
    if (settings.reranker_backend or "local").lower() in ("http", "tei"):
        return "on", f"re-ranking runs in the sidecar at {settings.reranker_url}."
    return "off", "re-ranking runs inside the engine process (~6 s per query, CPU-bound)."


async def _detect_fsm_strict(db) -> tuple[str, str]:
    from app.config import settings
    if settings.assist_step_fsm_strict:
        return "on", "illegal step moves are refused."
    return "off", "illegal step moves are logged (step_fsm_violation) and allowed."


# ---------------------------------------------------------------------------
# The recipes. Briefs are written as the operator's own idea: concrete
# machines, paths, ports and names, so refine → research → plan has facts to
# stand on instead of guessing them. They are the only place these facts
# live; the console renders them and the job carries them.
# ---------------------------------------------------------------------------

_ENGINE_HOST = ("The engine host is this machine: the repo is at ~/scaffold-engine, the engine answers at "
                "http://localhost:8000, its settings live in ~/scaffold-engine/.env (restart after editing with: "
                "cd ~/scaffold-engine && docker compose up -d scaffold-orchestrator), and the API key is the "
                "SCAFFOLD_API_KEY value in that file, sent as the X-API-Key header.")

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="local_runner",
        title="Let the state check run its own commands",
        summary="Verify state runs its read-only checks through a small helper on the target machine instead of asking you to paste.",
        why_off="The engine's rule is that it never touches your machine; this is the one fenced exception, and only you can open it.",
        effort="30 min",
        requires=(),
        detect=_detect_local_runner,
        brief=(
            "Set up the scaffold-engine local runner so the assist state check can execute its own read-only "
            "probes instead of asking me to paste command output.\n\n"
            + _ENGINE_HOST + "\n\n"
            "The TARGET machine is the one my plans are about (ask me for its IP/hostname and the user I log in as). "
            "Steps:\n"
            "1. On the target machine: copy ~/scaffold-engine/scripts/local_runner_mcp.py from the engine host "
            "(scp is fine), create a Python venv (python3 -m venv ~/runner-venv), install \"mcp>=2.0\" uvicorn "
            "starlette into it, choose a long random secret token, and start the helper with: "
            "~/runner-venv/bin/python local_runner_mcp.py --host 0.0.0.0 --port 8790 --token <secret>. "
            "It must keep running after I log out (a systemd service or nohup is fine). Only the engine host "
            "needs to reach port 8790.\n"
            "2. On the engine host: register it with POST http://localhost:8000/mcp/servers (header X-API-Key) "
            "and JSON body {\"name\":\"pve-runner\",\"transport\":\"streamable_http\","
            "\"endpoint\":\"http://<target-ip>:8790/mcp/\",\"headers\":{\"X-Runner-Token\":\"<secret>\"}}. "
            "Then GET http://localhost:8000/mcp/servers/pve-runner/tools must list a tool named run_readonly.\n"
            "3. On the engine host: add the line ASSIST_LOCAL_RUNNER_SERVER=pve-runner to ~/scaffold-engine/.env, "
            "restart the engine, and confirm curl -s localhost:8000/health reports \"healthy\".\n"
            "4. Verify: in an assist session press Verify state; the reply must say \"Running N read-only checks "
            "through your local runner (pve-runner)\" instead of asking me to paste.\n"
            "The helper refuses anything that is not read-only; nothing here changes the target machine."
        ),
    ),
    Recipe(
        id="runner_sudo",
        title="Give the runner administrator rights for specific commands",
        summary="Checks that need root (nginx -t, pct config …) stop coming back as 'unknown'.",
        why_off="Root access is a decision for the machine's owner; the runner is unprivileged until you list exact commands.",
        effort="20 min",
        requires=("local_runner",),
        detect=_detect_runner_sudo,
        brief=(
            "Allow the scaffold-engine local runner (already running on my target machine on port 8790 as an "
            "unprivileged user) to run a short list of READ-ONLY commands as root, so state-check probes like "
            "'nginx -t' and 'pct config <id>' return real output instead of 'a password is required'.\n\n"
            "Two things must agree on the target machine: (a) a sudoers rule for the user that runs the helper, "
            "written with sudo visudo -f /etc/sudoers.d/scaffold-runner, one line of the form "
            "<user> ALL=(root) NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/pct config * — full paths (find them with "
            "which nginx), one entry per command, a trailing * only where arguments follow; and (b) the helper "
            "restarted with the matching allow-list: ~/runner-venv/bin/python local_runner_mcp.py --host 0.0.0.0 "
            "--port 8790 --token <secret> --sudo-allow \"nginx -t\" \"pct config\". Ask me which commands I want "
            "before writing the rule.\n"
            "Verify on the target machine that sudo -n nginx -t prints nginx's 'syntax is ok' lines and not a "
            "password prompt. Anything not on the list still runs unprivileged and the helper says so in its output."
        ),
    ),
    Recipe(
        id="queue_worker",
        title="Move background chores to the queue worker",
        summary="Cleanup, health evaluations, the weekly model self-evaluation and stuck-turn closing run in a separate container with retries and a record of every run.",
        why_off="A second container is a deployment decision; the in-process loops work and the queue path was proven but is new.",
        effort="20 min",
        requires=(),
        detect=_detect_queue,
        brief=(
            "Move scaffold-engine's background chores onto its procrastinate queue worker.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps, all on the engine host in ~/scaffold-engine:\n"
            "1. make queue-schema — creates the queue's own tables in the composed Postgres (safe to repeat).\n"
            "2. make queue-once — defers the cleanup sweep once and runs a one-shot worker; every line must end "
            "with 'ended with status: Success'. Note the weekly model self-evaluation task makes real model calls "
            "and can take over ten minutes if it decides it is due.\n"
            "3. Add QUEUE_ENABLED=true to ~/scaffold-engine/.env, then: docker compose --profile queue up -d queue "
            "(the service queue already exists in ~/scaffold-engine/docker-compose.yml under the queue profile) "
            "and docker compose up -d scaffold-orchestrator.\n"
            "4. Verify: docker logs scaffold-orchestrator --since 2m 2>&1 | grep -c delegated_to_queue prints 2 "
            "(the cleanup loop and the three evaluation jobs both reported delegation), and docker logs "
            "scaffold-queue --since 5m shows the worker starting; within 15 minutes it runs its first chore.\n"
            "Rollback: QUEUE_ENABLED=false, restart the engine, docker compose --profile queue stop queue."
        ),
    ),
    Recipe(
        id="reranker_sidecar",
        title="Move the slow search step to its own container",
        summary="The relevance re-sort (about 6 s a query, all the CPU it can get) stops stalling everything else.",
        why_off="It is a second container on the same host; the in-process reranker works, just slowly.",
        effort="15 min",
        requires=(),
        detect=_detect_reranker,
        brief=(
            "Run scaffold-engine's reranker as its sidecar container instead of inside the engine process.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps on the engine host in ~/scaffold-engine:\n"
            "1. docker compose --profile reranker up -d scaffold-reranker — the service scaffold-reranker already "
            "exists in ~/scaffold-engine/docker-compose.yml under the reranker profile (the engine image with the "
            "same model; the first start loads the model, allow a minute).\n"
            "2. Add RERANKER_BACKEND=http and RERANKER_URL=http://scaffold-reranker:80 to ~/scaffold-engine/.env, "
            "then docker compose up -d scaffold-orchestrator.\n"
            "3. Verify: curl -s localhost:8000/health | grep -o '\"reranker\":{[^}]*}' shows \"status\":\"up\"; "
            "a knowledge search still returns results and the engine stays responsive while one runs.\n"
            "Rollback: RERANKER_BACKEND=local, restart the engine, docker compose --profile reranker stop scaffold-reranker."
        ),
    ),
    Recipe(
        id="step_fsm_strict",
        title="Make the step rulebook strict",
        summary="Illegal step-state moves are refused instead of logged and allowed.",
        why_off="Refusing is safer only once the warnings have stayed at zero for a week of real use.",
        effort="5 min, after a week of zero warnings",
        requires=(),
        detect=_detect_fsm_strict,
        brief=(
            "Switch scaffold-engine's assist step state machine to strict mode, but only after confirming it has "
            "logged no violations in a week of normal use.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps on the engine host:\n"
            "1. docker logs scaffold-orchestrator --since 168h 2>&1 | grep -c step_fsm_violation — must print 0. "
            "If it is not 0, STOP: show me the lines (same command without -c); each one is a bug to fix before "
            "going strict, not something to override.\n"
            "2. When it is 0: add ASSIST_STEP_FSM_STRICT=true to ~/scaffold-engine/.env and restart the engine.\n"
            "3. Verify: curl -s localhost:8000/health reports \"healthy\", and one ordinary assist step "
            "(claim → paste → done) still commits normally."
        ),
    ),
)

BY_ID: dict[str, Recipe] = {r.id: r for r in RECIPES}


# ---------------------------------------------------------------------------
# Status view + start.
# ---------------------------------------------------------------------------

async def _open_jobs_by_recipe(db) -> dict[str, dict]:
    """{recipe_id: {job_id, status}} for the newest job per recipe, so the
    console can link 'in progress' to the walkthrough and 'off' to the last
    attempt."""
    try:
        rows = (await db.execute(text("""
            SELECT DISTINCT ON (metadata->>'setup_recipe') metadata->>'setup_recipe' AS rid, id::text AS job_id, status
              FROM jobs WHERE metadata ? 'setup_recipe'
             ORDER BY metadata->>'setup_recipe', created_at DESC
        """))).mappings().all()
    except Exception as exc:
        logger.warning("engine_setup_jobs_lookup_failed err=%r", exc)
        return {}
    return {r["rid"]: {"job_id": r["job_id"], "status": r["status"]} for r in rows if r["rid"]}


async def list_recipes(db) -> list[dict]:
    jobs = await _open_jobs_by_recipe(db)
    out: list[dict] = []
    for r in RECIPES:
        status, detail = await r.detect(db) if r.detect else ("manual", "")
        job = jobs.get(r.id)
        if status != "on" and job and job["status"] not in _OPEN_JOB_STATUSES:
            status, detail = "in_progress", f"a walkthrough is open (job {job['job_id'][:8]}…, {job['status']})."
        out.append({
            "id": r.id, "title": r.title, "summary": r.summary, "why_off": r.why_off, "effort": r.effort,
            "requires": list(r.requires), "status": status, "status_detail": detail,
            "job_id": job["job_id"] if job else None, "job_status": job["status"] if job else None,
        })
    return out


async def start_recipe(db, recipe_id: str, *, owner: Optional[str]) -> dict:
    """Open the walkthrough: the recipe's brief goes through the same door as
    any operator idea. Raises KeyError for an unknown id and ValueError when
    a prerequisite recipe is not on."""
    r = BY_ID[recipe_id]
    for dep in r.requires:
        detect = BY_ID[dep].detect
        if detect is None:  # §17.1141 — a prerequisite without a detector cannot be verified
            raise ValueError(f"'{BY_ID[dep].title}' must be on first (no detector to confirm it)")
        st, _ = await detect(db)
        if st != "on":
            raise ValueError(f"'{BY_ID[dep].title}' must be on first")
    from app.modules.idea_refinement import create_ideation_job
    from app.modules.ideation_workflow import spawn_phase1_background
    job_id = await create_ideation_job(r.brief, db, owner=owner)
    await db.execute(text("""
        UPDATE jobs SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :jid
    """), {"m": json.dumps({"setup_recipe": r.id, "prescriptive": True}), "jid": job_id})
    await db.commit()
    spawn_phase1_background(job_id, r.brief)
    logger.info("engine_setup_started recipe=%s job_id=%s", r.id, job_id)
    return {"job_id": job_id, "status": "refining", "recipe": r.id}


async def job_is_prescriptive(db, job_id: str) -> bool:
    """§17.1081b — a PRESCRIPTIVE brief already lists its exact steps: the
    research phase must not branch it into options and the planner must not
    open it with a decision node or an 'inspect/assess' prelude. Live: the
    reranker recipe planned as 'Decide reranker wiring approach' with three
    invented options (one violating its own constraint) and 'Add reranker
    service definition' for a service that already exists. Stamped in
    jobs.metadata by `start_recipe`; any caller may set it."""
    try:
        meta = (await db.execute(text("SELECT metadata FROM jobs WHERE id = :jid"), {"jid": job_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        return bool(isinstance(meta, dict) and meta.get("prescriptive"))
    except Exception as exc:
        logger.warning("prescriptive_lookup_failed job=%s err=%r", job_id, exc)
        return False


PRESCRIBED_BLOCK = """PRESCRIBED PLAN — the operator's brief already lists the exact steps, in order, with the commands to run.
Emit ONE node per listed step, in the brief's order, carrying the brief's own commands and names verbatim.
Do NOT add: decision nodes, "decide/choose/assess/inspect/evaluate options" preludes, alternatives to what
the brief prescribes, or steps that create/download/define things the brief says already exist.
Keep every verification the brief names as its own node. If a listed step is destructive or unclear, keep it
as written and say so in its notes — do not replace it with your own design.

"""


def runner_nudge() -> str:
    """One line for the state check's paste request when no runner is set:
    the moment the operator feels the cost is the moment to say the engine
    can carry it."""
    from app.config import settings
    if (settings.assist_local_runner_server or "").strip():
        return ""
    return ("\n\n_The engine can run these checks itself through a small helper on your machine — "
            "**Capabilities → Let the state check run its own commands** in the console walks you through it._")
