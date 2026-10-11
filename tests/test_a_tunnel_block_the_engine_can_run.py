"""§17.1488 — ADD160 (AirVPN WireGuard with a kill-switch for CT 105) — three engine defects in one frame.

Live (2026-10-11; fixtures: the step and its drafted helper script):
1. refused "this block has no check at all" though the step names three checks (`wg show`, two address reads);
2. refused "`wg-quick@wg0` is not a unit anything the engine holds names" four lines after
   `apt-get install -y wireguard-tools`, the package that ships it;
3. NOT refused: `printf '%s\\n' "$AIRVPN_WG_CONF" > /etc/wireguard/wg0.conf` INSIDE CT 105, where the runner never
   sends the secret — the tunnel config would have been an empty file.
"""
import json
import pathlib

from app.modules import supervised_runs as sr

FIX = pathlib.Path("tests/fixtures")
FILES = json.loads((FIX / "add160_drafted_files_2026_10_11.json").read_text())
NODE = json.loads((FIX / "add160_node_2026_10_11.json").read_text())


def test_the_steps_own_checks_count_when_the_frame_is_drawn():
    assert len(sr.with_own_checks(NODE, [])) >= 2


def test_a_unit_the_block_installs_is_named():
    block = FILES[0]["content"]
    assert sr.unit_from_installed_package("wg-quick@wg0", block)
    assert not sr.unit_from_installed_package("qbittorent-nox", block)        # a typo stays refused
    assert sr.unit_from_installed_package("nginx", "apt-get install -y nginx")


def test_a_secret_read_inside_the_guest_is_refused():
    out = sr.secret_inside_a_guest_script(FILES, {"AIRVPN_WG_CONF", "PANEL_PASSWORD"})
    assert len(out) == 1 and sr._IN_GUEST_SECRET_MARKER in out[0]["why"]
    assert "pct exec 105 -- sh -c 'umask 077; cat > <PATH>' <<< \"$AIRVPN_WG_CONF\"" in out[0]["why"]
    assert sr.secret_inside_a_guest_script(FILES, {"GID", "UNIT"}) == [], "the helper's own variables are not secrets"


def test_the_refusal_is_wired_and_redrafts():
    src = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert "refused = refused + secret_inside_a_guest_script(files, _stored)" in src
    assert src.count('"is read inside the guest, where the runner never sends it"') == 3   # the constant + both registries
