"""§17.1466 — a run that moves a machine tells the plan where it went.

Live (2026-10-11): ADD154 switched VM 106 (palworld-server) from its fixed 192.168.1.106 to a router lease so the
router app could reserve it. The run worked and the VM came back as 192.168.1.44, and nothing else knew: ADD152's
walkthrough still said "reserve 192.168.1.106", and the control panel (built by ADD122) still opened its Palworld
settings over ssh to 192.168.1.106. §17.1449 carries an address change the SESSION observes (a pasted `ip addr`)
into the steps ahead; a change the engine's own run made never reached it.

Before a supervised run, the addresses of the guests it touches are read off the machine; after it is done, they
are read again. A guest that moved gets:

- a fact ("VM 106 (palworld-server) moved from 192.168.1.106 to 192.168.1.44 — measured after ADD154"),
- the pending steps that name the old address corrected (`plan_reconcile.reconcile_after_address_change`),
- and when DONE steps put the old address into something they set up, one new step ahead of the rest, to point
  those at the new one — a done step's work is on the machine, and editing its text changes nothing there.

Fail-soft throughout: a guest that cannot be read is not a move.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")

_GUEST_RE = re.compile(r"\b(?:(qm)\s+(?:guest\s+exec|set|start|reboot|shutdown|stop)|(pct)\s+(?:exec|set|start|reboot|"
                       r"stop))\s+(\d{3,5})\b|\bin_(vm|ct)_(\d{3,5})_")
_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/\d+\b[^\n]*\bscope\s+global\b")
MAX_GUESTS = 4


def guests_touched(commands: list[str], files: Optional[list[dict]] = None) -> dict[str, bool]:
    """``{guest id: is_vm}`` for every guest a block's commands (or written scripts) act on."""
    out: dict[str, bool] = {}
    blob = "\n".join([str(c) for c in commands or []]
                     + [str((f or {}).get("path") or "") + "\n" + str((f or {}).get("content") or "") for f in files or []])
    for m in _GUEST_RE.finditer(blob):
        if m.group(3):
            out.setdefault(m.group(3), m.group(1) == "qm")
        else:
            out.setdefault(m.group(5), m.group(4) == "vm")
    return dict(list(out.items())[:MAX_GUESTS])


def parse_addresses(out: str) -> list[str]:
    return list(dict.fromkeys(_INET_RE.findall(out or "")))


async def measure(spec, guests: dict[str, bool]) -> dict[str, list[str]]:
    """``{guest: [global IPv4s]}`` read off the machine; a guest that could not be read is absent."""
    from app.modules.service_truth import _probe, in_guest
    got: dict[str, list[str]] = {}
    for gid, vm in (guests or {}).items():
        ok, out = await _probe(spec, f"{in_guest(gid, vm)} ip -4 addr show scope global")
        addrs = parse_addresses(out) if ok else []
        if addrs:
            got[gid] = addrs
    return got


def moves(before: dict[str, list[str]], after: dict[str, list[str]]) -> list[dict]:
    """``[{id, old, new}]`` — a guest whose only address before is gone after, replaced by one new one."""
    out = []
    for gid, old in (before or {}).items():
        new = (after or {}).get(gid)
        if not new or len(old) != 1 or old[0] in new:
            continue
        fresh = [a for a in new if a not in old]
        if len(fresh) == 1:
            out.append({"id": gid, "old": old[0], "new": fresh[0]})
    return out


def _label(gid: str, vm: bool, name: str) -> str:
    return f"{'VM' if vm else 'CT'} {gid}" + (f" ({name})" if name else "")


def _addr_re(addr: str) -> re.Pattern:
    return re.compile(rf"(?<![\d.]){re.escape(addr)}(?![\d])")


