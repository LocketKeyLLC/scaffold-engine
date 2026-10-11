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


async def carry(db, job_id: str, node_key: str, moved: list[dict], guests: dict[str, bool], spec=None) -> dict:
    """Record each move, correct the steps ahead, and add one step for what done steps set up."""
    from app.modules import plan_reconcile as _pr
    from app.modules.machine_truth import guest_name_from_plan
    # §17.1468 — a move already carried is not carried again. Re-running ADD154 (VM 106 to DHCP, after a host
    # restart undid it) measures the same .106 → .44 again; a second reconcile would rewrite ADR1's own
    # "moved from 192.168.1.106 to 192.168.1.44" into ".44 to .44", and a second repoint step would be added.
    known = await _known_moves(db, job_id)
    moved = [m for m in moved if (m["id"], m["old"], m["new"]) not in known]
    if not moved:
        return {"moved": [], "facts": [], "reconciled": False, "added": None, "stale": [], "already_carried": True}
    plan = [dict(r) for r in (await db.execute(text(f"""
        SELECT node_key, title, status, output_text, execution_order, depends_on, {_pr.STEP_TEXT_SQL}
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
    added = await _add_repoint_step(db, job_id, node_key, plan, stale, spec) if stale else None
    await db.commit()
    logger.warning("address_watch_carried job=%s node=%s moved=%r stale=%r added=%s", job_id, node_key,
                   [(c["label"], c["old"], c["new"]) for c in changes], [s[0]["node_key"] for s in stale], added)
    return {"moved": changes, "facts": facts, "reconciled": bool(reconciled), "added": added,
            "stale": [s[0]["node_key"] for s in stale]}


_MOVED_FACT_RE = re.compile(r"\b(?:VM|CT)\s+(\d{3,5})\b[^\n]*?\bmoved from\s+(\S+)\s+to\s+(\S+?)\s+—\s+measured")


async def _known_moves(db, job_id: str) -> set[tuple[str, str, str]]:
    row = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE job_id = :jid ORDER BY updated_at DESC "
                                 "LIMIT 1"), {"jid": job_id})).mappings().first()
    facts = (((row or {}).get("metadata") or {}).get("environment") or {}).get("facts") or []
    out = set()
    for f in facts:
        t = str(f if isinstance(f, str) else (f or {}).get("text") or "")
        m = _MOVED_FACT_RE.search(t)
        if m:
            out.add((m.group(1), m.group(2), m.group(3)))
    return out


async def _add_repoint_step(db, job_id: str, node_key: str, plan: list[dict], stale: list, spec=None) -> Optional[str]:
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
    checks_ = stale_checks(stale)
    found = await where_it_is(spec, checks_) if spec is not None else []
    if found:
        lines.append("Measured when this step was added (each place that still holds the old address):\n"
                     + "\n".join(f"  - {f}" for f in found))
    desc = (f"{node_key} changed a machine's address. " + "\n".join(lines)
            + "\n\nDone when nothing those steps set up still names the old address and each service that read "
              "it answers again — each of these prints nothing: "
            + ", ".join(f"`{c}`" for c in checks_))
    from app.modules.node_editor import insert_node
    res = await insert_node(job_id, {"node_key": new_key, "title": title[:240], "description": desc, "tool": "Shell",
                                     "node_type": "task", "depends_on": [node_key]},
                            edited_by=f"engine (address watch after {node_key})", db=db)
    if res.get("status") != "ok":
        logger.warning("address_watch_insert_failed job=%s err=%r", job_id, res.get("error"))
        return None
    # §17.1468 — a step that builds on the work being repointed waits for the repoint. Live: ADR1 was appended
    # with nothing waiting on it, and T37 "Validate entire build" (which depends on ADD151, one of the stale
    # steps) was drafted while the panel still reached the Palworld VM at its old address.
    for nk in waits_for_repoint(plan, {str(n["node_key"]) for n, _m, _l in stale}):
        await db.execute(text(
            "UPDATE dag_nodes SET depends_on = array_append(COALESCE(depends_on, ARRAY[]::text[]), :new), "
            "updated_at = NOW() WHERE job_id = :jid AND node_key = :nk AND status = 'pending' "
            "AND NOT (:new = ANY(COALESCE(depends_on, ARRAY[]::text[])))"),
            {"jid": job_id, "nk": nk, "new": new_key})
    return new_key


_GREP_ROOTS = "/etc /opt /srv /home /root"
_GREP_SKIP = ("--exclude-dir=node_modules --exclude-dir=.git --exclude-dir=logs --exclude='*.bak*' --exclude='*.orig' "
              "--exclude='*.old' --exclude='*.log'")


_WHERE_RE = re.compile(r"^(?:(pct)\s+exec|(qm)\s+guest\s+exec)\s+(\d+)")


async def where_it_is(spec, checks_: list[str]) -> list[str]:
    """§17.1471 — run the repoint's own checks once and name every file they find, by machine.

    Live (2026-10-11): ADR1 said "find each place on the machine that holds 192.168.1.106"; the drafter, not told
    where, wrote `sed -i 's/…/…/g' <FILE_PATH_1> <FILE_PATH_2> <FILE_PATH_3>` on the Proxmox host (refused). The engine
    had already measured the places — the same greps found palworld_settings_route.js, remote.js and the panel's
    known_hosts in LXC 111."""
    from app.modules.service_truth import _probe
    out: list[str] = []
    for c in checks_:
        try:
            ok, text_ = await _probe(spec, c)
        except Exception:
            continue
        if not ok:
            continue
        m = _WHERE_RE.match(c)
        where = (f"LXC {m.group(3)}" if m.group(1) else f"VM {m.group(3)}") if m else "the Proxmox host"
        for ln in (text_ or "").splitlines():
            ln = ln.strip()
            if ln.startswith("/"):
                out.append(f"{where}: {ln}")
    return out[:20]


def stale_checks(stale: list) -> list[str]:
    """§17.1468 — one read per place the stale steps worked: a grep for the old address that must print nothing.
    Live: ADR1's "grep for it" named no command, so the frame refused it ("names no command the engine can run")."""
    from app.modules.machine_truth import subject_guest
    from app.modules.service_truth import in_guest
    out: list[str] = []
    for n, m, _label in stale:
        old = m["old"]
        gid = subject_guest(n)
        if gid:
            vm = bool(re.search(rf"\bVM\s*{gid}\b", str(n.get("title") or "")))
            if not vm:
                vm = gid == m["id"] and bool(re.search(rf"\bVM\s*{gid}\b", _label))
            c = (f"{in_guest(gid, vm)} sh -c \"grep -rIl {_GREP_SKIP} -e '{old}' {_GREP_ROOTS} 2>/dev/null; true\"")
        else:
            c = f"grep -rIl -e '{old}' /etc/pve/firewall /etc/hosts 2>/dev/null; true"
        if c not in out:
            out.append(c)
    return out[:6]


def waits_for_repoint(plan: list[dict], stale_keys: set[str]) -> list[str]:
    """The pending steps whose dependencies reach, directly or through others, a step being repointed."""
    deps = {str(n["node_key"]): [str(d) for d in (n.get("depends_on") or [])] for n in plan}
    out = []
    for n in plan:
        if n.get("status") != "pending":
            continue
        seen, todo = set(), list(deps.get(str(n["node_key"]), []))
        while todo:
            k = todo.pop()
            if k in seen:
                continue
            seen.add(k)
            todo.extend(deps.get(k, []))
        if seen & stale_keys:
            out.append(str(n["node_key"]))
    return out


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
        return await carry(db, job_id, node_key, moved, guests, spec) if moved else None
    except Exception as exc:
        logger.warning("address_watch_after_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return None
