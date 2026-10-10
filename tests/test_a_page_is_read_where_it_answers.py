"""§17.1443 — a fetched page reaches the model as its stretches about the query, not its first 2000 chars.

ADD4's router walkthrough fetched PureVPN's Spectrum port-forwarding article; `text[:2000]` was its
"what is port forwarding" intro, and the My Spectrum app steps ("… Port Forwarding & IP Reservations. Tap
Add Port Assignment … Device IP (assign a static IP to the target device first)") never reached the model.
It wrote "Tap Add Port Forwarding" and left out the IP reservation the app needs first — where the
operator got stuck. The fixture is an excerpt of the real extracted page (intro + the steps + troubleshooting).
"""
import pathlib

from app.utils.page_focus import focus

PAGE = pathlib.Path("tests/fixtures/purevpn_spectrum_port_forwarding.txt").read_text()
QUERY = "Spectrum SAX1V1K port forwarding My Spectrum app steps"   # the live pass's own query


def test_the_head_cut_missed_the_steps():
    assert len(PAGE) > 2000 and "Add Port Assignment" not in PAGE[:2000]


def test_the_focused_page_holds_the_app_steps_and_the_reservation():
    out = focus(PAGE, QUERY, 2000)
    assert len(out) <= 2000
    for want in ("Services > Router > Advanced Settings > Port Forwarding & IP Reservations",
                 "Tap Add Port Assignment", "assign a static IP to the target device first"):
        assert want in out, want


def test_a_short_page_and_no_query_are_unchanged():
    assert focus("short page", QUERY, 2000) == "short page"
    assert focus(PAGE, "", 2000) == PAGE[:2000]
    assert focus(PAGE, "zzqx wvvy", 2000) == PAGE[:2000]


def test_kept_stretches_stay_in_page_order():
    out = focus(PAGE, QUERY, 2000)
    assert out.index("Method 1") < out.index("Tap Add Port Assignment")


def test_assist_research_uses_it():
    src = pathlib.Path("app/modules/assist_research_lib.py").read_text()
    assert 'focus_page(p["content"], query, 2000)' in src and 'p["content"][:2000]' not in src


def test_the_callout_carries_the_path_because_the_rest_is_folded():
    from app.modules.assist_directives import _NEXT_CALLOUT_DIRECTIVE as d
    assert "FOLDED" in d and "never write 'follow the steps below'" in d and "the callout IS the path" in d
