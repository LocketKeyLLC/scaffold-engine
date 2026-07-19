"""Final-output compilation for execution_agent.

Assembles the deliverable from completed dag_nodes using three ordered
strategies:

  Strategy 0  explicit ``is_output_node`` markers (set by dag_generator from
              the leaf-set of the DAG). One leaf done → that's the deliverable.
              Multiple leaves done → joined with horizontal rules.
  Strategy 2  last terminal-order CodeGen node is the deliverable. Triggers
              for code-producing DAGs that didn't carry an explicit leaf marker.
  Strategy 3  concat-all-done-with-headers. Fallback for partial completion
              and LLM-only DAGs without an explicit leaf marker. Strategy 3
              prepends a "Partial deliverable" preamble so consumers can tell
              this isn't a clean Strategy-0 result, and truncates per-node
              proportionally if the total exceeds settings.compile_output_max_chars.

Sprint W.7: when ``settings.compile_synthesis_enabled`` is True, the heuristic
output is fed through an LLM post-processor that rewrites the sectioned dump
into a coherent narrative. Default OFF (opt-in). CodeGen-deliverable jobs
(Strategy 2 with tool='CodeGen' source) skip synthesis even when enabled —
executable code passes through verbatim.

Empty result (no done node contributed any output) returns ``None`` rather
than ``""`` so callers can store ``compiled_output=NULL`` — the semantically
correct state for "we never produced output" vs. "we produced an empty string".
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

from app.config import settings
from app.providers.base import Tool

logger = logging.getLogger("scaffold.execution_compile")


SYNTHESIS_TOOL = Tool(
    name="render_summary",
    description=(
        "Rewrite the heuristic compiled output into a coherent narrative "
        "that preserves every concrete fact, name, number, and code block "
        "while removing redundancy across sections. Do not invent values "
        "(IPs, keys, hostnames, hashes, paths) that are not in the source; "
        "preserve placeholders verbatim."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "The synthesized narrative. Plain prose that flows "
                    "section-to-section. No section headers, no horizontal "
                    "rules, no preamble. Preserve code blocks verbatim "
                    "inside their original triple-backtick fences. "
                    "Preserve any placeholders (<PROXMOX_HOST_IP>, "
                    "${VAR}, {{x}}) verbatim — do not fill them in."
                ),
            },
        },
        "required": ["summary"],
    },
)


SYNTHESIS_SYSTEM = """You are a workflow-output synthesizer. Given the original \
project goal and a heuristic-compiled deliverable that stitches together outputs \
from multiple DAG nodes (with section headers), rewrite it into a coherent \
narrative.

