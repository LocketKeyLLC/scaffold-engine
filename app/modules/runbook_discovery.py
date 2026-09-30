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

# §17.1229 — the rest of what a Proxmox host answers about itself.
#
# ADD96 asked the operator to type ELEVEN values: the node name, the storage,
# three container ids, three IP addresses and three API keys. Every one of them
# is readable through the same channel, and the *arr API keys are readable from
# each container's own `config.xml`. The engine asked a person to go and look up
# eleven things about a machine it was already reading.
#
# The name carries the SERVICE (`PROWLARR_IP` → prowlarr), matched against the
# guest names `pct list` reports, so nothing is guessed from a bare `CONTAINER_IP`
# (§17.1189(E) — an unqualified name drew the operator's public address).
#
# The service part may be several words (`PROWLARR_CONTAINER_IP`), so it allows
# underscores and `service_words` picks the distinguishing one. Generic words are
# NOT a service: `PROXMOX_NODE_IP` is the HOST's address, and matching it against
# a guest called "node" would be §17.1189(E) all over again.
_CTID_NAMES = re.compile(r"^([A-Z][A-Z0-9_]*?)_(?:CTID|CT_ID|CONTAINER_ID|LXC_ID|VMID)$")
# `_HOST`/`_HOSTNAME` count: a drafter names the same value either way
# (`PROWLARR_IP` one draw, `PROWLARR_HOST` the next), and on this LAN the guest's
# ADDRESS is the answer both times — a bare container name does not resolve, and
# the ledger cheerfully offers "prowlarr" as if it did.
_IP_NAMES = re.compile(r"^([A-Z][A-Z0-9_]*?)_(?:IP|IP_ADDRESS|IPADDR|ADDR|ADDRESS|HOST|HOSTNAME)$")
_APIKEY_NAMES = re.compile(r"^([A-Z][A-Z0-9_]*?)_API_KEY$")
_NODE_NAMES = re.compile(r"^(?:PROXMOX_)?NODE(?:_NAME)?$")
#: names for the HOST's own address, which `pct list` cannot answer.
_HOST_IP_NAMES = re.compile(r"^(?:PROXMOX_)?(?:NODE|HOST|PVE|SERVER)_(?:IP|IP_ADDRESS|IPADDR|ADDR|ADDRESS)$")

#: words that name no service. A placeholder made only of these is about the
#: host or is simply unqualified, and must draw nothing from the guest list.
_GENERIC_WORDS = frozenset({
    "PROXMOX", "PVE", "NODE", "HOST", "SERVER", "LOCAL", "TARGET", "REMOTE",
    "CONTAINER", "LXC", "VM", "GUEST", "CT", "THE", "MY", "MAIN", "PRIMARY",
    "SERVICE", "APP", "DOCKER",
})


def service_words(prefix: str) -> list[str]:
    """The distinguishing words in a placeholder's prefix, longest first.

    ``PROWLARR_CONTAINER`` -> ``["prowlarr"]``; ``PROXMOX_NODE`` -> ``[]``,
    because nothing in it names a service.
    """
    parts = [w for w in (prefix or "").split("_") if w and w not in _GENERIC_WORDS]
    return sorted({w.lower() for w in parts}, key=len, reverse=True)

#: `pct list` rows: VMID Status Lock Name — the name is the last column.
_PCT_NAMED = re.compile(r"^\s*(\d{3,5})\s+(\S+)(?:\s+\S*)?\s+(\S+)\s*$", re.M)
#: the first IPv4 out of `hostname -I` / `ip -4 addr`, loopback excluded.
_IPV4 = re.compile(r"\b(?!127\.)(\d{1,3}(?:\.\d{1,3}){3})\b")
#: `<ApiKey>e6b…</ApiKey>` in a *arr `config.xml`.
_APIKEY_XML = re.compile(r"<ApiKey>\s*([A-Za-z0-9]{16,64})\s*</ApiKey>", re.I)


def guests_by_name(text_out: str) -> dict[str, str]:
    """``{lowercased guest name: ctid}`` from `pct list`. The header row has
    "NAME" as its name column and is dropped by the digit anchor on the id."""
    out: dict[str, str] = {}
    for m in _PCT_NAMED.finditer(text_out or ""):
        ctid, name = m.group(1), m.group(3).strip().lower()
        if name and name != "name":
            out.setdefault(name, ctid)
    return out


def match_guest(service: str, by_name: dict[str, str]) -> Optional[str]:
    """The ctid whose guest name IS, or clearly contains, this service.

    `service` is a placeholder PREFIX: its generic words are stripped first, then
    each remaining word is tried longest-first — exact match, then a unique
    substring hit. A prefix with no distinguishing word (``PROXMOX_NODE``) and an
    ambiguous one both resolve to None: a wrong ctid here runs a write against
    the wrong container.
    """
    for svc in service_words(service):
        if svc in by_name:
            return by_name[svc]
        hits = sorted({cid for name, cid in by_name.items() if svc in name or name in svc})
        if len(hits) == 1:
            return hits[0]
    return None


def first_ipv4(text_out: str) -> Optional[str]:
    m = _IPV4.search(text_out or "")
    return m.group(1) if m else None


