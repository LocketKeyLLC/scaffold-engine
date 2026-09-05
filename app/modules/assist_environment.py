"""The assist session's environment concern — extracted from assist_agent.py.

§17.856 (audit "assist decomposition") — a cohesive, self-contained slice: the
metadata→environment accessors (`get_environment`/`set_environment` + the two pure
parse helpers) and the deterministic execution-context monitor (§17.701/703/716 —
detect a pasted `user@host` shell prompt and keep `metadata.environment.profile`
in sync). Depends only on stdlib + sqlalchemy + settings, with NO back-reference
into the rest of assist_agent, so it lifts out without a cycle. Every name is
re-exported from assist_agent (`# noqa: F401`) so assist_agent.<NAME>, the wide
internal use of `_environment_from_metadata`, and the tests keep resolving.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings

# Shared logger name with assist_agent so log lines (and any caplog assertions)
# read identically before and after the move.
logger = logging.getLogger("scaffold.assist")


# §17.939 — WHICH SUBJECT is this fact about?
#
# The ledger is a flat list with a global cap, so eviction was purely "oldest
# first" (§17.920 carved out corrections, nothing else). That is fine while a
# session works one thing, and wrong the moment it works several: the operator
# spent days on VM 110, forty VM-106/VM-110 facts filled the ledger, and EVERY
# media-stack fact — the Radarr/Prowlarr/Sonarr addresses, the container ids —
# was evicted. Returning to that step a week later, the engine had no context
# for it at all and the addresses survived only because they happened to still
# be in the transcript window.
#
# Bucketing by subject lets the cap be spent FAIRLY: a subject with one fact
# keeps it while a subject with twenty gets trimmed. Deliberately coarse and
# deterministic — resource ids and hosts are what these facts are actually
# about, and a wrong bucket costs a little fairness, never correctness.
_SUBJ_VM_RE = __import__("re").compile(r"(?<![\w/])VM\s+(\d{2,5})\b")
_SUBJ_CT_RE = __import__("re").compile(
    r"(?<![\w/])(?:LXC\s+)?(?:container|CT)\s+(\d{2,5})\b", __import__("re").I)
_SUBJ_IP_RE = __import__("re").compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _fact_subject(fact: str) -> str:
    """A stable bucket key for one fact. Most specific wins; `general` is the
    catch-all for facts that name no resource (host-wide observations)."""
    s = str(fact or "")
    m = _SUBJ_VM_RE.search(s)
    if m:
        return f"vm:{m.group(1)}"
    m = _SUBJ_CT_RE.search(s)
    if m:
        return f"ct:{m.group(1)}"
    m = _SUBJ_IP_RE.search(s)
    if m:
        return f"host:{m.group(1)}"
    return "general"


def _fair_share_keep(facts: list, room: int) -> list:
    """Trim `facts` to `room` entries WITHOUT wiping out any one subject.

    Repeatedly drops the oldest fact from the currently-largest bucket, so the
    budget is spent across subjects instead of on whichever one the operator
    happened to touch most recently. Original order is preserved in the result.
    """
    if room <= 0:
        return []
    if len(facts) <= room:
        return list(facts)
    buckets: dict[str, list] = {}
    for f in facts:
        buckets.setdefault(_fact_subject(f), []).append(f)
    total = len(facts)
    while total > room:
        # largest bucket; ties broken by the OLDEST head, so eviction stays
        # deterministic rather than dict-order dependent.
        key = max(buckets, key=lambda k: (len(buckets[k]), -facts.index(buckets[k][0])))
        buckets[key].pop(0)
        if not buckets[key]:
            del buckets[key]
        total -= 1
    kept = {id(f) for b in buckets.values() for f in b}
    return [f for f in facts if id(f) in kept]


def _environment_from_metadata(metadata: Any) -> dict:
    """Pull the `environment` sub-object out of a session's metadata JSONB.

    Tolerates None / str (asyncpg usually hands back a dict for jsonb, but a
    string body is decoded defensively) and always returns a dict with the
    `profile`/`substitutions` shape so callers don't branch.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    env = (metadata or {}).get("environment") if isinstance(metadata, dict) else None
    if not isinstance(env, dict):
        return {"profile": "", "substitutions": {}, "facts": []}
    facts = env.get("facts")
    playbook = env.get("playbook")
    by_node = env.get("substitutions_by_node")
    return {
        "profile": env.get("profile") or "",
        "substitutions": env.get("substitutions") or {},
        # §17.892 — NODE-SCOPED auto-learned/auto-pinned values. Global
        # `substitutions` are the operator's explicit pins (verbatim
        # everywhere, §17.850); auto-pins live here, keyed by the node they
        # were learned on, and apply only when regenerating THAT step. The
        # live incident: HOSTNAME=DarthSidious auto-pinned during the HP
        # switch step deterministically named the PalWorld VM after the
        # switch. §17.881b — MUST round-trip here or the next env write
        # erases it.
        "substitutions_by_node": by_node if isinstance(by_node, dict) else {},
        # §17.893 — values the operator has explicitly ruled out for new use
        # ([{value, reason}]); enforced deterministically on guide/fix output.
        # §17.881b — must round-trip or the next env write erases it.
        "banned_values": env.get("banned_values") if isinstance(env.get("banned_values"), list) else [],
        # §17.709 — durable observed facts about the operator's system.
        "facts": facts if isinstance(facts, list) else [],
        # §17.913 — tools the operator's shell has PROVEN it does not have
        # ([{tool, host}]). Live: the engine emitted `sudo lvextend ...` to an
        # operator whose profile says "runs commands as root@pve"; PVE is Debian
        # minimal with no sudo, so it died with `sudo: command not found` — and
        # the next fix prescribed `qm config 106` instead of noticing.
        # §17.881b — MUST round-trip here or the next env write erases it.
        "missing_tools": env.get("missing_tools") if isinstance(env.get("missing_tools"), list) else [],
        # §17.914 — STRUCTURED resource state parsed from the operator's own
        # command output. The engine asked for `qm config 106` 21 times on the
        # live session because there was nowhere to keep the 6 answers it got.
        # §17.881b — MUST round-trip here.
        "system_state": env.get("system_state") if isinstance(env.get("system_state"), dict) else {},
        # §17.881b — the session playbook MUST round-trip through this
        # deserializer: set_environment writes the WHOLE env dict back, so a
        # key dropped here is erased by the very next fact fold (live: T14's
        # proven servarr pattern derived, then clobbered by T13's write
        # seconds later — only the last step's entries survived).
        "playbook": playbook if isinstance(playbook, dict) else {},
    }


