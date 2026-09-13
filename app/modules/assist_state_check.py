"""§17.1050 — the state check: stop the walkthrough, verify what is actually
true on the operator's system against everything the plan believes, then
walk through the repairs.

Why. Live (homelab T37 "validate the entire build"): each paste revealed one
more thing the plan assumed done but that was not — container 111 stopped,
container 120 stopped, VM 110 stopped, the backend never installed as a
service. The engine answered each with one more fix and one more "reply
confirm" offer; the step never converged and the fix path looked like it
had forgotten itself. The missing move was to STOP and re-establish ground
truth as a whole, not one symptom at a time.

Two halves, both grounded and both surfaced through machinery that exists:

1. PROBE — the ledger's claims (committed steps' recorded results, facts,
   pins) become one read-only, copy-paste script. Every probe is prefixed
   with an ``== <claim id> ==`` marker so its output can be attributed
   deterministically; a probe that could write anything is refused by a
   deterministic gate (``read_only_command``) before the operator sees it.
   The pending check is stashed on the session like a completion-confirm.

2. JUDGE — the pasted output is split by the markers; a model records a
   verdict per claim (confirmed / contradicted / unknown, one-line reason);
   a verdict is accepted only for a claim that was probed and whose section
   is non-empty. Contradicted FACTS are retracted from the ledger (§17.725's
   path). Contradicted STEP results are STAGED as plan changes through the
   §17.677 proposal — ``reopen`` (the result is gone, redo the step) or
   ``repair`` (insert a step that puts it right, via §17.736 add_step) — the
   operator confirms, nothing is applied silently.

Entry: the 🩺 button / ``command='verify_state'``, the phrase "verify
state" / "state check", and — the point of it — an automatic offer once
several fixes on one step have not resolved it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.providers.base import Tool

logger = logging.getLogger("scaffold")

MARKER_RE = re.compile(r"^\s*==\s*([A-Z]:[A-Za-z0-9_.-]+)\s*==\s*$", re.M)
_MARKER_ECHO_RE = re.compile(r"echo\s+[\"']?==\s*[A-Z]:[A-Za-z0-9_.-]+\s*==[\"']?\s*$")
STATE_CHECK_PHRASE_RE = re.compile(
    r"(?i)^\s*(?:/assist\s+)?(?:verify|check|confirm)\s+(?:the\s+)?(?:current\s+)?(?:state|build|system|where\s+we\s+are)\b"
    r"|^\s*state\s*check\b|^\s*🩺")

# Commands that change state. A probe is READ-ONLY by construction; anything
# matching here is refused before the operator sees it (a safety list, not an
# answer key — like assist_guide._CONSUMING_MARKERS).
_MUTATION_RE = re.compile(
    r"(?<![\w-])(?:rm|rmdir|mv|cp|dd|mkfs\w*|fdisk|parted|truncate|chmod|chown|chattr|ln|tee|touch|mkdir|"
    r"kill|pkill|killall|reboot|shutdown|poweroff|halt|init|useradd|userdel|passwd|"
    r"apt(?:-get)?|dpkg|yum|dnf|pacman|zypper|snap|pip3?|npm|yarn|cargo|make|"
    r"git\s+(?:clone|pull|checkout|reset|push)|sed\s+-i|perl\s+-i|"
    r"systemctl\s+(?:start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload|edit|set-property)|"
    r"service\s+\S+\s+(?:start|stop|restart|reload)|"
    r"(?:pct|qm)\s+(?:start|stop|shutdown|reboot|create|destroy|set|resize|migrate|clone|template|snapshot|rollback|delsnapshot|unlock|restore|move\w*|resize)|"
    r"docker\s+(?:run|rm|rmi|stop|start|restart|kill|create|pull|push|build|exec\s+-it|compose\s+(?:up|down|restart|pull|rm))|"
    r"virsh\s+(?:start|destroy|shutdown|reboot|define|undefine|create)|"
    r"iptables|nft|ufw\s+(?:allow|deny|delete|enable|disable|reset)|firewall-cmd\s+--(?:add|remove|reload)|"
    r"ip\s+(?:link|addr|address|route|neigh)\s+(?:add|del|set|change|replace|flush)|"
    r"wg\s+set|wg-quick|nmcli\s+(?:con\w*|dev\w*)\s+(?:up|down|add|del|mod)|"
    r"zfs\s+(?:create|destroy|set|snapshot|rollback|send|receive)|zpool\s+(?:create|destroy|add|remove)|"
    r"crontab\s+-[er]|visudo|update-\w+|"
    r"curl\b[^\n|]*-X\s*(?:POST|PUT|DELETE|PATCH)|wget\b|"
    r"(?:>|>>)\s*(?!/dev/null|&\d)|\|\s*(?:sh|bash|zsh|python\d?)\b)"
)


def read_only_command(cmd: str) -> bool:
    """True when the command can only read. Conservative: any mutation verb,
    any redirect that is not to /dev/null, any pipe into a shell → False."""
    c = (cmd or "").strip()
    if not c or c.startswith("#"):
        return False
    if _MUTATION_RE.search(" " + c + " "):
        return False
    try:
        from app.modules.assist_guide import find_shell_unsafe_commands
        if find_shell_unsafe_commands("```bash\n" + c + "\n```"):
            return False
    except Exception:  # noqa: BLE001 — the local gate above stands on its own
        pass
    return True


# ---------------------------------------------------------------------------
# Claims — what the plan believes, with an id per claim.
# ---------------------------------------------------------------------------

async def build_claims(*, db, session_id: str, max_facts: int = 24) -> dict:
    """``{claims: [{id, kind, node_key?, title?, text}], job_id, environment}``.
    Claims come from committed steps (their recorded result), the facts
    ledger and the operator's pins. Deterministic; no model."""
    from app.modules.assist_environment import _environment_from_metadata
    from app.modules.assist_render import parse_recap
    sess = (await db.execute(text("""
        SELECT job_id, metadata, current_node_key FROM assist_sessions WHERE id = :sid
    """), {"sid": session_id})).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    job_id = str(sess["job_id"])
    env = _environment_from_metadata(sess.get("metadata"))
    rows = (await db.execute(text("""
        SELECT s.node_key, n.title, s.evidence, s.progress_recap, s.committed_at
          FROM assist_steps s JOIN dag_nodes n ON n.job_id = s.job_id AND n.node_key = s.node_key
         WHERE s.session_id = :sid AND s.status = 'committed'
         ORDER BY n.execution_order
    """), {"sid": session_id})).mappings().all()
    claims: list[dict] = []
    for r in rows:
        done = parse_recap(r.get("progress_recap")).get("done") or []
        summary = "; ".join(str(d) for d in done[:3]) if done else " ".join((r.get("evidence") or "").split())[:240]
        if not summary:
            summary = f"step completed: {r.get('title') or r['node_key']}"
        claims.append({"id": f"S:{r['node_key']}", "kind": "step", "node_key": r["node_key"],
                       "title": r.get("title") or "", "text": summary[:300]})
    facts = [str(f).strip() for f in (env.get("facts") or []) if str(f).strip()]
    for i, f in enumerate(facts[-max_facts:]):
        claims.append({"id": f"F:{i + 1}", "kind": "fact", "text": f[:300]})
    for k, v in (env.get("substitutions") or {}).items():
        claims.append({"id": f"K:{k}", "kind": "pin", "text": f"{k} = {v}"})
    return {"claims": claims, "job_id": job_id, "environment": env,
            "current_node_key": sess.get("current_node_key")}


