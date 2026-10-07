"""Grade drafter outputs by REHEARSING them: parse each exactly as the engine does,
run it in the sandbox against the seeds, report the round trip."""
import json, os, subprocess, sys
sys.path.insert(0, "/code")
from app.modules.supervised_runs import file_writes, runbook_commands

d = "/x/"
seeds = json.load(open(d + "seeds.json"))
src = sys.argv[1]
drafts = json.load(open(d + src))
RT = {"get": "http://127.0.0.1:3001/api/palworld/settings", "put": "http://127.0.0.1:3001/api/palworld/settings",
      "config": "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
      "baseline": "/opt/palworld/DefaultPalWorldSettings.ini"}
for k, v in drafts.items():
    t = v.get("text") or ""
    if not t or t.startswith(("EMPTY", "ERROR")):
        print(f"{k:24s} NO DRAFT ({t[:40]})"); continue
    files, cmds = file_writes(t), runbook_commands(t)
    job = {"seeds": seeds, "files": [{"path": f["path"], "content": f["content"]} for f in files],
           "commands": cmds, "roundtrip": RT}
    print(json.dumps({"key": k, "job": job}))
