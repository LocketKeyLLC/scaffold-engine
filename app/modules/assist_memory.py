"""Assist session memory + facts — extracted from assist_agent.py.

§17.856 (audit "assist decomposition") — the durable-memory subsystem: derive
per-turn memory (derive_turn_memory + bg/schedule/drain), consolidate the facts
ledger (consolidate_session_facts + bg/schedule/drain), capture/learn facts from a
submit (capture_session_facts / learn_from_submit / sweep_superseded_facts /
_sibling_facts / _durable_facts_for_session), and the submit-grounding check. Calls
the assist_guide distill/classify LLM helpers (module import) and — for the two
functions that touch operator notes — record_note / _coerce_notes via a patch-safe
late import (notes cluster stays in assist_agent for now). Every name re-exported
from assist_agent (incl. _NOTE_KINDS, which the staying record_note uses).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.modules import assist_guide

logger = logging.getLogger("scaffold.assist")


async def _durable_facts_for_session(*, session_id: str, metadata, db) -> list[str]:
    """§17.759 — the DURABLE, cross-cutting infrastructure facts of a session
    (shared host / network / storage / hardware), cached in
    ``metadata.environment`` (``durable_facts`` + ``durable_facts_n`` watermark) and
    recomputed only when the fact count changes — so cross-component sharing filters
    to a clean shared baseline with NO classifier call at generation time (cache
    hit). On a classifier FAILURE, falls back to ALL facts (the §17.757 behavior) so
    sharing degrades gracefully rather than going empty."""
    from app.modules import assist_guide
    # Parse the RAW environment (not _environment_from_metadata, which strips the
    # durable_facts cache keys) so the cache read works.
    md = metadata
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (ValueError, TypeError):
            md = {}
    env = (md or {}).get("environment") if isinstance(md, dict) else {}
    env = env if isinstance(env, dict) else {}
    facts = [str(f).strip() for f in (env.get("facts") or []) if str(f).strip()]
    if not facts:
        return []
    cached = env.get("durable_facts")
    if isinstance(cached, list) and env.get("durable_facts_n") == len(facts):
        return [str(f).strip() for f in cached if str(f).strip()]
    idxs = await assist_guide.classify_durable_facts(facts=facts)
    if idxs is None:            # classifier unavailable → share all (fail-soft)
        return facts
    durable = [facts[i] for i in idxs]
    try:  # cache back (best-effort; sharing must not break on a cache write)
        await db.execute(
            text("UPDATE assist_sessions SET metadata = jsonb_set(jsonb_set("
                 "COALESCE(metadata, '{}'::jsonb),"
                 "'{environment,durable_facts}', CAST(:df AS jsonb), true),"
                 "'{environment,durable_facts_n}', CAST(:n AS jsonb), true) "
                 "WHERE id = :sid"),
            {"df": json.dumps(durable), "n": json.dumps(len(facts)), "sid": session_id},
        )
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("assist_durable_facts_cache_failed sid=%s err=%r", session_id, e)
    return durable


async def _sibling_facts(*, job_id: str, db) -> list[str]:
    """§17.757 — facts observed on OTHER components of the same umbrella project.
    A decomposed homelab shares one host / network / storage, so a durable fact a
    sibling component learned (host NAT, the bridge, the ZFS pool, hardware) is
    ground truth here too. Returns the sibling sessions' facts (same
    ``parent_job_id``, excluding this job), deduped case-insensitively and capped.
    §17.759 — with ``assist_cross_component_durable_only`` on, each sibling
    contributes only its DURABLE infrastructure subset (cached), not transient or
    component-specific noise. Empty for a standalone job or when the valve is off."""
    from app.modules.assist_agent import _environment_from_metadata  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    if not settings.assist_cross_component_facts_enabled:
        return []
    try:
        parent = (await db.execute(
            text("SELECT parent_job_id FROM jobs WHERE id = :jid"), {"jid": job_id},
        )).scalar()
        if not parent:
            return []
        rows = (await db.execute(
            text("SELECT s.id, s.metadata FROM assist_sessions s JOIN jobs j ON j.id = s.job_id "
                 "WHERE j.parent_job_id = :p AND s.job_id <> :jid"),
            {"p": str(parent), "jid": job_id},
        )).mappings().all()
    except Exception as e:  # noqa: BLE001 — sharing must never break the turn
        logger.debug("assist_sibling_facts_failed job_id=%s err=%r", job_id, e)
        return []
    durable_only = settings.assist_cross_component_durable_only
    cap = int(settings.assist_cross_component_facts_cap)
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        if durable_only:
            facts = await _durable_facts_for_session(
                session_id=str(r["id"]), metadata=r.get("metadata"), db=db)
        else:
            facts = [str(f).strip()
                     for f in (_environment_from_metadata(r.get("metadata")).get("facts") or [])]
        for f in facts:
            f = str(f).strip()
            k = f.lower()
            if f and k not in seen:
                seen.add(k)
                out.append(f)
                if len(out) >= cap:
                    return out
    return out


async def learn_from_submit(
    *, session_id: str, node_key: str, evidence: str, db,
) -> dict:
    """§17.490 — fold concrete values from a submit into the session environment.

    Reads the step's cached walkthrough; if it emitted ``<PLACEHOLDER>`` slots,
    extracts the values the operator actually used from their evidence and
    merges the **new** ones into ``metadata.environment.substitutions`` (never
    overwriting an operator-set or previously-learned value). Returns the dict
    of newly-learned values (for the caller to surface). Best-effort: any
    failure returns ``{}`` and never disturbs the submit.

    §17.701 — also captures the operator's execution context (single interactive
    shell) from a pasted prompt, once, when unset — independent of placeholder
    substitutions.
    """
    from app.modules.assist_agent import capture_execution_context, get_environment, set_environment  # §17.856 re-exports (patch-safe deferred)
    from app.modules import assist_guide

    # §17.701/703 — keep the operator's execution context (single interactive
    # shell, `user@host`) in sync. Delegated to the standalone monitor, which is
    # idempotent and fail-soft. The router ALSO calls it unconditionally on every
    # submit (§17.703); this call keeps CLI/other callers of learn_from_submit
    # covered without a second code path.
    await capture_execution_context(
        session_id=session_id, evidence=evidence, db=db,
    )

    cached = await assist_guide.read_cached_guidance(
        session_id=session_id, node_key=node_key, db=db,
    )
    if not cached or not cached.get("guidance"):
        return {}
    extracted = await assist_guide.extract_substitutions(
        guidance_text=cached["guidance"], evidence=evidence,
    )
    if not extracted:
        return {}
    current = await get_environment(session_id=session_id, db=db) or {}
    existing = current.get("substitutions") or {}
    # Only-add-new: an operator-set or already-learned key wins over a re-read.
    new = {k: v for k, v in extracted.items() if k not in existing}
    if not new:
        return {}
    await set_environment(session_id=session_id, substitutions=new, db=db)
    logger.info(
        "assist_learned_substitutions session_id=%s node_key=%s keys=%s",
        session_id, node_key, ",".join(new.keys()),
    )
    return new


async def capture_session_facts(
    *, session_id: str, node_key: str, evidence: str, db,
) -> list[str]:
    """§17.709 — distill durable facts about the operator's ACTUAL system from a
    submit's evidence and append them to the session facts ledger
    (``metadata.environment.facts``), which renders into EVERY later step's
    guidance + decision-deliberation context.

    This is the retention layer substitution-learning misses: an audit /
    inventory / gather step carries real system state but has no
    ``<PLACEHOLDER>`` tokens, so ``learn_from_submit`` retained nothing and later
    decisions fabricated assumptions ("Assumption: Fresh Proxmox VE server"). Now
    the facts survive independently of placeholders and of digest truncation.
    Best-effort: any failure returns ``[]`` and never disturbs the submit.
    """
    from app.modules.assist_agent import get_environment, set_environment  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_guide

    if not settings.assist_capture_facts_enabled or not (evidence or "").strip():
        return []
    try:
        row = (await db.execute(
            text("""
                SELECT d.title, d.prompt_template
                  FROM assist_steps s
                  JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
                 WHERE s.session_id = :sid AND s.node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().first()
        # §17.725 — show the distiller the current ledger so a contradicted fact
        # can be echoed for retraction (valve-gated at fold time below).
        known_facts: list[str] = []
        if settings.assist_unified_memory_enabled and settings.assist_umem_supersede:
            env_now = await get_environment(session_id=session_id, db=db) or {}
            known_facts = [str(f) for f in (env_now.get("facts") or [])]
        res = await assist_guide.distill_facts(
            evidence=evidence,
            title=(row or {}).get("title") or "",
            task_prompt=(row or {}).get("prompt_template") or "",
            known_facts=known_facts or None,
        )
        facts = res.get("facts") or []
        superseded = (res.get("superseded") or []) if known_facts else []
        if not facts and not superseded:
            return []
        env_after = await set_environment(
            session_id=session_id, facts=facts,
            retract_facts=superseded or None, db=db,
        )
        logger.info(
            "assist_captured_facts session_id=%s node_key=%s n=%d retracted=%d",
            session_id, node_key, len(facts), len(superseded),
        )
        # §17.727 — a fold that pushed the ledger past the threshold triggers a
        # background consolidation pass (debounced inside).
        schedule_consolidate_facts(
            session_id=session_id, fact_count=_fact_count_of(env_after),
        )
        return facts
    except Exception as e:  # noqa: BLE001 — fact capture must never break submit
        logger.debug(
            "capture_session_facts_failed session_id=%s err=%r", session_id, e,
        )
        return []


