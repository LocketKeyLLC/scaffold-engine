"""§17.1476 — a read of the BMC is a read; a BMC command that changes something is not.

Live (2026-10-11): ADD157 armed the BMC watchdog; its own check `ipmitool mc watchdog get` was not in the read-only
table, so `checks()` dropped it from the verify, and ADD158 (that read alone) was refused "this block has no check
at all". `ipmitool sel elist`, `sdr type …`, `chassis status` (ADD155's diagnosis) were not reads either.
"""
import pytest

from app.modules.assist_state_check import read_only_command


@pytest.mark.parametrize("cmd", [
    "ipmitool mc watchdog get", "ipmitool sel elist", "ipmitool sel list", "ipmitool sel info",
    "ipmitool sdr type Temperature", "ipmitool sdr type 'Power Supply'", "ipmitool sdr", "ipmitool sdr elist",
    "ipmitool chassis status", "ipmitool fru print", "ipmitool sensor list", "ipmitool mc info",
    "ras-mc-ctl --errors", "ras-mc-ctl --summary",
])
def test_reads(cmd):
    assert read_only_command(cmd) is True, cmd


@pytest.mark.parametrize("cmd", [
    "ipmitool chassis power off", "ipmitool chassis power cycle", "ipmitool mc reset cold", "ipmitool sel clear",
    "ipmitool mc watchdog reset", "ipmitool mc watchdog off", "ipmitool sdr fill sensors", "ipmitool raw 0x06 0x24",
    "ipmitool sel time set now", "ipmitool fru write 0 x.bin", "ipmitool user set password 2 x",
    "ipmitool -H 10.0.0.1 -P secret sel elist", "ipmitool chassis bootdev pxe", "ipmitool lan set 1 ipaddr 1.2.3.4",
    "ras-mc-ctl --register-labels",
])
def test_not_reads(cmd):
    assert read_only_command(cmd) is False, cmd


def test_the_step_check_now_survives():
    from app.modules.operator_steps import checks
    node = {"description": "… Done when `ipmitool mc watchdog get` shows the timer running and "
                           "`systemctl show -p RuntimeWatchdogUSec --value` shows 1min."}
    assert checks(node) == ["ipmitool mc watchdog get", "systemctl show -p RuntimeWatchdogUSec --value"]
