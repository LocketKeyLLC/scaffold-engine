"""§17.1185 — the supervised write channel: gated at both ends, signed per
command, approved per block by the operator, recorded as a paste would be."""
from __future__ import annotations

import importlib.util
import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _runner():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


ALLOW = ["apt-get install", "pct set", "pct start", "tee -a /etc/caddy/", "systemctl restart"]


# ── the gate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "apt-get install -y qemu-guest-agent",
    "sudo apt-get install -y caddy",
    "pct set 111 --nameserver 192.168.1.1",
    "pct status 111",                                    # read-only passes on its own
    "echo 'reverse_proxy 127.0.0.1:3001' | tee -a /etc/caddy/Caddyfile",
    "systemctl restart caddy && systemctl is-active caddy",
    'echo "== S:ADD88/abcd1234 =="',                      # the step sentinel
])
def test_allowed_shapes(cmd):
    assert sw.write_allowed(cmd, ALLOW) == (True, ""), cmd


@pytest.mark.parametrize("cmd,why", [
    ("rm -rf /var/lib/caddy", "not on the write-allow list"),
    ("apt-get remove caddy", "not on the write-allow list"),
    ("pct destroy 111", "not on the write-allow list"),
    ("tee -a /etc/passwd", "not on the write-allow list"),          # the prefix is /etc/caddy/
    ("echo x > /etc/caddy/Caddyfile", "redirect"),
    ("pct set 111 --hostname $(hostname)", "substitution"),
    ("bash -c 'apt-get install -y x'", "not on the write-allow list"),
    ("apt-get install -y x; rm -rf /tmp/y", "not on the write-allow list"),   # every segment is judged
])
def test_refused_shapes(cmd, why):
    ok, reason = sw.write_allowed(cmd, ALLOW)
    assert not ok and why in reason, (cmd, reason)


def test_an_empty_allow_list_refuses_everything():
    assert sw.write_allowed("pct set 111 --onboot 1", [])[0] is False


@pytest.mark.parametrize("cmd", ["rm -rf /", "rm -rf /etc", "sudo rm -rf /var/*", "mkfs.ext4 /dev/sda1",
                                 "dd if=/dev/zero of=/dev/sda", "reboot", "systemctl reboot", "iptables -F",
                                 "systemctl stop local-runner-mcp", ":(){ :|:& };:", "chown -R x /"])
def test_catastrophic_is_refused_even_when_a_prefix_would_match(cmd):
    assert sw.catastrophic(cmd), cmd
    runnable, refused = sw.gate_block([cmd], ["rm -rf", "mkfs.ext4", "dd", "reboot", "systemctl", "iptables", "chown"])
    assert runnable == [] and refused and refused[0]["command"] == cmd


def test_gate_block_refuses_the_block_when_one_line_fails():
    runnable, refused = sw.gate_block(["apt-get install -y x", "pct destroy 111", "pct start 111"], ALLOW)
    assert runnable == ["apt-get install -y x", "pct start 111"] and refused[0]["command"] == "pct destroy 111"


def test_block_commands_keeps_order_and_the_sentinel_drops_comments_and_fences():
    block = "```bash\n# install\n$ apt-get install -y x\n\npct start 111\necho \"== S:ADD50/deadbeef ==\"\n```"
    assert sw.block_commands(block) == ["apt-get install -y x", "pct start 111", 'echo "== S:ADD50/deadbeef =="']


# ── approvals ────────────────────────────────────────────────────────────

def test_a_minted_approval_verifies_on_the_runner_once_and_only_for_those_bytes():
    r = _runner()
    ap = sw.mint_approval("pct start 111", "tok", now=1000)
    seen: dict = {}
    assert r.verify_approval("pct start 111", ap, "tok", now=1001, seen=seen) == (True, "")
    assert r.verify_approval("pct start 111", ap, "tok", now=1002, seen=seen)[1] == "approval already used"
    assert r.verify_approval("pct start 112", ap, "tok", now=1001, seen={})[1].startswith("approval signature")
    assert r.verify_approval("pct start 111", ap, "other", now=1001, seen={})[1].startswith("approval signature")
    assert r.verify_approval("pct start 111", ap, "tok", now=1000 + sw.APPROVAL_TTL + 1, seen={})[1] == "approval expired"
    long = sw.mint_approval("pct start 111", "tok", ttl=3600, now=1000)
    assert r.verify_approval("pct start 111", long, "tok", now=1001, seen={})[1] == "approval lifetime too long"
    assert r.verify_approval("pct start 111", {"id": "x"}, "tok", now=1001, seen={})[1] == "malformed approval"
    assert r.verify_approval("pct start 111", ap, "", now=1001, seen={})[1].startswith("no token")