# §17.920 — a fact that records what does NOT exist / is NOT available. These
# prevent a repeated wrong attempt every time they are read, so they must not be
# the first thing a FIFO cap discards.
_CORRECTION_FACT_RE = __import__("re").compile(
    r"\b(?:there\s+is\s+no|there\s+are\s+no|no\s+option|not\s+available|"
    r"does\s+not\s+exist|doesn'?t\s+exist|is\s+not\s+installed|"
    r"not\s+installed|cannot\s+be|can'?t\s+be|is\s+not\s+supported|"
    r"never\s+use|do\s+not\s+use|don'?t\s+use|must\s+not|reserved\s+for|"
    r"command\s+not\s+found|no\s+such)\b",
    __import__("re").IGNORECASE,
)


_VERBOSITY_LEVELS = ("terse", "normal", "detailed")


def _verbosity_from_metadata(metadata: Any) -> str:
    """§17.499 — the session's walkthrough verbosity (default 'normal')."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    v = (metadata or {}).get("verbosity") if isinstance(metadata, dict) else None
    return v if v in _VERBOSITY_LEVELS else "normal"


async def get_environment(*, session_id: str, db) -> Optional[dict]:
    """Return the session's environment profile + substitutions + verbosity. None if no session."""
    sess = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return None
    env = _environment_from_metadata(sess.get("metadata"))
    env["verbosity"] = _verbosity_from_metadata(sess.get("metadata"))
    return env


