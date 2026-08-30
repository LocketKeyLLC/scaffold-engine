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
    return {
        "profile": env.get("profile") or "",
        "substitutions": env.get("substitutions") or {},
        # §17.709 — durable observed facts about the operator's system.
        "facts": facts if isinstance(facts, list) else [],
        # §17.881b — the session playbook MUST round-trip through this
        # deserializer: set_environment writes the WHOLE env dict back, so a
        # key dropped here is erased by the very next fact fold (live: T14's
        # proven servarr pattern derived, then clobbered by T13's write
        # seconds later — only the last step's entries survived).
        "playbook": playbook if isinstance(playbook, dict) else {},
    }


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
        current["facts"] = existing[-int(_s.assist_facts_max):]
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
            pb[key] = cur[-int(_s.assist_playbook_max):]
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