# ---------------------------------------------------------------------------
# Probe — one read-only command per claim, gated, rendered as one script.
# ---------------------------------------------------------------------------

PLAN_PROBES_TOOL = Tool(
    name="plan_state_probes",
    description="For each claim about the operator's system, one READ-ONLY shell command whose output shows whether the claim still holds.",
    input_schema={
        "type": "object",
        "properties": {
            "probes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The claim id, verbatim."},
                        "command": {"type": "string",
                                    "description": "One read-only command (no writes, no service changes, no installs). Empty when the claim is not checkable from this shell."},
                        "expect": {"type": "string", "description": "One line: what the output shows when the claim holds."},
                    },
                    "required": ["id", "command"],
                },
            }
        },
        "required": ["probes"],
    },
)

_PROBE_OPENING = (
    "The operator is partway through a guided build and something the plan assumed done may no longer "
    "be true. Below are the claims the plan currently believes about their system, each with an id. For "
    "each claim, give ONE read-only shell command, runnable from the operator's usual shell, whose output "
    "shows whether the claim still holds now — status queries, listings, config reads, connectivity "
    "checks. Never a command that starts, stops, installs, writes, edits or deletes anything. Leave the "
    "command empty when a claim cannot be checked from a shell. Use the operator's actual context "
    "(host, containers, addresses) exactly as the claims state it."
)


