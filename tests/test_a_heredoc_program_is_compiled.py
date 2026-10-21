r"""§17.1723 — a `python3 - <<'PY'` program is compiled before it is offered; a reply read before it is parsed is refused.

Live (job e5e22b92 SG1 "Grab the Power Rangers S3 replacement pack", 2026-10-21): the draft
`pct exec 104 -- sh -c 'python3 - <<"PY" … PY'` framed `refused: []` with two defects. A backslash-escaped quote inside
an f-string, which no Python compiles -- §17.1257's compile reads `-c` payloads and `printf | tee` scripts only; and
`return r.status, json.load(r) if r.read(1) else {}` in the helper that POSTs the grab, which dies after Sonarr took it.
"""
from __future__ import annotations

import inspect

from app.modules import qbittorrent_read as qr
from app.modules import qbittorrent_trackers as qt
from app.modules import supervised_runs as sr

# live, SG1's command (the parts that matter)
SG1 = (
    "pct exec 104 -- sh -c 'python3 - <<\"PY\"\n"
    "import json, sys, time, urllib.request, urllib.error\n"
    "base = \"http://127.0.0.1:8989/api/v3\"\n"
    "headers = {\"X-Api-Key\": key, \"Content-Type\": \"application/json\"}\n"
    "def get(path, timeout=180):\n"
    "    req = urllib.request.Request(base + path, headers=headers)\n"
    "    with urllib.request.urlopen(req, timeout=timeout) as r:\n"
    "        return json.load(r)\n"
    "def post(path, body, timeout=180):\n"
    "    req = urllib.request.Request(base + path, method=\"POST\", headers=headers,\n"
    "                                 data=json.dumps(body).encode())\n"
    "    try:\n"
    "        with urllib.request.urlopen(req, timeout=timeout) as r:\n"
    "            return r.status, json.load(r) if r.read(1) else {}\n"
    "    except urllib.error.HTTPError as e:\n"
    "        return e.code, json.loads(e.read().decode() or \"{}\")\n"
    "pr = [s for s in get(\"/series\") if s.get(\"tvdbId\") == 72553]\n"
    "sid = pr[0][\"id\"]\n"
    "print(f\"SERIES: {pr[0].get(\\\"title\\\")} (id {sid})\")\n"
    "PY'")
QUOTES_FIXED = SG1.replace('get(\\"title\\")', "get('title')")
BOTH_FIXED = QUOTES_FIXED.replace(
    "            return r.status, json.load(r) if r.read(1) else {}\n",
    "            body = r.read()\n            return r.status, json.loads(body) if body else {}\n")


def test_the_live_draft_is_refused_as_not_python():
    hit = sr.a_heredoc_program_will_not_compile([SG1])
    assert len(hit) == 1 and "not valid Python" in hit[0]["why"] and "line continuation" in hit[0]["why"]
    assert 'get(\\"title\\")' in hit[0]["command"]
    assert sr.payload_will_not_compile([SG1]) == []                # the gap: §17.1257 never saw it


def test_the_quotes_fixed_compiles_and_the_reply_read_is_refused():
    assert sr.a_heredoc_program_will_not_compile([QUOTES_FIXED]) == []
    hit = sr.the_reply_is_read_before_it_is_parsed([QUOTES_FIXED])
    assert len(hit) == 1 and "the reply is read before it is parsed" in hit[0]["why"]
    assert hit[0]["command"] == "return r.status, json.load(r) if r.read(1) else {}"


def test_the_right_program_passes_both():
    assert sr.a_heredoc_program_will_not_compile([BOTH_FIXED]) == []
    assert sr.the_reply_is_read_before_it_is_parsed([BOTH_FIXED]) == []


def test_two_helpers_that_each_name_their_reply_r_are_not_confused():
    prog = ("python3 - <<'PY'\nimport json, urllib.request\n"
            "def a(u):\n    with urllib.request.urlopen(u) as r:\n        return json.load(r)\n"
            "def b(u):\n    with urllib.request.urlopen(u) as r:\n        return r.read().decode()\n"
            "body = b('x')\ndata = json.loads(body)\nPY")
    assert sr.the_reply_is_read_before_it_is_parsed([prog]) == []
    assert sr.a_heredoc_program_will_not_compile([prog]) == []


