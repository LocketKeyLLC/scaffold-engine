"""§17.1039 — the evidence layer on the EXECUTOR.

§17.1027 put need → ranked evidence → verified answer on every assist path;
§17.1038 checked the plan. Autonomous execution still ran on the older shape:
a SearXNG node searched the web for its TITLE and was handed snippets; an LLM
node got a RAG block; and NOTHING checked what a node then wrote. A node
output that states an address, a port, a version or a URL from the model's
memory is persisted as `output_text`, handed to every downstream node as
"upstream context", and compiled into the deliverable — the same laundering
the assist paths closed in §17.1028, one layer down.

Three pieces, each the shared machinery and not a copy of it:

* ``node_need`` — the task's title and notes (and the project goal) become a
  `Need` through `derive_need`, so the web query carries the task's terms and
  the operator's named hardware instead of a bare title.
* ``web_sources`` — SearXNG nodes fetch the PAGES (not snippets) the way the
  assist research path does, ranked by relevance → authority → date, rendered
  with publication dates and the currency rule.
* ``verify_node_output`` — every concrete value in a node's output is traced
  to what the operator gave (brief + request), the task text, the upstream
  outputs and the sources; one regeneration with the values named; what is
  still unsupported is REPORTED (execution log + node result), never appended
  to the output text — a footer inside `output_text` would be parroted by the
  next node and compiled into the deliverable (§17.1037d). Values an upstream
  node's report flagged are not credited by that upstream's text (§17.1028).
  CodeGen output is checked but never regenerated — a pinned version in code
  is a claim to report, not a paragraph to redraw.

Both valves default ON; `verify_answer` additionally honours the assist
verification valve, which is the layer's master switch.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text

from app.config import settings
from app.modules.assist_evidence import (
    Need, derive_need, finalize_query, owned_hosts, rank_evidence, unsupported_specifics,
    verify_answer,
)

logger = logging.getLogger("scaffold")


def node_task_text(node: dict) -> str:
    return "\n".join(p for p in (str(node.get("title") or ""),
                                 str(node.get("prompt_template") or "")) if p)


def node_need(node: dict, brief: Optional[dict]) -> Need:
    """The task as an information need. Task notes are prose (no failure line,
    rarely a question), so this lands on the ``goal`` kind: the query is the
    operator's hardware plus the task's own terms, capped."""
    title = str(node.get("title") or "")
    goal = ""
    if isinstance(brief, dict):
        goal = str(brief.get("description") or "")
        if not goal:
            goals = brief.get("goals") or []
            goal = str(goals[0]) if goals else ""
    need = derive_need(node_task_text(node), title=title,
                       goal_terms=f"{title}. {goal}".strip(". "))
    if not need.researchable:
        need = Need(kind="goal", subject=title, query=" ".join(title.split()[:12]),
                    reason="task title")
    return need


async def web_sources(need: Need, *, node_key: str, domain: Optional[str]) -> list[dict]:
    """§17.1039 — page content for a SearXNG node, ranked and dated. Falls
    back to the snippet path when the valve is off."""
    from app.modules.assist_research_lib import _confirm_query, _is_useful_grounding
    # The need's hardware and goal terms are ENFORCED into the query here, at
    # the retrieval site itself (the §17.1020 retrieval-grounding gate).
    query = finalize_query(need, node_key=node_key)
    if not query:
        return []
    if settings.execution_evidence_retrieval_enabled:
        srcs = await _confirm_query(query, node_key=node_key, domain=domain, deep=True)
        ranked = rank_evidence(srcs, need, node_key=node_key)
        logger.info("node_web_sources node_key=%s query=%r fetched=%d kept=%d dated=%d",
                    node_key, query[:120], len(srcs), len(ranked),
                    sum(1 for s in ranked if s.get("date")))
        return ranked
    from app.modules.execution_agent import _searxng_search
    block = await _searxng_search(query)
    return [{"kind": "searxng", "query": query, "text": block.strip()}] if _is_useful_grounding(block) else []


def render_node_sources(sources: list[dict]) -> str:
    from app.modules.assist_research_lib import _render_research_block
    return _render_research_block(sources) if sources else ""


async def fetch_upstream_flagged(db, job_id: str, depends_on: list[str]) -> set[str]:
    """Values the upstream nodes' evidence reports left unsupported (§17.1040:
    read from ``dag_nodes.evidence``, the latest report per node). Fail-soft."""
    if not depends_on:
        return set()
    out: set[str] = set()
    try:
        rows = await db.execute(text("""
            SELECT evidence FROM dag_nodes
             WHERE job_id = :jid AND node_key = ANY(:keys) AND evidence IS NOT NULL
        """), {"jid": job_id, "keys": list(depends_on)})
        for (ev,) in rows.fetchall():
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except (ValueError, TypeError):
                    ev = {}
            for v in (ev or {}).get("unsupported") or []:
                if isinstance(v, str) and v.strip():
                    out.add(v.strip())
    except Exception as exc:  # noqa: BLE001 — a ledger miss must not fail a node
        logger.warning("node_upstream_flagged_read_failed job=%s err=%r", job_id, exc)
    return out


def given_text(brief: Optional[dict], input_text: Optional[str]) -> str:
    """What the OPERATOR gave: the refined brief and their original request —
    the confirmed tier for autonomous execution."""
    from app.modules.assist_agent import _brief_text
    return "\n".join(p for p in (_brief_text(brief), str(input_text or "")) if p)


def evidence_summary(report: dict) -> dict:
    if not report or not report.get("checked"):
        return {"checked": False}
    cite = report.get("citation") or {}
    return {
        "checked": True,
        "unsupported": [u["value"] for u in report.get("unsupported") or []],
        "plan_only": [u["value"] for u in report.get("plan_only") or []],
        "command_shape": [s["command"] for s in report.get("command_shape") or []],
        "citation_score": cite.get("score") if isinstance(cite, dict) else None,
        "regenerated": bool(report.get("regenerated")),
        "sourced_now": [it["value"] for it in report.get("sourced_now") or []],
        "footer": report.get("footer") or "",
    }


async def verify_node_output(
    output: str,
    *,
    need: Optional[Need],
    sources: list[dict],
    brief: Optional[dict],
    input_text: Optional[str],
    task_text: str,
    upstream_text: str,
    upstream_flagged: set[str],
    tool: Optional[str],
    node_key: str,
    regenerate: Callable[[str], Awaitable[str]],
) -> tuple[str, dict]:
    """Verify one node's output against its grounding. Returns the (possibly
    regenerated) output — never annotated — and the verifier's report."""
    if not settings.execution_answer_verification_enabled or not (output or "").strip():
        return output, {"checked": False}
    tool_l = (tool or "").lower()
    if tool_l == "mcp":
        return output, {"checked": False}
    given = given_text(brief, input_text)
    corpus = "\n".join(p for p in (given, task_text, upstream_text) if p)

    async def _regen(notice: str) -> str:
        if tool_l == "codegen":
            return ""  # report, never redraw code
        return await regenerate(notice)

    out, report = await verify_answer(
        output, sources=sources, corpus=corpus, need=need, node_key=node_key,
        label=f"node_{tool_l or 'llm'}", regenerate=_regen, trusted="",
        flagged=set(upstream_flagged or ()), sourced=set(),
        owned_hosts=owned_hosts({"_brief_text": given}), confirmed=given,
        annotate=False,
    )
    if report.get("unsupported"):
        logger.warning("node_output_unsupported node_key=%s tool=%s values=%r regenerated=%s",
                       node_key, tool_l, [u["value"] for u in report["unsupported"]][:6],
                       report.get("regenerated"))
    return out, report


