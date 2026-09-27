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


def _add(out: list[dict], value: str, source: str, confidence: str) -> None:
    value = str(value or "").strip()
    if not value or any(o["value"] == value for o in out) or len(out) >= MAX_SUGGESTIONS:
        return
    out.append({"value": value, "source": source[:120], "confidence": confidence})


def suggest_for(name: str, env: dict) -> list[dict]:
    """Ranked ``[{value, source, confidence}]`` for one placeholder name.
    ``confidence`` is ``pinned`` (prefill), ``map`` or ``fact`` (offered)."""
    out: list[dict] = []
    env = env or {}
    if _SECRET_RE.search(name):
        return out
    subs = env.get("substitutions") or {}
    if isinstance(subs, dict):
        for k, v in subs.items():
            if str(k).upper() == name.upper():
                _add(out, str(v), f"pinned as {k}", "pinned")
    kind = _kind(name)
    words = _words(name)
    state = env.get("system_state") or {}
    if isinstance(state, dict):
        for sid, ent in state.items():
            if not isinstance(ent, dict):
                continue
            attrs = ent.get("attrs") if isinstance(ent.get("attrs"), dict) else {}
            label = attrs.get("name") or attrs.get("hostname") or ""
            ekind = str(ent.get("kind") or "")
            src = f"system map: {ekind} {sid}" + (f" ({label})" if label else "")
            mentions = any(w.lower() in (label or "").lower() or w.lower() in str(sid).lower() for w in words)
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
        if kind == "user":
            _add(out, m.group(1), "the shell you work in", "map")
        elif kind == "hostname":
            _add(out, m.group(2), "the shell you work in", "map")
    facts = env.get("facts") or []
    for f in facts if isinstance(facts, list) else []:
        text = f if isinstance(f, str) else str((f or {}).get("text") or f)
        low = text.lower()
        if words and not any(w.lower() in low for w in words):
            continue
        if not words and kind not in ("ip", "port", "vmid"):
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
    return out


def suggest_inputs(inputs: list[dict], env: Optional[dict]) -> list[dict]:
    """The frame's ``inputs`` with ``suggestions`` and a prefilled ``value``
    when the operator pinned that exact name."""
    out = []
    for i in inputs:
        sugg = suggest_for(str(i.get("name") or ""), env or {})
        pinned = next((s["value"] for s in sugg if s["confidence"] == "pinned"), "")
        out.append({**i, "suggestions": sugg, "value": pinned})
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
