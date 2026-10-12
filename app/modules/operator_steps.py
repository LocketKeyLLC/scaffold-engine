"""§17.1458 — in Auto mode, the operator's part of the plan is a walkthrough the run waits on, then checks.

Live (2026-10-10, the home-lab Auto run): ADD150 advertised the Tailscale subnet from the host, and the
route then needed approving in the operator's own Tailscale admin console. Nothing walked them through it:
the engine's half ran, its check read 0, the step was recorded FAILED, and the operator was told what to click
in chat. Tailscale had been installed on the host and never set up anywhere else — one device in the tailnet.
And a step the plan tags for a human (`tool: human`) was worse: Auto mode recorded it done, "Skipped: human
review not required in auto mode". ADD152 (the router port-forward, in the operator's phone app) would have
gone the same way.

So a step whose remaining work is the operator's — wholly (tagged human, or it names an operator part and
gives the engine no command to run), or the rest of one the engine has done its half of — parks the run as
``kind: "yours"``: a walkthrough of THEIR part, written from research, and the step's own read-only checks.
"I've done it — check" runs those checks on the machine; the step is done only when they confirm it, and
otherwise stays parked with what the machine answered. Skip skips it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold")

KIND = "yours"
CHOICES = ("done", "skip")
ENGINE_HALF_DONE = "engine_half_done"

# Sentences that hand work to the operator: "…is the operator's", "in their own account", an app or
# console only they can open.
_OPERATORS_RE = re.compile(
    # §17.1487 — "own" only for a place the operator works ("the operator's own phone"), not their possessions: live,
    # ADD159 ("stop qBittorrent") was handed to the operator for "sends its traffic on the operator's own connection"
    r"\b(?:is|are)\s+the\s+operator'?s\b|\bthe\s+operator'?s\s*(?:,|\(|own\s+(?:screen|phone|device|account|"
    r"browser|computer|laptop|hands|login)\b|part\b|to\s+do\b|action\b)|"
    r"\bin\s+their\s+own\s+account\b|\bin\s+(?:the\s+)?my\s+spectrum\s+app\b|\badmin\s+console\b|"
    # §17.1493 — not "reports": what the operator SAID is not a task for them ("The operator reports the torrents look
    # halted WITH several peers" handed ADD169 — reading qBittorrent's API — to the operator)
    r"\bthe\s+operator\s+(?:then\s+)?(?:approves|adds|reserves|logs\s+in|signs\s+in|installs|opens)\b",
    re.I)
# §17.1468 — a sentence ends at punctuation followed by a space or the end; the dots inside `192.168.1.44`,
# `defrusciohomelab.duckdns.org` or `106.fw` do not end it. Live (ADD152): the operator's part was cut to
# "… Port Forwarding & IP Reservations): reserve 192." in the card's record.
_SENTENCE_RE = re.compile(r"(?:[^.!?\n]|[.!?](?=[^\s.!?]))+(?:[.!?]+(?=\s|$)|$)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_DONE_WHEN_RE = re.compile(r"\bDone when\b", re.I)


def _as_dict_md(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return dict(v) if isinstance(v, dict) else {}


def _text(node: Optional[dict]) -> str:
    return str((node or {}).get("description") or (node or {}).get("prompt_template") or "")


def operators_part(node: Optional[dict]) -> str:
    """The operator's part of a step, in the step's own words ('' when it has none)."""
    tool = str((node or {}).get("tool") or "").lower()
    body = _text(node)
    if tool == "human":   # §17.1461 — `human_review` is an optional review Auto mode may skip
        return body.strip()
    work = _DONE_WHEN_RE.split(body, 1)[0]
    hits = [s.strip() for s in _SENTENCE_RE.findall(work) if _OPERATORS_RE.search(s)]
    return " ".join(dict.fromkeys(hits)).strip()


def is_wholly_operators(node: Optional[dict]) -> bool:
    """Tagged for a human, or it names an operator part and gives the engine no command before 'Done when'."""
    tool = str((node or {}).get("tool") or "").lower()
    if tool == "human":   # §17.1461 — `human_review` is an optional review Auto mode may skip
        return True
    if not operators_part(node):
        return False
    work = _DONE_WHEN_RE.split(_text(node), 1)[0]
    from app.modules.assist_state_check import read_only_command
    engine_cmds = [c for c in _BACKTICK_RE.findall(work) if _looks_like_command(c) and not read_only_command(c)]
    return not engine_cmds


def _looks_like_command(c: str) -> bool:
    return bool(re.match(r"\s*[a-z][\w.-]*(?:\s|$)", c)) and " " in c.strip()


def checks(node: Optional[dict]) -> list[str]:
    """The step's own read-only checks: backticked commands after 'Done when'."""
    parts = _DONE_WHEN_RE.split(_text(node), 1)
    if len(parts) < 2:
        return []
    from app.modules.assist_state_check import read_only_command
    return [c.strip() for c in dict.fromkeys(_BACKTICK_RE.findall(parts[1]))
            if _looks_like_command(c) and read_only_command(c.strip())]


