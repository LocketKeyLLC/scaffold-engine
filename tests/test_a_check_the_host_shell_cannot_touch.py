"""§17.1496 — three places a value reached a shell that was not the one meant to read it.

- The in-guest templates rendered the model's check RAW inside `bash -c "{VERIFY_INSIDE}"` on
  the HOST: `grep -q "ListenPort = 51820" wg0.conf` word-split into `bash -c "grep -q ListenPort"
  = "51820 wg0.conf"` (grep reads stdin, exits 1, an already-met step is not recorded) and
  `awk '{print $5}'` lost its `$5` to the host. The check is escaped for the double-quoted
  word now.
- `secret_files_from_text` turned ANY `$NAME … to /path` prose into a truncating
  `cat > /path <<< "$NAME"` in the helper's start phase; `$PANEL_PUBKEY` (not a stored secret,
  so never injected) expands empty in the root shell and authorized_keys became one newline.
- `read_on_the_machine` pasted the `$(cat …)` read wherever the command NAMED the guest,
  including bare `pct exec 103 -- curl -H "X-Api-Key: <KEY>"` and a double-quoted `sh -c "…"`
  payload, where the HOST shell expands it first (the §17.1342 empty-header 401, re-created).
"""
from __future__ import annotations

import subprocess

import pytest

from app.modules import machine_truth as mt
from app.modules import machine_values as mv
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
ENV = {"profile": "root@pve", "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}},
                                                "106": {"kind": "vm", "attrs": {"name": "palworld-server"}},
                                                "111": {"kind": "ct", "attrs": {"hostname": "control-panel"}}}}
ADD84 = {"node_key": "ADD84", "title": "Grow the VM 106 filesystem to fill the disk",
         "description": "Inside the palworld-server guest, grow the partition and filesystem so the OS sees the full "
                        "~100GB. Done when `df -h` inside the guest reports the root filesystem at ~100GB."}
ADD100 = {"node_key": "ADD100", "title": "Rework the control-panel backend in LXC 111",
          "description": "Rework the control-panel backend in LXC 111 so it does the things chosen."}
CHECK = 'grep -q "ListenPort = 51820" /etc/wireguard/wg0.conf'


def _truth(kind, agent=False):
    return mt.GuestTruth(gid="106" if kind == "vm" else "111", kind=kind, status="running", agent=agent)


def _frame(rb, node):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(node, rb, spec, POLICY, env=ENV)


def _what_the_guest_gets(verify_line: str) -> str:
    """Run the host side of `… -- bash -c "<slot>"` for real: what bash inside the guest would be handed."""
    inner = verify_line.split(" -- bash -c ", 1)[1]
    return subprocess.run(["bash", "-c", f"printf '%s' {inner}"], capture_output=True, text=True).stdout


def test_escaping_is_exactly_the_four_characters_the_host_shell_reads_in_double_quotes():
    assert rt.escape_for_double_quotes(CHECK) == 'grep -q \\"ListenPort = 51820\\" /etc/wireguard/wg0.conf'
    assert rt.escape_for_double_quotes("awk '{print $5}' `x` \\n") == "awk '{print \\$5}' \\`x\\` \\\\n"
    assert rt.escape_for_double_quotes("df -h /") == "df -h /", "a plain check is untouched"
    for check in (CHECK, "awk '{print $5}' /x", "echo `hostname` \\ done", "ls -la /opt/palworld/*.sh"):
        assert _what_the_guest_gets(f'pct exec 111 -- bash -c "{rt.escape_for_double_quotes(check)}"') == check


@pytest.mark.parametrize("tpl,node,truth,prefix", [
    (rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), "qm guest exec 106 -- bash -c "),
    (rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), "pct exec 111 -- bash -c "),
])
def test_a_quoted_check_reaches_the_guest_whole(tpl, node, truth, prefix):
    vals = {"REMOTE_COMMANDS": "true", "VERIFY_INSIDE": rt.escape_for_double_quotes(CHECK)}
    rb = rt.render(tpl, rt.values_for(tpl, node, truth, ENV, vals))
    verify = sr.verify_commands(rb)
    assert verify == [prefix + '"grep -q \\"ListenPort = 51820\\" /etc/wireguard/wg0.conf"'], verify
    assert _what_the_guest_gets(verify[0]) == CHECK
    frame = _frame(rb, node)
    # §17.1630 — and after them the step's own "Done when" checks, which the run makes too (ADD84's `df -h`)
    assert frame["verify"] == sr.with_own_checks(node, verify, frame["commands"]), [r["why"][:100] for r in frame["refused"]]
    assert frame["verify"][:1] == verify


