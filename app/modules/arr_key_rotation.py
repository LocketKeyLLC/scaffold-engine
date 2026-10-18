r"""§17.1645 — "rotate <arr app>'s API key and update every holder", as the engine's own block.

Live (job 5db1b088 RK1, 2026-10-18): Radarr's ApiKey in CT 103 was the literal word `RADARR_API_KEY`, Prowlarr's copy
was a different value, so every indexer sync to Radarr answered 401 (Radarr held 13 indexers, Prowlarr 45 enabled).
The operator chose to rotate. The model's draft generated the new key ON THE HOST and spliced it into eight
`pct exec … sh -c "…<key>…"` command lines (visible in every process listing), checked `ls` on files the script itself
deleted, and would have broken the panel: the key's old value is also the variable's NAME in
`Environment=RADARR_API_KEY=RADARR_API_KEY`, so a plain text replace renames the variable.

The engine's block, one runner call per phase (`run_block` stops at the first failure):

1. inside the app's guest: back up config.xml, generate the key there (`secrets.token_hex(16)`), keep old and new in
   root-only files, stop the unit, write the key, start it, and wait until the app answers 200 WITH the new key.
   Re-running after a partial run finds its own key already in place and goes on (never generates twice).
2. host: pull those two files into root-only files on the host.
3. Prowlarr's guest (when the app is not Prowlarr itself): PUT each Prowlarr application of that implementation back with
   the new apiKey (forceSave), then queue ApplicationIndexerSync.
4. each other guest the step names, with the files and units it names there: replace the old key where it is a VALUE
   (a JSON string equal to it; elsewhere after `=` `:` `>` or a quote and not followed by `=`), back each file up,
   daemon-reload when a unit file changed, restart the named units.
5. host: delete the transfer files; inside the app's guest too.

No key value is ever on a command line, in the approval, or printed.
"""
from __future__ import annotations

import re
from typing import Optional

#: app -> (port, API version) for the *arr apps the engine knows
ARR = {"radarr": ("7878", "v3"), "sonarr": ("8989", "v3"), "lidarr": ("8686", "v1"), "readarr": ("8787", "v1"),
       "prowlarr": ("9696", "v1")}
_ROTATE_RE = re.compile(r"\b(?:rotate|regenerate|re-?key|replace|reset|change)\b[^.;\n]{0,40}\bapi[- ]?key\b", re.I)
_GUEST_RE = re.compile(r"\b(?:CT|LXC|container)\s*(\d{3,5})\b", re.I)
_PATH_RE = re.compile(r"(?<![\w.~-])(/(?:etc|opt|var|srv|home)/[\w./@-]+[\w])")
_UNIT_RE = re.compile(r"\brestart\s+([\w@.-]+\.service)\b", re.I)

ROTATE_PY = (
    "import os,sys,re,time,secrets,shutil,subprocess,urllib.request\n"
    "app,port,ver=sys.argv[1],sys.argv[2],sys.argv[3]\n"
    "cands=[p for p in ('/var/lib/%s/config.xml'%app,'/config/config.xml','/opt/%s/config.xml'%app) if os.path.exists(p)]\n"
    "if not cands: sys.exit('FAILED: no config.xml for %s'%app)\n"
    "cfg=cands[0]; m=re.search(r'<ApiKey>([^<]*)</ApiKey>',open(cfg).read())\n"
    "if not m: sys.exit('FAILED: no <ApiKey> in '+cfg)\n"
    "cur=m.group(1); newp='/root/.scaffold-rotate.new'; oldp='/root/.scaffold-rotate.old'; os.umask(0o077)\n"
    "if os.path.exists(newp) and os.path.exists(oldp) and open(newp).read().strip()==cur:\n"
    "    new=cur; print('ALREADY ROTATED: '+cfg+' holds the key an earlier run of this step generated')\n"
    "else:\n"
    "    new=secrets.token_hex(16); open(oldp,'w').write(cur); open(newp,'w').write(new)\n"
    "    bak=cfg+'.bak.'+time.strftime('%Y%m%d%H%M%S'); shutil.copy2(cfg,bak); print('backed up '+cfg+' to '+bak)\n"
    "    subprocess.run(['systemctl','stop',app+'.service'],check=True)\n"
    "    x=open(cfg).read(); open(cfg,'w').write(x.replace('<ApiKey>'+cur+'</ApiKey>','<ApiKey>'+new+'</ApiKey>',1))\n"
    "    subprocess.run(['systemctl','start',app+'.service'],check=True); print('wrote the new key and restarted '+app)\n"
    "last=None\n"
    "for i in range(24):\n"
    "    try:\n"
    "        r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:%s/api/%s/system/status'%(port,ver),headers={'X-Api-Key':new}),timeout=3)\n"
    "        if r.status==200: print('ROTATED: %s answers 200 with the new key'%app); sys.exit(0)\n"
    "    except Exception as e: last=e\n"
    "    time.sleep(2)\n"
    "sys.exit('FAILED: %s did not answer with the new key: %r'%(app,last))\n")