async def check_submit_grounding(
    *, session_id: str, node_key: str, evidence: str, db,
) -> Optional[dict]:
    """§17.710c — warn-only grounding gate. Does this submit's result contradict
    what we already know about the operator's system? Reads the session
    environment (facts/provided/profile) + notes as memory, asks the grounding
    checker, and returns ``{reason}`` ONLY on a contradiction (else None) so the
    caller can surface a non-blocking warning. Gated on the master + grounding
    valves; fail-soft. Run BEFORE this submit's own facts are folded in, so the
    result can't be judged consistent with its own claims."""
    from app.modules.assist_agent import get_environment, _coerce_notes  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_guide

    if not (settings.assist_unified_memory_enabled and settings.assist_umem_grounding):
        return None
    if not (evidence or "").strip():
        return None
    try:
        env = await get_environment(session_id=session_id, db=db) or {}
        notes = None
        sess = (await db.execute(
            text("SELECT notes FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if sess:
            notes = _coerce_notes(sess.get("notes"))
        verdict = await assist_guide.check_grounding(
            evidence=evidence, environment=env, operator_notes=notes,
        )
        if verdict.get("contradicts"):
            logger.info(
                "assist_grounding_contradiction session_id=%s node_key=%s reason=%r",
                session_id, node_key, verdict.get("reason"),
            )
            return {"reason": verdict.get("reason") or ""}
        return None
    except Exception as e:  # noqa: BLE001 — never block a submit on the gate
        logger.debug(
            "check_submit_grounding_failed session_id=%s err=%r", session_id, e,
        )
        return None


# Strong refs so fire-and-forget derive tasks aren't GC'd mid-flight; tests
# await them via drain_derive_tasks(). Same pattern as assist_replan's
# _BACKGROUND_TASKS.
_DERIVE_TASKS: set = set()


# Pure control tokens carry no durable info — skip the extraction call for a
# bare "yes"/"next"/"ok" (a real plan change is always ≥2 words: "drop it",
# "use wireguard"). Cheap pre-filter only; anything with 2+ words goes through.
_TRIVIAL_TURN = {
    "yes", "no", "y", "n", "ok", "okay", "next", "skip", "done", "pause",
    "resume", "continue", "stop", "go", "sure", "yep", "yeah", "nope",
    "thanks", "thx", "ty", "cool", "great", "perfect", "confirm", "confirmed",
}


def _norm_note(text_: str) -> str:
    return " ".join((text_ or "").lower().split())


async def derive_turn_memory(
    *, session_id: str, node_key: str | None, message: str, db,
) -> dict:
    """§17.715 — the unconditional review the trigger-gated paths miss: extract
    any durable, plan-relevant memory from ONE operator message and LOG it into
    the notes/facts guidance injects. Dedup-safe (won't restate standing memory).
    Silent — does NOT surface an interactive re-plan (that stays on the §17.693
    pivot path). Gated on the master + derive valves; fail-soft (returns a
    summary dict, never raises).

    This closes the gap §17.710a left: capture was made unconditional, but the
    derive/review step was still gated on intent (skip/question≥6w pivots,
    explicit notes, submit-facts). A plan change stated in a message routed to
    ask/fix/etc. was captured raw yet never became memory that shapes later
    steps. Now every message is reviewed."""
    from app.modules.assist_agent import _apply_shell_context, _detect_shell_context, _environment_from_metadata, set_environment, record_note, _coerce_notes  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_guide

    result = {"notes_added": 0, "facts_added": 0}
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_derive):
        return result
    msg = (message or "").strip()
    if len(msg.split()) < 2 or msg.lower() in _TRIVIAL_TURN:
        return result
    try:
        sess = (await db.execute(
            text("SELECT status, notes, metadata FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return result
        existing_notes = _coerce_notes(sess.get("notes"))
        env = _environment_from_metadata(sess.get("metadata"))
        known_note_texts = [n.get("text", "") for n in existing_notes if n.get("text")]
        known_facts = [str(f) for f in (env.get("facts") or [])]
        derived = await assist_guide.distill_turn_memory(
            message=msg, known_notes=known_note_texts, known_facts=known_facts,
        )
        # §17.716 — keep the execution context (user@host) fresh from EVERY
        # message, not just submits. (a) deterministic: a prompt line pasted in a
        # non-submit message; (b) the operator saying in prose they've moved hosts
        # (what the anchored regex can't catch — the reported root@pve →
        # root@DeFruscio-HomeLab miss). Deterministic wins; both go through the
        # shared §17.703 retention rules (respect operator-set profiles).
        det = _detect_shell_context(msg)
        if det:
            await _apply_shell_context(
                session_id=session_id, user=det[0], host=det[1], db=db, source="turn",
            )
        else:
            ec = derived.get("execution_context")
            if isinstance(ec, dict) and ec.get("user") and ec.get("host"):
                await _apply_shell_context(
                    session_id=session_id, user=ec["user"], host=ec["host"],
                    db=db, source="prose",
                )
        # Dedup notes against what's already recorded (exact/substring, both
        # ways) so a restated standing decision doesn't pile up on every turn.
        seen = {_norm_note(t) for t in known_note_texts}
        for n in derived.get("notes") or []:
            cand = _norm_note(n["text"])
            if not cand or cand in seen:
                continue
            if any(cand in s or s in cand for s in seen):
                continue
            stored = await record_note(
                session_id=session_id, text_=n["text"], kind=n["kind"],
                node_key=node_key, db=db,
            )
            if stored:
                seen.add(cand)
                result["notes_added"] += 1
        # Facts: set_environment already dedups case-insensitively + caps.
        # §17.725 — retract the known facts this message directly contradicted
        # (verbatim ledger matches only), valve-gated.
        new_facts = [f for f in (derived.get("facts") or [])]
        superseded = (
            list(derived.get("superseded") or [])
            if settings.assist_umem_supersede else []
        )
        if new_facts or superseded:
            env_after = await set_environment(
                session_id=session_id, facts=new_facts,
                retract_facts=superseded or None, db=db,
            )
            result["facts_added"] = len(new_facts)
            result["facts_retracted"] = len(superseded)
            # §17.727 — background consolidation when the ledger has grown big.
            schedule_consolidate_facts(
                session_id=session_id, fact_count=_fact_count_of(env_after),
            )
        if result["notes_added"] or result["facts_added"] or superseded:
            logger.info(
                "assist_derived_turn_memory session_id=%s notes=+%d facts=+%d facts=-%d",
                session_id, result["notes_added"], result["facts_added"],
                len(superseded),
            )
    except Exception as e:  # noqa: BLE001 — the scribe must never break the turn
        logger.debug("derive_turn_memory_failed session_id=%s err=%r", session_id, e)
    return result


async def _derive_turn_memory_bg(
    *, session_id: str, node_key: str | None, message: str,
) -> None:
    """Background worker: open a fresh session (the request session is gone by
    the time this runs) and derive. Swallows every exception — a scribe hiccup
    surfaces only in logs, never as an unhandled-task warning."""
    try:
        async with async_session() as bg_db:
            await derive_turn_memory(
                session_id=session_id, node_key=node_key, message=message, db=bg_db,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("derive_turn_memory_bg_failed session_id=%s err=%r", session_id, e)


_RECENT_DERIVES: dict[tuple[str, int], float] = {}


_RECENT_DERIVE_TTL = 300.0  # seconds


def _derived_recently(session_id: str, message: str) -> bool:
    """§17.812 — True when this exact content was already scheduled for this
    session within the TTL. The same operator turn reaches the derive funnel
    twice on NL paths (the pipeline's /turn capture AND the endpoint's own
    capture, e.g. message+submit); without this guard the scribe LLM runs twice
    concurrently and can double-insert before either's notes land."""
    now = time.monotonic()
    key = (session_id, hash((message or "").strip()))
    if len(_RECENT_DERIVES) > 256:  # bound the map; entries age out lazily
        for k, ts in list(_RECENT_DERIVES.items()):
            if now - ts > _RECENT_DERIVE_TTL:
                _RECENT_DERIVES.pop(k, None)
    seen = _RECENT_DERIVES.get(key)
    _RECENT_DERIVES[key] = now
    return seen is not None and (now - seen) <= _RECENT_DERIVE_TTL


def schedule_derive_turn_memory(
    *, session_id: str, node_key: str | None, message: str,
) -> None:
    """§17.715 — fire-and-forget the per-turn derive off the request path so the
    conversation never waits on it (same posture as context_only divergence).
    No-op unless the derive valve is on. Strong ref via ``_DERIVE_TASKS`` so the
    task isn't GC'd before it finishes."""
    from app.config import settings
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_derive):
        return
    if _derived_recently(session_id, message):
        return
    task = asyncio.create_task(
        _derive_turn_memory_bg(
            session_id=session_id, node_key=node_key, message=message,
        )
    )
    _DERIVE_TASKS.add(task)
    task.add_done_callback(_DERIVE_TASKS.discard)


async def drain_derive_tasks() -> None:
    """Await all in-flight derive tasks. Tests call this between a /turn and any
    assertion on the derived notes/facts; production never waits."""
    if not _DERIVE_TASKS:
        return
    await asyncio.gather(*list(_DERIVE_TASKS), return_exceptions=True)


_CONSOLIDATE_TASKS: set = set()


def _fact_count_of(env: object) -> int:
    """Ledger size from a ``set_environment`` return — tolerant of mocks and
    malformed shapes (a bad count must never break the fold that produced it)."""
    if isinstance(env, dict):
        facts = env.get("facts")
        if isinstance(facts, list):
            return len(facts)
    return 0


# Re-consolidate only after the ledger has grown by this many facts since the
# last pass — one model call per burst of growth, not per fold.
_CONSOLIDATE_REGROW = 5


def _apply_fact_merges(current: list[str], merges: list[dict]) -> list[str]:
    """§17.727 — deterministic, lossless-by-construction application of merge
    groups to the CURRENT ledger, by VALUE (the ledger may have gained/lost
    entries while the model was thinking). Per group: members present in the
    ledger are removed and the replacement lands at the position of the group's
    NEWEST member (so §17.722's newest-kept trimming still treats fresh info as
    fresh); a group with <2 members still present is skipped (a retraction or
    cap already handled the rest); anything not in a valid group is untouched."""
    member_of: dict[str, int] = {}
    for mid, m in enumerate(merges):
        for t in m.get("replaces") or []:
            member_of[str(t).strip().lower()] = mid
    last_pos: dict[int, int] = {}
    present: dict[int, int] = {}
    for pos, f in enumerate(current):
        mid = member_of.get(str(f).strip().lower())
        if mid is not None:
            last_pos[mid] = pos
            present[mid] = present.get(mid, 0) + 1
    active = {mid for mid, n in present.items() if n >= 2}
    out: list[str] = []
    seen: set[str] = set()
    for pos, f in enumerate(current):
        mid = member_of.get(str(f).strip().lower())
        if mid is None or mid not in active:
            text_ = f
        elif pos == last_pos[mid]:
            text_ = str(merges[mid].get("text") or "").strip() or f
        else:
            continue
        key = text_.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text_)
    return out


async def consolidate_session_facts(*, session_id: str, db) -> dict:
    """§17.727 — one consolidation pass over the session's facts ledger: ask
    the model for redundant-group merges, apply them losslessly, record the
    debounce watermark. Gated on the master + consolidate valves; skips below
    the size threshold or when the ledger hasn't regrown since the last pass.
    Fail-soft — returns a summary dict, never raises."""
    from app.modules.assist_agent import _environment_from_metadata  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_guide

    result = {"before": 0, "after": 0, "merges": 0}
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_consolidate):
        return result
    try:
        sess = (await db.execute(
            text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess:
            return result
        env = _environment_from_metadata(sess.get("metadata"))
        facts = [str(f) for f in (env.get("facts") or [])]
        result["before"] = result["after"] = len(facts)
        meta = sess.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        watermark = int((meta or {}).get("facts_consolidated_n") or 0)
        if (
            len(facts) < int(settings.assist_facts_consolidate_min)
            or len(facts) < watermark + _CONSOLIDATE_REGROW
        ):
            return result
        merges = await assist_guide.consolidate_facts(facts)
        # Watermark even when nothing merged, so a ledger with no redundancy
        # isn't re-scanned on every subsequent fold.
        if merges:
            # Re-read under the row lock and apply by value — folds/retractions
            # that landed while the model was thinking survive untouched.
            locked = (await db.execute(
                text("SELECT metadata FROM assist_sessions WHERE id = :sid FOR UPDATE"),
                {"sid": session_id},
            )).mappings().first()
            cur_env = _environment_from_metadata((locked or {}).get("metadata"))
            cur = [str(f) for f in (cur_env.get("facts") or [])]
            new = _apply_fact_merges(cur, merges)
            # §17.812 — the raced no-op case (merges proposed but the ledger
            # changed under the model so nothing applies) must watermark the
            # LOCKED length, not the stale pre-model `facts` snapshot below —
            # else the debounce re-fires a model pass on every subsequent fold.
            facts = cur
            if new != cur:
                cur_env["facts"] = new
                await db.execute(
                    text("""
                        UPDATE assist_sessions
                           SET metadata = COALESCE(metadata, '{}'::jsonb)
                                          || CAST(:patch AS jsonb),
                               updated_at = NOW()
                         WHERE id = :sid
                    """),
                    {"sid": session_id, "patch": json.dumps({
                        "environment": cur_env,
                        "facts_consolidated_n": len(new),
                    })},
                )
                await db.commit()
                result["after"] = len(new)
                result["merges"] = len(merges)
                logger.info(
                    "assist_facts_consolidated session_id=%s before=%d after=%d merges=%d",
                    session_id, len(cur), len(new), len(merges),
                )
                return result
        await db.execute(
            text("""
                UPDATE assist_sessions
                   SET metadata = COALESCE(metadata, '{}'::jsonb)
                                  || CAST(:patch AS jsonb)
                 WHERE id = :sid
            """),
            {"sid": session_id,
             "patch": json.dumps({"facts_consolidated_n": len(facts)})},
        )
        await db.commit()
    except Exception as e:  # noqa: BLE001 — tidying must never break the turn
        logger.debug("consolidate_session_facts_failed session_id=%s err=%r", session_id, e)
    return result


async def _consolidate_facts_bg(*, session_id: str) -> None:
    """Background worker with its own session (mirrors ``_derive_turn_memory_bg``)."""
    try:
        async with async_session() as bg_db:
            await consolidate_session_facts(session_id=session_id, db=bg_db)
    except Exception as e:  # noqa: BLE001
        logger.debug("consolidate_facts_bg_failed session_id=%s err=%r", session_id, e)


def schedule_consolidate_facts(*, session_id: str, fact_count: int) -> None:
    """§17.727 — fire-and-forget a consolidation pass when a fold pushed the
    ledger past the threshold. The pass itself re-checks the threshold AND the
    regrowth watermark, so over-scheduling is cheap (an early return, no model
    call). No-op unless the consolidate valve is on."""
    from app.config import settings
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_consolidate):
        return
    if fact_count < int(settings.assist_facts_consolidate_min):
        return
    task = asyncio.create_task(_consolidate_facts_bg(session_id=session_id))
    _CONSOLIDATE_TASKS.add(task)
    task.add_done_callback(_CONSOLIDATE_TASKS.discard)


async def drain_consolidate_tasks() -> None:
    """Await in-flight consolidation tasks (tests only; production never waits)."""
    if not _CONSOLIDATE_TASKS:
        return
    await asyncio.gather(*list(_CONSOLIDATE_TASKS), return_exceptions=True)


# Kinds an operator-raised note can be tagged as. Free-form is coerced to
# 'note'; the classifier/pipeline pick a more specific kind when they can.
_NOTE_KINDS = ("note", "addition", "decision", "constraint", "preference")


async def sweep_superseded_facts(*, session_id: str, note_text: str, db) -> dict:
    """§17.755 — when an operator note declares a reset/rebuild (§17.714), RETRACT
    the facts that describe the abandoned system so the append-only ledger stops
    dragging dead state into every later step. §17.714 previously only DEMOTED the
    superseded facts at render time — they lingered, ate the budget, and leaked
    (e.g. the abandoned VM's guest username resurfacing in guidance). An LLM pass
    (``classify_superseded_facts``) picks the abandoned-system facts; durable host /
    network / storage / new-build facts are kept. Guardrails: valve-gated;
    fail-soft → ``{retracted: []}``; and a hard cap so a mis-firing model can never
    wipe most of the ledger. Returns the retracted facts for surfacing/logging."""
    from app.modules.assist_agent import _environment_from_metadata, set_environment  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_guide

    if not settings.assist_reset_facts_sweep_enabled:
        return {"retracted": []}
    try:
        sess = (await db.execute(
            text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        env = _environment_from_metadata((sess or {}).get("metadata"))
        facts = [str(f).strip() for f in (env.get("facts") or []) if str(f).strip()]
        if len(facts) < 3:  # nothing meaningful to sweep
            return {"retracted": []}
        idxs = await assist_guide.classify_superseded_facts(
            note_text=note_text, facts=facts)
        retract = [facts[i] for i in idxs]
        if not retract:
            return {"retracted": []}
        # Hard guardrail: a reset supersedes SOME facts, never (almost) all of them.
        # If the model wants to retract >= the cap fraction, it has misfired — skip.
        cap = max(1, int(len(facts) * settings.assist_reset_facts_sweep_max_frac))
        if len(retract) > cap:
            logger.warning(
                "assist_facts_sweep_overbroad session_id=%s want=%d/%d cap=%d — skipped",
                session_id, len(retract), len(facts), cap,
            )
            return {"retracted": [], "skipped": "overbroad"}
        await set_environment(session_id=session_id, retract_facts=retract, db=db)
        logger.info(
            "assist_facts_sweep session_id=%s retracted=%d kept=%d",
            session_id, len(retract), len(facts) - len(retract),
        )
        return {"retracted": retract}
    except Exception as e:  # noqa: BLE001 — the sweep must never break note-taking
        logger.warning("assist_facts_sweep_failed session_id=%s err=%r", session_id, e)
        return {"retracted": []}


# ── §17.881 — commit-time reconciliation + session playbook ─────────────────
#
# The "stops repeating what already failed" overhaul. Two structural gaps this
# closes, both live-hit on the HomeLab session:
#   1. The facts ledger accumulated the WHOLE troubleshooting saga as timeless
#      truth — "prowlarr is not installed in container 102" still injected as
#      OBSERVED fact hours after the successful install. Redundancy
#      consolidation (§17.727) merges duplicates; nothing retired facts the
#      committed OUTCOME contradicts.
#   2. Hard-won method lessons ("<app>.servarr.com/v1/update/... updatefile
#      works; apt.servarr.com is unreachable from these containers") lived as
#      scattered prose, ranked no higher than model priors — the very next
#      component install guessed fresh URLs from memory.
# On EVERY step commit, one structured pass over (ledger + committed evidence)
# returns: facts to retire, methods proven here, approaches ruled out here.
# Retirements go through set_environment's retract path (transcript keeps the
# raw text); the playbook persists in environment.playbook and renders as a
# BINDING block in every generation (render_session_memory).

_RECONCILE_SYSTEM = (
    "A step of a multi-step infrastructure build just COMPLETED successfully. "
    "You maintain the session's memory. Given the step's goal, the evidence of "
    "its successful completion, and the current facts ledger, call "
    "update_session_memory with:\n"
    "- retire_facts: ledger entries (echoed VERBATIM) that describe transient "
    "mid-troubleshooting states the completed outcome now CONTRADICTS (e.g. "
    "'X is not installed' after X was installed; 'no Y.service unit exists' "
    "after the unit was created). Durable infrastructure facts (hardware, "
    "networks, credentials, versions) are NOT retired. When unsure, keep the "
    "fact.\n"
    "- proven_methods: concrete methods/commands/URL patterns this step PROVED "
    "work on THIS system, stated so a later step can reuse them (e.g. "
    "'Servarr apps install via https://<app>.servarr.com/v1/update/master/"
    "updatefile?os=linux&runtime=netcore&arch=x64 tarball — worked for "
    "Prowlarr'). Only what the evidence demonstrates; include the general "
    "pattern when it clearly generalizes.\n"
    "- ruled_out_approaches: approaches tried during this step that FAILED or "
    "are unreachable/broken on this system, with the reason (e.g. "
    "'apt.servarr.com apt repo — does not resolve from the LXC containers'). "
    "Only what was actually attempted and failed; not hypotheticals."
)

_UPDATE_MEMORY_TOOL = None  # built lazily — model_router import stays deferred


def _update_memory_tool():
    global _UPDATE_MEMORY_TOOL
    if _UPDATE_MEMORY_TOOL is None:
        from app import model_router
        _UPDATE_MEMORY_TOOL = model_router.Tool(
            name="update_session_memory",
            description=(
                "Reconcile session memory with a step's successful completion: "
                "retire contradicted facts, record proven methods and "
                "ruled-out approaches."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "retire_facts": {"type": "array", "items": {"type": "string"}},
                    "proven_methods": {"type": "array", "items": {"type": "string"}},
                    "ruled_out_approaches": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["retire_facts", "proven_methods", "ruled_out_approaches"],
            },
        )
    return _UPDATE_MEMORY_TOOL


async def reconcile_on_commit(
    *, session_id: str, node_key: str, evidence: str, db,
) -> dict:
    """§17.881 — one reconciliation pass after a step commits. Fail-soft."""
    from app import model_router
    from app.modules.assist_agent import get_environment, set_environment
    from app.utils.tool_call_args import read_tool_args

    result = {"retired": 0, "proven": 0, "ruled_out": 0}
    if not settings.assist_commit_reconcile_enabled:
        return result
    try:
        env = await get_environment(session_id=session_id, db=db) or {}
        facts = [str(f) for f in (env.get("facts") or [])]
        row = (await db.execute(
            text("""
                SELECT d.title, d.prompt_template
                  FROM assist_steps s
                  JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
                 WHERE s.session_id = :sid AND s.node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().first()
        ledger_block = (
            "CURRENT FACTS LEDGER:\n" + "\n".join(f"- {f}" for f in facts)
            if facts else "CURRENT FACTS LEDGER: (empty)"
        )
        # §17.873 house pattern — keep the TAIL of long evidence (outcomes and
        # final states live at the end of pasted output).
        ev = evidence or ""
        if len(ev) > 6000:
            ev = "(earlier output truncated)\n…" + ev[-6000:]
        user_msg = (
            f"COMPLETED STEP: {(row or {}).get('title') or node_key}\n"
            f"STEP TASK: {((row or {}).get('prompt_template') or '')[:1500]}\n\n"
            f"{ledger_block}\n\n"
            f"EVIDENCE OF SUCCESSFUL COMPLETION:\n{ev}\n\n"
            "Call update_session_memory."
        )
        args = None
        for _draw in range(3):  # §17.749 pattern — re-draw a missing tool call
            resp = await model_router.tool_call(
                messages=[
                    {"role": "system", "content": _RECONCILE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=[_update_memory_tool()],
                role=settings.assist_guide_model_role,
                temperature=0.0,
                tool_choice="auto",
                max_tokens=8192,
            )
            args = read_tool_args(resp)
            if args is not None:
                break
            logger.info("assist_reconcile_empty_redraw draw=%d/3", _draw + 1)
        if args is None:
            return result
        # Retire only VERBATIM ledger echoes (same contract as §17.725).
        ledger_lower = {f.strip().lower() for f in facts}
        retire = [str(x).strip() for x in (args.get("retire_facts") or [])
                  if str(x).strip().lower() in ledger_lower]
        proven = [str(x).strip()[:300] for x in (args.get("proven_methods") or [])
                  if str(x).strip()][:6]
        ruled = [str(x).strip()[:300] for x in (args.get("ruled_out_approaches") or [])
                 if str(x).strip()][:6]
        if retire or proven or ruled:
            await set_environment(
                session_id=session_id,
                retract_facts=retire or None,
                playbook_proven=proven or None,
                playbook_ruled_out=ruled or None,
                db=db,
            )
        result.update(retired=len(retire), proven=len(proven), ruled_out=len(ruled))
        logger.info(
            "assist_commit_reconciled session_id=%s node_key=%s retired=%d proven=%d ruled_out=%d",
            session_id, node_key, len(retire), len(proven), len(ruled),
        )
    except Exception as e:  # noqa: BLE001 — reconciliation must never break commit
        logger.warning("assist_reconcile_on_commit_failed session_id=%s err=%r", session_id, e)
    return result


async def _reconcile_bg(*, session_id: str, node_key: str, evidence: str) -> None:
    try:
        async with async_session() as bg_db:
            await reconcile_on_commit(
                session_id=session_id, node_key=node_key, evidence=evidence, db=bg_db,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("assist_reconcile_bg_failed session_id=%s err=%r", session_id, e)


_RECONCILE_TASKS: set = set()


def schedule_reconcile_on_commit(
    *, session_id: str, node_key: str, evidence: str,
) -> None:
    """§17.881 — fire-and-forget the commit reconciliation (mirrors the
    consolidate/derive schedulers; strong-ref set keeps tasks alive)."""
    if not settings.assist_commit_reconcile_enabled:
        return
    try:
        task = asyncio.get_running_loop().create_task(
            _reconcile_bg(session_id=session_id, node_key=node_key, evidence=evidence)
        )
        _RECONCILE_TASKS.add(task)
        task.add_done_callback(_RECONCILE_TASKS.discard)
    except RuntimeError:  # no running loop (sync test context) — skip
        logger.debug("assist_reconcile_schedule_no_loop session_id=%s", session_id)
