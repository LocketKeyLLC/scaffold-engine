"""Assist Mode guidance layer (§17.486).

When a human claims a DAG step in Assist Mode, the engine should walk them
through *how to do it* — copy-paste terminal commands for shell/codegen work,
numbered step-by-step instructions for non-coding work — rather than handing
them the raw LLM ``prompt_template`` (an execution hint written for a model)
and saying "paste your output."

This module is the generator. It is pure logic + DB persistence; it does no
HTTP and owns no session lifecycle (that stays in ``assist_agent``). The
flow per step:

    1. (optional) research pre-pass — ask the model what facts a human would
       need to look up (versions, current flags, exact package names), then
       confirm each via the existing SearXNG / Milvus grounding and cite them.
    2. generate the walkthrough with a human-facing system prompt selected by
       the node's tool (shell → runbook, codegen → code+run, else → steps).
    3. persist the result on the owning ``assist_steps`` row so a re-view does
       not re-spend an LLM call. ``/assist guide`` regenerates with ``force``.

Why reuse rather than reinvent: the autonomous executor already has a
copy-paste runbook system prompt (``prompt_assembly.EXECUTION_SYSTEM_RUNBOOK``)
and grounding helpers (``execution_agent._searxng_search`` / ``_milvus_search``)
— this module composes them for the human-in-the-loop path. The generation
goes through ``chat_until_nonempty`` because the cloud thinking models can
return ``success=True`` with empty content when reasoning eats the token
budget (§17.465); a generous ``max_tokens`` plus retry-on-empty avoids it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app import model_router
from app.config import settings
from app.modules.prompt_assembly import (
    EXECUTION_SYSTEM_RUNBOOK,
    StepContext,
)
from app.utils.llm_retry import chat_until_nonempty

logger = logging.getLogger("scaffold.assist_guide")


# ── Human-facing system prompts ──────────────────────────────────────────
# These differ from the executor prompts (RUNBOOK/CODEGEN/LLM): those tell an
# *LLM* to produce a deliverable; these tell the engine to produce
# instructions the *human operator* will follow to produce it themselves.

# §17.640 — always-on beginner-audience framing. Injected into every human
# guide/fix system prompt so the walkthrough NEVER assumes prior expertise or
# that the operator already knows an unspoken sub-task (the reported failure:
# a step said "connect the PC to the other PC" with no explanation of HOW).
# Verbosity is a separate dial (terse = fewer words, detailed = more WHY); this
# floor holds at every level, so "always assume limited knowledge" is guaranteed
# regardless of the verbosity setting.
_AUDIENCE_FRAMING = (
    "Audience — ALWAYS assume the operator is a capable beginner: they will "
    "follow precise instructions carefully but are NOT assumed to know this "
    "domain. Never assume prior expertise, and never assume they already know "
    "how to do a sub-task the step takes for granted — opening a terminal, "
    "connecting one machine to another, finding a device's IP address, editing "
    "a config file, plugging in a cable, logging into a router. When a step "
    "depends on such a sub-task, spell out HOW to do it (the exact clicks, "
    "commands, cables, or menu paths), not just 'configure X' or 'connect A to "
    "B'. Expand every acronym and define jargon in-line the first time it "
    "appears. When more than one common setup exists (wired vs Wi-Fi, two "
    "machines on the same network vs across the internet), name the one you are "
    "assuming and give the reader a quick way to tell which fits their case."
)

# §17.641 — pacing floor. §17.640 made walkthroughs thorough ("spell out HOW"),
# which for a large step turns into one long flat list that overwhelms a
# first-timer doing it by hand. This keeps the SAME completeness but chunks and
# paces it — group into phases, checkpoint each, one action per item. Chunk the
# work, never cut it.
_PACING_FRAMING = (
    "Pacing — the reader is ONE person doing this by hand, possibly for the "
    "first time, so the walkthrough must stay digestible and never read as an "
    "overwhelming wall of actions. (1) Cover ONLY what THIS step needs — never "
    "fold in work that belongs to a later step. (2) If the step needs more than "
    "a handful of actions, GROUP them into a few short, clearly labeled phases "
    "(roughly 3-6 actions each) and end each phase with a one-line "
    "'Checkpoint:' the reader confirms before moving on — chunk the work, do "
    "NOT cut it (every necessary action still appears). (3) One concrete action "
    "per numbered item; no compound 'do A, then B, then C' items. (4) Put "
    "anything nice-to-have or advanced behind an explicit '(Optional)' label so "
    "it is clearly skippable, never inline as if required. (5) Open with a "
    "single short sentence naming how many phases there are, so the reader sees "
    "a short, finite path instead of an endless list."
)

_RUNBOOK_HUMAN_FRAMING = (
    "You are a hands-on co-pilot guiding a human operator through ONE step of "
    "a larger plan. The reader will perform this step themselves on their own "
    "machine. Produce the runbook they will follow — every command copy-paste "
    "ready, every operator-supplied value a <PLACEHOLDER>, and a clear way to "
    "confirm success. You are NOT performing the step; do not narrate it as "
    "done.\n\n" + _AUDIENCE_FRAMING + "\n\n" + _PACING_FRAMING
)

_HEADING_META_RULE = (
    "IMPORTANT — the parenthetical text under each heading below tells YOU what "
    "to write there; it is guidance for you, not text for the reader. Write the "
    "heading line as the exact short heading shown (e.g. `## Goal`) and NOTHING "
    "else on that line. NEVER copy the parenthetical guidance into your answer — "
    "the reader must see clean headings like `## Goal`, `## Steps`, followed by "
    "your actual content."
)

GUIDE_SYSTEM_CODEGEN = f"""You are a hands-on co-pilot guiding a human operator through ONE code step of a larger plan. The reader will create and run this code themselves.

{_AUDIENCE_FRAMING}

{_PACING_FRAMING}