def test_the_gate_helpers_are_byte_equal_at_both_ends():
    r = _runner()
    for name in ("catastrophic", "write_allowed", "approval_key", "approval_message", "split_segments", "mask_quoted"):
        assert inspect.getsource(getattr(r, name)) == inspect.getsource(getattr(sw, name)), name
    assert [x.pattern for x, _ in r._CATASTROPHIC] == [x.pattern for x, _ in sw._CATASTROPHIC]
    assert r.APPROVAL_MAX_TTL >= sw.APPROVAL_TTL


# ── the run ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_block_signs_each_command_and_stops_at_the_first_failure():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "tok"}
    calls = []

    async def fake_call(spec_, tool, args):
        calls.append((tool, args["command"], args["approval"]))
        r = MagicMock(); r.structured = None
        r.text = "[exit 0]\nok" if "install" in args["command"] else "[exit 1]\nE: no such CT"
        return r

    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        done = await sw.run_block(spec, ["apt-get install -y x", "pct start 999", "pct start 111"])
    assert [c[0] for c in calls] == ["run_supervised", "run_supervised"]        # the third never ran
    assert all(set(c[2]) == {"id", "nonce", "exp", "sig"} for c in calls)
    assert calls[0][2]["nonce"] != calls[1][2]["nonce"]
    assert [d["ok"] for d in done] == [True, False] and done[1]["exit"] == 1
    rec = sw.record_text("pve-runner", done)
    assert rec.startswith(sw.MARK) and "$ apt-get install -y x\nok" in rec and "$ pct start 999\nE: no such CT\nrc=1" in rec
    assert "stopped at the first failure" in rec
    note = sw.render_note("pve-runner", done, [])
    assert "Stopped at `pct start 999`" in note and "exited 1" in note


@pytest.mark.asyncio
async def test_a_runner_refusal_is_not_an_answer():
    spec = MagicMock(); spec.name = "r"; spec.headers = {"X-Runner-Token": "tok"}
    r = MagicMock(); r.structured = None; r.text = "(refused by the local runner: not on the write-allow list: pct destroy)"
    with patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=r)):
        done = await sw.run_block(spec, ["pct destroy 111"])
    assert done[0]["refused"] and not done[0]["ok"] and done[0]["exit"] is None


@pytest.mark.asyncio
async def test_write_policy_is_none_without_the_tool_or_with_an_empty_list():
    spec = MagicMock(); spec.name = "r"
    sw.clear_policy_cache()
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=[{"name": "run_readonly"}])):
        assert await sw.write_policy(spec) is None
    res = MagicMock(); res.structured = None; res.text = '{"helper": "11", "allow": [], "sudo": false}'
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=[{"name": "run_readonly"}, {"name": "run_supervised"}, {"name": "write_policy"}])), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        assert await sw.write_policy(spec) is None
    res.text = '{"helper": "11", "allow": ["pct set"], "sudo": true}'
    sw.clear_policy_cache()                      # the empty answer above was cached for this runner
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(return_value=[{"name": "run_supervised"}, {"name": "write_policy"}])), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        # §17.1273 — the contract gained `can_write_files`: whether this runner can
        # be handed a FILE rather than a shell command that builds one. False here,
        # because the tool list above has no `write_file` (helper v11).
        assert await sw.write_policy(spec) == {"allow": ["pct set"], "sudo": True, "helper": "11",
                                              "secrets": [], "can_write_files": False}


# ── the runner side ──────────────────────────────────────────────────────

def test_runner_sudoers_lines_resolve_paths_and_scope_arguments():
    r = _runner()
    which = {"apt-get": "/usr/bin/apt-get", "pct": "/usr/sbin/pct", "tee": "/usr/bin/tee"}.get
    text, unresolved = r.sudoers_writes_text(["apt-get install", "pct set", "tee -a /etc/caddy/", "frob x"], "scaffold-runner", which=which)
    assert "scaffold-runner ALL=(root) NOPASSWD: /usr/bin/apt-get install *" in text
    assert "scaffold-runner ALL=(root) NOPASSWD: /usr/sbin/pct set *" in text
    assert "scaffold-runner ALL=(root) NOPASSWD: /usr/bin/tee -a /etc/caddy/*" in text
    assert unresolved == ["frob x"] and "frob" not in text