async def plan_probes(claims: list[dict], environment: Optional[dict], *,
                      model_overrides: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """``(probes, refused)`` — probes the read-only gate accepted, and the
    ones it refused (logged, never shown). Fail-soft → ``([], [])``."""
    if not claims:
        return [], []
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    profile = str((environment or {}).get("profile") or "")
    claims_block = "\n".join(f"- {c['id']}: {c['text']}" for c in claims)
    msg = (_PROBE_OPENING + ("\n\nOPERATOR CONTEXT:\n" + profile[:600] if profile else "")
           + "\n\nCLAIMS:\n" + claims_block)
    try:
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": msg}], tools=[PLAN_PROBES_TOOL],
            role="model_general", overrides=model_overrides, temperature=0.0,
            tool_choice="auto", max_tokens=3000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("state_check_probe_model_failed: %r", exc)
        return [], []
    raw = ((read_tool_args(resp) or {}).get("probes")) or []
    by_id = {c["id"]: c for c in claims}
    probes: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        cmd = " ".join(str(item.get("command") or "").split())
        if cid not in by_id or cid in seen or not cmd:
            continue
        seen.add(cid)
        if not read_only_command(cmd):
            refused.append({"id": cid, "command": cmd[:120]})
            continue
        probes.append({"id": cid, "command": cmd[:300], "expect": str(item.get("expect") or "")[:200],
                       "claim": by_id[cid]["text"], "kind": by_id[cid]["kind"],
                       "node_key": by_id[cid].get("node_key")})
    logger.info("state_check_probes claims=%d probes=%d refused=%r", len(claims), len(probes),
                [(r["id"], r["command"][:50]) for r in refused][:4])
    return probes, refused


def render_probe_script(probes: list[dict]) -> str:
    lines = []
    for p in probes:
        lines.append(f'echo "== {p["id"]} =="')
        lines.append(p["command"])
    return "\n".join(lines)


def render_probe_message(probes: list[dict], *, checked: int, unchecked: int) -> str:
    if not probes:
        return ("🩺 **State check** — I found nothing here I can verify from a shell. Tell me in your own "
                "words what is and is not working and I will take it from there.")
    what = "\n".join(f"- `{p['id']}` {p['claim'][:110]}" for p in probes[:20])
    return (
        f"🩺 **State check — {len(probes)} thing{'s' if len(probes) != 1 else ''} the plan believes about your system.**\n"
        "Nothing here changes anything: every command only reads. Paste the whole block into your usual "
        "shell, then paste ALL of the output back here (keep the `== … ==` lines).\n\n"
        "```bash\n" + render_probe_script(probes) + "\n```\n\n"
        "What each line checks:\n" + what
        + (f"\n\n({unchecked} claim{'s' if unchecked != 1 else ''} cannot be checked from a shell and are left as they are.)" if unchecked else "")
    )


# ---------------------------------------------------------------------------
# Judge — attribute output by marker, model verdicts, deterministic gates.
# ---------------------------------------------------------------------------

def attribute_sections(pasted: str) -> dict[str, str]:
    """``{claim id: output text}`` split on the ``== id ==`` markers the
    script printed. Text before the first marker is ignored."""
    out: dict[str, str] = {}
    # The operator's shell echoes `echo "== id =="` (prompt + command) before
    # the marker itself prints; that echo belongs to no section.
    cleaned = "\n".join(ln for ln in (pasted or "").splitlines()
                        if not _MARKER_ECHO_RE.search(ln))
    parts = MARKER_RE.split(cleaned)
    # parts: [prefix, id1, text1, id2, text2, ...]
    for i in range(1, len(parts) - 1, 2):
        cid, body = parts[i].strip(), parts[i + 1]
        body_lines = [ln for ln in body.splitlines() if ln.strip()]
        out[cid] = ("\n".join(body_lines)).strip()
    return out


RECORD_VERDICTS_TOOL = Tool(
    name="record_state_verdicts",
    description="One verdict per probed claim, from its command output only.",
    input_schema={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["confirmed", "contradicted", "unknown"]},
                        "reason": {"type": "string", "description": "One line quoting the decisive output."},
                        "repair": {"type": "string",
                                   "description": "For a contradicted claim: what has to be done to make it true again, as a one-line task for the operator (empty when the step must simply be redone)."},
                        "caused_by": {"type": "string",
                                      "description": "For a contradicted claim that is a CONSEQUENCE of another contradicted claim (a service unreachable because its container is gone): that claim's id. Empty when this claim is its own cause."},
                    },
                    "required": ["id", "verdict"],
                },
            }
        },
        "required": ["verdicts"],
    },
)

