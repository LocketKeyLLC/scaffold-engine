"""§17.1978 — `$NAME` inside a JavaScript string no shell expands is the name, not the value; `${NAME}` in a template
literal is an undeclared JS variable. §17.1969 read Python only."""
from types import SimpleNamespace as NS

from app.modules import supervised_runs as sr
from app.modules.supervised_runs import a_secret_name_written_as_its_value as gate

_POLICY = {"secrets": ["PANEL_PASSWORD", "RADARR_API_KEY"]}
_MARK = "writes the secret's NAME where its VALUE belongs"

# the shape the corpus's playwright harnesses take (traces 13140-13154), with the env read swapped for the literal name
_SECTIONS = ("import { chromium } from 'playwright';\n"
             "const BASE = process.argv[2];\n"
             "const browser = await chromium.launch({ headless: true });\n"
             "const ctx = await browser.newContext({ httpCredentials: { username: 'panel', password: '$PANEL_PASSWORD' } });\n"
             "const page = await ctx.newPage();\n"
             "await page.goto(BASE + '/');\n"
             "console.log((await page.title()) || 'no title');\n"
             "await browser.close();\n")


def test_every_javascript_text_no_shell_expands_is_judged():
    cases = [
        # a single-quoted `node -e`: the shell hands `$RADARR_API_KEY` over as text
        ("node -e 'fetch(\"http://10.20.30.40:7878/api/v3/system/status\", {headers: {\"X-Api-Key\": "
         "\"$RADARR_API_KEY\"}}).then(r => console.log(r.status))'", [], "sits inside a JavaScript string"),
        # a quoted heredoc into node: `${PANEL_PASSWORD}` in a template is a JS variable nothing declares
        ("node --input-type=module - <<'JS'\nconst auth = Buffer.from(`panel:${PANEL_PASSWORD}`).toString('base64');\n"
         "const r = await fetch('http://10.20.30.40:3001/', { headers: { Authorization: `Basic ${auth}` } });\n"
         "console.log(r.status);\nJS", [], "ReferenceError"),
        # a quoted heredoc that writes a `.mjs` file
        ("cat > /tmp/probe.mjs <<'EOF'\nconst auth = 'panel:$PANEL_PASSWORD';\nEOF\nnode /tmp/probe.mjs", [],
         "sits inside a JavaScript string"),
        # a written `.mjs` file
        ("node /tmp/sections.mjs http://10.20.30.40:3001", [{"path": "/tmp/sections.mjs", "content": _SECTIONS}],
         "sits inside a JavaScript string"),
        # a written file without a JS suffix that `node <path>` runs; `$NAME` in a template's TEXT is literal
        ("node /opt/panel-check/run http://10.20.30.40:3001",
         [{"path": "/opt/panel-check/run", "content": "const u = `panel:$PANEL_PASSWORD`;\nfetch(process.argv[2], "
                                                      "{headers: {Authorization: 'Basic ' + btoa(u)}});\n"}],
         "sits inside a JavaScript string"),
        # `sh -c '…'` around the heredoc: `'\''` is one quote by the time node reads it
        ("pct exec 111 -- sh -c 'node - <<\"JS\"\nconst pw = '\\''$PANEL_PASSWORD'\\'';\nfetch(\"http://127.0.0.1:3001/\")\nJS'",
         [], "sits inside a JavaScript string"),
    ]
    for cmd, files, says in cases:
        out = gate([cmd], files, _POLICY)
        assert len(out) == 1 and _MARK in out[0]["why"] and says in out[0]["why"], cmd
        assert "process.env." in out[0]["why"] and "node …" in out[0]["why"]
    out = gate(["node /tmp/sections.mjs http://10.20.30.40:3001"], [{"path": "/tmp/sections.mjs", "content": _SECTIONS}],
               _POLICY)
    assert out[0]["command"].startswith("const ctx = ") and "`$PANEL_PASSWORD`" in out[0]["why"]


def test_a_regex_literal_with_a_quote_does_not_hide_the_string_after_it():
    src = "const clean = s => s.replace(/'/g, '');\nconst auth = \"panel:$PANEL_PASSWORD\";\n"
    out = gate(["node /tmp/a.js"], [{"path": "/tmp/a.js", "content": src}], _POLICY)
    assert len(out) == 1 and out[0]["command"].startswith("const auth")


