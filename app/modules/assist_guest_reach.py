"""§17.1154 — "can the engine reach that guest?" is a question the engine
answers itself, from the host, before it asks the operator to look.

Live (ADD49, 2026-09-20 22:05–23:18): `ssh aedefruscio@192.168.1.127 …` →
"No route to host". The engine had VM 110's MAC (net0 in the system map),
its boot order (scsi0;ide2), the Ubuntu ISO on ide2, and a connected runner
on the Proxmox host — and still spent an hour sending the operator to the
VM console to read `ip a`, repeating `qm guest cmd` it had already run, and
retrying the same ssh. Everything needed was one read-only block on the host:

    qm status 110
    qm guest cmd 110 network-get-interfaces
    ip neigh show | grep -i BC:24:11:B4:AF:15
    bridge fdb show | grep -i BC:24:11:B4:AF:15
    ping -c 1 -W 2 192.168.1.127
    nc -z -w 2 192.168.1.127 22

The verdict is deterministic: stopped → start it; no address anywhere for
that MAC → the guest has no network identity (with the ISO on ide2 and boot
order scsi0;ide2, that is a VM that has not finished installing its OS) →
the OS install is the step; an address that differs from the assumed one →
say which; an address that answers ping but not port 22 → sshd is the
step. When a finding names work on the target, it becomes a PLAN STEP
(§17.1149 — never an error card), inserted before the current step, and
its paste is verified by re-running these probes.

Read-only at both gates; runs through the local runner when one is
connected, else the block is handed to the operator as any other look-up.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")

GUEST_MARK = "Engine guest check:"

_SSH_FAIL_RE = re.compile(
    r"(?im)^\s*ssh:\s*connect to host\s+(?P<ip>[0-9a-f.:]+)\s+port\s+(?P<port>\d+):\s*(?P<why>No route to host|Connection refused|Connection timed out|Network is unreachable)")
_SSH_CMD_RE = re.compile(r"\bssh\s+(?:-\S+\s+)*(?:(?P<user>[a-z_][a-z0-9_-]*)@)?(?P<host>[0-9]{1,3}(?:\.[0-9]{1,3}){3})\b", re.I)
_GUEST_ID_RE = re.compile(r"\b(?:VM|CT|LXC|container|vmid)\s*#?\s*(\d{3,4})\b", re.I)
_IPV4_RE = re.compile(r"\b(?!127\.)(\d{1,3}(?:\.\d{1,3}){3})\b")
_SYMPTOM = {"No route to host": "no_route", "Connection refused": "refused", "Connection timed out": "timeout",
            "Network is unreachable": "no_route"}


def detect(text_value: str) -> Optional[dict]:
    """``{ip, port, user, symptom}`` when the text carries an ssh connection
    failure to a guest, else None. The paste's own ssh line supplies the user."""
    t = text_value or ""
    m = _SSH_FAIL_RE.search(t)
    if not m:
        return None
    user = None
    for c in _SSH_CMD_RE.finditer(t):
        if c.group("host") == m.group("ip"):
            user = c.group("user")
            break
    return {"ip": m.group("ip"), "port": int(m.group("port")), "user": user, "symptom": _SYMPTOM.get(m.group("why"), "unknown")}


def find_guest(system_state: dict, *texts: str) -> Optional[dict]:
    """The guest the conversation is about: an id named in the step or the
    message that exists in the system map (preferred), else a map entry
    whose name/hostname is mentioned. ``{id, kind, name, mac, boot, iso}``."""
    sm = system_state if isinstance(system_state, dict) else {}
    blob = "\n".join(t or "" for t in texts)
    ids = [gid for gid in _GUEST_ID_RE.findall(blob) if gid in sm]
    gid = ids[0] if ids else None
    if gid is None:
        low = blob.lower()
        for k, v in sm.items():
            if not isinstance(v, dict) or v.get("kind") not in ("vm", "ct"):
                continue
            attrs = v.get("attrs") or {}
            for nm in (attrs.get("name"), attrs.get("hostname"), attrs.get("_listed_name")):
                if nm and str(nm).lower() in low:
                    gid = k
                    break
            if gid:
                break
    if gid is None:
        return None
    v = sm.get(gid) or {}
    attrs, devs = (v.get("attrs") or {}), (v.get("devices") or {})
    mac = None
    for dk, dv in devs.items():
        if str(dk).startswith("net"):
            mm = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", str(dv))
            if mm:
                mac = mm.group(1).upper()
                break
    iso = next((str(dv) for dk, dv in devs.items() if "iso/" in str(dv)), None)
    return {"id": str(gid), "kind": v.get("kind") or "vm", "name": attrs.get("name") or attrs.get("hostname") or "",
            "mac": mac, "boot": attrs.get("boot"), "iso": iso, "status": attrs.get("status")}


