"""§17.1346 — the engine measures the SERVICES on a guest, not just the guest.

`machine_truth` measures machines: running or not, disks, MAC, address, agent,
units. Nothing measured the APPLICATIONS on them — which guest an app lives in,
which port it answers on, where its config is, which user it runs as, whether it
rewrites that config itself. So every draft re-derived all of it from the step's
prose, and every rule that reads prose became a new way to be wrong. One day's
worth, all the same shape:

* a step about an ssh key was drafted as a 4.6 GB game install, because the guest
  is NAMED after the game (§17.1339);
* the shared-storage step was drafted inside Jellyfin, the one container that
  already had the mount, because its measured note mentioned Jellyfin first
  (§17.1340);
* `in-container 999:996` — a service's uid and gid — read as guest 999, and a
  correct host command was refused for not reaching it (§17.1344);
* an edit to qBittorrent's config was thrown away because the service rewrites
  that file on shutdown, which nothing knew (§17.1343);
* the root-folder step put Sonarr's call inside Radarr's container, where there is
  no sonarr config and nothing listening on 8989.

Every one of those is a fact a machine will state plainly when asked. This module
asks:

    systemctl show -p Id -p User -p Group -p ExecStart -p FragmentPath <unit>
    ss -tlnp                      → the port, with the process holding it
    id <user>                     → uid, gid
    stat -c '%U:%G %a %n' <conf>  → who owns the config, and who may write it

and hands the answers to the drafter as facts and to the gates as values to
compare against, in place of prose.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("scaffold")

#: `ExecStart={ path=/opt/Radarr/Radarr ; argv[]=/opt/Radarr/Radarr -nobrowser -data=/var/lib/radarr ; … }`
_ARGV_RE = re.compile(r"argv\[\]=(?P<argv>[^;]*)")
#: `LISTEN 0 512 *:7878 *:* users:(("Radarr",pid=127,fd=319))`
_LISTEN_RE = re.compile(r"^LISTEN\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+\s*(?:users:\((?P<users>.*)\))?\s*$", re.M)
_PROC_RE = re.compile(r'\("(?P<proc>[^"]+)",pid=(?P<pid>\d+)')
#: `uid=999(radarr) gid=996(radarr) groups=…`
_ID_RE = re.compile(r"uid=(?P<uid>\d+)\((?P<user>[^)]*)\)\s+gid=(?P<gid>\d+)\((?P<group>[^)]*)\)")
#: `radarr:radarr 644 /var/lib/radarr/config.xml`
_STAT_RE = re.compile(r"^(?P<owner>[^:\s]+):(?P<group>[^\s]+)\s+(?P<mode>\d{3,4})\s+(?P<path>\S.*)$", re.M)
#: where an app is told to keep its data: `-data=/var/lib/radarr`, `--config /etc/x`
_DATA_RE = re.compile(r"(?:^|\s)(?:-{1,2}(?:data|config|configdir|conf|config-file)[= ])(?P<path>/[^\s]+)")


@dataclass
class ServiceTruth:
    """What a machine says about one service it runs. Every field is a read; a
    field that could not be read is empty, never a guess."""
    guest: str = ""
    unit: str = ""
    name: str = ""
    state: str = ""
    user: str = ""
    group: str = ""
    uid: str = ""
    gid: str = ""
    argv: str = ""
    fragment: str = ""
    data_dir: str = ""
    #: §17.1346b — every config file the service has, because one app keeps several
    #: (`qBittorrent.conf` beside `qBittorrent-data.conf`, Jellyfin's four XMLs) and
    #: a gate that knows only the first misses the file the step actually edits.
    configs: tuple[str, ...] = ()
    config_stat: dict = field(default_factory=dict)   # path -> (owner:group, mode)
    address: str = ""                                # §17.1368 — the guest's own IPv4
    vm: bool = False                                 # §17.1402 — a VM is reached through its agent
    workdir: str = ""                                # §17.1402 — the unit's WorkingDirectory
    ports: tuple[str, ...] = ()
    reads: dict = field(default_factory=dict)

    @property
    def config(self) -> str:
        return self.configs[0] if self.configs else ""

    @property
    def config_owner(self) -> str:
        return (self.config_stat.get(self.config) or ("", ""))[0]

    @property
    def config_mode(self) -> str:
        return (self.config_stat.get(self.config) or ("", ""))[1]

    def rewrites(self, path: str) -> Optional[bool]:
        """Can this service write `path`? None when unmeasured."""
        owner, mode = self.config_stat.get(str(path or ""), ("", ""))
        if not owner or not self.user:
            return None
        try:
            bits = int(mode or "0", 8)
        except ValueError:
            return None
        who, _, grp = owner.partition(":")
        if who == self.user:
            return bool(bits & 0o200)
        if grp and grp == self.group:
            return bool(bits & 0o020)
        return False

    @property
    def rewrites_its_own_config(self) -> Optional[bool]:
        """Can this service write the file that configures it?

        §17.1343 — qBittorrent's conf is `qbittorrent-nox:qbittorrent-nox 664`, so
        the service owns it and rewrites the whole file when it shuts down: an edit
        made while it runs is replaced. Caddy's Caddyfile is root-owned and caddy
        runs as `caddy`, so editing it live is safe. None when unmeasured.
        """
        hits = [self.rewrites(p) for p in self.configs]
        return True if any(h is True for h in hits) else (False if any(h is False for h in hits) else None)

    def says(self) -> str:
        """One line for the drafter: the facts, not prose."""
        bits = [f"{self.name} on guest {self.guest}"
                + (f" (at {self.address})" if self.address else "")]
        if self.ports:
            bits.append("port " + "/".join(self.ports))
        if self.user:
            bits.append(f"runs as {self.user}" + (f" (uid {self.uid}, gid {self.gid})" if self.uid else ""))
        if self.unit:
            bits.append(f"unit {self.unit}" + (f" ({self.state})" if self.state else ""))
        if self.workdir:
            bits.append(f"installed in {self.workdir}")          # §17.1402
        if self.config and _is_template(self.config):
            # §17.1403 — said plainly, so a drafter never edits the template and calls it done
            bits.append(f"settings TEMPLATE {self.config} (the server copies from it and does not read it; "
                        f"the live settings file was not found)")
        elif self.config:
            own = self.rewrites_its_own_config
            bits.append(f"config {self.config}" + (" (the service rewrites it: stop it before editing)"
                                                   if own else " (not writable by the service)" if own is False else ""))
        return " · ".join(bits)


def parse_show(text: str) -> dict:
    """`systemctl show -p …` into a dict, with `ExecStart`'s argv pulled out."""
    out: dict = {}
    for ln in str(text or "").split("\n"):
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        out[k.strip()] = v.strip()
    m = _ARGV_RE.search(out.get("ExecStart", ""))
    if m:
        out["argv"] = " ".join(m.group("argv").split())
    return out


def parse_id(text: str) -> tuple[str, str, str, str]:
    """``(user, uid, group, gid)`` out of `id <user>`."""
    m = _ID_RE.search(str(text or ""))
    return (m.group("user"), m.group("uid"), m.group("group"), m.group("gid")) if m else ("", "", "", "")


def parse_stat(text: str) -> tuple[str, str, str]:
    """``(owner:group, mode, path)`` out of `stat -c '%U:%G %a %n'`."""
    m = _STAT_RE.search(str(text or ""))
    return (f"{m.group('owner')}:{m.group('group')}", m.group("mode"), m.group("path")) if m else ("", "", "")


def parse_stats(text: str) -> list[tuple[str, str, str]]:
    """``[(owner:group, mode, path)]`` for every line of a multi-file `stat`."""
    out: list[tuple[str, str, str]] = []
    for m in _STAT_RE.finditer(str(text or "")):
        out.append((f"{m.group('owner')}:{m.group('group')}", m.group("mode"), m.group("path").strip()))
    return out


