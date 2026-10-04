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
        bits = [f"{self.name} on guest {self.guest}"]
        if self.ports:
            bits.append("port " + "/".join(self.ports))
        if self.user:
            bits.append(f"runs as {self.user}" + (f" (uid {self.uid}, gid {self.gid})" if self.uid else ""))
        if self.unit:
            bits.append(f"unit {self.unit}" + (f" ({self.state})" if self.state else ""))
        if self.config:
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


def config_candidates(name: str, user: str, data_dir: str, home: str = "") -> list[str]:
    """Paths worth a `stat`, most specific first. No invention: each is either the
    service's own data dir, or its home, or a name the unit itself gave."""
    out: list[str] = []
    if data_dir:
        out += [f"{data_dir}/config.xml", f"{data_dir}/{name}.conf", f"{data_dir}/settings.json"]
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
                                       limit: int = 10, reading=None) -> dict:
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
    # §17.1363 — a guest that is not running is a GAP, not an absence of services
    if reading is not None:
        for g, st in sorted((cts or {}).items()):
            if str(st) != "running":
                reading.gap(f"guest {g}", f"{st} — nothing in it could be read")
    found: dict = {}
    swept: list = []
    for gid in running[:limit]:
        if len(found) == len(wanted):
            break
        ok, ss_text = await _probe(spec, f"pct exec {gid} -- sh -c 'ss -tlnp'")
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
                        mentioned: Optional[list[str]] = None, reading=None) -> list[ServiceTruth]:
    """Measure the services on one running container: what listens, and what each
    listener is. Fail-soft per field; unreadable means empty, never a guess."""
    if spec is None or not gid:
        return []
    ok, ss_text = await _probe(spec, f"pct exec {gid} -- sh -c 'ss -tlnp'")
    if not ok:
        # §17.1363 — an empty list here used to be indistinguishable from "this
        # guest runs nothing". It is a gap, and it is recorded as one.
        if reading is not None:
            reading.gap(f"the services in guest {gid}",
                        "the runner could not read `ss -tlnp` in it" if ok is None
                        else "`ss -tlnp` in it answered nothing")
        return []
    procs = listeners_in(ss_text)      # §17.1356 — one parser, used by the guest lookup too
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
            ok, show = await _probe(spec, f"pct exec {gid} -- sh -c 'systemctl show -p Id -p User -p Group "
                                          f"-p ExecStart -p FragmentPath -p ActiveState -p LoadState {cand}'")
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
        s = ServiceTruth(guest=str(gid), unit=unit, name=re.sub(r"\.service$", "", unit),
                         state=sh.get("ActiveState") or "", user=user, group=sh.get("Group") or "",
                         argv=sh.get("argv") or "", fragment=sh.get("FragmentPath") or "")
        s.reads["systemctl show"] = unit
        s.data_dir = data_dir_of(s.argv)
        s.ports = ports_of(ss_text, s.name) or ports_of(ss_text, (s.argv.split("/")[-1].split()[0] if s.argv else ""))
        if user:
            ok2, id_text = await _probe(spec, f"pct exec {gid} -- sh -c 'id {user}'")
            if ok2:
                _u, s.uid, _g, s.gid = parse_id(id_text)
        cands = config_candidates(s.name, user, s.data_dir)
        ok3, found = await _probe(spec, f"pct exec {gid} -- sh -c 'ls -1d {' '.join(cands)} 2>/dev/null'")
        paths = [ln.strip() for ln in (found or "").split("\n") if ln.strip().startswith("/")] if ok3 else []
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
                spec, f"pct exec {gid} -- sh -c 'grep -c -E \"^\\[|^[A-Za-z][A-Za-z0-9_.-]*=\" "
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
            ok4, st_text = await _probe(spec, f"pct exec {gid} -- sh -c 'stat -c \"%U:%G %a %n\" "
                                              + " ".join(paths[:8]) + "'")
            if ok4:
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
        for seg in t.split("\n"):
            m = _IN_GUEST_RE.search(seg)
            if not m:
                continue
            here = m.group("gid")
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
                    f"`pct exec {s.guest} -- sh -c '…'`, one command per machine.")})
                break
    return out


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