RULES:
- Preserve every concrete fact, name, number, version, or specification. Do not \
summarize away content.
- Preserve any code blocks verbatim inside their original triple-backtick fences. \
Code is the deliverable, not narrative.
- Remove redundancy: if two sections describe the same thing in different words, \
merge them.
- Remove the section headers (## T1: …) and horizontal rules — produce flowing \
prose with topic transitions.
- Drop any 'Partial deliverable' preamble, but keep the substantive content.
- Length: roughly the same total length as the input (don't compress aggressively).
- Report your output by calling the render_summary tool. Do NOT respond with \
prose; the deliverable must come from the tool call.

VALUE-FABRICATION GUARDS (§17.360 — closes the synthesizer hole left by §17.359):
- DO NOT invent IPs, hostnames, MAC addresses, ports, auth keys, API tokens, \
SSH keys, password hashes, file paths, container IDs, version numbers, dates, \
or any other concrete value that is NOT explicitly present in the source \
sections. The synthesizer's job is rewriting, not filling in.
- Preserve placeholders VERBATIM — strings like `<PROXMOX_HOST_IP>`, \
`<TAILSCALE_AUTH_KEY>`, `${VAR_NAME}`, `{{value}}`, `<...>`, `your-host-here` \
must appear in the output exactly as they appear in the source. Do not replace \
them with example-looking values (`192.168.1.10`, `tskey-abc123…`, \
`pve01.internal`); those are fabrication, not synthesis.
- If a section says "Inputs needed: X" or marks a value as unknown, propagate \
that "needed/unknown" status into the narrative. Never invent the value to \
make the prose flow more naturally.
- Capability boundary: you do not have shell access; you have not executed \
any command in the source. Do not introduce past-tense narration that wasn't \
already in the source ("Created the file", "Installed the package", \
"tcpdump shows…", "Verified at /var/log/…"). If the source frames a step as \
"Run this: <cmd>", the synthesis must keep that forward-tense framing — do \
not rewrite it as "We ran <cmd> and got X"."""


SYNTHESIS_PROMPT = """PROJECT GOAL:
{goal}

HEURISTIC-COMPILED DELIVERABLE (sectioned, possibly redundant):
{heuristic}

Rewrite the deliverable into a coherent narrative per the rules. Return ONLY \
the tool call."""


async def _synthesize_compiled_output(
    *,
    job_id: str,
    heuristic: str,
    source_strategy: str,
    source_tool: str | None,
    db,
    model_overrides: dict | None = None,
) -> str | None:
    """Run an LLM post-processor that rewrites a sectioned heuristic output
    into a coherent narrative.

    Sprint W.7. Fail-open: any LLM/parse failure returns ``None`` and the
    caller falls back to the heuristic body unchanged.

    Verbatim-tool guard: ``source_tool in {'CodeGen', 'Shell'}`` short-circuits
    without an LLM call. The deliverable IS executable code (CodeGen) or a
    host runbook the human will execute (Shell, §17.359); rewriting either as
    prose would silently corrupt the output. §17.360 generalizes the original
    W.7 CodeGen-only guard to Shell after the homelab retry surfaced the
    synthesizer fabricating concrete values (`tskey-abc123…`, hardcoded IPs)
    in place of the runbook's `<PLACEHOLDER>` tokens. Logged at INFO so
    operators can spot when the guard fired.
    """
    if source_tool in ("CodeGen", "Shell"):
        logger.info(
            "compile_synthesis_skip_verbatim: job=%s strategy=%s tool=%s",
            job_id, source_strategy, source_tool,
        )
        return None
    if not heuristic or not heuristic.strip():
        return None

    # Fetch the project goal from the job's refined_brief.
    row = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :jid"),
        {"jid": job_id},
    )).mappings().first()
    # §17.619 (audit #12) — release the pooled DB connection BEFORE the
    # multi-minute synthesis LLM call (and the faithfulness/CoVe grounding gate
    # that follows in _maybe_synthesize, which uses its own session). This
    # refined_brief SELECT is the LAST DB read in the compile path — the node
    # read (_compile_output), the synthesis-override read
    # (_resolve_synthesis_enabled), and this one are all SELECT-only, and every
    # caller commits its writes before invoking _compile_output. Committing here
    # therefore finalizes a read-only transaction and returns the connection to
    # the pool instead of pinning it across the model round-trip (the
    # no-session-across-LLM policy the §17.598 artifact isolation established).
    # The caller re-acquires a connection for its compiled_output UPDATE after
    # _compile_output returns.
    await db.commit()
    raw_brief = (row or {}).get("refined_brief") or {}
    if isinstance(raw_brief, str):
        try:
            raw_brief = json.loads(raw_brief)
        except (ValueError, TypeError):
            raw_brief = {}
    goal = ""
    if isinstance(raw_brief, dict):
        goal = (raw_brief.get("description") or "").strip()
        if not goal:
            goals = raw_brief.get("goals") or []
            if isinstance(goals, list) and goals:
                goal = str(goals[0])

    prompt = SYNTHESIS_PROMPT.format(
        goal=goal or "(unspecified)",
        heuristic=heuristic,
    )

    # Defer the import to avoid a circular dependency at module-import time.
    from app import model_router
    from app.utils.cost_tracking import call_kind

    route_kwargs = {"role": "model_general"}
    if model_overrides:
        route_kwargs["overrides"] = model_overrides

    # §17.90 — tag this LLM call as "synthesis" so the cost rollup can
    # split synthesis spend from execution spend. Without the tag the
    # row inserts with call_kind=NULL and falls into the rollup's
    # "uncategorized" bucket.
    try:
        with call_kind("synthesis"):
            resp = await model_router.tool_call(
                messages=[
                    {"role": "system", "content": SYNTHESIS_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                tools=[SYNTHESIS_TOOL],
                temperature=0.2,
                max_tokens=settings.compile_synthesis_max_tokens,
                **route_kwargs,
            )
    except Exception as exc:
        logger.warning(
            "compile_synthesis_call_failed: job=%s error=%s", job_id, exc,
        )
        return None

    if not resp.success:
        logger.warning(
            "compile_synthesis_response_unsuccessful: job=%s error=%s",
            job_id, resp.error,
        )
        return None

    calls = getattr(resp, "tool_calls", None) or []
    if not calls:
        logger.warning(
            "compile_synthesis_no_tool_call: job=%s text=%r",
            job_id, (resp.text or "")[:200],
        )
        return None

    args = calls[0].arguments
    if not isinstance(args, dict):
        logger.warning(
            "compile_synthesis_args_not_dict: job=%s args=%r",
            job_id, str(args)[:200],
        )
        return None

    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        logger.warning(
            "compile_synthesis_empty_summary: job=%s args=%r",
            job_id, str(args)[:200],
        )
        return None

    logger.info(
        "compile_synthesis_done: job=%s strategy=%s in_chars=%d out_chars=%d",
        job_id, source_strategy, len(heuristic), len(summary),
    )
    return summary


def _truncate(content: str, max_chars: int) -> str:
    """Mirror execution_agent._truncate_output but local to avoid an import
    cycle. Preserves first/last 20% with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    head = int(max_chars * 0.2)
    tail = int(max_chars * 0.2)
    removed = len(content) - head - tail
    return (
        content[:head]
        + f"\n[...truncated {removed} chars...]\n"
        + content[-tail:]
    )


def _format_section(node: dict) -> str:
    return f"## {node['node_key']}: {node['title']}\n\n{node['output_text']}"


def _join_sections(sections: list[str]) -> str:
    return "\n\n---\n\n".join(sections)


async def _resolve_synthesis_enabled(job_id: str, db) -> bool:
    """Sprint X.6 — per-job override takes precedence over the global
    setting. Returns True iff synthesis should run for this job.

    Resolution:
      - jobs.compile_synthesis_override = TRUE  → True (force on)
      - jobs.compile_synthesis_override = FALSE → False (force off)
      - jobs.compile_synthesis_override IS NULL → settings.compile_synthesis_enabled

    The SELECT is a single round-trip; on any DB error (job missing,
    connection drop) we fail open to the global setting, matching the
    fail-open pattern that already governs synthesis itself.
    """
    try:
        row = await db.execute(
            text("SELECT compile_synthesis_override FROM jobs WHERE id = :jid"),
            {"jid": job_id},
        )
        override = row.scalar()
    except Exception as exc:
        logger.debug(
            "synthesis_override_read_failed: job=%s error=%s "
            "(falling through to global setting)",
            job_id, exc,
        )
        return settings.compile_synthesis_enabled
    if override is None:
        return settings.compile_synthesis_enabled
    return bool(override)


def _format_grounding_banner(verdict: dict, *, corrected: bool = False) -> str:
    """§17.569 — the ⚠️ low-grounding banner prepended to a synthesized
    deliverable whose claims aren't supported by the source node-work.
    §17.570 — when ``corrected`` a CoVe auto-revision ran but couldn't lift
    grounding past the threshold; note it so the reader knows it was attempted."""
    pct = int(round(verdict.get("score", 0.0) * 100))
    note = " (an auto-revision was attempted but couldn't fully ground it)" if corrected else ""
    lines = [
        f"> ⚠️ **Grounding check:** only {pct}% of this deliverable's claims "
        f"({verdict.get('supported', 0)}/{verdict.get('total', 0)}) are supported "
        f"by the source work{note} — treat the unsupported claims below with caution.",
    ]
    for c in (verdict.get("unsupported_claims") or [])[:5]:
        lines.append(f"> - _unsupported:_ {c}")
    return "\n".join(lines) + "\n\n"


async def _record_grounding_metadata(job_id: str, record: dict) -> None:
    """Best-effort write of jobs.metadata.grounding (own session — never
    touches the caller's transaction; never breaks compile)."""
    try:
        from app.database import async_session
        async with async_session() as mdb:
            await mdb.execute(
                text(
                    "UPDATE jobs SET metadata = COALESCE(metadata, '{}'::jsonb) "
                    "|| jsonb_build_object('grounding', CAST(:v AS jsonb)) "
                    "WHERE id = :jid"
                ),
                {"v": json.dumps(record), "jid": job_id},
            )
            await mdb.commit()
    except Exception as exc:  # best-effort metric — never break compile
        logger.warning("grounding_metadata_write_failed: job=%s err=%s", job_id, exc)


async def _maybe_grounding_gate(
    job_id: str, text_value: str, evidence: str,
) -> str:
    """§17.569/§17.570 — grounding LOOP on a synthesized deliverable: detect
    (faithfulness) → correct (CoVe) → re-verify → flag-if-still-low. Default
    ON, fail-soft, NEVER blocks. A scorer miss (None) is a no-op (no DB write).
    When ``grounding_correct_enabled`` and the deliverable scores below
    ``grounding_min_score``, it CoVe-revises + re-scores before deciding to
    banner — so a low deliverable auto-corrects rather than merely warning.
    """
    if not settings.grounding_gate_enabled:
        return text_value
    from app.modules.faithfulness import score_faithfulness  # circular-safe
    verdict = await score_faithfulness(
        text_value, evidence, role=settings.faithfulness_model_role,
    )
    if verdict is None:
        return text_value  # not scored → no-op (no DB write, no banner)
    score_before = verdict.get("score", 1.0)
    corrected = False

    # §17.570 — when low, CoVe-revise + re-score before deciding to banner.
    if score_before < settings.grounding_min_score and settings.grounding_correct_enabled:
        try:
            from app.modules.cove import cove_revise  # circular-safe
            rev = await cove_revise(
                text_value, evidence, role=settings.cove_model_role,
            )
            if rev and rev.get("changed") and rev.get("revised"):
                rescore = await score_faithfulness(
                    rev["revised"], evidence, role=settings.faithfulness_model_role,
                )
                text_value = rev["revised"]
                corrected = True
                if rescore is not None:
                    verdict = rescore
        except Exception as exc:  # fail-soft — keep the pre-correction text
            logger.warning("grounding_correct_failed: job=%s err=%s", job_id, exc)

    score_after = verdict.get("score", 1.0)
    record = dict(verdict)
    record["corrected"] = corrected
    if corrected:
        record["score_before"] = score_before
        record["score_after"] = score_after
    await _record_grounding_metadata(job_id, record)
    logger.info(
        "grounding_scored: job=%s score=%.2f supported=%d/%d corrected=%s",
        job_id, score_after, verdict.get("supported", 0),
        verdict.get("total", 0), corrected,
    )
    if score_after < settings.grounding_min_score:
        return _format_grounding_banner(verdict, corrected=corrected) + text_value
    return text_value


async def _maybe_synthesize(
    *, job_id: str, heuristic: str | None,
    strategy: str, source_tool: str | None, db,
) -> tuple[str | None, bool]:
    """If synthesis is enabled and the heuristic is non-empty, run the
    LLM post-processor; on failure (or CodeGen guard) return the heuristic
    unchanged.

    Sprint X.2 — returns (text, was_synthesized: bool). was_synthesized is
    True iff the LLM rewrite actually replaced the heuristic. Synthesis
    disabled, CodeGen-guarded, fail-open, and empty-heuristic paths all
    return (text_or_None, False). Lets callers persist the synthesized
    flag on jobs.compiled_output_synthesized.

    Sprint X.6 — synthesis-enabled is now resolved per-job via
    `_resolve_synthesis_enabled`, so a job can opt in/out independently
    of the global `settings.compile_synthesis_enabled` knob.
    """
    if heuristic is None:
        return heuristic, False
    if not await _resolve_synthesis_enabled(job_id, db):
        return heuristic, False
    synthesized = await _synthesize_compiled_output(
        job_id=job_id, heuristic=heuristic,
        source_strategy=strategy, source_tool=source_tool, db=db,
    )
    if synthesized:
        # §17.569 — grounding gate: the synthesis can introduce claims absent
        # from the source work; flag (never block) when unsupported.
        annotated = await _maybe_grounding_gate(job_id, synthesized, heuristic)
        return annotated, True
    return heuristic, False


def _prepend_skipped_banner(text: str | None, skipped_count: int, total: int) -> str | None:
    """Sprint X.2 — when N nodes were skipped during execution, prepend a
    short operational banner so consumers can tell the deliverable doesn't
    cover the full DAG. Sits AFTER synthesis on the call path, so the
    banner survives any LLM rewriting (operational metadata, not narrative
    content).

    Returns the input unchanged when text is None or skipped_count is 0.
    """
    if text is None or skipped_count <= 0:
        return text
    plural = "task" if skipped_count == 1 else "tasks"
    banner = (
        f"_Note: {skipped_count} of {total} {plural} were skipped during "
        f"execution; the deliverable below covers the verified tasks only._"
        f"\n\n---\n\n"
    )
    return banner + text


def _prepend_plan_only_banner(
    text: str | None, runbook_count: int, total: int, job_id: str,
) -> str | None:
    """§17.506 — when N nodes are Shell/runbook steps the engine did NOT
    execute (``shell_tool_enabled`` False, the default), prepend a banner
    making clear the deliverable is a *plan to perform on real systems*, not
    a completed build — and steer the user to Assist Mode, which walks each
    step and records real per-step completion.

    Why: autonomous execution of a hands-on-hardware job (install Proxmox,
    configure a firewall, …) only generates runbooks and marks the nodes
    ``done``, so the job rolls up to ``completed`` and the compiled output
    reads like a finished build when nothing was actually executed. The
    banner closes that "hallucinated completion" gap at the surface the user
    reads. Sits AFTER synthesis (like ``_prepend_skipped_banner``) so it
    survives any LLM rewriting — operational metadata, not narrative.

    Returns the input unchanged when ``text`` is None or ``runbook_count`` is 0.
    """
    if text is None or runbook_count <= 0:
        return text
    plural = "step" if runbook_count == 1 else "steps"
    verb = "is a runbook" if runbook_count == 1 else "are runbooks"
    banner = (
        f"> ⚠️ **PLAN — NOT EXECUTED.** This job includes {runbook_count} of "
        f"{total} {plural} that {verb} of actions to perform on real systems; "
        f"the engine generated them but did **not** run them, so nothing has "
        f"been built or changed. To carry them out with the engine guiding and "
        f"verifying each step, run `/assist {job_id}`."
        f"\n\n---\n\n"
    )
    return banner + text


async def render_plan_preview(job_id: str, db) -> str:
    """§17.624 — render a PARKED job's DAG as a human-executable plan.

    Unlike ``_compile_output`` this reads each node's *plan* (title +
    description), not its output, because a hands-on job parked in
    ``awaiting_assist`` has never run — its nodes are still ``pending`` and
    ``output_text`` is NULL. Ordered by ``execution_order`` so the plan reads
    top-to-bottom the way the operator will perform it under /assist.
    """
    title_row = (await db.execute(
        text("SELECT title FROM jobs WHERE id = :jid"), {"jid": job_id},
    )).mappings().first()
    job_title = (title_row or {}).get("title") or "Plan"
    rows = (await db.execute(
        text(
            "SELECT node_key, title, description, tool "
            "FROM dag_nodes WHERE job_id = :jid "
            "ORDER BY execution_order NULLS LAST, node_key"
        ),
        {"jid": job_id},
    )).mappings().all()
    parts = [f"# {job_title} — Plan", ""]
    for i, r in enumerate(rows, 1):
        tool = r.get("tool") or "LLM"
        parts.append(f"## {i}. {r['title']}")
        parts.append(f"`{r['node_key']}` · tool: `{tool}`")
        desc = (r.get("description") or "").strip()
        if desc:
            parts.append("")
            parts.append(desc)
        parts.append("")
    return "\n".join(parts).strip()


async def compile_awaiting_assist_plan(
    job_id: str, db, *, nonexec_count: int, total: int,
) -> str:
    """§17.624 — the deliverable for a hands-on job PARKED in awaiting_assist:
    the rendered plan with the §17.506 PLAN-NOT-EXECUTED / run-/assist banner
    on top. Reuses the same banner the autonomous plan_only path uses so the
    two surfaces read identically."""
    body = await render_plan_preview(job_id, db)
    return _prepend_plan_only_banner(body, nonexec_count, total, job_id) or body


async def compute_deliverable_kind(
    job_id: str, db, *, assist_completed: bool = False,
) -> str:
    """§17.519 — machine-readable companion to the §17.506/§17.516 banners.

    Returns one of: 'assist_completed' (operator executed via Assist Mode),
    'plan_only' (autonomous run produced unexecuted Shell runbooks, i.e.
    `shell_tool_enabled` False with done Shell nodes), or 'executed' (real
    autonomous output). Lets consumers branch without parsing banner text.
    Mirrors the banner gating in `_compile_output`; persisted to
    `jobs.deliverable_kind` by the finalize sites.
    """
    if assist_completed:
        return "assist_completed"
    try:
        row = await db.execute(
            text(
                "SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid "
                "AND status = 'done' AND lower(tool) = 'shell'"
            ),
            {"jid": job_id},
        )
        shell_done = row.scalar() or 0
    except Exception:  # noqa: BLE001 — never block finalize on this read
        shell_done = 0
    if shell_done and not settings.shell_tool_enabled:
        return "plan_only"
    return "executed"


def _prepend_assist_completed_banner(
    text: str | None, step_count: int,
) -> str | None:
    """§17.516 — positive header for a deliverable compiled from an Assist Mode
    run. The operator executed and verified each step on their own systems, so
    (unlike autonomous runbook output) this is a *record of work actually done*
    — and the §17.506 PLAN-NOT-EXECUTED banner must be suppressed for it. The
    deliverable below is synthesized from the evidence the operator submitted.

    Returns the input unchanged when ``text`` is None.
    """
    if text is None:
        return text
    plural = "step" if step_count == 1 else "steps"
    banner = (
        f"> ✅ **Completed via Assist Mode** — you executed and verified "
        f"{step_count} {plural} on your own systems. The summary below is "
        f"compiled from the evidence you submitted."
        f"\n\n---\n\n"
    )
    return banner + text


# §17.473 — dominant-leaf preference for Strategy 0. dag_generator marks
# EVERY structural leaf (a node nothing depends on) is_output_node, so a DAG
# with a dead-end side-branch — e.g. a "configure Tailscale exit node" node
# that nothing downstream consumes — flags that branch as a co-deliverable
# alongside the real convergence/synthesis node. Concatenating both buries
# the synthesis under an orphan branch. A non-primary leaf is treated as a
# dead-end and dropped only when a *dominant* leaf both (a) already covers
# that leaf's entire upstream and (b) has a closure at least this many times
# larger. Co-equal / disjoint leaves (genuine multi-deliverable, "config +
# README") have no dominant leaf, so all survive — the prior concat behavior.
_DOMINANT_LEAF_FACTOR = 2

# §17.482 — leaf tools whose output is a genuine user-facing artifact and is
# therefore NEVER dropped by the dominance test, even when a larger leaf
# subsumes its upstream. CodeGen (executable code) and LLM (the reasoning /
# summary / validation / document text that is usually the actual deliverable)
# both qualify. Only "action" tools whose leaf output is an intermediate side
# effect — Shell runbooks (Proxmox's dead-end "configure Tailscale exit node"),
# filesystem writes, raw retrieval dumps — stay droppable. Rationale: a wrong
# DROP loses a real deliverable (a true mis-fire); a wrong KEEP merely appends
# a harmless dead-end branch to the concat. With LLM-vs-LLM the size factor
# alone could not tell a parallel deliverable ("validate directory structure",
# "set up network security node") from a dead-end, so it mis-fired both ways —
# protecting LLM makes the heuristic err toward keeping, eliminating the
# false-drops while still collapsing Shell/FS dead-end branches.
_PROTECTED_LEAF_TOOLS = frozenset({"CodeGen", "LLM"})


def _dependency_closure(key: str, deps_by_key: dict[str, list[str]]) -> set[str]:
    """Transitive dependency closure of ``key`` (inclusive of ``key``).

    Iterative + visited-guarded so a malformed cyclic ``depends_on`` can't
    recurse forever (DAGs are acyclic by construction, but compile must not
    assume it — a bad graph should degrade, not hang)."""
    seen: set[str] = set()
    stack = [key]
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        for dep in deps_by_key.get(k, ()) or ():
            if dep not in seen:
                stack.append(dep)
    return seen


def _select_dominant_leaves(explicit: list, all_nodes: list) -> tuple[list, list[str]]:
    """§17.473 — drop dead-end-branch leaves dominated by a larger leaf.

    ``explicit`` is the done is_output_node leaves (in execution order).
    Returns ``(survivors, dropped_keys)``:

      - ``primary`` = the leaf with the largest dependency closure (ties →
        latest in execution order, i.e. last in ``explicit``).
      - a non-primary leaf ``L`` is dropped iff ``L``'s tool is **not** in
        ``_PROTECTED_LEAF_TOOLS`` (CodeGen/LLM) AND ``primary`` covers all of
        ``L``'s upstream (``closure(L) - {L} ⊆ closure(primary)``) AND
        ``primary``'s closure is ≥ ``_DOMINANT_LEAF_FACTOR`` × ``L``'s. The
        conditions matter: the protected-tool guard (§17.482) keeps
        executable-code and LLM-text deliverables — the only droppable leaves
        are action-tool dead-ends (Shell runbooks, FS writes); the subset test
        ensures ``L`` adds nothing but itself; the size factor protects
        co-equal deliverables that merely share a common base node.

    Survivors keep their original execution order. Never empty (``primary``
    always survives)."""
    deps_by_key = {
        n["node_key"]: list(n.get("depends_on") or []) for n in all_nodes
    }
    closures = {
        n["node_key"]: _dependency_closure(n["node_key"], deps_by_key)
        for n in explicit
    }
    # Largest closure wins; on a tie the later (higher execution_order) leaf
    # wins — index in `explicit` is the execution-order rank.
    primary = max(
        enumerate(explicit),
        key=lambda iv: (len(closures[iv[1]["node_key"]]), iv[0]),
    )[1]
    pkey = primary["node_key"]
    pclosure = closures[pkey]

    dropped: list[str] = []
    for n in explicit:
        nkey = n["node_key"]
        if nkey == pkey:
            continue
        # §17.482 — never drop a protected-tool leaf (CodeGen or LLM). CodeGen
        # output is executable code, a deliverable by definition (cf. Strategy
        # 2, "last CodeGen node IS the deliverable"); LLM output is the
        # reasoning / summary / validation / document text that is usually the
        # real deliverable. The original §17.473 rule guarded only CodeGen, so
        # an LLM leaf whose closure another LLM leaf merely subsumed got
        # dropped even when it was a parallel deliverable — observed dropping
        # "validate directory structure" (Homelab) and "set up network
        # security node" (AI-Research). Only action-tool leaves stay droppable:
        # Shell runbooks (Proxmox's dead-end "configure Tailscale exit node"),
        # FS writes, raw retrieval dumps — where a dead-end side-branch is the
        # case the rule actually targets.
        if n.get("tool") in _PROTECTED_LEAF_TOOLS:
            continue
        nclosure = closures[nkey]
        deps_only = nclosure - {nkey}
        if deps_only <= pclosure and len(pclosure) >= _DOMINANT_LEAF_FACTOR * len(nclosure):
            dropped.append(nkey)

    dropped_set = set(dropped)
    survivors = [n for n in explicit if n["node_key"] not in dropped_set]
    return survivors, dropped


async def _compile_output(
    job_id: str, db, *, assist_completed: bool = False,
) -> tuple[str | None, bool]:
    """Compile node outputs into a single deliverable.

    §17.516 — ``assist_completed=True`` marks an Assist Mode finalization: the
    operator executed the steps themselves, so the §17.506 PLAN-NOT-EXECUTED
    banner is suppressed and a positive "Completed via Assist Mode" header is
    prepended instead.

    Returns ``(text, was_synthesized)``:
      - ``text`` is ``None`` when no done node contributed output.
      - ``was_synthesized`` is True iff the W.7 LLM-synthesis pass
        actually replaced the heuristic. False when synthesis is
        disabled, fail-open, CodeGen-guarded, or empty-heuristic.

    Sprint X.2 changed the return type from ``str | None`` to
    ``tuple[str | None, bool]`` so callers can persist the synthesized
    flag on ``jobs.compiled_output_synthesized``. The X.2 skipped-verify
    banner is prepended *after* synthesis (operational metadata, not
    narrative — survives any LLM rewriting).
    """
    rows = await db.execute(
        text(
            "SELECT node_key, title, tool, status, output_text, depends_on, "
            "       COALESCE(is_output_node, FALSE) AS is_output_node, "
            "       COALESCE(is_deliverable, FALSE) AS is_deliverable "
            "FROM dag_nodes WHERE job_id = :jid ORDER BY execution_order"
        ),
        {"jid": job_id},
    )
    nodes = rows.mappings().all()

    # Sprint X.2 — count skipped + total for the banner. Total counts
    # only nodes that ever had something to do (excludes failed deps
    # blocking; those show up as 'pending' in /exec/status' counts).
    skipped_count = sum(1 for n in nodes if n["status"] == "skipped")
    total_count = len(nodes)

    # §17.506 — count Shell/runbook nodes the engine did NOT execute. When
    # `shell_tool_enabled` is False (default) a Shell node only ever produces
    # a runbook for the human to run, yet is marked `done` — so the job can
    # roll up to `completed` with nothing actually built. Gate on the flag so
    # a future real shell backend (shell_tool_enabled=True) suppresses it.
    # §17.516 — in an Assist Mode finalization the operator executed every step,
    # so there is no "unexecuted runbook" — force runbook_count to 0 so the
    # PLAN-NOT-EXECUTED banner never fires (a positive assist header is used).
    runbook_count = (
        sum(1 for n in nodes
            if n["status"] == "done" and (n["tool"] or "").lower() == "shell")
        if (not settings.shell_tool_enabled and not assist_completed) else 0
    )
    done_count = sum(1 for n in nodes if n["status"] == "done")

    async def _finish(text_value: str | None, was_synthesized: bool) -> tuple[str | None, bool]:
        """Apply the X.2 skipped banner + §17.506 plan-only / §17.516 assist
        banner + return. The top banner is applied last so it lands first —
        either the plan-only warning (autonomous, unexecuted) or the positive
        assist-completed header (operator executed it), never both."""
        banner_text = _prepend_skipped_banner(text_value, skipped_count, total_count)
        if assist_completed:
            banner_text = _prepend_assist_completed_banner(banner_text, done_count)
        else:
            banner_text = _prepend_plan_only_banner(
                banner_text, runbook_count, total_count, job_id,
            )
        return (banner_text, was_synthesized)

    # Strategy 0 — explicit DELIVERABLE marker (§17.475) is the primary
    # signal: the DAG generator named exactly which node(s) produce the
    # user-facing artifact. Trust those verbatim — no topological collapse,
    # and the deliverable need NOT be a topological leaf (e.g. a CodeGen
    # node with downstream docs/validation).
    deliverable = [
        n for n in nodes
        if n.get("is_deliverable") and n["status"] == "done" and n["output_text"]
    ]
    if deliverable:
        explicit = deliverable
    else:
        # §17.475 legacy fallback — pre-048 jobs (is_deliverable all FALSE)
        # or a draw where the model marked nothing: fall back to the
        # topological is_output_node leaves + §17.473 dominant-leaf, which
        # drops dead-end branches. Retained, not deleted.
        explicit = [
            n for n in nodes
            if n.get("is_output_node") and n["status"] == "done" and n["output_text"]
        ]
        if len(explicit) > 1:
            explicit, dropped = _select_dominant_leaves(explicit, nodes)
            if dropped:
                logger.info(
                    "compile_dominant_leaf: job=%s kept=%s dropped=%s "
                    "(dead-end branches subsumed by a dominant leaf)",
                    job_id,
                    [n["node_key"] for n in explicit],
                    dropped,
                )
    if explicit:
        if len(explicit) == 1:
            heuristic = explicit[0]["output_text"]
            text_value, was_syn = await _maybe_synthesize(
                job_id=job_id, heuristic=heuristic,
                strategy="0_single_leaf", source_tool=explicit[0]["tool"],
                db=db,
            )
            return await _finish(text_value, was_syn)
        heuristic = _join_sections([_format_section(n) for n in explicit])
        # Multi-leaf: if every leaf is the same verbatim-tool (CodeGen or
        # Shell), pass that tool through so `_synthesize_compiled_output`
        # short-circuits and the heuristic — which is the joined runbooks
        # or code — is preserved verbatim. §17.360: closes the case where
        # all 7 Shell leaves rendered through synthesis and the LLM
        # rewriter helpfully filled in `<PROXMOX_HOST_IP>` placeholders
        # with fabricated `192.168.x.x` values. Heterogeneous leaf set
        # (mixed LLM + Shell + CodeGen) keeps source_tool=None so
        # synthesis runs, constrained by the §17.360 fabrication clauses
        # in SYNTHESIS_SYSTEM.
        leaf_tools = {n["tool"] for n in explicit}
        if len(leaf_tools) == 1 and next(iter(leaf_tools)) in ("CodeGen", "Shell"):
            homogeneous_tool: str | None = next(iter(leaf_tools))
        else:
            homogeneous_tool = None
        text_value, was_syn = await _maybe_synthesize(
            job_id=job_id, heuristic=heuristic,
            strategy="0_multi_leaf", source_tool=homogeneous_tool, db=db,
        )
        return await _finish(text_value, was_syn)

    # Strategy 2: last CodeGen node is the deliverable.
    done = [n for n in nodes if n["status"] == "done" and n["output_text"]]
    if done and done[-1]["tool"] == "CodeGen":
        text_value, was_syn = await _maybe_synthesize(
            job_id=job_id, heuristic=done[-1]["output_text"],
            strategy="2_last_codegen", source_tool="CodeGen", db=db,
        )
        return await _finish(text_value, was_syn)

    # Strategy 3: concat-all-done-with-headers fallback.
    if not done:
        return None, False

    # Diagnostic — this path means the DAG produced output but no leaf node
    # was marked. Either the dag_generator's leaf-set logic missed this DAG
    # shape, or the leaf nodes failed/are still pending. Logged so the team
    # can spot patterns over time.
    logger.warning(
        "compile_strategy3_fallback: job=%s done=%d total=%d "
        "(no is_output_node leaf done with output)",
        job_id, len(done), len(nodes),
    )

    sections = [_format_section(n) for n in done]
    body = _join_sections(sections)

    # Apply storage cap. We truncate per-section proportionally so each node
    # keeps representative head/tail content, mirroring the upstream-truncation
    # pattern in execution_agent.
    cap = settings.compile_output_max_chars
    if len(body) > cap:
        # Reserve ~10% of cap for the preamble + section headers + separators.
        budget = max(1000, int(cap * 0.9))
        per_section = max(
            settings.compile_output_min_chunk, budget // max(1, len(sections)),
        )
        truncated_sections = [
            f"## {n['node_key']}: {n['title']}\n\n"
            f"{_truncate(n['output_text'], per_section)}"
            for n in done
        ]
        body = _join_sections(truncated_sections)
        logger.info(
            "compile_strategy3_truncated: job=%s original_chars=%d "
            "truncated_chars=%d per_section_cap=%d",
            job_id, sum(len(s) for s in sections), len(body), per_section,
        )

    preamble = (
        f"_Partial deliverable — {len(done)} of {len(nodes)} node(s) "
        f"contributed. No terminal output node was reached; sections below "
        f"are stitched in execution order._\n\n---\n\n"
    )
    heuristic = preamble + body
    # Strategy 3 has no single source-tool — done set is heterogeneous.
    # CodeGen guard isn't applicable; SYNTHESIS_SYSTEM still preserves
    # code blocks verbatim within the prose.
    text_value, was_syn = await _maybe_synthesize(
        job_id=job_id, heuristic=heuristic,
        strategy="3_concat_all", source_tool=None, db=db,
    )
    return await _finish(text_value, was_syn)
