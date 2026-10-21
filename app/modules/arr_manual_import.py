r"""§17.1712 — "import these download folders into these seasons" is the engine's own Sonarr manual import.

Live (job e5e22b92 PI2, 2026-10-20, an operator-approved import of two finished Power Rangers packs): two drafts, both
passing every gate, both wrong in ways that change the library -- the first took `SERIES_ID` from `/series[0]` (the
first show in the library, not Power Rangers) and read the key from a path that holds none; the second used the TVDB id
72553 as Sonarr's series id and POSTed the ManualImport command TWICE. And the preview it builds on has a trap: with a
`seriesId`, Sonarr's `/manualimport` previews the series' own library folder and ignores `folder` (PI1's second run).

The mapping is fixed, so the engine writes it. A step claims it by stating the mapping plainly:

    tvdbId 72553
    FOLDER /media/downloads/<pack A> -> SEASON 26 COUNT 22
    FOLDER /media/downloads/<pack B> -> SEASON 21 COUNT 20 SKIP EXTENDED

Inside Sonarr's guest the program finds the series by its tvdbId; for each folder it reads Sonarr's own preview (folder
URL-encoded, NO seriesId), drops files whose names hold a SKIP word, maps each file by the episode number in ITS OWN
name (`S01E07` -> episode 7) to the TARGET season, and refuses before importing anything when a file does not map or a
COUNT is off. Then ONE `ManualImport` command (copy: the torrent keeps seeding), waited on, and a per-season result.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

_PORT = "8989"
_TVDB_RE = re.compile(r"\btvdbId\s*[:=]?\s*(\d{4,8})\b", re.I)
#: §17.1722 — a path with spaces is quoted: `FOLDER "/media/downloads/Power Rangers Super Ninja Steel (Season 25, 2018)" ->`
_FOLDER_RE = re.compile(r"""\bFOLDER\s+(?:"(?P<qpath>/[^"\n]+)"|(?P<path>/\S+))\s*->\s*SEASON\s+(?P<season>\d{1,3})"""
                        r"(?:\s+COUNT\s+(?P<count>\d{1,4}))?(?:\s+SKIP\s+(?P<skip>[A-Za-z0-9._-]+))?", re.I)
_TITLE_RE = re.compile(r"\bimport\b", re.I)

#: runs inside Sonarr's guest as `python3 -c "<this>" <key> <plan json>` -- no double quotes, no `$`, no backslash-quote
IMPORT_PY = (
    "import sys,json,re,time,urllib.request,urllib.parse,urllib.error\n"
    "KEY=sys.argv[1]; PLAN=json.loads(sys.argv[2]); BASE='http://127.0.0.1:" + _PORT + "/api/v3'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    with urllib.request.urlopen(r,timeout=180) as u: d=u.read(); return json.loads(d) if d else None\n"
    "ser=[s for s in api('/series') if s.get('tvdbId')==PLAN['tvdbId']]\n"
    "if len(ser)!=1: print('FAILED: no single series with tvdbId %s' % PLAN['tvdbId']); sys.exit(1)\n"
    "S=ser[0]; SID=S['id']; print('SERIES: %s (id %d)' % (S.get('title'), SID))\n"
    "files=[]\n"
    "for f in PLAN['folders']:\n"
    "    try:\n"
    "        eps={e['episodeNumber']:e['id'] for e in api('/episode?seriesId=%d&seasonNumber=%d' % (SID,f['season']))}\n"
    "        prev=api('/manualimport?'+urllib.parse.urlencode({'folder':f['path'],'filterExistingFiles':'false'})) or []\n"
    "    except (TimeoutError, OSError, urllib.error.URLError) as e:\n"
    "        print('FAILED: Sonarr did not answer for %s (%r) -- nothing imported' % (f['path'],e)); sys.exit(1)\n"
    "    keep=[p for p in prev if not (f.get('skip') and f['skip'].lower() in str(p.get('path') or '').lower())]\n"
    "    if f.get('count') and len(keep)!=f['count']:\n"
    "        print('FAILED: %s holds %d files to import, the step says %d -- nothing imported' % (f['path'],len(keep),f['count'])); sys.exit(1)\n"
    "    nums=[]\n"
    "    for p in keep:\n"
    "        base=str(p.get('path') or '').rsplit('/',1)[-1]\n"
    "        m=re.search(r'[Ss]\\d{1,3}[Ee](\\d{1,3})', base)\n"
    # §17.1722 — a pack whose files name only the episode: `Episode 7 The Need for Speed.mkv`
    "        m=m or re.match(r'(?i)(?:episode|ep)[ ._-]*(\\d{1,3})(?!\\d)', base)\n"
    "        n=int(m.group(1)) if m else None\n"
    "        if n is None or n not in eps:\n"
    "            print('FAILED: %s maps to no episode of season %d -- nothing imported' % (str(p.get('path'))[-70:],f['season'])); sys.exit(1)\n"
    "        files.append({'path':p['path'],'seriesId':SID,'episodeIds':[eps[n]],'quality':p.get('quality'),'languages':p.get('languages') or []})\n"
    "        nums.append(n)\n"
    "    if len(set(nums))!=len(nums):\n"
    "        print('FAILED: two files in %s map to one episode -- nothing imported' % f['path']); sys.exit(1)\n"
    "    print('MAP: %d files of %s -> S%02dE%02d..E%02d' % (len(nums), f['path'].rsplit('/',1)[-1][:60], f['season'], min(nums), max(nums)))\n"
    "cmd=api('/command','POST',{'name':'ManualImport','importMode':'copy','files':files})\n"
    "cid=cmd.get('id'); print('IMPORT: ManualImport command %s for %d files' % (cid,len(files)))\n"
    "st={}\n"
    "for i in range(60):\n"
    "    try: st=api('/command/%s' % cid) or {}\n"
    "    except (TimeoutError, OSError, urllib.error.URLError) as e: print('WAIT: %r' % e); time.sleep(5); continue\n"
    "    if st.get('status') in ('completed','failed','aborted','cancelled'): break\n"
    "    time.sleep(5)\n"
    "print('STATUS: %s %s' % (st.get('status'), st.get('message') or ''))\n"
    "S=api('/series/%d' % SID)\n"
    "for f in PLAN['folders']:\n"
    "    se=[x for x in S.get('seasons') or [] if x.get('seasonNumber')==f['season']]\n"
    "    stt=(se[0].get('statistics') or {}) if se else {}\n"
    "    print('SEASON %d: have %s of %s' % (f['season'], stt.get('episodeFileCount'), stt.get('episodeCount')))\n"
    "if st.get('status')!='completed': sys.exit(1)\n"
)