{_HEADING_META_RULE}

Use these section headings, in order, and omit any that don't apply:

## What you're building
(One or two sentences on the deliverable and where the file goes.)

## Code
(The complete implementation in a single fenced code block. Real, working code, not a sketch. One implementation, not alternatives.)

## Run this
(Numbered, copy-paste-ready terminal commands to save, install deps, and run/test it. Use fenced code blocks. One command group per step.)

## Verify
(How the operator confirms it worked: the expected output paired with the exact command that produces it.)

## Inputs needed
(Any value you could not determine — paths, names, keys. Each MUST appear in the code or commands as a <SCREAMING_SNAKE_CASE> placeholder, never as a guessed concrete value.)

Hard rules:
- Never write past-tense narration ("Created the file", "Ran it and got…", "Output confirmed…"). The human runs it, not you.
- Never invent concrete values (IPs, hostnames, ports, keys, versions) absent from the task or research block — use placeholders.
- If the task enumerates specifics (a full language list, default values, every flag), implement them COMPLETELY; do not silently truncate to a subset.
- If a confirmed-research block is provided, treat those facts as authoritative and use them (correct package name, current flag, exact version).
- No emoji, no "let me know if…", no completion checkmarks — the operator marks completion.

Produce the walkthrough for THIS step only. Nothing more."""

GUIDE_SYSTEM_NONCODE = f"""You are a hands-on co-pilot guiding a human operator through ONE non-coding step of a larger plan. The deliverable is a decision, a written artifact, a configuration in a UI, or a manual action — not code or shell commands.

{_AUDIENCE_FRAMING}

{_PACING_FRAMING}

{_HEADING_META_RULE}

Use these section headings, in order, and omit any that don't apply:

## Goal
(One or two sentences: what this step produces and why it matters to the steps that follow.)

## Steps
(A NUMBERED list the operator follows in order. Each step is one concrete action — "Open X and click Y", "Decide between A and B — pick A because…", "Write a paragraph covering Z". Be specific enough to act on without guessing.)

## What to decide
(When the step is a decision, lay out the real options with the trade-off that picks the winner, then state the recommended choice. Do not leave the decision hanging. Omit this heading entirely when the step is not a decision.)

## Done when
(The observable signal that the step is complete — a file exists, a setting shows X, the document covers the listed points.)

## Inputs needed
(Anything the operator must supply that you could not determine. Mark each as a <PLACEHOLDER>, never a guessed value.)

Hard rules:
- Never write past-tense narration as if you performed the step ("Configured…", "Decided…", "Wrote…"). The human does it.
- Never invent concrete values (names, URLs, account IDs, versions) absent from the task or research block — use placeholders.
- If a confirmed-research block is provided, treat those facts as authoritative.
- No emoji, no filler closers, no completion checkmarks.

Produce the walkthrough for THIS step only. Nothing more."""

GUIDE_SYSTEM_FIX = f"""You are a hands-on co-pilot helping a human operator who hit a problem while performing ONE step of a larger plan. They will paste the error / what went wrong; you diagnose it and give them the exact commands to recover and finish the step.

{_AUDIENCE_FRAMING}

{_HEADING_META_RULE}

Use these section headings, in order, and omit any that don't apply:

## Diagnosis
(What the error means and the most likely cause, in 1-3 sentences. Be concrete; name the actual failing thing.)

## Fix
(Numbered, copy-paste-ready commands or edits that resolve it. Use fenced code blocks. If there are multiple plausible causes, lead with the most likely and label the alternatives.)