_JUDGE_OPENING = (
    "For each claim below, decide from ITS command output only whether the claim still holds: "
    "confirmed (the output shows it), contradicted (the output shows it is not so — stopped, missing, "
    "refused, absent), or unknown (the output does not settle it, or errored for an unrelated reason). "
    "Quote the decisive part of the output in the reason. For a contradicted claim, say in one line what "
    "the operator must do to make it true again, using only what the output and the claim state. When "
    "several contradicted claims share ONE cause (a container that no longer exists makes its port, its "
    "mounts and its HTTP response all fail), name the root claim's id in `caused_by` on the others, so "
    "there is one repair, not three."
)


async def judge_outputs(probes: list[dict], pasted: str, *,
                        model_overrides: Optional[dict] = None) -> list[dict]:
    """Per-claim verdicts. A verdict is kept only for a probed claim whose
    section is non-empty; missing sections are ``unknown``. Fail-soft."""
    sections = attribute_sections(pasted)
    probed = {p["id"]: p for p in probes}
    verdicts: dict[str, dict] = {}
    for cid, p in probed.items():
        verdicts[cid] = {"id": cid, "verdict": "unknown", "reason": "no output pasted for this check",
                         "repair": "", "kind": p["kind"], "node_key": p.get("node_key"), "claim": p["claim"]}
    # A marker that is PRESENT with nothing under it is evidence too ("docker
    # ps … returns no rows"); only an absent marker is "no output pasted".
    with_output = {cid: (sections[cid] or "(no output — the command printed nothing)")
                   for cid in probed if cid in sections}
    if not with_output:
        return list(verdicts.values())
    # Deterministic pre-pass: the probe said what the output looks like when
    # the claim holds; when that text is literally there, it is confirmed.
    to_judge: dict[str, str] = {}
    for cid, txt in with_output.items():
        exp = " ".join((probed[cid].get("expect") or "").split()).lower()
        if len(exp) >= 6 and exp in " ".join(txt.split()).lower():
            verdicts[cid].update({"verdict": "confirmed", "reason": f"output contains the expected '{exp[:80]}'"})
        else:
            verdicts[cid]["reason"] = "the judge returned no verdict for this check"
            to_judge[cid] = txt
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    ids = list(to_judge)
    for start in range(0, len(ids), _JUDGE_BATCH):  # small batches: a long list gets truncated verdicts
        batch = ids[start:start + _JUDGE_BATCH]
        block = "\n\n".join(
            f"[{cid}] CLAIM: {probed[cid]['claim']}\nCOMMAND: {probed[cid]['command']}\n"
            f"EXPECTED WHEN TRUE: {probed[cid].get('expect') or '(unspecified)'}\nOUTPUT:\n{to_judge[cid][:1200]}"
            for cid in batch)
        try:
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": _JUDGE_OPENING + f"\n\nGive one verdict for EACH of the {len(batch)} claims, in order.\n\n" + block}],
                tools=[RECORD_VERDICTS_TOOL], role="model_general", overrides=model_overrides,
                temperature=0.0, tool_choice="auto", max_tokens=3000)
            raw = ((read_tool_args(resp) or {}).get("verdicts")) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("state_check_judge_model_failed: %r", exc)
            raw = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            v = str(item.get("verdict") or "").strip()
            if cid not in to_judge or v not in ("confirmed", "contradicted", "unknown"):
                continue
            verdicts[cid].update({"verdict": v, "reason": str(item.get("reason") or "")[:300],
                                  "repair": str(item.get("repair") or "")[:300],
                                  "caused_by": str(item.get("caused_by") or "").strip()})
    return list(verdicts.values())


_JUDGE_BATCH = 5


