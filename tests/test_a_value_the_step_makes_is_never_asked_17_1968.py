"""§17.1968 — a `<NAME>` whose value the step's own commands produce is not an input to ask.

Live (job 7cc2a7ac T4 "Create restore point", 2026-11-20): the first command made a backup folder named
`$(date -u +%Y%m%dT%H%M%SZ)` and the rest wrote into `…-<TIMESTAMP>/`; the frame asked "It needs 2 values
from you first". The fixtures below are that block's shape with made-up names.
"""
from __future__ import annotations

from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "sudo": True, "secrets": [], "can_write_files": True}
NODE = {"node_key": "R4", "title": "Create restore point",
        "description": "Take a restorable copy of the site directory on the host before any change."}

LIVE_CMDS = [
    "install -d -m 0700 /root/site-backup-$(date -u +%Y%m%dT%H%M%SZ) && ls -ld /root/site-backup-*",
    "pct exec 214 -- tar -C <SITE_DIR> -czf - . | tee /root/site-backup-<TIMESTAMP>/site-dir.tar.gz | wc -c",
    "tar -tzf /root/site-backup-<TIMESTAMP>/site-dir.tar.gz | head -20",
]
LIVE_VERIFY = ["ls -ld /root/site-backup-*", "wc -c /root/site-backup-<TIMESTAMP>/site-dir.tar.gz"]


def _runbook(cmds, verify):
    return ("## Run this\n\n```bash\n" + "\n".join(cmds) + "\n```\n\n## Verify\n\n"
            + "\n".join(f"- check: `{v}`" for v in verify) + "\n")


def _frame(cmds, verify):
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://10.9.8.7:8790/mcp/"})()
    return sr.frame_run(NODE, _runbook(cmds, verify), spec, POLICY, env={"profile": "root@pve"},
                        inventory={"cts": {"214": "running"}, "vms": {}, "names": {"214": "site"}})


def test_the_live_t4_timestamp_is_made_by_the_block():
    hits = sr.values_the_block_makes(LIVE_CMDS, LIVE_VERIFY)
    assert [h["name"] for h in hits] == ["TIMESTAMP"], hits
    assert "`/root/site-backup-$(date -u +%Y%m%dT%H%M%SZ)` makes it" in hits[0]["why"] and"is a value this block makes itself" in hits[0]["why"]


def test_the_frame_never_asks_for_it_and_the_redraft_is_told():
    frame = _frame(LIVE_CMDS, LIVE_VERIFY)
    names = [i["name"] for i in frame["inputs"]]
    assert "TIMESTAMP" not in names, names
    assert "SITE_DIR" in names, "a real operator input is still asked"
    assert any("TIMESTAMP" in r["why"] for r in frame["refused"])
    assert "needs 2 values" not in frame["question"]
    assert sr.shape_retry_note(frame), "the refusal reaches the §17.1196 redraft"
    # refused once, not as a `<TIMESTAMP>` "redirect" on every command
    assert sum("redirect" in r["why"] for r in frame["refused"]) == 0, [r["why"][:80] for r in frame["refused"]]


def test_an_assigned_name_is_made_by_the_block():
    hits = sr.values_the_block_makes(['STAMP=$(date +%s) && mkdir /var/tmp/snap-"$STAMP"',
                                      "ls /var/tmp/snap-<STAMP>"], [])
    assert [h["name"] for h in hits] == ["STAMP"]
    assert [h["name"] for h in sr.values_the_block_makes(["read -r NODE_ID < /tmp/id; echo <NODE_ID>"], [])] == ["NODE_ID"]


def test_a_position_filled_by_a_variable_the_block_assigns():
    hits = sr.values_the_block_makes(['D=$(mktemp -d) && cp a /srv/drop-$D/', "ls /srv/drop-<DROP>/"], [])
    assert [h["name"] for h in hits] == ["DROP"]