def probe_commands(guest: dict, ip: str, port: int = 22) -> list[tuple[str, str]]:
    """``[(id, command)]`` — read-only on the HOST (both gates allow every one)."""
    gid, mac = guest["id"], guest.get("mac")
    out: list[tuple[str, str]] = []
    if guest.get("kind") == "ct":
        out += [("status", f"pct status {gid}"),
                ("addr", f"pct exec {gid} -- ip -4 -brief addr 2>&1 | head -10"),
                ("sshd", f"pct exec {gid} -- ss -tlnp 2>/dev/null | grep ':{port} '")]
    else:
        out += [("status", f"qm status {gid}"),
                ("agent", f"qm guest cmd {gid} network-get-interfaces 2>&1 | head -60")]
    if mac:
        out += [("neigh", f"ip neigh show 2>/dev/null | grep -i {mac}"),
                ("fdb", f"bridge fdb show 2>/dev/null | grep -i {mac}")]
    out += [("ping", f"ping -c 1 -W 2 {ip} 2>&1 | tail -2"),
            ("port", f"nc -z -w 2 {ip} {port} 2>&1; echo rc=$?")]
    return out


def _agent_ips(text_value: str) -> list[str]:
    """IPv4 addresses from `qm guest cmd … network-get-interfaces` (JSON), non-loopback."""
    t = text_value or ""
    ips: list[str] = []
    try:
        data = json.loads(t[t.index("["):]) if "[" in t else []
        for iface in data if isinstance(data, list) else []:
            for a in (iface.get("ip-addresses") or []):
                if a.get("ip-address-type") == "ipv4" and not str(a.get("ip-address", "")).startswith("127."):
                    ips.append(a["ip-address"])
    except Exception:
        ips = [ip for ip in _IPV4_RE.findall(t) if not ip.startswith("127.")]
    return ips


def judge(guest: dict, ip: str, outputs: dict[str, str]) -> dict:
    """Deterministic verdict from the probe outputs: ``{class, found_ip,
    detail, checks}`` — class ∈ stopped | no_ip | ip_differs | no_ssh |
    reachable | unknown."""
    o = {k: (v or "").strip() for k, v in outputs.items()}
    checks: dict[str, str] = {}
    status = o.get("status", "")
    running = bool(re.search(r"status:\s*running", status, re.I))
    stopped = bool(re.search(r"status:\s*stopped", status, re.I))
    checks["status"] = "running" if running else "stopped" if stopped else "unknown"
    if stopped:
        return {"class": "stopped", "found_ip": None, "checks": checks,
                "detail": f"{_label(guest)} is stopped — nothing at {ip} can answer until it runs."}
    agent = o.get("agent", "")
    agent_ips = _agent_ips(agent) if agent and "not configured" not in agent.lower() and "not running" not in agent.lower() else []
    checks["agent"] = ", ".join(agent_ips) if agent_ips else ("no guest agent" if agent else "n/a")
    addr_ips = [i for i in _IPV4_RE.findall(o.get("addr", "")) if not i.startswith("127.")]
    if addr_ips:
        checks["addr"] = ", ".join(addr_ips)
    neigh_ips = _IPV4_RE.findall(o.get("neigh", ""))
    checks["neigh"] = ", ".join(neigh_ips) if neigh_ips else ("absent" if guest.get("mac") else "n/a")
    checks["fdb"] = "present" if o.get("fdb") else ("absent" if guest.get("mac") else "n/a")
    ping_ok = bool(re.search(r"\b1 (?:packets )?received", o.get("ping", "")))
    checks["ping"] = "answers" if ping_ok else "no answer"
    port_ok = "rc=0" in o.get("port", "")
    port_known = "rc=" in o.get("port", "") and "not found" not in o.get("port", "")
    checks["port"] = "open" if port_ok else ("closed" if port_known else "unknown")
    found = agent_ips or addr_ips or neigh_ips
    if port_ok:
        return {"class": "reachable", "found_ip": ip, "checks": checks,
                "detail": f"{ip} accepts connections on the ssh port — {_label(guest)} is reachable; the failure was transient or on the client side."}
    if found and ip not in found:
        return {"class": "ip_differs", "found_ip": found[0], "checks": checks,
                "detail": f"{_label(guest)} has the address {found[0]}, not {ip} — the plan's address is wrong."}
    if found and (ping_ok or port_known):
        return {"class": "no_ssh", "found_ip": found[0], "checks": checks,
                "detail": f"{_label(guest)} is at {found[0]} and {'answers ping' if ping_ok else 'has an address'} but nothing listens on port 22 — no SSH server inside it yet."}
    if not found and (checks["status"] == "running" or not status) and (guest.get("mac") or agent):
        hint = ""
        if guest.get("iso") and guest.get("boot"):
            hint = (f" It boots `{guest['boot']}` with `{guest['iso'].split(',')[0]}` on the CD-ROM: if its disk has no operating "
                    f"system yet, it is sitting in the installer — which is exactly what these results look like.")
        return {"class": "no_ip", "found_ip": None, "checks": checks,
                "detail": (f"{_label(guest)} is running but has NO address anywhere the host can see: the guest agent does not "
                           f"answer, and its MAC {guest.get('mac') or '(unknown)'} appears in neither the neighbour table nor the bridge's "
                           f"forwarding table. It has no network identity yet.{hint}")}
    return {"class": "unknown", "found_ip": found[0] if found else None, "checks": checks,
            "detail": f"the checks did not settle it (status {checks['status']}, agent {checks['agent']}, neighbour {checks['neigh']})."}


