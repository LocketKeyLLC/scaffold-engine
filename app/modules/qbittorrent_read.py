r"""§17.1708 — "read/list qBittorrent's torrents" is the engine's own read block.

qBittorrent has no readable API key (§17.1384): every read of its Web API is a login (`POST /api/v2/auth/login` with
`admin` and the stored password) and then a GET. A read-only step therefore could never be drafted in a shape the
engine accepts -- a piped program that POSTs is a change by §17.1680c's rule, a `curl | python3 -c` is not a read
(§17.1673b), and a raw `torrents/info` overruns the runner's 20,000 characters (§17.1691). Live (job e5e22b92 T1
"Login qBittorrent list torrents", 2026-10-20): drafted five ways, refused or cut each time, finally skipped; and later
the S3 Mighty Morphin pack Sonarr "sent to qBittorrent" could not be looked for at all.

The engine reads it itself, inside qBittorrent's container: the password reaches the program through the environment
(expanded on the host by the runner, which holds it; never printed), the program logs in to its own loopback Web UI,
GETs `/api/v2/torrents/info` and prints totals by state and category and one short line per torrent -- filtered to the
names the step quotes, when it quotes any. Nothing in qBittorrent changes: a login only opens a session.
"""
from __future__ import annotations

import re
import shlex
from typing import Callable, Optional

_PORT = "8080"
_USER = "admin"

#: runs inside qBittorrent's guest as `python3 -` with QBT_PASS and QBT_FILTER in its environment
READ_PY = (
    "import os, sys, json, urllib.request, urllib.parse, http.cookiejar\n"
    "base = 'http://127.0.0.1:" + _PORT + "'\n"
    "jar = http.cookiejar.CookieJar()\n"
    "op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))\n"
    "body = urllib.parse.urlencode({'username': '" + _USER + "', 'password': os.environ.get('QBT_PASS', '')}).encode()\n"
    "req = urllib.request.Request(base + '/api/v2/auth/login', data=body, headers={'Referer': base})\n"
    "ans = op.open(req, timeout=30).read().decode(errors='replace').strip()\n"
    "if ans != 'Ok.':\n"
    "    print('FAILED: qBittorrent refused the login: ' + ans[:80]); sys.exit(1)\n"
    "ts = json.loads(op.open(base + '/api/v2/torrents/info', timeout=60).read() or b'[]')\n"
    "words = [w.lower() for w in json.loads(os.environ.get('QBT_FILTER') or '[]')]\n"
    "def pick(t):\n"
    "    n = str(t.get('name') or '').lower()\n"
    "    return all(w in n for w in words)\n"
    "sel = [t for t in ts if pick(t)]\n"
    "by_state, by_cat = {}, {}\n"
    "for t in ts:\n"
    "    by_state[t.get('state')] = by_state.get(t.get('state'), 0) + 1\n"
    "    by_cat[t.get('category') or '-'] = by_cat.get(t.get('category') or '-', 0) + 1\n"
    "print('TORRENTS: %d' % len(ts))\n"
    "print('BY STATE: ' + ', '.join('%s=%d' % kv for kv in sorted(by_state.items(), key=lambda kv: -kv[1])))\n"
    "print('BY CATEGORY: ' + ', '.join('%s=%d' % kv for kv in sorted(by_cat.items(), key=lambda kv: -kv[1])))\n"
    "if words:\n"
    "    print('MATCHING %s: %d' % (' '.join(words), len(sel)))\n"
    "sel.sort(key=lambda t: -(t.get('added_on') or 0))\n"
    "for t in sel[:60]:\n"
    "    print('%s | %s | %.0f%% | %.1f GB | seeds %s | %s | %s' % (str(t.get('name') or '')[:80], t.get('state'),\n"
    "          100 * float(t.get('progress') or 0), float(t.get('size') or 0) / 1e9, t.get('num_seeds'),\n"
    "          t.get('category') or '-', str(t.get('hash') or '')[:8]))\n"
    "if len(sel) > 60:\n"
    "    print('... and %d more' % (len(sel) - 60))\n"
)

_QBT_RE = re.compile(r"\bq(?:bit(?:torrent)?|bt)\b", re.I)
_READ_WORDS_RE = re.compile(r"\b(?:list|read|show|check|inspect|look|find|status|torrents?)\b", re.I)
#: §17.1711 — a quote opens and closes at a word boundary: the apostrophe in "Sonarr's" is not a quote
_QUOTED_RE = re.compile(r"""(?<![\w])['"“‘]([^'"”’\n]{3,60})['"”’](?![\w])""")
#: §17.1711 — qBittorrent is the step's SUBJECT: in its title, or "qBittorrent('s) torrents" / "torrents in qBittorrent"
_SUBJECT_RE = re.compile(r"\bq(?:bit(?:torrent)?|bt)(?:'s)?\s+torrents?\b|\btorrents?\s+in\s+q(?:bit(?:torrent)?|bt)\b", re.I)


def wants(node: Optional[dict]) -> bool:
    """A step that only reads, about qBittorrent's torrents -- qBittorrent its subject, not a word in passing.

    §17.1711 — live (job e5e22b92 PI1, a Sonarr manual-import preview that mentioned "its qBittorrent download client
    settings"): the step was claimed and its "name filter" read from "Sonarr's container … find Sonarr"."""
    from app.modules.supervised_runs import only_reads
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    subject = bool(_QBT_RE.search(title)) or bool(_SUBJECT_RE.search(text))
    return bool(subject and _READ_WORDS_RE.search(text) and only_reads(node))


def name_filter(node: Optional[dict]) -> list[str]:
    """The words of the first name the step quotes (e.g. 'Mighty Morphin'), lower-cased; [] for all torrents."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    for m in _QUOTED_RE.finditer(text):
        q = m.group(1).strip()
        if not re.search(r"(?i)qbit|torrents?/info|api/v2|admin|password", q):
            return [w for w in re.split(r"\s+", q.lower()) if w]
    return []


def command(gid: str, words: list[str]) -> str:
    import json
    return (f'pct exec {gid} -- env QBT_PASS="$MASS_PASSWORD" QBT_FILTER={shlex.quote(json.dumps(words))} '
            f"python3 - <<'PY'\n{READ_PY}PY")


def is_engine_read(cmd: str) -> bool:
    """The engine's own qBittorrent read, exactly as `command` writes it."""
    c = str(cmd or "")
    return READ_PY in c and bool(re.match(r"\s*pct\s+exec\s+\d{3,5}\s+--\s+env\s+QBT_PASS=\"\$MASS_PASSWORD\"\s+QBT_FILTER=", c))


def block(node: Optional[dict], qbt_guest: Callable[[], Optional[str]]) -> Optional[tuple[list[str], list[str], str]]:
    if not wants(node):
        return None
    gid = qbt_guest()
    if not gid:
        return None
    words = name_filter(node)
    note = (f"read qBittorrent as the engine's own block (§17.1708): inside guest {gid}, the stored password reaches the "
            "program through the environment (never printed), it logs in to its loopback Web UI and GETs "
            "/api/v2/torrents/info, printing totals by state and category and one line per torrent"
            + (f" whose name has {' '.join(words)!r}" if words else "") + ". Nothing in qBittorrent changes.")
    return [command(gid, words)], [], note
