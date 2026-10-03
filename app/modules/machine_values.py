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

#: `<ApiKey>…</ApiKey>` out of the first config.xml that has one
_ARR_EXTRACT = (r"sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' | head -n 1")

#: `<RADARR_API_KEY>`, `<SONARR_APIKEY>`, `<PROWLARR_KEY>` — the shapes the
#: drafter actually writes (live: all three of ADD115's were `<APP_API_KEY>`).
_NAME_RE = re.compile(
    r"^(" + "|".join(a.upper() for a in _ARR_APPS) + r")[_-]?(?:API[_-]?)?KEY$")


@dataclass(frozen=True)
class Readable:
    """A value the engine can read off a machine, and where from."""
    app: str
    paths: tuple[str, ...]
    extract: str

    @property
    def where(self) -> str:
        return " or ".join(self.paths)

    def read(self, gid: Optional[str] = None) -> str:
        """The shell that prints the value. With ``gid``, from the host through
        `pct exec`; without, from inside the guest the script already runs in."""
        cat = "cat " + " ".join(self.paths) + " 2>/dev/null"
        if gid:
            cat = f"pct exec {gid} -- sh -c {_sq(cat)}"
        return f"{cat} | {self.extract}"


def _sq(s: str) -> str:
    """`s` as one single-quoted shell word."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def readable_for(name: str) -> Optional[Readable]:
    """The `Readable` that answers input `name`, or None."""
    m = _NAME_RE.match(str(name or "").strip().upper())
    if not m:
        return None
    app = m.group(1).lower()
    return Readable(app=app, paths=(f"/var/lib/{app}/config.xml", "/config/config.xml"),
                    extract=_ARR_EXTRACT)


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
        ph, inline = f"<{name}>", f"$({r.read(gid)})"
        out_cmds = [c.replace(ph, inline) for c in out_cmds]
        out_verify = [v.replace(ph, inline) for v in out_verify]
        notes.append({"why": (
            f"read {name} off the machine instead of asking: {r.app} keeps it in {r.where} on guest "
            f"{gid}. Live (§17.1332), ADD115's run block read all three *arr keys itself while the "
            f"same step's checks asked the operator to paste them.")})
    return out_cmds, out_verify, notes


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
            f"<{name}> is not a question for the operator: {r.app} writes it into {r.where} on its own "
            f"guest, so read it there -- `{r.read(gid)}` -- and use that command substitution in the "
            f"command. Live (§17.1332), three *arr keys were asked for in checks while the same "
            f"step's run block read them off the disk.")})
    return out