def test_a_program_inside_the_in_guest_script_is_compiled():
    bad = SG1.split("\n", 1)[1].rsplit("\n", 1)[0]                   # the program alone
    f = {"path": "/tmp/in_ct_104.sh",
         "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\nset -e\npython3 - <<'PY'\n" + bad + "\nPY\nREMOTE\n"}
    hit = sr.a_heredoc_program_will_not_compile([], [f])
    assert len(hit) == 1 and "/tmp/in_ct_104.sh" in hit[0]["why"]
    good = dict(f, content=f["content"].replace('get(\\"title\\")', "get('title')"))
    assert sr.a_heredoc_program_will_not_compile([], [good]) == []
    assert sr.the_reply_is_read_before_it_is_parsed([], [good])      # the file's reply read is judged too


def test_an_unquoted_tag_is_not_judged():
    # the shell expands that body first (`$x`, `\\`), so python never sees it as written
    assert sr.a_heredoc_program_will_not_compile(["python3 - <<PY\nprint(\\\"$HOME\\\")\nPY"]) == []


def test_the_engines_own_programs_pass():
    own = [qr.command("105", ["power"]), qr.status_command("105"), qt.command("105", ["new", "stuck"])]
    assert len(sr._quoted_heredoc_programs(own)) == 3
    assert sr.a_heredoc_program_will_not_compile(own) == []
    assert sr.the_reply_is_read_before_it_is_parsed(own) == []


def test_it_is_wired_into_the_frame_and_registered():
    src = inspect.getsource(sr.frame_run)
    assert "a_heredoc_program_will_not_compile(cmds, shape_files)" in src
    assert "the_reply_is_read_before_it_is_parsed(cmds, shape_files)" in src
    assert "the reply is read before it is parsed" in sr._SHAPE_REFUSALS
    assert sr._WHOSE_GAP.get("the reply is read before it is parsed") == "drafter"


# ------------------------------------------------------------ §17.1723b — a file pushed into a guest runs there

from app.modules.service_truth import ServiceTruth, loopback_on_the_host, pushed_and_run_in  # noqa: E402

SONARR = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active",
                      ports=("8989",), address="192.168.1.23")
RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active",
                      ports=("7878",), address="192.168.1.22")
# live, SG1's redraft (the parts that matter)
PUSHED = ["pct push 104 /tmp/grab_s3.py /tmp/grab_s3.py", "pct exec 104 -- python3 /tmp/grab_s3.py"]
GRAB_PY = {"path": "/tmp/grab_s3.py",
           "content": 'BASE = "http://127.0.0.1:8989/api/v3"\nreq = urllib.request.Request(BASE + "/release")\n'}


def test_the_live_redraft_is_not_read_as_the_hosts():
    assert pushed_and_run_in(PUSHED) == {"/tmp/grab_s3.py": "104"}
    assert loopback_on_the_host(PUSHED, [GRAB_PY], [SONARR, RADARR]) == []


def test_a_pushed_file_reaching_another_guests_port_is_refused():
    other = dict(GRAB_PY, content=GRAB_PY["content"].replace("8989", "7878"))
    hit = loopback_on_the_host(PUSHED, [other], [SONARR, RADARR])
    assert len(hit) == 1 and "runs inside guest 104" in hit[0]["why"] and "guest 103" in hit[0]["why"]


def test_a_file_pushed_but_run_on_the_host_or_elsewhere_is_still_the_hosts():
    # pushed into 104 but the host runs its own copy: the host's loopback has no Sonarr
    host_run = ["pct push 104 /tmp/grab_s3.py /tmp/grab_s3.py", "python3 /tmp/grab_s3.py"]
    assert pushed_and_run_in(host_run) == {}
    assert loopback_on_the_host(host_run, [GRAB_PY], [SONARR, RADARR])
    assert pushed_and_run_in(["pct push 104 /tmp/a.py /tmp/b.py", "pct exec 104 -- python3 /tmp/b.pyc"]) == {}
