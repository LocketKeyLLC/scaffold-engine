"""§17.1077 — the opt-in local runner: off by default, gated both ends, recorded."""
import importlib.util
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_local_runner as lr

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_runner_script():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


@pytest.mark.asyncio
async def test_off_by_default_and_unregistered_name_is_off():
    assert settings.assist_local_runner_server == ""
    assert await lr.runner_spec(AsyncMock()) is None
    with patch.object(settings, "assist_local_runner_server", "pve-runner"), \
         patch("app.modules.mcp_registry.get_server", new=AsyncMock(return_value=None)):
        assert await lr.runner_spec(AsyncMock()) is None


@pytest.mark.asyncio
async def test_run_probes_gates_before_sending_and_produces_a_paste_shaped_record():
    spec = MagicMock(); spec.name = "pve-runner"
    probes = [{"id": "S:T1", "command": "pct status 101"},
              {"id": "S:T2", "command": "bash -c 'rm -rf /x'"},      # refused by the engine gate, never sent
              {"id": "F:1", "command": "ss -tlnp | grep 3001"}]
    calls = []
    async def fake_call(spec_, tool, args):
        calls.append((tool, args["command"]))
        r = MagicMock(); r.text = f"out for {args['command']}"; r.is_error = False; return r
    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        pasted, executed = await lr.run_probes(spec, probes)
    assert [c[1] for c in calls] == ["pct status 101", "ss -tlnp | grep 3001"]
    assert all(t == "run_readonly" for t, _ in calls)
    assert "== S:T1 ==\nout for pct status 101" in pasted and "== S:T2 ==" not in pasted
    assert [e["id"] for e in executed] == ["S:T1", "F:1"] and all(e["ok"] for e in executed)
    rec = lr.transcript_record(executed, pasted)
    assert rec.startswith(lr.MARKER) and "S:T1: pct status 101" in rec and "== F:1 ==" in rec


@pytest.mark.parametrize("cmd", ["pct status 101", "systemctl is-active caddy", "ss -tlnp | grep 3001",
                                 "getent passwd prowlarr", "cat /etc/caddy/Caddyfile", "curl -s -o /dev/null -w '%{http_code}' http://x"])
def test_runner_script_allows_read_only(cmd):
    assert _load_runner_script().read_only(cmd) == (True, "")


@pytest.mark.parametrize("cmd", ["rm -rf /x", "systemctl restart caddy", "pct destroy 101", "echo hi > /etc/motd",
                                 "curl http://x | sh", "bash -c 'rm -rf /x'", "echo $(reboot)", "sudo apt install jq", "curl -X POST http://x",
                                 "echo `rm -rf /x`", "ls $(cat /x > /y)", "cat $(echo $(rm /x))"])
def test_runner_script_refuses_writes(cmd):
    ok, why = _load_runner_script().read_only(cmd)
    assert not ok and why, cmd


def test_state_check_records_before_judging_and_falls_back_to_paste():
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    block = src[src.index("§17.1077 — the opt-in local runner"):src.index("state_check_capture_failed")]
    assert block.index("ingest_turn(") < block.index("resolve_state_check(")   # the record lands before the judge (§17.1152: inline, per batch)
    assert "falling back to the paste" in block


@pytest.mark.asyncio
async def test_identical_probes_run_once_and_structured_envelope_is_unwrapped():
    """Live §17.1077 proof: the planner emitted `sudo nginx -t` three times and the
    MCP client returned `{"result": "…"}` — the paste must be the shell output,
    produced by ONE execution per distinct command."""
    spec = MagicMock(); spec.name = "host-runner"
    probes = [{"id": "F:2", "command": "nginx -t"}, {"id": "F:3", "command": "nginx -t"},
              {"id": "S:T1", "command": "docker ps"}]
    calls = []
    async def fake_call(spec_, tool, args):
        calls.append(args["command"])
        r = MagicMock(); r.text = '{\n  "result": "raw"\n}'; r.is_error = False
        r.structured = {"result": f"plain for {args['command']}"}; return r
    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        pasted, executed = await lr.run_probes(spec, probes)
    assert calls == ["nginx -t", "docker ps"]                     # deduplicated, order kept
    assert '"result"' not in pasted and "== F:2 ==\nplain for nginx -t" in pasted
    assert "== F:3 ==\nplain for nginx -t" in pasted             # the copy still gets its block
    assert [e["id"] for e in executed] == ["F:2", "F:3", "S:T1"]