# §17.1464 — where the operator's part happens: the app or console they have to open.
_SURFACE_RE = re.compile(r"\b(my spectrum app|tailscale(?: admin console| app)?|admin console|router app|[a-z][\w-]+ app)\b", re.I)


def surfaces(node: Optional[dict]) -> set[str]:
    return {m.group(1).lower() for m in _SURFACE_RE.finditer(operators_part(node) or "")}


async def same_place_steps(db: AsyncSession, job_id: str, node: dict) -> list[dict]:
    """§17.1464 — the operator's OTHER pending steps on the same app or console whose dependencies are done (or are
    this step), so one trip covers them. Live: two router-app forwards (ADD4, ADD152) were two separate trips into the
    My Spectrum app, the second hitting a rule the first had already learned."""
    here = surfaces(node)
    if not here:
        return []
    rows = (await db.execute(text("""
        SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on, n.tool, n.node_type
          FROM dag_nodes n
         WHERE n.job_id = :jid AND n.status = 'pending' AND n.node_key <> :nk
           AND NOT EXISTS (
               SELECT 1 FROM unnest(COALESCE(n.depends_on, ARRAY[]::text[])) AS dep(k)
                WHERE dep.k <> :nk AND NOT EXISTS (SELECT 1 FROM dag_nodes d WHERE d.job_id = :jid
                                    AND d.node_key = dep.k AND d.status IN ('done', 'skipped')))
         ORDER BY n.execution_order ASC
    """), {"jid": job_id, "nk": node.get("node_key")})).mappings().all()
    out = []
    for r in rows:
        other = dict(r)
        if is_wholly_operators(other) and surfaces(other) & here:
            out.append(other)
    return out[:3]


