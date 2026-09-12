"""§17.1043 — plan reconciliation: a confirmed fix is applied to the whole plan.

The operator's proposal, verbatim: *"a system alongside assist that fixes the
broken workflow as a whole. Its sole job being to take addressed errors and
apply them or updated information or changes to the entire plan."*

Why it is needed. When the fix path corrects a wrong path, port or address on
step N and the operator's next paste shows it worked, the correction reaches
the session ledger (facts, sourced values) — and nothing else. The remaining
steps' task text still carries the old value, a cached walkthrough of a later
step may already have been drawn from it, and the digest keeps quoting the
step that carried it. The model is then told the truth in one prompt block
and shown the stale plan in another; the §17.1034 plan-only tier is how that
contradiction has been papered over.

This module owns ONE trigger for now — *fix confirmed*: a step is committed
after one or more fix replies on it. It derives correction records
deterministically (old value → new value, by kind), applies them to every
PENDING step's task text, invalidates any cached walkthrough that mentions the
old value so it regenerates through the verified path, records the correction
as a fact in the ledger, and keeps a durable change ledger on the job. Done
steps are never touched — they are the operator's own executed work.

Grounding rules (the same discipline as the evidence layer):
* an OLD value must appear in what the operator pasted while the step was
  failing (or in the plan text) and must NOT appear in the paste that closed
  the step;
* a NEW value must appear in a fix reply AND in the operator's closing paste
  or the session's confirmed facts — the operator's system said it;
* one old and one new per kind, or nothing: an ambiguous pair is logged and
  left alone. A model may later propose corrections; the application stays
  deterministic and the gates stay.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.modules.assist_evidence import extract_specifics

logger = logging.getLogger("scaffold")

# Absolute POSIX paths with at least two segments and no shell glob; a doubled
# separator (``/opt//opt/x``) is exactly the live class and is kept verbatim.
_PATH_RE = re.compile(r"(?<![\w.:/])(/[A-Za-z0-9_.+-]+(?:/+[A-Za-z0-9_.+-]+)+/?)")
_PATH_SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/")
KINDS = ("ip", "port", "version", "url", "path")
PROVENANCE_PREFIX = "🔁 Updated after step"


def values_in(text_value: str) -> list[dict]:
    """``{kind, value}`` for every concrete value in the text: the evidence
    layer's specifics (ip / port / version / url) plus absolute paths."""
    found = list(extract_specifics(text_value or ""))
    seen = {(f["kind"], f["value"]) for f in found}
    for m in _PATH_RE.finditer(text_value or ""):
        p = m.group(1).rstrip("/")
        if len(p) < 4 or p.startswith(_PATH_SKIP_PREFIXES):
            continue
        if ("path", p) not in seen:
            seen.add(("path", p))
            found.append({"kind": "path", "value": p})
    return found


def _mentions(value: str, hay: str) -> bool:
    return re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w])", hay or "") is not None


def derive_corrections(*, failing_pastes: list[str], fix_replies: list[str],
                       closing_paste: str, plan_text: str,
                       confirmed_facts: str = "") -> tuple[list[dict], list[dict]]:
    """Returns ``(corrections, declined)``. A correction is
    ``{kind, old, new}``; ``declined`` records candidate sets the rules could
    not resolve to exactly one pair, for the log."""
    failing = "\n".join(failing_pastes or [])
    fixes = "\n".join(fix_replies or [])
    closing = closing_paste or ""
    confirmed = (closing + "\n" + (confirmed_facts or "")).strip()
    if not fixes.strip() or not confirmed.strip():
        return [], []
    corrections: list[dict] = []
    declined: list[dict] = []
    by_kind_fix: dict[str, list[str]] = {k: [] for k in KINDS}
    for it in values_in(fixes):
        if it["value"] not in by_kind_fix[it["kind"]]:
            by_kind_fix[it["kind"]].append(it["value"])
    by_kind_old: dict[str, list[str]] = {k: [] for k in KINDS}
    for it in values_in(failing + "\n" + plan_text):
        if it["value"] not in by_kind_old[it["kind"]]:
            by_kind_old[it["kind"]].append(it["value"])
    for kind in KINDS:
        # NEW: proposed by a fix AND stated by the operator's system afterwards.
        new = [v for v in by_kind_fix[kind] if _mentions(v, confirmed)]
        # OLD: seen while failing (or in the plan), absent from the closing
        # paste and from the confirmed facts, and not itself a proposed value.
        old = [v for v in by_kind_old[kind]
               if v not in new and not _mentions(v, confirmed)
               and (_mentions(v, failing) or not by_kind_old[kind])]
        old = [v for v in old if _mentions(v, failing)]
        if not new or not old:
            continue
        if len(new) == 1 and len(old) == 1 and new[0] != old[0]:
            corrections.append({"kind": kind, "old": old[0], "new": new[0]})
        else:
            declined.append({"kind": kind, "old": old[:4], "new": new[:4]})
    return corrections, declined