# ---------------------------------------------------------------------------
# §17.1041 — the COMPILE step. The deliverable is a rewrite of verified node
# outputs; two things can still go wrong with its values: the synthesizer
# INTRODUCES one that no step output and no operator text states (§17.360's
# class — `tskey-abc123…`, hardcoded addresses in place of placeholders), or
# it CARRIES one that a step's own check left unverified. Both are named at
# the top of the deliverable — the surface the operator reads — and recorded
# on the job. No model call, no rewrite: the node layer already regenerated.
# ---------------------------------------------------------------------------

def compile_value_check(text_value: str, *, nodes: list, brief: Optional[dict],
                        input_text: Optional[str]) -> tuple[list[dict], list[dict]]:
    """Returns ``(introduced, carried)`` — values in the deliverable that
    appear in no step output and nothing the operator gave; and values a
    step's evidence report flagged that nothing the operator gave confirms."""
    given = given_text(brief, input_text)
    node_text = "\n".join(str(n.get("output_text") or "") for n in (nodes or [])
                          if isinstance(n, dict) or hasattr(n, "get"))
    flagged: set[str] = set()
    for n in nodes or []:
        ev = n.get("evidence") if hasattr(n, "get") else None
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except (ValueError, TypeError):
                ev = None
        if isinstance(ev, dict):
            flagged.update(str(v) for v in (ev.get("unsupported") or []) if v)
    owned = owned_hosts({"_brief_text": given})
    introduced = unsupported_specifics(text_value, given + "\n" + node_text, owned=owned)
    not_given = unsupported_specifics(text_value, given, owned=owned)
    intro_vals = {u["value"] for u in introduced}
    carried = [u for u in not_given if u["value"] in flagged and u["value"] not in intro_vals]
    return introduced, carried


def compile_value_banner(introduced: list[dict], carried: list[dict]) -> str:
    """Operational metadata prepended AFTER synthesis, like the other compile
    banners — it names values, it does not rewrite them."""
    lines: list[str] = []
    if introduced:
        lines.append(
            "> ⚠️ **Value check:** these values appear in none of the step outputs "
            "or the brief — the compile step introduced them; confirm each before "
            "relying on it: " + ", ".join(f"`{u['value']}` ({u['kind']})" for u in introduced[:8]))
    if carried:
        lines.append(
            "> ℹ️ **Carried unverified:** a step stated these and its own check could "
            "not trace them to a source or the brief: "
            + ", ".join(f"`{u['value']}` ({u['kind']})" for u in carried[:8]))
    return ("\n".join(lines) + "\n\n") if lines else ""
