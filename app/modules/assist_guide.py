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
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.assist_guide")


# §17.855 — human-facing prompts moved to app/modules/assist_prompts.py;
# re-exported here so assist_guide.<NAME> and the tests keep resolving.
from app.modules.assist_prompts import (  # noqa: F401,E402
    _AUDIENCE_FRAMING,
    _PACING_FRAMING,
    _TARGET_SAFETY_FRAMING,
    _RUNBOOK_HUMAN_FRAMING,
    _HEADING_META_RULE,
    GUIDE_SYSTEM_CODEGEN,
    GUIDE_SYSTEM_NONCODE,
    GUIDE_SYSTEM_DECISION,
    GUIDE_SYSTEM_FIX,
    _FIX_USER_TRAILER,
    _GUIDE_USER_TRAILER,
    _GUIDE_DECISION_TRAILER,
)
# §17.771 (deferred, now done) — render-path suggestion validation. A DECISION
# step's first-view walkthrough MUST carry a recommendation; the audit found the
# model can silently drop the "## My suggestion" section with nothing catching it
# (the commit path is now decisive; this makes the render path so too). Present
# = a suggestion/recommendation heading OR a clear lean phrase; absent → enforce.
_SUGGESTION_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s*(?:my\s+)?(?:suggestion|recommendation)\b"
)
_SUGGESTION_LEAN_RE = re.compile(
    r"(?i)\b(?:i'?d\s+(?:lean|go\s+with|recommend|suggest|pick|choose)|"
    r"my\s+recommendation\b|i\s+(?:recommend|suggest)\b|i'?d\s+go\b)"
)


def _has_decision_suggestion(text: str) -> bool:
    """True when a decision walkthrough already carries a recommendation — a
    ``## My suggestion``/``## Recommendation`` heading OR a clear lean phrase."""
    t = text or ""
    return bool(_SUGGESTION_HEADING_RE.search(t) or _SUGGESTION_LEAN_RE.search(t))


_DECISION_SUGGESTION_SYSTEM = (
    "You are a hands-on co-pilot. The operator is on a DECISION step and you were "
    "shown the options already laid out for them. Recommend exactly ONE of those "
    "options and the single main reason it fits THIS operator's system and goal. "
    "It is a suggestion they can reject — decisive, but their call. Pick from the "
    "options given; do not invent a new one. Call recommend_option once."
)

# ── §17.851 — code-enforced placeholder resolution ─────────────────────────
# The §17.850 prompt rules alone did NOT stick (live evidence: the facts held
# "https://192.168.1.156:8006" verbatim and the walkthrough still emitted
# <PROXMOX_HOST_IP>) — the §17.668 lesson again: LLMs ignore prompt rules, so
# enforce in code. After generation: Layer 1 substitutes pinned values
# deterministically; Layer 2 maps leftovers against the facts ledger via one
# small tool-call (known value / suggested free-choice name / unknown);
# resolved values are AUTO-PINNED so the next run is Layer-1 deterministic and
# the operator sees + can edit them in the Pinned values panel. Fail-soft at
# every stage: worst case the original text ships unchanged.







_DECISION_SUGGESTION_TOOL = model_router.Tool(
    name="recommend_option",
    description=(
        "Recommend ONE of the options presented for the operator's decision, with "
        "the single main reason it fits their situation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "leaning": {
                "type": "string",
                "description": "Short label of the recommended option (one of those shown).",
            },
            "why": {
                "type": "string",
                "description": "The ONE main reason it fits, tailored to their system/goal.",
            },
        },
        "required": ["leaning", "why"],
    },
)


async def _generate_decision_suggestion(
    *, title: str, task_prompt: str, options_text: str,
    environment: Optional[dict], role: str,
) -> str:
    """Generate ONLY the ``## My suggestion`` block for a decision whose
    walkthrough omitted it, tailored to the options already produced + the
    operator's system. Returns the markdown block, or "" on any failure (the
    caller then ships the un-enforced walkthrough — fail-soft)."""
    env = render_environment_block(environment)
    user = (
        f"Decision: {title}\n\nWhat to decide:\n{(task_prompt or '')[:1000]}\n\n"
        + (f"{env.strip()}\n\n" if env.strip() else "")
        + f"The options presented to the operator:\n{(options_text or '')[:2000]}\n\n"
        "Recommend ONE of these and the single main reason. Call recommend_option."
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": _DECISION_SUGGESTION_SYSTEM},
                {"role": "user", "content": user},
            ],
            [_DECISION_SUGGESTION_TOOL],
            role=role,
            temperature=0.2,
            max_tokens=256,
            tool_choice="auto",
        )
    except Exception as exc:  # never block the walkthrough on the follow-up call
        logger.warning("assist_decision_suggestion_failed: %s", exc)
        return ""
    if not resp.success or not resp.tool_calls:
        return ""
    args = resp.tool_calls[0].arguments or {}
    leaning = (args.get("leaning") or "").strip()
    if not leaning:
        return ""
    why = (args.get("why") or "").strip().rstrip(". ").strip()
    if why:
        return f"## My suggestion\nI'd lean **{leaning}** — {why}. But it's your call."
    return f"## My suggestion\nI'd lean **{leaning}** — but it's your call."

# §17.674 — the pivot `ask`/research answer was RELAYING sources, not helping the
# operator ACT. A live homelab test: the operator pivoted mid-step to ask how to
# do something; research pulled a forum thread and the answer just recapped the
# forum ("the thread suggests…") instead of telling them how to achieve it on
# THEIR setup. This prompt inverts that: sources are raw material to MINE a
# working procedure from, then adapt to the project and hand back as the steps the
# operator takes — never a summary of a page's contents.
_RESEARCH_SYNTH_SYSTEM = (
    "You help ONE operator ACHIEVE something in an in-progress project — they "
    "paused mid-step to ask you how to do it. Give them a concrete, do-this-now "
    "answer for THEIR situation. Do NOT summarize what a forum thread, wiki, or "
    "search result says.\n"
    "Sources are raw material, not the answer — a forum post, doc, or search hit "
    "is where you MINE the working procedure; then adapt it to this project's "
    "setup (from the PROJECT CONTEXT: its hosts, addresses, decisions already "
    "made) and hand back the exact steps the operator should take. NEVER answer "
    "with 'the forum suggests…', 'according to the thread…', or a recap of a "
    "page — turn it into what the operator actually DOES. If sources disagree or "
    "are version-specific, pick the approach that fits this project and say in one "
    "line why.\n"
    "When the PROJECT CONTEXT already settles part of it (a chosen host name, IP, "
    "filesystem, or decision), use THOSE specifics rather than generic "
    "placeholders — they are asking about THIS project, not the topic in general.\n"
    "Write for a beginner: simplest plain words, short sentences, expand any "
    "acronym / define jargon in 3-5 words the first time, commands and clicks "
    "copy-paste-ready with the exact button or menu text, one action per step. Be "
    "brief — the fewest steps that actually get them there; no background they did "
    "not ask for. Cite a source index like [1] for a specific fact or command you "
    "drew from it, but the ANSWER is the procedure, not the citation. If neither "
    "the project context nor the sources let you answer, say so plainly and name "
    "what you'd need — do not guess. No preamble, no filler.\n"
    "CURRENCY: do NOT state a specific version number, release name, or "
    "download URL from memory — those go stale. Use an exact version/URL ONLY "
    "when a source here confirms it. Otherwise tell the operator how to get the "
    "current one (e.g. 'download the latest LTS from the official releases "
    "page', 'check the newest driver with <command>') instead of naming a "
    "possibly-outdated one."
)


# §17.856 — the system-prompt directive appliers moved to
# app/modules/assist_directives.py; re-exported here so assist_guide.<NAME> and
# the tests keep resolving. (Private *_DIRECTIVE/_FRAMING constants are re-exported
# too — tests/test_assist_guide.py reads assist_guide._PROBLEM_SOLVING_FRAMING /
# ._NEXT_CALLOUT_DIRECTIVE.)
from app.modules.assist_directives import (  # noqa: F401,E402
    VERBOSITY_LEVELS,
    apply_verbosity,
    guide_system_for_tool,
    apply_problem_solving,
    apply_next_callout,
    apply_ground_or_ask,
    apply_screen_grounding,
    apply_location_callout,
    _PROBLEM_SOLVING_FRAMING,
    _NEXT_CALLOUT_DIRECTIVE,
    _GROUND_OR_ASK_DIRECTIVE,
    _SCREEN_GROUNDING_DIRECTIVE,
    _LOCATION_CALLOUT_DIRECTIVE,
)

# §17.856 — the block renderers moved to app/modules/assist_render.py;
# re-exported so assist_guide.<NAME> and the external callers keep resolving.
from app.modules.assist_render import (  # noqa: F401,E402
    render_environment_block,
    render_facts_block,
    render_operator_notes_block,
    _operator_reset_intent,
    render_session_memory,
    _render_memory_or_legacy,
    render_conversation_block,
    render_step_recap_block,
    render_project_recap_block,
    _recap_add,
    parse_recap,
    render_status_panel,
    _RESET_INTENT_PATTERNS,
    _RECAP_LABELS,
)