def test_a_real_operator_input_is_still_asked():
    # the operator's value handed IN by an assignment makes nothing
    assert sr.values_the_block_makes(['TITLE="<TITLE>" python3 /tmp/find.py'], []) == []
    # a value nothing in the block produces
    assert sr.values_the_block_makes(["tar -C <SITE_DIR> -czf /root/site-backup.tar.gz ."], []) == []
    # a runner secret at the same position is not made by the block
    assert sr.values_the_block_makes(['curl -H "X-Key: $SITE_KEY" http://10.9.8.7/api?key=$SITE_KEY',
                                      "curl http://10.9.8.7/api?key=<SITE_KEY>"], []) == []
    # a bare `/root/` or `http://` before both is no shared position
    assert sr.values_the_block_makes(["ls /root/$(hostname)", "ls /root/<BACKUP_NAME>"], []) == []
    assert sr.values_the_block_makes(["curl -s http://$(cat /tmp/ip)/", "curl -s http://<HOST_IP>/"], []) == []
    frame = _frame(["tar -C <SITE_DIR> -czf /root/site-backup.tar.gz ."], ["tar -tzf /root/site-backup.tar.gz"])
    assert [i["name"] for i in frame["inputs"]] == ["SITE_DIR"]
    assert not any("makes itself" in r["why"] for r in frame["refused"])


def test_a_named_allow_list_judges_the_shape_not_the_placeholder():
    # §17.1187 — the gate reads a bare `<NAME>` as a redirect; the refused name gets a dummy like any input
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://10.9.8.7:8790/mcp/"})()
    policy = {**POLICY, "allow": ["install", "pct", "tar", "ls", "wc"]}
    frame = sr.frame_run(NODE, _runbook(LIVE_CMDS, LIVE_VERIFY), spec, policy, env={"profile": "root@pve"},
                         inventory={"cts": {"214": "running"}, "vms": {}, "names": {"214": "site"}})
    assert not [r for r in frame["refused"] if "redirect" in r["why"]], [r["why"][:80] for r in frame["refused"]]
    assert sum("makes itself" in r["why"] for r in frame["refused"]) == 1


# §17.1968b — a value another command of the SAME block reads off the machine is not asked either
READ_CMDS = [
    "pct exec 214 -- systemctl show web-panel.service -p ExecStart -p WorkingDirectory --value",
    "pct exec 214 -- tar -C <PANEL_DIR> -czf - . | tee /root/site-backup/site-dir.tar.gz | wc -c",
]


def test_the_live_t4_panel_dir_is_read_by_the_block():
    hits = sr.reads_a_value_and_asks_for_it(READ_CMDS, [])
    assert [h["name"] for h in hits] == ["PANEL_DIR"], hits
    why = hits[0]["why"]
    assert "reads this value itself and asks the operator for it" in why
    # the remedy reads ONE property, in the command that uses it, in the guest it lives in
    assert "pct exec 214 -- systemctl show web-panel.service -p WorkingDirectory --value" in why


def test_the_frame_does_not_ask_for_a_read_value_and_the_redraft_is_told():
    frame = _frame(LIVE_CMDS[:1] + READ_CMDS + LIVE_CMDS[1:], LIVE_VERIFY)
    names = [i["name"] for i in frame["inputs"]]
    assert "PANEL_DIR" not in names and "TIMESTAMP" not in names, names
    hits = [r for r in frame["refused"] if "reads this value itself" in r["why"]]
    assert len(hits) == 1 and sr.shape_retry_note({"kind": "run", "refused": hits})


def test_a_read_of_another_unit_or_property_is_no_hit():
    # another unit: `<PANEL_DIR>` is not what caddy's WorkingDirectory says
    assert sr.reads_a_value_and_asks_for_it(
        ["pct exec 220 -- systemctl show caddy.service -p WorkingDirectory --value", "tar -C <PANEL_DIR> -czf x ."],
        []) == []
    # another property: ExecStart prints no directory
    assert sr.reads_a_value_and_asks_for_it(
        ["systemctl show web-panel.service -p ExecStart --value", "tar -C <PANEL_DIR> -czf x ."], []) == []
    # a config path is not a working directory
    assert sr.reads_a_value_and_asks_for_it(
        ["systemctl show caddy.service -p WorkingDirectory --value", "cat <CADDY_CONFIG_PATH>"], []) == []
    # no read at all: a real input
    assert sr.reads_a_value_and_asks_for_it(["tar -C <PANEL_DIR> -czf x ."], []) == []
    # the read that already uses the placeholder itself reads nothing for it
    assert sr.reads_a_value_and_asks_for_it(
        ["systemctl show <PANEL_UNIT> -p WorkingDirectory --value"], []) == []
