"""§17.1689 — a curl that only logs in is a read in a step that declares it changes nothing."""
from app.modules import supervised_runs as sr

NODE = {"title": "Login qBittorrent list torrents",
        "description": "Read-only: authenticate with stored credentials and list all torrents. No changes."}

# live, job e5e22b92 T1 (2026-10-20)
LOGIN = ("pct exec 105 -- sh -c 'curl -s -c /tmp/qb_cookies.txt -H \"Referer: http://127.0.0.1:8080\" --data "
         "\"username=admin&password='\"$MASS_PASSWORD\"'\" http://127.0.0.1:8080/api/v2/auth/login'")
LIST = "pct exec 105 -- sh -c 'curl -s -b /tmp/qb_cookies.txt \"http://127.0.0.1:8080/api/v2/torrents/info\"'"


def test_the_live_login_and_list_pass_the_read_only_gate():
    assert sr.writing_segments(LOGIN), "precondition: the login is a write by shape"
    assert sr.writes_in_a_read_only_step([LOGIN, LIST], NODE) == []


def test_a_bare_login_is_a_read():
    assert sr.a_login_read('curl -s -c /tmp/c --data "username=admin&password=x" http://127.0.0.1:8080/api/v2/auth/login')


def test_a_change_is_still_refused():
    for cmd in (
        "pct exec 105 -- sh -c 'curl -s -b /tmp/c --data \"hashes=all\" http://127.0.0.1:8080/api/v2/torrents/delete'",
        "pct exec 105 -- sh -c 'curl -s --data x http://127.0.0.1:8080/api/v2/auth/login; rm -rf /var/lib/qbit'",
        "pct exec 105 -- sh -c 'curl -s --data x http://127.0.0.1:8080/api/v2/auth/login && systemctl stop qbittorrent'",
        "pct exec 105 -- sh -c 'curl -s -c /etc/qb.cookies --data x http://127.0.0.1:8080/api/v2/auth/login'",
        "pct exec 105 -- sh -c 'curl -s -X DELETE http://127.0.0.1:8080/api/v2/auth/login'",
        "pct exec 105 -- sh -c 'curl -s --data x http://127.0.0.1:8080/api/v2/auth/login http://127.0.0.1:8080/api/v2/app/setPreferences'",
    ):
        assert not sr.a_login_read(cmd), cmd
        assert sr.writes_in_a_read_only_step([cmd], NODE), cmd
