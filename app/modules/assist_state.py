"""§17.914 — a durable, STRUCTURED model of the operator's system.

The root cause behind §17.906-913, stated plainly: the engine has a PLAN (what
should happen) and a TRANSCRIPT (what was said), and no model of what the
operator's machine actually IS. Every turn it re-derives the world from prose,
or asks again.

Measured on the live session (613dd1df): the engine asked for `qm config 106`
**21 times**; the operator pasted the answer **6 times**. The environment had
nowhere to keep it — only free-text `profile`, LLM-distilled prose `facts`, and
small scalar lists. So the ground truth arrived, was read for exactly one turn,
and was thrown away.

Every gate built in §17.906-913 compensates for that absence rather than
removing it. §17.907 in particular *instructs* the engine to ask for state,
which is precisely why it asked twenty-one times.

This module parses the operator's OWN pasted command output into structured
resource records. Deterministic — no model call, no judgment. A parser only
fires on output it can recognise unambiguously, and records where each value
came from so a stale reading can be told from a fresh one.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("scaffold.assist_state")

# `qm config 106` / `pct config 104` — the command echo tells us the id AND the
# kind, and the body is a flat `key: value` block. Anchored to the prompt echo
# so we never parse a block the operator did not actually run.
_CONFIG_ECHO_RE = re.compile(
    r"(?:^|\n)[^\n]*?\b(qm|pct)\s+config\s+(\d{2,5})\b[^\n]*\n(.*?)(?=\n[^\n]*[$#]\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_KV_RE = re.compile(r"^([a-z][a-z0-9_]{1,20}):\s*(.+?)\s*$", re.IGNORECASE)

# Values worth keeping: the ones the engine kept guessing at.
_CONFIG_KEYS = frozenset({
    "boot", "name", "memory", "cores", "ostype", "scsihw", "onboot", "arch",
    "hostname", "rootfs", "cpu", "machine", "bios", "agent",
})
_DEVICE_KEY_RE = re.compile(r"^(?:scsi|ide|sata|virtio|net|efidisk|tpmstate|mp|unused)\d+$",
                            re.IGNORECASE)


def parse_system_state(operator_text: str) -> dict[str, dict[str, Any]]:
    """Structured resource records from pasted command output.

    Returns ``{resource_id: {kind, attrs, devices, source}}``. Empty when the
    text contains nothing recognisable — this never guesses.
    """
    out: dict[str, dict[str, Any]] = {}
    for m in _CONFIG_ECHO_RE.finditer(operator_text or ""):
        verb, rid, body = m.group(1).lower(), m.group(2), m.group(3)
        attrs: dict[str, str] = {}
        devices: dict[str, str] = {}
        for line in body.splitlines():
            kv = _KV_RE.match(line.strip())
            if not kv:
                continue
            key, val = kv.group(1).lower(), kv.group(2).strip()
            if _DEVICE_KEY_RE.match(key):
                devices[key] = val
            elif key in _CONFIG_KEYS:
                attrs[key] = val
        if attrs or devices:
            out[rid] = {
                "kind": "vm" if verb == "qm" else "ct",
                "attrs": attrs,
                "devices": devices,
                "source": f"{verb} config {rid}",
            }
    return out


def merge_system_state(current: dict | None, observed: dict) -> dict:
    """Newer observation wins per resource; untouched resources survive."""
    merged = {k: v for k, v in (current or {}).items() if isinstance(v, dict)}
    for rid, rec in (observed or {}).items():
        merged[rid] = rec
    return dict(list(merged.items())[-40:])


def render_system_state(state: dict | None) -> str:
    """The prompt block. This is the half that makes the ledger real — a record
    the model cannot see is a record the engine does not have (§17.913)."""
    rows = [(rid, rec) for rid, rec in (state or {}).items() if isinstance(rec, dict)]
    if not rows:
        return ""
    # §17.917 — SCOPE. The first version of this header said only "GROUND TRUTH
    # … do NOT contradict it", and the model drew a conclusion the data never
    # supported. Live (session 613dd1df, turn 1445): "Guide me" on ADD5
    # "Install Ubuntu Server 22.04 on VM 106" produced an entirely POST-INSTALL
    # walkthrough — fix the boot order, detach the ISO, "wait for the login
    # prompt" — because this block showed `boot: order=scsi0` and a 100G
    # `scsi0` disk. A disk existing is not an OS existing. Worse, the §17.714
    # reset branch had just demoted the facts that said the install was HUNG to
    # "earlier observations … most will not hold", so the one authoritative
    # block in the prompt was a CONFIGURATION snapshot, and configuration
    # outranked observation.
    #
    # A `qm config` read establishes what the hypervisor is configured to do.
    # It establishes nothing about what is installed, running, or working
    # inside the guest. Say so, in the block itself.
    lines = [
        "### CONFIRMED resource CONFIGURATION (read from the operator's own "
        "command output — accurate for what it covers; do NOT ask them to "
        "re-run a command whose answer is already here, and do NOT contradict "
        "these values).\n"
        "SCOPE — this is hypervisor configuration ONLY. It does NOT establish "
        "that any OS or software is installed, booted, running or working "
        "inside these resources: a disk being attached is not an OS being "
        "installed on it. Never infer that a step's goal is already achieved "
        "from configuration alone."
    ]
    for rid, rec in sorted(rows):
        kind = "VM" if rec.get("kind") == "vm" else "container"
        attrs = rec.get("attrs") or {}
        devices = rec.get("devices") or {}
        head = f"- {kind} {rid}"
        if attrs.get("name") or attrs.get("hostname"):
            head += f" ({attrs.get('name') or attrs.get('hostname')})"
        lines.append(head + f"  [via `{rec.get('source', '?')}`]")
        for k in sorted(attrs):
            if k in ("name", "hostname"):
                continue
            lines.append(f"    - {k}: {attrs[k]}")
        for d in sorted(devices):
            lines.append(f"    - {d}: {devices[d]}")
    return "\n".join(lines)


# §17.914 — asking for state you already hold. Storing and rendering the state
# is necessary but NOT sufficient: with the CONFIRMED block in the prompt, the
# live model still opened with `qm config 106` and still asserted "Boot Order is
# currently set to prioritize the virtual CD-ROM" while the block said
# `boot: order=scsi0`. Prompt rules are guidance; this is enforcement — the
# §17.668/882 lesson applied to state.
_DISCOVERY_FOR_RE = re.compile(
    r"\b(qm|pct)\s+config\s+(\d{2,5})\b", re.IGNORECASE)


def find_redundant_discovery(text_out: str, state: dict | None) -> list[dict]:
    """Discovery commands in the draft whose answer is already in `state`.

    Returns ``[{command, resource, known}]`` — `known` is a compact rendering of
    what is already on file, so the regeneration directive can name it.
    """
    if not state or not (text_out or "").strip():
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for block in re.findall(r"```[a-z]*\n(.*?)```", text_out, re.DOTALL):
        for m in _DISCOVERY_FOR_RE.finditer(block):
            rid = m.group(2)
            rec = state.get(rid)
            if not isinstance(rec, dict) or rid in seen:
                continue
            seen.add(rid)
            attrs = rec.get("attrs") or {}
            devices = rec.get("devices") or {}
            known = ", ".join(
                f"{k}={v}" for k, v in list(attrs.items())[:4]
            ) or ", ".join(f"{k}={v}" for k, v in list(devices.items())[:3])
            hits.append({
                "command": f"{m.group(1).lower()} config {rid}",
                "resource": rid,
                "known": known,
            })
    return hits
