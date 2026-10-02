"""§17.1188 — the values a runbook asks for, offered from what the engine
already knows.

A run pause (§17.1186/1187) asks the operator for every ``<PLACEHOLDER>`` in
the drafted commands. Most of those values are already in the session's
environment ledger: the pinned substitutions (``JELLYFIN_IP → 192.168.1.20``,
§17.1039), the system map (ids, names, addresses learned from ``pct list`` /
``qm config`` / ``ip addr``), and the facts the operator confirmed. This
module matches a placeholder NAME to those sources deterministically —
propose structurally, never guess with a model — and returns ranked
suggestions with their provenance:

* a **pin** with the same name is prefilled (it is the operator's own value);
* the **system map** and the **facts** are offered as chips, each naming its
  source, and the operator picks;
* a secret-named placeholder is never prefilled.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CIDR_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})/(\d{1,2})\b")
_PORT_RE = re.compile(r"(?:\bport\s+|:)(\d{2,5})\b", re.I)
_ID_RE = re.compile(r"\b(?:VM|CT|LXC|container|vmid|ctid)\s*(\d{3,5})\b", re.I)
_SECRET_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL", re.I)
#: §17.1275 — a PUBLIC key is public material: masking it, encrypting it and
#: refusing to suggest it only stops the operator seeing what was installed.
_GUEST_MENTION_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*\d{3,5}\b", re.I)
_PUBLIC_RE = re.compile(r"PUBLIC|PUBKEY|PUB_KEY|_PUB\b", re.I)
#: `aedefruscio@192.168.1.129` in a step's own words — whose account, on which machine.
_USER_AT_HOST_RE = re.compile(r"(?<![\w<.-])([a-z_][a-z0-9_-]{0,31})@((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9][a-z0-9.-]*[a-z0-9])\b")


def secret_name(name: str) -> bool:
    """§17.1275 — ONE definition of "this placeholder holds a secret", shared with
    `supervised_runs.inputs_for` (sibling call sites drift). `OPERATOR_PUBLIC_KEY`
    matched `KEY` and was kept encrypted, masked and never suggested — for a value
    whose whole purpose is to be handed out."""
    n = str(name or "")
    return bool(_SECRET_RE.search(n)) and not _PUBLIC_RE.search(n)
_GENERIC = frozenset({"IP", "ADDR", "ADDRESS", "ID", "NAME", "VALUE", "THE", "OF", "NEW", "TARGET", "HOST", "VM", "CT",
                      "LXC", "CONTAINER", "PORT", "PREFIX", "CIDR", "GATEWAY", "GW", "DNS", "USER", "HOSTNAME",
                      "VMID", "CTID", "NAMESERVER", "VLAN"})
MAX_SUGGESTIONS = 4


def _kind(name: str) -> str:
    n = name.upper()
    if any(t in n for t in ("VMID", "CTID", "VM_ID", "CT_ID")) or n.endswith("_ID") and any(t in n for t in ("VM", "CT", "LXC", "CONTAINER")):
        return "vmid"
    if "PORT" in n:
        return "port"
    if "PREFIX" in n or "CIDR" in n:
        return "prefix"
    if any(t in n for t in ("IP", "ADDR", "GATEWAY", "GW", "DNS", "NAMESERVER")):
        return "ip"
    if "HOSTNAME" in n or n.endswith("_HOST") or n == "HOST":
        return "hostname"
    if "USER" in n:
        return "user"
    if "VLAN" in n and n.endswith("_ID"):
        return "vlan"
    return "text"


def _words(name: str) -> list[str]:
    return [w for w in name.upper().split("_") if w and w not in _GENERIC and len(w) > 1]


def _mentions(text: str, words: list[str]) -> bool:
    """§17.1189 — does this text name one of the placeholder's own words?

    This was a SUBSTRING test, which a short word cannot survive: ``AI_VM_IP``
    reduces to the single word ``AI`` (``VM`` and ``IP`` are generic), and
    ``"ai" in text`` is true of *domain*, *available*, *chain*, *main* — so
    every fact with an address in it looked like a match. Matched on word
    boundaries instead (``ai-vm`` and ``ai_vm`` still match; ``domain`` does
    not)."""
    low = (text or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", low) for w in words)


def _add(out: list[dict], value: str, source: str, confidence: str) -> None:
    value = str(value or "").strip()
    if not value or any(o["value"] == value for o in out) or len(out) >= MAX_SUGGESTIONS:
        return
    out.append({"value": value, "source": source[:120], "confidence": confidence})


def _step_ips(text: str, words: list[str]) -> list[str]:
    """The IPv4 literals a step's text states, the one nearest each mention of
    the placeholder's own words first. `<PROWLARR_IP>` beside "Radarr
    (192.168.1.22) at Prowlarr (192.168.1.21)" must offer .21 alone; with no
    distinguishing word, or none within reach of an address, every address the
    step names is offered (the step is about them) and only a single one
    prefills."""
    spans = [(m.start(), m.end(), m.group()) for m in _IP_RE.finditer(text or "")]
    ips = list(dict.fromkeys(ip for _s, _e, ip in spans))
    if not words or len(ips) < 2:
        return ips
    low = (text or "").lower()
    near: list[str] = []
    for w in words:
        for wm in re.finditer(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", low):
            gap = lambda sp: max(0, wm.start() - sp[1], sp[0] - wm.end())   # noqa: E731
            best = min(spans, key=gap)
            if gap(best) <= 40 and best[2] not in near:
                near.append(best[2])
    return near or ips


def suggest_for(name: str, env: dict, text: str = "") -> list[dict]:
    """Ranked ``[{value, source, confidence}]`` for one placeholder name.
    ``confidence`` is ``pinned`` (prefill), ``step`` (the step's own text —
    prefills when it is the only one), ``map`` or ``fact`` (offered)."""
    out: list[dict] = []
    env = env or {}
    if secret_name(name):
        return out
    subs = env.get("substitutions") or {}
    if isinstance(subs, dict):
        for k, v in subs.items():
            if str(k).upper() == name.upper():
                _add(out, str(v), f"pinned as {k}", "pinned")
    kind = _kind(name)
    words = _words(name)
    # §17.1288 — what the system map says about the HOST and about the guest
    # this placeholder is named after. Live, ADD82 asked for <PALWORLD_IP> and
    # prefilled 192.168.1.156 -- the Proxmox host's own address, the only one
    # the step's text named (its web console, `https://192.168.1.156:8006`) --
    # and offered `root`, the host shell's user, for an account inside VM 106.
    # The map knows both: `host` carries that address and `106 vm palworld-server`
    # is the guest the name points at. A guest's address is never the host's,
    # and the host shell's user is no candidate for an account inside a guest.
    state = env.get("system_state") or {}
    state = state if isinstance(state, dict) else {}
    host_addrs: set[str] = set()
    about_guest = False
    for sid, ent in state.items():
        if not isinstance(ent, dict):
            continue
        attrs = ent.get("attrs") if isinstance(ent.get("attrs"), dict) else {}
        ekind = str(ent.get("kind") or "")
        if ekind in ("host", "node") and attrs.get("ip"):
            host_addrs.add(str(attrs["ip"]).split("/")[0])
        if ekind in ("vm", "ct") and words and _mentions(f"{attrs.get('name') or attrs.get('hostname') or ''} {sid}", words):
            about_guest = True
    # §17.1288h — or the step's own text names a guest and the placeholder's words
    # (`PALWORLD_USER` ↔ "the palworld-server guest (VM 106)"), with no map to ask.
    if not about_guest and words and text and _GUEST_MENTION_RE.search(text) and _mentions(text, words):
        about_guest = True
    # §17.1275 — the step's OWN words. Live, ADD26 was titled "Install the SSH
    # public key on the AI VM (192.168.1.129)", its description said `ssh
    # aedefruscio@192.168.1.129`, and the frame asked for <AI_VM_IP> with no
    # suggestion and offered `root` for <AI_VM_USER> — the Proxmox shell's user,
    # for an account on the VM. The drafter's own hint even said "task line
    # suggests `aedefruscio`" and asked anyway. A value the step states is the
    # operator's (or the planner's) decision about THIS step and outranks the
    # shell; it is below a pin, which is the operator's decision by name.
    step_users: list[str] = []
    if text:
        pairs = _USER_AT_HOST_RE.findall(text)
        step_users = list(dict.fromkeys(u for u, _h in pairs))
        if kind == "user":
            for u in step_users:
                host = next(h for uu, h in pairs if uu == u)
                _add(out, u, f"the step's own text (`{u}@{host}`)", "step")
        elif kind == "ip":
            for ip in _step_ips(text, words):
                _add(out, ip, "the step's own text", "step")
        elif kind == "hostname":
            for _u, h in pairs:
                if not _IP_RE.fullmatch(h):
                    _add(out, h, f"the step's own text (`{_u}@{h}`)", "step")
    if isinstance(state, dict):
        for sid, ent in state.items():
            if not isinstance(ent, dict):
                continue
            attrs = ent.get("attrs") if isinstance(ent.get("attrs"), dict) else {}
            label = attrs.get("name") or attrs.get("hostname") or ""
            ekind = str(ent.get("kind") or "")
            src = f"system map: {ekind} {sid}" + (f" ({label})" if label else "")
            mentions = _mentions(f"{label} {sid}", words)
            if kind == "vmid" and ekind in ("vm", "ct") and str(sid).isdigit():
                if mentions or not words:
                    _add(out, str(sid), src, "map")
            elif kind == "ip" and attrs.get("ip") and (mentions or not words):
                _add(out, str(attrs["ip"]).split("/")[0], src, "map")
            elif kind == "hostname" and label and (mentions or not words):
                _add(out, str(label), src, "map")
    profile = str(env.get("profile") or "")
    m = re.search(r"\b([a-z_][a-z0-9_-]*)@([a-z0-9][a-z0-9.-]*)", profile, re.I)
    if m:
        # §17.1275 — when the step names whose account it is, the shell's user
        # is not an alternative: `root` on the Proxmox host is the wrong answer
        # for an account on the VM, and offering it beside the right one is noise.
        if kind == "user" and not step_users and not about_guest:     # §17.1288 — nor for a guest's account
            _add(out, m.group(1), "the shell you work in", "map")
        elif kind == "hostname":
            _add(out, m.group(2), "the shell you work in", "map")
    facts = env.get("facts") or []
    for f in facts if isinstance(facts, list) else []:
        text = f if isinstance(f, str) else str((f or {}).get("text") or f)
        if words and not _mentions(text, words):
            continue
        # §17.1189 — a name whose every word is generic (``CONTAINER_IP``,
        # ``HOST_IP``) has nothing to match on, and an ADDRESS drawn from an
        # unrelated fact is a different machine or network: on the real session
        # this offered 67.240.32.243 — the public address of the operator's
        # DuckDNS domain, learned from a `getent hosts` fact — as a candidate
        # container IP. An id or a port from an unmatched fact is still an id
        # or a port on this host, so those stay on offer (with their source).
        if not words and kind in ("ip", "prefix"):
            continue
        if not words and kind not in ("ip", "port", "vmid", "prefix"):
            continue
        src = "fact: " + text[:100]
        if kind == "ip":
            for ip in _IP_RE.findall(text):
                _add(out, ip, src, "fact")
        elif kind == "prefix":
            for _, p in _CIDR_RE.findall(text):
                _add(out, p, src, "fact")
        elif kind == "port":
            for p in _PORT_RE.findall(text):
                _add(out, p, src, "fact")
        elif kind == "vmid":
            for i in _ID_RE.findall(text):
                _add(out, i, src, "fact")
        elif kind == "vlan":
            for v in re.findall(r"\bvlan\s*(?:id\s*)?(\d{1,4})\b", text, re.I):
                _add(out, v, src, "fact")
    if about_guest and kind == "ip" and host_addrs:
        # §17.1288 — a pin is the operator's decision by name and stays; every
        # other source offering the host's own address for a guest is wrong.
        out = [o for o in out if o.get("confidence") == "pinned" or o.get("value") not in host_addrs]
    return out


def suggest_inputs(inputs: list[dict], env: Optional[dict], text: str = "") -> list[dict]:
    """The frame's ``inputs`` with ``suggestions`` and a prefilled ``value``
    when the operator pinned that exact name — or (§17.1275) when the step's own
    text states exactly one candidate. Two candidates in the text are both
    offered and neither prefilled, the §17.1212 two-disks rule."""
    out = []
    for i in inputs:
        sugg = suggest_for(str(i.get("name") or ""), env or {}, text)
        pinned = next((s["value"] for s in sugg if s["confidence"] == "pinned"), "")
        stepped = [s["value"] for s in sugg if s["confidence"] == "step"]
        value = pinned or (stepped[0] if len(stepped) == 1 else "")
        out.append({**i, "suggestions": sugg, "value": value})
    return out


async def job_environment(db, job_id: str) -> dict:
    """The environment ledger for a job: its assist session's (the newest),
    else the job row's own ``metadata.environment``; ``{}`` when neither."""
    from sqlalchemy import text
    from app.modules.assist_environment import _environment_from_metadata
    try:
        row = (await db.execute(
            text("SELECT metadata FROM assist_sessions WHERE job_id = :jid ORDER BY updated_at DESC LIMIT 1"),
            {"jid": job_id})).mappings().first()
        if row and row.get("metadata"):
            env = _environment_from_metadata(row["metadata"])
            if env.get("facts") or env.get("substitutions") or env.get("system_state"):
                return env
        row = (await db.execute(text("SELECT metadata FROM jobs WHERE id = :jid"), {"jid": job_id})).mappings().first()
        return _environment_from_metadata((row or {}).get("metadata")) if row else {}
    except Exception:
        return {}


def _as_dict(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return dict(v) if isinstance(v, dict) else {}