def ports_of(ss_text: str, proc: str) -> tuple[str, ...]:
    """The ports a process is LISTENing on, from `ss -tlnp`.

    The process name is the binary's, not the unit's (`Radarr` for
    `radarr.service`), so the match is case-insensitive on a prefix.
    """
    want = re.sub(r"[^a-z0-9]", "", str(proc or "").lower())[:8]
    if not want:
        return ()
    out: list[str] = []
    for m in _LISTEN_RE.finditer(str(ss_text or "")):
        users = m.group("users") or ""
        names = [re.sub(r"[^a-z0-9]", "", p.group("proc").lower()) for p in _PROC_RE.finditer(users)]
        if not any(n.startswith(want[:4]) or want.startswith(n[:4]) for n in names if n):
            continue
        port = str(m.group("local")).rsplit(":", 1)[-1]
        if port.isdigit() and port not in out:
            out.append(port)
    return tuple(out)


def data_dir_of(argv: str) -> str:
    """Where the service was told to keep its data (`-data=/var/lib/radarr`)."""
    m = _DATA_RE.search(" " + str(argv or ""))
    return m.group("path").rstrip("/") if m else ""


def _is_template(path: str) -> bool:
    """`DefaultPalWorldSettings.ini`, `default.conf.example`: a file to copy from, not the live one."""
    base = str(path or "").rsplit("/", 1)[-1].lower()
    return base.startswith("default") or base.endswith((".example", ".sample", ".dist", ".template"))


def config_candidates(name: str, user: str, data_dir: str, home: str = "", workdir: str = "") -> list[str]:
    """Paths worth a `stat`, most specific first. No invention: each is either the
    service's own data dir, or its home, or a name the unit itself gave."""
    out: list[str] = []
    if data_dir:
        out += [f"{data_dir}/config.xml", f"{data_dir}/{name}.conf", f"{data_dir}/settings.json"]
    # §17.1402 — the unit's own WorkingDirectory is a name the unit gave: Palworld's
    # `palworld.service` says `/opt/palworld`, where `DefaultPalWorldSettings.ini` is.
    if workdir:
        out += [f"{workdir}/config.xml", f"{workdir}/*.ini", f"{workdir}/*.conf",
                # §17.1403 — an Unreal server reads its settings from `<Game>/Saved/Config/<Platform>/`:
                # Palworld's live file is `/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini`
                f"{workdir}/*/Saved/Config/*/*.ini"]
    base = (home or f"/var/lib/{name}").rstrip("/")
    # §17.1346b — the directory under `.config` is the APP's own spelling, not the
    # unit's: qBittorrent keeps its file at `.config/qBittorrent/qBittorrent.conf`
    # while the service is `qbittorrent-nox`. The shell's glob finds it; guessing
    # the capitalisation does not.
    out += [f"{base}/config.xml", f"{base}/.config/*/*.conf", f"{base}/*.conf", f"/etc/{name}/*.xml"]
    return list(dict.fromkeys(out))


def measured_units_by_guest(services: list) -> dict:
    """``{guest: [unit names]}`` for every service the engine MEASURED.

    §17.1357 — `unsourced_service_name` was handed `_truth.units`: the unit list
    of the step's SUBJECT guest alone. ADD132's draft said
    `pct exec 105 -- systemctl is-active qbittorrent-nox.service` — correct,
    measured that evening, `LoadState=loaded` — and was refused with
    "`qbittorrent-nox` is not a unit anything the engine holds names", because
    103's list does not have it.

    By GUEST, not pooled: a union would also stop refusing `pct exec 103 --
    systemctl restart qbittorrent-nox`, the right unit in the wrong container,
    and measurement says nothing else catches that (`values_from_another_guest`
    judges ports, data dirs and configs, not units).
    """
    out: dict = {}
    for svc in services or []:
        gid = str(getattr(svc, "guest", "") or "")
        if not gid:
            continue
        names = out.setdefault(gid, [])
        for n in (getattr(svc, "name", ""), re.sub(r"\.service$", "", str(getattr(svc, "unit", "")))):
            if n and n not in names:
                names.append(n)
    return out


def table(services: list[ServiceTruth], reading=None) -> str:
    """The facts block handed to every draft about this step's machines.

    §17.1363 — followed by the SCOPE of those facts. The header asserts "read
    just now; use these values, do not infer others" and used to say nothing
    about what had NOT been read, so a draft could not tell a measured blank
    from an unmeasured one — which is how §17.1356/1357/1360/1361 happened.
    """
    lines = [s.says() for s in services if s.name]
    head = ("SERVICES MEASURED ON THESE MACHINES (read just now; use these values, do not infer others):\n"
            + "\n".join(f"- {ln}" for ln in lines)) if lines else ""
    scope = reading.says() if reading is not None else ""
    return "\n\n".join([t for t in (head, scope) if t])


def in_guest(gid: str, vm: bool = False) -> str:
    """§17.1402 — the prefix that runs a command INSIDE guest `gid`.

    Every read here was `pct exec`, which reaches containers only. Live,
    2026-10-06, ADD122 (a control-panel backend that edits the Palworld server's
    settings) was drafted against `/home/aedefruscio/Steam/…/PalServer` and an ssh
    as aedefruscio. Measured on VM 106: the install is `/opt/palworld`,
    `palworld.service` runs as `User=steam`, and the directory is
    `drwxr-xr-x steam` -- none of which the drafter was told, because the engine's
    service read never reached a VM. `qm guest exec` answers JSON, which `_probe`
    already unwraps (§17.1304)."""
    return f"qm guest exec {gid} --" if vm else f"pct exec {gid} --"


def exec_hint(svc) -> str:
    """How the drafter should address this service's guest, in its own words."""
    return f"{in_guest(getattr(svc, 'guest', 'N'), bool(getattr(svc, 'vm', False)))} sh -c '…'"


async def _probe(spec, command: str) -> tuple[Optional[bool], str]:
    """``(ok, output)`` — ``ok`` None when the runner could not be asked."""
    try:
        from app.modules.assist_local_runner import _plain_output, unwrap_guest_exec
        from app.modules.mcp_client import call_tool
        res = await call_tool(spec, "run_readonly", {"command": command, "timeout_s": 20})
        return (not bool(res.is_error)), unwrap_guest_exec(command, _plain_output(res) or "")
    except Exception as exc:
        logger.warning("service_truth_probe_failed cmd=%r err=%r", command, exc)
        return None, ""


MAX_SERVICES = 6


