"""§17.1959 — a guest the step's words exclude ("not on CT 111") is not where the step works."""
from app.modules.machine_truth import place_guest, subject_guest, without_excluded_guests

# the live T7 (job 7cc2a7ac "Install Playwright toolchain")
_T7 = {"title": "Install Playwright toolchain", "description": "",
       "prompt_template": "Only if T1 chose Playwright or Hybrid. npm install playwright + browser binaries on the lab "
                          "runner (not on CT 111). CPU-only is fine."}


def test_the_live_step_has_no_guest_place():
    assert place_guest(_T7) is None
    assert subject_guest({**_T7, "description": _T7["prompt_template"]}) is None


def test_a_named_place_still_places_the_work():
    node = {"title": "CT 111: fix the Radarr handler", "prompt_template": "Edit the handler on container 111."}
    assert place_guest(node) == "111"


def test_a_place_with_an_exclusion_beside_it_keeps_the_place():
    node = {"title": "Install the browser", "prompt_template": "Install it on CT 112, not on CT 111 (the panel)."}
    assert place_guest(node) == "112"


def test_each_negation_form_is_dropped():
    for phrase in ("not on CT 111", "never inside VM 106", "instead of container 120", "rather than LXC 105",
                   "except guest 110", "Not in the CT 111"):
        assert "1" not in without_excluded_guests(phrase).strip(), phrase


def test_plain_mentions_are_kept():
    assert without_excluded_guests("reach CT 111 from CT 120") == "reach CT 111 from CT 120"


def test_a_service_named_as_the_route_is_not_the_guest():
    # the live T8 (job 7cc2a7ac "Curl API routes via Caddy"); CT 120 is caddy-proxy
    inv = {"names": {"120": "caddy-proxy", "111": "control-panel"}}
    node = {"title": "Curl API routes via Caddy", "description": ""}
    assert subject_guest(node, inv) is None
    assert subject_guest({"title": "Restart Caddy", "description": ""}, inv) == "120"