# §17.856 — placeholder resolution moved to app/modules/assist_placeholders.py;
# re-exported so assist_guide.<NAME> keeps resolving.
from app.modules.assist_placeholders import (  # noqa: F401,E402
    resolve_placeholders,
    find_placeholders,
    extract_substitutions,
    _PLACEHOLDER_TOKEN_RE,
    _UNSAFE_RESOLVER_VALUE_RE,
    _PLACEHOLDER_RESOLVER_TOOL,
    _PLACEHOLDER_RESOLVER_SYSTEM,
    _PLACEHOLDER_RE,
    _LEARN_SUBS_TOOL,
)


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
    environment_block: str = "",
) -> list[str]:
    """Ask the model which facts a human would need to confirm. Fail-soft.

    §17.771 (Phase 3) — when the operator's observed system (`environment_block`
    = profile + §17.709 facts ledger) is given, the queries are grounded in THEIR
    box (their GPU model, OS version, board) instead of generic lookups. This is
    what makes a decision step's researched options system-SPECIFIC rather than a
    textbook list — the sharpest tailoring gap the §17.771 audit found (the option
    research pre-pass was environment-blind while the render prompt only narrated
    facts after the fact)."""
    if max_queries <= 0:
        return []
    env = (environment_block or "").strip()
    env_line = (
        "\nThe operator's ACTUAL system (ground every query in THIS — ask about "
        f"their real hardware / OS / installed versions, not generic ones):\n{env}\n"
        if env else ""
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": (
                    "You help a human prepare to execute a task. List only "
                    "lookups that genuinely matter for correctness; prefer an "
                    "empty list over speculative queries. When the operator's "
                    "system is given, make each query SPECIFIC to it (their exact "
                    "GPU/board/OS version) rather than generic."
                )},
                {"role": "user", "content": (
                    f"Task tool: {tool}\n{env_line}\nTask:\n{task_text}\n\n"
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
    fetch the result pages. Fail-soft → [].

    §17.729 — brought in line with `execution_agent._searxng_search`: this DEEP
    path (the one `/assist research` + `/assist fix` use — the reported "gave me
    Ubuntu 22.04.3 from memory" failure went through here) still called
    `categories=general` (additive, floods with keyword-matchers per §17.503)
    with NO 0-results fallback. Now it uses the curated `engines` backbone,
    retries the widest net on 0 results (§17.712), and RELEVANCE-FILTERS the
    hits so a live keyword-matcher's navigational junk can't reach the fetcher.
    """
    from app.utils.http_clients import get_searxng_client
    from app.modules.research_extractors import (
        _engines_for_category, SEARXNG_FALLBACK_ENGINES, relevant_search_results,
    )
    try:
        client = get_searxng_client()
        resp = await client.get(
            "/search",
            params={"q": query, "format": "json",
                    "engines": _engines_for_category("general")},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            fb = await client.get(
                "/search",
                params={"q": query, "format": "json",
                        "engines": SEARXNG_FALLBACK_ENGINES},
            )
            if fb.status_code == 200:
                results = fb.json().get("results") or []
        results = relevant_search_results(query, results)
        return [
            {"title": r.get("title", ""), "content": r.get("content", ""), "url": r.get("url", "")}
            for r in results[:max_results]
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
    kb_query_extra: Optional[str] = None, web_query: Optional[str] = None,
) -> list[dict]:
    """Confirm one query via Milvus (local KB) + web.

    ``deep`` (used by /assist research + /assist fix) fetches & extracts the top
    SearXNG result PAGES (real doc content); otherwise (the auto-guide pre-pass)
    it uses fast search snippets. ``kb_query_extra`` (§17.650) biases ONLY the
    local-KB embedding query with project entities (brief/environment) so a
    generic operator question still retrieves this project's ingested research.
    ``web_query`` (§17.729) is the query used for the WEB search — a
    keyword-focused rewrite of a conversational question; the KB query stays the
    raw ``query`` (embeddings handle prose fine, and §17.650 keeps the KB query
    intact). Defaults to ``query`` so callers that don't focus are unchanged.
    Returns ``{query, kind, text[, url]}`` source dicts, only non-empty/
    non-failure. Never raises (helpers are fail-soft).
    """
    from app.modules.execution_agent import _milvus_search, _searxng_search

    sources: list[dict] = []
    kb_query = f"{query}\n{kb_query_extra.strip()}" if (kb_query_extra or "").strip() else query
    web_q = (web_query or "").strip() or query
    milvus = await _milvus_search(kb_query, node_key=node_key, domain=domain)
    if _is_useful_grounding(milvus):
        sources.append({"query": query, "kind": "milvus", "text": milvus.strip()})

    if deep and settings.assist_research_fetch_top_n > 0:
        web = await _deep_web_sources(web_q, top_n=settings.assist_research_fetch_top_n)
        if web:
            sources.extend(web)
            return sources
        # fetch found nothing → fall through to the snippet path.

    searx = await _searxng_search(web_q)
    if _is_useful_grounding(searx):
        sources.append({"query": web_q, "kind": "searxng", "text": searx.strip()})
    return sources


async def _research_prepass(
    *, task_text: str, tool: str, role: str, max_queries: int,
    node_key: str, domain: Optional[str], deep: bool = False,
    environment_block: str = "",
) -> list[dict]:
    queries = await _detect_unknowns(
        task_text=task_text, tool=tool, role=role, max_queries=max_queries,
        environment_block=environment_block,
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
        # §17.643 — research is for the model's ACCURACY, not for the reader.
        # The old header ("authoritative facts; use them") led the model to
        # transcribe the research depth into the walkthrough, ~doubling its
        # length and burying the steps in expert detail. Use it silently.
        "## Research (confirmed facts — for YOUR accuracy only, NOT to reproduce)\n"
        "Use these to get package names, versions, flags, and exact commands "
        "right. Do NOT copy this material, its background, or its depth into the "
        "walkthrough — the reader needs the steps, not the research.\n"
        "§17.729 CURRENCY: a version number, release name, or download URL that "
        "these sources do NOT confirm is likely STALE — do not state it from "
        "memory. Prefer telling the operator to fetch the current one (latest "
        "LTS / newest driver) over naming a possibly-outdated specific value."
    ]
    for i, s in enumerate(sources, 1):
        src = f"{s['kind']}: {s['url']}" if s.get("url") else s["kind"]
        parts.append(f"[{i}] ({src}) query: {s['query']}\n{s['text']}")
    return "\n\n".join(parts)












_FACTS_SWEEP_SYSTEM = (
    "You maintain a system-state ledger for a hands-on build. The operator has "
    "just declared a RESET / REBUILD — they are erasing or starting over a part of "
    "the system. Some recorded facts now describe the ABANDONED thing (the machine "
    "being destroyed and its problems, config that was wiped, guest-OS state that "
    "no longer exists) and must be RETRACTED so they stop misleading later steps. "
    "OTHER facts are DURABLE and must be KEPT: physical hardware, the host / "
    "hypervisor configuration, network and storage infrastructure that survives the "
    "rebuild, and any fact about the NEW system being built. Call "
    "report_superseded_facts with the indices of ONLY the superseded "
    "(abandoned-system) facts. Be precise and conservative: when unsure whether a "
    "fact survives the rebuild, KEEP it (do not retract). Never retract a fact "
    "about the host, the network/bridge, storage, or the new build."
)

_REPORT_SUPERSEDED_TOOL = model_router.Tool(
    name="report_superseded_facts",
    description=(
        "Report which recorded facts describe the ABANDONED system the operator's "
        "reset/rebuild supersedes, so they can be retracted from the ledger."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "superseded_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "The 0-based indices of facts that describe the abandoned "
                    "system (empty if none are superseded)."
                ),
            },
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["superseded_indices"],
    },
)


async def classify_superseded_facts(
    *, note_text: str, facts: list[str], role: str = "model_general",
) -> list[int]:
    """§17.755 — given a reset/rebuild note and the numbered facts ledger, return
    the indices of facts that describe the ABANDONED system (to retract). Durable
    host/network/storage/new-build facts are kept. Fail-soft → [] (retract nothing)
    so a flaky classifier never nukes the ledger."""
    facts = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not facts:
        return []
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _FACTS_SWEEP_SYSTEM},
                {"role": "user", "content": (
                    f"The operator just declared a reset/rebuild:\n"
                    f"\"{(note_text or '').strip()[:1500]}\"\n\n"
                    f"Currently recorded system facts (numbered):\n{numbered[:9000]}\n\n"
                    "Call report_superseded_facts with the indices of the "
                    "abandoned-system facts."
                )},
            ],
            tools=[_REPORT_SUPERSEDED_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # §17.583/727 — thinking model reasons before the tool call; a big
            # numbered-facts ledger needs a generous budget or it returns empty args.
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — a flaky sweep must never break note-taking
        logger.warning("assist_facts_sweep_classify_failed: %s", exc)
        return []
    args = read_tool_args(resp)
    idxs = (args or {}).get("superseded_indices") or []
    return sorted({i for i in idxs if isinstance(i, int) and 0 <= i < len(facts)})


_DURABLE_FACTS_SYSTEM = (
    "You maintain a SHARED infrastructure ledger for a multi-component build on ONE "
    "physical system (e.g. a homelab: several VMs/services on one Proxmox host). "
    "Given ONE component's observed facts, return the indices of facts that are "
    "DURABLE, cross-cutting INFRASTRUCTURE that OTHER components on the same system "
    "would reuse: physical hardware (CPU, RAM, disks, storage controllers, GPUs, "
    "PCI devices), host network topology (bridges, physical NICs, subnets, "
    "gateways, NAT, DNS, the host's own IP), storage (pools, volumes, filesystems, "
    "datastores), and host access (hypervisor URL, how to log in, versions). "
    "EXCLUDE: transient states (a link/device currently up or down, 'currently at "
    "screen X', a boot loop, an in-progress error, a value being waited on), and "
    "facts specific to ONE component's own workload (a particular VM's guest-OS "
    "state, one service's internal config) that a sibling would not reuse. When "
    "unsure whether a fact is durable SHARED infrastructure, EXCLUDE it — the goal "
    "is a clean shared baseline, not completeness."
)

_REPORT_DURABLE_TOOL = model_router.Tool(
    name="report_durable_facts",
    description=(
        "Report which of a component's facts are durable, cross-cutting "
        "infrastructure that sibling components on the same physical system reuse."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "durable_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "0-based indices of the durable shared-infrastructure facts "
                    "(empty if none qualify)."
                ),
            },
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["durable_indices"],
    },
)


async def classify_durable_facts(
    *, facts: list[str], role: str = "model_general",
) -> list[int] | None:
    """§17.759 — given a component's facts, return the indices of the DURABLE,
    cross-cutting infrastructure facts (shared host/network/storage/hardware) that
    sibling components should inherit — excluding transient states and
    component-specific detail. Returns ``None`` on a model/parse FAILURE (so the
    caller falls back to sharing all facts, the §17.757 behavior) vs ``[]`` when
    the model genuinely found no durable facts."""
    facts = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not facts:
        return []
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _DURABLE_FACTS_SYSTEM},
                {"role": "user", "content": (
                    f"A component's observed system facts (numbered):\n{numbered[:9000]}\n\n"
                    "Call report_durable_facts with the indices of the durable "
                    "shared-infrastructure facts."
                )},
            ],
            tools=[_REPORT_DURABLE_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # §17.583/727 — a thinking model (model_general) reasons before the tool
            # call; a big numbered-facts prompt needs a generous budget or it returns
            # EMPTY tool args (observed: 40 facts at 2048 → no args → None).
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — never break sharing on a flaky classifier
        logger.warning("assist_durable_facts_classify_failed: %s", exc)
        return None
    args = read_tool_args(resp)
    if not args or "durable_indices" not in args:
        return None
    idxs = args.get("durable_indices") or []
    return sorted({i for i in idxs if isinstance(i, int) and 0 <= i < len(facts)})








# ── Success verification (§17.487 — did the submitted step actually work?) ─

_JUDGE_OUTCOME_TOOL = model_router.Tool(
    name="judge_step_outcome",
    description=(
        "Judge, from the operator's pasted evidence, whether THIS step's actual "
        "goal (the Task title) was achieved. Four outcomes:\n"
        "- 'succeeded': the evidence shows the step's DELIVERABLE is done (e.g. "
        "for 'Install the OS', a login prompt / `lsb_release` from inside the "
        "installed system; for 'Configure X', the applied config confirmed).\n"
        "- 'failed': a clear failure signal — error, traceback, non-zero exit, "
        "'command not found', 'permission denied', 'No such file'.\n"
        "- 'incomplete': the evidence shows the operator did SETUP or an EARLIER "
        "phase of this step but NOT the step's actual deliverable — e.g. 'Install "
        "the OS' but the evidence only downloads the installer ISO or sits at the "
        "boot/installer menu; 'Install the driver' but the evidence is the same "
        "download / a boot screen. Use this when the goal is affirmatively NOT "
        "reached yet — NOT for a valid alternative that DOES meet the goal when a "
        "hardware/software constraint rules out the literally-named method (that "
        "is 'succeeded').\n"
        "- 'unclear': you genuinely can't tell. \n"
        "Be conservative about 'incomplete' and 'failed': if the deliverable "
        "MIGHT be present, or you can't tell, use 'unclear' (not 'incomplete'). "
        "Judge against the TASK TITLE's goal, not merely 'is there an error'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string",
                        "enum": ["succeeded", "failed", "incomplete", "unclear"]},
            "reason": {"type": "string", "description": "One sentence, citing the signal."},
            "suggestion": {"type": "string",
                           "description": "If failed/incomplete: the concrete next move to finish the step."},
            "goal_met_via_alternative": {
                "type": "boolean",
                "description": (
                    "True ONLY when outcome='succeeded' AND the operator met the "
                    "step's GOAL via a DIFFERENT method than the task literally "
                    "named, because a hardware/software CONSTRAINT made the named "
                    "method impossible (e.g. the board's chip locks PWM to "
                    "automatic, so manual fan curves aren't possible, but "
                    "automatic control keeps temps safe). False for a "
                    "straightforward success done the planned way."),
            },
            "constraint": {
                "type": "string",
                "description": (
                    "If goal_met_via_alternative: the specific constraint that "
                    "ruled out the named method AND how the goal was met instead, "
                    "as one durable sentence the plan should carry forward (e.g. "
                    "'This board's NCT7904D locks pwm_enable to automatic, so "
                    "manual fan curves aren't possible; fan control is automatic "
                    "only and holds temps in range'). Empty otherwise."),
            },
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


# §17.688 — a DECISION node's deliverable is a CHOICE, not a finished artifact.
# The default verifier judged an operator's decision ("3 vlans", "option a")
# against the node's concrete-artifact task text ("Produce a table: VLAN ID,
# subnet/CIDR, DHCP scope, isolation rules") and returned 'failed' → the user
# who correctly answered the framed question got "⚠️ This may have failed." A
# decision step is judged on whether a clear, on-topic choice was made; the
# concrete artifact is applied by later implementer steps (e.g. T2 "Define VLAN
# plan" decides → T12 "Create VLAN interfaces" / T17 "Configure switch" apply).
_VERIFY_DECISION_SYSTEM = (
    "You verify whether a human operator MADE THE DECISION a planning step asked "
    "of them, from what they wrote. This step's deliverable is a CHOICE or a "
    "stated direction — NOT a finished artifact, command output, or a fully "
    "specified table/config. The concrete implementation (exact IDs, subnets, "
    "commands, config values) is produced by LATER steps that apply this "
    "decision. Return 'succeeded' when the operator expressed a clear, on-topic "
    "choice or direction for THIS decision (picked an option, stated a "
    "count/approach, or gave a constraint that settles it). Return 'failed' ONLY "
    "if the message is empty, off-topic, or explicitly refuses to decide. Return "
    "'unclear' only if you genuinely cannot tell. NEVER return 'failed' merely "
    "because the operator did not reproduce the full concrete artifact named in "
    "the task — for a decision step that is expected and correct."
)


async def verify_step_success(
    *, title: str, task_prompt: str, tool: str, evidence: str,
    environment: Optional[dict] = None, is_decision: bool = False,
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
    # A decision step produces no code, so the sandbox path never applies.
    sandbox: Optional[dict] = None
    if (not is_decision) and (tool or "").lower() == "codegen" \
            and settings.codegen_execution_check_enabled \
            and (settings.coderunner_url or "").strip():
        sandbox = await _sandbox_codegen_check(evidence)
        if sandbox and sandbox["verdict"] == "fail":
            return {
                "outcome": "failed",
                "reason": sandbox["reason"],
                "suggestion": "Fix the runtime error and resubmit — `/assist fix <the error>` can help.",
                "grounded_by": "sandbox",
            }

    # §17.710b — verify grounds on the unified memory (facts help judge against
    # the real system) or the legacy env block, per the valve. No notes here.
    env_block = "\n\n".join(_render_memory_or_legacy(environment, None))
    if is_decision:  # §17.688 — judge the CHOICE, not the downstream artifact
        system = _VERIFY_DECISION_SYSTEM
        user = (
            f"Decision step: {title}\n\n"
            "What this decision is ultimately about (context — NOT a checklist the "
            "operator's answer must fully satisfy; later steps apply the concrete "
            f"details):\n{task_prompt}\n\n"
            + (f"{env_block}\n\n" if env_block else "")
            + f"Operator's decision / message for this step:\n{evidence[:6000]}\n\n"
            "Judge whether they made a clear, on-topic decision. Call judge_step_outcome."
        )
    else:
        system = (
            "You verify whether a human operator's step achieved ITS GOAL, from "
            "the output they pasted. Judge against the step's Task title and its "
            "underlying GOAL — not a literal method checklist, and not merely 'is "
            "there an error'. §17.731: if the evidence shows only SETUP or an "
            "earlier phase (e.g. the installer downloaded / the boot menu shown, "
            "but the OS not actually installed), the step is 'incomplete', not "
            "'succeeded'.\n"
            "§17.771 — CREDIT A VALID ALTERNATIVE: return 'succeeded' when the "
            "evidence shows the step's GOAL is met, EVEN IF via a different method "
            "than the task literally names — ESPECIALLY when the evidence also "
            "shows the operator hit a genuine hardware/software CONSTRAINT that "
            "makes the named method impossible (e.g. the task says 'manual PWM fan "
            "control via fancontrol' but the board's sensor chip locks PWM to "
            "automatic, and the evidence shows temperatures held safe under load). "
            "Do NOT return 'incomplete' merely because the exact named tool/method "
            "wasn't used, when the outcome the step exists for is demonstrably "
            "achieved. 'incomplete' is for when the GOAL ITSELF is not yet reached, "
            "NOT for a valid alternative path to the same goal.\n"
            "Stay conservative: 'failed' only on a clear failure signal; "
            "'incomplete' only when the goal is affirmatively NOT reached yet; "
            "'unclear' when you genuinely can't tell (do NOT guess 'incomplete' out "
            "of caution)."
        )
        user = (
            f"Task (this step's goal): {title}\n\n{task_prompt}\n\n"
            + (f"{env_block}\n\n" if env_block else "")
            + f"Operator's pasted evidence / output for this step:\n{evidence[:6000]}\n\n"
            "Does the evidence show THIS step's goal was achieved? Call judge_step_outcome."
        )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
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
    if outcome not in ("succeeded", "failed", "incomplete", "unclear"):
        outcome = "unclear"
    reason = (args.get("reason") or "").strip()
    # §17.491 — the code ran cleanly in the sandbox; the LLM judged task-fit.
    # Record that the success is sandbox-backed, not just a text judgment.
    grounded_by = "model"
    if sandbox and sandbox["verdict"] == "pass":
        grounded_by = "sandbox+model"
        if outcome == "succeeded":
            reason = (reason + " (code executed cleanly in the sandbox)").strip()
    # §17.771 — the constraint-adaptation signal (only meaningful on 'succeeded').
    via_alt = bool(args.get("goal_met_via_alternative")) and outcome == "succeeded"
    constraint = (args.get("constraint") or "").strip() if via_alt else ""
    return {
        "outcome": outcome,
        "reason": reason,
        "suggestion": (args.get("suggestion") or "").strip(),
        "grounded_by": grounded_by,
        "goal_met_via_alternative": via_alt and bool(constraint),
        "constraint": constraint,
    }


# ── Multi-turn decision deliberation (§17.689) ─────────────────────────────
# A decision node whose deliverable is a CONCRETE artifact (a VLAN table, a
# partition layout, a config set) is assembled ACROSS turns rather than
# committed on the operator's first partial answer. Each decision turn either
# proposes a specific artifact to confirm/adjust (needs_input) or, once the
# operator has confirmed / the choices fully determine it, emits the complete
# artifact to record (resolved). A simple binary decision resolves in one turn.

_DELIBERATE_TOOL = model_router.Tool(
    name="resolve_or_continue",
    description=(
        "Report whether the operator's decision is now fully settled (resolved) "
        "or needs another round (needs_input), with the operator-facing message "
        "and, when resolved, the concrete decision to record for later steps."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["needs_input", "resolved"]},
            "message": {
                "type": "string",
                "description": (
                    "Markdown shown to the operator. For needs_input: a SPECIFIC "
                    "concrete proposal they can accept or adjust, then one short "
                    "line inviting them to confirm or say what to change. For "
                    "resolved: a brief confirmation of what was decided."
                ),
            },
            "decision_record": {
                "type": "string",
                "description": (
                    "REQUIRED when resolved: the COMPLETE, self-contained concrete "
                    "artifact to persist as this step's output (e.g. the full table "
                    "with every field). Later steps build directly from this. Leave "
                    "empty for needs_input."
                ),
            },
        },
        "required": ["status", "message"],
    },
)

_DELIBERATE_SYSTEM_DECISION = """You help a human operator finalize ONE decision step whose deliverable is a CONCRETE artifact (a table / list / plan / config with specific values). You are given the step's task, the project context, and the conversation so far (including any proposal you already made). Call resolve_or_continue exactly once.

Choose the outcome:

- status="resolved" when the operator's choices now fully determine the concrete deliverable, OR the operator has CONFIRMED a proposal you made (e.g. "looks good", "yes", "go with that", "that works"). Emit `decision_record` = the COMPLETE concrete artifact, assembled from what the operator chose — every row/field the task asks for, self-contained, ready for later steps to build from.

- status="needs_input" when more is needed to make the deliverable concrete. In `message`, make a DECISIVE recommendation: lead with the ONE option you recommend and the single main reason it fits THIS operator's system and goal, then PROPOSE the specific, ready-to-use default that reflects it (real, usable values — take concrete values from the operator environment when given; use a clearly-labeled <PLACEHOLDER> ONLY for something only the operator can supply, e.g. their exact ISP-facing IP). Do not dump a neutral menu of equal options and ask them to pick — commit to a recommendation and let them override it. Frame it as your recommendation, not a settled fact ("I'd go with X — reason. Sound good, or change it?"). If a FOUNDATIONAL choice is still open, recommend one (with the real alternatives named) — do NOT fabricate past a choice the operator has not made.

Rules:
- Resolve as soon as the operator confirms — never force an extra round once they've said yes.
- When a Research section is present, GROUND the recommendation and any versions/package names/flags in it — prefer its current, system-specific facts over anything from memory (§17.729 currency). Do NOT copy the research depth into the message; the operator needs the recommendation, not the sources.
- Do NOT pre-assume a count, topology, or set the operator has not agreed to.
- GROUND on the observed system facts + project context above. NEVER assume the system is fresh, empty, or new: if the facts describe an EXISTING system, decide for THAT system; if a needed detail was not captured or a check was inconclusive (a command errored / returned empty), treat it as UNKNOWN and ask — do NOT invent a "fresh install" assumption to fill the gap. EXCEPTION (§17.714): if the session memory says the operator has CHANGED DIRECTION / chosen to reinstall or rebuild, follow THAT decision — a fresh start is then what they asked for, not an assumption; treat the earlier "observed facts" as describing the abandoned system.
- Never invent values the operator must own (real public IPs, ISP specifics) — use sensible, clearly-labeled defaults they can change.
- Keep `message` tight and skimmable. No preamble, no emoji, no completion checkmarks."""

# §17.690 — the SAME across-turns machinery for a GATHER step: one whose task
# asks the operator to PROVIDE several specific pieces of information (e.g.
# "Operator provides: exact model, disk inventory, GPU(s), NIC models"). The
# operator commonly supplies these ONE PORTION AT A TIME; the step used to
# commit on the first portion (the reported bug — disk inventory pasted, step
# committed with model/GPU/NIC still missing). Now it accumulates across turns
# and only resolves once every requested item is present.
_DELIBERATE_SYSTEM_GATHER = """You help a human operator complete ONE step whose deliverable is SPECIFIC INFORMATION they provide — the task lists exactly which items. You are given the step's task, the project context, and the conversation so far (which may already contain items the operator supplied in EARLIER turns). Call resolve_or_continue exactly once.

The operator often provides the requested items ONE PIECE AT A TIME across several messages. Your job is to accumulate them and know when the set is complete.

Choose the outcome:

- status="resolved" when EVERY item the task asks for has now been provided across the whole conversation (an item the operator marks as absent or unknown — "no GPU", "not sure of the NIC model", "N/A" — COUNTS as provided; do not block on it). Emit `decision_record` = the COMPLETE, self-contained record of ALL the gathered information, every requested item labeled, assembled from the entire conversation — ready for later steps to build from.

- status="needs_input" when one or more requested items are STILL missing. In `message`: briefly acknowledge what you have CAPTURED so far, then clearly list ONLY the specific items still MISSING, and invite the operator to provide the rest. They may answer a piece at a time.

Rules:
- Track the WHOLE conversation: information given in earlier turns still counts — NEVER re-ask for something already provided, and never discard earlier pieces.
- GROUND on the observed system facts + project context above — a fact captured from an earlier step (e.g. the audit) COUNTS as provided; do not re-ask for it, and never assume a fresh/empty system to skip an item.
- Resolve as soon as the full set is present (or explicitly marked absent/unknown) — do not force extra rounds.
- Do NOT invent or assume values the operator must supply — ask for them.
- Keep `message` tight and skimmable. No preamble, no emoji, no completion checkmarks."""

_DELIBERATE_SYSTEMS = {
    "decision": _DELIBERATE_SYSTEM_DECISION,
    "gather": _DELIBERATE_SYSTEM_GATHER,
}


async def deliberate_decision(
    *,
    title: str,
    task_prompt: str,
    environment: Optional[dict] = None,
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    conversation: Optional[str] = None,
    latest_message: str,
    kind: str = "decision",
    research_block: str = "",
) -> dict:
    """§17.689/§17.690 — one turn of a COLLECT step's deliberation.

    ``kind='decision'`` — the deliverable is a choice / concrete artifact the
    operator decides (§17.689). ``kind='gather'`` — the deliverable is specific
    information the operator supplies, often one portion at a time (§17.690).
    Both accumulate across turns and commit only when complete.

    Returns ``{status: 'needs_input'|'resolved'|'error', message, decision_record}``.
    Fail-soft: any model/parse failure returns ``status='error'`` so the caller
    can fall back to the plain single-turn commit rather than trapping the
    operator in a broken loop.
    """
    role = settings.assist_guide_model_role
    system = _DELIBERATE_SYSTEMS.get(kind, _DELIBERATE_SYSTEM_DECISION)
    lead = (
        f"Gather step: {title}\n\n"
        f"The information this step must collect from the operator:\n{task_prompt}"
        if kind == "gather" else
        f"Decision step: {title}\n\n"
        f"What this step must ultimately produce (the concrete deliverable):\n{task_prompt}"
    )
    parts = [lead]
    if job_digest and job_digest.strip():
        parts.append(job_digest.strip())
    # §17.710b — unified session memory (or legacy env + notes) per the valve.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    # §17.771 (Phase 3) — evidence-backed commit: current, system-specific facts
    # so the recommendation isn't drawn from stale model memory (the audit's
    # "commit path has zero fresh research" gap). Decision kind only; "" no-ops.
    if research_block and research_block.strip():
        parts.append(research_block.strip())
    if conversation and conversation.strip():
        parts.append(conversation.strip())
    parts.append(f"## Operator's latest message\n{(latest_message or '').strip()}")
    parts.append(
        "Decide whether this step is now resolved (complete) or needs another "
        "round, and call resolve_or_continue."
    )
    user = "\n\n".join(parts)
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            [_DELIBERATE_TOOL],
            role=role,
            temperature=0.2,
            max_tokens=settings.assist_guide_max_tokens,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — fall back to plain commit
        logger.warning("assist_deliberate_decision_failed: %s", exc)
        return {"status": "error", "message": "", "decision_record": ""}
    args = None
    if resp and resp.success and resp.tool_calls:
        args = resp.tool_calls[0].arguments or {}
    if not args:
        logger.warning("assist_deliberate_decision_unparsed title=%r", title)
        return {"status": "error", "message": "", "decision_record": ""}
    status = args.get("status")
    if status not in ("needs_input", "resolved"):
        return {"status": "error", "message": "", "decision_record": ""}
    return {
        "status": status,
        "message": (args.get("message") or "").strip(),
        "decision_record": (args.get("decision_record") or "").strip(),
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
    "ask", "question", "note",
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
                    "ask = they want a real ANSWER or hands-on HELP with a task — not a re-rendering of the current step. Covers (a) a factual lookup ('what is X', 'latest version', 'is X safe', comparisons), (b) a project/design/planning question about their setup or another part of the plan ('which of my two machines should host WireGuard', 'how should the VLANs be laid out', 'do I need X for Y'), AND (c) a PRACTICAL HOW-TO they need help accomplishing — 'how do I connect the Proxmox server to my laptop', 'how do I set up X', 'help me get Y talking to Z', 'how to configure/connect/install W'. In every case a researched, project-aware answer with the actual steps helps more than re-showing the current step. If they want to DO something, need help, or want a real answer, it is ask — PIVOT and help them, even when it's a sub-task or a task other than the current step. "
                    "question = they want to clarify or ADJUST the WORDING of the CURRENT step as shown — what a part of it means, redo THIS step for a different OS, more detail on one bullet. Use this ONLY when they're asking about the step's own instructions in front of them. A how-to, a request for help DOING something, or a question about the wider project / a different task is ask, NOT question. Do NOT default here for how-to or help requests. "
                    "note = they are RECORDING a new requirement, constraint, preference, or decision to remember for the rest of the project — not asking a question and not reporting they did the current step ('also, I want a DMZ', 'remember I only have 2 NICs', 'note: everything must survive a reboot', 'from now on use 10.x addresses', 'add a backup step later'). These are additions to keep, not the current step's evidence. When in doubt between note and submit: if it states a fact/wish to carry FORWARD rather than the OUTCOME of the current step, it is note."
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
            "note_text": {
                "type": "string",
                "description": (
                    "When intent=note: the requirement / constraint / preference "
                    "/ decision to remember, as a short standalone statement "
                    "(e.g. 'wants a DMZ segment', 'only 2 physical NICs "
                    "available', 'all VMs must survive a host reboot'). Omit otherwise."
                ),
            },
            "note_kind": {
                "type": "string",
                "enum": ["addition", "constraint", "preference", "decision", "note"],
                "description": (
                    "When intent=note: how to tag it — addition (a new thing to "
                    "include), constraint (a hard limit), preference (a soft "
                    "want), decision (a choice already made), or note (generic). "
                    "Omit otherwise."
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
    "- wants an ANSWER or HANDS-ON HELP with a task → ask. This covers a factual "
    "lookup ('difference between ZFS and LVM', 'latest Proxmox version', 'is ZFS "
    "safe on non-ECC'), a project/design question about THEIR setup or another part "
    "of the plan ('which of my two machines should host WireGuard', 'how should I "
    "lay out the VLANs', 'do I need a separate NIC'), AND a practical HOW-TO they "
    "need help doing ('how do I connect the Proxmox server to my laptop', 'how do I "
    "set up X', 'help me get Y talking to Z', 'how to configure/connect/install "
    "W'). If they want a real answer or help DOING something, it is ask — PIVOT and "
    "help, even for a sub-task or a task other than the current step.\n"
    "- wants to CLARIFY/ADJUST the WORDING of the CURRENT step as shown ('what does "
    "part 2 mean', 'redo this one for macOS', 'more detail on the third bullet') → "
    "question. ONLY when they're asking about the step's own instructions in front "
    "of them. A how-to, a request for help, or a question about a different task is "
    "ask, NOT question — do not default here for how-to/help.\n"
    "- tells you about their MACHINE ('I'm on Ubuntu 24.04', 'IP is 10.0.0.5') → set_env\n"
    "- RECORDS a new requirement/constraint/preference/decision to carry forward "
    "for the rest of the project, not tied to finishing the current step ('also I "
    "want a DMZ', 'remember I only have 2 NICs', 'from now on use 10.x') → note. "
    "Distinguish from submit: note carries a fact/wish FORWARD; submit reports the "
    "current step's OUTCOME.\n"
    "Call classify_turn exactly once."
)


_CLASSIFY_DECISION_HINT = (
    "\n\nTHIS STEP IS A DECISION (its deliverable is a CHOICE the operator "
    "makes). Treat the operator MAKING or CONFIRMING that choice as submit: "
    "picking an option, stating a count / approach / value ('3 vlans', 'go with "
    "ZFS'), OR confirming a proposal you offered ('looks good', 'yes', 'that "
    "works', 'go with that', 'perfect') → submit. Only route to ask when they "
    "want a real answer to a QUESTION (e.g. 'what's the difference between the "
    "options', 'what do you recommend and why') rather than stating their pick."
)


async def classify_turn(
    *, message: str, title: str, task_prompt: str, tool: str,
    role: str | None = None, conversation: str | None = None,
    is_decision: bool = False,
) -> dict:
    """Classify an operator's plain-language turn into an assist intent.

    Returns ``{"intent": <one of ASSIST_INTENTS>, "evidence": str,
    "error_text": str, "query": str}``. Fail-soft: on any model/parse error
    returns ``intent='question'`` so a flaky classifier degrades to the guide/
    refine behavior rather than misfiring a submit/skip/handoff.

    §17.689 — ``is_decision`` biases a made/confirmed CHOICE toward ``submit`` so
    it reaches the server-side decision-deliberation path (which assembles the
    concrete artifact across turns) instead of being read as a bare question.

    §17.687 — ``conversation`` (the recent back-and-forth) lets the classifier
    resolve a message whose intent depends on what was JUST said ("yes, that
    one", "tell me more about the program you suggested") — anaphora it cannot
    read from the current step alone."""
    role = role or settings.assist_classify_model_role
    fallback = {"intent": "question", "evidence": "", "error_text": "", "query": "",
                "note_text": "", "note_kind": "note"}
    convo_block = (
        f"{conversation.strip()}\n\n" if (conversation or "").strip() else ""
    )
    user = (
        f"Current step: {title}\n\n"
        f"What the step asks:\n{(task_prompt or '')[:1500]}\n\n"
        f"Tool for this step: {tool or 'LLM'}\n\n"
        f"{convo_block}"
        f"Operator's message:\n{message[:2000]}\n\n"
        "Call classify_turn."
    )
    system = _CLASSIFY_SYSTEM + (_CLASSIFY_DECISION_HINT if is_decision else "")
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
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
    note_kind = (args.get("note_kind") or "note").strip()
    return {
        "intent": intent,
        "evidence": (args.get("evidence") or "").strip(),
        "error_text": (args.get("error_text") or "").strip(),
        "query": (args.get("query") or "").strip(),
        "note_text": (args.get("note_text") or "").strip(),
        "note_kind": note_kind if note_kind in
        ("addition", "constraint", "preference", "decision", "note") else "note",
    }


# ── Auto-learn substitutions (§17.490 — concrete values from evidence) ─────








# ── Session facts ledger (§17.709 — durable observed system state) ──────────

_RECORD_FACTS_TOOL = model_router.Tool(
    name="record_facts",
    description=(
        "Record durable FACTS about the operator's ACTUAL system, observed in "
        "the command output they pasted, that LATER steps must know."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short, standalone factual statements about the operator's "
                    "REAL system — e.g. 'Existing Proxmox VE 9.2.6 (not a fresh "
                    "install)', 'Network: vmbr0 = 192.168.1.156/24, gw "
                    "192.168.1.1', 'Two ZFS pools: rpool, tank'. Rules: (1) only "
                    "state what the output actually shows; NEVER guess. (2) If a "
                    "command ERRORED or returned empty because a service was down "
                    "(e.g. 'Connection refused', 'command not found'), record that "
                    "the state is UNKNOWN/unverified — do NOT record 'none' or "
                    "'fresh' from a failed check. (3) Prefer facts about existing "
                    "software/versions, users, VMs/containers, storage/pools, and "
                    "network config. Return [] if the output shows nothing durable."
                ),
            },
            "superseded_facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "§17.725 — KNOWN facts (from the provided list) that this "
                    "output DIRECTLY CONTRADICTS, echoed VERBATIM, character for "
                    "character. Only a real conflict counts (the output shows the "
                    "opposite / a different value for the same thing). A "
                    "refinement, an addition, or a fact the output is silent on "
                    "is NOT superseded. Return [] when unsure."
                ),
            },
        },
        "required": ["facts"],
    },
)

_FACTS_SYSTEM = (
    "You extract durable facts about a human operator's ACTUAL system AND what "
    "they are BUILDING from the command output they pasted, so later steps ground "
    "on reality instead of assuming. Report only what the output shows; never "
    "guess.\n"
    "CAPTURE, specifically:\n"
    "- The BUILD SPEC the operator just created or configured — the concrete "
    "values that define their setup: names (VM/container/host/service names), "
    "resource allocations (RAM, cores, disk sizes), storage/pool choices, network "
    "(bridges, NICs, IPs, gateways, DNS), device assignments (PCI passthrough IDs, "
    "GPUs), OS/image/ISO and version choices, ports, users. E.g. a `qm create` "
    "that succeeds is the fact 'VM 100 (AI-VM) created: 4GB RAM, 2 cores, q35/OVMF, "
    "32G disk on local-lvm, NIC e1000 on DeFruscioBridge, ISO ubuntu-26.04, agent "
    "on'. These are the MOST important facts to keep — they are the operator's "
    "personal build.\n"
    "- Observed system STATE and blockers (what exists, what failed).\n"
    "PARTIAL ERRORS: a trailing or single error does NOT invalidate the parts that "
    "SUCCEEDED — capture the successful configuration AND, separately, record the "
    "error as its own fact (e.g. 'the `qm create` ran but `-bash: scsi0: command "
    "not found` — the boot-order `;` was unescaped so --agent/--boot may be "
    "incomplete'). Only treat an aspect as UNKNOWN when the check for THAT aspect "
    "errored or was blank — never infer a 'fresh'/'empty' system from a failure.\n"
    "If a KNOWN fact list is provided and this output directly contradicts one of "
    "those facts, echo that known fact VERBATIM in superseded_facts so the ledger "
    "can retract it — but only for a real conflict, never for an addition or "
    "refinement. Call record_facts exactly once."
)


def _match_superseded(raw: object, known_facts: list[str] | None) -> list[str]:
    """§17.725 — filter a model's ``superseded_facts`` echo down to entries that
    actually exist in the known ledger (normalized case/whitespace match),
    returning the LEDGER's spelling so retraction is an exact removal. A cap of
    5 bounds the damage a hallucinating model could do in one call."""
    if not raw or not isinstance(raw, list) or not known_facts:
        return []
    by_norm = {str(k).strip().lower(): str(k) for k in known_facts}
    out: list[str] = []
    for s in raw:
        hit = by_norm.get(str(s).strip().lower())
        if hit and hit not in out:
            out.append(hit)
        if len(out) >= 5:
            break
    return out


async def distill_facts(
    *, evidence: str, title: str = "", task_prompt: str = "",
    known_facts: list[str] | None = None, role: str = "model_general",
) -> dict:
    """§17.709 — distill durable facts about the operator's real system from
    their pasted evidence: ``{"facts": [str], "superseded": [str]}``.
    Reasoning/extraction task → ``model_general`` (NOT the verifier; cf.
    §17.677). Fail-soft → empty dict shape. Bounds each fact's length so a
    runaway model can't bloat the ledger. §17.725: when ``known_facts`` is
    given, the model may echo (verbatim) the known facts this output directly
    contradicts; ``superseded`` returns only echoes that match the ledger."""
    empty = {"facts": [], "superseded": []}
    if not (evidence or "").strip():
        return empty
    known_block = ""
    if known_facts:
        known_block = (
            "KNOWN FACTS (already in the ledger — do not repeat; if this output "
            "DIRECTLY CONTRADICTS one, echo it verbatim in superseded_facts):\n"
            + "\n".join(f"- {k}" for k in known_facts) + "\n\n"
        )
    user_msg = (
        (f"STEP: {title}\n" if title else "")
        + (f"TASK: {task_prompt}\n\n" if task_prompt else "\n")
        + known_block
        + f"Operator output:\n{evidence[:6000]}\n\nCall record_facts."
    )
    # §17.749 — model_general (deepseek-v4-pro) is a THINKING model: at
    # max_tokens=1024 it spends the whole budget reasoning and returns an EMPTY
    # tool call (§17.465/583/727), so fact capture intermittently recorded
    # NOTHING — a rich `qm create` submit distilled to zero facts, and the
    # operator's build spec never reached the ledger. Give it the 8192 floor
    # big-prompt tool calls need, and RE-DRAW when the model fails to call the
    # tool at all (an empty-content flake) rather than losing the facts silently.
    args = None
    for _draw in range(3):
        try:
            resp = await model_router.tool_call(
                messages=[
                    {"role": "system", "content": _FACTS_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=[_RECORD_FACTS_TOOL],
                role=role,
                temperature=0.0,
                tool_choice="auto",
                max_tokens=8192,
            )
        except Exception as exc:  # noqa: BLE001 — fact capture must never break submit
            logger.warning("assist_distill_facts_failed: %s", exc)
            return empty
        args = read_tool_args(resp)
        if args is not None:  # the model made the tool call (facts may be [])
            break
        logger.info("assist_distill_facts_empty_redraw draw=%d/3", _draw + 1)
    raw = (args or {}).get("facts") or []
    if not isinstance(raw, list):
        return empty
    out: list[str] = []
    for f in raw:
        t = str(f).strip()
        if t:
            out.append(t[:300])
    return {
        "facts": out,
        "superseded": _match_superseded((args or {}).get("superseded_facts"), known_facts),
    }


# ── Per-step progress recap (§17.738 — stay coherent over a long step) ──────

_STEP_RECAP_SYSTEM = (
    "You keep a running recap of ONE step of a hands-on build, so the assistant "
    "and the operator don't lose the thread over a long back-and-forth. You are "
    "given the step's goal and the full transcript of work on it (operator "
    "messages + the assistant's replies). Write a SHORT recap with these labels, "
    "omitting any that are empty:\n"
    "GOAL: one line — what this step must achieve (its done-condition).\n"
    "DONE: sub-tasks already resolved in this step (bullet fragments).\n"
    "OPEN: what's still blocking / not yet working (bullet fragments) — be "
    "specific about the CURRENT blocker.\n"
    "CONSTRAINTS: hard limits on HOW we can proceed that later steps MUST respect "
    "— e.g. no copy-paste in this console, the guest agent is unavailable, "
    "GUI-only, offline-only, a login the operator doesn't have — PLUS approaches "
    "already tried and RULED OUT (don't retry them). One short fragment each; "
    "omit if there are none.\n"
    "NEXT: one line — the single most immediate action to take now, stated as an "
    "OBJECTIVE in plain words: WHAT to achieve and WHY, NOT the specific command "
    "to run. Describe the goal of the action (e.g. 'change VM 100's network card "
    "to the Intel E1000 model so the installer detects it, then restart the "
    "installer'), never a literal shell command line (do NOT write 'run "
    "`qm set …`'). A later step chooses the easiest TOOL for this objective — "
    "often a GUI — so the recap must stay tool-neutral and not bias it toward the "
    "CLI. Omit only if the step is fully done.\n"
    "CONTEXT: key state that's easy to lose — especially WHICH machine the next "
    "commands run on (host vs the VM/guest), IPs, filenames, and values already "
    "chosen.\n"
    "You may also be given the operator's recorded CONSTRAINTS/DECISIONS and "
    "observed system FACTS (from earlier steps / the durable ledger). Fold a hard "
    "limit or ruled-out approach into CONSTRAINTS, and durable system state (build "
    "spec, versions, IPs, what already exists) into CONTEXT — even if this step's "
    "transcript did not repeat it. But DONE/OPEN/NEXT still come ONLY from the "
    "transcript: a stated constraint or a system fact is NOT completed work, so "
    "never turn one into a DONE item or invent progress from it.\n"
    "Ground in the transcript (plus those recorded constraints/facts); never invent "
    "progress. Be terse (a compact status board, not prose). For OPEN and NEXT, "
    "describe blockers and "
    "objectives in tool-neutral plain words — the exact shell commands live in "
    "the transcript, not here; do NOT copy command lines into the recap (that "
    "would wrongly anchor the next answer to the CLI when a GUI is easier). If "
    "almost nothing has happened yet, a one-line GOAL is enough."
)


async def summarize_step_progress(
    *, title: str, transcript: str, role: str = "model_general",
    facts_block: str = "", notes_block: str = "",
) -> str:
    """§17.738 — a compact running recap of one step from its full transcript,
    so fix/guide/research stay on-thread over a long troubleshooting marathon
    (the 6-turn window loses it). Reasoning task → ``model_general``. Fail-soft
    → "" so callers thread it unconditionally.

    §17.752 — ``facts_block``/``notes_block`` (the durable ledgers) let the recap
    ground its CONSTRAINTS/CONTEXT in what the operator stated on EARLIER steps and
    in observed system facts, not just this node's transcript; DONE/OPEN/NEXT stay
    transcript-derived (see ``_STEP_RECAP_SYSTEM``)."""
    if not (transcript or "").strip():
        return ""
    ledger = ""
    if (notes_block or "").strip():
        ledger += f"\nOperator's recorded constraints/decisions:\n{notes_block.strip()}\n"
    if (facts_block or "").strip():
        ledger += f"\n{facts_block.strip()}\n"
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _STEP_RECAP_SYSTEM},
                {"role": "user", "content": (
                    f"Step goal: {title}\n"
                    f"{ledger}\n"
                    f"Transcript of work on this step (oldest first):\n{transcript[:12000]}\n\n"
                    "Write the recap."
                )},
            ],
            {"role": role},
            temperature=0.1,
            max_tokens=2048,   # thinking model clears reasoning before the recap
            draws=2,
            label="assist_step_recap",
        )
    except Exception as exc:  # noqa: BLE001 — a recap must never break the turn
        logger.warning("assist_summarize_step_progress_failed: %s", exc)
        return ""
    if resp and resp.success:
        return (resp.text or "").strip()[:2000]
    return ""




# ── §17.753 — the cross-step "living project recap" (§17.679) ───────────────
# The per-step recap above keeps ONE step coherent; the job digest (§17.650) dumps
# raw done-node outputs. This distills a compact, EVOLVING whole-project state board
# so guidance/pivot on step N reason with the arc — what earlier steps decided, what
# remains, cross-step constraints — instead of a single-node view or a raw dump.

_PROJECT_RECAP_SYSTEM = (
    "You keep a running recap of a WHOLE multi-step build, so the assistant and the "
    "operator never lose the arc across steps. You are given the project goal, the "
    "list of plan steps with their status (done / in-progress / pending / skipped) "
    "and a short summary of what each DONE step produced, plus the operator's "
    "recorded decisions/constraints and observed system facts. Write a SHORT "
    "whole-project state board with these labels, omitting any that are empty:\n"
    "GOAL: one line — the overall deliverable.\n"
    "DONE: what has been ACCOMPLISHED across steps, phase-level (not raw output) — "
    "bullet fragments.\n"
    "IN PROGRESS: the step(s) currently being worked, one fragment each.\n"
    "REMAINING: what still lies ahead, terse — the shape of the rest, not every "
    "step verbatim.\n"
    "DECISIONS: choices already locked in that later steps must stay consistent "
    "with (from the operator's decisions + what done steps established) — bullet "
    "fragments.\n"
    "CONSTRAINTS: hard limits that span the project (from the operator's stated "
    "constraints / preferences / ruled-out approaches). One fragment each.\n"
    "SYSTEM: durable facts about the operator's ACTUAL system (build spec, versions, "
    "hosts/IPs, what already exists) — ground on these; never assume a fresh/empty "
    "system.\n"
    "Ground ONLY in the given step statuses, done-step summaries, and the operator's "
    "decisions/constraints/facts; NEVER invent completion — a step is DONE only if "
    "its status says so. Be terse (a status board, not prose). Stay tool-neutral: "
    "describe WHAT each step achieved, not the specific commands. If almost nothing "
    "has happened yet, a one-line GOAL is enough."
)


async def summarize_project_progress(
    *, goal: str, nodes_block: str, facts_block: str = "", notes_block: str = "",
    role: str = "model_general",
) -> str:
    """§17.753 — a compact running recap of the WHOLE project from its step
    statuses + done-step summaries + the durable ledgers. Reasoning task →
    ``model_general``. Fail-soft → "" so callers thread it unconditionally."""
    if not (nodes_block or "").strip():
        return ""
    ledger = ""
    if (notes_block or "").strip():
        ledger += f"\nOperator's recorded decisions/constraints:\n{notes_block.strip()}\n"
    if (facts_block or "").strip():
        ledger += f"\n{facts_block.strip()}\n"
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _PROJECT_RECAP_SYSTEM},
                {"role": "user", "content": (
                    f"Project goal: {goal or '(untitled)'}\n"
                    f"{ledger}\n"
                    f"Plan steps (status + what each DONE step produced):\n{nodes_block[:12000]}\n\n"
                    "Write the whole-project state board."
                )},
            ],
            {"role": role},
            temperature=0.1,
            max_tokens=2048,   # thinking model clears reasoning before the board
            draws=2,
            label="assist_project_recap",
        )
    except Exception as exc:  # noqa: BLE001 — a recap must never break the turn
        logger.warning("assist_summarize_project_progress_failed: %s", exc)
        return ""
    if resp and resp.success:
        return (resp.text or "").strip()[:2500]
    return ""




# ── Operator-facing status panel + "do this next" callout (§17.741) ─────────
# The §17.738 recap above already distills a compact GOAL/DONE/OPEN/NEXT/CONTEXT
# status board — but only into the MODEL's prompt. These helpers turn it into a
# visible "📍 Where we are" panel the operator sees above each walkthrough, so a
# long problem-solving step reads as tracked rather than lost, and mandate a
# leading "👉 Do this next" section so the single immediate action draws the eye.









# ── Draft an inserted step (§17.736 — turn a foundational gap into a step) ──

_DRAFT_STEP_TOOL = model_router.Tool(
    name="draft_step",
    description=(
        "Draft ONE new plan step from the operator's request to add work the "
        "plan doesn't cover (e.g. a setup task a later step depends on)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "A short imperative step title, e.g. 'Configure the VM's "
                    "network for internet access'. No numbering."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "1-3 sentences stating the step's concrete GOAL and how it's "
                    "confirmed done (the deliverable), grounded in the project "
                    "context. No command list — the walkthrough is generated "
                    "separately."
                ),
            },
        },
        "required": ["title", "description"],
    },
)

_DRAFT_STEP_SYSTEM = (
    "You turn an operator's request into ONE concrete plan step for a "
    "human-in-the-loop build. Use the project context (its hosts, addresses, "
    "decisions) so the step is specific to THIS system, not generic. The step "
    "should be the smallest coherent unit that resolves the operator's need "
    "(e.g. 'Configure the VM's network for internet access', with a clear "
    "done-condition like 'the VM can ping an external host'). Call draft_step "
    "exactly once."
)


async def draft_step(
    *, request: str, job_context: str | None = None, role: str = "model_general",
) -> dict:
    """§17.736 — draft ``{title, description}`` for a step the operator asked to
    add (a foundational task the plan doesn't cover). Fail-soft: on any error
    returns a title/description derived from the raw request so the insert still
    succeeds."""
    req = (request or "").strip()
    fallback = {
        "title": (req[:80] or "Additional setup step"),
        "description": req[:400] or "Operator-requested step added mid-assist.",
    }
    if not req:
        return fallback
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _DRAFT_STEP_SYSTEM},
                {"role": "user", "content": (
                    (f"{job_context.strip()}\n\n" if (job_context or "").strip() else "")
                    + f"Operator's request for a new step:\n{req[:1000]}\n\n"
                    "Call draft_step."
                )},
            ],
            tools=[_DRAFT_STEP_TOOL],
            role=role,
            temperature=0.1,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — never block the insert on the draft
        logger.warning("assist_draft_step_failed: %s", exc)
        return fallback
    args = read_tool_args(resp) or {}
    title = str(args.get("title") or "").strip()[:120]
    desc = str(args.get("description") or "").strip()[:600]
    if not title:
        return fallback
    return {"title": title, "description": desc or fallback["description"]}


# ── Ledger consolidation (§17.727 — merge redundant same-truth facts) ───────

_CONSOLIDATE_TOOL = model_router.Tool(
    name="propose_fact_merges",
    description=(
        "Propose merges of REDUNDANT facts in a system-state ledger: groups of "
        "entries that state the same truth about the same subject, each with "
        "one replacement sentence carrying ALL their distinct details."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "merges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "replaces": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Indices (from the numbered list) of 2 or more "
                                "facts that state the SAME truth about the SAME "
                                "subject. Never group facts about different "
                                "subjects, and never group facts that add "
                                "distinct information unless the replacement "
                                "sentence carries every detail."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "ONE replacement sentence containing ALL the "
                                "distinct details from the group — nothing "
                                "dropped, nothing invented."
                            ),
                        },
                    },
                    "required": ["replaces", "text"],
                },
                "description": (
                    "Only clearly-redundant groups. A fact that is unsure, "
                    "unique, or only loosely related stays UNGROUPED (omit it "
                    "entirely — ungrouped facts are kept as-is). Return [] when "
                    "nothing is clearly redundant."
                ),
            }
        },
        "required": ["merges"],
    },
)

_CONSOLIDATE_SYSTEM = (
    "You tidy a ledger of observed facts about a human operator's real system. "
    "Multiple entries often state the SAME truth with different wording or "
    "detail level (e.g. the same storage pool described three ways). Propose "
    "merge groups: each group lists the indices of clearly-redundant entries "
    "plus ONE replacement sentence that preserves EVERY distinct detail from "
    "the group (sizes, versions, addresses, names, states). Rules: never merge "
    "entries about different subjects; never drop a detail; never invent one; "
    "an entry you are not sure about stays ungrouped (it is kept as-is). "
    "Conflicting entries are NOT redundant — leave conflicts ungrouped. Call "
    "propose_fact_merges exactly once."
)


async def consolidate_facts(
    facts: list[str], *, role: str = "model_general",
) -> list[dict]:
    """§17.727 — propose merges of redundant ledger facts. Returns validated
    groups ``[{"replaces": [fact-text, …], "text": merged}]`` (indices resolved
    to the ledger's texts so the caller can apply by VALUE, robust to a ledger
    that changed while the model was thinking). Validation is strict — out-of-
    range/duplicate indices dropped, groups need ≥2 distinct members, no fact
    in two groups, replacement text bounded — and fail-soft → []."""
    if not facts or len(facts) < 2:
        return []
    numbered = "\n".join(f"[{i}] {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _CONSOLIDATE_SYSTEM},
                {"role": "user", "content": (
                    f"Ledger:\n{numbered}\n\nCall propose_fact_merges."
                )},
            ],
            tools=[_CONSOLIDATE_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # A thinking model_general spends output tokens on reasoning BEFORE
            # the tool call, and a 37-fact ledger is a big prompt — 2048 starved
            # every draw into tool_call_empty_redraw (same pitfall as §17.583 /
            # the qwen3.5 empty-content lesson). Be generous here; this call is
            # rare (debounced, threshold-gated).
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — tidying must never break anything
        logger.warning("assist_consolidate_facts_failed: %s", exc)
        return []
    args = read_tool_args(resp)
    raw = (args or {}).get("merges") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    used: set[int] = set()
    for m in raw:
        if not isinstance(m, dict):
            continue
        text_ = str(m.get("text") or "").strip()
        if not text_:
            continue
        idxs: list[int] = []
        for i in (m.get("replaces") or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(facts) and i not in used and i not in idxs:
                idxs.append(i)
        if len(idxs) < 2:
            continue  # a "merge" of one is a rewrite — not allowed
        if len(text_) > 600:
            # A replacement that long can't be truncated without losing the
            # details it exists to preserve — skip the group (originals kept).
            continue
        used.update(idxs)
        out.append({"replaces": [facts[i] for i in idxs], "text": text_})
    return out


# ── Unconditional per-turn derive (§17.715 — review + log EVERY message) ────

_RECORD_TURN_MEMORY_TOOL = model_router.Tool(
    name="record_turn_memory",
    description=(
        "Log any DURABLE, plan-relevant information the operator stated in this "
        "one message, so later steps retain it. Two buckets: plan notes and "
        "system facts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["decision", "constraint", "addition", "preference"],
                            "description": (
                                "decision = a choice/change of direction that "
                                "reshapes the plan ('let's do a fresh install "
                                "instead', 'drop the VPN'); constraint = a hard "
                                "limit ('only 2 NICs', 'must stay under $50'); "
                                "addition = a NEW requirement to fold in ('also add "
                                "a Palworld server'); preference = a soft "
                                "leaning ('I'd rather use WireGuard')."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "One self-contained sentence stating the note in "
                                "the third person ('Operator has decided to …'). "
                                "Standalone — do not reference 'this' or 'that'."
                            ),
                        },
                    },
                    "required": ["kind", "text"],
                },
                "description": (
                    "Plan-affecting statements the operator made THIS message. "
                    "Empty unless they actually decided/constrained/added/preferred "
                    "something new."
                ),
            },
            "facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Durable facts about the operator's REAL system stated in this "
                    "message (e.g. 'Router is a UniFi UDM at 192.168.1.1'). Only "
                    "what they actually said; never guess."
                ),
            },
            "superseded_facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "§17.725 — entries from ALREADY KNOWN FACTS that this message "
                    "DIRECTLY CONTRADICTS, echoed VERBATIM, character for "
                    "character. Only a real conflict counts (the operator states "
                    "the opposite / corrects the value). A refinement, an "
                    "addition, or a fact the message is silent on is NOT "
                    "superseded. Return [] when unsure."
                ),
            },
            "execution_context": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "Shell user, e.g. 'root'."},
                    "host": {"type": "string", "description": "Hostname, e.g. 'DeFruscio-HomeLab'."},
                },
                "description": (
                    "Set ONLY when the operator states, in prose, the shell "
                    "user@host they are NOW running commands on / logged into — "
                    "e.g. 'I'm on root@DeFruscio-HomeLab now', 'switched to "
                    "root@pve2'. This changes where later steps run. OMIT for a "
                    "passing mention, an email address, an example, or the host of "
                    "some OTHER machine they are not operating from. Requires BOTH "
                    "user and host; omit the whole object if either is unclear."
                ),
            },
        },
        "required": ["notes", "facts"],
    },
)

_TURN_MEMORY_SYSTEM = (
    "You are a memory scribe for a human-in-the-loop build session. Read ONE "
    "operator message and extract only DURABLE, plan-relevant information worth "
    "carrying into later steps: decisions / changes of direction, hard "
    "constraints, new requirements, stated preferences (as notes), and concrete "
    "facts about their real system (as facts). Be conservative and precise:\n"
    "- Return EMPTY notes and facts for a PURE question, request for help, "
    "acknowledgement ('ok', 'thanks'), small talk, or a refinement that does not "
    "change the plan.\n"
    "- A question can still CARRY durable information — when the operator states "
    "or corrects the state of their system inside a question (\"no, i am "
    "currently installing it, and have three options — which do I choose?\"), "
    "record that stated/corrected state as a fact even though the message asks "
    "something. Only the pure ask itself is not memory.\n"
    "- Do NOT restate anything already listed under ALREADY KNOWN — only NEW "
    "information the operator added in this message.\n"
    "- If the message DIRECTLY CONTRADICTS one of the ALREADY KNOWN FACTS (the "
    "operator states the opposite or corrects the value), echo that known fact "
    "verbatim in superseded_facts so the ledger can retract it. Only a real "
    "conflict — never an addition or refinement.\n"
    "- Never guess or infer beyond what the message says.\n"
    "Call record_turn_memory exactly once."
)


async def distill_turn_memory(
    *, message: str, known_notes: list[str] | None = None,
    known_facts: list[str] | None = None, role: str = "model_general",
) -> dict:
    """§17.715 — extract durable, plan-relevant memory from ONE operator message:
    ``{"notes": [{"kind","text"}], "facts": [str]}``. This is the unconditional
    review the trigger-gated pivot/note/facts paths miss. Reasoning/extraction →
    ``model_general`` (cf. §17.677). Conservative (empty for questions / chit-
    chat / step-refinements). ``known_*`` are folded into the prompt so the model
    does not restate standing memory. Fail-soft → ``{"notes": [], "facts": []}``."""
    empty = {"notes": [], "facts": [], "superseded": []}
    if not (message or "").strip():
        return empty
    known_lines = ""
    if known_notes:
        known_lines += "ALREADY KNOWN NOTES (do not repeat):\n" + "\n".join(
            f"- {k}" for k in known_notes
        ) + "\n\n"
    if known_facts:
        # §17.725 — facts listed separately so a contradiction can be echoed
        # verbatim into superseded_facts for retraction.
        known_lines += (
            "ALREADY KNOWN FACTS (do not repeat; if this message DIRECTLY "
            "CONTRADICTS one, echo it verbatim in superseded_facts):\n"
            + "\n".join(f"- {k}" for k in known_facts) + "\n\n"
        )
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _TURN_MEMORY_SYSTEM},
                {"role": "user", "content": (
                    known_lines
                    + f"Operator message:\n{message[:4000]}\n\nCall record_turn_memory."
                )},
            ],
            tools=[_RECORD_TURN_MEMORY_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — a scribe must never break the turn
        logger.warning("assist_distill_turn_memory_failed: %s", exc)
        return empty
    args = read_tool_args(resp)
    if not args:
        return empty
    notes_out: list[dict] = []
    for n in (args.get("notes") or []):
        if not isinstance(n, dict):
            continue
        t = str(n.get("text") or "").strip()
        k = str(n.get("kind") or "").strip().lower()
        if t and k in ("decision", "constraint", "addition", "preference"):
            notes_out.append({"kind": k, "text": t[:400]})
    facts_out: list[str] = []
    for f in (args.get("facts") or []):
        t = str(f).strip()
        if t:
            facts_out.append(t[:300])
    out = {
        "notes": notes_out,
        "facts": facts_out,
        # §17.725 — known facts this message directly contradicts (verbatim
        # ledger spellings only), for retraction at fold time.
        "superseded": _match_superseded(args.get("superseded_facts"), known_facts),
    }
    # §17.716 — an explicit prose statement of the operator's current shell host
    # (what the anchored prompt-line sensor can't see). Only when BOTH parts are
    # present; the caller re-validates + applies under the §17.703 retention rules.
    ec = args.get("execution_context")
    if isinstance(ec, dict):
        u, h = str(ec.get("user") or "").strip(), str(ec.get("host") or "").strip()
        if u and h:
            out["execution_context"] = {"user": u, "host": h}
    return out


# ── Grounding gate (§17.710c — warn when a result contradicts memory) ───────

_RECORD_GROUNDING_TOOL = model_router.Tool(
    name="record_grounding",
    description=(
        "Report whether the operator's step result CONTRADICTS what's already "
        "known about their system (the session memory)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "contradicts": {
                "type": "boolean",
                "description": (
                    "True ONLY if the result states or assumes something that "
                    "conflicts with a KNOWN fact — e.g. it assumes a fresh/empty "
                    "system when memory says an existing one, or names a value "
                    "that conflicts with a provided value. Adding NEW information, "
                    "normal progress, or anything memory doesn't speak to is NOT "
                    "a contradiction."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One short sentence naming the conflict. Omit if not contradicts.",
            },
        },
        "required": ["contradicts"],
    },
)

_GROUNDING_SYSTEM = (
    "You check whether a human operator's step result is CONSISTENT with what is "
    "already known about their system. You are given the session memory (observed "
    "facts, provided values, notes) and the operator's result for this step. Flag "
    "a contradiction ONLY when the result conflicts with a known fact — most "
    "often assuming a fresh/empty system when memory shows an existing one. Be "
    "conservative: adding new info or anything memory is silent on is NOT a "
    "contradiction; default to contradicts=false when unsure. §17.714: if the "
    "memory says the operator has CHANGED DIRECTION / chosen to reinstall or "
    "rebuild, a result that assumes a fresh system is CONSISTENT, not a "
    "contradiction. Call record_grounding once."
)


async def check_grounding(
    *, evidence: str, environment: dict | None,
    operator_notes: list[dict] | None = None, role: str = "model_general",
) -> dict:
    """§17.710c — warn-only grounding check. Does ``evidence`` contradict the
    session memory? Returns ``{contradicts: bool, reason: str}``. A reasoning
    task → ``model_general`` (not the verifier; cf. §17.677). Fail-soft →
    ``{contradicts: False}``; no-op (same) when there's no memory to check
    against."""
    memory = render_session_memory(environment, operator_notes)
    if not memory or not (evidence or "").strip():
        return {"contradicts": False}
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _GROUNDING_SYSTEM},
                {"role": "user", "content": (
                    f"{memory}\n\n## Operator's result for this step\n"
                    f"{evidence[:6000]}\n\nCall record_grounding."
                )},
            ],
            tools=[_RECORD_GROUNDING_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — a flaky gate must never block a submit
        logger.warning("assist_grounding_check_failed: %s", exc)
        return {"contradicts": False}
    args = read_tool_args(resp)
    if not args or "contradicts" not in args:
        return {"contradicts": False}
    return {
        "contradicts": bool(args.get("contradicts")),
        "reason": (args.get("reason") or "").strip(),
    }


# ── Destructive-command safety gate (§17.492) ──────────────────────────────

# High-confidence, command-context-anchored patterns only — a destructive
# verb in prose ("this removes the file") must NOT trip the gate; only an
# actual command form does. (compiled regex, human-readable why).
#
# §17.644 — two groups. COMMAND patterns anchor to the START of the command
# (via re.match after the prompt/sudo/env prefix is stripped): a real command
# leads with its tool name, so `parted /dev/sdb` fires but the prose/heading
# lines "Create a single partition with parted", "1. Open parted…", "4. Exit
# parted:" do NOT. The pre-§17.644 patterns used `\b<tool>\b` (word anywhere),
# so a bare mention of parted/fdisk/mkfs/dd/etc. in a sentence tripped the
# banner — crying wolf on nearly every shell step, which blunts the real
# warning (the §17.613 lesson, generalized from `rm` to every command verb).
# CONTENT patterns are not command-led (redirects, SQL, fork bomb), so they
# stay search-anywhere.
_DESTRUCTIVE_CMD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # §17.613 (audit #7) — require an actual dash-flag bearing r/f/R. The old
    # pattern needed no leading dash, so `rm file.conf` and even the safe
    # `rm -i file` tripped the gate — crying wolf trains operators to ignore it.
    (re.compile(r"rm\s+(-\S*\s+)*-\S*[rfR]"), "recursive/forced file deletion (rm -rf)"),
    (re.compile(r"dd\s+(if|of)="), "raw disk write (dd)"),
    (re.compile(r"mkfs(\.\w+)?\b"), "format filesystem (mkfs)"),
    (re.compile(r"wipefs\b"), "wipe filesystem signatures (wipefs)"),
    (re.compile(r"shred\b"), "secure file wipe (shred)"),
    (re.compile(r"(fdisk|parted|sgdisk)\b"), "partition-table edit"),
    (re.compile(r"chmod\s+-R\s+0?777\b"), "world-writable recursive chmod"),
    (re.compile(r"git\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+(-f|--force))"),
     "destructive git (hard reset / force push / clean -f)"),
    (re.compile(r"docker\s+(system\s+prune|volume\s+(rm|prune)|rm\s+-f)"), "docker resource removal"),
    (re.compile(r"kubectl\s+delete\b"), "kubectl delete"),
]
_DESTRUCTIVE_CONTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"--no-preserve-root"), "rm targeting / (--no-preserve-root)"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)"), "overwrite a block device"),
    (re.compile(r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE(\s+TABLE)?)\b", re.IGNORECASE),
     "destructive SQL (DROP/TRUNCATE)"),
    (re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE), "unfiltered SQL DELETE (no WHERE)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
]

# Leading prefixes that precede the actual command verb (so `sudo parted …` and
# `FOO=bar dd …` still anchor on parted/dd). Stripped before the CMD match.
_CMD_PREFIX_RE = re.compile(r"^(sudo\s+|doas\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def scan_destructive(text: str) -> list[dict]:
    """Deterministic scan for high-confidence destructive commands.

    Returns ``[{line, why}]`` (deduped by line; line truncated). Strips leading
    prompt/fence chars so ``$ rm -rf x`` matches. No LLM. Best-effort — this
    informs the operator, it does not block.

    §17.644 — command patterns anchor to the start of the command (after any
    prompt/`sudo`/env-var prefix) so a destructive verb appearing mid-sentence
    in prose or a numbered heading does not trip the banner.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("$#>` ").strip()
        if not line or line in seen:
            continue
        matched: Optional[str] = None
        # Command-led: strip sudo/doas/env prefixes, then require the tool at
        # the start (re.match anchors at position 0).
        cmd_line = _CMD_PREFIX_RE.sub("", line)
        for rx, why in _DESTRUCTIVE_CMD_PATTERNS:
            if rx.match(cmd_line):
                matched = why
                break
        # Content-led: high-confidence tokens that aren't command-first.
        if matched is None:
            for rx, why in _DESTRUCTIVE_CONTENT_PATTERNS:
                if rx.search(line):
                    matched = why
                    break
        if matched is not None:
            out.append({"line": line[:200], "why": matched})
            seen.add(line)
    return out


# ── Generation ───────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_guide_user_prompt(
    ctx: StepContext, node_description: Optional[str],
    sources: list[dict], refine_hint: Optional[str],
    environment: Optional[dict] = None,
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
) -> str:
    """Compose the user message: the same upstream-last task the executor
    would see, plus the operator environment, a project-wide digest of work
    already completed on the job (§17.650), the operator's captured notes &
    additions (§17.654), the recent conversation (§17.687), a confirmed-research
    block, and a human-walkthrough trailer (a decision-framing trailer when the
    node is a decision).
    """
    parts: list[str] = [ctx.assembled_prompt]
    if node_description and node_description.strip() and node_description.strip() not in ctx.assembled_prompt:
        parts.append(f"Task description: {node_description.strip()}")
    if job_digest and job_digest.strip():
        parts.append(job_digest.strip())
    # §17.710b — one injection path (unified session memory) or the legacy
    # env + notes blocks, per the assist_umem_inject valve.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    if conversation and conversation.strip():  # §17.687 — recent back-and-forth
        parts.append(conversation.strip())
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    parts.append(_GUIDE_DECISION_TRAILER if is_decision else _GUIDE_USER_TRAILER)
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
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
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
            # §17.771 (Phase 3) — ground the option research in the operator's
            # actual system so a DECISION step's options are system-specific, not
            # a generic textbook list. render_environment_block folds in the
            # §17.709 facts ledger; "" when unknown (fail-soft, generic queries).
            environment_block=render_environment_block(environment),
        )

    system = apply_verbosity(
        guide_system_for_tool(ctx.tool, is_decision=is_decision), verbosity
    )
    system = apply_next_callout(  # §17.741 — lead with the immediate action
        system, is_decision=is_decision,
        enabled=settings.assist_next_callout_enabled,
    )
    system = apply_problem_solving(  # §17.742 — don't thrash on tangled steps
        system, enabled=settings.assist_problem_solving_enabled,
    )
    system = apply_ground_or_ask(  # §17.756 — placeholder + ask, never guess a value
        system, is_decision=is_decision,
        enabled=settings.assist_ground_or_ask_enabled,
    )
    system = apply_screen_grounding(  # §17.758 — confirm the on-screen state first
        system, is_decision=is_decision,
        enabled=settings.assist_screen_grounding_enabled,
    )
    system = apply_location_callout(  # §17.852 — say WHERE, announce switches
        system, is_decision=is_decision,
        enabled=settings.assist_location_callout_enabled,
    )
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
        job_digest=job_digest, operator_notes=operator_notes, is_decision=is_decision,
        conversation=conversation,
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
    # §17.771 (deferred, now done) — render-path suggestion validation: on a
    # DECISION step, guarantee a recommendation. If the model laid out options but
    # dropped the "## My suggestion" lean, generate just that block from the
    # options it produced and append it. Only on a miss; fail-soft (append is ""
    # → ship the un-enforced walkthrough). Off by default (tests/fresh installs).
    suggestion_enforced = False
    if (is_decision and text_out
            and settings.assist_decision_suggestion_enforce
            and not _has_decision_suggestion(text_out)):
        block = await _generate_decision_suggestion(
            title=ctx.title, task_prompt=ctx.base_prompt, options_text=text_out,
            environment=environment, role=role,
        )
        if block:
            text_out = f"{text_out}\n\n{block}"
            suggestion_enforced = True
            logger.info("assist_decision_suggestion_enforced node_key=%s", node_key)
    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": getattr(resp, "model", "") if resp else "",
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "suggestion_enforced": suggestion_enforced,
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
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    conversation: Optional[str] = None,
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
    if job_digest and job_digest.strip():   # §17.653 — project-wide context
        parts.append(job_digest.strip())
    # §17.745 — same unified session memory (facts + provided values + operator
    # notes, with §17.714 reset supersession) that guide/ask/decision inject.
    # Previously /fix rendered only the legacy env block (facts, no notes), so a
    # captured pivot or tool preference never reached the fix walkthrough.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    if conversation and conversation.strip():  # §17.687 — recent back-and-forth
        parts.append(conversation.strip())
    parts.append(f"## Error the operator hit\n{error_text.strip()}")
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    parts.append(_FIX_USER_TRAILER)
    user = "\n\n".join(parts)

    resp = await chat_until_nonempty(
        model_router.chat,
        [
            {"role": "system", "content": apply_location_callout(  # §17.852
                apply_screen_grounding(  # §17.758
                    apply_ground_or_ask(  # §17.756
                        apply_problem_solving(  # §17.742
                            apply_next_callout(  # §17.741
                                apply_verbosity(GUIDE_SYSTEM_FIX, verbosity),
                                is_decision=False, enabled=settings.assist_next_callout_enabled),
                            enabled=settings.assist_problem_solving_enabled),
                        is_decision=False, enabled=settings.assist_ground_or_ask_enabled),
                    is_decision=False, enabled=settings.assist_screen_grounding_enabled),
                is_decision=False, enabled=settings.assist_location_callout_enabled)},
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
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
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
        job_digest=job_digest,
        operator_notes=operator_notes,
        is_decision=is_decision,
        conversation=conversation,
    )
    # §17.851 — code-enforced placeholder resolution (see resolve_placeholders).
    from app.config import settings as _settings
    if res.get("guidance") and _settings.assist_placeholder_resolver_enabled:
        resolved, resolved_map = await resolve_placeholders(
            text=res["guidance"], session_id=session_id, environment=environment,
            step_title=ctx.title, db=db,
        )
        res["guidance"] = resolved
        meta = res.setdefault("guidance_meta", {})
        meta["placeholders_resolved"] = resolved_map
        # §17.854 (audit C3) — generate_guidance scanned the PRE-resolution text,
        # so a value substituted in here (e.g. a pinned "local-lvm; wipefs …")
        # was never re-scanned. Re-run the destructive scan on the resolved text
        # so the meta the SPA reads reflects what will actually be shown/run.
        if _settings.assist_destructive_scan:
            meta["destructive"] = scan_destructive(resolved)
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
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
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
            # §17.854 (audit C1) — the STREAM path had dropped the §17.771
            # environment grounding the non-stream path passes, so a streamed
            # DECISION step (the SPA path) researched generic textbook options
            # instead of system-specific ones. Restored to parity.
            environment_block=render_environment_block(environment),
        )

    system = apply_verbosity(
        guide_system_for_tool(ctx.tool, is_decision=is_decision), verbosity
    )
    system = apply_next_callout(  # §17.741 — lead with the immediate action
        system, is_decision=is_decision,
        enabled=settings.assist_next_callout_enabled,
    )
    system = apply_problem_solving(  # §17.742 — don't thrash on tangled steps
        system, enabled=settings.assist_problem_solving_enabled,
    )
    system = apply_ground_or_ask(  # §17.756 — placeholder + ask, never guess a value
        system, is_decision=is_decision,
        enabled=settings.assist_ground_or_ask_enabled,
    )
    system = apply_screen_grounding(  # §17.758 — confirm the on-screen state first
        system, is_decision=is_decision,
        enabled=settings.assist_screen_grounding_enabled,
    )
    system = apply_location_callout(  # §17.852 — say WHERE, announce switches
        system, is_decision=is_decision,
        enabled=settings.assist_location_callout_enabled,
    )
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
        job_digest=job_digest, operator_notes=operator_notes, is_decision=is_decision,
        conversation=conversation,
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

    # §17.854 (audit C1) — decision-suggestion enforcement was ONLY on the
    # non-stream path, so a streamed DECISION walkthrough that dropped the
    # "## My suggestion" lean shipped un-enforced even with the valve on. Run the
    # same guard here; the appended block is yielded as a delta AND folded into
    # the durable copy the client reloads post-stream. Fail-soft.
    suggestion_enforced = False
    if (is_decision and text_out
            and settings.assist_decision_suggestion_enforce
            and not _has_decision_suggestion(text_out)):
        block = await _generate_decision_suggestion(
            title=ctx.title, task_prompt=ctx.base_prompt, options_text=text_out,
            environment=environment, role=role,
        )
        if block:
            yield {"type": "delta", "text": f"\n\n{block}"}
            text_out = f"{text_out}\n\n{block}"
            suggestion_enforced = True
            logger.info("assist_decision_suggestion_enforced(stream) node_key=%s", node_key)

    # §17.851 — code-enforced placeholder resolution before persist: the
    # DURABLE copy (what load() re-renders and future reads serve) carries
    # concrete values; the live-streamed raw text is replaced on the client's
    # post-stream reload.
    resolved_map: dict = {}
    if text_out and settings.assist_placeholder_resolver_enabled:
        text_out, resolved_map = await resolve_placeholders(
            text=text_out, session_id=session_id, environment=environment,
            step_title=ctx.title, role=role, db=db,
        )

    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": model_used,
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "suggestion_enforced": suggestion_enforced,
        "status": status,
        "generated_at": _utcnow_iso(),
        "placeholders_resolved": resolved_map,
        # §17.854 (audit C3) — scan runs on the POST-resolution text (matches the
        # ensure_guidance rescan on the non-stream path), so substituted values
        # that introduce a destructive command are caught.
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


_FOCUS_QUERY_SYSTEM = (
    "You turn an operator's conversational question into ONE concise web-search "
    "query — the keywords a person would actually type. Drop filler ('can you "
    "walk me through', 'step by step', 'how do I'), keep the concrete nouns: "
    "product/tool names, the specific action, error text, versions. If PROJECT "
    "keywords are given, fold in the ones that pin down the RIGHT tech (e.g. the "
    "platform 'Proxmox') so the search doesn't drift to a different tool — but "
    "don't pad with every keyword. If the question asks for the CURRENT/LATEST/"
    "newest of something, keep that word — it matters. Reply with ONLY the "
    "query, no quotes, no preamble."
)


async def _focus_web_query(question: str, *, role: str, hint: str = "") -> str:
    """§17.729 — compress a conversational question into a keyword search query.

    The ask/`/assist research` path used the operator's RAW message as the
    SearXNG query ("can you walk me through fixing the VM 100 step by step"),
    which keyword engines can't match — so research returned nothing and the
    answer fell back to the model's stale memory (the reported Ubuntu 22.04.3).
    ``hint`` (the project goal/entities) anchors the query on the RIGHT stack —
    without it "fix the VM" drifted to VirtualBox/VMware instead of Proxmox.
    Fail-soft: any hiccup (or a question already short AND with no project hint
    to fold in) returns the original text, so this only ever helps.
    """
    q = (question or "").strip()
    if len(q.split()) <= 6 and not (hint or "").strip():
        return q  # already terse and no project context to fold — skip the call
    try:
        user = q[:1000]
        if (hint or "").strip():
            user = f"PROJECT: {hint.strip()[:300]}\n\nQuestion: {q[:1000]}"
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _FOCUS_QUERY_SYSTEM},
                {"role": "user", "content": user},
            ],
            {"role": role},
            temperature=0.0,
            # §17.465 — model_general is a thinking model; a tight cap gets spent
            # on reasoning and returns empty. 2048 clears the reasoning for what
            # is a one-line answer.
            max_tokens=2048,
            draws=3,
            label="assist_focus_query",
        )
    except Exception as exc:  # noqa: BLE001 — never block research on this
        logger.debug("assist_focus_web_query_failed: %s", exc)
        return q
    if resp and resp.success:
        lines = [ln.strip().strip('"') for ln in (resp.text or "").splitlines()]
        focused = next((ln for ln in lines if ln), "")
        if focused:
            return focused[:200]
    return q