def render_verdicts(verdicts: list[dict]) -> str:
    n = {"confirmed": 0, "contradicted": 0, "unknown": 0}
    for v in verdicts:
        n[v["verdict"]] = n.get(v["verdict"], 0) + 1
    lines = [f"🩺 **State check result — {n['confirmed']} confirmed, {n['contradicted']} contradicted, {n['unknown']} unknown.**"]
    icon = {"confirmed": "✅", "contradicted": "❌", "unknown": "❔"}
    for v in sorted(verdicts, key=lambda x: ("contradicted", "unknown", "confirmed").index(x["verdict"])):
        lines.append(f"- {icon[v['verdict']]} `{v['id']}` {v['claim'][:90]}" + (f" — {v['reason'][:140]}" if v.get("reason") and v["verdict"] != "confirmed" else ""))
    bad = [v for v in verdicts if v["verdict"] == "contradicted"]
    if bad:
        lines.append("\nContradicted facts have been retracted from what I believe. The step results that no longer hold "
                     "are in the plan-change proposal below — confirm it and I will walk you through putting them right, "
                     "one step at a time, before we go on.")
    else:
        lines.append("\nEverything I could check still holds. Continuing from where we were.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration — stage, resolve, and hand structural changes to the confirm
# surface.
# ---------------------------------------------------------------------------

async def start_state_check(*, db, session_id: str, node_key: Optional[str]) -> dict:
    """Build claims → probes → stash ``pending_state_check`` → return the
    message for the operator. Raises ValueError on a bad session."""
    built = await build_claims(db=db, session_id=session_id)
    claims = built["claims"]
    probes, refused = await plan_probes(claims, built["environment"])
    pending = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node_key": node_key or built.get("current_node_key"),
        "probes": probes,
        "claims_total": len(claims),
    }
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id, "patch": json.dumps({"pending_state_check": pending})})
    await db.commit()
    logger.warning("state_check_started session_id=%s node_key=%s claims=%d probes=%d refused=%d",
                   session_id, pending["node_key"], len(claims), len(probes), len(refused))
    return {"message": render_probe_message(probes, checked=len(probes), unchecked=len(claims) - len(probes)),
            "probes": probes, "claims_total": len(claims), "refused": refused}


async def get_pending_state_check(*, db, session_id: str) -> Optional[dict]:
    try:
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        p = (meta or {}).get("pending_state_check") if isinstance(meta, dict) else None
        return p if isinstance(p, dict) and p.get("probes") else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("state_check_pending_read_failed sid=%s err=%r", session_id, exc)
        return None


async def clear_pending_state_check(*, db, session_id: str) -> None:
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = COALESCE(metadata, '{}'::jsonb) - 'pending_state_check', updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id})
    await db.commit()


def looks_like_probe_output(text_value: str) -> bool:
    return bool(MARKER_RE.search(text_value or ""))


async def resolve_state_check(*, db, session_id: str, pasted: str) -> dict:
    """Judge the pasted output, retract contradicted facts, stage the
    structural proposal, record the check on the session. Returns
    ``{message, verdicts, proposal, retracted}``."""
    pending = await get_pending_state_check(db=db, session_id=session_id)
    if not pending:
        return {"message": "", "verdicts": [], "proposal": None, "retracted": []}
    probes = pending.get("probes") or []
    verdicts = await judge_outputs(probes, pasted)
    contradicted = [v for v in verdicts if v["verdict"] == "contradicted"]
    retracted: list[str] = []
    if contradicted:
        facts = [v["claim"] for v in contradicted if v["kind"] == "fact"]
        if facts:
            try:
                from app.modules.assist_environment import set_environment
                await set_environment(session_id=session_id, db=db, retract_facts=facts,
                                      facts=[f"State check at {pending.get('node_key') or '?'}: NOT true any more — {v['claim'][:120]} ({v['reason'][:100]})"
                                             for v in contradicted if v["kind"] == "fact"])
                retracted = facts
            except Exception as exc:  # noqa: BLE001
                logger.warning("state_check_retract_failed sid=%s err=%r", session_id, exc)
    proposal = None
    affected = proposals_from_verdicts(verdicts, anchor_node_key=pending.get("node_key"))
    if affected:
        try:
            from app.modules.assist_notes import _stage_replan_proposal
            structural = [v for v in contradicted if v["kind"] == "step" or (v.get("repair") or "").strip()]
            note_text = (f"State check at {pending.get('node_key') or '?'}: "
                         + "; ".join(f"{v['id']} {v['reason'][:80]}" for v in structural)[:400])
            proposal = await _stage_replan_proposal(
                session_id=session_id, note_text=note_text, note_kind="state_check", affected=affected, db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("state_check_stage_failed sid=%s err=%r", session_id, exc)
    # record the check on the session (for the automatic-offer guard) and clear the pending
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "node_key": pending.get("node_key"),
              "confirmed": sum(1 for v in verdicts if v["verdict"] == "confirmed"),
              "contradicted": len(contradicted),
              "unknown": sum(1 for v in verdicts if v["verdict"] == "unknown")}
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'pending_state_check')
                          || jsonb_build_object('state_checks',
                               COALESCE(metadata->'state_checks', '[]'::jsonb) || CAST(:r AS jsonb)),
               updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id, "r": json.dumps(record)})
    await db.commit()
    logger.warning("state_check_resolved session_id=%s node_key=%s confirmed=%d contradicted=%d unknown=%d retracted=%d proposal=%s",
                   session_id, record["node_key"], record["confirmed"], record["contradicted"],
                   record["unknown"], len(retracted), bool(proposal))
    return {"message": render_verdicts(verdicts), "verdicts": verdicts, "proposal": proposal, "retracted": retracted}


