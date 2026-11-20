"""§17.1956 — the in-guest body of a step that only READS installs nothing and starts or stops nothing."""
from app.modules.runbook_templates import APT_REPAIR_PRELUDE
from app.modules.supervised_runs import writes_in_a_read_only_step

_RUNBOOK = "<!-- runbook-template: reach_ct_and_run -->\n"
_CMDS = ["bash /tmp/in_ct_120.sh start", "bash /tmp/in_ct_120.sh wait", "bash /tmp/in_ct_120.sh last"]


def _files(body: str) -> list[dict]:
    return [{"path": "/tmp/in_ct_120.sh", "content": (
        "#!/usr/bin/env bash\nGID=120\ncat > /tmp/in_ct_120_remote.sh <<'REMOTE'\nset -e\n" + body + "\nREMOTE\n"
        "pct push \"$GID\" /tmp/in_ct_120_remote.sh /root/.scaffold_step.sh\n"
        "ginfo \"systemctl stop $UNIT 2>/dev/null; systemd-run --unit $UNIT bash /root/.scaffold_step.sh\"\n")}]


_VERIFY = {"title": "Verify the Caddy vhost on CT 120",
           "prompt_template": "Verify: the Caddy vhost on CT 120. (split from T2: Verify network reachability)"}
_READS = "\n".join([
    "systemctl is-active caddy.service",
    "ss -tlnp | grep -E ':(80|443)\\b' || true",
    "for f in /etc/caddy/Caddyfile; do",
    "  if [ -f \"$f\" ]; then echo \"=== $f ===\"; fi",
    "done",
    "curl -s -o /dev/null -w 'code:%{http_code}\\n' http://127.0.0.1:2019/config/ || true",
    "journalctl -u caddy.service --no-pager -n 40 2>/dev/null || true",
])


def test_a_check_that_installs_its_tools_is_refused():
    body = "\n".join(["export DEBIAN_FRONTEND=noninteractive", *APT_REPAIR_PRELUDE,
                      "DEBIAN_FRONTEND=noninteractive apt-get install -y curl jq", _READS])
    hits = writes_in_a_read_only_step(_CMDS, _VERIFY, _RUNBOOK, _files(body))
    assert len(hits) == 1 and "apt-get install -y curl jq" in hits[0]["command"]
    assert "not installed" in hits[0]["why"]


def test_a_check_that_restarts_its_service_is_refused():
    hits = writes_in_a_read_only_step(_CMDS, _VERIFY, _RUNBOOK, _files("systemctl restart caddy\n" + _READS))
    assert hits and "systemctl restart caddy" in hits[0]["command"]


def test_a_check_that_only_reads_passes_and_the_template_frame_is_not_judged():
    # the frame's own `systemctl stop $UNIT` / `systemd-run` lines are the engine's, outside the body
    assert writes_in_a_read_only_step(_CMDS, _VERIFY, _RUNBOOK, _files(_READS)) == []


def test_the_engine_prelude_alone_is_not_the_drafters_write():
    assert writes_in_a_read_only_step(_CMDS, _VERIFY, _RUNBOOK, _files("\n".join([*APT_REPAIR_PRELUDE, _READS]))) == []


def test_a_step_that_changes_things_may_install():
    node = {"title": "Install Playwright toolchain", "prompt_template": "npm install playwright + browser binaries"}
    assert writes_in_a_read_only_step(_CMDS, node, _RUNBOOK, _files("apt-get install -y nodejs npm\n" + _READS)) == []


def test_a_step_that_says_read_only_is_judged_inside_its_template_too():
    # before §17.1956 the template's frame exempted a declared read-only step's body whole
    node = {"title": "CT 120: read Caddy's config", "prompt_template": "Read only, change nothing. Print the vhosts."}
    hits = writes_in_a_read_only_step(_CMDS, node, _RUNBOOK, _files("apt install -y jq\n" + _READS))
    assert hits and "apt install -y jq" in hits[0]["command"]