def plan_changes(nodes: list[dict], steps: list[dict], corrections: list[dict],
                 *, source_node_key: str) -> dict:
    """Pure: which pending nodes' task text change and how, and which cached
    walkthroughs must be regenerated. ``nodes``: ``{node_key, status,
    prompt_template}``; ``steps``: ``{node_key, status, guidance}``."""
    node_updates: list[dict] = []
    for n in nodes or []:
        nk = n.get("node_key")
        if nk == source_node_key or (n.get("status") or "") not in ("pending", "blocked"):
            continue
        txt = n.get("prompt_template") or ""
        # Earlier provenance lines quote the old value; they are never a match
        # target (idempotence) and are kept below the rewritten body.
        body_lines = [ln for ln in txt.splitlines() if not ln.lstrip().startswith(PROVENANCE_PREFIX)]
        prov_lines = [ln for ln in txt.splitlines() if ln.lstrip().startswith(PROVENANCE_PREFIX)]
        body = "\n".join(body_lines).rstrip()
        new_body = body
        applied: list[dict] = []
        for c in corrections:
            if _mentions(c["old"], new_body):
                new_body = re.sub(r"(?<![\w.])" + re.escape(c["old"]) + r"(?![\w])", c["new"], new_body)
                applied.append(c)
        if applied:
            prov_lines.extend(
                f"{PROVENANCE_PREFIX} {source_node_key}: `{c['old']}` → `{c['new']}` ({c['kind']})."
                for c in applied)
            new_txt = new_body + "\n\n" + "\n".join(prov_lines)
            node_updates.append({"node_key": nk, "prompt_template": new_txt,
                                 "corrections": applied})
    guidance_resets: list[str] = []
    for s in steps or []:
        nk = s.get("node_key")
        if nk == source_node_key or (s.get("status") or "") in ("committed", "skipped", "handed_off", "escalated"):
            continue
        g = s.get("guidance") or ""
        if g and any(_mentions(c["old"], g) for c in corrections):
            guidance_resets.append(nk)
    return {"node_updates": node_updates, "guidance_resets": guidance_resets}


def render_note(result: dict) -> str:
    """The operator-facing line. Empty when nothing changed."""
    if not result or not (result.get("node_updates") or result.get("guidance_resets")):
        return ""
    src = result.get("source_node_key", "?")
    pairs: dict[tuple, list[str]] = {}
    for u in result.get("node_updates") or []:
        for c in u["corrections"]:
            pairs.setdefault((c["old"], c["new"], c["kind"]), []).append(u["node_key"])
    lines = [f"🔁 **Plan updated after step {src}** — the fix that worked has been applied to the steps ahead:"]
    for (old, new, kind), nks in pairs.items():
        lines.append(f"- `{old}` → `{new}` ({kind}) in {', '.join(nks)}")
    resets = result.get("guidance_resets") or []
    if resets:
        lines.append(f"- the walkthrough{'s' if len(resets) > 1 else ''} for {', '.join(resets)} "
                     f"will be rewritten from the corrected plan when you reach {'them' if len(resets) > 1 else 'it'}")
    return "\n".join(lines)


