"""Assist turn transcript helpers — extracted from assist_agent.py.

§17.856 (audit "assist decomposition") — read/format helpers over the
assist_turns ledger: ingest an assistant reply (capture_assistant_reply), read
history (history_from_turns / _history_or_transcript / list_turns), and render a
node transcript / conversation block / step-recap-annotated block. Only stdlib +
sqlalchemy; every name re-exported from assist_agent. (ingest_turn moves too; its
schedule_derive_turn_memory call uses a patch-safe late import.)
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold.assist")


def _conversation_block_for(history: list[dict] | None) -> str:
    """§17.687 — render the recent OWUI back-and-forth into a recall block,
    gated by settings. Returns "" when disabled, empty, or on any render error
    so callers can thread the result unconditionally (fail-soft: a history
    render must never break the guidance/fix/research/classify turn)."""
    from app.config import settings
    from app.modules import assist_guide

    if (
        not settings.assist_conversation_context_enabled
        or settings.assist_conversation_context_max_chars <= 0
        or settings.assist_conversation_context_turns <= 0
        or not history
    ):
        return ""
    try:
        turns = settings.assist_conversation_context_turns
        recent = history[-turns:]
        return assist_guide.render_conversation_block(
            recent, max_chars=settings.assist_conversation_context_max_chars,
        )
    except Exception as exc:  # noqa: BLE001 — never block the turn on a render
        logger.warning("assist_conversation_block_failed job_id_unknown: %s", exc)
        return ""


def _with_step_recap(conversation: str, recap: str) -> str:
    """§17.738 — prepend the running step recap to the conversation block so it
    leads the recall context in guidance/fix prompts. Either part may be ""; the
    recap comes FIRST (the load-bearing full-thread state) so a budget trim on
    the conversation tail never drops it."""
    from app.modules import assist_guide

    block = assist_guide.render_step_recap_block(recap)
    parts = [p for p in (block, conversation) if (p or "").strip()]
    return "\n\n".join(parts)


async def ingest_turn(
    *, session_id: str, role: str, kind: str, content: str,
    node_key: str | None = None, evidence_kind: str | None = None, db,
) -> bool:
    """§17.710a — append a raw turn to the append-only ``assist_turns``
    transcript, UNCONDITIONALLY and BEFORE any intent classification.

    This is the lossless capture layer the narrow retention channels missed:
    whatever didn't match a channel's trigger (an audit paste with no
    placeholders, a message the classifier mislabeled) still lands here, so
    Stage B can derive ``session_memory`` from the transcript. Gated on the
    master + capture valves; commits its own insert so a later rollback in the
    caller can't lose the turn; fail-soft (never disturbs the caller). Returns
    True iff a row was written."""
    from app.config import settings

    if not (settings.assist_unified_memory_enabled and settings.assist_umem_capture):
        return False
    # A skip carries no content but is still a real turn worth recording.
    if not (content or "").strip() and kind != "skip":
        return False
    try:
        # INSERT…SELECT pulls the session's job_id and no-ops if the session is
        # unknown (no matching row → nothing inserted).
        res = await db.execute(
            text("""
                INSERT INTO assist_turns
                    (session_id, job_id, node_key, role, kind, content, evidence_kind)
                SELECT :sid, s.job_id, :nk, :role, :kind, :content, :ek
                  FROM assist_sessions s WHERE s.id = :sid
            """),
            {"sid": session_id, "nk": node_key, "role": role, "kind": kind,
             "content": content or "", "ek": evidence_kind},
        )
        # §17.720 — a captured turn IS session activity. Without this bump an
        # actively-chatting session kept its pre-§17.710a last_activity_at, so
        # it ranked as idle (reaper staleness, reconnect recency) while the
        # operator was mid-conversation in it.
        if getattr(res, "rowcount", 0):
            await db.execute(
                text("""
                    UPDATE assist_sessions
                       SET last_activity_at = now(), updated_at = now()
                     WHERE id = :sid
                """),
                {"sid": session_id},
            )
        await db.commit()
        recorded = bool(getattr(res, "rowcount", 0))
        # §17.812 (audit gap 1) — derive parity: the per-turn derive (§17.715)
        # fired only from POST /turn, so slash/CLI/SDK submits, notes and fixes
        # were captured but their durable plan-relevant memory never derived.
        # Folding it into the capture funnel gives every operator capture site
        # the same treatment (assistant turns are the engine's own words —
        # skipped). schedule_derive_turn_memory gates on its own valves and
        # dedupes recent content, so NL turns that reach the funnel twice
        # (message + submit/fix double-record) derive once.
        # §17.854 (audit C4) — a 'submit' turn's durable facts are extracted by
        # the /submit endpoint's own capture_session_facts (supersession-aware,
        # the specific extractor for evidence). Scheduling derive_turn_memory too
        # meant TWO different fact-extraction prompts per submit whose near-dup
        # facts slipped past set_environment's exact-text dedup and bloated the
        # ledger (which consolidate_facts then paid a THIRD call to mop up). Skip
        # the background derive for submits; message/fix turns are unaffected.
        if (recorded and (role or "").strip().lower() == "operator"
                and kind != "submit"):
            # §17.856 — schedule_derive_turn_memory stays in assist_agent (memory
            # cluster). Deferred import avoids the load-time cycle and stays
            # patch-safe (a test patching assist_agent.schedule_derive_turn_memory
            # is picked up here at call time).
            from app.modules.assist_agent import schedule_derive_turn_memory
            schedule_derive_turn_memory(
                session_id=session_id, node_key=node_key, message=content or "",
            )
        return recorded
    except Exception as e:  # noqa: BLE001 — capture must never break the turn
        try:  # §17.888(#14) — clear the poisoned tx so later writes survive
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.debug("ingest_turn_failed session_id=%s err=%r", session_id, e)
        return False


async def capture_assistant_reply(
    *, session_id: str, node_key: str | None, kind: str, content: str, db,
) -> bool:
    """§17.726 — record what the ENGINE told the operator (guide / ask / fix /
    deliberation) as a ``role='assistant'`` turn. Pre-§17.726 the transcript was
    operator-only (``record_turn_bg`` hard-codes the role), so across the
    engine's fresh-per-call model the only memory of its own replies was the
    pipeline's 6-turn OWUI history window — gone entirely on a cross-chat
    reconnect. Same valve gating + fail-soft as ``ingest_turn``; bounded so a
    long walkthrough doesn't bloat the transcript (the full text lives in the
    guidance cache / step output anyway).

    §17.812 (gap 2) — dedupes against the node's MOST RECENT assistant turn so
    cached re-presents can be captured by callers without back-to-back replays
    stacking identical rows: a replay after intervening turns IS new dialogue
    worth recording (the operator saw it again at that point in the thread)."""
    from app.config import settings

    bounded = (content or "")[:8000]
    if settings.assist_unified_memory_enabled and settings.assist_umem_capture:
        try:
            last = (await db.execute(
                text("""
                    SELECT content FROM assist_turns
                     WHERE session_id = :sid AND role = 'assistant'
                       AND node_key IS NOT DISTINCT FROM :nk
                     ORDER BY created_at DESC, id DESC LIMIT 1
                """),
                {"sid": session_id, "nk": node_key},
            )).scalar()
            if (last or "") == bounded:
                return False
        except Exception as e:  # noqa: BLE001 — dedupe is best-effort
            logger.debug("capture_dedupe_check_failed session_id=%s err=%r", session_id, e)
    return await ingest_turn(
        session_id=session_id, role="assistant", kind=kind,
        content=bounded, node_key=node_key, db=db,
    )


async def history_from_turns(
    *, session_id: str, db, limit: int = 12, exclude_tail: str | None = None,
) -> list[dict]:
    """§17.726 — rebuild a recent-conversation ``history`` from the durable
    transcript when the client sent none (curl/CLI, or a cross-chat reconnect
    where the new OWUI chat has no shared history). Returns oldest-first
    ``[{role: 'user'|'assistant', content}]`` shaped for
    ``render_conversation_block``. ``exclude_tail`` drops the most recent
    operator turn when it IS the current message (it's threaded separately as
    the refine/question/error). Fail-soft → []."""
    try:
        rows = (await db.execute(
            text("""
                SELECT role, content FROM assist_turns
                 WHERE session_id = :sid AND kind <> 'skip'
                 ORDER BY created_at DESC, id DESC LIMIT :lim
            """),
            {"sid": session_id, "lim": int(limit)},
        )).mappings().all()
    except Exception as e:  # noqa: BLE001 — a fallback must never break the turn
        logger.debug("history_from_turns_failed session_id=%s err=%r", session_id, e)
        return []
    out: list[dict] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if (r.get("role") or "") == "assistant" else "user"
        # The same paste often lands twice (a 'message' row + a 'submit' row) —
        # collapse consecutive duplicates so the rebuilt history reads clean.
        if out and out[-1]["role"] == role and out[-1]["content"] == content:
            continue
        out.append({"role": role, "content": content})
    out.reverse()
    if (
        exclude_tail and out and out[-1]["role"] == "user"
        and out[-1]["content"].strip() == exclude_tail.strip()
    ):
        out.pop()
    return out


async def _history_or_transcript(
    *, history: list[dict] | None, session_id: str, db,
    exclude_tail: str | None = None,
) -> list[dict] | None:
    """§17.726 — prefer the client-supplied history (same-chat OWUI, freshest);
    fall back to the durable transcript when none arrived. Gated on the master
    valve so the legacy path is byte-identical with the stack off."""
    if history:
        return history
    from app.config import settings
    if not settings.assist_unified_memory_enabled:
        return history
    return await history_from_turns(
        session_id=session_id, db=db, exclude_tail=exclude_tail,
    ) or None


async def list_turns(*, session_id: str, limit: int = 200, db) -> list[dict]:
    """§17.710a — the session's raw transcript, oldest-first. Backs GET
    /assist/{sid}/turns and (Stage B) session_memory consolidation.

    §17.928 — the window is the NEWEST ``limit`` turns, rendered oldest-first.
    It used to be ``ORDER BY created_at, id LIMIT :lim`` — the OLDEST ``limit``
    turns — which is the same query only while a session is shorter than the
    cap. Past it the endpoint froze: every turn after number ``limit`` was
    unreachable, so the transcript silently stopped at the moment the session
    crossed 200 turns and never moved again.

    This one line was three separate operator-visible failures:

      * **the engine "can't figure out the current problem"** — the client
        derives its model history from this response (`historyForGuide()` sends
        the last 8), so guidance was reasoning about the wrong week entirely;
      * **"my messages vanish"** — the client renders this response as the
        transcript, so every message the operator sent after the cap
        disappeared on the next reload (§17.929 covers the racing half);
      * **the step never advances cleanly** — the recap and next-action both
        read a transcript that ended days ago.

    Measured on the live session (613dd1df, 545 turns): the endpoint returned
    turns 1-200, ending 2026-08-30, while the operator was working 2026-09-05.
    Six days of context — an entire finished sub-project — were invisible to
    both the screen and the model.

    Ordering the window DESC and re-sorting ASC in a subquery keeps the
    contract ("oldest-first") for every existing caller while making the cap
    mean "the most recent N", which is what a transcript window is for.
    """
    rows = (await db.execute(
        text("""
            SELECT id, node_key, role, kind, content, evidence_kind, created_at
              FROM (
                    SELECT id, node_key, role, kind, content,
                           evidence_kind, created_at
                      FROM assist_turns WHERE session_id = :sid
                     ORDER BY created_at DESC, id DESC LIMIT :lim
                   ) AS recent
             ORDER BY created_at ASC, id ASC
        """),
        {"sid": session_id, "lim": int(limit)},
    )).mappings().all()
    return [dict(r) for r in rows]


def _render_node_transcript(turns: list[dict], *, max_chars: int = 12000) -> str:
    """§17.738 — render node-scoped turns into a compact transcript for the
    recap summarizer. Operator/assistant labeled; message+submit double-records
    collapsed; keeps the MOST RECENT within budget (drops oldest)."""
    lines: list[str] = []
    prev = None
    for t in turns:
        content = (t.get("content") or "").strip()
        if not content:
            continue
        role = "Assistant" if (t.get("role") == "assistant") else "Operator"
        sig = (role, content)
        if sig == prev:
            continue
        prev = sig
        if len(content) > 1500:  # a long assistant walkthrough — keep the head
            content = content[:1500].rstrip() + " …"
        lines.append(f"{role}: {content}")
    # keep most-recent within budget
    kept: list[str] = []
    total = 0
    for ln in reversed(lines):
        if kept and total + len(ln) + 2 > max_chars:
            break
        kept.append(ln)
        total += len(ln) + 2
    kept.reverse()
    return "\n\n".join(kept)
