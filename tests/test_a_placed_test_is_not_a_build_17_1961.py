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


def test_a_step_placed_on_the_proxmox_host_is_never_developed():
    from app.modules.develop import host_for
    from types import SimpleNamespace
    svc = SimpleNamespace(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service",
                          name="control-panel", port="3001", user="", group="", uid="", gid="")
    for t in ("Proxmox host: baseline the game-server routes through the Caddy front door",
              "On the Proxmox host: add the panel route",
              "Proxmox host: read the game-server route handlers to learn their inputs and effects"):
        assert host_for({"title": t, "tool": "CodeGen", "prompt_template": "the control-panel backend"}, [svc]) is None, t
    # a placed GUEST build is still developed
    assert host_for({"title": "CT 111: add the media error state to the control-panel UI", "tool": "CodeGen",
                     "prompt_template": "edit /opt/control-panel-backend"}, [svc]) is not None
