r"""§17.1656 — "update <arr app> to <version>" is the engine's own block, from the install it MEASURED.

Live (job d89078ed, 2026-10-19): the three update steps were drafted as `apt-get install --only-upgrade sonarr=…` /
`dpkg -l prowlarr` -- but every *arr app on this host is a release TARBALL under /opt/<App> (systemd ExecStart
`/opt/Sonarr/Sonarr -nobrowser -data=/var/lib/sonarr`); there is no package to upgrade. Two drafts needed the operator
to send them back with the install shape, and "Fix or document Knaben" re-drafted all three updates as
`systemctl stop … && apt-get install … && systemctl start` -- which would have left each app STOPPED.

The engine knows the shape from `service_truth` (argv + data dir + user), so the block is its own, in three phases
(each inside one runner call): download the official release first (the app keeps running), then stop / back up its
database and config.xml under /var/backups/<app>/ / unpack over the install dir / chown / start, then wait until the
API -- the key read inside the guest -- reports the asked version. Only an x86_64 tarball install under /opt is claimed.
"""
from __future__ import annotations

import re
from typing import Optional

#: app -> (release asset, port, API version)
RELEASES = {
    "radarr": ("https://github.com/Radarr/Radarr/releases/download/v{v}/Radarr.master.{v}.linux-core-x64.tar.gz", "7878", "v3"),
    "sonarr": ("https://github.com/Sonarr/Sonarr/releases/download/v{v}/Sonarr.main.{v}.linux-x64.tar.gz", "8989", "v3"),
    "prowlarr": ("https://github.com/Prowlarr/Prowlarr/releases/download/v{v}/Prowlarr.master.{v}.linux-core-x64.tar.gz", "9696", "v1"),
    "lidarr": ("https://github.com/Lidarr/Lidarr/releases/download/v{v}/Lidarr.master.{v}.linux-core-x64.tar.gz", "8686", "v1"),
    "readarr": ("https://github.com/Readarr/Readarr/releases/download/v{v}/Readarr.develop.{v}.linux-core-x64.tar.gz", "8787", "v1"),
}
_UPDATE_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:update|upgrade)\s+(?P<app>[A-Za-z]+)\b", re.I)
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+\.\d+)\b")

WAIT_PY = (
    "import sys,re,time,urllib.request,json\n"
    "port,ver,want,cfg=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]\n"
    "key=re.search(r'<ApiKey>([^<]*)</ApiKey>',open(cfg).read()).group(1)\n"
    "got=None\n"
    "for i in range(30):\n"
    "    try:\n"
    "        r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:%s/api/%s/system/status'%(port,ver),headers={'X-Api-Key':key}),timeout=4)\n"
    "        got=json.loads(r.read()).get('version')\n"
    "        if got==want: print('UPDATED: answers its API on version '+got); sys.exit(0)\n"
    "    except Exception as e: got=got or repr(e)\n"
    "    time.sleep(3)\n"
    "sys.exit('FAILED: wanted version %s, the API says %s'%(want,got))\n")


def _sq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def updated_app(node: Optional[dict]) -> tuple[str, str]:
    """(app, version) the step updates -- the app named in the title, the version from the title or its text."""
    title = str((node or {}).get("title") or "")
    m = _UPDATE_RE.match(title)
    if not m or m.group("app").lower() not in RELEASES:
        return "", ""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    v = _VERSION_RE.search(title) or _VERSION_RE.search(text)
    return (m.group("app").lower(), v.group(1)) if v else ("", "")


def block(node: Optional[dict], services: Optional[list]) -> Optional[tuple[list[str], list[str], str]]:
    app, version = updated_app(node)
    if not app:
        return None
    svc = next((s for s in services or [] if str(getattr(s, "name", "")).lower() == app and getattr(s, "guest", "")
                and not getattr(s, "vm", False)), None)
    if svc is None:
        return None
    exe = str(getattr(svc, "argv", "") or "").split()[0] if getattr(svc, "argv", "") else ""
    if not re.fullmatch(r"/opt/[\w.-]+/[\w.-]+", exe):
        return None                                       # not a tarball install under /opt: leave it to the draft
    install, data = exe.rsplit("/", 1)[0], str(getattr(svc, "data_dir", "") or f"/var/lib/{app}")
    user, group = str(getattr(svc, "user", "") or app), str(getattr(svc, "group", "") or getattr(svc, "user", "") or app)
    unit, gid = str(getattr(svc, "unit", "") or f"{app}.service"), str(svc.guest)
    url_t, port, api = RELEASES[app]
    url, tgz = url_t.format(v=version), f"/tmp/scaffold-{app}-{version}.tar.gz"
    bak = f"/var/backups/{app}"
    fetch = (f'[ "$(uname -m)" = x86_64 ] || {{ echo "FAILED: not an x86_64 guest"; exit 1; }}; '
             f"curl -fsSL -o {tgz} {url} && tar -tzf {tgz} >/dev/null && echo FETCHED {version}")
    swap = (f"set -e; ts=$(date +%Y%m%d%H%M%S); mkdir -p {bak}; systemctl stop {unit}; "
            f"for f in {data}/{app}.db {data}/config.xml; do if [ -f \"$f\" ]; then cp -a \"$f\" "
            f"\"{bak}/$(basename \"$f\").pre-update.$ts\"; fi; done; "
            f"tar -xzf {tgz} -C {install} --strip-components=1; chown -R {user}:{group} {install}; "
            f"systemctl start {unit}; rm -f {tgz}; echo SWAPPED {install} backups in {bak}")
    cmds = [f"pct exec {gid} -- sh -c {_sq(fetch)}",
            f"pct exec {gid} -- sh -c {_sq(swap)}",
            f"pct exec {gid} -- python3 -c {_sq(WAIT_PY)} {port} {api} {version} {data}/config.xml"]
    verify = [f"pct exec {gid} -- systemctl is-active {unit}"]
    note = (f"drafted the update from the engine's own shape (§17.1656): {app} on guest {gid} is a release tarball under "
            f"{install} (measured ExecStart), not a package -- the official {version} release is fetched first while the "
            f"app runs, then the app is stopped, its database and config.xml are copied to {bak}/, the release is "
            f"unpacked over {install}, ownership restored, the app started, and the step waits until its API reports "
            f"{version}.")
    return cmds, verify, note