## Then
(What to run to confirm it's fixed and how to complete the original step.)

## If that fails
(The next thing to check or try, so the operator isn't stuck.)

Hard rules:
- Address THIS error and THIS task. Don't restate the whole step from scratch unless the fix requires it.
- Never write past-tense narration ("Fixed it", "Ran it and it worked"). The operator runs your commands.
- Never invent concrete values (versions, paths, package names, ports) absent from the task, the error, the environment, or the research block — use a <PLACEHOLDER>.
- If a confirmed-research block is provided, treat those facts as authoritative (correct package name, current flag, known-bug workaround).
- If the error text is too vague to diagnose, say exactly what additional output you need (e.g. "paste the full traceback" / "run `<cmd>` and share the output") instead of guessing.
- No emoji, no filler closers, no completion checkmarks.

Produce the troubleshooting help for THIS error only. Nothing more."""

_FIX_USER_TRAILER = (
    "---\n\n"
    "The operator performed the step above and hit the error shown. Diagnose it "
    "and give the copy-paste commands to recover and complete the step, following "
    "the output structure and hard rules in your system instructions exactly."
)

_GUIDE_USER_TRAILER = (
    "---\n\n"
    "Using the task and any upstream/research context above, write the "
    "walkthrough the human operator will follow to COMPLETE this step "
    "themselves. Follow the output structure and hard rules in your system "
    "instructions exactly."
)

_RESEARCH_SYNTH_SYSTEM = (
    "You answer a single operator question using only the provided search "
    "results. Be concise and concrete. Cite the source index like [1] for "
    "each fact. If the results do not answer the question, say so plainly "
    "rather than guessing. No preamble, no filler."
)


# §17.499 — verbosity / skill-level directives appended to the system prompt.
VERBOSITY_LEVELS = ("terse", "normal", "detailed")
_VERBOSITY_DIRECTIVE = {
    "terse": (
        "\n\nVERBOSITY: TERSE — the operator asked for brevity: output only the "
        "commands/steps with a one-line reason each, and omit background and "
        "rationale. Brevity means FEWER WORDS, not assuming expertise — still "
        "spell out any non-obvious sub-task and keep every command copy-paste "
        "ready (the beginner-audience rule above always holds)."
    ),
    "detailed": (
        "\n\nVERBOSITY: DETAILED — assume a less-experienced operator: briefly "
        "explain WHY each step matters and what to watch for, and expand the "
        "verification. Stay concrete and copy-paste-ready — explanation in "
        "addition to the commands, never instead of them."
    ),
}


def apply_verbosity(system: str, verbosity: str | None) -> str:
    """Append the verbosity directive to a system prompt. `normal`/unknown → no
    change (current behavior)."""
    return system + _VERBOSITY_DIRECTIVE.get(verbosity or "normal", "")


def guide_system_for_tool(tool: str) -> str:
    """Pick the human-facing system prompt for a node's tool type.

    Mirrors ``prompt_assembly.system_for_tool`` (shell/codegen/else) but
    targets the human operator rather than the LLM executor. The ``shell``
    variant reuses ``EXECUTION_SYSTEM_RUNBOOK`` verbatim (it already targets a
    human performing host commands) with a one-line operator framing prepended.
    """
    t = (tool or "").lower()
    if t == "shell":
        return f"{_RUNBOOK_HUMAN_FRAMING}\n\n{EXECUTION_SYSTEM_RUNBOOK}"
    if t == "codegen":
        return GUIDE_SYSTEM_CODEGEN
    return GUIDE_SYSTEM_NONCODE


# ── Research pre-pass (confirm unknowns) ──────────────────────────────────

# A single-tool schema. Native tool-calling is the robust path here; the
# coaxing fallback in model_router.tool_call covers providers without it.
_FLAG_UNKNOWNS_TOOL = model_router.Tool(
    name="flag_unknowns",
    description=(
        "Report the web/knowledge-base lookups a human operator would need "
        "to perform this step correctly — version-specific commands, current "
        "CLI flags, exact package names, API endpoints. Each query is a short "
        "search string. Return an empty list if nothing needs looking up."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to a few concrete search queries.",
            }
        },
        "required": ["queries"],
    },
)

# Markers the grounding helpers return when there is nothing useful. Sources
# with these (or empty) bodies are dropped so the citation footnote and the
# injected research block only ever carry real, confirmed facts. Strings must
# match execution_agent._searxng_search / _milvus_search verbatim (lowercased).
_EMPTY_MARKERS = ("no search results found.", "no knowledge base results found.")
_FAILURE_PREFIXES = ("searxng search failed", "knowledge base search failed")


def _is_useful_grounding(body: str) -> bool:
    if not body or not body.strip():
        return False
    low = body.strip().lower()
    if low in _EMPTY_MARKERS:
        return False
    return not any(low.startswith(p) for p in _FAILURE_PREFIXES)


async def _detect_unknowns(
    *, task_text: str, tool: str, role: str, max_queries: int,
) -> list[str]:
    """Ask the model which facts a human would need to confirm. Fail-soft."""
    if max_queries <= 0:
        return []
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": (
                    "You help a human prepare to execute a task. List only "
                    "lookups that genuinely matter for correctness; prefer an "
                    "empty list over speculative queries."
                )},
                {"role": "user", "content": (
                    f"Task tool: {tool}\n\nTask:\n{task_text}\n\n"
                    f"Call flag_unknowns with up to {max_queries} search "
                    f"queries (or an empty list)."
                )},
            ],
            [_FLAG_UNKNOWNS_TOOL],
            role=role,
            temperature=0.2,
            max_tokens=1024,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — never block guidance
        logger.warning("assist_guide_detect_unknowns_failed: %s", exc)
        return []
    if not resp.success or not resp.tool_calls:
        return []
    args = resp.tool_calls[0].arguments or {}
    raw = args.get("queries") or []
    queries = [q.strip() for q in raw if isinstance(q, str) and q.strip()]
    return queries[:max_queries]


async def _searxng_structured(query: str, max_results: int = 5) -> list[dict]:
    """§17.500 — structured SearXNG results ([{title, content, url}]) so we can
    fetch the result pages. Fail-soft → []."""
    try:
        from app.utils.http_clients import get_searxng_client
        resp = await get_searxng_client().get(
            "/search", params={"q": query, "format": "json", "categories": "general"},
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "content": r.get("content", ""), "url": r.get("url", "")}
            for r in (resp.json().get("results") or [])[:max_results]
            if r.get("url")
        ]
    except Exception as exc:
        logger.warning("assist_searxng_structured_failed: %s", exc)
        return []


async def _deep_web_sources(query: str, *, top_n: int) -> list[dict]:
    """§17.500 — fetch + trafilatura-extract the top-N SearXNG pages for real
    doc content. Reuses the research-agent fetcher. Fail-soft → []."""
    results = await _searxng_structured(query)
    if not results or top_n <= 0:
        return []
    try:
        from app.modules.research_agent import _fetch_and_extract
        pages = await _fetch_and_extract(results[:top_n])
    except Exception as exc:
        logger.warning("assist_deep_fetch_failed: %s", exc)
        return []
    return [
        {"query": query, "kind": "web", "text": p["content"][:2000], "url": p.get("url", "")}
        for p in pages if (p.get("content") or "").strip()
    ]


async def _confirm_query(
    query: str, *, node_key: str, domain: Optional[str], deep: bool = False,
) -> list[dict]:
    """Confirm one query via Milvus (local KB) + web.

    ``deep`` (used by /assist research + /assist fix) fetches & extracts the top
    SearXNG result PAGES (real doc content); otherwise (the auto-guide pre-pass)
    it uses fast search snippets. Returns ``{query, kind, text[, url]}`` source
    dicts, only non-empty/non-failure. Never raises (helpers are fail-soft).
    """
    from app.modules.execution_agent import _milvus_search, _searxng_search

    sources: list[dict] = []
    milvus = await _milvus_search(query, node_key=node_key, domain=domain)
    if _is_useful_grounding(milvus):
        sources.append({"query": query, "kind": "milvus", "text": milvus.strip()})

    if deep and settings.assist_research_fetch_top_n > 0:
        web = await _deep_web_sources(query, top_n=settings.assist_research_fetch_top_n)
        if web:
            sources.extend(web)
            return sources
        # fetch found nothing → fall through to the snippet path.

    searx = await _searxng_search(query)
    if _is_useful_grounding(searx):
        sources.append({"query": query, "kind": "searxng", "text": searx.strip()})
    return sources


async def _research_prepass(
    *, task_text: str, tool: str, role: str, max_queries: int,
    node_key: str, domain: Optional[str], deep: bool = False,
) -> list[dict]:
    queries = await _detect_unknowns(
        task_text=task_text, tool=tool, role=role, max_queries=max_queries,
    )
    if not queries:
        return []
    logger.info("assist_guide_research: %d queries node_key=%s deep=%s", len(queries), node_key, deep)
    # One round-trip: all queries confirmed concurrently.
    batches = await asyncio.gather(
        *[_confirm_query(q, node_key=node_key, domain=domain, deep=deep) for q in queries],
        return_exceptions=True,
    )
    sources: list[dict] = []
    for b in batches:
        if isinstance(b, Exception):
            logger.warning("assist_guide_confirm_query_failed: %s", b)
            continue
        sources.extend(b)
    return sources


def _render_research_block(sources: list[dict]) -> str:
    if not sources:
        return ""
    parts = [
        "## Research (confirmed — authoritative facts; use them, do not contradict them)"
    ]
    for i, s in enumerate(sources, 1):
        src = f"{s['kind']}: {s['url']}" if s.get("url") else s["kind"]
        parts.append(f"[{i}] ({src}) query: {s['query']}\n{s['text']}")
    return "\n\n".join(parts)


def render_environment_block(environment: dict | None) -> str:
    """§17.487 — the operator's environment so the model emits concrete commands.

    ``environment`` = ``{"profile": str, "substitutions": {KEY: value}}`` (stored on
    ``assist_sessions.metadata.environment``). Returns "" when empty so callers no-op.
    """
    if not environment:
        return ""
    profile = (environment.get("profile") or "").strip()
    subs = environment.get("substitutions") or {}
    if not profile and not subs:
        return ""
    parts = [
        "## Operator environment (use these concrete values; emit a <PLACEHOLDER> "
        "ONLY for values not given here)"
    ]
    if profile:
        parts.append(profile)
    if subs:
        parts.append("\n".join(f"- {k} = {v}" for k, v in subs.items()))
    return "\n\n".join(parts)


# ── Success verification (§17.487 — did the submitted step actually work?) ─

_JUDGE_OUTCOME_TOOL = model_router.Tool(
    name="judge_step_outcome",
    description=(
        "Judge whether the operator's pasted evidence shows the step SUCCEEDED. "
        "Look for failure signals: error messages, tracebacks, non-zero exit "
        "codes, 'command not found', 'permission denied', 'No such file', empty "
        "output where output was expected. Be conservative — only 'failed' on a "
        "clear failure signal; 'unclear' when ambiguous or there's not enough to tell."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["succeeded", "failed", "unclear"]},
            "reason": {"type": "string", "description": "One sentence, citing the signal."},
            "suggestion": {"type": "string", "description": "If failed: the likely next move."},
        },
        "required": ["outcome", "reason"],
    },
)


async def _sandbox_codegen_check(evidence: str) -> Optional[dict]:
    """§17.491 — run pasted codegen evidence in the scaffold-coderunner sandbox.

    Reuses the executor's `codegen_exec_smoke` classifier (pass | skip | fail).
    Returns ``{verdict, reason}`` or None on any error (fail-soft → the caller
    falls back to the LLM verdict).
    """
    try:
        from app.sandbox.codegen_check import codegen_exec_smoke
        chk = await codegen_exec_smoke(evidence)
        return {"verdict": chk.verdict, "reason": chk.reason}
    except Exception as exc:
        logger.warning("assist_sandbox_check_failed: %s", exc)
        return None


async def verify_step_success(
    *, title: str, task_prompt: str, tool: str, evidence: str,
    environment: Optional[dict] = None,
) -> dict:
    """Judge whether pasted evidence indicates the step worked. Fail-soft.

    Returns ``{outcome, reason, suggestion, grounded_by}``. On any model/parse
    failure returns ``outcome='unclear'`` so verification never blocks a submit
    it couldn't assess.

    §17.491 — for ``codegen`` steps with the sandbox enabled, the pasted code is
    RUN first: a definite runtime error (`fail`) authoritatively overrides to
    ``failed`` and skips the LLM call; ``pass``/``skip`` fall through to the LLM
    (which judges task-fit — "it runs" is necessary, not sufficient).
    """
    role = settings.assist_guide_model_role

    # Sandbox pre-check (codegen only, sandbox on). Deterministic ground truth.
    sandbox: Optional[dict] = None
    if (tool or "").lower() == "codegen" and settings.codegen_execution_check_enabled \
            and (settings.coderunner_url or "").strip():
        sandbox = await _sandbox_codegen_check(evidence)
        if sandbox and sandbox["verdict"] == "fail":
            return {
                "outcome": "failed",
                "reason": sandbox["reason"],
                "suggestion": "Fix the runtime error and resubmit — `/assist fix <the error>` can help.",
                "grounded_by": "sandbox",
            }

    env_block = render_environment_block(environment)
    user = (
        f"Task: {title}\n\n{task_prompt}\n\n"
        + (f"{env_block}\n\n" if env_block else "")
        + f"Operator's pasted evidence / output for this step:\n{evidence[:6000]}\n\n"
        "Call judge_step_outcome."
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": (
                    "You verify whether a human operator's step succeeded, from the "
                    "output they pasted. Conservative: 'failed' only on a clear "
                    "failure signal, 'unclear' when you can't tell."
                )},
                {"role": "user", "content": user},
            ],
            [_JUDGE_OUTCOME_TOOL],
            role=role,
            temperature=0.1,
            max_tokens=1024,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.warning("assist_verify_step_failed: %s", exc)
        return {"outcome": "unclear", "reason": "verification unavailable",
                "suggestion": "", "grounded_by": "model"}
    if not resp.success or not resp.tool_calls:
        return {"outcome": "unclear", "reason": "verification unavailable",
                "suggestion": "", "grounded_by": "model"}
    args = resp.tool_calls[0].arguments or {}
    outcome = args.get("outcome")
    if outcome not in ("succeeded", "failed", "unclear"):
        outcome = "unclear"
    reason = (args.get("reason") or "").strip()
    # §17.491 — the code ran cleanly in the sandbox; the LLM judged task-fit.
    # Record that the success is sandbox-backed, not just a text judgment.
    grounded_by = "model"
    if sandbox and sandbox["verdict"] == "pass":
        grounded_by = "sandbox+model"
        if outcome == "succeeded":
            reason = (reason + " (code executed cleanly in the sandbox)").strip()
    return {
        "outcome": outcome,
        "reason": reason,
        "suggestion": (args.get("suggestion") or "").strip(),
        "grounded_by": grounded_by,
    }


# ── Natural-language turn classification (§17.626) ─────────────────────────
# When a chat has an ACTIVE assist session, plain text is an intent, not a new
# idea. This classifier maps the message to one of the assist verbs so the
# operator drives the flow by talking. Obvious verbs are matched deterministically
# in the pipeline (no LLM); this handles the substantive/ambiguous messages —
# principally telling "I did it, here's the output" (submit) from "how do I do
# this?" (question) from "it broke with X" (fix), using the current step as context.

ASSIST_INTENTS = (
    "advance", "skip", "submit", "fix", "finalize", "pause",
    "handoff", "status", "explain_plan", "set_env", "set_verbosity",
    "ask", "question",
)

_CLASSIFY_TURN_TOOL = model_router.Tool(
    name="classify_turn",
    description=(
        "Classify what the operator wants to do next in an interactive, "
        "step-by-step assist session. They are currently on ONE specific step "
        "(given). Choose exactly one intent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(ASSIST_INTENTS),
                "description": (
                    "advance = move on to the next step ('next', 'ok what's next', 'move on'). "
                    "skip = skip/pass on the current step. "
                    "submit = they DID the step or are reporting the result / pasting output / "
                    "stating the decision they made — record it and continue. "
                    "fix = they hit an error or something isn't working and want help recovering. "
                    "finalize = finish the whole job and see the compiled result ('show me the result', 'we're all done'). "
                    "pause = stop for now. "
                    "handoff = they want the ENGINE to do this step (or the rest) automatically for them "
                    "('you do it', 'run it for me', 'just handle the rest', 'automate this'). "
                    "status = they want their progress / where they are ('where am I', 'how many left', 'status'). "
                    "explain_plan = they want the whole plan / all the steps / the big picture ('what's the overall plan', 'show me all the steps'). "
                    "set_env = they're telling you about their machine/environment ('I'm on Ubuntu 24.04 with apt', 'my host IP is 10.0.0.5', 'I use bash'). "
                    "set_verbosity = they want more or less detail in the instructions ('explain more', 'be more detailed', 'just give me the commands', 'too verbose'). "
                    "ask = a factual lookup that benefits from web/knowledge-base search — versions, current commands, comparisons, 'what is X', 'what's the latest', 'is X safe' — where a researched, cited answer helps more than re-showing the step. "
                    "question = they want help understanding or ADJUSTING this step's instructions (clarify, redo for a different OS, more detail on one part) — the safe default when unsure."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "When intent=submit: the operator's result to record for the "
                    "step (their pasted output, or a short restatement of the "
                    "decision/action they reported). Omit otherwise."
                ),
            },
            "error_text": {
                "type": "string",
                "description": (
                    "When intent=fix: the error or problem they described, as "
                    "concretely as they gave it. Omit otherwise."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "When intent=ask: the factual question to research, phrased "
                    "as a clear standalone query. Omit otherwise."
                ),
            },
        },
        "required": ["intent"],
    },
)

_CLASSIFY_SYSTEM = (
    "You route messages in a hands-on assist session where a human operator is "
    "working through a plan ONE step at a time, with the engine as co-pilot. "
    "Given the current step and the operator's message, decide what they want.\n\n"
    "Key distinctions:\n"
    "- STATES a result/decision ('I picked ZFS', 'done, 0 errors', pasted output) → submit\n"
    "- reports a FAILURE ('it errored with…', 'not working', 'command not found') → fix\n"
    "- wants the ENGINE to do the work ('you do this', 'run it for me', 'handle the rest') → handoff\n"
    "- a FACTUAL lookup ('what's the difference between ZFS and LVM', 'latest Proxmox version', 'is ZFS safe on non-ECC') → ask\n"
    "- wants to CLARIFY/ADJUST this step's instructions ('how do I do this', 'redo for macOS', 'more detail on part 2') → question\n"
    "- tells you about their MACHINE ('I'm on Ubuntu 24.04', 'IP is 10.0.0.5') → set_env\n"
    "Call classify_turn exactly once."
)


async def classify_turn(
    *, message: str, title: str, task_prompt: str, tool: str,
    role: str | None = None,
) -> dict:
    """Classify an operator's plain-language turn into an assist intent.

    Returns ``{"intent": <one of ASSIST_INTENTS>, "evidence": str,
    "error_text": str, "query": str}``. Fail-soft: on any model/parse error
    returns ``intent='question'`` so a flaky classifier degrades to the guide/
    refine behavior rather than misfiring a submit/skip/handoff."""
    role = role or settings.assist_classify_model_role
    fallback = {"intent": "question", "evidence": "", "error_text": "", "query": ""}
    user = (
        f"Current step: {title}\n\n"
        f"What the step asks:\n{(task_prompt or '')[:1500]}\n\n"
        f"Tool for this step: {tool or 'LLM'}\n\n"
        f"Operator's message:\n{message[:2000]}\n\n"
        "Call classify_turn."
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": user},
            ],
            [_CLASSIFY_TURN_TOOL],
            role=role,
            temperature=0.0,
            max_tokens=512,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — never block the turn
        logger.warning("assist_classify_turn_failed: %s", exc)
        return fallback
    if not resp.success or not resp.tool_calls:
        return fallback
    args = resp.tool_calls[0].arguments or {}
    intent = args.get("intent")
    if intent not in ASSIST_INTENTS:
        return fallback
    return {
        "intent": intent,
        "evidence": (args.get("evidence") or "").strip(),
        "error_text": (args.get("error_text") or "").strip(),
        "query": (args.get("query") or "").strip(),
    }


# ── Auto-learn substitutions (§17.490 — concrete values from evidence) ─────

# A walkthrough emits operator-supplied slots as <SCREAMING_SNAKE> (or
# <kebab>) placeholders (prompt_assembly §17.361). 2+ chars to avoid matching
# stray "<x>" in pasted output.
_PLACEHOLDER_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]{1,})>")

_LEARN_SUBS_TOOL = model_router.Tool(
    name="report_values",
    description=(
        "Report the concrete value the operator actually used for each named "
        "placeholder, read from their pasted command output / evidence. Include "
        "a placeholder ONLY if its value is clearly present in the evidence; "
        "omit any you cannot determine with confidence. Do NOT guess."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "values": {
                "type": "object",
                "description": "Map of PLACEHOLDER name (no angle brackets) → concrete value.",
                "additionalProperties": {"type": "string"},
            }
        },
        "required": ["values"],
    },
)


def find_placeholders(text: str) -> list[str]:
    """Distinct placeholder names (no brackets) in a walkthrough, order-preserved."""
    seen: dict[str, None] = {}
    for m in _PLACEHOLDER_RE.findall(text or ""):
        seen.setdefault(m, None)
    return list(seen.keys())


async def extract_substitutions(
    *, guidance_text: str, evidence: str, role: str | None = None,
) -> dict:
    """Learn concrete values the operator used for the walkthrough's placeholders.

    Cheap gate: if the guidance emitted no placeholders, return {} WITHOUT an
    LLM call. Otherwise a single tool_call fills the placeholders it can read
    from the evidence. Fail-soft → {}.
    """
    placeholders = find_placeholders(guidance_text)
    if not placeholders:
        return {}
    role = role or settings.assist_guide_model_role
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": (
                    "You extract the concrete values an operator used, from the "
                    "command output they pasted. Only report a value you can see "
                    "in the evidence; omit the rest. Never guess."
                )},
                {"role": "user", "content": (
                    f"Placeholders to fill (omit any you can't determine): "
                    f"{', '.join(placeholders)}\n\n"
                    f"Operator evidence:\n{evidence[:6000]}\n\n"
                    "Call report_values."
                )},
            ],
            [_LEARN_SUBS_TOOL],
            role=role,
            temperature=0.0,
            max_tokens=1024,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.warning("assist_learn_extract_failed: %s", exc)
        return {}
    if not resp.success or not resp.tool_calls:
        return {}
    raw = (resp.tool_calls[0].arguments or {}).get("values") or {}
    if not isinstance(raw, dict):
        return {}
    # Keep only placeholders we actually asked about, with non-empty string
    # values; strip stray angle brackets the model may echo.
    allowed = set(placeholders)
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip().strip("<>")
        val = str(v).strip()
        if key in allowed and val:
            out[key] = val
    return out


# ── Destructive-command safety gate (§17.492) ──────────────────────────────

# High-confidence, command-context-anchored patterns only — a destructive
# verb in prose ("this removes the file") must NOT trip the gate; only an
# actual command form does. (compiled regex, human-readable why).
_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # §17.613 (audit #7) — require an actual dash-flag bearing r/f/R. The old
    # pattern needed no leading dash, so `rm file.conf` and even the safe
    # `rm -i file` tripped the gate — crying wolf trains operators to ignore it.
    (re.compile(r"\brm\s+(-\S*\s+)*-\S*[rfR]"), "recursive/forced file deletion (rm -rf)"),
    (re.compile(r"--no-preserve-root"), "rm targeting / (--no-preserve-root)"),
    (re.compile(r"\bdd\b\s+(if|of)="), "raw disk write (dd)"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "format filesystem (mkfs)"),
    (re.compile(r"\bwipefs\b"), "wipe filesystem signatures (wipefs)"),
    (re.compile(r"\bshred\b"), "secure file wipe (shred)"),
    (re.compile(r"\b(fdisk|parted|sgdisk)\b"), "partition-table edit"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)"), "overwrite a block device"),
    (re.compile(r"\bchmod\s+-R\s+0?777\b"), "world-writable recursive chmod"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+(-f|--force))"),
     "destructive git (hard reset / force push / clean -f)"),
    (re.compile(r"\bdocker\s+(system\s+prune|volume\s+(rm|prune)|rm\s+-f)"), "docker resource removal"),
    (re.compile(r"\bkubectl\s+delete\b"), "kubectl delete"),
    (re.compile(r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE(\s+TABLE)?)\b", re.IGNORECASE),
     "destructive SQL (DROP/TRUNCATE)"),
    (re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE), "unfiltered SQL DELETE (no WHERE)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
]


def scan_destructive(text: str) -> list[dict]:
    """Deterministic scan for high-confidence destructive commands.

    Returns ``[{line, why}]`` (deduped by line; line truncated). Strips leading
    prompt/fence chars so ``$ rm -rf x`` matches. No LLM. Best-effort — this
    informs the operator, it does not block.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("$#>` ").strip()
        if not line or line in seen:
            continue
        for rx, why in _DESTRUCTIVE_PATTERNS:
            if rx.search(line):
                out.append({"line": line[:200], "why": why})
                seen.add(line)
                break
    return out


# ── Generation ───────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_guide_user_prompt(
    ctx: StepContext, node_description: Optional[str],
    sources: list[dict], refine_hint: Optional[str],
    environment: Optional[dict] = None,
) -> str:
    """Compose the user message: the same upstream-last task the executor
    would see, plus the operator environment, a confirmed-research block, and
    a human-walkthrough trailer.
    """
    parts: list[str] = [ctx.assembled_prompt]
    if node_description and node_description.strip() and node_description.strip() not in ctx.assembled_prompt:
        parts.append(f"Task description: {node_description.strip()}")
    env_block = render_environment_block(environment)
    if env_block:
        parts.append(env_block)
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    parts.append(_GUIDE_USER_TRAILER)
    if refine_hint and refine_hint.strip():
        parts.append(
            f"Operator refinement — apply this to the walkthrough: {refine_hint.strip()}"
        )
    return "\n\n".join(parts)


async def generate_guidance(
    *,
    ctx: StepContext,
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    node_key: str,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
) -> dict:
    """Generate (do not persist) the human walkthrough for one step.

    Returns ``{"guidance": str, "guidance_meta": dict, "status": str}``.
    ``status`` is ``"ready"`` when non-empty content was produced, else
    ``"failed"`` — never raises for an LLM/research failure (the caller shows
    a graceful fallback to the raw prompt).
    """
    role = settings.assist_guide_model_role

    sources: list[dict] = []
    if research:
        sources = await _research_prepass(
            task_text=ctx.base_prompt,
            tool=ctx.tool,
            role=role,
            max_queries=settings.assist_guide_max_research_queries,
            node_key=node_key,
            domain=domain,
        )

    system = apply_verbosity(guide_system_for_tool(ctx.tool), verbosity)
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
    )

    resp = await chat_until_nonempty(
        model_router.chat,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        {"role": role},
        temperature=0.3,
        max_tokens=settings.assist_guide_max_tokens,
        draws=3,
        label="assist_guide",
    )

    text_out = (resp.text or "").strip() if (resp and resp.success) else ""
    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": getattr(resp, "model", "") if resp else "",
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "status": status,
        "generated_at": _utcnow_iso(),
        # §17.492 — destructive-command safety gate.
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
    }
    if status == "failed":
        meta["error"] = (getattr(resp, "error", None) if resp else None) or "empty model output"
        logger.warning(
            "assist_guide_generation_empty node_key=%s tool=%s error=%s",
            node_key, ctx.tool, meta["error"],
        )
    return {"guidance": text_out, "guidance_meta": meta, "status": status}