def _norm(s: str) -> str:
    """A service name with the punctuation and case taken out: `ss` says
    `Radarr` and `qbittorrent-nox` where a step says `radarr`, `qBittorrent`."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def listeners_in(ss_text: str) -> list[str]:
    """Every process name holding a listening port, in the order `ss` reports them.

    `sshd`, `master` (postfix) and `systemd` are on every guest and name no app.
    """
    out: list[str] = []
    for m in _LISTEN_RE.finditer(str(ss_text or "")):
        for p in _PROC_RE.finditer(m.group("users") or ""):
            name = p.group("proc")
            if name and name not in out and name not in ("sshd", "master", "systemd"):
                out.append(name)
    return out


def _same_service(a: str, b: str) -> bool:
    """Is `a` the name of the thing `b` is? `qbittorrent` IS `qbittorrent-nox`."""
    x, y = _norm(a), _norm(b)
    if len(x) < 4 or len(y) < 4:
        return False
    return x == y or x.startswith(y) or y.startswith(x)


async def guests_of_the_named_services(spec, names: list[str], cts: Optional[dict] = None,
                                       limit: int = 10, reading=None,
                                       vms: Optional[dict] = None,
                                       guest_names: Optional[dict] = None) -> dict:
    """``{service name: guest id}`` for the services a step NAMES, from what listens.

    §17.1356 — the pause measured services only in the step's SUBJECT guest, so a
    step spanning guests drafted blind about the others. Live, 2026-10-04: ADD132
    ("Make Radarr and Sonarr actually drive qBittorrent") was measured in guest
    103 alone — Radarr's — and of the eleven drafter prompts that followed, ONE
    carried the string `qbittorrent-nox` and none carried that service's config
    path. The draft duly said `systemctl restart qbittorrent` (the unit is
    `qbittorrent-nox`) against `/var/lib/qbittorrent/…` (it is
    `/var/lib/qbittorrent-nox/.config/…`), and the engine refused its own block
    for facts it had never been given.

    One `ss -tlnp` per running container — the same read `read_services` opens
    with — is enough: qBittorrent names itself `qbittorrent-nox` there, which no
    guest NAME would have revealed (its container is called `download-client`).

    Unreadable guests are skipped; a name nothing listens as is simply absent.
    """
    wanted = [str(n) for n in (names or []) if str(n).strip()]
    if spec is None or not wanted:
        return {}
    running = [g for g, st in sorted((cts or {}).items()) if str(st) == "running"]
    # §17.1402 — and the running VMs, through their agent: Palworld is in VM 106
    _vm_ids = {g for g, st in (vms or {}).items() if str(st) == "running"}
    running += sorted(_vm_ids)
    # §17.1363 — a guest that is not running is a GAP, not an absence of services
    if reading is not None:
        for g, st in sorted({**(vms or {}), **(cts or {})}.items()):
            if str(st) != "running":
                reading.gap(f"guest {g}", f"{st} — nothing in it could be read")
    found: dict = {}
    swept: list = []
    for gid in running[:limit]:
        if len(found) == len(wanted):
            break
        ok, ss_text = await _probe(spec, f"{in_guest(gid, gid in _vm_ids)} sh -c 'ss -tlnp'")
        if not ok:
            if reading is not None:
                reading.gap(f"guest {gid}", "the runner could not read `ss -tlnp` in it"
                            if ok is None else "`ss -tlnp` in it answered nothing")
            continue
        swept.append(str(gid))
        procs = listeners_in(ss_text)
        for name in wanted:
            if name in found:
                continue
            if any(_same_service(name, p) for p in procs):
                found[name] = str(gid)
    # §17.1402 — a process need not carry the service's name: Palworld listens as
    # `PalServer-Linux-Shipping`, so the sweep above never places "palworld". The guest
    # the inventory NAMES for it (`palworld-server`, VM 106) does -- one guest only, and
    # only one that is running; an ambiguous name places nothing.
    for name in wanted:
        if name in found:
            continue
        hits = [str(g) for g, nm in (guest_names or {}).items()
                if str(g) in running and _norm(name) and len(_norm(name)) >= 4
                and _norm(name) in _norm(nm)]
        if len(hits) == 1:
            found[name] = hits[0]
            if reading is not None:
                reading.note(f"{name} is in guest {hits[0]}", "the guest's own name in `qm list`/`pct list`")
    if reading is not None:
        if swept:
            reading.note("what listens in guest(s) " + ", ".join(swept), "ss -tlnp")
        # §17.1363 — a guest the sweep never reached because every service the step
        # names was already placed is NOT a gap: the engine is not ignorant of
        # anything this step needs. The `read:` line above says where it stopped.
        # §17.1363 — a name the sweep looked for and did not find is a NEGATIVE
        # READING, not a gap: the engine did look. Only "I could not look" is a
        # gap, which is the whole distinction this record exists to draw -- and the
        # first draft of it got this wrong, filing nine findings as gaps.
        for name in wanted:
            if name not in found:
                reading.note(f"{name} is not running",
                             "nothing listens as it in any guest that could be read")
    logger.warning("service_guests named=%s found=%s", wanted, found)
    return found


async def read_services(spec, gid: str, units: Optional[list[str]] = None,
                        mentioned: Optional[list[str]] = None, reading=None,
                        vm: bool = False) -> list[ServiceTruth]:
    """Measure the services on one running container: what listens, and what each
    listener is. Fail-soft per field; unreadable means empty, never a guess."""
    if spec is None or not gid:
        return []
    ok, ss_text = await _probe(spec, f"{in_guest(gid, vm)} sh -c 'ss -tlnp'")
    if not ok:
        # §17.1363 — an empty list here used to be indistinguishable from "this
        # guest runs nothing". It is a gap, and it is recorded as one.
        if reading is not None:
            reading.gap(f"the services in guest {gid}",
                        "the runner could not read `ss -tlnp` in it" if ok is None
                        else "`ss -tlnp` in it answered nothing")
        return []
    procs = listeners_in(ss_text)      # §17.1356 — one parser, used by the guest lookup too
    # §17.1368 — and the guest's own address, because a service's port means nothing
    # without the machine it is on. Live, a draft running ON THE HOST called
    # `http://127.0.0.1:7878` for Radarr, which is in container 103: measured,
    # `host->127.0.0.1:7878 = 000`, the host listens on none of those ports, and
    # 103 is 192.168.1.22. The facts said "port 7878 on guest 103" and never said
    # where guest 103 is, so the only address the drafter had was loopback.
    _ok_addr, _addr_text = await _probe(spec, f"{in_guest(gid, vm)} hostname -I")
    _addr = ""
    if _ok_addr:
        _addr = next((w for w in str(_addr_text or "").split()
                      if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", w)), "")
    # §17.1356 — a name is a CANDIDATE for a unit name, never a unit name. Three
    # sources disagree on purpose: `ss` reports the PROCESS (`Radarr`,
    # `qbittorrent-nox`), the step uses an English word (`qbittorrent`), and the
    # unit is a third thing (`radarr.service`, `qbittorrent-nox.service`) whose
    # name is case-sensitive. Measuring the step's word gave the drafter
    # `qbittorrent.service (inactive)` as a FACT, which it wrote, and which the
    # engine then refused as "not a unit anything the engine holds names"; and
    # resolving to the listener alone loses `Radarr`, because `systemctl show
    # Radarr` is not `radarr.service`. So each name carries its alternatives and
    # `LoadState` picks the one the guest actually has.
    wanted: list[list[str]] = []
    def _add(cands: list[str]) -> None:
        clean = [c for c in dict.fromkeys(cands) if c]
        if clean and not any(set(clean) & set(w) for w in wanted):
            wanted.append(clean)
    for p_name in procs:
        _add([p_name, p_name.lower()])
    for name in mentioned or []:
        hit = next((p for p in procs if _same_service(name, p)), "")
        _add([hit, hit.lower(), name] if hit else [name])
    out: list[ServiceTruth] = []
    for cands in wanted[:MAX_SERVICES]:
        base, sh = "", {}
        for cand in cands:
            ok, show = await _probe(spec, f"{in_guest(gid, vm)} sh -c 'systemctl show -p Id -p User -p Group "
                                          f"-p ExecStart -p FragmentPath -p ActiveState -p LoadState -p WorkingDirectory {cand}'")
            if not ok or not show.strip():
                continue
            # §17.1356 — systemctl answers for a unit it has never heard of:
            # ActiveState comes back `inactive`, which reads as a service that
            # exists and is stopped. Measuring every mentioned name in every guest
            # that way produced six phantom facts (`radarr.service (inactive)` on
            # the download client, and so on) beside the three real ones.
            # `LoadState=not-found` is the difference, and it is also what picks
            # the right spelling out of this name's alternatives.
            if (parse_show(show).get("LoadState") or "").strip() == "not-found":
                continue
            base, sh = cand, parse_show(show)
            break
        if not base:
            continue
        unit = sh.get("Id") or f"{base}.service"
        user = sh.get("User") or ""
        s = ServiceTruth(guest=str(gid), vm=vm, unit=unit, name=re.sub(r"\.service$", "", unit),
                         state=sh.get("ActiveState") or "", user=user, group=sh.get("Group") or "",
                         argv=sh.get("argv") or "", fragment=sh.get("FragmentPath") or "")
        s.address = _addr
        s.reads["systemctl show"] = unit
        if _addr:
            s.reads["hostname -I"] = _addr
        s.data_dir = data_dir_of(s.argv)
        # systemd writes `WorkingDirectory=-/opt/x` or `!/opt/x` for its modifiers; `~` is the user's home
        _wd = str(sh.get("WorkingDirectory") or "").lstrip("-!+")
        s.workdir = _wd if _wd.startswith("/") else ""
        s.ports = ports_of(ss_text, s.name) or ports_of(ss_text, (s.argv.split("/")[-1].split()[0] if s.argv else ""))
        if user:
            ok2, id_text = await _probe(spec, f"{in_guest(gid, vm)} sh -c 'id {user}'")
            if ok2:
                _u, s.uid, _g, s.gid = parse_id(id_text)
        cands = config_candidates(s.name, user, s.data_dir, workdir=s.workdir)
        ok3, found = await _probe(spec, f"{in_guest(gid, vm)} sh -c 'ls -1d {' '.join(cands)} 2>/dev/null'")
        paths = [ln.strip() for ln in (found or "").split("\n") if ln.strip().startswith("/")] if ok3 else []
        # §17.1403 — a saved-config directory holds the ENGINE's files too (Engine.ini,
        # Input.ini, forty more): keep only the ones named for this service.
        _w = _norm(s.name)[:6]
        paths = [q for q in paths if "/Saved/Config/" not in q or (_w and _w in _norm(q.rsplit("/", 1)[-1]))]
        # §17.1361 — a service can have SEVERAL config files, and `ls` returns them
        # alphabetically. Live, qBittorrent keeps `qBittorrent.conf` (5 sections,
        # `[Preferences]`, `WebUI\Port=8080`) beside `qBittorrent-data.conf` (1
        # section, no `[Preferences]`), and `-` sorts before `.`, so the facts named
        # the one with no settings in it. ADD132 then wrote the WebUI password into
        # that file, its `sed /^\[Preferences\]/a` matched nothing, and the login
        # check said `Fails.`. Which file holds the settings is measurable: count the
        # `[section]` headers and `key=` lines. All files are kept; the richest leads.
        if len(paths) > 1:
            ok5, counts = await _probe(
                spec, f"{in_guest(gid, vm)} sh -c 'grep -c -E \"^\\[|^[A-Za-z][A-Za-z0-9_.-]*=\" "
                      + " ".join(paths[:8]) + " 2>/dev/null'")
            if ok5:
                score = {}
                for ln in (counts or "").split("\n"):
                    bits = ln.strip().rsplit(":", 1)
                    if len(bits) == 2 and bits[1].strip().isdigit():
                        score[bits[0].strip()] = int(bits[1].strip())
                if any(score.values()):
                    paths = sorted(paths, key=lambda q: -score.get(q, 0))
                    s.reads["config settings"] = ", ".join(f"{q.rsplit('/', 1)[-1]}={score.get(q, 0)}"
                                                           for q in paths[:4])
        if paths:
            ok4, st_text = await _probe(spec, f"{in_guest(gid, vm)} sh -c 'stat -c \"%U:%G %a %n\" "
                                              + " ".join(paths[:8]) + "'")
            if ok4:
                # §17.1403 — a `Default*` file is the template the server copies FROM, not the file
                # it reads; it never leads, however many keys it has (Palworld's live file is 1 byte).
                paths = [q for q in paths if not _is_template(q)] + [q for q in paths if _is_template(q)]
                s.configs = tuple(paths[:8])
                s.config_stat = {p: (o, m) for o, m, p in parse_stats(st_text)}
                s.reads["stat"] = f"{len(s.config_stat)} config file(s)"
        out.append(s)
    # §17.1363 — the provenance each ServiceTruth already recorded now travels
    # with the facts instead of only into the log, and a name this guest does not
    # have is a gap rather than a silence.
    if reading is not None:
        for s in out:
            reading.absorb(s.reads, prefix=f"{s.name} in guest {gid} · ")
            if len(s.configs) > 1 and not s.reads.get("config settings"):
                reading.gap(f"{s.name}'s config", f"{len(s.configs)} candidate files, "
                                                  f"none scored — the first is named above")
            if not s.configs:
                reading.note(f"{s.name} has no config file",
                             "none of the candidate paths exist on the guest")
            if not s.user:
                reading.note(f"{s.name} runs as root", "its unit names no User=")
        # §17.1363 — `systemctl show` answering `LoadState=not-found` is a reading.
        # It is also noise at per-guest-per-name granularity (nine lines for three
        # services across three guests), and the sweep above already states which
        # guest each named service lives in, so nothing is recorded here.
    logger.warning("service_truth_read guest=%s services=%s", gid, [s.name for s in out])
    return out


#: a command that runs INSIDE a guest
_IN_GUEST_RE = re.compile(r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+(?P<gid>\d{3,5})\b", re.I)
#: an edit to a path
_EDIT_RE = re.compile(r"\b(?:sed\s+-i|tee\s|cat\s*>|printf|echo)\b[^\n]*?(?P<path>/[\w./-]+\.(?:conf|xml|ini|cfg|json|yaml|yml))")
_STOPS_RE = re.compile(r"\bsystemctl\s+stop\s+(?P<unit>[\w@.-]+)", re.I)


#: §17.1358 — what precedes a `:port` in a URL or a host:port pair.
_HOST_PORT_RE = re.compile(
    r"(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.-]+):(?P<port>\d{2,5})\b")
#: addresses that mean "this machine"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]", "", "*"})
#: §17.1358 — `"host": "…"` as a JSON field, with or without shell escaping
_JSON_HOST_RE = re.compile(r'\\?"(?:host|hostname|server|address|url)\\?"\s*:\s*\\?"(?P<host>[^"\\]+)')


def _reaches_another_host(seg: str, port: str) -> bool:
    """Is this port reached at some OTHER machine's address?

    §17.1358 — `127.0.0.1:8989` is a claim that the service is HERE; `192.168.1.24:8080`
    or `<QBITTORRENT_IP>:8080` is a claim that it is somewhere else, which is what a
    step wiring two services together must say. A bare port with no host at all
    (`--port 8080`) is local.
    """
    for m in _HOST_PORT_RE.finditer(str(seg or "")):
        if m.group("port") != str(port):
            continue
        host = (m.group("host") or "").strip().lower()
        if host not in _LOCAL_HOSTS:
            return True
    # a placeholder the engine fills with another machine's address
    if re.search(rf"<[A-Z][A-Z0-9_]*>:{re.escape(str(port))}\b", str(seg or "")):
        return True
    # §17.1358 — and the shape the *arr APIs actually use: the host and the port are
    # separate JSON fields, escaped inside a shell-quoted body. Live, ADD132's draft
    # sent `\"host\":\"<QBITTORRENT_IP>\",\"port\":8080` to Radarr, which is the
    # download client's address, correctly stated.
    if re.search(rf'\\?"port\\?"\s*:\s*"?{re.escape(str(port))}"?\b', str(seg or "")):
        for m in _JSON_HOST_RE.finditer(str(seg or "")):
            host = (m.group("host") or "").strip().lower()
            if host and host not in _LOCAL_HOSTS:
                return True
    return False


def values_from_another_guest(commands: list[str], files: Optional[list[dict]],
                             services: list[ServiceTruth]) -> list[dict]:
    """§17.1346 — a command running in one guest using another guest's port or path.

    Live: the root-folder step put both calls inside container 103 — Radarr's, and
    Sonarr's, reading `/var/lib/sonarr/config.xml` and calling `127.0.0.1:8989`.
    Inside 103 there is no sonarr config and nothing listening on 8989, so the
    second call could only fail. The measured services say which machine each value
    belongs to.
    """
    if not services:
        return []
    by_guest: dict[str, list[ServiceTruth]] = {}
    for s in services:
        by_guest.setdefault(s.guest, []).append(s)
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        # §17.1376 — the mirrored question, through the same resolver. Asked with
        # its own `pct exec N` line regex this gate was BLIND to a helper-dispatched
        # block: no refusal rather than a wrong one. Only a line whose guest is
        # KNOWN can be judged -- SOME_GUEST means the text does not say which.
        where = where_each_line_runs(t)
        for i, seg in enumerate(t.split("\n"), 1):
            here = where.get(i, THE_HOST)
            if here in (THE_HOST, SOME_GUEST):
                continue
            mine = by_guest.get(here) or []
            for s in services:
                if s.guest == here or not (s.ports or s.data_dir):
                    continue
                hit = next((p for p in s.ports if re.search(rf"[:\s]{p}\b", seg)), "")
                path = s.data_dir if (s.data_dir and s.data_dir in seg) else ""
                if not hit and not path:
                    continue
                if any((hit and hit in o.ports) or (path and path == o.data_dir) for o in mine):
                    continue                       # the same value exists here too
                # §17.1358 — a port reached at ANOTHER HOST's address is not this
                # mistake; it is how services talk. Live, ADD132's correct draft ran
                # `pct exec 103 -- curl … <addr>:8080 …` to register the download
                # client with Radarr, and this gate refused the one thing the step
                # exists to do. The original defect was `127.0.0.1:8989` inside the
                # wrong container, so the discriminator is the HOST in the value: a
                # loopback or bare port is local and wrong, a real address or a name
                # that is not this guest is remote and right.
                if hit and _reaches_another_host(seg, hit):
                    continue
                out.append({"command": seg.strip()[:200], "why": (
                    f"this runs inside guest {here}, and {hit or path} belongs to {s.name} on guest "
                    f"{s.guest} (measured: " + s.says() + f"). Inside {here} there is nothing of {s.name}'s "
                    f"-- the call can only fail. Address {s.name} in ITS guest: "
                    f"`{exec_hint(s)}`, one command per machine.")})
                break
    return out


#: §17.1368 — a loopback target: `http://127.0.0.1:7878`, `localhost:8989`.
_LOOPBACK_PORT_RE = re.compile(
    r"(?:https?://)?(?P<host>127(?:\.\d{1,3}){3}|localhost|\[::1\]|::1)[:/](?P<port>\d{2,5})\b"
    r"|(?:https?://)?(?P<h2>127(?:\.\d{1,3}){3}|localhost)\b[^\n]{0,40}?[\"']?(?P<p2>\d{2,5})\b")
#: a line the HOST runs: not wrapped in `pct exec` / `qm guest exec`
_WRAPPED_RE = re.compile(r"(?<![\w-])(?:pct\s+exec|qm\s+guest\s+exec|lxc-attach)\b", re.I)
#: §17.1368 — a shell PROMPT: these lines are a record of a session, not a draft
_PROMPT_RE = re.compile(r"^[a-z_][\w.-]*@[\w.-]+:[^\n]*[#$]\s", re.M | re.I)

#: §17.1376 — a guest wrapper spelled as an ARGV list rather than a shell word:
#: `["pct", "exec", str(ctid), "--", "sh", "-c", cmd]`, `["lxc-attach","-n",…]`.
_ARGV_EXEC_RE = re.compile(
    r"""["']pct["']\s*,\s*["']exec["']"""
    r"""|["']lxc-attach["']\s*,\s*["']-n["']"""
    r"""|["']qm["']\s*,\s*["']guest["']\s*,\s*["']exec["']""")