async def carry(db, job_id: str, node_key: str, moved: list[dict], guests: dict[str, bool]) -> dict:
    """Record each move, correct the steps ahead, and add one step for what done steps set up."""
    from app.modules import plan_reconcile as _pr
    from app.modules.machine_truth import guest_name_from_plan
    plan = [dict(r) for r in (await db.execute(text(f"""
        SELECT node_key, title, status, output_text, execution_order, {_pr.STEP_TEXT_SQL}
          FROM dag_nodes WHERE job_id = :jid ORDER BY execution_order
    """), {"jid": job_id})).mappings().all()]
    changes, facts, stale = [], [], []
    for m in moved:
        label = _label(m["id"], guests.get(m["id"], False), guest_name_from_plan(plan, m["id"]))
        changes.append({"id": m["id"], "label": label, "old": m["old"], "new": m["new"]})
        facts.append(f"{label} moved from {m['old']} to {m['new']} — measured on the machine after {node_key}.")
        rx = _addr_re(m["old"])
        for n in plan:
            if n["status"] == "done" and n["node_key"] != node_key and rx.search(str(n.get("output_text") or "")):
                stale.append((n, m, label))
    sess = (await db.execute(text("SELECT id FROM assist_sessions WHERE job_id = :jid ORDER BY updated_at DESC LIMIT 1"),
                             {"jid": job_id})).scalar()
    reconciled = None
    if sess:
        from app.modules.assist_environment import set_environment
        await set_environment(session_id=str(sess), facts=facts, db=db)
        reconciled = await _pr.reconcile_after_address_change(db=db, session_id=str(sess), job_id=job_id,
                                                             changes=changes)
    added = await _add_repoint_step(db, job_id, node_key, plan, stale) if stale else None
    await db.commit()
    logger.warning("address_watch_carried job=%s node=%s moved=%r stale=%r added=%s", job_id, node_key,
                   [(c["label"], c["old"], c["new"]) for c in changes], [s[0]["node_key"] for s in stale], added)
    return {"moved": changes, "facts": facts, "reconciled": bool(reconciled), "added": added,
            "stale": [s[0]["node_key"] for s in stale]}


async def _add_repoint_step(db, job_id: str, node_key: str, plan: list[dict], stale: list) -> Optional[str]:
    keys = {str(n["node_key"]) for n in plan}
    i = 1
    while f"ADR{i}" in keys:
        i += 1
    new_key = f"ADR{i}"
    by_move: dict[tuple, list[str]] = {}
    for n, m, label in stale:
        by_move.setdefault((label, m["old"], m["new"]), []).append(f"{n['node_key']} ({n.get('title') or ''})")
    title = "Point what earlier steps set up at the new address of " + ", ".join(k[0] for k in by_move)
    lines = []
    for (label, old, new), steps in by_move.items():
        lines.append(f"- {label} moved from {old} to {new} (measured after {node_key}). These done steps set "
                     f"something up that names {old}: {'; '.join(steps)}. Find each place on the machine that "
                     f"holds {old} (config files, scripts, unit files, proxy targets, firewall rules), replace it "
                     f"with {new} and restart whatever reads it.")
    desc = (f"{node_key} changed a machine's address. " + "\n".join(lines)
            + "\n\nDone when nothing those steps set up still names the old address (grep for it) and each "
              "service that read it answers again.")
    from app.modules.node_editor import insert_node
    res = await insert_node(job_id, {"node_key": new_key, "title": title[:240], "description": desc, "tool": "Shell",
                                     "node_type": "task", "depends_on": [node_key]},
                            edited_by=f"engine (address watch after {node_key})", db=db)
    if res.get("status") != "ok":
        logger.warning("address_watch_insert_failed job=%s err=%r", job_id, res.get("error"))
        return None
    return new_key


async def around_run(spec, commands: list[str], files: Optional[list[dict]] = None) -> tuple[dict, dict]:
    """``(guests, before)`` — read before the run starts."""
    try:
        guests = guests_touched(commands, files)
        return guests, (await measure(spec, guests) if guests else {})
    except Exception as exc:
        logger.warning("address_watch_before_failed err=%r", exc)
        return {}, {}


async def after_run(db, spec, job_id: str, node_key: str, guests: dict, before: dict) -> Optional[dict]:
    if not before:
        return None
    try:
        moved = moves(before, await measure(spec, {g: guests[g] for g in before}))
        return await carry(db, job_id, node_key, moved, guests) if moved else None
    except Exception as exc:
        logger.warning("address_watch_after_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return None