def test_javascript_the_shell_expands_or_that_reads_the_environment_passes():
    goods = [
        "node -e \"console.log('$PANEL_PASSWORD'.length)\"",                         # double quotes: the shell expands it
        "node - <<JS\nconst pw = '$PANEL_PASSWORD';\nJS",                              # unquoted tag: the shell expands it
        "PANEL_PASSWORD=\"$PANEL_PASSWORD\" node - <<'JS'\nconst pw = process.env.PANEL_PASSWORD;\n"
        "const auth = Buffer.from(`panel:${pw}`).toString('base64');\nJS",
        "PANEL_PASSWORD=\"$PANEL_PASSWORD\" node - <<'JS'\nconst pw = process.env['PANEL_PASSWORD'];\nJS",
        # the program declares the name itself: `${PANEL_PASSWORD}` is that binding
        "PANEL_PASSWORD=\"$PANEL_PASSWORD\" node - <<'JS'\nconst { PANEL_PASSWORD } = process.env;\n"
        "const u = `panel:${PANEL_PASSWORD}`;\nJS",
        "node - <<'JS'\nconst [, , PANEL_PASSWORD] = process.argv;\nconst u = `panel:${PANEL_PASSWORD}`;\nJS",
        # a program that hands its strings to a shell
        "node - <<'JS'\nconst { execSync } = require('child_process');\n"
        "execSync('curl -u panel:$PANEL_PASSWORD http://10.20.30.40:3001/', { stdio: 'inherit' });\nJS",
        "node - <<'JS'\nconst { spawnSync } = require('child_process');\n"
        "spawnSync('bash', ['-c', 'curl -u panel:$PANEL_PASSWORD http://10.20.30.40:3001/']);\nJS",
        # messages, comments, a comparison, a placeholder the program fills itself
        "node - <<'JS'\n// reads $PANEL_PASSWORD from the environment\n/* `${PANEL_PASSWORD}` is never typed */\n"
        "if (!process.env.PANEL_PASSWORD) { console.error('export $PANEL_PASSWORD first'); process.exit(2); }\n"
        "if (process.argv[2] === '$PANEL_PASSWORD') throw new Error(`pass the value, not $PANEL_PASSWORD`);\nJS",
        "node - <<'JS'\nconst cfg = 'user = \"panel:$PANEL_PASSWORD\"';\n"
        "const out = cfg.replace('$PANEL_PASSWORD', process.env.PANEL_PASSWORD);\nJS",
    ]
    for cmd in goods:
        assert gate([cmd], [], _POLICY) == [], cmd
    # a shell script is expanded when it runs: not this rule's
    assert gate(["bash /tmp/p.sh"], [{"path": "/tmp/p.sh", "content": 'curl -u "panel:$PANEL_PASSWORD" http://10.20.30.40/\n'}],
                _POLICY) == []


def test_the_frame_refuses_a_runbook_whose_harness_carries_the_name():
    rb = ("## Write these files\n\n### /tmp/sections.mjs\n```javascript\n" + _SECTIONS + "```\n\n"
          "## Run this\n\n```bash\nnode /tmp/sections.mjs http://10.20.30.40:3001\n```\n\n"
          "## Verify\n\n- `curl -s -o /dev/null -w '%{http_code}' -u \"panel:$PANEL_PASSWORD\" http://10.20.30.40:3001/` prints `200`\n")
    frame = sr.frame_run({"node_key": "T15", "title": "t"}, rb, NS(name="pve-runner"), {**_POLICY, "can_write_files": True})
    assert any("sits inside a JavaScript string" in r["why"] for r in frame["refused"])
    fixed = rb.replace("'$PANEL_PASSWORD'", "process.env.PANEL_PASSWORD").replace(
        "node /tmp/sections.mjs", "PANEL_PASSWORD=\"$PANEL_PASSWORD\" node /tmp/sections.mjs")
    frame = sr.frame_run({"node_key": "T15", "title": "t"}, fixed, NS(name="pve-runner"), {**_POLICY, "can_write_files": True})
    assert not any(_MARK in r["why"] for r in frame["refused"])
