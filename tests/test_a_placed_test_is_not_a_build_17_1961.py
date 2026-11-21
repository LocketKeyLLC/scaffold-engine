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


def test_the_engines_done_when_boilerplate_is_not_a_route_inventory():
    from app.modules.supervised_runs import rewrite_inventory_service_routes
    node = {"title": "Proxmox host: exercise the Palworld start/stop control and confirm the effect",
            "prompt_template": "Call the Palworld control that stops the server, then the one that starts it. On container "
                               "111. Done when it WORKS: exercise it and read a real value back (call the route and check "
                               "the answer carries the real data)."}
    cmds, ver, notes = rewrite_inventory_service_routes(node, ["curl -s http://192.168.1.25:3001/api/palworld/status"], [])
    assert cmds == ["curl -s http://192.168.1.25:3001/api/palworld/status"] and not notes


def test_a_read_only_read_step_is_not_developed_but_a_read_titled_build_is():
    from app.modules.develop import host_for
    from types import SimpleNamespace
    svc = SimpleNamespace(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service",
                          name="control-panel", port="3001", user="", group="", uid="", gid="")
    rb1 = {"title": "CT 111: read the request bodies the panel page sends for its controls", "tool": "Shell",
           "prompt_template": "Read only, change nothing. Print the request code the control panel page uses. "
                              "`pct exec 111 -- grep -nE -A12 api/media-request /opt/control-panel-ui/src/App.jsx`"}
    assert host_for(rb1, [svc]) is None
    build = {"title": "CT 111: read the host's guests through the Proxmox API", "tool": "CodeGen",
             "prompt_template": "Change the control panel's /api/network-ui handler to read the host through its API."}
    assert host_for(build, [svc]) is not None
