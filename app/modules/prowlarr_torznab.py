r"""§17.1672 — "add <name> to Prowlarr as a Generic Torznab indexer at <url>" is the engine's own block.

Live (job 3cc24343 T15 "Add bitmagnet to Prowlarr", 2026-10-19; text: "Add indexer in Prowlarr: Generic Torznab, URL
http://192.168.1.29:3333/torznab. Does NOT modify anything else on CT 102."): the drafted in-guest script was one line,
`systemctl stop prowlarr.service` -- it would have stopped Prowlarr and added nothing (refused only for having no check).

bitmagnet documents the settings as "Generic Torznab", URL `http://<host>:3333/torznab`, nothing else, then Test
(https://bitmagnet.io/guides/servarr-integration.html). The engine adds it the way §17.1526 adds indexers: a small
program inside Prowlarr's guest, the key read there (§17.1332, it never leaves the guest), Prowlarr's OWN catalog
template (`implementation: Torznab`), the URL set, the first app profile, and a plain POST -- WITHOUT forceSave, so
Prowlarr tests the indexer before it accepts it. An indexer already at that URL or with that name is reported, not
duplicated; nothing else in Prowlarr is touched.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

_TITLE_RE = re.compile(r"^\s*add\s+(?P<name>[\w .-]{2,40}?)\s+(?:indexer\s+)?(?:to|in)\s+prowlarr\b", re.I)
_URL_RE = re.compile(r"\bhttps?://[\w.-]+(?::\d+)?/[\w./-]*torznab[\w./-]*", re.I)
_PORT = "9696"

#: runs inside Prowlarr's guest as `python3 -c "<this>" <key> <name> <url>` -- no double quotes, no `$`, no backslash
ADD_PY = (
    "import sys,json,urllib.request,urllib.error\n"
    "key,name,url=sys.argv[1],sys.argv[2],sys.argv[3]\n"
    "base='http://127.0.0.1:" + _PORT + "'\n"
    "def api(path,method='GET',body=None):\n"
    "    data=json.dumps(body).encode() if body is not None else None\n"
    "    req=urllib.request.Request(base+path,method=method,data=data,headers={'X-Api-Key':key,'Content-Type':'application/json'})\n"
    "    try:\n"
    "        with urllib.request.urlopen(req,timeout=90) as r: return json.loads(r.read() or b'null'),None\n"
    "    except urllib.error.HTTPError as e: return None,'HTTP %d %s' % (e.code,e.read().decode(errors='replace')[:400])\n"
    "    except Exception as e: return None,repr(e)\n"
    "def field(ix,n):\n"
    "    for f in ix.get('fields') or []:\n"
    "        if f.get('name')==n: return f.get('value')\n"
    "existing,err=api('/api/v1/indexer')\n"
    "if err: print('FAILED: cannot read the indexers: '+err); sys.exit(1)\n"
    "for ix in existing:\n"
    "    if str(field(ix,'baseUrl') or '').rstrip('/')==url.rstrip('/') or ix.get('name')==name:\n"
    "        print('ALREADY: %s is indexer %d at %s' % (ix.get('name'),ix['id'],field(ix,'baseUrl'))); sys.exit(0)\n"
    "schema,err=api('/api/v1/indexer/schema')\n"
    "if err: print('FAILED: cannot read the indexer catalog: '+err); sys.exit(1)\n"
    "tz=[s for s in schema if str(s.get('implementation','')).lower()=='torznab']\n"
    "tpl=next((s for s in tz if 'generic' in str(s.get('definitionName','')+s.get('name','')).lower()),None) or (tz[0] if tz else None)\n"
    "if tpl is None: print('FAILED: this Prowlarr has no Generic Torznab definition'); sys.exit(1)\n"
    "profiles,err=api('/api/v1/appprofile')\n"
    "body=dict(tpl); body.pop('id',None); body['name']=name; body['enable']=True\n"
    "if profiles: body['appProfileId']=profiles[0]['id']\n"
    "body['fields']=[dict(f,value=url) if f.get('name')=='baseUrl' else f for f in tpl.get('fields') or []]\n"
    "if not any(f.get('name')=='baseUrl' for f in body['fields']): print('FAILED: the Torznab template has no baseUrl field'); sys.exit(1)\n"
    "made,err=api('/api/v1/indexer','POST',body)\n"
    "if err: print('FAILED: Prowlarr refused the indexer (it tests before it saves): '+err); sys.exit(1)\n"
    "print('ADDED: %s as indexer %d, Generic Torznab at %s (Prowlarr tested it before saving)' % (name,made['id'],url))\n"
)


def target(node: Optional[dict]) -> tuple[str, str]:
    """(indexer name, torznab url) -- or empty strings."""
    title = str((node or {}).get("title") or "")
    m = _TITLE_RE.match(title)
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    u = _URL_RE.search(text)
    if not m or not u or not re.search(r"\btorznab\b", text, re.I):
        return "", ""
    return m.group("name").strip(), u.group(0).rstrip(".,;:")


def block(node: Optional[dict], prowlarr_guest: Callable[[], str]) -> Optional[tuple[list[str], list[str], str]]:
    name, url = target(node)
    if not name:
        return None
    gid = prowlarr_guest()
    if not gid:
        return None
    from app.modules.supervised_runs import _sq_payload
    payload = f'python3 -c "{ADD_PY}" "<PROWLARR_API_KEY>" "{name}" "{url}"'
    cmds = [f"pct exec {gid} -- sh -c {_sq_payload(payload)}"]
    verify = [f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null -w "prowlarr indexer API HTTP %{{http_code}}" '
              f'-H "X-Api-Key: $PROWLARR_API_KEY" "http://127.0.0.1:{_PORT}/api/v1/indexer"']
    note = (f"added {name} to Prowlarr as the engine's own block (§17.1672): inside guest {gid}, with the key read there, "
            f"Prowlarr's own Generic Torznab template is posted with the URL {url} and the first app profile -- without "
            "forceSave, so Prowlarr tests it before it saves; an indexer already at that URL or with that name is "
            "reported, not duplicated, and nothing else is touched. The draft only stopped prowlarr.service.")
    return cmds, verify, note
