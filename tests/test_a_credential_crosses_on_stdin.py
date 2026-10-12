"""§17.1494 — the engine's own secret advice must not teach a leak or a nonsense file (live ADD170)."""
from app.modules import supervised_runs as sr


def _helper(remote: str) -> list[dict]:
    content = ("#!/bin/bash\ncat > /tmp/in_ct_105_remote.sh <<'REMOTE'\n" + remote + "\nREMOTE\n")
    return [{"path": "/tmp/in_ct_105.sh", "content": content}]


def test_a_login_password_inside_a_guest_is_sent_to_the_host_call():
    out = sr.secret_inside_a_guest_script(_helper(
        'curl -s -c /tmp/c --data-urlencode password@- http://127.0.0.1:8080/api/v2/auth/login <<< "$MASS_PASSWORD"'),
        {"MASS_PASSWORD"})
    assert len(out) == 1
    why = out[0]["why"]
    assert "credential" in why and "pct exec 105 -- sh -c" in why and "password@-" in why
    assert "SECRET_FILES" not in why and "wg0.conf" not in why


def test_a_config_written_from_a_secret_still_goes_to_secret_files():
    out = sr.secret_inside_a_guest_script(_helper('printf "%s\\n" "$AIRVPN_WG_CONF" > /etc/wireguard/wg0.conf'),
                                          {"AIRVPN_WG_CONF"})
    assert len(out) == 1 and "SECRET_FILES" in out[0]["why"]


def test_the_env_hint_never_teaches_the_prefix():
    out = sr.script_secret_not_passed([], [{"path": "/tmp/x.sh", "content": 'curl -u admin:"$MASS_PASSWORD" http://h/'}]
                                      ) or sr.script_secret_not_passed(["bash /tmp/x.sh"],
                                      [{"path": "/tmp/x.sh", "content": 'curl -u admin:"$MASS_PASSWORD" http://h/'}])
    assert out, "the gate must bite on a script that reads a secret it is never handed"
    assert 'MASS_PASSWORD="$MASS_PASSWORD"' not in out[0]["why"]
    assert "bash /tmp/x.sh # $MASS_PASSWORD" in out[0]["why"]