PROWLARR_PY = (
    "import os,sys,re,json,urllib.request,urllib.error\n"
    "impl=sys.argv[1]\n"
    "cfg=[p for p in ('/var/lib/prowlarr/config.xml','/config/config.xml') if os.path.exists(p)]\n"
    "pk=re.search(r'<ApiKey>([^<]*)</ApiKey>',open(cfg[0]).read()).group(1)\n"
    "new=open('/root/.scaffold-rotate.new').read().strip()\n"
    "def api(m,p,b=None):\n"
    "    r=urllib.request.Request('http://127.0.0.1:9696/api/v1'+p,data=(json.dumps(b).encode() if b is not None else None),method=m,headers={'X-Api-Key':pk,'Content-Type':'application/json'})\n"
    "    try:\n"
    "        with urllib.request.urlopen(r,timeout=25) as u: d=u.read(); return u.status,(json.loads(d) if d else None)\n"
    "    except urllib.error.HTTPError as e: sys.exit('FAILED: %s %s answered %s: %s'%(m,p,e.code,e.read()[:300]))\n"
    "    except (urllib.error.URLError,TimeoutError,OSError) as e: sys.exit('FAILED: %s %s did not answer: %r'%(m,p,e))\n"
    "_,apps=api('GET','/applications')\n"
    "mine=[a for a in apps or [] if str(a.get('implementation') or '').lower()==impl.lower()]\n"
    "if not mine: sys.exit('FAILED: Prowlarr has no %s application'%impl)\n"
    "for a in mine[:3]:\n"
    "    for f in a.get('fields') or []:\n"
    "        if f.get('name')=='apiKey': f['value']=new\n"
    "    st,_=api('PUT','/applications/%s?forceSave=true'%a['id'],a)\n"
    "    print('PROWLARR: application %s (id %s) holds the new key: HTTP %s'%(a.get('name'),a['id'],st))\n"
    "st,_=api('POST','/command',{'name':'ApplicationIndexerSync'})\n"
    "print('PROWLARR: ApplicationIndexerSync queued: HTTP %s'%st)\n"
    "os.remove('/root/.scaffold-rotate.new')\n")

HOLDER_PY = (
    "import os,sys,re,json,time,shutil,subprocess\n"
    "old=open('/root/.scaffold-rotate.old').read().strip(); new=open('/root/.scaffold-rotate.new').read().strip()\n"
    "a=sys.argv[1:]; i=a.index('--'); paths,units=a[:i],a[i+1:]\n"
    "pat=re.compile('(?<=[=:>\\x22\\x27 ])'+re.escape(old)+'(?![=\\\\w])')\n"
    "def swap(v):\n"
    "    if isinstance(v,dict): return {k:swap(x) for k,x in v.items()}\n"
    "    if isinstance(v,list): return [swap(x) for x in v]\n"
    "    return new if v==old else v\n"
    "for p in paths:\n"
    "    if not os.path.exists(p): print('MISSING: '+p); continue\n"
    "    x=open(p).read()\n"
    "    if p.endswith('.json'): y=json.dumps(swap(json.loads(x)),indent=2)+'\\n'\n"
    "    else: y=pat.sub(new,x)\n"
    "    if y==x: print('UNCHANGED: %s holds no value equal to the old key'%p); continue\n"
    "    shutil.copy2(p,p+'.bak.'+time.strftime('%Y%m%d%H%M%S')); open(p,'w').write(y); print('UPDATED: %s (backed up)'%p)\n"
    "if any(p.startswith('/etc/systemd/') for p in paths): subprocess.run(['systemctl','daemon-reload'],check=True)\n"
    "if units: subprocess.run(['systemctl','restart',*units],check=True); print('RESTARTED: '+' '.join(units))\n"
    "for f in ('/root/.scaffold-rotate.old','/root/.scaffold-rotate.new'): os.remove(f)\n")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _py_in(gid: str, prog: str, args: list[str]) -> str:
    return f"pct exec {gid} -- python3 -c {_sq(prog)} " + " ".join(_sq(a) for a in args)


