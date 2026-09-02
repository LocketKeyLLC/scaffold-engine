"""Research pre-pass + web-source gathering — extracted from assist_guide.py.

§17.856 (audit "assist decomposition") — the assist research subsystem: confirm
unknowns before a walkthrough (_research_prepass → _detect_unknowns / _confirm_query),
gather + rank web sources (_searxng_structured / _deep_web_sources), and answer a
job-scoped operator question (research_one, §17.650/674). Calls model_router and
the searxng client (function-local import) plus two directive appliers; every name
is re-exported from assist_guide so assist_guide.<NAME> and the external callers
(assist_agent.run_step_research) keep resolving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from app import model_router
from app.config import settings
from app.utils.llm_retry import chat_until_nonempty
from app.utils.tool_call_args import read_tool_args
from app.modules.assist_directives import (  # §17.897 — full output contract
    apply_ground_or_ask,
    apply_location_callout,
    apply_next_callout,
    apply_problem_solving,
    apply_recommendation,  # §17.903
    apply_screen_grounding,
    promote_inline_commands,
)

logger = logging.getLogger("scaffold.assist_guide")


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
                    "GPU/board/OS version) rather than generic. §17.876: when the "
                    "task (or its error) involves installing or configuring a "
                    "NAMED third-party program from a repo/URL/script, ALWAYS "
                    "include one query for that program's current officially "
                    "recommended install method (its official docs) — repos move "
                    "and methods get deprecated, and the fix must target the "
                    "current standard, not a remembered one."
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
            think_off_rescue=True,  # §17.876
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
                # §17.897 — the ask path now carries the SAME output contract as
                # guide/fix. It previously applied only two of the five
                # directives, and the missing one mattered most: the mandate
                # that every command sits in its OWN fenced block lives in
                # apply_next_callout. Without it the model answered with inline
                # `code spans`, and only fenced blocks get a ⧉ copy button
                # (util.js mdToHtml) — so a research answer's commands were
                # literally not copy-pasteable. Live proof: an answer that told
                # the operator to run `qm resize 106 scsi0 +60G` with no way to
                # copy it, in the same session where guide/fix output had
                # copy buttons on every command.
                # §17.903 — outermost: this is the ASK path, the one the
                # operator uses to ask a direct question, so the answer-and-lean
                # rule belongs here above all else.
                {"role": "system", "content": apply_recommendation(
                  apply_location_callout(  # §17.852
                    apply_screen_grounding(  # §17.758
                        apply_ground_or_ask(  # §17.760
                            apply_problem_solving(  # §17.742
                                apply_next_callout(  # §17.741/897
                                    _RESEARCH_SYNTH_SYSTEM,
                                    is_decision=False,
                                    enabled=settings.assist_next_callout_enabled),
                                enabled=settings.assist_problem_solving_enabled),
                            is_decision=False, enabled=settings.assist_ground_or_ask_enabled),
                        is_decision=False, enabled=settings.assist_screen_grounding_enabled),
                    is_decision=False, enabled=settings.assist_location_callout_enabled))},
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
            think_off_rescue=True,  # §17.876
        )
        if resp and resp.success:
            answer = (resp.text or "").strip() or None
            if answer:  # §17.897 — code-enforced copy-paste format
                answer = promote_inline_commands(answer)
    return {"question": question, "sources": sources, "answer": answer}
