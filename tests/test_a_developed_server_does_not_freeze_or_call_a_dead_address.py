"""§17.1598 — a developed server must not freeze itself, and must not wait on an address nothing answers.

Live, 2026-10-15, control-panel job c2792d4b: network_ui_route.js made three blocking
`execFileSync('ssh', …)` calls, 15 s each, to `192.168.1.2` -- a device on the LAN with no ssh; the
Proxmox host is elsewhere. Node serves every request on one thread, so each page load froze the whole
panel for 45 s and every section, the healthy ones too, sat on "Loading…". The T7 version that carried
that file scored 0 (nothing refused): the unsourced-address gate (§17.1312) is silent for a job with no
ledger of facts, and no gate knew a blocking call.

Addresses below are made up; the shapes are the live file's.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import textwrap

import pytest

from app.modules import develop as dv
from app.modules import execution_agent

HOST = dv.Host(guest="111", vm=False, workdir="/opt/panel-backend", unit="panel.service",
               extra_dirs=("/opt/panel-ui",))

# the live route's shape: a kit-style host object, three blocking ssh calls in a sync handler
LIVE_ROUTE = textwrap.dedent("""\
    const remote = require('/opt/panel-backend/scaffold-kit/remote');
    const PROXMOX_HOST = { address: '10.9.8.2', user: 'root' };
    function getContainers() {
      const { execFileSync } = require('child_process');
      const result = execFileSync('ssh', ['-o', 'BatchMode=yes',
        `${PROXMOX_HOST.user}@${PROXMOX_HOST.address}`, "pct list 2>/dev/null"], { encoding: 'utf8', timeout: 15000 });
      return result;
    }
    module.exports = function (app) {
      app.get('/api/network-ui', (req, res) => { res.json({ containers: getContainers() }); });
    };
    """)

ASYNC_ROUTE = textwrap.dedent("""\
    const remote = require('/opt/panel-backend/scaffold-kit/remote');
    const PROXMOX_HOST = { address: '10.9.8.156', user: 'root' };
    module.exports = function (app) {
      app.get('/api/network-ui', async (req, res) => {
        const [cts, vms] = await Promise.all([remote.promises.run(PROXMOX_HOST, ['pct', 'list']),
                                              remote.promises.run(PROXMOX_HOST, ['qm', 'list'])]);
        res.json({ cts, vms });
      });
    };
    """)

# a route the version leaves alone that uses the kit's BLOCKING form under its own binding name
CARRIED_KIT_SYNC = textwrap.dedent("""\
    const kit = require('./scaffold-kit/remote.js');
    const GAME = { address: '10.9.8.44', user: 'steam' };
    module.exports = (app) => app.get('/api/game/status', (req, res) => res.json({ s: kit.unit(GAME, 'is-active', 'game') }));
    """)


# ── the blocking-call gate ──

def test_bites_on_the_live_routes_blocking_ssh_call():
    out = dv.blocks_the_event_loop({"/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}, {}, HOST, {})
    assert len(out) == 1
    assert "`execFileSync`" in out[0]["why"] and "BLOCKS the whole Node service" in out[0]["why"]
    assert "remote.promises" in out[0]["why"] and "Promise.all" in out[0]["why"]


def test_passes_the_async_version_of_the_same_route():
    assert dv.blocks_the_event_loop({"/opt/panel-backend/network_ui_route.js": ASYNC_ROUTE}, {}, HOST, {}) == []


def test_a_file_the_version_leaves_alone_is_judged_too_and_says_so():
    ws = {"/opt/panel-backend/game_route.js": CARRIED_KIT_SYNC,
          "/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}
    out = dv.blocks_the_event_loop({"/opt/panel-backend/network_ui_route.js": ASYNC_ROUTE}, ws, HOST, {})
    assert [o["command"].split(":")[0] for o in out] == ["/opt/panel-backend/game_route.js"]
    assert "`kit.unit`" in out[0]["why"], "the kit's blocking form under its own binding name"
    assert "already on the machine, unchanged by this version" in out[0]["why"]


def test_the_kit_itself_and_the_browsers_files_are_not_judged():
    kit = dv.kit_for(HOST, {"/etc/systemd/system/panel.service": "ExecStart=/usr/bin/node server.js"})
    assert kit, "a Node service gets the kit"
    assert any("execFileSync" in c for c in kit.values()), "the kit keeps its blocking forms for delivered code"
    ws = {"/opt/panel-ui/src/sync.js": "execSync('x')"}
    assert dv.blocks_the_event_loop({}, {**ws, **kit}, HOST, kit) == []


# ── the dead-address gate ──

def _run(coro):
    return asyncio.run(coro)


def _probe(table):
    calls = []

    async def probe(ip, port):
        calls.append((ip, port))
        return table.get((ip, port))
    return probe, calls


def test_ports_come_from_what_the_file_says():
    assert dv.ports_for_address(LIVE_ROUTE, "10.9.8.2") == ["22"], "a kit-style host object is an ssh target"
    assert dv.ports_for_address('fetch("http://10.9.8.30/api/x")', "10.9.8.30") == ["80"]
    assert dv.ports_for_address('const u = "http://10.9.8.26:8191/v1"', "10.9.8.26") == ["8191"]
    assert dv.ports_for_address('{"host": "10.9.8.27", "port": 8191}', "10.9.8.27") == ["8191"]
    assert dv.ports_for_address('const ADDR = "10.9.8.9";', "10.9.8.9") == [], "the file does not say: nothing judged"


def test_bites_when_the_address_refuses_the_port_the_file_uses():
    probe, calls = _probe({("10.9.8.2", "22"): False})
    spec = type("S", (), {"endpoint": "http://10.9.8.156:8790/mcp"})()
    out = _run(dv.addresses_that_do_not_answer(spec, {"/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}, {},
                                               HOST, ["10.9.8.25"], {}, {}, probe))
    assert calls == [("10.9.8.2", "22")]
    assert len(out) == 1 and "`10.9.8.2` on port 22" in out[0]["why"]
    assert "The Proxmox host is 10.9.8.156." in out[0]["why"]


def test_a_held_address_is_never_probed_and_an_answer_or_an_unknown_passes():
    spec = type("S", (), {"endpoint": "http://10.9.8.156:8790/mcp"})()
    files = {"/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}
    probe, calls = _probe({})
    assert _run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, ["10.9.8.2"], {}, {}, probe)) == []
    assert calls == []
    probe, _ = _probe({("10.9.8.2", "22"): True})
    assert _run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, [], {}, {}, probe)) == []
    probe, _ = _probe({("10.9.8.2", "22"): None})          # the probe could not run: no evidence
    assert _run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, [], {}, {}, probe)) == []


def test_one_answering_port_of_several_is_enough():
    spec = type("S", (), {"endpoint": ""})()
    body = 'const a = "http://10.9.8.40:8080/x"; const b = "http://10.9.8.40:9090/y";'
    probe, _ = _probe({("10.9.8.40", "8080"): False, ("10.9.8.40", "9090"): True})
    assert _run(dv.addresses_that_do_not_answer(spec, {"/opt/panel-backend/r.js": body}, {}, HOST, [], {}, {}, probe)) == []


def test_probes_are_cached_across_rounds():
    spec = type("S", (), {"endpoint": ""})()
    files = {"/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}
    cache: dict = {}
    probe, calls = _probe({("10.9.8.2", "22"): False})
    _run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, [], {}, cache, probe))
    _run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, [], {}, cache, probe))
    assert calls == [("10.9.8.2", "22")]


def test_the_real_probe_reads_the_runners_answer(monkeypatch):
    """The production probe, through service_truth._probe: OPEN/SHUT is the evidence; anything else is none."""
    from app.modules import service_truth as st
    answers = {}

    async def fake(spec, cmd):
        assert cmd.startswith("nc -z -w2 10.9.8.2 22 ")
        return answers["v"]
    monkeypatch.setattr(st, "_probe", fake)
    spec = type("S", (), {"endpoint": ""})()
    files = {"/opt/panel-backend/network_ui_route.js": LIVE_ROUTE}
    for v, refused in (((True, "SHUT\n"), 1), ((True, "OPEN\n"), 0), ((False, "(runner error: x)"), 0),
                       ((True, "(refused by the local runner)"), 0)):
        answers["v"] = v
        assert len(_run(dv.addresses_that_do_not_answer(spec, files, {}, HOST, [], {}, {}))) == refused, v


def test_the_probe_passes_the_read_only_classifier():
    from app.modules.assist_state_check import read_only_command
    assert read_only_command("nc -z -w2 10.9.8.2 22 && echo OPEN || echo SHUT")


def test_the_drafter_is_told_where_the_proxmox_host_is():
    spec = type("S", (), {"endpoint": "http://10.9.8.156:8790/mcp"})()
    assert dv.host_address(spec) == "10.9.8.156"
    assert "THE PROXMOX HOST: 10.9.8.156" in dv.machines_doc(spec)
    assert dv.machines_doc(type("S", (), {"endpoint": ""})()) == ""


def test_the_kit_doc_teaches_the_promises_form_the_gate_wants():
    doc = dv.kit_doc(HOST, {"k": "v"})
    assert "remote.promises.readFile" in doc and "remote.promises.run" in doc and "Promise.all" in doc
    assert "BLOCK" in doc


# ── wired into the develop loop ──

def test_the_develop_loop_runs_both_gates_and_states_the_host():
    src = inspect.getsource(execution_agent)
    assert "develop.blocks_the_event_loop(files, workspace, host, kit)" in src
    assert "await develop.addresses_that_do_not_answer(spec, files, workspace, host, _held_addrs," in src
    assert "develop.machines_doc(spec)" in src


# ── bash-level: the kit's promises form, run for real in Node ──

_IMAGE = "scaffold-rehearsal:local"


def _have_node_image() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _have_node_image(), reason="needs docker and the rehearsal image (Node 20)")
def test_bash_level_the_kits_promises_form_works_and_leaves_the_loop_free(tmp_path):
    kit_dir = os.path.join(os.path.dirname(dv.__file__), "develop_kit", "node")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text(textwrap.dedent("""\
        #!/bin/sh
        last=""; for a in "$@"; do last="$a"; done
        case "$last" in *fail*) echo "Permission denied (publickey)." >&2; exit 255;; esac
        case "$last" in *slow*) sleep 2;; esac
        printf 'ARGS:%s\\n' "$*"; printf 'IN:'; cat; echo
        """))
    (bindir / "ssh").chmod(0o755)
    (tmp_path / "t.js").write_text(textwrap.dedent("""\
        const r = require('/kit/remote.js'); const assert = require('assert');
        const h = {address: '10.9.8.6', user: 'op'};
        (async () => {
          assert.match(r.readFile(h, '/a b'), /ConnectTimeout=5 op@10\\.9\\.8\\.6 'sudo' 'cat' '\\/a b'/);
          assert.match(await r.promises.readFile(h, "/it's"), /'sudo' 'cat' '\\/it'\\\\''s'/);
          assert.match(await r.promises.run(h, ['pct', 'list'], {input: 'hello'}), /IN:hello/);
          await assert.rejects(r.promises.readFile(h, '/fail'), /failed: Permission denied \\(publickey\\)/);
          assert.strictEqual(await r.promises.exists(h, '/fail'), false);
          assert.strictEqual(await r.promises.exists(h, '/ok'), true);
          await assert.rejects(r.promises.unit(h, 'rm', 'x'), /not allowed/);
          let ticks = 0; const iv = setInterval(() => ticks++, 100);
          await r.promises.run(h, ['slow']); clearInterval(iv);
          assert.ok(ticks >= 10, 'the loop ticked ' + ticks + ' times');
          ticks = 0; const iv2 = setInterval(() => ticks++, 100); r.readFile(h, 'slow'); clearInterval(iv2);
          assert.strictEqual(ticks, 0, 'the blocking form blocks: why the gate exists');
          console.log('KIT OK');
        })().catch(e => { console.error('KIT FAIL', e); process.exit(1); });
        """))
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{kit_dir}:/kit:ro",
                        "-v", f"{tmp_path}:/t:ro", "-e", "PATH=/t/bin:/usr/local/bin:/usr/bin:/bin",
                        "--entrypoint", "node", _IMAGE, "/t/t.js"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "KIT OK" in r.stdout, r.stdout + r.stderr