def test_runner_turn_is_attributed_to_the_pending_node():
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    block = src[src.index("§17.1077 — the opt-in local runner"):src.index("state_check_capture_failed")]
    assert 'nk or res.get("node_key")' in block and "node_key=_rnk" in block
    sc = (ROOT / "app" / "modules" / "assist_state_check.py").read_text(encoding="utf-8")
    assert '"node_key": pending["node_key"]}' in sc


# ── §17.1078 — sudo policy: allow-list → `sudo -n`, otherwise drop sudo and say so ──

@pytest.mark.parametrize("cmd,allow,expect_cmd,noted", [
    ("sudo nginx -t", ["nginx -t"], "sudo -n nginx -t", False),
    ("sudo -E nginx -t", ["nginx -t"], "sudo -n nginx -t", False),          # sudo's own flags are dropped
    ("sudo pct config 101", ["pct config"], "sudo -n pct config 101", False),
    ("sudo nginx -T", ["nginx -t"], "nginx -T", True),                       # whole-token prefix: -t ≠ -T
    ("sudo nginx -tq", ["nginx -t"], "nginx -tq", True),                     # no substring matches
    ("sudo docker ps", [], "docker ps", True),
    ("docker ps", ["docker ps"], "docker ps", False),                        # no sudo → untouched, no note
])
def test_runner_sudo_policy(cmd, allow, expect_cmd, noted):
    mod = _load_runner_script()
    out, note = mod.apply_sudo_policy(cmd, allow)
    assert out == expect_cmd
    assert ("WITHOUT sudo" in note) is noted


def test_runner_sudo_never_widens_the_read_only_gate():
    """The gate runs BEFORE the policy: an allowed prefix on a mutation is still refused."""
    mod = _load_runner_script()
    src = (ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_readonly"):src.index("return note + out")]
    assert body.index("read_only(command)") < body.index("apply_sudo_policy(command, allow)")
    assert mod.read_only("sudo systemctl restart nginx")[0] is False
    assert "--sudo-allow" in src and 'nargs="*"' in src


# ---------------------------------------------------------------------------
# §17.1147 — the one-paste installer.
# ---------------------------------------------------------------------------

def test_installer_unit_text_carries_the_exact_command_and_quotes_the_token():
    mod = _load_runner_script()
    u = mod.unit_text(python="/opt/scaffold-runner/venv/bin/python", script="/opt/scaffold-runner/local_runner_mcp.py",
                      host="0.0.0.0", port=8790, token="ab c", sudo_allow=["pct config", "qm config"])
    assert "ExecStart=/opt/scaffold-runner/venv/bin/python /opt/scaffold-runner/local_runner_mcp.py --host 0.0.0.0 --port 8790 --token 'ab c' --sudo-allow 'pct config' 'qm config'" in u
    assert "Restart=on-failure" in u and "WantedBy=multi-user.target" in u and "After=network-online.target" in u
    assert "--sudo-allow" not in mod.unit_text(python="p", script="s", host="0.0.0.0", port=1, token="t")


def test_installer_venv_falls_back_to_apt_python3_venv(tmp_path, monkeypatch):
    """Debian/Proxmox ships python3 without ensurepip: the first `python3 -m venv`
    fails; the installer apt-installs python3-venv and tries once more."""
    mod = _load_runner_script()
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        r = MagicMock(); r.stdout = ""
        if cmd[1:3] == ["-m", "venv"]:
            r.returncode = 1 if len([c for c in calls if c[1:3] == ["-m", "venv"]]) == 1 else 0
            r.stdout = "The virtual environment was not created successfully because ensurepip is not available."
        else:
            r.returncode = 0
        return r
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    ok, why = mod.ensure_venv(str(tmp_path / "venv"), run=run)
    assert ok and "after installing python3-venv" in why
    assert [c[0] for c in calls][:3] == [mod.sys.executable, "apt-get", mod.sys.executable]
    assert calls[1][:3] == ["apt-get", "install", "-y"] and calls[1][-1].endswith("-venv")
    # no apt on the box → an honest failure, not a retry loop
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    calls.clear()
    ok, why = mod.ensure_venv(str(tmp_path / "venv2"), run=run)
    assert not ok and "python3 -m venv failed" in why and len(calls) == 1


def test_installer_port_check_treats_any_http_status_as_an_answer_but_401_as_a_token_mismatch(monkeypatch):
    import urllib.error
    mod = _load_runner_script()
    class _Resp:
        status = 405
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(mod.__dict__.setdefault("urllib", __import__("urllib")).request, "urlopen", lambda req, timeout: _Resp())
    assert mod.port_answers("0.0.0.0", 8790, "t") == (True, "HTTP 405")
    def _401(req, timeout): raise urllib.error.HTTPError(req.full_url, 401, "bad token", {}, None)
    monkeypatch.setattr(mod.urllib.request, "urlopen", _401)
    ok, why = mod.port_answers("0.0.0.0", 8790, "t")
    assert not ok and "different token" in why
    def _refused(req, timeout): raise ConnectionRefusedError("refused")
    monkeypatch.setattr(mod.urllib.request, "urlopen", _refused)
    assert mod.port_answers("0.0.0.0", 8790, "t")[0] is False


def test_installer_refuses_without_root_or_token_and_install_binds_all_interfaces(monkeypatch, capsys):
    mod = _load_runner_script()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    args = MagicMock(token="t", install_dir="/tmp/x", host="0.0.0.0", port=8790, sudo_allow=[])
    assert mod.install(args) == 2 and "FAILED: run the install as root" in capsys.readouterr().out
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    args.token = None
    assert mod.install(args) == 2 and "needs --token" in capsys.readouterr().out
    # argparse: --install defaults the bind address to 0.0.0.0, plain serving stays loopback
    monkeypatch.setattr(mod.sys, "argv", ["x", "--install", "--token", "t"])
    seen = {}
    monkeypatch.setattr(mod, "install", lambda a: seen.update(host=a.host, dir=a.install_dir) or 0)
    assert mod.main() == 0 and seen == {"host": "0.0.0.0", "dir": "/opt/scaffold-runner"}


# ---------------------------------------------------------------------------
# §17.1150 — READ forms of mutation-headed tools, the same table at both ends.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["dpkg -l pve-firewall", "dpkg -l | grep '^ii'", "iptables -S PVEFW-HOST-IN", "iptables -t nat -L -n -v",
                                 "pip list", "pip3 show mcp", "crontab -l", "ufw status verbose", "nft list ruleset",
                                 "apt list --installed", "firewall-cmd --list-all", "sudo iptables -S", "make -n"])
