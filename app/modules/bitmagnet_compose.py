r"""§17.1671 — "write the official bitmagnet docker-compose.yml" is the engine's own file, from bitmagnet's install page.

Live (job 3cc24343 T12 "Create bitmagnet compose file", 2026-10-19; text: "Write official bitmagnet docker-compose.yml
(bitmagnet + PostgreSQL) with port 3333 mapped. Does NOT start containers."): the model wrote the compose from memory --
no `command:` (bitmagnet runs as `worker run --keys=http_server --keys=queue_server --keys=dht_crawler`; without it there
is no web UI, no Torznab and no crawler), no BitTorrent port 3334/tcp+udp, no config volume, no Postgres `shm_size`.
The engine already carries its own deploy shapes (FlareSolverr §17.1516, Byparr §17.1535); this is the same for
bitmagnet: OFFICIAL_COMPOSE below is https://bitmagnet.io/setup/installation.html as published (read 2026-10-19), with
only the relative paths kept relative to the directory the step names. The file is carried as base64 (no quoting of
YAML through two shells), any existing file is kept as a dated .bak, the copy is checksum-compared and parsed by
`docker compose config` -- and nothing is started.
"""
from __future__ import annotations

import base64
import hashlib
import re
import shlex
from typing import Optional

#: https://bitmagnet.io/setup/installation.html (Docker Compose), as published 2026-10-19
OFFICIAL_COMPOSE = """services:
  bitmagnet:
    image: ghcr.io/bitmagnet-io/bitmagnet:latest
    container_name: bitmagnet
    ports:
      # API and WebUI port:
      - "3333:3333"
      # BitTorrent ports:
      - "3334:3334/tcp"
      - "3334:3334/udp"
    restart: unless-stopped
    environment:
      - POSTGRES_HOST=postgres
      - POSTGRES_PASSWORD=postgres
    #      - TMDB_API_KEY=your_api_key
    volumes:
      - ./config:/root/.config/bitmagnet
    command:
      - worker
      - run
      - --keys=http_server
      - --keys=queue_server
      # disable the next line to run without DHT crawler
      - --keys=dht_crawler
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    container_name: bitmagnet-postgres
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    #    ports:
    #      - "5432:5432" Expose this port if you'd like to dig around in the database
    restart: unless-stopped
    environment:
      - POSTGRES_PASSWORD=postgres
      - POSTGRES_DB=bitmagnet
      - PGUSER=postgres
    shm_size: 1g
    healthcheck:
      test:
        - CMD-SHELL
        - pg_isready
      start_period: 20s
      interval: 10s
"""

_TITLE_RE = re.compile(r"^\s*(?:create|write|add|place|make)\b[^\n]{0,40}\bbitmagnet\b[^\n]{0,30}\bcompose\b", re.I)
_DIR_RE = re.compile(r"(?P<dir>/[\w.@+-]+(?:/[\w.@+-]+)*?)/(?:docker-)?compose\.ya?ml\b")
_CT_RE = re.compile(r"\b(?:CT|container|LXC)\s+(?P<gid>\d{3,5})\b", re.I)
#: a guest the draft targets: `pct exec N`, or the run_in_container template's own `GID=N` / `/tmp/in_ct_N.sh` (live, T12's
#: draft named its guest only that way and the first version of this capability never fired)
_PCT_RE = re.compile(r"\bpct\s+(?:exec|push)\s+(\d{3,5})\b|\bGID=(\d{3,5})\b|/in_ct_(\d{3,5})\.sh\b")
_STARTS_RE = re.compile(r"\bdoes\s+not\s+start\b|\bdon'?t\s+start\b|\bnot\s+start(?:ed)?\b", re.I)


def target(node: Optional[dict], commands: Optional[list[str]] = None,
           files: Optional[list[dict]] = None) -> tuple[str, str]:
    """(guest id, directory) the compose file goes to -- or empty strings."""
    title = str((node or {}).get("title") or "")
    if not _TITLE_RE.match(title):
        return "", ""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    drafted = " ".join([str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []])
    ct = _CT_RE.search(text)
    gids = {next(g for g in m.groups() if g) for m in _PCT_RE.finditer(drafted)}
    gid = ct.group("gid") if ct else (next(iter(gids)) if len(gids) == 1 else "")
    d = _DIR_RE.search(text) or _DIR_RE.search(drafted)
    return (gid, d.group("dir") if d else "/opt/bitmagnet") if gid else ("", "")