@pytest.mark.asyncio
async def test_the_draw_is_escaped_once_at_render(monkeypatch):
    import app.utils.llm_retry as lr

    async def fake(gen, prompt, params, *, system, **kw):
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\ndf -h / | awk '{print $5}'\n```"})()
        return type("R", (), {"text": "```bash\ngrowpart /dev/sda 1\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    vals = await rt.fill_free_params(rt.RUN_IN_VM_VIA_AGENT, ADD84, "brief")
    assert vals["VERIFY_INSIDE"] == "df -h / | awk '{print \\$5}'"
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, rt.values_for(rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), ENV, vals))
    verify = sr.verify_commands(rb)
    assert _what_the_guest_gets(verify[0]) == "df -h / | awk '{print $5}'"
    frame = _frame(rb, ADD84)
    assert frame["verify"] == verify + ["qm guest exec 106 -- bash -c 'df -h'"], "§17.1630: and ADD84's own check"


def test_a_public_key_or_a_shell_variable_never_becomes_a_secret_file():
    airvpn = {"description": "write the stored AirVPN config ($AIRVPN_WG_CONF, never as a literal) to /etc/wireguard/wg0.conf"}
    assert rt.secret_files_from_text(airvpn) == "/etc/wireguard/wg0.conf AIRVPN_WG_CONF"
    for prose in ("Append the panel's public key ($PANEL_PUBKEY) to /root/.ssh/authorized_keys in CT 120",
                  "copy $HOME/.ssh/id_rsa.pub to /root/.ssh/authorized_keys",
                  "copy ${HOME}/.ssh/id_rsa.pub to /root/.ssh/authorized_keys"):
        assert rt.secret_files_from_text({"description": prose}) == "", prose
    writes, refs = rt.secret_file_parts("/etc/wireguard/wg0.conf AIRVPN_WG_CONF\n/etc/x A")
    assert "/etc/x" not in writes, "a one-letter name is not a secret reference"
    assert refs == " # $AIRVPN_WG_CONF"


def test_the_write_refuses_an_empty_value_instead_of_emptying_the_file():
    """The value the runner did not inject expands empty in the root shell; `cat >` would truncate."""
    writes, _ = rt.secret_file_parts("/etc/wireguard/wg0.conf AIRVPN_WG_CONF")
    guard, write = writes.strip("\n").split("\n")
    assert guard.strip().startswith('[ -n "${AIRVPN_WG_CONF:-}" ] ||') and "holds no AIRVPN_WG_CONF" in guard
    assert "cat > /etc/wireguard/wg0.conf" in write
    r = subprocess.run(["bash", "-c", guard.strip()], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 1 and "FAILED: the runner holds no AIRVPN_WG_CONF" in r.stdout
    r = subprocess.run(["bash", "-c", guard.strip()], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "AIRVPN_WG_CONF": "[Interface]"})
    assert r.returncode == 0


INV = {"names": {"102": "prowlarr", "103": "radarr", "104": "sonarr", "111": "control-panel"}}
IN_GUEST_QUOTED = ("pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: <RADARR_API_KEY>\" "
                   "http://127.0.0.1:7878/api/v3/rootfolder'")
IN_GUEST_BARE = 'pct exec 103 -- curl -s -H "X-Api-Key: <RADARR_API_KEY>" http://127.0.0.1:7878/api/v3/rootfolder'
IN_GUEST_DQ = 'pct exec 103 -- sh -c "curl -s -H \'X-Api-Key: <RADARR_API_KEY>\' http://127.0.0.1:7878/api/v3/rootfolder"'


def test_the_read_is_pasted_only_where_the_guest_shell_expands_it():
    cmds, _v, notes = mv.read_on_the_machine([IN_GUEST_QUOTED], [], INV)
    assert "<RADARR_API_KEY>" not in cmds[0] and "$(" in cmds[0]
    assert len(notes) == 1 and "inside guest 103" in notes[0]["why"]
    for bad in (IN_GUEST_BARE, IN_GUEST_DQ):
        cmds, _v, notes = mv.read_on_the_machine([bad], [], INV)
        assert cmds == [bad], "left as an input: the host shell would run the read"
        assert len(notes) == 1 and "single-quoted" in notes[0]["why"] and "pct exec 103 -- sh -c" in notes[0]["why"]


def test_in_single_quotes_walks_the_quote_state_like_the_host_shell():
    assert mv.in_single_quotes(IN_GUEST_QUOTED, "<RADARR_API_KEY>") is True
    assert mv.in_single_quotes(IN_GUEST_BARE, "<RADARR_API_KEY>") is False
    assert mv.in_single_quotes(IN_GUEST_DQ, "<RADARR_API_KEY>") is False, "a ' inside \"…\" is a character"
    assert mv.in_single_quotes("sh -c 'a \\' <K>'", "<K>") is False, "a backslash inside '…' escapes nothing: ' closed"
    assert mv.in_single_quotes("sh -c \"a\" 'b <K>'", "<K>") is True
