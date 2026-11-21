"""§17.1969 — `$NAME` inside a Python string no shell expands is the name, not the value; a search FOR the name is a read."""
from types import SimpleNamespace as NS

from app.modules import supervised_runs as sr
from app.modules.supervised_runs import a_secret_name_written_as_its_value as gate

_POLICY = {"secrets": ["PANEL_PASSWORD", "RADARR_API_KEY"]}
_MARK = "writes the secret's NAME where its VALUE belongs"

# the live shape (7cc2a7ac ADD5's draft, trace 13289), addresses made up
_PAL_STOP = ('import json, subprocess, sys\n\nBASE = "http://" + sys.argv[1] + ":3001"\n'
             "CFG = 'user = \"panel:$PANEL_PASSWORD\"\\n'\n\n\n"
             "def call(method, path):\n"
             '    args = ["curl", "--config", "/dev/stdin", "-s", "-w", "%{http_code}", "-X", method, BASE + path]\n'
             "    return subprocess.run(args, input=CFG, capture_output=True, text=True, timeout=20).stdout\n\n\n"
             'print(call("POST", "/api/palworld/stop"))\n')


def test_the_live_python_file_that_sends_the_literal_name_is_refused():
    out = gate(["python3 /tmp/pal_stop.py 10.20.30.40"], [{"path": "/tmp/pal_stop.py", "content": _PAL_STOP}], _POLICY)
    assert len(out) == 1 and _MARK in out[0]["why"] and "`$PANEL_PASSWORD`" in out[0]["why"]
    assert out[0]["command"].startswith("CFG = ")


def test_every_python_text_no_shell_expands_is_judged():
    for cmd in ("python3 -c 'import base64; tok = base64.b64encode(b\"panel:$PANEL_PASSWORD\")'",
                "python3 - <<'PY'\nimport urllib.request\nreq = urllib.request.Request('http://10.20.30.40:3001/',"
                " headers={'X-Pw': '${PANEL_PASSWORD}'})\nPY",
                "cat > /tmp/probe.py <<'EOF'\nAUTH = ('panel', '$PANEL_PASSWORD')\nEOF\npython3 /tmp/probe.py"):
        out = gate([cmd], [], _POLICY)
        assert len(out) == 1 and _MARK in out[0]["why"], cmd


def test_python_the_shell_expands_or_that_reads_the_environment_passes():
    goods = [
        "python3 -c \"pw = '$PANEL_PASSWORD'; print(len(pw))\"",                  # double quotes: the shell expands it
        "python3 - <<PY\npw = '$PANEL_PASSWORD'\nPY",                              # unquoted tag: the shell expands it
        "PANEL_PASSWORD=\"$PANEL_PASSWORD\" python3 - <<'PY'\nimport os\npw = os.environ['PANEL_PASSWORD']\nPY",
        "python3 - <<'PY'\nimport subprocess\nsubprocess.run('curl -u panel:$PANEL_PASSWORD http://10.20.30.40/', shell=True)\nPY",
        "python3 - <<'PY'\nimport os, sys\nif not os.environ.get('PANEL_PASSWORD'):\n"
        "    raise SystemExit(\"run it with a trailing '# $PANEL_PASSWORD'\")\nprint('set $PANEL_PASSWORD first')\nPY",
        "python3 - <<'PY'\n\"\"\"Reads $PANEL_PASSWORD from the environment.\"\"\"\nimport os\nPY",
        "python3 -c 'print(\"export $PANEL_PASSWORD before running this\")'",              # a message, not a value
    ]
    for cmd in goods:
        assert gate([cmd], [], _POLICY) == [], cmd
    # a shell script is expanded when it runs: not this rule's
    assert gate(["bash /tmp/p.sh"], [{"path": "/tmp/p.sh", "content": 'curl -u "panel:$PANEL_PASSWORD" http://10.20.30.40/\n'}],
                _POLICY) == []


def test_a_search_for_the_name_is_a_read_and_a_value_beside_it_is_still_refused():
    for live in ("pct exec 111 -- grep -c 'PANEL_PASSWORD' /etc/systemd/system/control-panel.service",
                 "pct exec 111 -- grep -rn -i -e 'basic' -e 'authorization' -e 'PANEL_PASSWORD' /opt/panel --include=*.js",
                 "systemctl cat panel | rg \"PANEL_PASSWORD\""):
        assert gate([live], [], _POLICY) == [], live
    assert gate(["grep -q ApiKey /c.xml && curl -H \"X-Api-Key: RADARR_API_KEY\" http://10.20.30.40:7878/"], [], _POLICY)
    assert gate(["sed -i 's|<ApiKey>.*</ApiKey>|<ApiKey>RADARR_API_KEY</ApiKey>|' /var/lib/radarr/config.xml"], [], _POLICY)


def test_the_frame_refuses_the_live_runbook():
    rb = ("## Write these files\n\n### /tmp/pal_stop.py\n```python\n" + _PAL_STOP + "```\n\n"
          "## Run this\n\n```bash\npython3 /tmp/pal_stop.py 10.20.30.40\n```\n\n"
          "## Verify\n\n- `curl -s -o /dev/null -w '%{http_code}' -u \"panel:$PANEL_PASSWORD\" http://10.20.30.40:3001/` prints `200`\n")
    frame = sr.frame_run({"node_key": "ADD5", "title": "t"}, rb, NS(name="pve-runner"), {**_POLICY, "can_write_files": True})
    assert any("sits inside a Python string" in r["why"] for r in frame["refused"])
