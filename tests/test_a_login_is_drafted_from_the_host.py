"""§17.1495 — a step that logs in with a stored secret is not drafted from an in-guest template.

Live 2026-10-12, a home-lab step that changed a download client's setting through its Web API (log in as admin
with a stored password) was drafted from run_in_container three times and refused three times: the runner hands a
stored secret only to a HOST command naming it, so the password is empty inside the guest. The same login drafted
with no template ran first time. A secret that IS a file's content (a VPN config) keeps the template's
SECRET_FILES (§17.1489)."""
from types import SimpleNamespace

from app.modules import runbook_templates as rt

CT = SimpleNamespace(gid="105", kind="lxc", running=True)

LOGIN = {"title": "CT 105 (download-client): set the client's listening port",
         "description": ("Its Web API is http://127.0.0.1:8080 inside CT 105; log in as user admin with the stored "
                         "secret MASS_PASSWORD fed on stdin (`curl … --data-urlencode password@- …` with "
                         "`<<< \"$MASS_PASSWORD\"`). Change the port through `/api/v2/app/setPreferences`.\n\n"
                         "Done when the preferences read back the new port.")}
API_KEY = {"title": "Set the media manager's root folder",
           "description": "Inside CT 103 call the API with `-H \"X-Api-Key: $RADARR_API_KEY\"` and set the root folder."}
CONFIG = {"title": "CT 105: put the VPN config in place and start the tunnel",
          "description": ("Write the WireGuard config from the stored secret VPN_WG_CONF to /etc/wireguard/wg0.conf "
                          "(mode 600) inside CT 105, then enable wg-quick@wg0.")}
PLAIN = {"title": "Inside CT 105 restart the download client", "description": "systemctl restart qbittorrent-nox"}


def test_a_login_with_a_stored_secret_is_recognised():
    assert rt.logs_in_with_a_stored_secret(LOGIN)
    assert rt.logs_in_with_a_stored_secret(API_KEY)


def test_a_secret_that_is_a_config_file_is_not_a_login():
    assert not rt.logs_in_with_a_stored_secret(CONFIG)
    assert not rt.logs_in_with_a_stored_secret(PLAIN)


def test_a_login_gets_no_in_guest_template(monkeypatch):
    monkeypatch.setattr(rt.RUN_IN_CONTAINER, "applies", lambda n, t: True)
    assert rt.select_template(LOGIN, CT) is None
    assert rt.select_template(PLAIN, CT) is rt.RUN_IN_CONTAINER


def test_an_os_install_keeps_its_own_template(monkeypatch):
    node = {"title": "Install Ubuntu 22.04 on VM 106 unattended",
            "description": "cloud-init user with the stored secret MASS_PASSWORD so the operator can log in"}
    assert rt.logs_in_with_a_stored_secret(node)
    monkeypatch.setattr(rt.INSTALL_OS_CLOUDINIT, "applies", lambda n, t: True)
    assert rt.select_template(node, SimpleNamespace(gid="106", kind="qemu", running=False)) is rt.INSTALL_OS_CLOUDINIT
