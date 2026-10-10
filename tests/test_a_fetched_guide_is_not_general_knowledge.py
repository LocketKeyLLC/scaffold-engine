"""§17.1448 — when the menu path came from a page fetched for the question, the footer says whose page.

Live (ADD4 turn 3155, fixture): "Services → Router → Advanced Settings → Port Forwarding & IP Reservations →
Add Port Assignment" — word for word in the PureVPN and natchecker pages fetched that turn — was footed
"No official documentation was retrieved … come from general knowledge, not from a page fetched for this
question": only an official documentation host counted.
"""
import pathlib

from app.modules.assist_evidence import grounding_footer, interface_labels, interface_path_hosts, strip_verifier_footers

ANSWER = strip_verifier_footers(pathlib.Path("tests/fixtures/add4_fix_3155.md").read_text())
PUREVPN = {"kind": "web", "url": "https://www.purevpn.com/blog/spectrum-port-forwarding/",
           "text": pathlib.Path("tests/fixtures/purevpn_spectrum_port_forwarding.txt").read_text()}
OFF = {"kind": "web", "url": "https://www.spectrum.com/internet/speed-test", "text": "Download speed refers to how fast"}


def test_the_live_answer_still_carried_the_old_footer():
    raw = pathlib.Path("tests/fixtures/add4_fix_3155.md").read_text()
    assert "come from general knowledge" in raw and "general knowledge" not in ANSWER


def test_the_path_labels_are_read_off_the_answer():
    labels = interface_labels(ANSWER)
    assert {"port forwarding & ip reservations", "add port assignment", "advanced settings"} <= labels


def test_the_page_that_states_the_path_is_named():
    assert interface_path_hosts(ANSWER, [OFF, PUREVPN]) == ["purevpn.com"]
    foot = grounding_footer([], None, unsourced_interface=True, interface_hosts=["purevpn.com"])
    assert "third-party guides fetched for this question (purevpn.com)" in foot
    assert "general knowledge" not in foot


def test_with_no_such_page_it_still_says_general_knowledge():
    assert interface_path_hosts(ANSWER, [OFF]) == []
    assert "general knowledge" in grounding_footer([], None, unsourced_interface=True, interface_hosts=[])