def test_runner_unit_carries_the_write_list_and_drops_nonewprivileges_only_when_sudo_is_on():
    r = _runner()
    u = r.unit_text(python="p", script="s", host="0.0.0.0", port=1, token="t", write_allow=["pct set"], write_sudo=True)
    assert "--write-allow 'pct set' --write-sudo" in u and "NoNewPrivileges" not in u and "supervised writes on" in u
    u2 = r.unit_text(python="p", script="s", host="0.0.0.0", port=1, token="t", write_allow=["pct set"], write_sudo=False)
    assert "--write-sudo" not in u2 and "NoNewPrivileges=yes" in u2
    u3 = r.unit_text(python="p", script="s", host="0.0.0.0", port=1, token="t")
    assert "--write-allow" not in u3 and "supervised writes" not in u3


def test_runner_supervised_tool_refuses_before_it_runs():
    """The tool body checks, in order: the allow-list exists, catastrophic,
    write_allowed, verify_approval — and only then runs (as sudo -n when the
    installer wrote the sudoers)."""
    src = inspect.getsource(_runner().build_server)
    body = src[src.index("async def run_supervised"):]
    order = [body.index(k) for k in ("if not writes", "catastrophic(command)", "write_allowed(command, writes)",
                                     "verify_approval(command, approval", "create_subprocess_shell")]
    assert order == sorted(order)
    assert 'log.warning("SUPERVISED id=%s RUN' in body and "[exit {proc.returncode}]" in body


def test_the_route_is_the_approval_and_refuses_without_a_channel():
    import app.routers.assist as ar
    src = inspect.getsource(ar.assist_run_block)
    assert "write_policy(spec)" in src and "gate_block(commands" in src and 'if refused:' in src
    assert src.index("gate_block(commands") < src.index("run_block(spec, runnable)") < src.index("start_turn_run(")
    assert "the runner has no supervised write channel" in src


@pytest.mark.asyncio
async def test_write_policy_is_cached_and_detection_never_calls_out():
    spec = MagicMock(); spec.name = "cached-runner"
    sw.clear_policy_cache()
    assert sw.cached_policy(spec) == (False, None)
    res = MagicMock(); res.structured = None; res.text = '{"helper": "11", "allow": ["pct set"], "sudo": false}'
    lt = AsyncMock(return_value=[{"name": "run_supervised"}, {"name": "write_policy"}])
    with patch("app.modules.mcp_client.list_tools", new=lt), patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        first = await sw.write_policy(spec)
        second = await sw.write_policy(spec)
    assert first == second and lt.await_count == 1                 # the second answer came from the cache
    assert sw.cached_policy(spec) == (True, first)
    sw.clear_policy_cache(spec.name)
    assert sw.cached_policy(spec) == (False, None)


# ── §17.1189 — "host power" is the HOST's power, not a guest's ────────────

@pytest.mark.parametrize("cmd", [
    "reboot", "sudo reboot", "sudo -n poweroff", "shutdown -h now", "systemctl reboot",
    "halt", "init 0", "echo done && reboot", "systemctl kexec",
])
def test_host_power_is_still_refused(cmd):
    assert sw.catastrophic(cmd) == "host power — do that by hand", cmd


@pytest.mark.parametrize("cmd", [
    "pct reboot 111",            # a container cycle: an ordinary build step
    "qm reboot 106",             # a VM cycle, likewise
    "pct shutdown 111",
    "qm shutdown 106",
    "systemctl restart sshd",    # a service, not the host
    "grep reboot /var/log/syslog",   # read-only: the word, not the act
    "ls -l /var/run/reboot-required",
])
def test_guest_power_and_the_word_itself_are_not_host_power(cmd):
    """The denylist matched the WORD anywhere in the line, so it refused
    `pct reboot 111` as "host power" while `qm start`/`pct start` were never
    on the list at all — the middle of a lifecycle it otherwise allows. It
    also refused a read-only `grep reboot …`. Measured on the operator's real
    plan: 1 of 21 pending hands-on steps lost this way."""
    assert sw.catastrophic(cmd) == "", cmd


def test_guest_power_still_needs_the_operators_allow_list():
    """Not catastrophic is not free: `pct reboot` still writes, so it runs
    only when the operator put that prefix on the runner's --write-allow."""
    assert sw.write_allowed("pct reboot 111", [])[0] is False
    assert sw.write_allowed("pct reboot 111", ["pct start"])[0] is False
    assert sw.write_allowed("pct reboot 111", ["pct reboot"])[0] is True