def block(node: Optional[dict], commands: Optional[list[str]] = None,
          files: Optional[list[dict]] = None) -> Optional[tuple[list[str], list[str], str]]:
    gid, d = target(node, commands, files)
    if not gid:
        return None
    path = f"{d}/docker-compose.yml"
    b64 = base64.b64encode(OFFICIAL_COMPOSE.encode()).decode()
    sha = hashlib.sha256(OFFICIAL_COMPOSE.encode()).hexdigest()
    p, dq = shlex.quote(path), shlex.quote(d)
    write = (f"mkdir -p {dq}/config {dq}/data/postgres; "
             f"if [ -f {p} ]; then cp -a {p} {p}.bak.$(date +%Y%m%d%H%M%S); fi; "
             f"echo {b64} | base64 -d > {p}.scaffold-new && "
             f"[ \"$(sha256sum < {p}.scaffold-new | cut -d' ' -f1)\" = {sha} ] && mv -f {p}.scaffold-new {p} && "
             f"echo \"WROTE {path}: bitmagnet's published compose (sha256 {sha[:12]})\"")
    check = f"cd {dq} && docker compose config --quiet && echo \"PARSES: docker compose reads {path}\""
    cmds = [f"pct exec {gid} -- sh -c {shlex.quote(write)}", f"pct exec {gid} -- sh -c {shlex.quote(check)}"]
    verify = [f"pct exec {gid} -- cat {p}"]
    started = "" if _STARTS_RE.search(" ".join(str((node or {}).get(k) or "") for k in ("prompt_template", "description"))) \
        else " (the step does not say to start it; starting is the next step's)"
    note = ("wrote bitmagnet's published docker-compose.yml (§17.1671, https://bitmagnet.io/setup/installation.html): "
            "the `worker run --keys=http_server --keys=queue_server --keys=dht_crawler` command, the web/API port 3333, "
            f"the BitTorrent port 3334 tcp+udp, the config and Postgres data volumes under {d}, Postgres 16 with its "
            "health check -- the draft had none of the command, port 3334 or the volumes; any existing file is kept as "
            f"a dated .bak, the copy is checksum-checked and parsed by `docker compose config`, and nothing is started{started}.")
    return cmds, verify, note


#: §17.1671b — "start the bitmagnet stack"
_START_RE = re.compile(r"^\s*(?:start|bring\s+up|launch|run|deploy)\b[^\n]{0,30}\bbitmagnet\b", re.I)