#: §17.1376 — a shell function head: `in_guest() {`, `function in_guest {`.
_SH_FUNC_RE = re.compile(r"^\s*(?:function\s+)?(?P<name>[A-Za-z_]\w*)\s*(?:\(\)\s*)?\{\s*$")
#: §17.1376 — the HOST runs a line unless something puts it inside a guest.
THE_HOST = ""
#: §17.1376 — inside a guest, but which one is not decidable from the text.
SOME_GUEST = "?"


def _dispatchers_in_python(src: str) -> dict:
    """§17.1376 — functions in a PYTHON block that run their argument in a guest.

    `pct_exec` carries the literal argv; `api_get`/`api_put`/`read_api_key` reach
    the guest only by calling it, so membership is a FIXPOINT, not a scan. Returns
    {name: index of the parameter that names the guest, or None}.
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return _dispatchers_in_text(src)       # §17.1376b — fenced or mixed text
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,
                                                                 ast.AsyncFunctionDef))}
    found: dict = {}
    for name, node in funcs.items():
        seg = ast.get_source_segment(src, node) or ""
        if _ARGV_EXEC_RE.search(seg) or _WRAPPED_RE.search(seg):
            found[name] = _guest_param_index(node, seg)
    changed = True
    while changed:                      # a caller of a dispatcher is a dispatcher
        changed = False
        for name, node in funcs.items():
            if name in found:
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id in found):
                    found[name] = _guest_param_index(node,
                                                     ast.get_source_segment(src, node) or "")
                    changed = True
                    break
    return found


def _py_funcs_in_text(text: str) -> dict:
    """§17.1376b — `def name(args):` and its indented body, without parsing.

    A model's answer is markdown around code, and a shell block can carry a Python
    heredoc, so `ast.parse` fails on most real text. The question "which machine
    runs this line" must still have an answer there: measured on the 318-trace
    corpus, the AST-only resolver left 10 traces of this same false positive
    standing, every one of them a helper-shaped draft inside a fence.
    """
    lines = text.split("\n")
    out: dict = {}
    for i, ln in enumerate(lines):
        m = re.match(r"^(?P<ind>\s*)(?:async\s+)?def\s+(?P<name>\w+)\s*\((?P<args>[^)]*)\)",
                     ln)
        if not m:
            continue
        ind = len(m.group("ind"))
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= ind:
                break
            j += 1
        args = [a.split(":")[0].split("=")[0].strip()
                for a in m.group("args").split(",") if a.strip()]
        args = [a for a in args if a not in ("self", "cls") and not a.startswith("*")]
        out[m.group("name")] = {"args": args, "span": (i + 1, j),
                                "body": "\n".join(lines[i:j])}
    return out


def _dispatchers_in_text(text: str) -> dict:
    """§17.1376b — the same fixpoint as the AST path, over text-found functions."""
    funcs = _py_funcs_in_text(text)
    found: dict = {}
    for name, f in funcs.items():
        if _ARGV_EXEC_RE.search(f["body"]) or _WRAPPED_RE.search(f["body"]):
            found[name] = _guest_arg_index(f["args"])
    changed = True
    while changed:
        changed = False
        for name, f in funcs.items():
            if name in found:
                continue
            if any(re.search(rf"(?<![\w.]){re.escape(d)}\s*\(", f["body"])
                   for d in list(found)):
                found[name] = _guest_arg_index(f["args"])
                changed = True
    return found


def _guest_arg_index(args: list):
    """§17.1376 — which argument names the guest: by NAME first, position as fallback."""
    for i, a in enumerate(args):
        if re.search(r"(?:^|_)(?:ct|ctid|cid|vmid|gid|guest|container|vm)(?:id)?$", a, re.I):
            return i
    return 0 if args else None


def _guest_param_index(node, seg: str):
    """Which parameter of this function names the guest — by NAME, not by position.

    `pct_exec(ctid, cmd)` and `api_get(ctid, port, key, path)` both carry it first,
    but nothing guarantees that, so the parameter whose name reads like a guest id
    wins and position is only the fallback.
    """
    return _guest_arg_index([a.arg for a in getattr(node.args, "args", [])])


def where_each_line_runs(text: str) -> dict:
    """§17.1376 — for each 1-based line of a block: which machine runs it.

    `THE_HOST` (""), a guest id ("103"), or `SOME_GUEST` ("?") when the text says
    a guest but not which. Every gate that needs the answer asks HERE, because
    each one that asked with its own line regex got a different answer.

    Live, 2026-10-05: §17.1375 taught the drafter to send a request body through a
    file, and ADD132's next draft did the whole job in Python — one `pct_exec`
    helper wrapping `subprocess.run(["pct","exec",str(ctid),…])`, and
    `api_get`/`api_put` on top of it. §17.1368 read the source line by line, saw
    `http://127.0.0.1:{port}` with no `pct exec` beside it, and refused three
    correct calls; §17.1358, asking the mirrored question the same way, went blind.
    The machine a line runs on is a property of the BLOCK, not of the line.
    """
    lines = text.split("\n")
    where: dict = {}
    # §17.1377c — a line inside a quote an EARLIER line opened belongs to the
    # command that opened it. `pct exec 103 -- sh -c 'cat > /tmp/b.json <<"JSON"
    # … JSON\ncurl … -d @/tmp/b.json'` is one command running in 103, and reading
    # its later lines as the host's refuses the very shape FILE_RULES recommends.
    open_from = 0                              # line that opened an unclosed quote
    for i, ln in enumerate(lines, 1):
        if open_from:
            where[i] = where.get(open_from, SOME_GUEST)
            if _closes_the_quote(ln, _quote_char(lines[open_from - 1])):
                open_from = 0
            continue
        m = _IN_GUEST_RE.search(ln)
        if m:
            where[i] = m.group("gid")
        elif _WRAPPED_RE.search(ln) or _ARGV_EXEC_RE.search(ln):
            where[i] = SOME_GUEST
        # §17.1377d — and a `\` continuation is the same command too (§17.1373
        # taught this for the gates' own line splitting; the resolver needs it
        # as well). Live: a corpus draft put `pct exec 103 -- curl … \` on one
        # line and its URL four lines down, and the URL read as the host's.
        if not where.get(i) and _continued_from(lines, i):
            prev = where.get(_continued_from(lines, i))
            if prev:
                where[i] = prev
        if where.get(i) and _quote_char(ln):
            open_from = i                      # the wrapper's argument runs on
    for name, span, gid in _dispatch_calls(text):
        for i in range(span[0], span[1] + 1):
            if where.get(i) in (None, SOME_GUEST):
                where[i] = gid
    for i, gid in _lines_feeding_dispatch(text, _dispatchers_in_python(text)):
        if where.get(i) in (None, SOME_GUEST):
            where[i] = gid
    return where


def _continued_from(lines: list, i: int) -> int:
    """§17.1377d — the line that STARTED the logical line ending at `i`, or 0.

    Walks back while each predecessor ends in an unescaped backslash, so a command
    broken over five lines resolves to the machine named on its first.
    """
    j = i - 1
    while j >= 1:
        prev = lines[j - 1].rstrip()
        if not prev.endswith("\\") or prev.endswith("\\\\"):
            return 0 if j == i - 1 else j + 1
        if j == 1 or not lines[j - 2].rstrip().endswith("\\"):
            return j
        j -= 1
    return 0


def _quote_char(line: str) -> str:
    """§17.1377c — the quote this line leaves OPEN, or "" when it is balanced."""
    q = ""
    k = 0
    while k < len(line):
        c = line[k]
        if c == "\\" and q != "'":
            k += 2
            continue
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        k += 1
    return q


def _closes_the_quote(line: str, q: str) -> bool:
    """§17.1377c — does this line close a quote `q` that was already open."""
    if not q:
        return True
    k = 0
    while k < len(line):
        c = line[k]
        if c == "\\" and q != "'":
            k += 2
            continue
        if c == q:
            return True
        k += 1
    return False


def _dispatch_calls(text: str) -> list:
    """§17.1376 — every call to a guest-dispatching helper: (name, (first, last), guest)."""
    out: list = []
    py = _dispatchers_in_python(text)
    if py:
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
            out.extend(_dispatch_calls_in_text(text, py))
        for node in ast.walk(tree) if tree else []:
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            idx = py.get(node.func.id, False)
            if idx is False:
                continue
            gid = SOME_GUEST
            if idx is not None and len(node.args) > idx:
                a = node.args[idx]
                if isinstance(a, ast.Constant) and isinstance(a.value, (int, str)):
                    gid = str(a.value)
            out.append((node.func.id, (node.lineno, node.end_lineno or node.lineno), gid))
    for name, span in _sh_functions(text):
        body = "\n".join(text.split("\n")[span[0] - 1:span[1]])
        if not (_WRAPPED_RE.search(body) or _ARGV_EXEC_RE.search(body)):
            continue
        for i, ln in enumerate(text.split("\n"), 1):
            if span[0] <= i <= span[1]:
                continue                       # the definition, not a call
            m = re.match(rf"^\s*{re.escape(name)}\s+(?P<a>\S+)", ln)
            if not m:
                continue
            a = m.group("a").strip("\"'")
            out.append((name, (i, i), a if a.isdigit() else SOME_GUEST))
    return out


def _dispatch_calls_in_text(text: str, dispatchers: dict) -> list:
    """§17.1376b — a call to a dispatcher and the lines it spans, without an AST.

    A call's arguments carry the text sent into the guest, and in real drafts the
    call spans several lines, so the span is read by balancing the parentheses from
    the opening one -- the same job `end_lineno` does on the AST path.
    """
    lines = text.split("\n")
    defs = _py_funcs_in_text(text)
    out: list = []
    for name, idx in dispatchers.items():
        own = (defs.get(name) or {}).get("span")
        for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(", text):
            first = text.count("\n", 0, m.start()) + 1
            if own and own[0] <= first <= own[1] and text[m.end() - 1:m.end()] == "(" \
                    and re.match(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b",
                                 lines[first - 1]):
                continue                       # the definition, not a call
            depth, k = 0, m.end() - 1
            while k < len(text):
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            last = text.count("\n", 0, min(k, len(text) - 1)) + 1
            gid = SOME_GUEST
            if idx is not None:
                arg = text[m.end():k].split(",")[idx] if \
                    len(text[m.end():k].split(",")) > idx else ""
                a = arg.strip().strip("\"'")
                if a.isdigit():
                    gid = a
            out.append((name, (first, last), gid))
    return out


def _lines_feeding_dispatch(text: str, dispatchers: dict) -> list:
    """§17.1376c — lines whose TEXT flows into a guest, not lines sitting inside one.

    Measured on the corpus: the drafts build the command into a variable and pass
    it to the dispatcher on the NEXT line --

        cmd = f"curl -s -H 'X-Api-Key: {key}' http://127.0.0.1:{port}/api/v3/…"
        out = run_pct(ct, cmd)

    so no call span covers the loopback and a span-only resolver still reads line 1
    as the host's. What makes that line guest-side is that its value reaches
    `run_pct`. The same shape in shell is `CMD=…` consumed by `pct exec N -- "$CMD"`.
    Returns [(line number, guest)].
    """
    out: list = []
    lines = text.split("\n")
    scopes = [(f["span"], f["body"]) for f in _py_funcs_in_text(text).values()]
    scopes.append(((1, len(lines)), text))      # module level, and shell
    for (lo, hi), body in scopes:
        fed: dict = {}
        for name in dispatchers:
            for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(", body):
                depth, k = 0, m.end() - 1
                while k < len(body):
                    if body[k] == "(":
                        depth += 1
                    elif body[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                for ident in re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?!\s*[\w(])",
                                        body[m.end():k]):
                    fed.setdefault(ident, SOME_GUEST)
        for i, ln in enumerate(lines[lo - 1:hi], lo):
            # §17.1376c — a shell wrapper consumes `"$CMD"`; a Python one a bare name
            m = re.match(r"^\s*(?:export\s+|local\s+)?(?P<v>[A-Za-z_]\w*)\s*(?:\+)?=",
                         ln)
            if not m:
                continue
            v = m.group("v")
            if v in fed:
                out.append((i, fed[v]))
                continue
            # §17.1376c — or consumed by the WRAPPER itself: `pct exec N -- "$CMD"`
            # in shell, and `['pct','exec',str(ct),'--','sh','-c', curl_cmd]` in
            # Python, where the variable never passes through a call's parens.
            wrappers = "\n".join(l for l in lines[lo - 1:hi]
                                  if _WRAPPED_RE.search(l) or _ARGV_EXEC_RE.search(l))
            if re.search(rf"\$\{{?{re.escape(v)}\b", wrappers) or \
                    re.search(rf"(?<![\w.$]){re.escape(v)}(?![\w(])", wrappers):
                out.append((i, SOME_GUEST))
    return out


def _sh_functions(text: str) -> list:
    """§17.1376 — shell function definitions as (name, (first line, last line))."""
    lines = text.split("\n")
    out: list = []
    i = 0
    while i < len(lines):
        m = _SH_FUNC_RE.match(lines[i])
        if m:
            depth = lines[i].count("{") - lines[i].count("}")
            j = i + 1
            while j < len(lines) and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            out.append((m.group("name"), (i + 1, min(j, len(lines)))))
            i = j
            continue
        i += 1
    return out



def loopback_on_the_host(commands: list[str], files: Optional[list[dict]],
                         services: list) -> list[dict]:
    r"""§17.1368 — a block running ON THE HOST reaching a guest's port on loopback.

    §17.1358 judges the same mistake the other way round: a guest's port used
    INSIDE a different guest. It finds the guest from `pct exec N --` on the line,
    so a call with no such wrapper — a `curl` in a host-side script, or
    `urllib.request.urlopen` in a Python file the host runs — is invisible to it.
    Gate one end and the other stays open ([[feedback_structural_fix_gate_both_ends]]).

    Live, 2026-10-04: ADD132's ninth draft moved to Python and `urllib` (which
    correctly solved §17.1367's quoting problem) and called
    `http://127.0.0.1:7878/api/v3/downloadclient` from a script the runner executes
    on the Proxmox host. Measured:

        host -> 127.0.0.1:7878 = 000      host -> 127.0.0.1:8989 = 000
        the host listens on none of those ports
        container 103 = 192.168.1.22      container 104 = 192.168.1.23

    The frame carried `suggested: run` and no refusals.
    """
    if not services:
        return []
    by_port: dict = {}
    for s in services:
        for p in (getattr(s, "ports", ()) or ()):
            by_port.setdefault(str(p), s)
    if not by_port:
        return []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    # §17.1368 — judged over the whole BLOCK, because the loopback and the port are
    # usually on different lines. Live, the draft built
    # `url = f"http://127.0.0.1:{port}/api/v3/downloadclient"` and passed the
    # literal from elsewhere: `get_download_client(103, radarr_key, 7878)`. A
    # line-at-a-time rule saw a loopback with no port and a port with no loopback,
    # and refused nothing.
    named = [p for p in by_port if re.search(rf"(?<![\w.]){re.escape(p)}(?![\w.])",
                                             "\n".join(texts))]
    out: list[dict] = []
    seen: set = set()
    for t in texts:
        # §17.1368 — a TRANSCRIPT is not a block. T20's record is a session inside
        # container 105 (`root@download-client:~# … curl -I http://localhost:8080`)
        # where that line was right; judged as a host-side draft it reads wrong.
        # A prompt marker says the lines already ran somewhere, and where.
        if _PROMPT_RE.search(t):
            continue
        # §17.1376 — ONE resolver answers "which machine runs this line", and it
        # resolves the block's own helpers. A line regex could only see a literal
        # `pct exec` beside the URL, so a Python block that wraps `pct exec` in a
        # function had every call read as the host's.
        runs_on = where_each_line_runs(t)
        for i, ln in enumerate(t.split("\n"), 1):
            s_line = ln.strip()
            if s_line.startswith("#") or runs_on.get(i, THE_HOST) != THE_HOST:
                continue                       # the guest runs this line, not the host
            hits = [(m.group("port") or m.group("p2") or "")
                    for m in _LOOPBACK_PORT_RE.finditer(ln)]
            if not hits and re.search(r"(?:https?://)?(?:127(?:\.\d{1,3}){3}|localhost|\[::1\])", ln):
                hits = [""]                    # a loopback whose port is a variable
            for port in hits:
                for cand in ([port] if port else named):
                    svc = by_port.get(cand)
                    if not svc or cand in seen:
                        continue
                    seen.add(cand)
                    port = cand
                    where = getattr(svc, "address", "") or ""
                    out.append({"command": s_line[:200], "why": (
                        f"this line runs on the HOST and reaches port {port} on loopback, and {port} belongs "
                        f"to {getattr(svc, 'name', 'a service')} in guest {getattr(svc, 'guest', '?')} "
                        f"(measured: " + (svc.says() if hasattr(svc, "says") else "") + f"). The host does not "
                        f"listen on it -- live (§17.1368), `host->127.0.0.1:{port}` answered `000` and the "
                        f"host's own `ss -tlnp` has nothing on that port. "
                        + (f"Reach it at {where}:{port}, which is guest "
                       f"{getattr(svc, 'guest', '?')}'s measured address, "
                       if where else "Reach it at that guest's own address, ")
                        + f"or run the call inside the guest with "
                      f"`{exec_hint(svc)}`.")})
    return out


#: §17.1377 — a request body (or any file) handed to a command by PATH.
_FILE_READ_RE = re.compile(
    r"(?<![\w-])(?:-d|--data|--data-raw|--data-binary|--json|-T|--upload-file)"
    r"(?![\w-])\s*[\"']?@?(?P<p>/[^\s\"';|)]+)"
    r"|(?<![\w>])<\s*[\"']?(?P<p2>/[^\s\"';|)]+)")
#: §17.1377b — a TRANSFER puts the file on the other machine: `pct push 103 SRC DEST`
#: lands DEST inside 103, `pct pull 103 SRC DEST` lands DEST on the host, and
#: `docker cp SRC name:DEST` lands DEST in the container. Measured: without this,
#: a correct corpus draft that pushed its payload into guest 103 was refused twice.
_PCT_PUSH_RE = re.compile(
    r"(?<![\w-])pct\s+(?P<dir>push|pull)\s+(?P<gid>\d+)\s+(?P<a>\S+)\s+(?P<b>\S+)")
_DOCKER_CP_RE = re.compile(
    r"(?<![\w-])docker\s+cp\s+(?P<a>\S+)\s+(?P<b>\S+)")
#: §17.1377 — and the ways a block CREATES one.
_FILE_WRITE_RE = re.compile(
    r">>?\s*[\"']?(?P<p>/[^\s\"';|)]+)"
    r"|(?<![\w-])tee\s+(?:-a\s+)?[\"']?(?P<p2>/[^\s\"';|)]+)"
    r"|open\(\s*[\"'](?P<p3>/[^\"']+)[\"']\s*,\s*[\"'][wa]")


def _outside_quotes_and_substitutions(line: str, at: int) -> bool:
    r"""§17.1377f — is `at` at the line's own level: no open quote, no open `$(`.

    Quoting restarts inside a command substitution, so a flat quote scan mis-pairs
    `COUNT="$(qm guest exec 106 -- sh -c "wc -l < /home/u/.ssh/authorized_keys")"`
    and reads that `<` as the host's when the GUEST performs it. Measured on the
    corpus: without the `$(` depth this was one false refusal.
    """
    q = ""
    depth = 0
    k = 0
    while k < at and k < len(line):
        c = line[k]
        if c == "\\" and q != "'":
            k += 2
            continue
        if q == "'":
            if c == "'":
                q = ""
        elif c == "$" and line[k + 1:k + 2] == "(":
            depth += 1
            k += 2
            continue
        elif depth and c == ")":
            depth -= 1
        elif q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        k += 1
    return not q and depth == 0


def _redirection_is_the_outer_shells(line: str, at: int) -> bool:
    r"""§17.1377e — a `<` or `>` the DISPATCHING shell performs, not the guest.

    The engine's own VM template is the case:

        qm guest exec "$GID" … -- bash -c "cat > /root/.scaffold_step.sh" < /tmp/in_vm_106_remote.sh

    The host's shell opens `/tmp/in_vm_106_remote.sh` and feeds it to `qm guest
    exec` as stdin; nothing inside the VM ever sees that path. Likewise
    `pct exec 103 -- curl … > /tmp/out.json` writes on the host. A `-d @path` is
    the opposite: that argument is interpreted by the program inside the guest.

    So a redirection counts as the outer shell's when it sits OUTSIDE the quoted
    argument on a line that dispatches into a guest.
    """
    if not (_WRAPPED_RE.search(line) or _ARGV_EXEC_RE.search(line)):
        return False
    return _outside_quotes_and_substitutions(line, at)


def a_file_read_on_another_machine(commands: list[str],
                                   files: Optional[list[dict]]) -> list[dict]:
    r"""§17.1377 — a file written on one machine and read on another.

    §17.1375 taught the drafter to send a request body through a file instead of a
    shell word, and §17.1376 let the correct draft through. The next draft took the
    lesson one machine off (live, 2026-10-05, ADD132):

        python3 - <<'EOF'                                   # on the HOST
        … json.dump(client, open('/tmp/radarr_dc_update.json', 'w'))
        EOF
        pct exec 103 -- sh -c 'curl … -d @/tmp/radarr_dc_update.json …'   # in 103

    The guest's `/tmp` is not the host's, so curl inside 103 cannot see the file it
    was handed. Nothing refused it: the body WAS in a file, and every line ran where
    it claimed to. What was wrong is that the writer and the reader are on different
    machines -- a question only §17.1376's resolver can answer.

    The file CHANNEL counts as a host write: the runner lays those down beside the
    block, on the machine that runs it.
    """
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    channel = {str((f or {}).get("path") or "") for f in files or []}
    channel.discard("")
    out: list[dict] = []
    seen: set = set()
    for t in texts:
        if _PROMPT_RE.search(t):
            continue                           # a transcript, not a draft
        runs_on = where_each_line_runs(t)
        wrote: dict = {}
        for i, ln in enumerate(t.split("\n"), 1):
            if ln.strip().startswith("#"):
                continue
            for m in _FILE_WRITE_RE.finditer(ln):
                path = m.group("p") or m.group("p2") or m.group("p3") or ""
                if not path:
                    continue
                at = runs_on.get(i, THE_HOST)
                if m.group("p") and _redirection_is_the_outer_shells(ln, m.start()):
                    at = THE_HOST              # §17.1377e — `pct exec … > /tmp/x`
                wrote.setdefault(path, at)
            # §17.1377b — a transfer is a write on the DESTINATION machine, and it
            # overrides an earlier host-side write of the same path.
            for m in _PCT_PUSH_RE.finditer(ln):
                dest = m.group("b")
                wrote[dest] = m.group("gid") if m.group("dir") == "push" else THE_HOST
            for m in _DOCKER_CP_RE.finditer(ln):
                a, b = m.group("a"), m.group("b")
                if ":" in b and not b.startswith("/"):
                    wrote[b.split(":", 1)[1]] = SOME_GUEST
                elif ":" in a and not a.startswith("/"):
                    wrote[b] = runs_on.get(i, THE_HOST)
        for i, ln in enumerate(t.split("\n"), 1):
            if ln.strip().startswith("#"):
                continue
            for m in _FILE_READ_RE.finditer(ln):
                path = m.group("p") or m.group("p2") or ""
                if not path or path in seen:
                    continue
                here = runs_on.get(i, THE_HOST)
                if m.group("p2") and _redirection_is_the_outer_shells(ln, m.start()):
                    here = THE_HOST            # §17.1377e — the dispatcher's shell
                if path in wrote:
                    there = wrote[path]
                elif path in channel:
                    there = THE_HOST           # the runner writes the channel here
                else:
                    continue                   # nothing in the block creates it
                if not _across_machines(there, here):
                    continue
                seen.add(path)
                out.append({"command": ln.strip()[:200], "why": (
                    f"`{path}` is written on {_machine_words(there)} and read on "
                    f"{_machine_words(here)}, and those are different filesystems -- the "
                    f"reader cannot see the file. Live (§17.1377), ADD132's draft built the "
                    f"request body on the host and then ran `curl … -d @{path}` inside the "
                    f"guest, where that path does not exist. Create the file ON the machine "
                    f"that reads it (`pct exec N -- sh -c 'cat > " + path + " <<'JSON'\n"
                    "…\nJSON'`), or hand the body over stdin with `-d @-` so no path is "
                    "involved at all.")})
    return out