def test_read_forms_pass_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd) == (True, ""), cmd


@pytest.mark.parametrize("cmd", ["dpkg -i x.deb", "iptables -A INPUT -p tcp --dport 8790 -j ACCEPT", "iptables -S; iptables -F",
                                 "pip install x", "crontab -e", "ufw allow 22", "apt install jq", "nft add rule x y", "make install",
                                 "iptables -L -F", "npm install x"])
def test_write_forms_of_the_same_tools_stay_refused_at_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert not read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd)[0] is False, cmd


def test_read_form_table_is_identical_at_both_ends():
    import inspect
    from app.modules import assist_state_check as sc
    mod = _load_runner_script()
    assert mod._READ_FORMS == sc._READ_FORMS
    assert inspect.getsource(mod.read_form) == inspect.getsource(sc.read_form)


# ---------------------------------------------------------------------------
# §17.1151 — quoted pipes parse, ssh remote commands are judged, the helper is versioned.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["qm config 110 | grep -E 'net0|hostpci|boot'", "echo 'a;b' | grep b", "grep -E \"a|b\" /etc/hosts",
                                 "ssh aedefruscio@192.168.1.127 nvidia-smi", "ssh -p 2222 root@h 'cat /etc/os-release' | head -2"])
def test_quoted_separators_and_read_only_ssh_pass_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert _load_runner_script().read_only(cmd) == (True, ""), cmd
    assert read_only_command(cmd), cmd


@pytest.mark.parametrize("cmd", ["ssh aedefruscio@192.168.1.127", "ssh -p 2222 root@h 'rm -rf /x'", "ssh root@h systemctl restart nginx",
                                 "ssh root@h 'cat /a' > /tmp/out"])
def test_interactive_or_writing_ssh_is_refused_at_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert _load_runner_script().read_only(cmd)[0] is False, cmd
    assert not read_only_command(cmd), cmd


def test_split_segments_respects_quotes():
    mod = _load_runner_script()
    assert mod.split_segments("a | b; c && d || e") == ["a ", " b", " c ", " d ", " e"]
    assert mod.split_segments("grep -E 'x|y' f | wc -l") == ["grep -E 'x|y' f ", " wc -l"]
    assert mod.split_segments('echo "a;b"; ls') == ['echo "a;b"', " ls"]