async def set_environment(
    *,
    session_id: str,
    profile: str | None = None,
    substitutions: dict | None = None,
    substitutions_by_node: dict | None = None,
    banned_values: list | None = None,
    missing_tools: list | None = None,
    system_state: dict | None = None,
    verbosity: str | None = None,
    facts: list[str] | None = None,
    retract_facts: list[str] | None = None,
    playbook_proven: list[str] | None = None,
    playbook_ruled_out: list[str] | None = None,
    db,
) -> dict:
    """Merge environment facts into `assist_sessions.metadata`.

    `profile` replaces the free-text profile when provided. `substitutions`
    are merged key-by-key (so `/assist env KEY=value` adds one without
    clobbering the rest). `verbosity` (§17.499) sets metadata.verbosity.
    `facts` (§17.709) are APPENDED to the durable facts ledger, de-duplicated
    (case-insensitive) against what's there, oldest-dropped-first to the
    `assist_facts_max` cap. `retract_facts` (§17.725) removes ledger entries a
    new observation directly contradicts — normalized exact match only, applied
    BEFORE the new facts fold in (the raw assist_turns transcript keeps the
    retracted text, so nothing is destroyed). Read-modify-write under the row
    so we never drop other `metadata` keys.
    """
    if verbosity is not None and verbosity not in _VERBOSITY_LEVELS:
        raise ValueError(f"verbosity must be one of {_VERBOSITY_LEVELS}, got {verbosity!r}")
    sess = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    current = _environment_from_metadata(sess.get("metadata"))
    if profile is not None:
        current["profile"] = profile
    if substitutions:
        merged = dict(current.get("substitutions") or {})
        merged.update(substitutions)
        # §17.850 — an empty/None value REMOVES the key ("/assist env KEY=" and
        # the SPA editor's ✕ both clear a pin this way); merge otherwise.
        current["substitutions"] = {
            k: v for k, v in merged.items() if v is not None and str(v).strip() != ""
        }
    if substitutions_by_node:
        # §17.892 — node-scoped auto-pins: merge per node per key; an empty
        # value deletes the key; an emptied node map is dropped.
        merged_bn = {k: dict(v) for k, v in (current.get("substitutions_by_node") or {}).items()
                     if isinstance(v, dict)}
        for nk, kv in substitutions_by_node.items():
            if not isinstance(kv, dict):
                continue
            node_map = merged_bn.setdefault(str(nk), {})
            node_map.update(kv)
            merged_bn[str(nk)] = {
                k: v for k, v in node_map.items()
                if v is not None and str(v).strip() != ""
            }
        current["substitutions_by_node"] = {k: v for k, v in merged_bn.items() if v}
    if banned_values:
        # §17.893 — merge by value (case-insensitive); a newer reason for the
        # same value replaces the old entry; capped like the other ledgers.
        cur_bv = [b for b in (current.get("banned_values") or [])
                  if isinstance(b, dict) and str(b.get("value") or "").strip()]
        by_val = {str(b["value"]).strip().lower(): b for b in cur_bv}
        for b in banned_values:
            if not isinstance(b, dict):
                continue
            v = str(b.get("value") or "").strip()
            if v:
                by_val[v.lower()] = {"value": v, "reason": str(b.get("reason") or "").strip()}
        current["banned_values"] = list(by_val.values())[-30:]
    if missing_tools:
        # §17.913 — merge by (tool, host); newest wins. Capped like the others.
        cur_mt = [m for m in (current.get("missing_tools") or [])
                  if isinstance(m, dict) and str(m.get("tool") or "").strip()]
        by_key = {(str(m["tool"]).strip().lower(),
                   str(m.get("host") or "").strip().lower()): m for m in cur_mt}
        for m in missing_tools:
            if not isinstance(m, dict):
                continue
            t = str(m.get("tool") or "").strip()
            if t:
                h = str(m.get("host") or "").strip()
                by_key[(t.lower(), h.lower())] = {"tool": t, "host": h}
        current["missing_tools"] = list(by_key.values())[-30:]
    if system_state:
        # §17.914 — newest observation wins per resource; others survive.
        from app.modules.assist_state import merge_system_state
        current["system_state"] = merge_system_state(
            current.get("system_state"), system_state)
    if retract_facts:
        # §17.725 — retract contradicted facts BEFORE folding the new ones in.
        gone = {str(r).strip().lower() for r in retract_facts if str(r).strip()}
        existing = list(current.get("facts") or [])
        kept = [f for f in existing if str(f).strip().lower() not in gone]
        if len(kept) != len(existing):
            removed = [f for f in existing if str(f).strip().lower() in gone]
            logger.info(
                "assist_facts_retracted session_id=%s n=%d retracted=%r",
                session_id, len(removed), removed,
            )
            current["facts"] = kept
    if facts:
        from app.config import settings as _s
        existing = list(current.get("facts") or [])
        seen = {str(f).strip().lower() for f in existing}
        for f in facts:
            t = str(f).strip()
            if t and t.lower() not in seen:
                existing.append(t)
                seen.add(t.lower())
        # Cap: keep the most recent (oldest drop first).
        # §17.920 — NEGATIVE KNOWLEDGE SURVIVES THE CAP. Plain FIFO discards
        # the most valuable facts first. Live (session 613dd1df): the operator
        # corrected the engine at turn 1417 — "There is no option to uncheck for
        # the security update" — the scribe recorded it correctly, the ledger
        # was at its 40-fact cap, and newer routine observations evicted it.
        # The engine then went on telling them to uncheck that box, which is
        # exactly the loop the correction existed to stop.
        #
        # A fact saying something does NOT exist / is NOT available prevents a
        # repeated wrong attempt every time it is read; a routine observation
        # usually restates what a command would show anyway. When the cap bites,
        # drop routine facts first and keep corrections, newest-last order
        # preserved within each class.
        cap = int(_s.assist_facts_max)
        if len(existing) > cap:
            keep_flags = [bool(_CORRECTION_FACT_RE.search(str(f))) for f in existing]
            corrections = [f for f, k in zip(existing, keep_flags) if k]
            routine = [f for f, k in zip(existing, keep_flags) if not k]
            # corrections are capped too — half the budget at most, newest kept
            corr_keep = corrections[-max(1, cap // 2):]
            room = cap - len(corr_keep)
            # §17.939 — spend the remaining budget FAIRLY across subjects
            # instead of newest-first. Newest-first meant whichever resource the
            # operator touched most recently owned the entire ledger, and every
            # other subject's context was gone when they came back to it.
            kept = set(map(id, corr_keep)) | set(map(id, _fair_share_keep(routine, room)))
            trimmed = [f for f in existing if id(f) in kept]
            if len(corrections) > len(corr_keep) or len(routine) > max(0, room):
                logger.info(
                    "assist_facts_trimmed session_id=%s kept=%d of %d "
                    "(corrections kept=%d, subjects kept=%d of %d)",
                    session_id, len(trimmed), len(existing), len(corr_keep),
                    len({_fact_subject(f) for f in trimmed}),
                    len({_fact_subject(f) for f in existing}))
            current["facts"] = trimmed[-cap:]
        else:
            current["facts"] = existing
    if playbook_proven or playbook_ruled_out:
        # §17.881 — the session playbook: methods PROVEN to work on this system
        # this session, and approaches that FAILED here. Merged like facts
        # (dedupe case-insensitive, newest kept under the cap) into
        # ``environment.playbook`` = {"proven": [...], "ruled_out": [...]}.
        from app.config import settings as _s
        pb = dict(current.get("playbook") or {})
        for key, adds in (("proven", playbook_proven), ("ruled_out", playbook_ruled_out)):
            if not adds:
                continue
            cur = [str(x).strip() for x in (pb.get(key) or []) if str(x).strip()]
            seen_pb = {x.lower() for x in cur}
            for a in adds:
                t = str(a).strip()
                if t and t.lower() not in seen_pb:
                    cur.append(t)
                    seen_pb.add(t.lower())
            # §17.940 — `ruled_out` is a BINDING prohibition, not a
            # convenience; it gets its own, larger budget (see config).
            _pb_cap = (_s.assist_playbook_ruled_out_max if key == "ruled_out"
                       else _s.assist_playbook_max)
            pb[key] = cur[-int(_pb_cap):]
        current["playbook"] = pb
    # Single jsonb merge patch — environment always, verbosity when given.
    patch: dict[str, Any] = {"environment": current}
    if verbosity is not None:
        patch["verbosity"] = verbosity
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb),
                   updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id, "patch": json.dumps(patch)},
    )
    await db.commit()
    current["verbosity"] = verbosity or _verbosity_from_metadata(sess.get("metadata"))
    return current