def _across_machines(a: str, b: str) -> bool:
    """§17.1377 — two machines that are decidably different.

    `SOME_GUEST` against a named guest is not a claim: the text does not say which
    guest, so it may well be the same one.
    """
    if a == b:
        return False
    if THE_HOST in (a, b):
        return True                            # the host and any guest always differ
    return SOME_GUEST not in (a, b)


def _machine_words(m: str) -> str:
    return ("the host" if m == THE_HOST else
            "a guest" if m == SOME_GUEST else f"guest {m}")


def edits_a_config_the_service_rewrites(commands: list[str], files: Optional[list[dict]],
                                        services: list[ServiceTruth]) -> list[dict]:
    """§17.1346 — editing a config the service itself writes, while it runs.

    Live (§17.1343): qBittorrent's conf is `qbittorrent-nox:qbittorrent-nox 664`,
    so the service rewrites the whole file on shutdown and the edit made while it
    ran was replaced on restart. The run reported success and changed nothing. A
    config NOT writable by the service (root-owned, service running as its own
    user) is left alone.
    """
    out: list[dict] = []
    owns = [s for s in services if s.configs]
    if not owns:
        return []
    joined = "\n".join([str(c) for c in commands or []] +
                       [str((f or {}).get("content") or "") for f in files or []])
    stopped = {m.group("unit").replace(".service", "") for m in _STOPS_RE.finditer(joined)}
    for s in owns:
        if s.name in stopped:
            continue
        for m in _EDIT_RE.finditer(joined):
            path = m.group("path")
            if path not in s.configs or s.rewrites(path) is not True:
                continue
            owner, mode = s.config_stat.get(path, ("", ""))
            out.append({"command": m.group(0).strip()[:200], "why": (
                f"{path} is written by {s.name} itself (measured: owner {owner}, mode "
                f"{mode}, service runs as {s.user}), and it rewrites the whole file when it stops. "
                f"An edit made while it runs is replaced the moment the service restarts -- live, that run "
                f"reported success and changed nothing. Stop it first: `systemctl stop {s.name}`, wait for "
                f"the process to go, edit, then start it, and read the value back AFTER the restart.")})
            break
    return out


#: the apps a step's own words name, so a service that holds no port is measured too
_NAME_RE = re.compile(r"\b(radarr|sonarr|prowlarr|lidarr|readarr|bazarr|jellyfin|plex|emby|qbittorrent(?:-nox)?|"
                      r"transmission(?:-daemon)?|deluged?|sabnzbd|caddy|nginx|pihole|pihole-FTL|unbound|"
                      r"palworld|control-panel|jackett|overseerr|tautulli)\b", re.I)


def names_in(node: Optional[dict]) -> list[str]:
    """The service names a step's text mentions, lower-cased and deduplicated."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))
    return list(dict.fromkeys(m.group(1).lower() for m in _NAME_RE.finditer(text)))
