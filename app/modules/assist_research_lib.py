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
    apply_interactive_prompt,  # §17.958
    apply_interface_fidelity,  # §17.959
    apply_truncated_paste,  # §17.962
    apply_location_callout,
    apply_next_callout,
    apply_problem_solving,
    apply_recommendation,  # §17.903
    apply_screen_grounding,
    promote_inline_commands,
    strip_operator_meta_preamble,
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
) -> Optional[list[str]]:
    """Ask the model which facts a human would need to confirm. Fail-soft.

    §17.912 — returns ``None`` when the machinery FAILED (provider error, no
    tool call) and ``[]`` when the model succeeded and declined to ask anything.
    Callers that treat both as falsy are unaffected; only the guide-path floor
    needs the distinction, because flooring after an infrastructure error would
    convert a deliberate fail-soft path into another live request.

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
        return None  # §17.912 — FAILED, not "nothing to ask"
    if not resp.success or not resp.tool_calls:
        return None  # §17.912 — FAILED, not "nothing to ask"
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
            {"title": r.get("title", ""), "content": r.get("content", ""),
             "url": r.get("url", ""),
             # §17.1027 — the engine's own publication date, when it has one.
             "date": str(r.get("publishedDate") or "")}
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
        from app.modules.research_extractors import relevant_search_results
        pages = await _fetch_and_extract(results[:top_n])
        # §17.1027 — the snippet passed §17.729's relevance filter; the PAGE
        # must too. A result whose title matched can still fetch a page about
        # something else (a forum index, a login wall), and feeding that to the
        # synthesis as source [n] is how a confident wrong answer gets a
        # citation.
        pages = relevant_search_results(query, pages, title_key="url", content_key="content")
    except Exception as exc:
        logger.warning("assist_deep_fetch_failed: %s", exc)
        return []
    by_url = {r.get("url"): r for r in results if r.get("url")}
    out = []
    for p in pages:
        if not (p.get("content") or "").strip():
            continue
        hit = by_url.get(p.get("url")) or {}
        out.append({
            "query": query, "kind": "web", "text": p["content"][:2000],
            "url": p.get("url", ""), "title": hit.get("title", ""),
            # Prefer the page's own declared date; fall back to the engine's.
            "date": (p.get("date") or hit.get("date") or "")[:10],
        })
    return out


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


# §17.912 — action-shaped steps whose guidance is real commands on a real box.
# A pure-LLM/document step ("draft the runbook") legitimately needs no lookup.
_ACTION_STEP_RE = re.compile(
    r"\b(?:install|configur|deploy|provision|set\s*up|setup|upgrade|migrat|"
    r"enable|harden|mount|partition|flash|image|boot|join|network)\w*\b",
    re.IGNORECASE,
)
_ACTION_TOOLS = frozenset({"shell", "runbook", "code", "codegen"})


def _floor_query(task_text: str, tool: str) -> str:
    """One query from the step's own words, for when the query generator
    declines. Returns "" when the step is not action-shaped."""
    head = ""
    for line in (task_text or "").splitlines():
        t = line.strip()
        if t and not t.lower().startswith("context:"):
            head = t
            break
    if not head:
        return ""
    # Stop at the project Context blob and at sentence 2 — the first sentence is
    # the task; the rest is acceptance criteria and scope.
    head = re.split(r"(?<=[.!?])\s", head)[0].strip()
    if not head:
        return ""
    if (tool or "").strip().lower() not in _ACTION_TOOLS and not _ACTION_STEP_RE.search(head):
        return ""
    # Instance detail is why the first cut matched only the local corpus: an
    # external engine has nothing to say about "(palworld-server)" or
    # "'ubuntu-22.04.3-live-server-amd64.iso'". Strip parentheticals and quoted
    # spans, keep the technology and the action.
    head = re.sub(r"\([^)]*\)", " ", head)
    head = re.sub(r"'[^']*'|\"[^\"]*\"", " ", head)
    head = re.sub(r"\s+", " ", head).strip(" .,:;-")
    # Strip punctuation BEFORE the dangling-preposition rule, or the `$` anchor
    # never matches the orphan left behind by the quoted span ("… using the .").
    head = re.sub(r"\s+(?:using|with|from|via|on|for)\s+(?:the|a|an)\s*$",
                  "", head, flags=re.I)
    return head.strip(" .,:;-")[:150]


async def _research_prepass(
    *, task_text: str, tool: str, role: str, max_queries: int,
    node_key: str, domain: Optional[str], deep: bool = False,
    environment_block: str = "", floor_when_empty: bool = False,
) -> list[dict]:
    queries = await _detect_unknowns(
        task_text=task_text, tool=tool, role=role, max_queries=max_queries,
        environment_block=environment_block,
    )
    if queries == [] and floor_when_empty:
        # §17.912 — DETERMINISTIC FLOOR for the guide path.
        #
        # `_detect_unknowns` is an LLM judgment about whether research is
        # needed, and it declines precisely when the step text reads
        # confidently. Live (ADD5 "Install Ubuntu Server 22.04 on VM 106"): it
        # returned ZERO queries, so the walkthrough was written from model
        # memory alone — while the very same retrieval stack, asked "ubuntu
        # 22.04 installer stalls at Downloading and installing security updates
        # fix", returned two useful sources. A confident-sounding step
        # description is not evidence that nothing needs looking up; for an
        # install step on a real machine it is usually the opposite. This is
        # §17.882's medicine ("one DETERMINISTIC query, always") applied to the
        # guide path, which never got the equivalent floor.
        #
        # OPT-IN per call site, because `_detect_unknowns` swallows its own
        # errors and returns [] either way: an unconditional floor cannot tell
        # "the model declined" from "the tool call failed". Applied everywhere
        # it broke /fix's documented fail-soft contract ("no queries -> no
        # confirm calls") and made a decision node's "empty research is a
        # no-op" path start threading a research block. Only the guide path
        # carries the §17.912 defect, so only the guide path asks for the floor.
        floor = _floor_query(task_text, tool)
        if not floor:
            return []
        queries = [floor]
        logger.info(
            "assist_research_floor_query node_key=%s query=%r "
            "(detect_unknowns returned none)", node_key, floor[:120])
    elif not queries:
        return []  # declined with the floor off, or the machinery failed
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
    # §17.976 — the missing instrument. The line above logs how many QUERIES were
    # generated and nothing logs how many SOURCES came back, so "the searches ran
    # and returned nothing" was invisible: 19 guided steps in this database
    # carry an empty `research_sources`, interleaved with steps carrying 2-6, and
    # no failure was ever logged for any of them. A count at zero is the whole
    # signal — say it loudly enough to grep.
    if not sources:
        logger.warning(
            "assist_research_empty node_key=%s queries=%d deep=%s q=%r",
            node_key, len(queries), deep, [q[:80] for q in queries][:3])
    else:
        logger.info("assist_research_sources node_key=%s queries=%d sources=%d",
                    node_key, len(queries), len(sources))
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
        "LTS / newest driver) over naming a possibly-outdated specific value.\n"
        "§17.1027 DATES: each source shows its publication date when known. "
        "Where sources disagree, prefer the newer one and say so in one line. "
        "When the operator asks for the CURRENT or LATEST state of something, "
        "name the date of the source you relied on; an undated source cannot "
        "establish what is current."
    ]
    for i, s in enumerate(sources, 1):
        bits = [s["kind"]]
        if s.get("date"):
            bits.append(f"published {str(s['date'])[:10]}")
        if s.get("url"):
            bits.append(s["url"])
        parts.append(f"[{i}] ({' · '.join(bits)}) query: {s['query']}\n{s['text']}")
    return "\n\n".join(parts)


_FOCUS_QUERY_SYSTEM = (
    "You turn an operator's conversational question into ONE concise web-search "
    "query — the keywords a person would actually type. Drop filler ('can you "
    "walk me through', 'step by step', 'how do I'), keep the concrete nouns: "
    "product/tool names, the specific action, error text, versions. If PROJECT "
    "keywords are given, fold in the ones that pin down the RIGHT tech — the "
    "platform, product or model name they contain — so the search doesn't drift "
    "to a different tool (§17.1025: the example here used to name one "
    "operator's hypervisor, which taught every other operator's query to lean "
    "that way) — but "
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


# §17.1025 — LANGUAGE-level function words, deliberately not domain terms.
# The distinction is the operator's objection: a list of English connectives
# generalises to every subject, a list of networking phrases encodes the answer
# to one. Nothing here names a technology.
_GOAL_STOPWORDS = frozenset({
    # articles / conjunctions / prepositions
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "it", "its", "this", "that", "yet",
    "from", "into", "at", "by", "but", "their", "they", "as", "so", "than",
    "then", "when", "while", "after", "before", "during", "via", "per",
    # pronouns / speaker framing
    "operator", "user", "we", "our", "you", "your", "me", "my", "i",
    # modals / auxiliaries / generic verbs that pair into junk bigrams
    "cannot", "can", "could", "will", "would", "should", "must", "may", "might",
    "not", "does", "did", "has", "have", "had", "keeps", "keep", "find", "finds",
    "see", "sees", "get", "gets", "got", "make", "makes", "use", "uses", "using",
    "try", "tries", "trying", "need", "needs", "want", "wants", "seem", "seems",
    # generic state adjectives that carry no retrieval signal on their own
    "only", "still", "again", "just", "very", "more", "most", "some", "any",
    "unreachable", "fields", "visible", "available", "missing", "wrong", "bad",
})


def _goal_keywords(goal_terms: str, limit: int = 4) -> list[str]:
    """§17.1025 — the few terms from a recap's OPEN line worth adding to a query.

    Multi-word phrases first, then bare nouns. Capped hard: the point is to aim
    the search, not to paste a sentence into it.

    §17.1025 — the phrases are DERIVED from the text, not matched against a
    list. The first cut carried a literal tuple — ``("port forwarding", "dns
    record", "bridge mode", "nat loopback", …)`` — assembled by reading the one
    operator transcript this was written for. The operator's objection was
    exact: that is handing the engine the answer to their question, and it
    helps nobody whose problem is a kernel panic or a boot order or a disk
    passthrough. Adjacent non-stopword tokens ARE the technical phrase in
    English prose, in any domain, so the bigram is extracted rather than
    recognised.
    """
    import re as _re
    low = " ".join((goal_terms or "").lower().split())
    toks = _re.findall(r"[a-z][a-z0-9-]{2,}", low)
    phrases: list[str] = []
    for a, b in zip(toks, toks[1:]):
        if a in _GOAL_STOPWORDS or b in _GOAL_STOPWORDS:
            continue
        ph = f"{a} {b}"
        # Only a phrase that survives verbatim in the prose — adjacency in the
        # token stream can jump punctuation, which is not a phrase.
        if ph in low and ph not in phrases:
            phrases.append(ph)
    words: list[str] = []
    for w in toks:
        if len(w) < 4 or w in _GOAL_STOPWORDS or any(w in ph for ph in phrases):
            continue
        if w not in words:
            words.append(w)
    return (phrases + words)[:limit]


def _cap_query(q: str, max_words: int = 12) -> str:
    """§17.1023 — keyword engines degrade with length; hold the query short."""
    parts = (q or "").split()
    return " ".join(parts[:max_words])


async def research_one(
    *, question: str, node_key: str = "?", domain: Optional[str] = None,
    synthesize: bool = True, job_context: Optional[str] = None,
    context_hint: Optional[str] = None, operator_notes: Optional[list] = None,
    goal_terms: Optional[str] = None,
    provenance: Optional[str] = None, flagged: Optional[set] = None,
    sourced: Optional[set] = None,  # §17.1030 — the session's source-confirmed ledger
    owned_hosts: Optional[set] = None,  # §17.1032 — hosts the operator's ledger names
    confirmed: Optional[str] = None,  # §17.1034 — the operator's ledger text (confirmed tier)
) -> dict:
    """Confirm a single operator-supplied question and optionally synthesize
    a short cited answer. Does not persist — this is a side query.

    §17.650 — ``job_context`` (the project's brief + environment + a digest of
    completed DAG-node work) is folded into the synthesis prompt so the answer
    relays what THIS project already established instead of a project-blind web
    lookup. ``context_hint`` biases only the local-KB retrieval (see
    ``_confirm_query``).

    §17.1028 — ``provenance`` is the text that may CREDIT a value the answer
    states (ledgers + the operator's own words, never the engine's earlier
    replies); it defaults to ``job_context`` for callers that do not build
    one. ``flagged`` is the set of values earlier replies already carried
    under the Unverified footer; those are credited only by the sources and
    the question itself.
    """
    role = settings.assist_guide_model_role
    # §17.729 — search on a keyword-focused query, not the raw conversational
    # question (which returns nothing → stale-memory fallback). The original
    # `question` still drives synthesis below; only the retrieval query changes.
    web_q = await _focus_web_query(question, role=role, hint=context_hint or "")
    # §17.1027 — the need is derived ONCE and enforced into the query. This
    # replaces the §17.1021 (hardware) and §17.1023 (goal terms + cap) blocks
    # that lived here inline: same rules, now in `assist_evidence` where the
    # fix path shares them instead of carrying its own copy.
    from app.modules.assist_evidence import (
        derive_need, finalize_query, rank_evidence, verify_answer)
    need = derive_need(question, operator_notes=operator_notes,
                       goal_terms=goal_terms, assume_question=True)
    web_q = finalize_query(need, web_q, node_key=node_key)
    sources = await _confirm_query(
        question, node_key=node_key, domain=domain, deep=True,
        kb_query_extra=context_hint, web_query=web_q,
    )
    # §17.1036 — a QUESTION about a program deserves its documentation, not
    # only whatever forum thread ranked first. When no fetched page is
    # documentation-shaped, run one more web query aimed at documentation and
    # merge; ranking then puts the authoritative page first.
    try:
        from app.modules.assist_evidence import max_source_authority, _DOC_AUTHORITY
        if (need.kind == "question" and settings.assist_research_fetch_top_n > 0
                and max_source_authority(sources) < _DOC_AUTHORITY):
            # §17.1037 — the documentation words go FIRST. Appended after the
            # 12-word cap they were the words the cap removed, so on any long
            # question the "documentation" query was the original query again
            # (live: 'docker compose restart unless-stopped host reboot stop
            # exit code 1 deployment approach' — twelve words, no
            # "documentation" in it). The base is held to nine words.
            # "documentation" alone: "official" drew dictionary pages for the
            # word itself (merriam-webster, cambridge) into the results.
            _doc_q = _cap_query("documentation "
                                + " ".join((web_q or question).split()[:10]))
            _doc_sources = await _deep_web_sources(_doc_q, top_n=2)
            if max_source_authority(_doc_sources) < _DOC_AUTHORITY:
                # §17.1037 — vendor help centres are often script-rendered and
                # extract to nothing, so the fetch keeps a blog instead (live:
                # help.ui.com was the FIRST search result, extracted to nothing,
                # and a blog with authority 0.50 was what survived). The search
                # SNIPPET still names the page; keep documentation-grade
                # snippets whenever no fetched page reaches that authority.
                from app.modules.assist_evidence import source_authority
                for r in await _searxng_structured(_doc_q, max_results=5):
                    if r.get("url") and source_authority(r["url"]) >= _DOC_AUTHORITY:
                        _doc_sources.append({
                            "query": _doc_q, "kind": "searxng", "url": r["url"],
                            "title": r.get("title", ""), "date": (r.get("date") or "")[:10],
                            "text": f"{r.get('title', '')}\n{r.get('content', '')}".strip(),
                        })
                        if len(_doc_sources) >= 2:
                            break
            logger.info("assist_research_docs_query node_key=%s q=%r pages=%d",
                        node_key, _doc_q[:120], len(_doc_sources))
            if _doc_sources:
                sources.extend(_doc_sources)
    except Exception as exc:  # noqa: BLE001 — extra grounding is fail-soft
        logger.warning("assist_research_docs_query_failed: %s", exc)
    sources = rank_evidence(sources, need, node_key=node_key)  # §17.1027
    answer: Optional[str] = None
    grounding: Optional[dict] = None  # §17.1030 — the verifier's report, for the caller's ledger
    # Synthesize when we have web/KB sources OR project context to relay — a
    # question answerable purely from the project's own prior work must not be
    # dropped just because the open web returned nothing.
    if synthesize and (sources or (job_context or "").strip()):
        # §17.1024 — BOUND the project context and put it BEHIND the sources.
        #
        # Measured on the live session, one variable changed and nothing else:
        #
        #   job_context = 30,255 chars -> "go to 192.168.1.1 ... check the sticker"
        #   job_context = dropped      -> "Services -> Router -> Advanced Settings
        #                                  -> Port Forwarding & IP Reservations"
        #
        # The retrieval was never the problem by then: the engine had already
        # fetched reddit.com/r/Spectrum/.../how_to_set_sax1v1k_port_forwarding.
        # The prompt put 30 KB of the project's OWN PRIOR OUTPUT first and the
        # two fresh sources last, so the model restated what the project had
        # already said — including the wrong claim it had made three times.
        # Every wrong answer is digested into the project context and steers
        # the next one; that loop is why the operator got byte-similar text on
        # three consecutive asks.
        _ctx_raw = (job_context or "").strip()
        _cap = max(1000, int(getattr(settings, "assist_job_context_max_chars", 6000)))
        if len(_ctx_raw) > _cap:
            _ctx_raw = _ctx_raw[:_cap].rsplit("\n", 1)[0]
            logger.info("assist_research_ctx_trimmed node_key=%s from=%d to=%d",
                        node_key, len(job_context or ""), len(_ctx_raw))
        ctx_block = (
            "## Project background (what THIS build has already established — "
            "use it for our own values and decisions, NOT as a source of "
            "external product facts)\n" + _ctx_raw + "\n\n"
        ) if _ctx_raw else ""
        # §17.958/959 — read the operator's own words BEFORE answering them.
        from app.modules import assist_policy as _pol
        _pending_prompt = _pol.detect_interactive_prompt(question)
        _gui_question = _pol.looks_like_gui_question(question)
        _truncated = _pol.detect_truncated_paste(question)   # §17.962
        if _pending_prompt:
            logger.info("assist_interactive_prompt_detected node_key=%s kind=%s",
                        node_key, _pending_prompt.get("kind"))
        if _gui_question:
            logger.info("assist_gui_question_detected node_key=%s", node_key)
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
                # §17.958/959 — outermost, above the answer-and-lean rule:
                # both live failures were on THIS path. A pending interactive
                # prompt makes the immediate action a keystroke, and a question
                # asked about the web UI has to be answered about the web UI.
                {"role": "system", "content": apply_truncated_paste(
                  apply_interface_fidelity(
                  apply_interactive_prompt(
                    apply_recommendation(
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
                        is_decision=False, enabled=settings.assist_location_callout_enabled)),
                    prompt=_pending_prompt),
                  gui=_gui_question), truncated=_truncated)},
                {"role": "user", "content": (
                    # §17.1024 — question first, then the FRESH sources, then
                    # project background. The retrieved material is what can
                    # answer a question about someone else's product; the
                    # project's own history cannot, and leading with 30 KB of
                    # it buried the sources the search had just fetched.
                    # §17.1031 — and the question AGAIN, last: the background
                    # ends with the previous exchange, and live the model
                    # answered that one instead. The last thing it reads is
                    # now the question it is answering.
                    f"Question: {question}\n\n"
                    f"{_render_research_block(sources)}\n\n"
                    f"{ctx_block}"
                    f"---\nAnswer THIS question (not an earlier one in the "
                    f"conversation): {question}"
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
            # §17.959 — the directive is guidance; this is enforcement. Live,
            # the model was perfectly willing to open with "the Web UI is
            # confusing, skip it". One regeneration, told exactly what it did.
            if answer and _gui_question and _pol.answer_dodges_the_interface(
                    answer, question):
                logger.warning(
                    "assist_gui_answer_deflected node_key=%s (regenerating)",
                    node_key)
                _retry = await chat_until_nonempty(
                    model_router.chat,
                    [
                        {"role": "system", "content": apply_interface_fidelity(
                            _RESEARCH_SYNTH_SYSTEM, gui=True)},
                        {"role": "user", "content": (
                            f"{ctx_block}"
                            f"Question: {question}\n\n"
                            f"{_render_research_block(sources)}\n\n"
                            "---\nREGENERATION NOTICE: your previous answer "
                            "contained no navigation of the interface the "
                            "operator asked about — no screen, no menu, no "
                            "field, nothing to click. They asked how to do "
                            "this in the graphical interface. Answer THAT "
                            "question: the exact path, every field on the "
                            "screen, and what to leave alone. Offer a "
                            "command-line alternative only AFTER it, if at "
                            "all.")},
                    ],
                    {"role": role}, temperature=0.2, max_tokens=8192,
                    draws=2, label="assist_research_gui_retry",
                    think_off_rescue=True,
                )
                if _retry and _retry.success and (_retry.text or "").strip():
                    answer = _retry.text.strip()
            if answer:
                # §17.1027 — VERIFY the answer against what it was given, then
                # regenerate once with the unsupported values named, then
                # annotate what still cannot be traced. The corpus is the
                # UNTRIMMED project context on purpose: provenance is anything
                # this session knows, even the part the prompt could not fit.
                _synth_system = apply_next_callout(
                    _RESEARCH_SYNTH_SYSTEM, is_decision=False,
                    enabled=settings.assist_next_callout_enabled)
                _user_msg = (f"Question: {question}\n\n"
                             f"{_render_research_block(sources)}\n\n{ctx_block}"
                             f"---\nAnswer THIS question (not an earlier one in the "
                             f"conversation): {question}")

                async def _regen(notice: str) -> str:
                    r = await chat_until_nonempty(
                        model_router.chat,
                        [{"role": "system", "content": _synth_system},
                         {"role": "user", "content": _user_msg + notice}],
                        {"role": role}, temperature=0.2, max_tokens=8192,
                        draws=2, label="assist_research_grounding_regen",
                        think_off_rescue=True,
                    )
                    return (r.text or "").strip() if (r and r.success) else ""

                _notes_text = "\n".join(
                    (n.get("text") if isinstance(n, dict) else str(n)) or ""
                    for n in (operator_notes or []))
                _trusted = question + "\n" + _render_research_block(sources)
                answer, _vreport = await verify_answer(
                    answer, sources=sources,
                    corpus="\n".join([_trusted,
                                      (provenance if provenance is not None
                                       else (job_context or "")),
                                      _notes_text, context_hint or ""]),
                    need=need, node_key=node_key, label="assist_research",
                    regenerate=_regen,
                    trusted=_trusted, flagged=flagged,  # §17.1028
                    sourced=sourced,  # §17.1030
                    owned_hosts=owned_hosts,  # §17.1032
                    confirmed=confirmed or "",  # §17.1034
                )
                grounding = _vreport
            if answer:  # §17.897 — code-enforced copy-paste format
                answer = strip_operator_meta_preamble(answer)  # §17.908
                answer = promote_inline_commands(answer)
    return {"question": question, "sources": sources, "answer": answer,
            "grounding": grounding}