async def _fix_context(*, db, session_id: str, node_key: str) -> Optional[dict]:
    step = (await db.execute(text("""
        SELECT presented_at, evidence FROM assist_steps
         WHERE session_id = :sid AND node_key = :nk
    """), {"sid": session_id, "nk": node_key})).mappings().first()
    if not step:
        return None
    rows = (await db.execute(text("""
        SELECT role, kind, content, created_at FROM assist_turns
         WHERE session_id = :sid AND node_key = :nk
           AND (:since IS NULL OR created_at >= :since)
         ORDER BY created_at, id
    """), {"sid": session_id, "nk": node_key, "since": step["presented_at"]})).mappings().all()
    fixes = [r["content"] or "" for r in rows if r["role"] == "assistant" and r["kind"] == "fix"]
    if not fixes:
        return None
    last_fix_at = max(r["created_at"] for r in rows if r["role"] == "assistant" and r["kind"] == "fix")
    failing = [r["content"] or "" for r in rows
               if r["role"] == "operator" and r["kind"] in ("message", "submit") and r["created_at"] <= last_fix_at]
    return {"fixes": fixes, "failing": failing, "closing": step["evidence"] or ""}


async def reconcile_after_commit(*, db, session_id: str, job_id: str, node_key: str,
                                 evidence: str) -> Optional[dict]:
    """The fix-confirmed trigger. Called after a step is committed; returns the
    change record (or None when the step had no fix, or nothing applied).
    Fail-soft: a reconciliation failure never breaks a commit."""
    if not settings.plan_reconcile_enabled:
        return None
    try:
        ctx = await _fix_context(db=db, session_id=session_id, node_key=node_key)
        if not ctx:
            return None
        closing = (evidence or "") + "\n" + (ctx["closing"] or "")
        nodes = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, prompt_template FROM dag_nodes
             WHERE job_id = :jid ORDER BY execution_order
        """), {"jid": job_id})).mappings().all()]
        steps = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, guidance FROM assist_steps WHERE session_id = :sid
        """), {"sid": session_id})).mappings().all()]
        plan_text = "\n".join((n.get("prompt_template") or "") for n in nodes
                              if (n.get("status") or "") in ("pending", "blocked"))
        from app.modules.assist_environment import _environment_from_metadata
        from app.modules.assist_evidence import ledger_text
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        env = _environment_from_metadata(meta)
        corrections, declined = derive_corrections(
            failing_pastes=ctx["failing"], fix_replies=ctx["fixes"], closing_paste=closing,
            plan_text=plan_text, confirmed_facts=ledger_text(env))
        if declined:
            logger.info("plan_reconcile_declined session_id=%s node_key=%s candidates=%r",
                        session_id, node_key, declined[:4])
        if not corrections:
            logger.info("plan_reconcile_no_corrections session_id=%s node_key=%s fixes=%d",
                        session_id, node_key, len(ctx["fixes"]))
            return None
        changes = plan_changes(nodes, steps, corrections, source_node_key=node_key)
        changes["source_node_key"] = node_key
        changes["corrections"] = corrections
        for u in changes["node_updates"]:
            await db.execute(text("""
                UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
            """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
        for nk in changes["guidance_resets"]:
            await db.execute(text("""
                UPDATE assist_steps
                   SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL,
                       updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
            """), {"sid": session_id, "nk": nk})
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "trigger": "fix_confirmed",
            "source_node_key": node_key,
            "corrections": corrections,
            "nodes": [u["node_key"] for u in changes["node_updates"]],
            "guidance_resets": changes["guidance_resets"],
        }
        await db.execute(text("""
            UPDATE jobs
               SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                        COALESCE(metadata->'reconciliation', '[]'::jsonb)
                                        || CAST(:e AS jsonb))
             WHERE id = :jid
        """), {"e": json.dumps(entry), "jid": job_id})
        await db.commit()
        try:
            from app.modules.assist_environment import set_environment
            await set_environment(
                session_id=session_id, db=db,
                facts=[f"Corrected at {node_key}: {c['old']} → {c['new']} ({c['kind']}), confirmed by the operator"
                       for c in corrections])
        except Exception as exc:  # noqa: BLE001 — the ledger fact is a courtesy
            logger.warning("plan_reconcile_fact_failed session_id=%s err=%r", session_id, exc)
        logger.warning(
            "plan_reconcile_applied session_id=%s node_key=%s corrections=%r nodes=%r guidance_resets=%r",
            session_id, node_key, [(c["old"], c["new"]) for c in corrections],
            entry["nodes"], entry["guidance_resets"])
        return changes
    except Exception as exc:  # noqa: BLE001 — never break a commit
        logger.warning("plan_reconcile_failed session_id=%s node_key=%s err=%r", session_id, node_key, exc)
        return None