def _label(guest: dict) -> str:
    kind = "VM" if guest.get("kind") != "ct" else "container"
    return f"{kind} {guest['id']}" + (f" ({guest['name']})" if guest.get("name") else "")


def render(guest: dict, ip: str, verdict: dict, outputs: dict[str, str], *, ran_where: str) -> str:
    rows = "\n".join(f"- `{cmd}` → {(outputs.get(pid) or '(no output)').strip().splitlines()[0][:110] if (outputs.get(pid) or '').strip() else '(no output)'}"
                     for pid, cmd in probe_commands(guest, ip))
    head = f"## 🔎 Can the host reach {_label(guest)}?\n\n**Checked from {ran_where}:**\n{rows}\n\n**Finding:** {verdict['detail']}\n"
    return head


def plan_step(guest: dict, ip: str, verdict: dict) -> Optional[dict]:
    """The step a finding turns into — or None when nothing on the target is
    needed (reachable / ip_differs / unknown)."""
    cls = verdict.get("class")
    gid, lbl, mac = guest["id"], _label(guest), guest.get("mac") or "<its MAC>"
    recheck = (f"Done when the engine's re-check finds an address for {lbl}: `qm guest cmd {gid} network-get-interfaces` "
               f"reports an IPv4 address, or `ip neigh show | grep -i {mac}` on the host lists one — I re-run these checks "
               f"from the host on every paste.")
    if cls == "stopped":
        cmd = f"qm start {gid}; sleep 20; qm status {gid}" if guest.get("kind") != "ct" else f"pct start {gid}; sleep 5; pct status {gid}"
        return {"title": f"Start {lbl}",
                "description": (f"On the Proxmox host shell: start the guest and wait for it.\n```bash\n{cmd}\n```\n"
                                f"Done when the status line says running — I re-run the reachability checks from the host on every paste."
                                f"\n\n_{GUEST_MARK} {guest.get('kind') or 'vm'} {gid} stopped_")}
    if cls == "no_ip":
        iso = (guest.get("iso") or "").split(",")[0].replace("local:iso/", "")
        return {"title": f"Install the operating system on {lbl} from the attached ISO (Proxmox console)",
                "description": (f"{lbl} has no network identity yet — it is booting the installer ({iso or 'the attached ISO'}) and "
                                f"has not been installed. This is done in the Proxmox web console, not the shell: open "
                                f"https://<proxmox host>:8006, select {gid}, click Console, and complete the installer (keep the "
                                f"defaults for storage, enable the OpenSSH server when offered, create your user). When it reboots into the "
                                f"installed system and shows a login prompt, come back here and paste what the console shows.\n"
                                f"{recheck}\n\n_{GUEST_MARK} {guest.get('kind') or 'vm'} {gid} no_ip_")}
    if cls == "no_ssh":
        found = verdict.get("found_ip") or ip
        return {"title": f"Enable the SSH server on {lbl} ({found})",
                "description": (f"{lbl} is at {found} but nothing listens on port 22. In its console (Proxmox web UI → {gid} → Console), "
                                f"log in and run: `sudo apt-get install -y openssh-server && sudo systemctl enable --now ssh`. "
                                f"Done when the engine's re-check finds port 22 open at {found} — I re-run the checks from the host on every paste."
                                f"\n\n_{GUEST_MARK} {guest.get('kind') or 'vm'} {gid} no_ssh_")}
    return None