async def research_one(
    *, question: str, node_key: str = "?", domain: Optional[str] = None,
    synthesize: bool = True, job_context: Optional[str] = None,
    context_hint: Optional[str] = None,
) -> dict:
    """Confirm a single operator-supplied question and optionally synthesize
    a short cited answer. Does not persist — this is a side query.

    §17.650 — ``job_context`` (the project's brief + environment + a digest of
    completed DAG-node work) is folded into the synthesis prompt so the answer
    relays what THIS project already established instead of a project-blind web
    lookup. ``context_hint`` biases only the local-KB retrieval (see
    ``_confirm_query``).
    """
    role = settings.assist_guide_model_role
    # §17.729 — search on a keyword-focused query, not the raw conversational
    # question (which returns nothing → stale-memory fallback). The original
    # `question` still drives synthesis below; only the retrieval query changes.
    web_q = await _focus_web_query(question, role=role, hint=context_hint or "")
    sources = await _confirm_query(
        question, node_key=node_key, domain=domain, deep=True,
        kb_query_extra=context_hint, web_query=web_q,
    )
    answer: Optional[str] = None
    # Synthesize when we have web/KB sources OR project context to relay — a
    # question answerable purely from the project's own prior work must not be
    # dropped just because the open web returned nothing.
    if synthesize and (sources or (job_context or "").strip()):
        ctx_block = (
            f"{job_context.strip()}\n\n" if (job_context or "").strip() else ""
        )
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": apply_ground_or_ask(  # §17.760
                    apply_problem_solving(  # §17.742
                        _RESEARCH_SYNTH_SYSTEM,
                        enabled=settings.assist_problem_solving_enabled),
                    is_decision=False, enabled=settings.assist_ground_or_ask_enabled)},
                {"role": "user", "content": (
                    f"{ctx_block}"
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