def plan(node: Optional[dict]) -> Optional[dict]:
    """{tvdbId, folders: [{path, season, count?, skip?}]} -- or None when the step does not state its mapping."""
    if not _TITLE_RE.search(str((node or {}).get("title") or "")):
        return None
    text = "\n".join(str((node or {}).get(k) or "") for k in ("description", "prompt_template"))
    tv = _TVDB_RE.search(text)
    folders, seen = [], set()
    for m in _FOLDER_RE.finditer(text):
        path = m.group("qpath") or m.group("path").rstrip(".,;")
        if path in seen:
            continue
        seen.add(path)
        f = {"path": path, "season": int(m.group("season"))}
        if m.group("count"):
            f["count"] = int(m.group("count"))
        if m.group("skip"):
            f["skip"] = m.group("skip")
        folders.append(f)
    if not tv or not folders:
        return None
    return {"tvdbId": int(tv.group(1)), "folders": folders}


def block(node: Optional[dict], sonarr_guest: Callable[[], Optional[str]]) -> Optional[tuple[list[str], list[str], str]]:
    p = plan(node)
    if not p:
        return None
    gid = sonarr_guest()
    if not gid:
        return None
    from app.modules.supervised_runs import _sq_payload
    payload = f'python3 -c "{IMPORT_PY}" "<SONARR_API_KEY>" {_sq_payload(json.dumps(p))}'
    cmds = [f"pct exec {gid} -- sh -c {_sq_payload(payload)}"]
    verify = [f'pct exec {gid} -- curl -s --fail-with-body -H "X-Api-Key: $SONARR_API_KEY" '
              f'"http://127.0.0.1:{_PORT}/api/v3/command" | grep -o \'"name": *"ManualImport"\\|"status": *"[a-z]*"\' | head -n 6']
    seasons = ", ".join(f"{f['path'].rsplit('/', 1)[-1][:40]} -> S{f['season']}" for f in p["folders"])
    note = (f"imported as the engine's own block (§17.1712): inside guest {gid}, the series found by tvdbId {p['tvdbId']}; "
            f"each folder ({seasons}) read through Sonarr's own preview with NO seriesId, each file mapped by the episode "
            "number in its own name to the target season, refused before importing on an unmapped file, a duplicate or a "
            "wrong count; then ONE ManualImport command (copy) waited on, and the seasons' counts read back.")
    return cmds, verify, note