def start_block(node: Optional[dict], commands: Optional[list[str]] = None,
                files: Optional[list[dict]] = None) -> Optional[tuple[list[str], list[str], str, dict]]:
    """Live (job 3cc24343 T13 "Start bitmagnet stack"): every check the draft had (`docker compose ps`, `ss | grep 3333`)
    sat inside its in-guest script, so the frame had none and was refused. The engine starts it and judges it: pull
    (bounded), `up -d`, then wait for Postgres healthy, bitmagnet running and the web UI answering 200 on 3333 -- and,
    behind the kill-switch, that the bitmagnet CONTAINER's public address is the tunnel's: its traffic is forwarded,
    never through the container host's OUTPUT, so this is the leak that matters."""
    title = str((node or {}).get("title") or "")
    if not _START_RE.match(title):
        return None
    gid, d = target({**(node or {}), "title": "write bitmagnet compose"}, commands, files)
    if not gid:
        return None
    dq = shlex.quote(d)
    # §17.1671c — the pull runs DETACHED and is waited on across calls (§17.1317's shape): live, ghcr's bitmagnet image
    # did not arrive inside one 170 s call through the tunnel (exit 124, Postgres had)
    images = " ".join(re.findall(r"^\s*image:\s*(\S+)", OFFICIAL_COMPOSE, re.M))
    have = f"for im in {images}; do docker image inspect \"$im\" >/dev/null 2>&1 || exit 1; done"
    pull = (f"if ( {have} ); then echo \"PULLED: the images are already here\"; exit 0; fi; "
            "systemctl is-active --quiet scaffold-bitmagnet-pull || systemd-run --unit scaffold-bitmagnet-pull --collect "
            f"-p WorkingDirectory={d} docker compose pull -q >/dev/null && echo \"PULLING in the background\"")
    def waiting(last: bool) -> str:
        return (f"for i in $(seq 1 33); do systemctl is-active --quiet scaffold-bitmagnet-pull || break; sleep 5; done; "
                f"if ( {have} ); then echo \"PULLED: {images}\"; exit 0; fi; "
                "if systemctl is-active --quiet scaffold-bitmagnet-pull; then "
                + ('echo "FAILED: the image pull is still running after every wait"; exit 1; fi; ' if last else
                   'echo "STILL PULLING (the next command keeps waiting)"; exit 0; fi; ')
                + 'echo "FAILED: the pull ended without the images"; exit 1')
    up = f"cd {dq} && docker compose up -d && echo \"STARTED the stack in {d}\""
    # bounded by the CLOCK, not a count: live, 75 × (2 s + a 3 s curl) outran the runner's 180 s
    wait = ('end=$(( $(date +%s) + 150 )); while [ "$(date +%s)" -lt "$end" ]; do '
            'pg=$(docker inspect -f "{{.State.Health.Status}}" bitmagnet-postgres 2>/dev/null); '
            'bm=$(docker inspect -f "{{.State.Running}}" bitmagnet 2>/dev/null); '
            'code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:3333/); '
            'case "$code" in 2*|3*) web=yes;; *) web="";; esac; '       # live: the UI answers 301 to its own page
            'if [ "$pg" = healthy ] && [ "$bm" = true ] && [ -n "$web" ]; then '
            'echo "UP: postgres healthy, bitmagnet running, web UI answers $code on 3333"; exit 0; fi; sleep 2; done; '
            'echo "FAILED: postgres=$pg bitmagnet_running=$bm web_ui_http=$code after 150 s"; '
            'docker logs --tail 30 bitmagnet 2>&1; exit 1')
    egress = ('ct=$(curl -s -m 15 https://ipinfo.io/ip); '
              'in=$(docker exec bitmagnet sh -c "wget -qO- -T 15 https://ipinfo.io/ip 2>/dev/null || curl -s -m 15 https://ipinfo.io/ip" 2>/dev/null); '
              'if [ -z "$in" ]; then echo "NOT TESTED: the bitmagnet image has no wget or curl to read its own address"; exit 0; fi; '
              'if [ "$in" = "$ct" ]; then echo "THROUGH THE TUNNEL: the bitmagnet container leaves as $in, the same as wg0"; '
              'else echo "FAILED: the bitmagnet container leaves as $in, the container host as $ct"; exit 1; fi')
    q = shlex.quote
    cmds = [f"pct exec {gid} -- sh -c {q(pull)}"] + [f"pct exec {gid} -- sh -c {q(waiting(i == 5))}" for i in range(6)] + [
            f"pct exec {gid} -- sh -c {q(up)}",
            f"pct exec {gid} -- sh -c {q(wait)}", f"pct exec {gid} -- sh -c {q(egress)}"]
    verify = [f'pct exec {gid} -- curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:3333/', f"pct exec {gid} -- ss -tln"]
    note = (f"started bitmagnet in container {gid} as the engine's own block (§17.1671b): images pulled in the background and "
            "waited on across calls (up to ~16 min), "
            f"`docker compose up -d` in {d}, then it waits up to 150 s for Postgres healthy, bitmagnet running and the web "
            "UI answering 200 on 3333, and reads the bitmagnet container's public address -- it must be the tunnel's, "
            "since a container's traffic is forwarded and never passes the host's OUTPUT rules.")
    return cmds, verify, note, {"always_run": ("the block proves where the bitmagnet container's traffic leaves; a port "
                                               "that answers says nothing about that")}
