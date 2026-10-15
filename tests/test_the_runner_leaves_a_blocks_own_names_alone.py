r"""§17.1578 — a block's own UPPERCASE names are not secrets the runner must hold; the engine predicts the runner at hand.

Live (c2792d4b T2, 2026-10-15, helper 22): the engine's probe (§17.1574c) opened `CPW=$(cat); … PORT=$(grep …)` and
used `$CPW`/`$PORT`. The runner's `secret_refs` reads every `$[A-Z]…` as a secret it must resolve, found neither in its
file nor sent, and refused the approved block: "this command needs $PORT, $CPW, which neither the engine sent nor this
runner's secrets file holds". The engine's own gate (§17.1348) knew the names were bound and said nothing, so Run was
offered for a block that could not run. Three ends: the engine's own payloads use lowercase locals; the frame refuses
such a block on a runner older than helper 23; helper 23's `secret_refs` leaves the names a block binds alone.
"""
from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _runner():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


BLOCK = "pct exec 111 -- sh -c 'TOK=$(cat); curl -s -u \"x:$TOK\" http://127.0.0.1:1/' <<< \"<PANEL_PASSWORD>\""
LIVE_T2 = {"node_key": "T2", "title": "Probe each endpoint authenticated", "description": "", "tool": "Shell",
           "prompt_template": "Using the panel user's session cookie, curl each endpoint on container 111 and record "
                              "status code, response body, and whether it hangs. Do not modify anything."}
INV = {"cts": {"111": "running"}, "vms": {}, "names": {"111": "control-panel"}, "unread": []}
LOGINS = [{"name": "PANEL_PASSWORD", "user": "panel", "hint": "the control panel's login password (user `panel`)"}]


def test_the_runner_leaves_a_blocks_own_names_alone():
    r = _runner()
    assert int(r.HELPER_VERSION) >= 23
    live = 'CPW=$(cat); fails=""; PORT=$(grep -rhoE x); for EP in a b; do curl -u "panel:$CPW" "http://127.0.0.1:$PORT$EP"; done'
    assert r.secret_refs(live) == []
    assert r.secret_refs("mysql -p$DB_PASSWORD; CPW=$(cat); echo $CPW") == ["DB_PASSWORD"]
    assert r.secret_refs("read -r CPW; echo $CPW $API_TOKEN") == ["API_TOKEN"]
    assert r.secret_refs("export KEY=abc; curl -H \"k: $KEY\"") == []
    assert r.secret_refs("sh -c 'X=1; echo $X'") == []                      # bound just inside a quote
    # an assignment FROM ITSELF binds nothing: still demanded, as §17.1383 says
    assert r.secret_refs("TOK=$TOK; echo $TOK") == ["TOK"] and r.secret_refs('TOK="${TOK}"; echo $TOK') == ["TOK"]
    assert r.secret_refs("echo --KEY=$KEY") == ["KEY"]                     # `--KEY=` is an option, not a binding
    assert r.secret_refs("a $ONE then $TWO then $ONE again") == ["ONE", "TWO"]   # the old cases hold


def test_the_engine_predicts_the_old_runners_refusal_and_is_silent_on_the_new_one():
    old = sr.locals_the_runner_reads_as_secrets([BLOCK], {"helper": "22", "held": ["PANEL_PASSWORD"]})
    assert len(old) == 1 and "`$TOK`" in old[0]["why"] and "helper 22" in old[0]["why"] and "lowercase (`tok`)" in old[0]["why"]
    assert "neither the engine sent nor this runner's secrets file holds" in old[0]["why"]
    assert sr.locals_the_runner_reads_as_secrets([BLOCK], {"helper": "23", "held": ["PANEL_PASSWORD"]}) == []
    assert sr.locals_the_runner_reads_as_secrets([BLOCK], {"held": ["PANEL_PASSWORD"]})        # no version said: the old rule
    # a name the runner HOLDS it resolves whatever the block does with it; a lowercase local is nobody's secret
    assert sr.locals_the_runner_reads_as_secrets(["PANEL_PASSWORD=x; echo $PANEL_PASSWORD"], {"helper": "22", "held": ["PANEL_PASSWORD"]}) == []
    assert sr.locals_the_runner_reads_as_secrets(["tok=$(cat); echo $tok"], {"helper": "22"}) == []
    # a reference nothing binds is §17.1348's (variables_nothing_sets), not this one's
    assert sr.locals_the_runner_reads_as_secrets(["echo $NOBODY"], {"helper": "22"}) == []
    assert "locals_the_runner_reads_as_secrets(list(cmds) + list(verify), policy)" in __import__("inspect").getsource(sr.frame_run)


def test_frame_run_turns_run_off_for_such_a_block_on_the_old_runner():
    rb = f"## Run this\n\n```bash\n{BLOCK}\n```\n\n## Verify\n\n- `pct exec 111 -- ss -tlnp` shows `3001`\n"
    frame = sr.frame_run({"node_key": "X", "title": "t"}, rb, SimpleNamespace(name="pve-runner"),
                         {"allow": ["ANY"], "helper": "22", "held": ["PANEL_PASSWORD"]})
    assert any("reads every `$UPPER`" in r["why"] for r in frame["refused"]) and frame["suggested"] == "myself"
    frame23 = sr.frame_run({"node_key": "X", "title": "t"}, rb, SimpleNamespace(name="pve-runner"),
                           {"allow": ["ANY"], "helper": "23", "held": ["PANEL_PASSWORD"]})
    assert not any("reads every" in r["why"] for r in frame23["refused"])


def test_the_engines_own_payloads_bind_no_name_the_runner_would_demand():
    probe = sr.rewrite_probe_service_endpoints(LIVE_T2, ["x"], [], [], INV, logins=LOGINS)[0]
    assert sr.locals_the_runner_reads_as_secrets(probe, {"helper": "22", "held": ["PANEL_PASSWORD"]}) == []
    assert "cpw=$(cat)" in probe[0] and '-u "panel:$cpw"' in probe[0] and "$CPW" not in probe[0] and "$PORT" not in probe[0]
    svc = SimpleNamespace(name="control-panel", guest="111", ports=("3001",), unit="control-panel.service", vm=False)
    naked = "pct exec 111 -- curl -s --fail-with-body http://127.0.0.1:3001/api/health"
    cmds, _, notes = mv.present_a_held_login([naked], [], [svc], LOGINS)
    assert notes and '-u "panel:$cpw"' in cmds[0]
    assert sr.locals_the_runner_reads_as_secrets(cmds, {"helper": "22", "held": ["PANEL_PASSWORD"]}) == []
    # and the runner, old rule or new, asks for exactly the one secret the engine names
    r = _runner()
    for c in (probe[0], cmds[0]):
        assert r.secret_refs(c.replace("<PANEL_PASSWORD>", "$PANEL_PASSWORD")) == ["PANEL_PASSWORD"], c[:80]
