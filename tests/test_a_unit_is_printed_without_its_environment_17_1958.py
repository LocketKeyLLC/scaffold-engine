"""§17.1958 — a read that prints a whole systemd unit prints the keys in its Environment= lines."""
from app.modules.prints_a_secret import prints_a_units_environment

# the live T4 shape (job 7cc2a7ac "Create restore point")
_T4 = ["pct exec 111 -- systemctl cat control-panel.service", "mkdir -p /root/panel-backup-<BACKUP_DATE>"]
_T4_VERIFY = ["ls -la /root/panel-backup-<BACKUP_DATE>",
              "cat /root/panel-backup-<BACKUP_DATE>/etc/systemd/system/control-panel.service"]


def test_the_live_backup_check_is_refused_for_both_prints():
    hits = prints_a_units_environment(_T4, _T4_VERIFY)
    assert [h["command"] for h in hits] == [_T4[0], _T4_VERIFY[1]]
    assert "systemctl show" in hits[0]["why"]


def test_a_head_of_the_unit_still_prints_it():
    assert prints_a_units_environment(["pct exec 111 -- bash -c 'systemctl cat control-panel.service | head -20'"], [])


def test_a_drop_in_file_is_a_unit_too():
    assert prints_a_units_environment(["cat /etc/systemd/system/control-panel.service.d/override.conf"], [])


def test_reads_that_keep_the_environment_out_pass():
    ok = ["systemctl cat control-panel.service | grep -v '^Environment'",
          "systemctl cat control-panel.service | grep -c Environment",
          "systemctl cat control-panel.service | grep -E '^(ExecStart|WorkingDirectory)='",
          "systemctl show control-panel.service -p ExecStart -p WorkingDirectory",
          "cmp /etc/systemd/system/control-panel.service /root/b/etc/systemd/system/control-panel.service",
          "systemctl is-active control-panel.service",
          "cat /etc/caddy/Caddyfile"]
    assert prints_a_units_environment(ok, []) == []


def test_choosing_the_environment_lines_is_still_printing_them():
    assert prints_a_units_environment(["systemctl cat control-panel.service | grep Environment"], [])


def test_the_frame_gate_runs_it():
    import inspect
    from app.modules import supervised_runs
    assert "_prints_a_units_environment(cmds, verify)" in inspect.getsource(supervised_runs)
