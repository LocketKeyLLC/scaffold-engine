"""§17.1489 — a file whose content is a stored secret has a place in the container template.

Live (2026-10-11): ADD160 (AirVPN WireGuard for CT 105). `run_in_container` took only commands to run INSIDE the
guest, where a stored secret never arrives; refused for reading `$AIRVPN_WG_CONF` there (§17.1488), the drafter
dropped the config, the tunnel and the kill-switch twice and drafted only `apt-get install`.
"""
import re
import subprocess

from app.modules import runbook_templates as rt

VALS = {"GID": "105", "STEP": "ADD160", "REMOTE_COMMANDS": "wg-quick up wg0", "VERIFY_INSIDE": "wg show",
        "GUEST_USER_NAME": "GUEST_USER"}


def _rendered(secret_files: str) -> str:
    return rt.render(rt.RUN_IN_CONTAINER, {**VALS, "SECRET_FILES": secret_files})


def _helper(rb: str) -> str:
    return re.search(r"### /tmp/in_ct_105\.sh\n```bash\n([\s\S]*?)\n```", rb).group(1)


def test_a_secret_file_is_written_from_the_host_and_named_on_the_start_command():
    rb = _rendered("/etc/wireguard/wg0.conf AIRVPN_WG_CONF")
    h = _helper(rb)
    assert ("pct exec \"$GID\" -- sh -c 'umask 077; mkdir -p /etc/wireguard; cat > /etc/wireguard/wg0.conf' "
            "<<< \"$AIRVPN_WG_CONF\"") in h
    assert h.index("<<< \"$AIRVPN_WG_CONF\"") < h.index("pct push"), "written before the commands are pushed"
    assert "bash /tmp/in_ct_105.sh start # $AIRVPN_WG_CONF" in rb
    assert rb.count("$AIRVPN_WG_CONF") == 2, "the value is never in the block — only the reference"


def test_no_secret_file_changes_nothing():
    rb = _rendered("")
    assert "bash /tmp/in_ct_105.sh start\n" in rb and "umask 077" not in rb


def test_a_malformed_line_is_dropped():
    w, refs = rt.secret_file_parts("relative/path NAME\n/etc/x lowercase\n/etc/ok.conf GOOD_NAME")
    assert w.count("cat >") == 1 and refs == " # $GOOD_NAME"


def test_the_rendered_helper_parses_and_the_value_crosses_on_stdin(tmp_path):
    h = _helper(_rendered("/etc/wireguard/wg0.conf AIRVPN_WG_CONF"))
    script = tmp_path / "h.sh"
    script.write_text(h)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    # the write line alone, with `pct exec … --` standing in as a local `sh -c` and the path redirected
    line = next(ln for ln in h.splitlines() if "<<< \"$AIRVPN_WG_CONF\"" in ln)
    target = tmp_path / "wg0.conf"
    local = (line.replace('pct exec "$GID" -- ', "").replace("/etc/wireguard/wg0.conf", str(target))
             .replace("mkdir -p /etc/wireguard", f"mkdir -p {tmp_path}"))
    r = subprocess.run(["bash", "-c", local], env={"AIRVPN_WG_CONF": "[Interface]\nPrivateKey = x", "GID": "105",
                                                   "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert target.read_text() == "[Interface]\nPrivateKey = x\n" and oct(target.stat().st_mode & 0o777) == "0o600"


def test_the_secret_file_is_read_off_the_step_not_drawn():
    import json
    import pathlib
    node = json.loads(pathlib.Path("tests/fixtures/add160_node_2026_10_11.json").read_text())
    assert rt.secret_files_from_text(node) == "/etc/wireguard/wg0.conf AIRVPN_WG_CONF"
    assert rt.secret_files_from_text({"description": "Install Caddy in CT 120."}) == ""
    assert not any(p.name == "SECRET_FILES" and p.source == "model" for p in rt.RUN_IN_CONTAINER.params)
