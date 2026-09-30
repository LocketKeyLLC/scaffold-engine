"""§17.1212 — if the engine can read the value off the machine, it must not ask
the operator to type it.

§17.1188 offers placeholder values from what the engine *already knows*: the
pins, the system map, the facts. When none of those carry the value, it asked.
And on a host the engine has a read-only channel to, that is the wrong question:

    <DISK_NAME>   the detached disk's storage/volume identifier as shown in
                  `pvesm status` … You will discover this in Step 1.
    <STORAGE>     the storage pool where the disk lives. Discovered in Step 1.

Both blank, both with no suggestions, on VM 106 — whose `qm config` says
`unused0: local-lvm:vm-106-disk-0` in one line, through the same runner the
engine had been using all evening. The hint even names the command that answers
it, because the engine drafted that command as Step 1 of its own runbook. It
knew where the answer was and asked the human to go and fetch it.

The operator, twice: *"there is, yet again no clear 'enter' to communicate to
the engine that the name of the storage should be within its own information
regarding the set up of that VM."*

So: a small deterministic probe table, read-only, run through the ordinary
channel. Structural, never a model — the same rule §17.1188 set for the ledger
sources. A discovered value arrives as a suggestion with its provenance ("read
from pve-runner just now"), and the highest-confidence one prefills the field,
so the operator confirms rather than transcribes. Fail-soft throughout: an
unreachable runner, a refusal, an unparseable answer all leave the ask exactly
as it was.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("scaffold")

#: A Proxmox guest id as it appears in the drafted commands (`qm set 106 …`).
_VMID_RE = re.compile(r"\b(?:qm|pct)\s+(?:[a-z-]+\s+)*?(\d{3,5})\b")

#: `qm config` volume lines: `unused0: local-lvm:vm-106-disk-0`,
#: `scsi0: local-lvm:vm-106-disk-0,size=40G`.
_VOLUME_RE = re.compile(r"^(unused\d+|scsi\d+|virtio\d+|sata\d+|ide\d+):\s*([^\s,]+)", re.M)

#: `pvesm status` rows: name, type, status, …
_STORAGE_RE = re.compile(r"^(\S+)\s+\S+\s+active\b", re.M)

#: What a placeholder NAME has to look like for each probe. Deliberately narrow:
#: a wrong guess prefilled into a command that changes a machine is worse than
#: an empty box.
_DISK_NAMES = re.compile(r"^(DISK|DISK_NAME|DISK_ID|VOLUME|VOLUME_ID|VOL|UNUSED_DISK)$")
_STORAGE_NAMES = re.compile(r"^(STORAGE|STORAGE_NAME|STORAGE_ID|POOL|TARGET_STORAGE)$")

MAX_PER_INPUT = 3


def vmid_from(commands: list[str]) -> Optional[str]:
    """The guest the commands are about, when they agree on one. Two different
    ids means the engine must not pick for the operator."""
    found: list[str] = []
    for c in commands or []:
        for m in _VMID_RE.finditer(str(c)):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found[0] if len(found) == 1 else None


def volumes_from_config(text_out: str) -> list[tuple[str, str]]:
    """``[(slot, volume)]`` from `qm config` output, detached disks first — a
    runbook re-attaching a disk wants the `unused*` one, and putting it at the
    top is the difference between confirming and hunting."""
    vols = [(m.group(1), m.group(2)) for m in _VOLUME_RE.finditer(text_out or "")]
    # ide2: none,media=cdrom is not a disk
    vols = [(s, v) for s, v in vols if v and v.lower() != "none"]
    return sorted(vols, key=lambda sv: (not sv[0].startswith("unused"), sv[0]))


def storages_from_status(text_out: str) -> list[str]:
    return [m.group(1) for m in _STORAGE_RE.finditer(text_out or "") if m.group(1) != "Name"]


def _sugg(value: str, runner: str, what: str) -> dict:
    return {"value": value, "source": f"read from {runner} just now ({what})",
            "confidence": "measured"}


async def discover_inputs(inputs: list[dict], commands: list[str], spec) -> list[dict]:
    """Fill what the machine can answer. Returns `inputs` (mutated in place).

    Only placeholders with NO value and NO suggestion are probed — the pins and
    the system map stay ahead of a fresh read, because a pin is the operator's
    own decision and this is only evidence.
    """
    if not inputs or spec is None:
        return inputs
    want = [i for i in inputs
            if not (i.get("value") or "").strip() and not (i.get("suggestions") or [])
            and not i.get("secret")
            and (_DISK_NAMES.match(i.get("name") or "") or _STORAGE_NAMES.match(i.get("name") or ""))]
    if not want:
        return inputs
    vmid = vmid_from(commands)
    runner = getattr(spec, "name", "the runner") or "the runner"

    cfg = await _read(spec, f"qm config {vmid}") if vmid else ""
    vols = volumes_from_config(cfg)
    # the storage a discovered disk lives on is the half before the colon —
    # no second command needed, and it cannot disagree with the disk.
    from_disk = [v.split(":", 1)[0] for _s, v in vols if ":" in v]
    pools: list[str] = []
    if any(_STORAGE_NAMES.match(i["name"]) for i in want) and not from_disk:
        pools = storages_from_status(await _read(spec, "pvesm status"))

    for i in want:
        name = i["name"]
        if _DISK_NAMES.match(name) and vols:
            picks = [_sugg(v, runner, f"qm config {vmid}, {slot}") for slot, v in vols[:MAX_PER_INPUT]]
        elif _STORAGE_NAMES.match(name):
            seen: list[str] = []
            for p in from_disk + pools:
                if p not in seen:
                    seen.append(p)
            picks = [_sugg(p, runner, f"qm config {vmid}" if p in from_disk else "pvesm status")
                     for p in seen[:MAX_PER_INPUT]]
        else:
            picks = []
        if not picks:
            continue
        i["suggestions"] = picks
        # Prefill only when the machine gave exactly one answer. With two disks
        # attached, choosing for the operator is how the wrong one gets resized.
        if len(picks) == 1:
            i["value"] = picks[0]["value"]
        logger.warning("runbook_input_discovered name=%s vmid=%s picks=%d prefilled=%s",
                       name, vmid, len(picks), len(picks) == 1)
    return inputs


async def _read(spec, command: str) -> str:
    """One read-only command through the ordinary channel. Gated by the same
    deterministic check the state check uses, then again by the runner itself.
    Never raises."""
    try:
        from app.modules.assist_local_runner import _plain_output, not_evidence
        from app.modules.assist_state_check import read_only_command
        from app.modules.mcp_client import call_tool
        if not read_only_command(command):        # belt and braces; these are literals
            return ""
        res = await call_tool(spec, "run_readonly", {"command": command, "timeout_s": 15})
        out = _plain_output(res)
        # §17.1204 — a refusal is not an answer, and must not be parsed as one.
        return "" if not_evidence(out, bool(res.is_error)) else out
    except Exception as exc:
        logger.warning("runbook_discovery_read_failed cmd=%r err=%r", command[:60], exc)
        return ""
