"""§17.1490 — a failed in-guest run is reported as failed.

Live (2026-10-11): ADD160's in-container script died on an apt 404 (`set -e`), and the helper reported
"scaffold-ADD160 finished (exit 0)": the unit runs with `--collect`, systemd discards a FAILED unit, and
`systemctl show -p Result/-p ExecMainStatus` then answers the defaults — success, 0. Every failed in-guest run read
as a success; only the step's own checks caught this one. The helper now records the script's own exit code.

The test RUNS the rendered helper with pct/systemd-run/systemctl stubbed (a sandbox for /root).
"""
import re
import subprocess

import pytest

from app.modules import runbook_templates as rt

STUBS = r'''
pct() { case "$1" in
  status) echo "status: running";;
  push) cp "$3" "$ROOT/.scaffold_step.sh";;
  exec) shift 3; "$@";;
esac; }
systemd-run() { while [ "$1" != "bash" ]; do shift; done; ( "$@" ) >>"$ROOT/.scaffold_step.log" 2>>"$ROOT/.scaffold_step.err"; return 0; }
systemctl() { case "$1" in is-active) echo inactive;; show) case "$*" in *Result*) echo success;; *ExecMainStatus*) echo 0;; esac;; *) :;; esac; }   # a collected unit answers the defaults
export -f pct systemd-run systemctl
'''


def _helper(remote: str) -> str:
    rb = rt.render(rt.RUN_IN_CONTAINER, {"GID": "105", "STEP": "ADD160", "REMOTE_COMMANDS": remote,
                                         "VERIFY_INSIDE": "true", "GUEST_USER_NAME": "GUEST_USER"})
    return re.search(r"### /tmp/in_ct_105\.sh\n```bash\n([\s\S]*?)\n```", rb).group(1)


# §17.1608b — a block that only READS exits 0 by design (its checks judge it), so the failing block here CHANGES
# something (a redirect into a file) before it fails, as ADD160's apt install did.
@pytest.mark.parametrize("remote,ok,code", [("true", True, "0"), ('echo working > "$ROOT/w"; false', False, "1"),
                                            ("exit 100", False, "100"), ("echo working; false", True, "0")])
def test_the_helper_reports_the_scripts_own_exit(tmp_path, remote, ok, code):
    root = tmp_path / "root"
    root.mkdir()
    h = _helper(remote).replace("/root/", f"{root}/").replace("/tmp/in_ct_105_remote.sh", f"{tmp_path}/remote.sh")
    script = tmp_path / "helper.sh"
    script.write_text(STUBS + "\n" + h)
    r = subprocess.run(["bash", str(script), "start"], capture_output=True, text=True,
                       env={"ROOT": str(root), "PATH": "/usr/bin:/bin"}, timeout=60)
    out = r.stdout + r.stderr
    if ok:
        assert r.returncode == 0 and "finished (exit 0)" in out, out
    else:
        assert r.returncode == int(code) and f"FAILED: scaffold-ADD160 ended with exit={code}" in out, out


def test_both_templates_record_the_exit():
    for tpl in (rt.RUN_IN_CONTAINER, rt.RUN_IN_VM_VIA_AGENT):
        body = next(iter(tpl.files.values()))
        assert "echo \\$? > /root/.scaffold_step.rc" in body and 'cat /root/.scaffold_step.rc' in body
        assert "systemctl show -p Result --value" not in body
