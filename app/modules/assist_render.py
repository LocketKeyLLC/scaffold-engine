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


# §17.1015 — wording that marks a fact as NOT established. Kept tight: these
# are the shapes the fact distiller actually emits when it records a doubt,
# plus the operator's own "unsure"/"not sure" carried through verbatim.
_UNCERTAIN_FACT_RE = re.compile(
    r"\b(?:un(?:sure|certain|verified|confirmed|known)|"
    r"not\s+(?:sure|certain|verified|confirmed|established|known)|"
    r"no\s+idea|could\s+not\s+(?:be\s+)?(?:verify|verified|confirm|confirmed)|"
    r"inconclusive|unclear|may\s+not\s+be|might\s+not\s+be|"
    r"has\s+not\s+been\s+(?:verified|confirmed))\b",
    re.IGNORECASE,
)


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
    # §17.913 — tools this shell has PROVEN it lacks.
    missing = [m for m in (environment.get("missing_tools") or [])
               if isinstance(m, dict) and str(m.get("tool") or "").strip()]
    state = environment.get("system_state") if isinstance(environment.get("system_state"), dict) else {}
    if not profile and not subs and not facts and not missing and not state:
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
        # §17.1015 — split SETTLED observations from OPEN ones. The header
        # already said "treat anything marked unknown/unverified as still open",
        # and the model read straight past it. Live (T35, 2026-09-11 02:09) the
        # ledger held, verbatim:
        #
        #   "A security group was added (operator is unsure where the 'dmz'
        #    security group came from)."
        #
        # and the walkthrough answered "the `dmz` group was created earlier in
        # the project" — asserting the provenance the fact records as UNKNOWN,
        # to the operator who had just said they did not know. A caveat inside a
        # heading is a request; putting the open items under their own heading
        # is structure, and structure survives a model skimming bullets.
        settled = [f for f in facts if not _UNCERTAIN_FACT_RE.search(f)]
        open_items = [f for f in facts if _UNCERTAIN_FACT_RE.search(f)]
        if settled:
            parts.append(
                "### Known facts about the operator's system (OBSERVED — ground "
                "on these; do NOT assume a fresh/empty system):\n"
                + "\n".join(f"- {f}" for f in settled)
            )
        if open_items:
            parts.append(
                "### OPEN — recorded as uncertain, NOT established\n"
                "These are things nobody has confirmed yet. Do NOT state them as "
                "settled fact, do NOT explain where they came from, and do NOT "
                "reassure the operator about them. If one matters to this step, "
                "give them a command or a screen that SETTLES it:\n"
                + "\n".join(f"- {f}" for f in open_items)
            )
    # §17.913 — WITHOUT this the ledger never reaches the model, and the engine
    # only "knows" a tool is missing on the one turn whose error text mentions
    # it. Live: `sudo lvextend …` was emitted to a root@pve shell, died with
    # `sudo: command not found`, and four minutes later the SAME sudo command
    # was emitted again — the shell's verdict had nowhere to live.
    # §17.914 — GROUND TRUTH first: parsed from the operator's own output, so
    # it outranks the LLM-distilled prose facts above it.
    if state:
        from app.modules.assist_state import render_system_state
        block = render_system_state(state)
        if block:
            parts.append(block)
    # §17.965 — expected vs measured file sizes, same ground-truth tier.
    from app.modules.assist_files import render_file_writes
    _fw_state = (environment.get("file_writes")
                 if isinstance(environment.get("file_writes"), dict) else {})
    _fw = render_file_writes(_fw_state)
    if _fw:
        parts.append(_fw)
    # §17.968 — and where two of those files disagree with each other.
    from app.modules.assist_contracts import (
        bounded_artefacts, find_contract_conflicts, render_contract_conflicts)
    _cc = render_contract_conflicts(
        find_contract_conflicts(bounded_artefacts(_fw_state)))
    if _cc:
        parts.append(_cc)
    if missing:
        parts.append(
            "### NOT AVAILABLE on the operator's system (the shell reported "
            "these missing — never emit a command that depends on one; if it is "
            "genuinely required, the FIRST step is installing it):\n"
            + "\n".join(
                f"- `{m['tool']}`"
                + (f" (on {m['host']})" if str(m.get('host') or '').strip() else "")
                + ("  — omit it for commands on THAT HOST ONLY; a VM/guest "
                   "console is an ordinary user and still needs sudo"
                   if str(m['tool']).lower() == "sudo" else "")
                for m in missing)
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
        # §17.908 — provenance, because "honor them" was read as "these are
        # requirements of record". Live: an operator aside ("I want to build a
        # markdown linter") became a note, and a later turn told the operator
        # "the project brief already lists several Extras (Markdown linter,
        # screenshot-to-PDF tool)". The brief contains no such thing — the note
        # was laundered into a fabricated fact about the approved plan.
        "These are things said IN CONVERSATION. They are NOT the approved brief "
        "or the plan: never describe one as something the brief lists, the plan "
        "includes, or the project already covers.\n"
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
    # §17.913/914 — ground truth read from the operator's own command output,
    # and the tools their shell has proven it lacks. These live HERE because
    # render_session_memory is the single injection path (§17.751): putting them
    # only in render_environment_block, as the first cut did, meant the fix path
    # never saw them at all — the engine went on asking for `qm config 106`
    # (21 times live) and contradicting a boot order it had been given.
    from app.modules.assist_state import render_system_state
    state_block = render_system_state(environment.get("system_state")
                                      if isinstance(environment.get("system_state"), dict) else {})
    from app.modules.assist_files import render_file_writes   # §17.965
    _fws = (environment.get("file_writes")
            if isinstance(environment.get("file_writes"), dict) else {})
    _fw_block = render_file_writes(_fws)
    if _fw_block:
        state_block = (state_block + "\n\n" + _fw_block) if state_block else _fw_block
    from app.modules.assist_contracts import (  # §17.968
        bounded_artefacts, find_contract_conflicts, render_contract_conflicts)
    _cc_block = render_contract_conflicts(
        find_contract_conflicts(bounded_artefacts(_fws)))
    if _cc_block:
        state_block = (state_block + "\n\n" + _cc_block) if state_block else _cc_block
    missing = [m for m in (environment.get("missing_tools") or [])
               if isinstance(m, dict) and str(m.get("tool") or "").strip()]
    missing_block = ""
    if missing:
        missing_block = (
            "**NOT AVAILABLE on this system** (the shell reported these missing — "
            "never emit a command that depends on one; if it is genuinely needed, "
            "installing it is the FIRST step):\n"
            + "\n".join(
                f"- `{m['tool']}`"
                + (f" (on {m['host']})" if str(m.get('host') or '').strip() else "")
                + ("  — the operator is root there, so simply omit it"
                   if str(m['tool']).lower() == "sudo" else "")
                for m in missing))
    if not (profile or facts or subs or notes or state_block or missing_block):
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
    # §17.941 — the playbook's OWN index. `direction_idx` is claimed by
    # whichever direction section lands first, so once a §17.914 state block
    # existed the playbook was unprotected and got dropped WHOLE under
    # budget pressure — taking every `ruled_out` prohibition with it, while
    # the §17.881 comment below promised the opposite. Measured at a 4k
    # budget with a full playbook.
    playbook_idx: int | None = None
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
        if state_block:  # §17.914 — survives a reset; it is measured, not inferred
            sections.append(state_block)
        if missing_block:
            sections.append(missing_block)
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
        # §17.914 — CONFIRMED state outranks the distilled prose facts below it.
        if state_block:
            direction_idx = len(sections) if direction_idx is None else direction_idx
            sections.append(state_block)
        if missing_block:
            sections.append(missing_block)
        # §17.881 — the playbook leads the facts: proven/ruled-out methods are
        # the highest-leverage memory (they change WHAT the model prescribes,
        # not just which values it fills in) and are never budget-dropped.
        # §17.941 — but `proven` is elastic WITHIN it. A quarter of the budget
        # is a generous share for re-derivable methods and leaves the facts
        # ledger room to breathe; `ruled_out` is exempt and always renders in
        # full. Without this the store cap had to stay artificially low (12) to
        # protect the prompt, which is what made the session forget methods it
        # had proven.
        pb_block = render_playbook_block(
            environment,
            proven_budget=(max(400, budget // 4) if budget else None),
        )
        if pb_block:
            direction_idx = len(sections) if direction_idx is None else direction_idx
            playbook_idx = len(sections)
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
                    if i not in (0, facts_idx, direction_idx, playbook_idx)
                ]
                if not droppable:
                    del sections[facts_idx]
                    break
                drop = max(droppable)
                del sections[drop]
                # §17.941 — adjust BOTH indices. Only facts_idx was shifted, so
                # after a single drop `direction_idx` aliased whatever slid into
                # its slot — usually the facts section — and the playbook it was
                # meant to protect became droppable on the next pass. Measured:
                # at a 4k budget a full playbook was deleted entirely while the
                # facts it was supposed to outrank survived, exactly inverting
                # the §17.881 priority the comment above still promised.
                if drop < facts_idx:
                    facts_idx -= 1
                if direction_idx is not None and drop < direction_idx:
                    direction_idx -= 1
                if playbook_idx is not None and drop < playbook_idx:
                    playbook_idx -= 1
        block = "\n\n".join(sections)
        if len(block) > budget:
            block = block[:budget].rstrip() + "\n… (memory truncated)"
    return block


def render_playbook_block(
    environment: dict | None, *, proven_budget: int | None = None,
) -> str:
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
    # §17.941 — `proven` is ELASTIC under budget pressure; `ruled_out` is not.
    #
    # The store cap had been doing prompt-budget duty: `assist_playbook_max`
    # was 12 not because twelve methods is the right thing to REMEMBER but
    # because more than that crowded the injected block. Measured on the live
    # session at the deployed 12k budget, proven=20 with a full ruled_out
    # already started evicting facts — so raising what the session remembers
    # was impossible without shrinking what it knows.
    #
    # Splitting the two lets the STORE cap govern memory and the RENDER budget
    # govern the prompt. Newest kept, because a later proof supersedes an
    # earlier one; `ruled_out` is never trimmed here — it is a prohibition, and
    # dropping one silently re-enables a known-failing approach (§17.940).
    proven_omitted = 0
    if proven_budget is not None and proven:
        kept: list[str] = []
        used = 0
        for entry in reversed(proven):
            cost = len(entry) + 3  # "- " prefix + newline
            if used + cost > proven_budget and kept:
                break
            kept.append(entry)
            used += cost
        kept.reverse()
        proven_omitted = len(proven) - len(kept)
        proven = kept
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
            + (f"\n(… {proven_omitted} older proven method(s) omitted to fit the "
               "memory budget — newest kept)" if proven_omitted else "")
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


# §17.1018 — a model/part number the operator has NAMED, next to the kind of
# thing it is. Shape: letters AND digits, no dots (excludes IPs and versions
# like "22.04"), adjacent to a hardware noun in the same note. Measured on the
# live session's 44 notes: 5 model-shaped tokens overall, of which the
# hardware-adjacency test keeps exactly the two real ones (SAX1V1K, ES2251) and
# drops `apt-secure` and `RestartSec`.
_MODEL_TOKEN_RE = re.compile(
    r"\b(?=[A-Za-z0-9-]{4,24}\b)(?=[^\s]*[A-Za-z]{2})(?=[^\s]*\d)"
    r"[A-Za-z][A-Za-z0-9-]{3,23}\b")
_HARDWARE_NOUN_RE = re.compile(
    r"\b(router|modem|switch|gateway|motherboard|mainboard|server|chassis|gpu|"
    r"graphics\s+card|nic|adapter|controller|enclosure|drive|ssd|hdd|cpu|"
    r"processor|access\s+point|appliance)\b", re.IGNORECASE)


def hardware_identifiers(operator_notes: list | None) -> list[str]:
    """§17.1018 — the exact models the operator has stated, for use in searches.

    The live failure: asked where port forwarding lives, the engine answered
    with generic advice ("try 192.168.1.1, look for Advanced or NAT, default
    admin/admin"). The operator's router is a **Spectrum SAX1V1K**, and that
    string exists in exactly ONE place in the session — the NOTES. Research
    grounding is built from `environment` (facts, profile, substitutions,
    playbook) and has never read notes, so the single token that makes the
    question answerable never reached a query.

    Returns [] when nothing qualifies, so callers append nothing.
    """
    out: list[str] = []
    for n in (operator_notes or []):
        text_ = (n.get("text") if isinstance(n, dict) else str(n)) or ""
        if not _HARDWARE_NOUN_RE.search(text_):
            continue
        for tok in _MODEL_TOKEN_RE.findall(text_):
            if tok not in out:
                out.append(tok)
    return out[:8]


def render_research_grounding(environment: dict | None,
                              operator_notes: list | None = None) -> str:
    """§17.975 — what a research QUERY needs to know about this system.

    Two gaps, both verified against the live session before writing this.

    **The playbook never reached any query.** `render_environment_block` — the
    grounding every guide and decision prepass passes — carries the §17.709
    facts and stops there. The playbook's `ruled_out` is described in its own
    code as a BINDING prohibition and is rendered into the model's prompt by
    §17.751, but the component that decides WHAT TO LOOK UP never saw it. So
    research could, and did, keep fetching material for approaches this system
    had already proven wouldn't work.

    **The fix path had no grounding at all.** §17.771 gave the guide/decision
    prepass an environment block and §17.854 restored it on the stream path;
    `generate_fix` was never included. Every troubleshooting query across
    T31–T35 was generated without knowing the operator is on a Proxmox host
    running Debian 12 containers — the one path where being system-specific
    matters most, because it only runs when something is already wrong.

    Framed for query generation rather than for narration: ruled-out approaches
    are things NOT to search for, proven ones are things to build on.
    """
    parts: list[str] = []
    base = (render_environment_block(environment) or "").strip()
    if base:
        parts.append(base)
    pb = (environment or {}).get("playbook")
    pb = pb if isinstance(pb, dict) else {}
    ruled = [str(r).strip() for r in (pb.get("ruled_out") or []) if str(r).strip()]
    proven = [str(p).strip() for p in (pb.get("proven") or []) if str(p).strip()]
    if ruled:
        parts.append(
            "### Approaches already RULED OUT on this system — do NOT search "
            "for these, or for variations of them:\n"
            + "\n".join(f"- {r[:200]}" for r in ruled[-8:]))
    if proven:
        parts.append(
            "### Methods already PROVEN to work on this system — prefer "
            "queries that build on these over queries about alternatives:\n"
            + "\n".join(f"- {p[:200]}" for p in proven[-6:]))
    # §17.1018 — the operator's own hardware, which lives in NOTES and has never
    # reached a query. Named models are the highest-value term a search can
    # carry: "SAX1V1K port forwarding" answers a question that "spectrum router
    # port forwarding" does not.
    models = hardware_identifiers(operator_notes)
    if models:
        parts.append(
            "### Hardware the operator has named — search the EXACT model, not "
            "a generic category:\n" + "\n".join(f"- {m}" for m in models))

    return "\n\n".join(parts).strip()