def proposals_from_verdicts(verdicts: list[dict], *, anchor_node_key: Optional[str]) -> list[dict]:
    """The structural changes a state check proposes, for the confirm surface:
    a contradicted STEP result → ``repair`` (a step that puts it right, when
    the judge named one) or ``reopen`` (redo the step); a contradicted FACT
    with a named repair → ``repair`` anchored on the current step (the fact
    was distilled from the operator's own work, and the work is what has to
    be redone). A contradicted fact with no repair is only retracted."""
    out: list[dict] = []
    contradicted_ids = {v.get("id") for v in verdicts or [] if v.get("verdict") == "contradicted"}
    for v in verdicts or []:
        if v.get("verdict") != "contradicted":
            continue
        # a consequence of another contradicted claim is repaired by that claim's repair
        cb = (v.get("caused_by") or "").strip()
        if cb and cb != v.get("id") and cb in contradicted_ids:
            continue
        repair = (v.get("repair") or "").strip()
        if v.get("kind") == "step":
            if repair:
                out.append({"node_key": v.get("node_key"), "action": "repair",
                            "current_assumption": (v.get("claim") or "")[:300],
                            "proposed_change": repair[:400], "reason": (v.get("reason") or "")[:300]})
            else:
                out.append({"node_key": v.get("node_key"), "action": "reopen",
                            "current_assumption": (v.get("claim") or "")[:300],
                            "proposed_change": f"Redo this step — {(v.get('reason') or '')[:200]}"})
        elif repair and anchor_node_key:
            out.append({"node_key": anchor_node_key, "action": "repair",
                        "current_assumption": (v.get("claim") or "")[:300],
                        "proposed_change": repair[:400], "reason": (v.get("reason") or "")[:300]})
    # one repair per cause: fold repairs whose distinctive terms mostly overlap
    uniq: list[dict] = []
    for p in out:
        terms = _repair_terms(p["proposed_change"])
        dup = False
        for q in uniq:
            if q["action"] == p["action"] == "repair":
                qt = _repair_terms(q["proposed_change"])
                # overlap coefficient (against the smaller set): two wordings of
                # one repair share most of the shorter one's identifiers
                if terms and qt and len(terms & qt) / min(len(terms), len(qt)) >= 0.5:
                    dup = True
                    break
            elif q["action"] == p["action"] and q.get("node_key") == p.get("node_key") and q["proposed_change"] == p["proposed_change"]:
                dup = True
                break
        if not dup:
            uniq.append(p)
    return uniq


_REPAIR_STOP = frozenset({"the", "and", "with", "from", "that", "this", "then", "into", "using", "make", "sure",
                          "again", "true", "host", "port", "container", "service", "create", "start", "restart",
                          "run", "running", "listening", "returns", "mapped", "bound", "image", "named", "volume"})


def _repair_terms(text_value: str) -> set[str]:
    """Identifier-ish tokens; hyphenated names also contribute their parts
    ("uptime-kuma" → uptime, kuma) so "Uptime Kuma" and "uptime-kuma" agree."""
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9][a-z0-9_.:/-]{2,}", (text_value or "").lower()):
        if t in _REPAIR_STOP:
            continue
        out.add(t)
        for part in re.split(r"[-/:]", t):
            if len(part) >= 4 and part not in _REPAIR_STOP and not part.isdigit():
                out.add(part)
    return {t for t in out if len(t) >= 4}


async def state_check_done_on_step(*, db, session_id: str, node_key: Optional[str]) -> bool:
    try:
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        checks = (meta or {}).get("state_checks") if isinstance(meta, dict) else None
        return any(isinstance(c, dict) and c.get("node_key") == node_key for c in (checks or []))
    except Exception:  # noqa: BLE001
        return False


def offer_text(streak: int) -> str:
    return (f"🩺 We've tried {streak} fixes on this step without closing it. That usually means something the plan "
            "assumes is done has changed on the machine. Press **🩺 Verify state** (or reply `verify state`) and "
            "I'll check what is actually running against what the plan believes, then walk you through the "
            "repairs one step at a time.")