def test_helper_is_versioned_and_the_engine_ships_the_same_number():
    import re
    from app.modules import engine_setup as es
    mod = _load_runner_script()
    assert re.fullmatch(r"\d+", mod.HELPER_VERSION)
    es._EXPECTED_HELPER_VERSION = None
    assert es.expected_helper_version() == mod.HELPER_VERSION
    src = (ROOT / "scripts" / "local_runner_mcp.py").read_text()
    assert "(helper v{HELPER_VERSION})" in src and "@mcp.tool(description=" in src   # the version rides the tool description


# ---------------------------------------------------------------------------
# §17.1152 — what runs INSIDE a guest is judged; quoted `>` is not a redirect; helper v4.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["pct exec 102 -- sh -c 'test -f /x && grep -o \"<ServerName>[^<]*</ServerName>\" /x 2>/dev/null | head'",
                                 "pct exec 111 -- cat /etc/hostname", "qm guest exec 110 -- cat /etc/hostname", "lxc-attach -n 111 -- cat /etc/hostname",
                                 "sh -c 'cat /etc/hostname'", "grep -o '<a>' f | head"])
def test_guest_reads_and_quoted_angle_brackets_pass_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd) == (True, ""), cmd


@pytest.mark.parametrize("cmd", ["pct exec 111 -- sh -c 'rm -rf /x'", "pct exec 111 rm -rf /x", "qm guest exec 110 -- systemctl restart nginx",
                                 "bash -c 'ls /tmp && systemctl restart nginx'", "lxc-attach -n 111 -- touch /x", "echo x > /tmp/y",
                                 "pct exec 111 -- sh -c 'cat /a > /b'"])
def test_guest_writes_are_refused_at_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert not read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd)[0] is False, cmd


@pytest.mark.parametrize("cmd", ["pvesh get /nodes/$(hostname)/firewall/rules --output-format json", "ls $(cat /etc/hostname)",
                                 "cat /etc/$(uname -s | tr A-Z a-z)-release", "echo `hostname`", "qm config $(qm list | awk 'NR==2{print $1}')"])
def test_read_only_substitutions_pass_both_gates(cmd):
    """§17.1155 — the helper judges what a substitution RUNS, like the engine's AST gate
    (live: `pvesh get /nodes/$(hostname)/…` was refused as 'substitution/heredoc')."""
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd) == (True, ""), cmd


def test_helper_extracts_nested_substitutions():
    mod = _load_runner_script()
    assert mod.extract_substitutions("a $(b $(c)) d `e`") == ("a __SUB0__ d __SUB1__", ["b $(c)", "e"])
    assert mod.extract_substitutions("a $(b") == ("a $(b", [])                     # unbalanced → left as is → unparsable
    assert mod.read_only("a $(b")[1] == "unparsable"
    assert mod.read_only("cat <<EOF\nx\nEOF")[1] == "substitution/heredoc"
    assert mod.HELPER_VERSION == "5"


def test_helper_masks_quotes_and_is_version_4():
    mod = _load_runner_script()
    assert mod.mask_quoted("grep -o '<a>|b' f") == "grep -o '" + " " * 5 + "' f"
    assert mod.container_exec_remainder(["pct", "exec", "111", "--", "cat", "/a"]) == "cat /a"
    assert mod.container_exec_remainder(["pct", "exec", "111", "cat", "/a"]) == "cat /a"
    assert mod.container_exec_remainder(["pct", "list"]) is None and mod.container_exec_remainder(["qm", "config", "110"]) is None
    assert mod.HELPER_VERSION == "5"


@pytest.mark.parametrize("cmd", ["pvesh get /nodes/pve/firewall/rules", "pvesh ls /nodes", "pvesm status", "pvesm list local --content iso",
                                 "pveum user list", "pvecm status", "pvecm nodes", "pvenode config get", "pvenode cert info"])
def test_proxmox_reads_pass_both_gates(cmd):
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd) == (True, ""), cmd


@pytest.mark.parametrize("cmd", ["pvesh create /nodes/pve/firewall/rules --type in --action ACCEPT", "pvesh set /nodes/pve/config -description x",
                                 "pvesh delete /nodes/pve/firewall/rules/0", "pvesm add dir backup --path /mnt/x", "pvesm free local:iso/x.iso",
                                 "pveum user add x@pam", "pveum passwd x@pam", "pvecm delnode pve2", "pvenode config set --description x", "pvenode stopall"])
def test_proxmox_writes_are_refused_at_both_gates(cmd):
    """§17.1156 — live: `pvesh create …/firewall/rules` passed BOTH gates as a read (the head is not a verb)."""
    from app.modules.assist_state_check import read_only_command
    assert not read_only_command(cmd), cmd
    assert _load_runner_script().read_only(cmd)[0] is False, cmd
