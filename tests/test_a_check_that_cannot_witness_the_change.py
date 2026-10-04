r"""§17.1365 — a check that could not have failed is not evidence.

Live, 2026-10-04. ADD132 — *"Make Radarr and Sonarr actually drive qBittorrent,
and prove the connection"* — was recorded:

    ADD132 | done | already met: 3 verify check(s) read off pve-runner before the
                    run confirm the goal

§17.1302 reads a step's OWN checks off the machine before the block is parked,
and when every one is `confirmed` it records the step done and runs nothing. That
is sound only if a check could have **failed**. ADD132's checks were

    pct exec 103 -- curl … http://127.0.0.1:7878/api/v3/downloadclient
    pct exec 104 -- curl … http://127.0.0.1:8989/api/v3/downloadclient

— a list that held a `qBittorrent` entry **before** the step and after it.
Measured afterwards:

    qBittorrent.conf WebUI keys:  WebUI\Port — and nothing else
    qBittorrent /api/v2/app/version from off the box:  403
    Radarr health / Sonarr health:  no download-client failure (nothing had tried)

The work — writing `WebUI\Username` and `WebUI\Password_PBKDF2` — never happened,
no check read either key, and the goal was recorded met. The operator's media
chain is still broken behind a green step.

So: when a block writes named settings and no check names one of them, the checks
cannot witness the change and a pre-pass is not evidence. A block that writes no
settings is untouched — ADD84's `growpart` + `resize2fs`, confirmed by `df -h /`,
is exactly what §17.1302 is for.

Measured over the job's own history: 24 records carry checks, 1 writes named
settings, **0** would be newly blocked. The first cut of the rule flagged **six**
— for `systemd-run -p StandardOutput=append:/root/…`, `ssh -o BatchMode=yes`,
`SSHPASS=… sshpass -e` and `>/dev/null` — none of which is a setting anyone reads
back, which is why the detector insists the key be written into a real file.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (checks_can_witness_the_change,
                                         keys_the_block_writes)

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_host_var_in_guest_payload_2026_10_04.json").read_text())


# ------------------------------------------------- what the block writes


def test_the_live_block_writes_the_two_webui_keys():
    keys = keys_the_block_writes(FRAME["commands"], FRAME["files"])
    assert len(keys) == 2, keys
    assert all("WebUI" in k for k in keys)
    assert any("Username" in k for k in keys) and any("Password_PBKDF2" in k for k in keys)


def test_a_command_option_is_not_a_setting():
    """The first cut flagged six real blocks for these."""
    for line in ("ginfo \"systemd-run --unit U -p StandardOutput=append:/root/.log bash /root/x.sh\" >/dev/null",
                 "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new u@h true",
                 "SSHPASS=\"$MASS_PASSWORD\" sshpass -e ssh-copy-id u@h",
                 "pct exec 105 -- systemctl is-active x > /dev/null"):
        assert keys_the_block_writes([line]) == [], line


def test_a_line_that_writes_nowhere_is_not_a_write():
    assert keys_the_block_writes(["curl -d 'name=qBittorrent' http://h:7878/api/v3/x"]) == []
    assert keys_the_block_writes(["# WebUI\\Username=admin"]) == []


def test_every_way_of_modifying_an_existing_config_counts():
    for line in ('printf "%s\\n" "Session\\\\DefaultSavePath=/media" >> /etc/x.conf',
                 'echo "Key=1" >> /etc/x.conf',
                 'sed -i "s|^Key=.*|Key=1|" /etc/x.conf',
                 "sed -i '/^\\[Preferences\\]/a Key=1' /etc/x.conf",
                 'tee -a /etc/x.conf <<EOF\nKey=1\nEOF'):
        assert keys_the_block_writes([line]), line


def test_writing_a_whole_new_file_is_witnessed_by_its_effect():
    """A block that creates a unit file and checks `systemctl is-active` has a
    perfectly good witness: the service cannot run unless the file was written.
    Measured, this distinction is the difference between flagging ADD132 alone
    and flagging T23, T24 and ADD114 as well — each writes `Description=`,
    `After=`, `Type=` into a NEW unit and proves it with `is-active`."""
    unit = ('tee /etc/systemd/system/palworld.service <<EOF\n'
            '[Unit]\nDescription=PalWorld\nAfter=network.target\n'
            '[Service]\nType=simple\nUser=steam\nEOF')
    assert keys_the_block_writes([unit]) == []
    assert checks_can_witness_the_change(
        [unit, "systemctl enable --now palworld"],
        ["systemctl is-active palworld"], []) == ""


# ------------------------------------------------------- the judgment


def test_the_live_frame_cannot_witness_its_own_change():
    why = checks_can_witness_the_change(FRAME["commands"], FRAME["verify"], FRAME["files"])
    assert why, "the checks only GET a list that was already non-empty"
    assert "no check reads any of them back" in why
    assert "WebUI" in why


def test_a_check_that_reads_the_key_back_is_enough():
    verify = ["pct exec 105 -- grep -E '^WebUI.Username=' "
              "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf"]
    assert checks_can_witness_the_change(FRAME["commands"], verify, FRAME["files"]) == ""


def test_a_block_that_writes_no_settings_is_left_to_17_1302():
    """ADD84: `growpart` + `resize2fs`, confirmed by `df -h /`. The case the
    already-met shortcut exists for."""
    assert checks_can_witness_the_change(
        ["qm guest exec 106 -- growpart /dev/sda 1", "qm guest exec 106 -- resize2fs /dev/sda1"],
        ["qm guest exec 106 -- df -h /"], []) == ""


def test_the_escaped_and_plain_spellings_of_a_key_both_satisfy_it():
    r"""The block writes `WebUI\\Username` through two quoting layers; a check
    greps `WebUI.Username` or `WebUI\Username`. Either must count."""
    for check in ("grep '^WebUI\\\\Username=' /x.conf",
                  "grep -E '^WebUI.Username=' /x.conf",
                  "grep 'WebUIUsername' /x.conf"):
        assert checks_can_witness_the_change(
            FRAME["commands"], [check], FRAME["files"]) == "", check


# ---------------------------------------------------------- the lane


def test_already_met_declines_when_the_checks_are_blind():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.already_met)
    assert "checks_can_witness_the_change(" in src
    assert "supervised_run_already_met_blind" in src
    # and it declines BEFORE spending a read on the runner
    assert src.index("checks_can_witness_the_change(") < src.index("run_verify(")


def test_the_already_met_record_still_names_its_evidence():
    """The shortcut is narrowed, not removed: §17.1302's own case is intact."""
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.already_met)
    assert "all(k == \"confirmed\" for k in kinds)" in src
