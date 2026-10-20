r"""§17.1715 — "add the public tracker list to qBittorrent (new downloads / the stuck torrents)" is the engine's own block.

Live (job e5e22b92 QS1, 2026-10-20): qBittorrent is connected (DHT 366, the VPN's forwarded port 3092 on wg0), but
`add_trackers_enabled` is False and ~27 magnets never fetched their metadata -- many carry one or two trackers. The
operator asked for a public tracker list on new downloads AND on the stuck torrents. Every qBittorrent call needs a
login first (§17.1384/1708), which no drafted shape carried; this is the same login, inside the guest, the password
through the environment, never printed.

The program fetches the list (ngosang/trackerslist `trackers_best.txt`, through the guest's own VPN) and refuses with
nothing changed unless at least 5 lines are tracker URLs; then, as the step asks:
  new   -- `app/setPreferences` {add_trackers_enabled: true, add_trackers: <list>}, read back;
  stuck -- every torrent in metaDL or stalledDL gets the list (`torrents/addTrackers`) and a `torrents/reannounce`,
           then its tracker count is read back.
The step's check is a separate read of qBittorrent's own config file (`AddTrackersEnabled=true`).
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Callable, Optional

_PORT = "8080"
_USER = "admin"
LIST_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
CONF = "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf"

#: runs inside qBittorrent's guest as `python3 -` with QBT_PASS, QBT_MODES and QBT_LIST in its environment
TRACKERS_PY = (
    "import os, sys, json, time, urllib.request, urllib.parse, urllib.error, http.cookiejar\n"
    "base = 'http://127.0.0.1:" + _PORT + "'\n"
    "modes = set(os.environ.get('QBT_MODES', '').split(','))\n"
    "try:\n"
    "    raw = urllib.request.urlopen(os.environ['QBT_LIST'], timeout=30).read().decode(errors='replace')\n"
    "except Exception as e:\n"
    "    print('FAILED: could not fetch the tracker list (%r) -- nothing changed' % e); sys.exit(1)\n"
    "tr = [l.strip() for l in raw.splitlines() if l.strip()]\n"
    "tr = [l for l in tr if l.split('://', 1)[0] in ('udp', 'http', 'https', 'wss') and ' ' not in l]\n"
    "if len(tr) < 5:\n"
    "    print('FAILED: the tracker list holds %d tracker URLs -- nothing changed' % len(tr)); sys.exit(1)\n"
    "print('LIST: %d trackers (first: %s)' % (len(tr), tr[0]))\n"
    "jar = http.cookiejar.CookieJar()\n"
    "op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))\n"
    "def call(p, form=None):\n"
    "    data = urllib.parse.urlencode(form).encode() if form is not None else None\n"
    "    return op.open(urllib.request.Request(base + p, data=data, headers={'Referer': base}), timeout=60).read()\n"
    "if call('/api/v2/auth/login', {'username': '" + _USER + "', 'password': os.environ.get('QBT_PASS', '')}).decode().strip() != 'Ok.':\n"
    "    print('FAILED: qBittorrent refused the login -- nothing changed'); sys.exit(1)\n"
    "if 'new' in modes:\n"
    "    call('/api/v2/app/setPreferences', {'json': json.dumps({'add_trackers_enabled': True, 'add_trackers': '\\n'.join(tr)})})\n"
    "    pr = json.loads(call('/api/v2/app/preferences'))\n"
    "    n = len([l for l in str(pr.get('add_trackers') or '').splitlines() if l.strip()])\n"
    "    print('NEW DOWNLOADS: add_trackers_enabled=%s, %d trackers set' % (pr.get('add_trackers_enabled'), n))\n"
    "if 'stuck' in modes:\n"
    "    ts = json.loads(call('/api/v2/torrents/info'))\n"
    "    stuck = [t for t in ts if t.get('state') in ('metaDL', 'stalledDL')]\n"
    "    print('STUCK: %d torrents (metaDL or stalledDL) of %d' % (len(stuck), len(ts)))\n"
    "    if stuck:\n"
    # §17.1715b — torrents/addTrackers takes ONE `hash` (live: `hashes` answered 400); reannounce takes `hashes`
    "        added = 0\n"
    "        for t in stuck:\n"
    "            try:\n"
    "                call('/api/v2/torrents/addTrackers', {'hash': t['hash'], 'urls': '\\n'.join(tr)}); added += 1\n"
    "            except (TimeoutError, OSError, urllib.error.URLError) as e:\n"
    "                print('  addTrackers failed for %s (%r)' % (str(t.get('name') or '')[:60], e))\n"
    "        print('ADDED: trackers to %d of %d stuck torrents' % (added, len(stuck)))\n"
    "        hs = '|'.join(t['hash'] for t in stuck)\n"
    "        call('/api/v2/torrents/reannounce', {'hashes': hs})\n"
    "        time.sleep(3)\n"
    "        for t in stuck[:40]:\n"
    "            try:\n"
    "                n = len([x for x in json.loads(call('/api/v2/torrents/trackers?hash=' + t['hash'])) if '://' in str(x.get('url'))])\n"
    "            except (TimeoutError, OSError, urllib.error.URLError) as e:\n"
    "                print('  %s | %s | trackers unreadable (%r)' % (str(t.get('name') or '')[:70], t.get('state'), e)); continue\n"
    "            print('  %s | %s | %d trackers' % (str(t.get('name') or '')[:70], t.get('state'), n))\n"
    "time.sleep(8)\n"
    "print('DONE')\n"
)

_TITLE_RE = re.compile(r"\b(?:add|set|apply|enable)\b[^.\n]{0,60}\btrackers?\b|\btrackers?\b[^.\n]{0,40}\b(?:add|set|apply|enable)", re.I)
_QBT_RE = re.compile(r"\bq(?:bit(?:torrent)?|bt)\b", re.I)
_NEW_RE = re.compile(r"\bnew\s+(?:downloads?|torrents?)\b|\bautomatically\b|\bpreferences?\b|\bsettings?\b", re.I)
_STUCK_RE = re.compile(r"\b(?:stuck|stalled|metadl|metadata)\b", re.I)


def modes(node: Optional[dict]) -> list[str]:
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not (_QBT_RE.search(text) and _TITLE_RE.search(str((node or {}).get("title") or ""))):
        return []
    return [m for m, rx in (("new", _NEW_RE), ("stuck", _STUCK_RE)) if rx.search(text)]


def command(gid: str, ms: list[str]) -> str:
    return (f'pct exec {gid} -- env QBT_PASS="$MASS_PASSWORD" QBT_MODES={shlex.quote(",".join(ms))} '
            f"QBT_LIST={shlex.quote(LIST_URL)} python3 - <<'PY'\n{TRACKERS_PY}PY")


def block(node: Optional[dict], qbt_guest: Callable[[], Optional[str]]) -> Optional[tuple[list[str], list[str], str]]:
    ms = modes(node)
    if not ms:
        return None
    gid = qbt_guest()
    if not gid:
        return None
    verify = [f"pct exec {gid} -- grep -c -i 'AddTrackersEnabled=true' {CONF}"] if "new" in ms else []
    what = " and ".join({"new": "new downloads (add_trackers_enabled + the list)",
                         "stuck": "every metaDL/stalledDL torrent (addTrackers + reannounce)"}[m] for m in ms)
    note = (f"added the public tracker list as the engine's own block (§17.1715): inside guest {gid}, the list fetched "
            f"through the guest's VPN and refused unless it holds at least 5 tracker URLs, a login with the stored "
            f"password via the environment, then {what}, each read back; the check reads qBittorrent's own config.")
    return [command(gid, ms)], verify, note
