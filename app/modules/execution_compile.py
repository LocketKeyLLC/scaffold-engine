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
        return synthesized, True
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


async def _compile_output(job_id: str, db) -> tuple[str | None, bool]:
    """Compile node outputs into a single deliverable.

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
            "SELECT node_key, title, tool, status, output_text, "
            "       COALESCE(is_output_node, FALSE) AS is_output_node "
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

    async def _finish(text_value: str | None, was_synthesized: bool) -> tuple[str | None, bool]:
        """Apply the X.2 skipped banner + return."""
        return (
            _prepend_skipped_banner(text_value, skipped_count, total_count),
            was_synthesized,
        )

    # Strategy 0 (#97): explicit is_output_node marker wins over heuristics.
    explicit = [
        n for n in nodes
        if n.get("is_output_node") and n["status"] == "done" and n["output_text"]
    ]
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