async def generate_fix(
    *,
    ctx: StepContext,
    error_text: str,
    research: bool,
    environment: Optional[dict] = None,
    node_key: str,
    domain: Optional[str] = None,
    verbosity: str = "normal",
) -> dict:
    """Diagnose an operator-reported error on a step and produce corrected steps.

    Conversational (not persisted). Reuses the research pre-pass with the error
    folded into the task text so unknown-detection surfaces error-specific
    lookups. Returns ``{"fix": str, "guidance_meta": dict, "status": str}``
    (``fix`` key so it can't be confused with persisted guidance). Fail-soft.
    """
    role = settings.assist_guide_model_role

    sources: list[dict] = []
    if research:
        sources = await _research_prepass(
            task_text=f"{ctx.base_prompt}\n\nOperator hit this error:\n{error_text}",
            tool=ctx.tool,
            role=role,
            max_queries=settings.assist_guide_max_research_queries,
            node_key=node_key,
            domain=domain,
            deep=True,  # §17.500 — troubleshooting wants real doc content, not snippets
        )

    parts = [ctx.assembled_prompt]
    env_block = render_environment_block(environment)
    if env_block:
        parts.append(env_block)
    parts.append(f"## Error the operator hit\n{error_text.strip()}")
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    parts.append(_FIX_USER_TRAILER)
    user = "\n\n".join(parts)

    resp = await chat_until_nonempty(
        model_router.chat,
        [
            {"role": "system", "content": apply_verbosity(GUIDE_SYSTEM_FIX, verbosity)},
            {"role": "user", "content": user},
        ],
        {"role": role},
        temperature=0.3,
        max_tokens=settings.assist_guide_max_tokens,
        draws=3,
        label="assist_fix",
    )
    text_out = (resp.text or "").strip() if (resp and resp.success) else ""
    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": getattr(resp, "model", "") if resp else "",
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "status": status,
        "generated_at": _utcnow_iso(),
        # §17.492 — destructive-command safety gate (fixes can carry rm/dd too).
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
    }
    if status == "failed":
        meta["error"] = (getattr(resp, "error", None) if resp else None) or "empty model output"
    return {"fix": text_out, "guidance_meta": meta, "status": status}