def rotated_app(node: Optional[dict]) -> Optional[str]:
    """The ONE *arr app whose API key the step rotates, or None."""
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not (_ROTATE_RE.search(title) or _ROTATE_RE.search(text[:400])):
        return None
    named = [a for a in ARR if re.search(rf"\b{a}\b", title, re.I)]
    return named[0] if len(named) == 1 else None


def holders(text: str, skip: set) -> dict:
    """{guest: (paths, units)} for each guest the text names with files or units of its own, guests in `skip` left out."""
    marks = [(m.start(), m.group(1)) for m in _GUEST_RE.finditer(text)]
    out: dict = {}

    def owner(at: int) -> Optional[str]:
        g = [gid for pos, gid in marks if pos < at]
        return g[-1] if g else None

    for m in _PATH_RE.finditer(text):
        g = owner(m.start())
        if g and g not in skip:
            out.setdefault(g, ([], []))[0].append(m.group(1))
    for m in _UNIT_RE.finditer(text):
        g = owner(m.start())
        if g and g in out:
            out[g][1].append(m.group(1))
    return {g: (list(dict.fromkeys(p)), list(dict.fromkeys(u))) for g, (p, u) in out.items()}


def block(node: Optional[dict], guest_of) -> Optional[tuple[list[str], list[str], str]]:
    """(commands, verify, note) for a rotation step, or None. `guest_of(app)` is the engine's guest lookup."""
    app = rotated_app(node)
    if not app:
        return None
    gx = guest_of(app)
    if not gx:
        return None
    port, ver = ARR[app]
    gp = guest_of("prowlarr") if app != "prowlarr" else None
    text = " ".join(str((node or {}).get(k) or "") for k in ("description", "prompt_template"))
    others = holders(text, {str(gx), str(gp or "")})
    stash = f"/root/.scaffold-rotate-{app}"
    cmds = [_py_in(gx, ROTATE_PY, [app, port, ver]),
            f"install -d -m 700 {stash} && pct pull {gx} /root/.scaffold-rotate.new {stash}/new "
            f"&& pct pull {gx} /root/.scaffold-rotate.old {stash}/old && chmod 600 {stash}/new {stash}/old"]
    if gp:
        cmds += [f"pct push {gp} {stash}/new /root/.scaffold-rotate.new --perms 600",
                 _py_in(gp, PROWLARR_PY, [app.capitalize()])]
    for g, (paths, units) in sorted(others.items()):
        cmds += [f"pct push {g} {stash}/new /root/.scaffold-rotate.new --perms 600 "
                 f"&& pct push {g} {stash}/old /root/.scaffold-rotate.old --perms 600",
                 _py_in(g, HOLDER_PY, [*paths, "--", *units])]
    cmds += [f"rm -rf {stash} && pct exec {gx} -- rm -f /root/.scaffold-rotate.new /root/.scaffold-rotate.old"]
    key = (f'$(cat /var/lib/{app}/config.xml /config/config.xml 2>/dev/null | '
           f'sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" | head -n 1)')
    status_read = ('curl -s -o /dev/null -w "' + app + ' HTTP %{http_code}" -H "X-Api-Key: ' + key + '" '
                   f"http://127.0.0.1:{port}/api/{ver}/system/status")
    verify = [f"pct exec {gx} -- sh -c {_sq(status_read)}"]
    verify += [f"pct exec {g} -- systemctl is-active {u}" for g, (_p, units) in sorted(others.items()) for u in units]
    who = (["Prowlarr's " + app.capitalize() + " application"] if gp else []) + \
          [f"{', '.join(p)} in guest {g}" for g, (p, _u) in sorted(others.items())]
    note = (f"drafted the rotation from the engine's own shape (§17.1645): the new {app} key is generated INSIDE guest {gx} and "
            f"moves only through root-only files, never a command line; {app} is restarted and must answer 200 with it before "
            f"anything else changes; then " + ("; ".join(who) or "no other holder") + " are updated where the old key is a "
            "VALUE (its old value is also a variable's name), each file backed up first.")
    return cmds, verify, note
