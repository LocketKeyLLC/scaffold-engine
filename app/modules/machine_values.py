"""§17.1332 — a value the machine holds is read ON the machine, not asked of the operator.

Live, 2026-10-03, the operator on the home-lab job::

    "The api keys for radarr and sonarr the engine should be able to retrieve
     itself, it did so with PRowlarr."

They were right, and the record shows exactly how the engine managed both at
once. ADD115's RUN block read all three keys itself::

    prowlarr_key = get_key(102, "/var/lib/prowlarr/config.xml")
    radarr_key   = get_key(103, "/var/lib/radarr/config.xml")
    sonarr_key   = get_key(104, "/var/lib/sonarr/config.xml")

while the same step's CHECKS asked the operator to type them::

    - How many indexers Prowlarr now has (paste the key from the previous check):
      curl -s -H "X-Api-Key: <PROWLARR_API_KEY>" http://<PROWLARR_IP>:9696/api/v1/indexer

So the frame carried three secret inputs for values the engine had just read off
the disk. `verify_needs_a_value_the_run_never_used` (§17.1331) does not catch
this one: the run DID use the value, it simply never shared it.

The fix is deterministic and belongs in one place. Every app in the *arr family
writes its own API key into its own config file, in the same element, on a guest
the runner can already reach. When a placeholder names such a value, the engine
substitutes a read of that file instead of asking: in a command, as a command
substitution; in a written file, hoisted to a guarded assignment near the top so
an unreadable key fails loudly instead of sending an empty header.

The guest is named by the engine's own measurement (the pause's inventory), never
by a number written in here: `radarr` is container 103 on THIS machine because
`pct list` says so.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("scaffold")

#: the *arr family: one shape, one element, one file per app
_ARR_APPS = ("prowlarr", "radarr", "sonarr", "lidarr", "readarr", "whisparr")

#: `<ApiKey>…</ApiKey>` out of the first config.xml that has one. §17.1342 — the
#: sed script is DOUBLE-quoted, because this whole read is substituted inside a
#: `pct exec <gid> -- sh -c '…'` whose own quotes are single.
_ARR_EXTRACT = (r'sed -n "s:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p" | head -n 1')

#: §17.1382 — Jellyfin keeps no config.xml key: its API keys are rows in its own
#: SQLite database. The guest already has `/bin/python3`, whose `sqlite3` is in
#: the standard library, so this needs nothing installed. Read-only (`mode=ro`)
#: so a live server is never locked, and LOUD when there is no key: an empty
#: value here would go out as an empty header and come back 401, which reads as
#: a wrong key rather than a missing one (§17.1342's measured failure).
_JELLYFIN_DB = "/var/lib/jellyfin/data/jellyfin.db"
_JELLYFIN_READ = (
    "python3 -c 'import sqlite3,sys;"
    f"r=sqlite3.connect(\"file:{_JELLYFIN_DB}?mode=ro\",uri=True)"
    ".execute(\"SELECT AccessToken FROM ApiKeys WHERE Name=? ORDER BY rowid DESC LIMIT 1\",(\"scaffold-engine\",)).fetchone();"
    "print(r[0]) if r else sys.exit(\"jellyfin has no scaffold-engine key: create one, and never read a key belonging to another service\")'"
)


@dataclass(frozen=True)
class Readable:
    """A value the engine can read off a machine, and where from."""
    app: str
    paths: tuple[str, ...]
    extract: str = ""
    #: §17.1382 — a whole command that PRINTS the value, for a service whose key
    #: is not a line in a file. Unlike `extract`, this runs entirely inside the
    #: guest, because a database has to be opened where it lives.
    command: str = ""

    @property
    def where(self) -> str:
        return " or ".join(self.paths)

    def read(self, gid: Optional[str] = None) -> str:
        """The shell that prints the value, read INSIDE the guest that owns it.

        §17.1342 — `gid` is kept for the host-side form, but the host form does not
        work on an unprivileged runner: the runner raises its own privileges for the
        LEADING command only, so a `pct exec` inside `$( … )` runs as the runner's
        own user and fails with `Unable to load access control list`. The key then
        comes back EMPTY and the request goes out with an empty header — a 401 that
        looks like a wrong key. Measured on pve-runner, 2026-10-04.
        """
        if self.command:
            # §17.1382 — opened where it lives, so the whole read goes into the guest.
            return f"pct exec {gid} -- sh -c {_sq(self.command)}" if gid else self.command
        cat = "cat " + " ".join(self.paths) + " 2>/dev/null"
        if gid:
            cat = f"pct exec {gid} -- sh -c {_sq(cat)}"
        return f"{cat} | {self.extract}"


def _sq(s: str) -> str:
    """`s` as one single-quoted shell word."""
    return "'" + str(s).replace("'", "'\\''") + "'"


#: §17.1382 — every service whose API key the engine can read, by name. §17.1332
#: built this as a FAMILY (`_ARR_APPS`, one file, one element) and the next
#: service with a different shape fell straight through it back onto the
#: operator. A registry is the shape that generalises: adding a service is an
#: entry, and `read_on_the_machine` / `still_asked` need no change at all.
_SERVICES: dict[str, Readable] = {
    **{app: Readable(app=app, paths=(f"/var/lib/{app}/config.xml", "/config/config.xml"),
                     extract=_ARR_EXTRACT)
       for app in _ARR_APPS},
    "jellyfin": Readable(app="jellyfin", paths=(_JELLYFIN_DB,), command=_JELLYFIN_READ),
}

#: `<RADARR_API_KEY>`, `<SONARR_APIKEY>`, `<PROWLARR_KEY>`, `<JELLYFIN_API_KEY>` —
#: the shapes the drafter actually writes (live: all three of ADD115's were
#: `<APP_API_KEY>`; ADD134's was `<JELLYFIN_API_KEY>`).
_NAME_RE = re.compile(
    r"^(" + "|".join(a.upper() for a in sorted(_SERVICES)) + r")[_-]?(?:API[_-]?)?(?:KEY|TOKEN)$")


def readable_for(name: str) -> Optional[Readable]:
    """The `Readable` that answers input `name`, or None."""
    m = _NAME_RE.match(str(name or "").strip().upper())
    if not m:
        return None
    return _SERVICES.get(m.group(1).lower())


def guest_for(app: str, inventory: Optional[dict]) -> Optional[str]:
    """The guest id whose NAME is this app, from the pause's own inventory.

    None when the inventory does not name it — then nothing is substituted and
    the value stays an operator input, exactly as before (blindness invents
    nothing, §17.1289).
    """
    names = (inventory or {}).get("names") or {}
    hits = [str(gid) for gid, nm in names.items()
            if app in re.sub(r"[^a-z0-9]", "", str(nm or "").lower())]
    return hits[0] if len(hits) == 1 else None


def _for_context(value: str, command: str, placeholder: str) -> str:
    """§17.1386 — ``value``, safe to paste where ``placeholder`` sits in ``command``.

    Live, 2026-10-06: the engine filled a Jellyfin key into

        pct exec 101 -- sh -c 'curl … -H "X-Emby-Token: <JELLYFIN_API_KEY>" …'

    and the read it pasted was `$(python3 -c 'import sqlite3,…')`. The read's
    first single quote CLOSED the payload, and the runner answered

        bash: -c: line 1: syntax error near unexpected token `('

    having changed nothing. §17.1342 dodged this by writing the *arr extract
    with double quotes only, and that discipline does not survive a read that is
    a program with its own string literals. So the quoting is settled HERE,
    once, for every service: inside a single-quoted word, each `'` becomes
    `'\''` — close, a literal quote, reopen — which is what `_sq` does for a
    whole word.
    """
    before = command.split(placeholder)[0]
    # an ODD number of single quotes before the placeholder means it sits inside
    # a single-quoted word.
    if before.count("'") % 2 == 0:
        return value
    return value.replace("'", "'\\''")


def read_on_the_machine(commands: list[str], verify: list[str],
                        inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    """Substitute every machine-readable placeholder in the COMMANDS and the
    CHECKS, returning ``(commands, verify, notes)``.

    Inline, as a command substitution: the runner's judge reads a command, and
    an assignment in front of one is not a command it knows (live, it refused
    `K=$(pct exec …)` inside `bash -c`).

    A WRITTEN FILE is deliberately left alone. The file a template pushes runs
    inside the guest while the file that pushes it runs on the host, and the
    inner one usually arrives inside a quoted heredoc where nothing expands --
    a substitution placed there would read the wrong machine or never expand at
    all. The drafter already reads these keys inside its own scripts (live,
    ADD115's run block did); a secret placeholder that reaches a file is refused
    by `secrets_in_files` (§17.1280), and `still_asked` hands the drafter the
    read to use.
    """
    found: dict[str, Readable] = {}
    for t in list(commands or []) + list(verify or []):
        for name in re.findall(r"<([A-Z][A-Z0-9_]{2,40})>", str(t)):
            r = readable_for(name)
            if r is not None:
                found[name] = r
    if not found:
        return list(commands or []), list(verify or []), []

    notes: list[dict] = []
    out_cmds, out_verify = list(commands or []), list(verify or [])
    for name, r in sorted(found.items()):
        gid = guest_for(r.app, inventory)
        if gid is None:
            logger.info("machine_values: %s is readable but the inventory names no %s guest "
                        "-- left as an input", name, r.app)
            continue
        ph, inside = f"<{name}>", f"$({r.read(None)})"
        # §17.1342 — the read goes WHERE THE COMMAND GOES. A command already running
        # in that guest reads the key there, with no `pct` in the substitution; a host
        # command is left alone, because the host form cannot work (see `read`).
        hit = False
        for seq in (out_cmds, out_verify):
            for i, c in enumerate(seq):
                if ph in c and addresses_guest(c, gid):
                    # §17.1386 — the read is inserted into a payload that is
                    # usually `sh -c '…'`, and a single quote in the read closes
                    # that payload. §17.1342 avoided it by writing the *arr
                    # extract with double quotes only; Jellyfin's read is a
                    # python program whose own strings need quotes, so the
                    # SUBSTITUTION escapes for the context instead of every
                    # future read having to dodge it.
                    seq[i] = c.replace(ph, _for_context(inside, c, ph))
                    hit = True
        if not hit:
            logger.info("machine_values: %s needs the call to run inside guest %s -- left as an input",
                        name, gid)
            continue
        notes.append({"why": (
            f"read {name} inside guest {gid} instead of asking: {r.app} keeps it in {r.where} there, and "
            f"the call already runs in that guest, so the key never leaves the machine. Live (§17.1332), "
            f"ADD115's run block read all three *arr keys itself while the same step's checks asked the "
            f"operator to paste them.")})
    return out_cmds, out_verify, notes


# ── §17.1333: what the ENGINE knows about itself ──────────────────────────────
#: `<SCAFFOLD_ENGINE_URL>`, `<ENGINE_IP>`, `<SCAFFOLD_ENGINE_HOST>` — the engine's
#: own address on the operator's network. Never a name holding a credential.
_ENGINE_RE = re.compile(
    r"^(?:SCAFFOLD(?:_ENGINE)?|ENGINE)_(?:IP|HOST|ADDR|ADDRESS|URL|BASE_URL|ENDPOINT)$"
    r"|^SCAFFOLD_ENGINE$")
#: a URL-shaped name gets the whole surface, a host-shaped one the bare address
_URLISH_RE = re.compile(r"(?:URL|ENDPOINT)$")


def engine_port() -> str:
    """The port this engine serves on, from its OWN configuration."""
    try:
        from app.config import settings
        m = re.search(r":(\d{2,5})\b", str(settings.web_loopback_url or ""))
        if m:
            return m.group(1)
    except Exception:            # settings unreadable: the engine's default
        pass
    return "8000"


def engine_value(name: str, address: str, port: str = "") -> Optional[str]:
    """What `<name>` is worth given the engine's measured address, or None."""
    if not address or not _ENGINE_RE.match(str(name or "").strip().upper()):
        return None
    from app.modules.runbook_inputs import secret_name   # §17.1275 — one definition
    if secret_name(name):        # a key or a token is never an address
        return None
    if _URLISH_RE.search(name.upper()):
        return f"http://{address}:{port or engine_port()}"
    return address


def read_from_the_engine(commands: list[str], verify: list[str],
                         files: Optional[list[dict]], address: Optional[str],
                         port: str = "") -> tuple[list[str], list[str], Optional[list[dict]], list[dict]]:
    """§17.1333 — fill every placeholder that names the engine's own address.

    Live, ADD124 ("implement the scaffold-engine capability") said the engine's
    address was an "operator-supplied value". It is not: the engine measured it
    (`machine_truth.engine_address`). A literal address is safe in a written
    FILE too, unlike §17.1332's shell read, so files are filled here.
    """
    if not address:
        return list(commands or []), list(verify or []), files, []
    texts = list(commands or []) + list(verify or []) + \
        [str((f or {}).get("content") or "") for f in files or []]
    found: dict[str, str] = {}
    for t in texts:
        for name in re.findall(r"<([A-Z][A-Z0-9_]{2,40})>", str(t)):
            v = engine_value(name, address, port)
            if v:
                found[name] = v
    if not found:
        return list(commands or []), list(verify or []), files, []
    out_cmds, out_verify = list(commands or []), list(verify or [])
    out_files = [dict(f) for f in files or []] if files is not None else files
    notes: list[dict] = []
    for name, value in sorted(found.items()):
        ph = f"<{name}>"
        out_cmds = [c.replace(ph, value) for c in out_cmds]
        out_verify = [v.replace(ph, value) for v in out_verify]
        for f in out_files or []:
            body = str(f.get("content") or "")
            if ph in body:
                f["content"] = body.replace(ph, value)
        notes.append({"why": (
            f"filled {name} with {value}: that is this engine's own address, measured -- the machine the "
            f"engine drives reports it as the peer on the runner's port (`ss -tn`). Live (§17.1333), the "
            f"step called it an operator-supplied value.")})
    return out_cmds, out_verify, out_files, notes


def engine_still_asked(inputs: Optional[list[dict]], address: Optional[str]) -> list[dict]:
    """The other end: an input naming the engine's own address while the engine
    has measured it."""
    out: list[dict] = []
    for i in inputs or []:
        name = str((i or {}).get("name") or "")
        v = engine_value(name, address or "")
        if v:
            out.append({"command": f"the value <{name}>", "why": (
                f"<{name}> is not a question for the operator: this engine's own address is {v}, measured "
                f"from the machine it drives (`ss -tn`, the peer on the runner's port). Use it.")})
    return out


#: `pct exec 103 -- …`, `qm guest exec 106 -- …`: a command that runs INSIDE a guest
_ADDRESSES_RE = re.compile(r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+(\d{3,5})\b", re.I)


def addresses_guest(command: str, gid: str) -> bool:
    """§17.1342 — does this command run inside guest `gid`?"""
    return any(m.group(1) == str(gid) for m in _ADDRESSES_RE.finditer(str(command or "")))


def still_asked(inputs: Optional[list[dict]], inventory: Optional[dict] = None) -> list[dict]:
    """The inputs the frame would still ASK for although a machine holds them —
    the other end of the structural fix (§17.1085). One refusal per value, with
    the read in the remedy."""
    out: list[dict] = []
    for i in inputs or []:
        name = str((i or {}).get("name") or "")
        r = readable_for(name)
        if r is None:
            continue
        gid = guest_for(r.app, inventory)
        if gid is None:
            continue           # no machine is named for it: asking is all that is left
        out.append({"command": f"the value <{name}>", "why": (
            f"<{name}> is not a question for the operator: {r.app} writes it into {r.where} on guest {gid}. "
            f"Put the call INSIDE that guest and read the key there, in one command -- "
            f"`pct exec {gid} -- sh -c 'curl -s -H \"X-Api-Key: $({r.read(None)})\" "
            f"http://127.0.0.1:<port>/api/...'`. §17.1342: a `pct exec` inside `$( … )` on the host runs "
            f"unprivileged and returns nothing, so the header goes out empty and the app answers 401.")})
    return out


# ── §17.1383: and the block must read it where the service keeps it ──────────

#: an `<ApiKey>`-shaped extraction: the *arr family's element, however it is cut
#: (`sed -n 's:.*<ApiKey>…'`, `grep -oP '(?<=<ApiKey>)…'`, `xmllint --xpath`).
_APIKEY_READ_RE = re.compile(r"ApiKey\s*>|ApiKey</|<ApiKey", re.I)

#: `/etc/jellyfin/database.xml`, `/var/lib/radarr/config.xml` — a path that
#: belongs to a service the registry knows.
_SERVICE_PATH_RE = re.compile(r"/(?:etc|var/lib|config)/(?P<app>[a-z][a-z0-9]{2,20})\b[\w./-]*")


def _strip_comment(line: str) -> str:
    """`line` with a trailing `#` comment removed, quotes respected.

    §17.1383 — both shells and Python comment with `#`, and a `#` inside a
    quoted string is not a comment (`"#!/bin/sh"`, a URL fragment). Walking the
    quotes is cheap and keeps the gate off its own documentation.
    """
    q = ""
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
        elif ch == "#":
            return line[:i]
    return line


def reads_a_key_where_the_service_does_not_keep_it(
        commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1383 — the block reads a service's API key from a path that has none.

    Live, 2026-10-06. §17.1382 put Jellyfin in the registry, so the engine knows
    its key is a row in `jellyfin.db`. The substitution only fills a
    `<PLACEHOLDER>` in a command or a check, and a written FILE is deliberately
    left alone (see `read_on_the_machine`) -- so ADD134's next draft wrote its
    own read inside `/tmp/chain_proof.py`, and wrote the *arr shape::

        # Jellyfin API key -- read from the guest's config
        # Actually Jellyfin requires an API key. We'll read it from the guest.
        "grep -oP '(?<=<ApiKey>)[^<]+' /etc/jellyfin/database.xml | head -1"

    `/etc/jellyfin/database.xml` holds no `<ApiKey>`; measured, that directory is
    `database.xml encoding.xml logging.default.json logging.json network.xml
    system.xml` and the key is in the SQLite database. The grep returns nothing,
    the header goes out empty, and Jellyfin answers 401 -- §17.1342's failure
    again, one service further along. The engine held the right read the whole
    time and nothing put it in front of the drafter.

    Judged only when the path names a service the registry knows AND that
    service's own paths do not include it AND the read is `<ApiKey>`-shaped. A
    correct read (`/var/lib/radarr/config.xml`) says nothing, and a service the
    registry does not know says nothing.
    """
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    seen: set[str] = set()
    for t in texts:
        for raw in t.splitlines():
            # §17.1040/§17.1377 — judge the CODE, not the prose about it. The
            # first cut of this gate refused FILE_RULES' own worked example,
            # whose comment says "/etc/jellyfin holds NO <ApiKey>" -- the gate
            # matched the sentence warning against the defect.
            line = _strip_comment(raw)
            if not _APIKEY_READ_RE.search(line):
                continue
            for m in _SERVICE_PATH_RE.finditer(line):
                app = m.group("app").lower()
                r = _SERVICES.get(app)
                if r is None or app in seen:
                    continue
                path = m.group(0)
                if any(path.startswith(p) or p.startswith(path) for p in r.paths):
                    continue                   # reading it where it is kept
                if not r.command:
                    continue                   # same family, a different file: not this gate's call
                seen.add(app)
                out.append({"command": line.strip()[:120], "why": (
                    f"this reads {app}'s API key out of `{path}`, which does not hold one: {app} keeps it "
                    f"in {r.where}. MEASURED on this host (§17.1383) -- `/etc/jellyfin` is "
                    f"`database.xml encoding.xml logging.default.json logging.json network.xml "
                    f"system.xml`, no `<ApiKey>` in any of them -- so the grep returns nothing, the "
                    f"header goes out EMPTY and the service answers 401, which reads as a wrong key "
                    f"rather than one that was never there. Read it where it lives:\n\n    "
                    f"{r.read(None)}")})
    return out


#: §17.1384 — the auth endpoints of services whose key the engine can READ. Only
#: a service with a `Readable` belongs here: qBittorrent's `/api/v2/auth/login`
#: is NOT in this table, because qBittorrent genuinely has no readable key and
#: a username/password login is the right call there.
_AUTH_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "jellyfin": ("/users/authenticatebyname", "/users/authenticate"),
}


def authenticates_instead_of_reading_the_key(
        commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1384 — the block logs in to a service whose key the engine can read.

    Live, 2026-10-06. §17.1382 put Jellyfin's key in the registry and §17.1383
    taught the read in `FILE_RULES`. The next draft did not read the wrong file
    -- it stopped reading altogether and invented a credential::

        def jellyfin_login():
            url = f"http://{JELLYFIN_IP}:{JELLYFIN_PORT}/Users/AuthenticateByName"
            data = {"Username": "jellyfin", "Pw": MASS_PASSWORD}
            return http_json(url, method="POST", data=data)["AccessToken"]

    A guessed username, the operator's mass password, and no
    `X-Emby-Authorization` header, which that endpoint requires -- measured, it
    answers **HTTP 400**, and `GET /Users/Public` on this server answers `[]`, so
    there is no user of that name to authenticate as in the first place. The step
    had already proved five of its six hops and died on the one the engine could
    have answered from a dict.

    §17.1383 judges a key read from the wrong PATH; this judges not reading at
    all. Only a service with a `Readable` is judged, so qBittorrent's own
    username/password login -- which is the correct call for a service with no
    readable key -- is left alone.
    """
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    seen: set[str] = set()
    for t in texts:
        low = t.lower()
        for app, endpoints in _AUTH_ENDPOINTS.items():
            r = _SERVICES.get(app)
            if r is None or app in seen:
                continue
            if not any(e in low for e in endpoints):
                continue
            seen.add(app)
            out.append({"command": next(
                (l.strip()[:120] for l in t.splitlines()
                 if any(e in l.lower() for e in endpoints)), app), "why": (
                f"this logs in to {app} to get a token, and {app}'s key is a value the engine READS: "
                f"{r.where}. MEASURED live (§17.1384) on exactly this shape -- a drafted "
                f"`/Users/AuthenticateByName` with an invented username and the operator's mass "
                f"password answered **HTTP 400** (that endpoint also requires an "
                f"`X-Emby-Authorization` header), and `GET /Users/Public` on this server answers "
                f"`[]`, so there is no such user to authenticate as. Do not guess a credential for a "
                f"service whose key is on the disk. Read it:\n\n    {r.read(None)}")})
    return out


# ── §17.1385: and if the service has no key yet, the engine makes one ────────

#: §17.1385 — read-or-create, for a service whose credential the engine can
#: read. The columns are INTROSPECTED rather than assumed: this engine has one
#: Jellyfin to measure and `PRAGMA table_info` costs nothing, so a schema that
#: differs fails by NAME instead of on a guessed INSERT. The new row is written
#: with the service stopped and the service restarted after, because a running
#: Jellyfin need not re-read the table.
_JELLYFIN_CREATE = (
    "python3 -c '"
    "import secrets,sqlite3,subprocess,sys;"
    f"p=\"{_JELLYFIN_DB}\";"
    "c=sqlite3.connect(p);"
    "cols=[r[1] for r in c.execute(\"PRAGMA table_info(ApiKeys)\")];"
    "have=c.execute(\"SELECT 1 FROM ApiKeys WHERE Name=? LIMIT 1\",(\"scaffold-engine\",)).fetchone();"
    "print(\"jellyfin already has a scaffold-engine key\") if have else None;"
    "sys.exit(0) if have else None;"
    "tok=secrets.token_hex(16);"
    "vals={\"AccessToken\":tok,\"Name\":\"scaffold-engine\",\"AppName\":\"scaffold-engine\","
    "\"DateCreated\":\"now\",\"DateLastActivity\":\"now\"};"
    "unknown=[x for x in cols if x.lower() not in (\"id\",\"rowid\") "
    "and x not in vals];"
    "sys.exit(\"jellyfin ApiKeys has columns this engine does not know: \"+repr(unknown)"
    "+\" (all: \"+repr(cols)+\")\") if unknown else None;"
    "use=[x for x in cols if x in vals];"
    "subprocess.run([\"systemctl\",\"stop\",\"jellyfin\"],check=True);"
    "c.execute(\"INSERT INTO ApiKeys (\"+\",\".join(use)+\") VALUES (\""
    "+\",\".join(\"datetime(?)\" if vals[x]==\"now\" else \"?\" for x in use)+\")\","
    "[vals[x] for x in use]);"
    "c.commit();"
    "subprocess.run([\"systemctl\",\"start\",\"jellyfin\"],check=True);"
    "print(\"jellyfin: scaffold-engine key created\")'"
)


def create_key_on_the_machine(app: str, gid: Optional[str] = None) -> str:
    """§17.1385 — the command that makes a credential for ``app``, or ``""``.

    The operator approves it like any other machine change: it is work, not a
    check. The operator asked for exactly this -- *"Can't it just create one
    with my permission"* -- and the engine's own approval flow IS that
    permission, so nothing here is handed back to them to do by hand.

    Idempotent: it prints the existing key and exits 0 when there already is
    one, so approving it twice creates nothing twice.
    """
    if app != "jellyfin":
        return ""                              # the *arr family writes its own at install
    return f"pct exec {gid} -- sh -c {_sq(_JELLYFIN_CREATE)}" if gid else _JELLYFIN_CREATE


#: §17.1385 — the auth headers each readable service's HTTP API takes. A
#: host-side program using one of these needs a key the runner cannot inject.
_AUTH_HEADERS: dict[str, tuple[str, ...]] = {
    # §17.1415b — `api_key=` only as Jellyfin's URL QUERY parameter. As a bare substring it matched the
    # engine's own delivery line `V_RADARR_API_KEY=$(…)` and refused ADD123 for "calling jellyfin's API".
    "jellyfin": ("x-emby-token", "x-mediabrowser-token", "?api_key=", "&api_key="),
}


def a_host_program_needs_a_key_only_the_guest_can_read(
        commands: list[str], files: Optional[list[dict]] = None,
        policy: Optional[dict] = None) -> list[dict]:
    r"""§17.1385 — a host-side program calling a service whose key it cannot get.

    Live, 2026-10-06. ADD134's proof calls Jellyfin's API from a Python program
    that runs on the HOST. The engine fills `<JELLYFIN_API_KEY>` in a command or
    a check that runs inside the guest, and `read_on_the_machine` deliberately
    leaves a written file alone; the runner injects only names its stores hold,
    and Jellyfin's key is in neither. So the program has no way to obtain the
    token -- which is why four drafts in a row reached for a login instead
    (§17.1384). The engine kept refusing the symptom without ever saying where
    the value comes from for code in that position.

    The remedy is the Verify section, or a `pct exec` command: the step's own
    done-condition -- *"Jellyfin's API lists the title"* -- is a READ, and a read
    that runs inside the guest is filled before the operator is asked.

    Judged only for a service with a `Readable`, only when the name is in
    NEITHER runner store (an *arr key the runner holds is fine in a host
    program), and only for a file the block runs from the host.
    """
    held = ({str(x) for x in ((policy or {}).get("secrets") or [])}
            | {str(x) for x in ((policy or {}).get("held") or [])})
    run_text = "\n".join(str(c) for c in commands or [])
    out: list[dict] = []
    for f in files or []:
        path = str((f or {}).get("path") or "")
        body = str((f or {}).get("content") or "")
        if not path or path not in run_text:
            continue                           # not a file this block runs
        if "pct exec" in run_text.split(path)[0].rsplit("\n", 1)[-1]:
            continue                           # the program itself runs in a guest
        low = body.lower()
        for app, headers in _AUTH_HEADERS.items():
            r = _SERVICES.get(app)
            if r is None or not any(h in low for h in headers):
                continue
            if any(n in held for n in (f"{app.upper()}_API_KEY", f"{app.upper()}_KEY")):
                continue                       # the runner can inject it
            out.append({"command": f"the file {path}", "why": (
                f"{path} runs on the HOST and calls {app}'s API, and nothing can give it the key "
                f"there: the engine fills `<{app.upper()}_API_KEY>` only in a command or a check that "
                f"runs INSIDE the guest (a written file is left alone on purpose), and the runner "
                f"injects only the names its stores hold -- {app} is not one of them. MEASURED live "
                f"(§17.1385): four drafts in a row reached for a login instead, because this was the "
                f"one position with no answer. Put that call where the key can be read -- the Verify "
                f"section is the natural home, since the step's done-condition is a READ:\n\n    "
                f"pct exec <gid> -- sh -c 'curl -s -H \"X-Emby-Token: $({r.read(None)})\" "
                f"\"http://127.0.0.1:8096/Items?Recursive=true&SearchTerm=<TITLE>\"'\n\n"
                f"If that read says `ApiKeys is empty`, {app} has no key yet -- create one ONCE under "
                f"## Run this (it changes the machine, so it is work, not a check):\n\n    "
                f"{create_key_on_the_machine(app, '<gid>')}")})
    return out


# ── §17.1388: the engine writes the check it already knows ───────────────────

#: §17.1388 — how to read a service's own state back, for a service whose key
#: the engine can read. One entry per service, like everything else in the
#: registry: the engine knows the service, the key and the read, so the check is
#: the one thing it was still only DESCRIBING.
_CONFIRM: dict[str, tuple[str, str]] = {
    # (the inner payload, with {key} where the key read goes; what it shows)
    "jellyfin": (
        'curl -s --fail-with-body -H "X-Emby-Token: $({key})" '
        '"http://127.0.0.1:8096/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path"',
        "the movies Jellyfin's own API lists, with their paths",
    ),
}


def a_check_the_engine_can_write(commands: list[str], inventory: Optional[dict] = None
                                 ) -> list[tuple[str, str]]:
    """§17.1388 — ``[(check, why)]`` the engine can compose for this block.

    Live, 2026-10-06, after nine entries of getting everything else right: the
    drafter would mint Jellyfin's key, place the call correctly and then write no
    check, and §17.1345 refused the block five drafts running for proving
    nothing. The engine was not short of knowledge -- it had already COMPOSED the
    exact check inside §17.1385's refusal text, down to the key read. It simply
    had no way to put it in the Verify section itself, and kept asking a drafter
    that would not take it.

    So it writes it. Only for a service the registry knows, only when that
    service's guest is one this block addresses, and the composed check is a
    pure READ -- the operator still approves it, and `engine_fixed` says what was
    added, because a silent repair is not a repair (§17.1270).
    """
    out: list[tuple[str, str]] = []
    for app, (payload, shows) in _CONFIRM.items():
        r = _SERVICES.get(app)
        gid = guest_for(app, inventory)
        if r is None or gid is None:
            continue
        if not any(addresses_guest(str(c), gid) for c in commands or []):
            continue
        inner = payload.format(key=r.read(None))
        out.append((f"pct exec {gid} -- sh -c {_sq(inner)}",
                    f"the block changes {app} on guest {gid} and named no check, so the engine wrote "
                    f"one: it reads {shows}, with {app}'s key read where {app} keeps it. A step "
                    f"recorded done on exit codes alone is how a change that did nothing passes "
                    f"(§17.1345); the engine had already composed this read and could only describe "
                    f"it until now."))
    return out
