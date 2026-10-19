"""§17.1659 — a check step does not change what it checks (job 5db1b088 T18 POSTed setPreferences)."""
from __future__ import annotations

from app.modules.check_writes import a_check_that_writes

T18 = {"title": "Verify qBittorrent's network interface is wg0"}
LIVE = ["pct exec 105 -- sh -c 'curl -s --fail-with-body -c /tmp/qb -H \"Referer: http://127.0.0.1:8080\" --data "
        "\"username=admin&password='\"$MASS_PASSWORD\"'\" http://127.0.0.1:8080/api/v2/auth/login'",
        "pct exec 105 -- sh -c 'curl -s --fail-with-body -b /tmp/qb -H \"Content-Type: application/json\" --data "
        "\"{\\\"outgoing_interface\\\":\\\"wg0\\\"}\" http://127.0.0.1:8080/api/v2/app/setPreferences'"]


def test_the_live_verify_that_writes_is_refused():
    out = a_check_that_writes(LIVE, T18)
    assert len(out) == 1 and "setPreferences" in out[0]["command"]


def test_tests_logins_reads_and_non_checks_pass():
    test_call = ["pct exec 103 -- sh -c 'curl -s -X POST -d @/tmp/dc.json http://127.0.0.1:7878/api/v3/downloadclient/test'"]
    read = ["pct exec 103 -- curl -s http://127.0.0.1:7878/api/v3/downloadclient"]
    assert a_check_that_writes(LIVE[:1] + test_call + read, T18) == []
    assert a_check_that_writes(LIVE, {"title": "Set qBittorrent's network interface to wg0"}) == []