# §17.701 — a pasted interactive-shell prompt (e.g. `root@pve:~#`) reveals the
# operator's ACTUAL execution context: one interactive shell on a named host
# (typically the Proxmox web console). Anchored on a leading prompt line so it
# doesn't fire on prose that merely mentions an email-like `user@host`.
_SHELL_PROMPT_RE = re.compile(r"(?m)^\s*([A-Za-z_][\w.-]*)@([\w.-]+):[^\n#$]*[#$]")

# §17.703 — sentinel prefix marking a profile string that WE auto-captured (vs.
# one the operator set explicitly via `/assist env`). Change-detection only ever
# replaces an auto-captured profile; an operator's explicit profile is sacred.
_EXEC_CTX_SENTINEL = "Operator runs commands as "


def _detect_shell_context(evidence: str) -> Optional[tuple[str, str]]:
    """§17.701 — infer the operator's execution context from a pasted shell
    prompt. Returns ``(user, host)`` when a prompt line is present, else None.
    Anchored on a leading prompt line so it doesn't fire on prose that merely
    mentions an email-like ``user@host``."""
    m = _SHELL_PROMPT_RE.search(evidence or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def _exec_context_profile(user: str, host: str) -> str:
    """Build the single-interactive-shell profile string for ``user@host``.

    Recording it makes later guidance single-shell-safe with the REAL host/user
    (reinforcing §17.700's runbook rule at the per-session level): the model is
    told the operator pastes a block into ONE shell on that host, not a
    multi-terminal SSH setup."""
    return (
        f"{_EXEC_CTX_SENTINEL}{user}@{host} in ONE interactive shell "
        f"(the host's console / a single SSH session), pasting a command block "
        f"and pasting the output back — NOT a multi-terminal setup. Keep every "
        f"step runnable in that single shell."
    )


# §17.716 — validate a (user, host) from ANY source (deterministic paste OR the
# per-turn LLM) before it can touch the profile, so a garbled host never lands.
_CTX_USER_RE = re.compile(r"^[A-Za-z_][\w.-]*$")
_CTX_HOST_RE = re.compile(r"^[\w][\w.-]*$")


async def _apply_shell_context(
    *, session_id: str, user: str, host: str, db, source: str = "paste",
) -> Optional[dict]:
    """§17.716 — apply a detected ``user@host`` to ``metadata.environment.profile``
    under the §17.703 retention rules, regardless of how it was detected (a
    pasted prompt line, or an explicit prose statement the per-turn LLM read).
    Centralizes the write so every source obeys the same rules:
      • profile empty                        → capture it.
      • prior auto-capture (``_EXEC_CTX_SENTINEL``), different host → switch it.
      • profile already names this ``user@host``                   → no-op.
      • operator-set (non-sentinel) profile   → leave it (explicit outranks
        inferred; mirrors :func:`learn_from_submit`).
    Returns ``{user, host, changed}`` on a write, else None."""
    user, host = (user or "").strip(), (host or "").strip()
    if not (_CTX_USER_RE.match(user) and _CTX_HOST_RE.match(host)):
        return None
    env = await get_environment(session_id=session_id, db=db) or {}
    current = (env.get("profile") or "").strip()
    marker = f"{_EXEC_CTX_SENTINEL}{user}@{host} "
    if marker in current:
        return None  # already recorded this exact context
    if current and not current.startswith(_EXEC_CTX_SENTINEL):
        return None  # respect an operator-set profile
    changed = bool(current)  # a prior auto-capture named a different host
    await set_environment(
        session_id=session_id, profile=_exec_context_profile(user, host), db=db,
    )
    logger.info(
        "assist_%s_shell_context session_id=%s ctx=%s@%s source=%s",
        "switched" if changed else "captured", session_id, user, host, source,
    )
    return {"user": user, "host": host, "changed": changed}


async def capture_execution_context(
    *, session_id: str, evidence: str, db, source: str = "paste",
) -> Optional[dict]:
    """§17.703 — the deterministic execution-environment monitor.

    Detects the operator's execution context (``user@host`` in ONE interactive
    shell) from a pasted prompt in their evidence and keeps
    ``metadata.environment.profile`` in sync with it. Runs on EVERY submit —
    decoupled from the substitution-learning valve and from the success verdict
    — so it captures on a failed/error paste too (that's still the operator's
    real shell). §17.716 — ALSO runs per-message (see ``derive_turn_memory``) so
    a prompt line pasted in a non-submit message (a question / fix) is not
    missed. Returns ``{user, host, changed}`` when it wrote a profile, else None.
    Fail-soft: any error returns None and never disturbs the caller."""
    try:
        detected = _detect_shell_context(evidence)
        if not detected:
            return None
        user, host = detected
        return await _apply_shell_context(
            session_id=session_id, user=user, host=host, db=db, source=source,
        )
    except Exception as e:  # noqa: BLE001 — context capture must never break submit
        logger.debug(
            "shell_context_capture_failed session_id=%s err=%r", session_id, e,
        )
        return None