def relevant_facts(node: dict, facts: Optional[list]) -> list[str]:
    """The session facts that share this step's place or two of its distinctive words."""
    part = (operators_part(node) or _text(node)).lower()
    here = surfaces(node)
    _skip = {"their", "which", "there", "shoul", "would", "opera", "engin", "learn"}

    def stems(t: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[a-z][a-z0-9-]{4,}", t)} - _skip
    mine = stems(part + " " + str((node or {}).get("title") or "").lower())
    scored = []
    for i, f in enumerate(facts or []):
        t = str(f if isinstance(f, str) else (f or {}).get("text") or "")
        low = t.lower()
        shared = len(mine & stems(low))
        if any(s in low for s in here) or shared >= 2:
            # §17.1478 — the most to do with this step first; ties keep the ledger's newest-last order reversed,
            # so a long step's newest measurements are not the ones cut by the cap
            scored.append((-(shared + (5 if any(s in low for s in here) else 0)), -i, t[:300]))
    return [t for _a, _b, t in sorted(scored)][:8]


async def pending_operator_step(db: AsyncSession, job_id: str) -> Optional[dict]:
    """The first dep-satisfied pending step whose remaining work is the operator's."""
    md = (await db.execute(text("SELECT metadata FROM jobs WHERE id = :jid"), {"jid": job_id})).scalar()
    md = json.loads(md) if isinstance(md, str) else (md or {})
    decided = (md or {}).get("decisions") or {}
    rows = (await db.execute(text("""
        SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on, n.tool, n.node_type,
               n.execution_order, n.last_verification_reason, n.output_text
          FROM dag_nodes n
         WHERE n.job_id = :jid AND n.status = 'pending'
           AND NOT EXISTS (
               SELECT 1 FROM unnest(COALESCE(n.depends_on, ARRAY[]::text[])) AS dep(k)
                WHERE NOT EXISTS (SELECT 1 FROM dag_nodes d WHERE d.job_id = :jid AND d.node_key = dep.k
                                    AND d.status IN ('done', 'skipped')))
         ORDER BY n.execution_order ASC
    """), {"jid": job_id})).mappings().all()
    for r in rows:
        node = dict(r)
        if str(node.get("node_type") or "") == "decision":
            continue
        entry = decided.get(node["node_key"]) or {}
        if isinstance(entry, dict) and entry.get("result") == ENGINE_HALF_DONE:
            node["engine_half"] = entry
            return node
        if entry:
            continue
        if is_wholly_operators(node):
            return node
    return None


async def _walkthrough(node: dict, part: str, upstream: str = "", facts: Optional[list[str]] = None,
                       also: Optional[list[dict]] = None) -> str:
    """The operator's part as a walkthrough, grounded on a fresh search; '' when no model answers."""
    from app.config import settings
    from app import model_router
    from app.modules.assist_research_lib import _deep_web_sources, _goal_keywords
    from app.utils.llm_retry import generate_until_nonempty
    query = " ".join(_goal_keywords(part, 6)) or str(node.get("title") or "")
    sources: list[dict] = []
    try:
        sources = await _deep_web_sources(query, top_n=3)
    except Exception as exc:
        logger.warning("operator_step_research_failed node=%s err=%r", node.get("node_key"), exc)
    src = "\n\n".join(f"[{i}] {s.get('url', '')}\n{str(s.get('text') or '')[:1800]}" for i, s in enumerate(sources, 1))
    system = (
        "You write the OPERATOR's part of one home-lab step as a walkthrough they follow on their own "
        "phone, browser or account — the engine cannot do this part and has already done the rest. "
        "Start with `## 👉 Do this next` carrying the whole first action (for a click path, every screen in "
        "order joined by →). Then `## Steps`, numbered, one action each, with the exact labels to look for. "
        "Then `## ✅ Done when` saying what they will see, and that pressing \"I've done it — check\" makes "
        "the engine read the machine to confirm it. Use the SOURCES for menu paths and labels; where they "
        "do not cover something, say it is unconfirmed rather than inventing it. Never ask them to run a "
        "command on the server, never paste a secret, and never describe work outside their part.")
    prompt = (f"Step {node.get('node_key')}: {node.get('title')}\n\nThe operator's part:\n{part}\n\n"
              f"The whole step, for context:\n{_text(node)[:3000]}\n\n"
              + (f"What earlier steps produced:\n{upstream[:2000]}\n\n" if upstream else "")
              + ("WHAT THE ENGINE ALREADY LEARNED (follow it; if it means something must be done first, say so "
                 "first):\n" + "\n".join(f"- {f}" for f in facts) + "\n\n" if facts else "")
              + ("ALSO THE OPERATOR'S, IN THE SAME PLACE — fold these into the same walkthrough so it is ONE trip, "
                 "each under its own heading with its step key:\n"
                 + "\n".join(f"- {o.get('node_key')}: {o.get('title')} — {operators_part(o)[:600]}" for o in also)
                 + "\n\n" if also else "")
              + (f"SOURCES (searched for: {query}):\n{src}" if src else "SOURCES: none found this time."))
    try:
        resp = await generate_until_nonempty(
            model_router.generate, prompt, {"role": "model_general", "think": False}, system=system,
            temperature=0.2, max_tokens=settings.node_generation_max_tokens,
            draws=settings.node_generation_max_draws, label=f"operator walkthrough {node.get('node_key')}")
        return (getattr(resp, "text", "") or "").strip()
    except Exception as exc:
        logger.warning("operator_step_walkthrough_failed node=%s err=%r", node.get("node_key"), exc)
        return ""


async def frame_operator(node: dict, *, upstream: str = "", last_check: str = "",
                         facts: Optional[list] = None, also: Optional[list[dict]] = None) -> dict:
    part = operators_part(node) or _text(node)
    also = list(also or [])
    walk = await _walkthrough(node, part, upstream, relevant_facts(node, facts), also)
    if not walk:
        walk = f"## 👉 Do this next\n{part}\n\n(The engine could not write a fuller walkthrough this time.)"
    chk = checks(node)
    return {
        "kind": KIND, "framed": True, "node_key": node.get("node_key"), "title": node.get("title"),
        "question": f"Your turn — step {node.get('node_key')}: {node.get('title')}",
        "walkthrough": walk, "part": part, "verify": chk,
        "engine_half": bool(node.get("engine_half")),
        # §17.1464 — the same trip: each folded step with its own part and checks
        "also": [{"node_key": o.get("node_key"), "title": o.get("title"), "part": operators_part(o),
                  "verify": checks(o)} for o in also],
        "last_check": last_check,
        "options": [
            {"id": "done", "label": "I've done it — check",
             "fit": ("the engine reads the machine with this step's own checks and records the step done "
                     "only when they confirm it") if chk else "the engine records it done on your word"},
            {"id": "skip", "label": "Skip this step", "fit": "not needed", "tradeoff": "steps after it may not make sense"},
        ],
        "suggested": "done", "depends_on": list(node.get("depends_on") or []),
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }


async def resolve_operator(db: AsyncSession, job_id: str, node_key: str, choice: str, waiting: dict) -> dict:
    """`done` → run the step's checks; done only if they confirm. `skip` → skipped."""
    c = (choice or "").strip().lower()
    if c in ("skip", "skip this step"):
        upd = await db.execute(text(
            "UPDATE dag_nodes SET status = 'skipped', output_text = :out, completed_at = NOW(), updated_at = NOW() "
            "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": node_key, "out": "Skipped by the operator (their part, Auto mode)."})
        return {"outcome": "skipped" if upd.rowcount else "node_gone", "node_status": "skipped"}
    if c not in ("done", "i've done it — check", "i've done it", "yes", "checked"):
        return {"outcome": "bad_choice", "choices": list(CHOICES)}
    chk = list(waiting.get("verify") or [])
    evidence = ""
    if chk:
        from app.modules import supervised_runs as sr
        ch = await sr.channel(db)
        if ch is None:
            return {"outcome": "no_channel"}
        spec, _policy = ch
        pasted, _ran = await sr.run_verify(spec, chk, {})
        verdicts = await sr._verify_verdicts(str(waiting.get("title") or node_key), chk, pasted)
        confirmed = bool(verdicts) and all(v.get("verdict") == "confirmed" for v in verdicts)
        evidence = (pasted or "").strip()[:3000]
        if not confirmed:
            logger.info("operator_step_not_yet job=%s node=%s verdicts=%r", job_id, node_key,
                        [v.get("verdict") for v in verdicts])
            return {"outcome": "not_yet", "node_key": node_key, "evidence": evidence,
                    "verdicts": verdicts}
    also_done = await _resolve_also(db, job_id, waiting)
    out = ("## Done by the operator\n\n" + str(waiting.get("part") or "")
           + (f"\n\n## Checked on the machine\n```\n{evidence}\n```" if evidence else
              "\n\n(No machine check exists for this part; recorded on the operator's word.)"))
    upd = await db.execute(text(
        "UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), "
        "started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
        "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
        {"jid": job_id, "nk": node_key, "out": out})
    return {"outcome": "done" if upd.rowcount else "node_gone", "node_status": "done", "evidence": evidence,
            "also_done": also_done}


async def _resolve_also(db: AsyncSession, job_id: str, waiting: dict) -> list[str]:
    """§17.1464 — each step folded into the same trip is done only when ITS checks confirm it."""
    done: list[str] = []
    for o in waiting.get("also") or []:
        nk, chk = o.get("node_key"), list(o.get("verify") or [])
        evidence = ""
        if chk:
            from app.modules import supervised_runs as sr
            ch = await sr.channel(db)
            if ch is None:
                continue
            pasted, _ran = await sr.run_verify(ch[0], chk, {})
            verdicts = await sr._verify_verdicts(str(o.get("title") or nk), chk, pasted)
            if not (verdicts and all(v.get("verdict") == "confirmed" for v in verdicts)):
                continue
            evidence = (pasted or "").strip()[:3000]
        else:
            continue                         # nothing to check it with: it keeps its own card
        upd = await db.execute(text(
            "UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), "
            "started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
            "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": nk, "out": "## Done by the operator (same trip)\n\n" + str(o.get("part") or "")
                                                + f"\n\n## Checked on the machine\n```\n{evidence}\n```"})
        if upd.rowcount:
            done.append(str(nk))
    return done


async def hand_back_engine_half(db: AsyncSession, job_id: str, node: dict, run_result: dict) -> bool:
    """§17.1458 — the engine ran its half and the check failed: if the step names an operator part, the
    rest is theirs. Put the step back to pending; the next pause asks them. False when it is not that."""
    if not operators_part(node) or is_wholly_operators(node):
        return False
    upd = await db.execute(text(
        "UPDATE dag_nodes SET status = 'pending', updated_at = NOW() "
        "WHERE job_id = :jid AND node_key = :nk AND status = 'failed'"),
        {"jid": job_id, "nk": node.get("node_key")})
    if not upd.rowcount:
        return False
    logger.info("operator_step_engine_half_done job=%s node=%s run=%s why=%r", job_id, node.get("node_key"),
                (run_result or {}).get("outcome"), str((run_result or {}).get("reason") or "")[:160])
    return True