def _all_ipv4(text_out: str) -> list[str]:
    """Every non-loopback IPv4, in order, deduplicated — a host usually has
    several and choosing for the operator is how the wrong interface gets used."""
    out: list[str] = []
    for m in _IPV4.finditer(text_out or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def apikey_from_config(text_out: str) -> Optional[str]:
    m = _APIKEY_XML.search(text_out or "")
    return m.group(1) if m else None

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
    # §17.1229 — the guest-shaped names are resolved separately: they need
    # `pct list` rather than `qm config`, and an API key ends up in the store
    # instead of on the screen.
    await _discover_guest_inputs(inputs, spec)
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


async def _discover_guest_inputs(inputs: list[dict], spec) -> None:
    """§17.1229 — resolve the ctid / IP / node-name / API-key inputs off the host.

    Mutates `inputs` in place, fail-soft throughout. Each kind is read only when
    something actually asks for it, and one `pct list` serves all of them.

    An API key is NOT prefilled: it is stored under its own name (§17.1193) and
    the input is marked satisfied by reference, so the command keeps `$NAME` and
    the value never enters a prompt, a block or a log. That is the whole point of
    the store — reading a secret onto the screen to save a person typing it would
    trade one problem for a worse one.
    """
    pend = [i for i in inputs if not (i.get("value") or "").strip() and not (i.get("suggestions") or [])]
    if not pend:
        return
    runner = getattr(spec, "name", "the runner") or "the runner"
    names = {i["name"]: i for i in pend if i.get("name")}
    ctid_want = {n: m.group(1) for n, m in ((n, _CTID_NAMES.match(n)) for n in names) if m}
    ip_want = {n: m.group(1) for n, m in ((n, _IP_NAMES.match(n)) for n in names)
               if m and not _HOST_IP_NAMES.match(n)}
    key_want = {n: m.group(1) for n, m in ((n, _APIKEY_NAMES.match(n)) for n in names) if m}
    node_want = [n for n in names if _NODE_NAMES.match(n)]

    if node_want:
        host = (await _read(spec, "hostname")).strip().splitlines()
        if host and host[0].strip():
            for n in node_want:
                names[n]["value"] = host[0].strip()
                names[n]["suggestions"] = [_sugg(host[0].strip(), runner, "hostname")]
                logger.warning("runbook_input_discovered name=%s kind=node value=%s", n, host[0].strip())

    # the HOST's own address — `pct list` cannot answer this one, and a name made
    # only of generic words (`PROXMOX_NODE_IP`) reaches no guest by design.
    host_ip_want = [n for n in names if _HOST_IP_NAMES.match(n)]
    if host_ip_want:
        addrs = _all_ipv4(await _read(spec, "hostname -I"))
        for n in host_ip_want:
            if not addrs:
                continue
            names[n]["suggestions"] = [_sugg(a, runner, "hostname -I on the host") for a in addrs[:MAX_PER_INPUT]]
            if len(addrs) == 1:
                names[n]["value"] = addrs[0]
            logger.warning("runbook_input_discovered name=%s kind=host_ip found=%d prefilled=%s",
                           n, len(addrs), len(addrs) == 1)

    if not (ctid_want or ip_want or key_want):
        return
    by_name = guests_by_name(await _read(spec, "pct list"))
    if not by_name:
        return
    # service -> ctid, resolved once and shared by all three kinds
    svc_ctid: dict[str, Optional[str]] = {}
    for svc in set(ctid_want.values()) | set(ip_want.values()) | set(key_want.values()):
        svc_ctid[svc] = match_guest(svc, by_name)

    for n, svc in ctid_want.items():
        cid = svc_ctid.get(svc)
        if cid:
            names[n]["value"] = cid
            names[n]["suggestions"] = [_sugg(cid, runner, f"pct list, guest named {svc}")]
            logger.warning("runbook_input_discovered name=%s kind=ctid value=%s", n, cid)

    for n, svc in ip_want.items():
        cid = svc_ctid.get(svc)
        if not cid:
            continue
        ip = first_ipv4(await _read(spec, f"pct exec {cid} -- hostname -I"))
        if ip:
            names[n]["value"] = ip
            names[n]["suggestions"] = [_sugg(ip, runner, f"hostname -I inside container {cid}")]
            logger.warning("runbook_input_discovered name=%s kind=ip ctid=%s value=%s", n, cid, ip)

    for n, svc in key_want.items():
        cid = svc_ctid.get(svc)
        if not cid:
            continue
        key = None
        for path in ("/config/config.xml", f"/var/lib/{svc.lower()}/config.xml",
                     f"/opt/{svc.lower()}/config.xml"):
            key = apikey_from_config(await _read(spec, f"pct exec {cid} -- cat {path}"))
            if key:
                break
        if not key:
            continue
        try:
            from app.database import async_session
            from app.modules import runner_secrets as _rs
            async with async_session() as db:
                await _rs.set_secret(db, n, key, runner=getattr(spec, "name", None),
                                     hint=f"read from {svc.lower()}'s config.xml in container {cid}")
                await db.commit()
        except Exception as exc:
            logger.warning("discovered_secret_store_failed name=%s err=%r", n, exc)
            continue
        # satisfied by REFERENCE: the command keeps `$NAME`, nothing is shown.
        names[n]["stored"] = True
        names[n]["secret"] = True
        names[n]["value"] = ""
        names[n]["hint"] = (f"already read off {svc.lower()} in container {cid} and stored — "
                            f"the command uses $" + n + ", you do not need to type it")
        logger.warning("runbook_input_stored name=%s kind=api_key ctid=%s chars=%d", n, cid, len(key))


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
