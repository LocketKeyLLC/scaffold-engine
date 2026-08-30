"""Block renderers for assist guidance — extracted from assist_guide.py.

§17.856 (audit "assist decomposition") — the pure, synchronous formatting helpers
that turn session state (environment, facts, operator notes, conversation, step /
project recap) into the markdown blocks injected into guidance prompts and shown
to the operator. Self-contained: they call only each other + stdlib, so the whole
cluster lifts out without a cycle. Every name is re-exported from assist_guide
(`# noqa: F401`) so assist_guide.<NAME> and the wide external use keep resolving.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("scaffold.assist_guide")


def render_environment_block(environment: dict | None) -> str:
    """§17.487 — the operator's environment so the model emits concrete commands.

    ``environment`` = ``{"profile": str, "substitutions": {KEY: value}}`` (stored on
    ``assist_sessions.metadata.environment``). Returns "" when empty so callers no-op.
    """
    if not environment:
        return ""
    profile = (environment.get("profile") or "").strip()
    subs = environment.get("substitutions") or {}
    facts = [str(f).strip() for f in (environment.get("facts") or []) if str(f).strip()]
    if not profile and not subs and not facts:
        return ""
    parts = [
        "## Operator environment (use these concrete values; emit a <PLACEHOLDER> "
        "ONLY for values not given here)"
    ]
    if profile:
        parts.append(profile)
    if subs:
        parts.append("\n".join(f"- {k} = {v}" for k, v in subs.items()))
    # §17.709 — durable facts observed about the operator's ACTUAL system. Ground
    # on these; never assume a fresh/empty system when facts describe an existing
    # one (or say a check was inconclusive).
    if facts:
        parts.append(
            "### Known facts about the operator's system (OBSERVED — ground on "
            "these; do NOT assume a fresh/empty system, and treat anything marked "
            "unknown/unverified as still open):\n"
            + "\n".join(f"- {f}" for f in facts)
        )
    return "\n\n".join(parts)


def render_facts_block(environment: dict | None, *, max_chars: int = 4000) -> str:
    """§17.752 — just the durable observed facts (§17.709) as a compact block, for
    prompts that ground on the operator's ACTUAL system state (the recap, the
    note-impact analyzer) without the full environment/substitutions framing.
    Returns "" when there are no facts so callers thread it unconditionally.
    §17.812 — budget-capped: the ledger itself is trimmed newest-kept (§17.722)
    but grows to dozens of long facts on a real build; an uncapped render let it
    crowd out the transcript/recap in every prompt that threads it. Keeps the
    NEWEST facts (tail) within ``max_chars``, preserving order."""
    facts = [str(f).strip() for f in ((environment or {}).get("facts") or []) if str(f).strip()]
    if not facts:
        return ""
    kept: list[str] = []
    total = 0
    for f in reversed(facts):          # newest last → walk from the tail
        line_len = len(f) + 3          # "- " + newline
        if kept and total + line_len > max_chars:
            break
        kept.append(f)
        total += line_len
    kept.reverse()
    return "Known facts about the operator's system (observed):\n" + "\n".join(
        f"- {f}" for f in kept
    )


def render_operator_notes_block(notes: list[dict] | None) -> str:
    """§17.654 — the operator's captured notes & additions, threaded into every
    later step's guidance so the engine respects what they raised and stops
    re-assuming. ``notes`` = list of ``{ts, kind, node_key, text}``. Returns ""
    when empty so callers can thread it unconditionally.
    """
    if not notes:
        return ""
    lines: list[str] = []
    for n in notes:
        text_ = (n.get("text") or "").strip() if isinstance(n, dict) else ""
        if not text_:
            continue
        kind = (n.get("kind") or "note").strip() if isinstance(n, dict) else "note"
        lines.append(f"- ({kind}) {text_}")
    if not lines:
        return ""
    return (
        "## Operator notes & additions (things the operator has raised for THIS "
        "project — honor them; do not contradict or re-assume around them)\n"
        + "\n".join(lines)
    )


# §17.714 — deterministic "operator has changed direction / wants a fresh
# start" detection. The facts ledger is append-only (``set_environment`` never
# retracts), and the "never assume a fresh system" grounding rule (§17.709) was
# built for the OPPOSITE failure (the model fabricating a fresh install when one
# already existed). So once the operator EXPLICITLY decides to reinstall /
# rebuild / start over, the earlier-gathered facts describe an abandoned
# approach and the anti-fresh rule actively fights the operator's stated intent
# — the recurring "it's not following the conversation" report. Detect the reset
# intent and let the renderer foreground the decision + suspend the anti-fresh
# rule (§17.679 lesson: deterministic gate, don't re-tune an LLM). Patterns are
# reset/rebuild-anchored — a bare "install" or "clean" must NOT trip them.
_RESET_INTENT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bre-?install(ing|ed)?\b", re.I),
    re.compile(r"\bre-?imag(e|ing|ed)\b", re.I),
    re.compile(r"\bfresh\b.{0,24}\binstall\b", re.I),
    re.compile(r"\bclean\s+install\b", re.I),
    re.compile(r"\bstart(ing)?\s+(over|fresh|clean|from\s+scratch)\b", re.I),
    re.compile(r"\bfrom\s+scratch\b", re.I),
    re.compile(r"\brebuild(ing|s)?\b", re.I),
    re.compile(r"\bbare[-\s]?metal\s+(install|reinstall|rebuild)\b", re.I),
    re.compile(r"\bwipe\b.{0,30}\b(install|reinstall|reimage|rebuild)\b", re.I),
    re.compile(r"\babandon\b.{0,48}\binstead\b", re.I),
    # §17.720 — the live pivot said none of the above. The operator announced an
    # OS install from removable media / a new ISO / an in-progress installer
    # ("set up the new Proxmox ISO first", "i am currently installing it",
    # "options from the USB they are to install") and every pattern missed, so
    # the answers kept arguing them back to the in-place plan. Installing an OS
    # image from boot media over a system the plan calls existing IS a fresh
    # start — anchor on the media/ISO + install pairing so a bare "install
    # nginx" still cannot trip it.
    re.compile(
        r"\b(currently|now|in\s+the\s+middle\s+of|busy)\s+(re)?installing\s+"
        r"(it|the\s+(os|operating\s+system|system))\b", re.I),
    re.compile(r"\binstall(ing|er|ation)?\b.{0,50}\b(usb|flash\s*drive|bootable|installation\s+media)\b", re.I),
    re.compile(r"\b(usb|flash\s*drive|bootable\s+media)\b.{0,50}\binstall", re.I),
    re.compile(r"\bnew\b.{0,24}\biso\b", re.I),
    re.compile(r"\bboot(ing|ed)?\s+(from|into|off)\s+(the\s+)?(usb|flash|installer|iso)\b", re.I),
)


def _operator_reset_intent(notes: list[dict] | None) -> bool:
    """§17.714 — True when an operator note/decision declares a fresh start or
    rebuild that supersedes previously-gathered system state. Deterministic on
    the note text (any kind — a pivot lands as ``kind='decision'`` via §17.693,
    but honor it wherever it was recorded)."""
    for n in notes or []:
        if not isinstance(n, dict):
            continue
        if any(p.search(n.get("text") or "") for p in _RESET_INTENT_PATTERNS):
            return True
    return False


def render_session_memory(
    environment: dict | None, operator_notes: list[dict] | None = None,
    *, budget: int | None = None,
) -> str:
    """§17.710b — ONE consolidated session-memory block: execution context +
    observed facts + provided values + operator notes, in priority order and
    truncated to ``budget`` chars. This is the single injection path that
    replaces the separate env + notes blocks when ``assist_umem_inject`` is on,
    so every prompt (guidance / deliberation / verify) grounds on the same
    memory through one renderer. Grounding rule is baked in: never assume a
    fresh/empty system; treat anything marked unknown as still open.

    §17.714 — SUPERSESSION: when an operator note declares a fresh start /
    rebuild (``_operator_reset_intent``), lead with that decision, DEMOTE the
    now-superseded facts to "earlier observations (re-verify)", and SUSPEND the
    anti-fresh rule — the append-only facts ledger otherwise keeps injecting the
    abandoned approach as authoritative ground truth on every later step."""
    environment = environment or {}
    profile = (environment.get("profile") or "").strip()
    facts = [str(f).strip() for f in (environment.get("facts") or []) if str(f).strip()]
    subs = environment.get("substitutions") or {}
    notes = [
        n for n in (operator_notes or [])
        if isinstance(n, dict) and (n.get("text") or "").strip()
    ]
    if not (profile or facts or subs or notes):
        return ""

    # §17.722 — the facts section is ELASTIC under budget pressure: the ledger
    # is append-only and grows without bound, while every other section stays
    # small. Track where it sits so the trim below can shrink the facts LIST
    # (oldest dropped — the newest facts describe the system's current state)
    # instead of popping whole sections.
    facts_idx: int | None = None
    # Never-dropped protected slot: §17.714 reset-mode direction (reset branch)
    # or the §17.881 session playbook (normal branch) — one per render.
    direction_idx: int | None = None
    facts_header = ""

    def _facts_section(header_: str, items: list[str], omitted: int) -> str:
        marker = (
            f"\n(… {omitted} older facts omitted to fit the memory budget — newest kept)"
            if omitted else ""
        )
        return header_ + marker + "\n" + "\n".join(f"- {f}" for f in items)

    if _operator_reset_intent(notes):
        # §17.714 — operator has explicitly chosen a fresh start. Direction
        # first (protected from budget-trim by the >2 guard below), facts
        # demoted + reframed, anti-fresh rule suspended.
        header = (
            "## Session memory — the operator has CHANGED DIRECTION (read this first)\n"
            "The operator has decided to start fresh / rebuild. Their **current "
            "direction** below SUPERSEDES the earlier gathered state AND any "
            "project goal / brief wording elsewhere in this prompt that conflicts "
            "with it — follow it: do NOT keep operating against the prior system, "
            "argue them back to it, or restate the old plan as what they should "
            "be doing. For THIS session the usual \"never assume a fresh system\" "
            "rule is SUSPENDED — they have explicitly chosen a fresh start; still "
            "treat anything unknown/unverified as open and ask."
        )
        sections: list[str] = [header]
        if notes:
            direction_idx = len(sections)
            sections.append(
                "**Operator's current direction (latest decision — supersedes the state below):**\n"
                + "\n".join(f"- [{(n.get('kind') or 'note')}] {n['text'].strip()}" for n in notes)
            )
        if facts:
            facts_header = (
                "**Earlier observations (gathered during the PREVIOUS approach the "
                "operator has since abandoned — re-verify before relying on any of "
                "them; most will not hold after the fresh start):**"
            )
            facts_idx = len(sections)
            sections.append(_facts_section(facts_header, facts, 0))
        if subs:
            sections.append("**Provided values:**\n" + "\n".join(f"- {k} = {v}" for k, v in subs.items()))
        if profile:
            sections.append(
                "**Execution context (re-confirm the host/hostname after a rebuild):** " + profile
            )
    else:
        header = (
            "## Session memory — what's known so far (ground on this; do NOT assume a "
            "fresh/empty system, and treat anything marked unknown/unverified as still open)"
        )
        # Priority order: context + facts are load-bearing for grounding; provided
        # values next; notes last. Under budget pressure the facts LIST trims
        # first (newest kept); whole sections drop from the tail only when even
        # that isn't enough.
        sections = [header]
        if profile:
            sections.append(f"**Execution context:** {profile}")
        # §17.881 — the playbook leads the facts: proven/ruled-out methods are
        # the highest-leverage memory (they change WHAT the model prescribes,
        # not just which values it fills in) and are never budget-dropped.
        pb_block = render_playbook_block(environment)
        if pb_block:
            direction_idx = len(sections) if direction_idx is None else direction_idx
            sections.append(pb_block)
        if facts:
            facts_header = "**Observed facts:**"
            facts_idx = len(sections)
            sections.append(_facts_section(facts_header, facts, 0))
        if subs:
            sections.append("**Provided values:**\n" + "\n".join(f"- {k} = {v}" for k, v in subs.items()))
        if notes:
            sections.append(
                "**Operator notes / requirements (carry forward):**\n"
                + "\n".join(f"- [{(n.get('kind') or 'note')}] {n['text'].strip()}" for n in notes)
            )
    block = "\n\n".join(sections)
    if budget and len(block) > budget:
        # §17.722 — trim the facts LIST first, whole sections only as a last
        # resort. The old logic popped whole sections from the tail (notes →
        # values → the ENTIRE facts section), so the moment a session's ledger
        # outgrew the budget the injected memory collapsed to just the header +
        # execution profile — the live "worked great, then suddenly stopped
        # retaining anything" cliff (facts, VMID/VM_NAME values, and the
        # operator's own notes all silently vanished from every prompt).
        if facts_idx is None:
            # No facts section — the old behavior (pop tail, keep the header +
            # the load-bearing second section) is still right.
            while len(sections) > 2 and len("\n\n".join(sections)) > budget:
                sections.pop()
        else:
            while True:
                overhead = sum(
                    len(s) + 2 for i, s in enumerate(sections) if i != facts_idx
                )
                # Room for the facts section, reserving space for the
                # omitted-count marker line.
                room = budget - overhead - 80
                kept: list[str] = []
                used = len(facts_header)
                for f in reversed(facts):
                    line = len(f) + 3  # "- " prefix + newline
                    if used + line > room:
                        break
                    kept.append(f)
                    used += line
                kept.reverse()
                if kept:
                    sections[facts_idx] = _facts_section(
                        facts_header, kept, len(facts) - len(kept)
                    )
                    break
                # Not even one (newest) fact fits — drop the lowest-priority
                # section and retry with the freed room. The header, the facts
                # slot, and a §17.714 direction section are never dropped.
                droppable = [
                    i for i in range(len(sections))
                    if i not in (0, facts_idx, direction_idx)
                ]
                if not droppable:
                    del sections[facts_idx]
                    break
                drop = max(droppable)
                del sections[drop]
                if drop < facts_idx:
                    facts_idx -= 1
        block = "\n\n".join(sections)
        if len(block) > budget:
            block = block[:budget].rstrip() + "\n… (memory truncated)"
    return block


def render_playbook_block(environment: dict | None) -> str:
    """§17.881 — the session playbook as a BINDING block: methods PROVEN on
    this system this session, and approaches that already FAILED here. Derived
    at step-commit time (reconcile_on_commit); rendered into every generation
    so the model prefers session-proven methods over its own priors — the live
    failure this closes: the engine guessed fresh install URLs for component
    N+1 while its own session had already proven the working pattern on
    component N. Returns "" when the playbook is empty."""
    pb = (environment or {}).get("playbook") or {}
    proven = [str(x).strip() for x in (pb.get("proven") or []) if str(x).strip()]
    ruled = [str(x).strip() for x in (pb.get("ruled_out") or []) if str(x).strip()]
    if not proven and not ruled:
        return ""
    parts = [
        "## Session playbook (BINDING — learned on THIS system, this session; "
        "takes precedence over remembered or generic methods)"
    ]
    if proven:
        parts.append(
            "**Proven to work here — when a task matches, use these instead of "
            "any method from memory:**\n" + "\n".join(f"- {p}" for p in proven)
        )
    if ruled:
        parts.append(
            "**Already failed here — do NOT prescribe these again (if truly "
            "unavoidable, state explicitly why it will work this time):**\n"
            + "\n".join(f"- {r}" for r in ruled)
        )
    return "\n\n".join(parts)


def _render_memory_or_legacy(
    environment: dict | None, operator_notes: list[dict] | None,
) -> list[str]:
    """§17.710b — the single decision point for memory injection. When
    ``assist_umem_inject`` is on, return the unified ``render_session_memory``
    block; else the legacy separate environment + notes blocks (byte-identical
    to pre-§17.710b). Returns the non-empty parts to append to a prompt."""
    if settings.assist_unified_memory_enabled and settings.assist_umem_inject:
        mem = render_session_memory(
            environment, operator_notes, budget=settings.assist_umem_max_chars,
        )
        return [mem] if mem else []
    out: list[str] = []
    env_block = render_environment_block(environment)
    if env_block:
        out.append(env_block)
    pb_block = render_playbook_block(environment)  # §17.881 — both paths carry it
    if pb_block:
        out.append(pb_block)
    notes_block = render_operator_notes_block(operator_notes)
    if notes_block:
        out.append(notes_block)
    return out


def render_conversation_block(
    history: list[dict] | None, *, max_chars: int = 4000,
) -> str:
    """§17.687 — the recent OWUI back-and-forth (you ⇄ operator) so a follow-up
    that refers back to something either of you just said resolves.

    ``history`` = list of ``{role, content}`` (oldest first). The CURRENT
    operator message is NOT included here — it's threaded separately as the
    refine / question / error. Keeps the MOST RECENT turns within ``max_chars``
    (drops oldest first) and returns "" when empty so callers thread it
    unconditionally. Fail-soft on malformed items.
    """
    if not history or max_chars <= 0:
        return ""
    rendered: list[str] = []
    for m in history:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # Guard a single runaway turn (a huge pasted walkthrough) so one message
        # can't blow the whole budget; keep the head (the suggestion/decision
        # framing lives up top per the brevity floor §17.643).
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + " …[truncated]"
        who = "Operator" if role == "user" else "You (assistant)"
        rendered.append(f"{who}: {content}")
    if not rendered:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(rendered):
        cost = len(line) + 2  # +2 for the blank-line join
        if kept and total + cost > max_chars:
            break
        kept.append(line)
        total += cost
    kept.reverse()
    return (
        "## Recent conversation (you ⇄ the operator, most recent last) — the "
        "operator may refer back to something either of you just said (\"that "
        "one\", \"the program you suggested\", \"yes, do it\"); honor it and stay "
        "consistent with what you already told them\n"
        + "\n\n".join(kept)
    )


def render_step_recap_block(recap: str | None) -> str:
    """§17.738 — the running recap as a prompt block. The assistant grounds on it
    so it doesn't re-suggest resolved fixes or forget which machine we're on."""
    r = (recap or "").strip()
    if not r:
        return ""
    return (
        "## Where we are on this step (running recap — the AUTHORITATIVE current "
        "state of the work; ground on this). It reflects what is TRUE RIGHT NOW, "
        "including rework the operator has done. If an earlier completed-step / "
        "upstream output above (even one marked MANDATORY) claims something is "
        "already done, but this recap's OPEN says it is NOT yet working, TRUST "
        "THIS RECAP — the operator likely redid or undid that work (e.g. rebuilt "
        "a machine), so the older output is stale. Do NOT re-suggest anything "
        "under DONE, do NOT push ahead to later work while an OPEN item blocks "
        "it, and keep straight which machine the next commands run on.\n" + r
    )


def render_project_recap_block(recap: str | None) -> str:
    """§17.753 — the whole-project recap as a prompt block, prepended to the raw
    job digest (§17.650) so every generation site leads with the distilled arc."""
    r = (recap or "").strip()
    if not r:
        return ""
    return (
        "## Whole-project state (distilled — where this build stands ACROSS all "
        "steps; ground on it for the arc: what earlier steps decided, what remains, "
        "and the project-wide constraints/system facts. It complements the raw "
        "per-step outputs below — trust it for the big picture and stay consistent "
        "with the DECISIONS/CONSTRAINTS it lists).\n" + r
    )


_RECAP_LABELS = ("GOAL", "DONE", "OPEN", "CONSTRAINTS", "NEXT", "CONTEXT")


def _recap_add(out: dict[str, Any], field: str, text_: str) -> None:
    text_ = text_.strip()
    if not text_:
        return
    if field in ("done", "open", "constraints"):
        out[field].append(text_)
    else:  # goal / next / context are single-valued
        out[field] = (out[field] + " " + text_).strip() if out[field] else text_


def parse_recap(recap: str | None) -> dict[str, Any]:
    """§17.741 — parse a labeled recap into ``{goal, done[], open[],
    constraints[], next, context}``. The recap is line-oriented (``LABEL:`` leads
    a line, optionally with inline text, then bullet fragments). Tolerant of
    markdown bullets, missing labels, and free spacing. Blank/unparseable → empty
    fields; never raises. (§17.742 added ``constraints``.)"""
    out: dict[str, Any] = {"goal": "", "done": [], "open": [], "constraints": [], "next": "", "context": ""}
    r = (recap or "").strip()
    if not r:
        return out
    current: Optional[str] = None
    for raw_line in r.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        stripped = line.lstrip("#*-•> ").strip()
        matched = None
        for lab in _RECAP_LABELS:
            up = stripped.upper()
            if up.startswith(lab + ":") or up == lab:
                matched = lab
                # tolerate a bold label ("**GOAL:** x" → the "**" after the
                # colon) as well as the plain "GOAL: x" the recap prompt emits.
                rest = stripped[len(lab):].lstrip(": *").strip()
                break
        if matched is not None:
            current = matched.lower()
            if rest:
                _recap_add(out, current, rest)
            continue
        if current:  # continuation / bullet under the current label
            _recap_add(out, current, stripped)
    return out


def render_status_panel(recap: str | None) -> str:
    """§17.741 — the operator-facing "📍 Where we are" panel, built from the
    §17.738 recap. Returns "" unless the recap carries REAL progress (at least
    one DONE / OPEN / NEXT item): a goal-only recap (turn 1, nothing done yet)
    is not worth a panel and would just be noise. Never raises."""
    p = parse_recap(recap)
    if not (p["done"] or p["open"] or p["next"] or p["constraints"]):
        return ""
    lines = ["**📍 Where we are on this step**", ""]
    if p["goal"]:
        lines.append(f"- **Goal:** {p['goal']}")
    if p["done"]:
        lines.append("- ✅ **Done:** " + " · ".join(p["done"]))
    if p["open"]:
        lines.append("- ⬜ **Still open:** " + " · ".join(p["open"]))
    if p["constraints"]:  # §17.742 — the limits/ruled-out approaches govern the next move
        lines.append("- ⚠️ **Constraints:** " + " · ".join(p["constraints"]))
    if p["next"]:
        lines.append("- 👉 **Next:** " + p["next"])
    return "\n".join(lines) + "\n"
