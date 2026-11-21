"""§17.1970 — a template's in-guest body is judged by the print-a-secret gates (§17.1652, §17.1673, §17.1958) like a
command: its lines are what runs inside the guest, and the template's commands are only `bash /tmp/in_ct_N.sh …`."""
import re
from unittest.mock import MagicMock

import pytest

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr
from app.modules.prints_a_secret import (in_guest_lines, prints_a_credential, prints_a_units_environment,
                                         prints_arr_settings)

_TEMPLATES = (rt.RUN_IN_CONTAINER, rt.RUN_IN_VM_VIA_AGENT, rt.REACH_VM_SSH_AND_RUN)


def _rendered(template, remote: str) -> tuple[list[str], list[dict]]:
    """The template's commands and files as the engine renders them (production frame, the drafter's body inside)."""
    vals = {"GID": "111", "STEP": "T4", "REMOTE_COMMANDS": remote, "VERIFY_INSIDE": "true", "GUEST_USER_NAME": "AI_USER"}
    rb = rt.render(template, vals)
    files = [{"path": m.group(1), "content": m.group(2) + "\n"}
             for m in re.finditer(r"^### (\S+)\n```bash\n(.*?)\n```\n", rb, re.S | re.M)]
    run = re.search(r"## Run this\n\n```bash\n(.*?)\n```", rb, re.S).group(1).splitlines()
    assert files and "<<'REMOTE'" in files[0]["content"]
    return run, files


# the live T4 line (job 7cc2a7ac "Create restore point"), written where most in-guest work is written: the body
_T4_BODY = "mkdir -p /root/panel-backup-20261120\nsystemctl cat control-panel.service\ncp -a /opt/control-panel /root/panel-backup-20261120/"


@pytest.mark.parametrize("template", _TEMPLATES, ids=lambda t: t.name)
def test_a_unit_printed_inside_any_guest_template_is_refused(template):
    cmds, files = _rendered(template, _T4_BODY)
    assert prints_a_units_environment(cmds, []) == []          # what the frame judged before: the wrapper alone
    hits = prints_a_units_environment(cmds, [], files)
    assert [h["command"] for h in hits] == ["in-guest body: systemctl cat control-panel.service"]


@pytest.mark.parametrize("template", _TEMPLATES, ids=lambda t: t.name)
def test_the_engines_own_frame_is_not_judged(template):
    cmds, files = _rendered(template, "systemctl is-active control-panel.service")
    assert prints_a_units_environment(cmds, [], files) == []
    assert prints_a_credential(cmds, [], files) == []
    assert prints_arr_settings(cmds, [], files) == []


def test_a_credential_printed_inside_the_body_is_refused_and_one_used_is_not():
    cmds, files = _rendered(rt.RUN_IN_CONTAINER, "grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/prowlarr/config.xml")
    assert prints_a_credential(cmds, [], files)
    used = ('curl -s -H "X-Api-Key: $(grep -oP \'(?<=<ApiKey>)[^<]+\' /var/lib/sonarr/config.xml)" '
            '"http://127.0.0.1:8989/api/v3/series" -o /dev/null -w "%{http_code}"')
    cmds, files = _rendered(rt.RUN_IN_CONTAINER, used)
    assert prints_a_credential(cmds, [], files) == []


def test_an_arr_settings_list_printed_inside_the_body_is_refused():
    key = "$(sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' /var/lib/prowlarr/config.xml)"
    cmds, files = _rendered(rt.RUN_IN_CONTAINER, f'curl -s -H "X-Api-Key: {key}" http://127.0.0.1:9696/api/v1/indexer')
    assert prints_arr_settings(cmds, [], files)
    cmds, files = _rendered(rt.RUN_IN_CONTAINER, f'curl -s -H "X-Api-Key: {key}" http://127.0.0.1:9696/api/v1/indexerstatus')
    assert prints_arr_settings(cmds, [], files) == []


def test_a_unit_the_body_writes_is_not_a_unit_it_prints():
    # the stored bodies' own shape (job 55f68b7f ADD114, the SteamCMD template): the unit is WRITTEN, its
    # Environment lines are heredoc data, and nothing is printed
    body = "\n".join([
        "cat > /etc/systemd/system/control-panel.service <<'EOF'",
        "[Service]", "Environment=PANEL_TOKEN=made-up", "ExecStart=/usr/bin/node /opt/panel/server.js", "EOF",
        "cat >/etc/systemd/system/palworld.service <<EOF", "[Service]", "ExecStart=/opt/pal/start.sh", "EOF",
        "systemctl daemon-reload", "systemctl enable --now control-panel.service",
        "python3 - <<'PY'", "print(open('/var/lib/sonarr/config.xml').read().count('<ApiKey>'))", "PY",
    ])
    cmds, files = _rendered(rt.RUN_IN_CONTAINER, body)
    assert prints_a_units_environment(cmds, [], files) == []
    assert prints_a_credential(cmds, [], files) == []
    assert not any("Environment=" in ln or "print(" in ln for ln in in_guest_lines(files))


def test_a_continued_line_is_one_line():
    files = [{"path": "/tmp/in_ct_111.sh", "content": "cat > /tmp/r.sh <<'REMOTE'\nsystemctl \\\n  cat control-panel.service\nREMOTE\n"}]
    assert prints_a_units_environment([], [], files)


def test_a_count_flag_takes_its_number_and_a_redirect_is_a_write():
    assert prints_a_units_environment(["head -n 5 /etc/systemd/system/control-panel.service"], [])
    assert prints_a_units_environment(["cat >/etc/systemd/system/control-panel.service <<'EOF'"], []) == []


def test_frame_run_refuses_the_body():
    vals = {"GID": "111", "STEP": "T4", "REMOTE_COMMANDS": _T4_BODY, "VERIFY_INSIDE": "test -d /root/panel-backup-20261120",
            "GUEST_USER_NAME": "GUEST_USER"}
    rb = rt.render(rt.RUN_IN_CONTAINER, vals)
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    f = sr.frame_run({"node_key": "T4", "title": "Create restore point", "tool": "Shell",
                      "prompt_template": "Back up the control panel in CT 111."}, rb, spec, policy, services=[],
                     inventory={"vms": {}, "cts": {"111": "running"}, "names": {"111": "panel"}})
    assert [r for r in f["refused"] if r["command"] == "in-guest body: systemctl cat control-panel.service"], f["refused"]
