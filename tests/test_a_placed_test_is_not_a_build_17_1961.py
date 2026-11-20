"""§17.1961 — a test step whose title names its place first ("Proxmox host: test …") is not developed as a build."""
from app.modules.develop import _CHECKS_OR_WRITES_UP_RE


def test_a_place_prefix_does_not_hide_the_verb():
    for t in ("Proxmox host: test the control panel game-server controls through its API",
              "CT 111: verify the panel sections", "Test Palworld settings control", "validate entire build"):
        assert _CHECKS_OR_WRITES_UP_RE.match(t), t


def test_a_build_title_with_a_place_is_still_a_build():
    for t in ("CT 111: add the media error state", "CT 111: fix the media-library route",
              "Proxmox host: install the test harness"):
        assert not _CHECKS_OR_WRITES_UP_RE.match(t), t