# ── Persistence (cache write + idempotent read) ──────────────────────────


async def persist_guidance(
    *, session_id: str, node_key: str, guidance: str,
    guidance_meta: dict, status: str, db,
) -> None:
    await db.execute(
        text("""
            UPDATE assist_steps
               SET guidance = :g,
                   guidance_meta = CAST(:m AS jsonb),
                   guidance_status = :s,
                   guidance_generated_at = CASE
                       WHEN :s = 'ready' THEN NOW()
                       ELSE guidance_generated_at END,
                   updated_at = NOW()
             WHERE session_id = :sid AND node_key = :nk
        """),
        {
            "g": guidance or None,
            "m": json.dumps(guidance_meta),
            "s": status,
            "sid": session_id,
            "nk": node_key,
        },
    )
    await db.commit()


async def read_cached_guidance(
    *, session_id: str, node_key: str, db,
) -> Optional[dict]:
    """Return cached guidance only when a prior generation succeeded."""
    row = (await db.execute(
        text("""
            SELECT guidance, guidance_meta, guidance_status, guidance_generated_at
              FROM assist_steps
             WHERE session_id = :sid AND node_key = :nk
        """),
        {"sid": session_id, "nk": node_key},
    )).mappings().first()
    if not row or row["guidance_status"] != "ready" or not row["guidance"]:
        return None
    meta = row["guidance_meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    gen_at = row["guidance_generated_at"]
    return {
        "guidance": row["guidance"],
        "guidance_meta": meta or {},
        "status": "ready",
        "cached": True,
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else gen_at,
    }


async def ensure_guidance(
    *,
    session_id: str,
    node_key: str,
    ctx: StepContext,
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    force: bool = False,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
    db,
) -> dict:
    """Return guidance, generating + persisting only when needed.

    ``force=False`` (the auto-guide / re-view path) returns a cached ``ready``
    row without spending an LLM call. ``force=True`` (``/assist guide``) always
    regenerates.
    """
    if not force:
        cached = await read_cached_guidance(
            session_id=session_id, node_key=node_key, db=db,
        )
        if cached:
            return cached
    res = await generate_guidance(
        ctx=ctx,
        node_description=node_description,
        research=research,
        refine_hint=refine_hint,
        node_key=node_key,
        domain=domain,
        environment=environment,
        verbosity=verbosity,
    )
    await persist_guidance(
        session_id=session_id,
        node_key=node_key,
        guidance=res["guidance"],
        guidance_meta=res["guidance_meta"],
        status=res["status"],
        db=db,
    )
    res["cached"] = False
    return res


# ── Streaming generation (§17.493) ─────────────────────────────────────────


async def generate_guidance_stream(
    *,
    session_id: str,
    node_key: str,
    ctx: StepContext,
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    force: bool = False,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
    db,
):
    """Stream a walkthrough as it generates. Yields event dicts:

      ``{"type": "delta", "text": ...}``  — one per content chunk
      ``{"type": "done", "status": ..., "guidance_meta": {...}, "cached": bool}``

    - Cache hit (``force=False``) → a single delta + a ``done(cached=True)``, no
      model stream (re-views stay instant).
    - Empty stream → falls back to the non-streamed ``chat_until_nonempty`` so
      streaming cannot regress the §17.465 thinking-model empty-content guard.
    - Persists the full accumulated text + meta (destructive scan, sources)
      before the ``done`` event — same record a non-streamed generate produces.
    """
    role = settings.assist_guide_model_role

    # (a) cache short-circuit — no stream.
    if not force:
        cached = await read_cached_guidance(
            session_id=session_id, node_key=node_key, db=db,
        )
        if cached:
            yield {"type": "delta", "text": cached["guidance"]}
            yield {"type": "done", "status": "ready",
                   "guidance_meta": cached.get("guidance_meta") or {}, "cached": True}
            return

    # (b) research pre-pass (awaited, non-streamed).
    sources: list[dict] = []
    if research:
        sources = await _research_prepass(
            task_text=ctx.base_prompt, tool=ctx.tool, role=role,
            max_queries=settings.assist_guide_max_research_queries,
            node_key=node_key, domain=domain,
        )

    system = apply_verbosity(guide_system_for_tool(ctx.tool), verbosity)
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    # (c) stream content deltas.
    chunks: list[str] = []
    model_used = role
    try:
        async for delta in model_router.stream_chat(
            messages, role=role, temperature=0.3,
            max_tokens=settings.assist_guide_max_tokens,
        ):
            if delta:
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
    except Exception as exc:
        logger.warning("assist_guide_stream_failed: %s", exc)

    text_out = "".join(chunks).strip()

    # (d) empty-guard fallback — preserve §17.465 (stream yielded nothing).
    if not text_out:
        resp = await chat_until_nonempty(
            model_router.chat, messages, {"role": role},
            temperature=0.3, max_tokens=settings.assist_guide_max_tokens,
            draws=3, label="assist_guide_stream_fallback",
        )
        text_out = (resp.text or "").strip() if (resp and resp.success) else ""
        model_used = getattr(resp, "model", role) if resp else role
        if text_out:
            yield {"type": "delta", "text": text_out}

    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": model_used,
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "status": status,
        "generated_at": _utcnow_iso(),
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
    }
    if status == "failed":
        meta["error"] = "empty model output"
    await persist_guidance(
        session_id=session_id, node_key=node_key, guidance=text_out,
        guidance_meta=meta, status=status, db=db,
    )
    yield {"type": "done", "status": status, "guidance_meta": meta, "cached": False}


# ── Explicit one-off research (/assist research <question>) ───────────────


async def research_one(
    *, question: str, node_key: str = "?", domain: Optional[str] = None,
    synthesize: bool = True,
) -> dict:
    """Confirm a single operator-supplied question and optionally synthesize
    a short cited answer. Does not persist — this is a side query.
    """
    role = settings.assist_guide_model_role
    sources = await _confirm_query(question, node_key=node_key, domain=domain, deep=True)
    answer: Optional[str] = None
    if synthesize and sources:
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _RESEARCH_SYNTH_SYSTEM},
                {"role": "user", "content": (
                    f"Question: {question}\n\n"
                    f"{_render_research_block(sources)}"
                )},
            ],
            {"role": role},
            temperature=0.2,
            # Generous budget: the cloud thinking model spends num_predict on
            # reasoning first, so a tight cap returns empty content (§17.465).
            # 8192 matches the node-exec budget that reliably clears reasoning.
            max_tokens=8192,
            draws=3,
            label="assist_research",
        )
        if resp and resp.success:
            answer = (resp.text or "").strip() or None
    return {"question": question, "sources": sources, "answer": answer}