def guest_step_class(description: Optional[str]) -> Optional[tuple[str, str, str]]:
    m = re.search(re.escape(GUEST_MARK) + r" (vm|ct) (\d+) ([a-z_]+)_", description or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


async def _session_bits(db, session_id: str, node_key: Optional[str]) -> tuple[dict, str, str]:
    """(system_state, step text, facts text) for a session/step."""
    from app.modules.assist_environment import _environment_from_metadata
    row = (await db.execute(text("SELECT job_id, metadata, current_node_key FROM assist_sessions WHERE id = :s"),
                            {"s": session_id})).mappings().first()
    if not row:
        return {}, "", ""
    env = _environment_from_metadata(row.get("metadata"))
    nk = node_key or row.get("current_node_key")
    step_text = ""
    if nk:
        d = (await db.execute(text("SELECT title || E'\\n' || COALESCE(description, '') FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
                              {"j": str(row["job_id"]), "k": nk})).scalar()
        step_text = d or ""
    facts = "\n".join(str(f) for f in (env.get("facts") or []))
    return env.get("system_state") or {}, step_text, facts


async def run_probes(db, guest: dict, ip: str, port: int) -> Optional[tuple[dict[str, str], str, list[dict]]]:
    """Through the connected runner: ``(outputs by id, runner name, executed)``;
    None when no runner is connected."""
    from app.modules import assist_local_runner as _lr
    spec = await _lr.runner_spec(db)
    if spec is None:
        return None
    probes = [{"id": pid, "command": cmd} for pid, cmd in probe_commands(guest, ip, port)]
    pasted, executed = await _lr.run_probes(spec, probes)
    outputs: dict[str, str] = {}
    for m in re.finditer(r"^== (\w+) ==\n(.*?)(?=^== \w+ ==\n|\Z)", pasted or "", re.S | re.M):
        outputs[m.group(1)] = m.group(2)
    return outputs, spec.name, executed


async def open_guest_step(db, session_id: str, gid: str, cls: str) -> Optional[str]:
    row = (await db.execute(text("""
        SELECT d.node_key FROM assist_steps s JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
         WHERE s.session_id = :sid AND d.description LIKE :mark AND s.status NOT IN ('committed', 'skipped', 'handed_off')
         ORDER BY d.node_key LIMIT 1
    """), {"sid": session_id, "mark": f"%{GUEST_MARK} % {gid} {cls}_%"})).mappings().first()
    return row["node_key"] if row else None


async def check_and_act(*, db, session_id: str, node_key: Optional[str], error_text: str,
                        capture_reply: bool = True) -> Optional[dict]:
    """The whole thing, for one error paste: detect → find the guest → probe
    through the runner → judge → render → (insert the step the finding
    names, presented, before the current step) → record. Returns
    ``{text, verdict, step, guest}`` or None when this is not an ssh-to-guest
    failure, the guest is unknown, or no runner is connected (then ``text``
    is the block for the operator to run, and ``step`` is None)."""
    det = detect(error_text)
    if not det:
        return None
    system_state, step_text, facts = await _session_bits(db, session_id, node_key)
    guest = find_guest(system_state, step_text, error_text, facts)
    if not guest:
        logger.info("guest_reach_no_guest sid=%s ip=%s", session_id, det["ip"])
        return None
    ran = await run_probes(db, guest, det["ip"], det["port"])
    if ran is None:
        cmds = "\n".join(c for _i, c in probe_commands(guest, det["ip"], det["port"]))
        txt = (f"## 🔎 Can the host reach {_label(guest)}?\n\n📍 On: the Proxmox host shell\n\n**Run this now:**\n\n```bash\n{cmds}\n```\n\n"
               f"Paste what it prints — it tells me whether {_label(guest)} is running, what address it has (if any), and whether "
               f"port {det['port']} answers, without opening its console.")
        logger.info("guest_reach_rendered_for_operator sid=%s guest=%s", session_id, guest["id"])
        if capture_reply:
            await _persist(db, session_id, node_key, txt)
        return {"text": txt, "verdict": None, "step": None, "guest": guest}
    outputs, runner_name, executed = ran
    verdict = judge(guest, det["ip"], outputs)
    txt = render(guest, det["ip"], verdict, outputs, ran_where=f"the Proxmox host, through your local runner ({runner_name})")
    try:
        from app.modules import assist_agent as _aa
        rec = (f"[local-runner] the engine checked whether {_label(guest)} is reachable ({len(executed)} read-only commands on the host):\n"
               + "\n".join(f"$ {e['command']}\n{(outputs.get(e['id']) or '(no output)').rstrip()}" for e in executed))
        await _aa.ingest_turn(session_id=session_id, role="operator", kind="message", content=rec, node_key=node_key, db=db)
    except Exception:
        logger.warning("guest_reach_record_failed sid=%s", session_id)
    step_key = None
    st = plan_step(guest, det["ip"], verdict)
    if st:
        existing = await open_guest_step(db, session_id, guest["id"], verdict["class"])
        if existing:
            step_key = existing
            txt += f"\n\nThe step for this — **{existing}** — is already in the plan; that is where we continue."
        else:
            from app.modules import assist_notes, engine_setup as _es
            anchor = node_key
            try:
                res = await assist_notes.add_step(session_id=session_id, request=st["title"], before_node_key=anchor, steps=[st], db=db)
                step_key = (res or {}).get("node_key")
                if step_key:
                    await _es._present(db, session_id, step_key)
                    txt += f"\n\nSo I added **{step_key}: {st['title']}** to the plan, right before the current step — that is the next thing to do."
            except Exception as exc:
                logger.warning("guest_reach_step_failed sid=%s err=%r", session_id, exc)
    elif verdict["class"] == "ip_differs":
        try:
            from app.modules.assist_environment import set_environment
            await set_environment(session_id=session_id, db=db, facts=[
                f"{_label(guest)} answers at {verdict['found_ip']}, not {det['ip']} (found by the engine's reachability check on the host)."])
            txt += f"\n\nRecorded: {_label(guest)} is at **{verdict['found_ip']}**. Use that address from here on."
        except Exception as exc:
            logger.warning("guest_reach_fact_failed sid=%s err=%r", session_id, exc)
    elif verdict["class"] == "reachable":
        txt += f"\n\nTry the ssh again now — the host reaches {det['ip']}:{det['port']}."
    logger.info("guest_reach sid=%s guest=%s class=%s step=%s", session_id, guest["id"], verdict["class"], step_key)
    if capture_reply:
        await _persist(db, session_id, node_key, txt)
    return {"text": txt, "verdict": verdict, "step": step_key, "guest": guest}


async def _persist(db, session_id: str, node_key: Optional[str], txt: str) -> None:
    """§17.1099 — ONE persist per path: the finding is persisted here, by the
    check, whichever caller asked for it (the turn loop's fix flow and the
    /fix endpoint both pass through; run_step_fix forwards its own flag)."""
    try:
        from app.modules import assist_agent as _aa
        await _aa.capture_assistant_reply(session_id=session_id, node_key=node_key, kind="fix", content=txt, db=db)
    except Exception:
        logger.warning("guest_reach_capture_failed sid=%s", session_id)


async def verify_guest_step(*, db, session_id: str, node_key: str, description: str, evidence: str) -> Optional[dict]:
    """§17.1154 — a guest-check step's paste is verified by RE-RUNNING the
    probes through the runner: the class it was inserted for must be gone.
    None when no runner is connected (the ordinary verifier reads the paste)."""
    parsed = guest_step_class(description)
    if not parsed:
        return None
    kind, gid, cls = parsed
    system_state, _s, _f = await _session_bits(db, session_id, node_key)
    guest = find_guest(system_state, f"VM {gid}") if kind == "vm" else find_guest(system_state, f"CT {gid}")
    if not guest:
        guest = {"id": gid, "kind": kind, "name": "", "mac": None, "boot": None, "iso": None}
    det = detect(evidence) or {}
    ip = det.get("ip") or next((i for i in _IPV4_RE.findall(evidence or "")), None) or "0.0.0.0"
    ran = await run_probes(db, guest, ip, 22)
    if ran is None:
        return None
    outputs, runner_name, _ex = ran
    v = judge(guest, ip, outputs)
    fixed = (cls == "stopped" and v["checks"].get("status") == "running") or \
            (cls == "no_ip" and bool(v.get("found_ip"))) or \
            (cls == "no_ssh" and v["class"] == "reachable")
    txt = render(guest, ip, v, outputs, ran_where=f"the host, through your local runner ({runner_name})")
    if fixed:
        return {"outcome": "success", "reason": v["detail"], "summary": f"re-checked from the host: {v['class']}", "recipe": "guest_reach", "probe_class": v["class"]}
    return {"outcome": "incomplete", "summary": v["detail"], "reason": f"**Re-checked from the host — not there yet.**\n\n{txt}",
            "recipe": "guest_reach", "probe_class": v["class"], "exhausted": v["class"] == cls}
