r"""§17.1717 — a retry is drafted from what the machine answered, not from the engine's own suggested script;
an *arr API call that cannot work is refused.

Live (job e5e22b92 HI1, 2026-10-21, a read of the HIMYM S6 import): the first attempt crashed reading Sonarr's history
(`'list' object has no attribute 'get'`). The engine's diagnosis blamed `statusMessages` -- the wrong line -- and its
`## Fix` held a whole script: `urlopen(base + "/series/75760")` with no `X-Api-Key`, 75760 being the step's tvdbId.
The retry context carried that `## Fix` and told the drafter its draft "must differ in the way the diagnosis says"; the
redraft was that script, and Sonarr answered 401 to its first call.
"""
from __future__ import annotations

import pathlib

from app.modules import arr_api_shapes as aas
from app.modules.previous_attempt import what_failed

REPORT = (pathlib.Path(__file__).parent / "fixtures" / "hi1_failed_report_2026_10_21.txt").read_text()

HI1 = {"node_key": "HI1", "title": "Read the HIMYM S6 import in Sonarr",
       "description": "Read-only, change nothing. Inside Sonarr's container (CT 104), How I Met Your Mother "
                      "(tvdbId 75760), season 6: the season's have N of M and the queue records."}
# live, HI1's second draft (the parts that matter)
SECOND = (
    "python3 - <<'PY'\nimport urllib.request, json\n"
    'base = "http://localhost:8989/api/v3"\n'
    'r = urllib.request.urlopen(base + "/series/75760", timeout=10)\nseries = json.load(r)\n'
    'r = urllib.request.urlopen(base + "/queue?pageSize=1000", timeout=10)\nqueue = json.load(r)\n'
    'for rec in queue.get("records", []):\n    if rec.get("seriesId") == 75760 and rec.get("seasonNumber") == 6:\n'
    '        print(rec.get("title"))\nPY\n')
FILE = {"path": "/tmp/in_ct_104.sh", "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\n" + SECOND + "REMOTE\n"}
RIGHT = (
    "python3 - <<'PY'\nimport urllib.request, json, xml.etree.ElementTree as ET\n"
    "key = ET.parse('/var/lib/sonarr/config.xml').getroot().find('ApiKey').text\n"
    "base = 'http://localhost:8989/api/v3'\n"
    "def get(p):\n    return json.load(urllib.request.urlopen(urllib.request.Request(base + p, headers={'X-Api-Key': key})))\n"
    "s = next(x for x in get('/series') if x.get('tvdbId') == 75760)\n"
    "q = get('/queue?pageSize=1000')\nrecs = [r for r in q['records'] if r.get('seriesId') == s['id']]\nPY\n")


# ------------------------------------------------------------ the retry context


def test_what_ran_is_carried_and_the_suggested_script_is_not():
    got = what_failed(REPORT)
    assert "AttributeError: 'list' object has no attribute 'get'" in got        # the machine's answer, whole
    assert "### Diagnosis" in got and "### Fix" in got                         # the reading's prose stays
    assert "/series/75760" not in got and "urlopen" not in got                 # its script does not
    assert "never run" in got


def test_a_fence_in_what_ran_is_kept():
    rep = "## Executed on pve-runner\n```\n$ curl -s x\n{\"ok\": 1}\n```\n## Fix\n```bash\ncurl -X POST y\n```\n"
    got = what_failed(rep)
    assert '{"ok": 1}' in got and "curl -X POST y" not in got


# ------------------------------------------------------------ the *arr calls


def test_the_live_redraft_is_refused_twice():
    hit = aas.calls_that_cannot_work(["bash /tmp/in_ct_104.sh start"], [FILE], HI1)
    whys = " ".join(h["why"] for h in hit)
    assert len(hit) == 2 and "no `X-Api-Key`" in whys and "75760 is the step's `tvdbId`" in whys


def test_the_right_program_is_not():
    right = dict(FILE, content=FILE["content"].replace(SECOND, RIGHT))
    assert aas.calls_that_cannot_work(["x"], [right], HI1) == []


def test_each_half_bites_alone():
    keyed = SECOND.replace("timeout=10)", "timeout=10) if 'X-Api-Key' else None")
    assert [h for h in aas.calls_that_cannot_work(["x"], [dict(FILE, content=keyed)], HI1)
            if "tvdbId" in h["why"]]
    looked_up = RIGHT.replace("headers={'X-Api-Key': key}", "headers={}")
    hit = aas.calls_that_cannot_work(["x"], [dict(FILE, content=looked_up)], HI1)
    assert len(hit) == 1 and "no `X-Api-Key`" in hit[0]["why"]
    # a step that names no tvdbId is not judged on ids; a Prowlarr call is judged on its key too
    assert all("tvdbId" not in h["why"] for h in aas.calls_that_cannot_work(["x"], [FILE], {"title": "Read the queue"}))
    assert aas.calls_that_cannot_work(["curl -s http://127.0.0.1:9696/api/v1/indexer"], [], None)


def test_it_is_wired_into_the_frame():
    import inspect

    from app.modules import supervised_runs as sr
    assert "_cannot_work(cmds, shape_files, node)" in inspect.getsource(sr.frame_run)
