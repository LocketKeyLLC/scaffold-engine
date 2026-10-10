"""§17.1446 — ADD4's open defects after the IP-reservation report (2026-10-10).

1. The engine's own read-only look-up through the local runner (`pct exec 120 -- ip a`, turn 3147) came
   back, was routed submit and judged "not done", and the operator was told "If it IS done, reply confirm".
2. The fix query: the runner's header ("… not in a sandbox") and `ip a`'s LOOPBACK line ("loop") read as a
   failure and became the query; the step's OPEN line gave "port-forward rules rules saved router app shows
   allow reservation".
3. The step kept and recalled Spectrum's speed-test and marketing pages as research.
"""
import pathlib

from app.modules.assist_evidence import blocker_clause, derive_need
from app.modules.assist_research_lib import _goal_keywords
from app.modules.assist_runner_lookup import LOOKUP_HEAD, is_own_lookup
from app.modules.step_sources import _keepable, about_its_query

LOOKUP = pathlib.Path("tests/fixtures/add4_runner_lookup_3147.txt").read_text()
RECAP = pathlib.Path("tests/fixtures/add4_recap_2026_10_10.txt").read_text()
TITLE = "Router app: forward TCP ports 80 and 443 to the Caddy proxy (192.168.1.26) -- a guided walkthrough"
APPROVED = ("[local-runner] ran this step's block through your local runner (pve-runner) with your approval — "
            "4 commands ran ON THE TARGET MACHINE itself, as the runner's account:\n$ pct reboot 120")


# ── 1 ──
def test_the_live_lookup_is_the_engines_own():
    assert LOOKUP.startswith(LOOKUP_HEAD) and is_own_lookup(LOOKUP)
    assert not is_own_lookup(APPROVED), "a block the operator approved is their own work"
    assert not is_own_lookup("it's done, both rules saved")


def test_the_blocked_submit_skips_the_offer_for_it():
    src = pathlib.Path("app/modules/assist_turn.py").read_text()
    i = src.index("_own_lookup = is_own_lookup(text_)")
    assert src.index("if _own_lookup:", i) < src.index("stage_completion_confirm(", i)


# ── 2 ──
def test_a_healthy_ip_a_is_not_an_error_and_the_header_is_not_the_query():
    n = derive_need(LOOKUP, title=TITLE, step_recap=RECAP)
    assert n.kind == "goal", (n.kind, n.subject)
    assert "local-runner" not in n.query and "LOOPBACK" not in n.query


def test_the_open_line_gives_its_obstacle_without_repeats():
    line = ("No port-forward rules saved yet; router app still shows no IP for bc:24:11:ac:c9:06 and won't "
            "allow a reservation.")
    assert blocker_clause(line).startswith("router app still shows no IP")
    q = derive_need(LOOKUP, title=TITLE, step_recap=RECAP).query
    words = q.split()
    assert len(words) == len(set(words)), q
    assert "ip" in words and "reservation" in words, q
    assert blocker_clause("Caddy's certificate request times out") == "Caddy's certificate request times out"


def test_overlapping_pairs_do_not_repeat_in_the_query():
    from app.modules.assist_evidence import _cap
    kw = _goal_keywords("No port-forward rules saved yet", 6)
    assert kw[:2] == ["port-forward rules", "rules saved"]
    assert _cap(" ".join(kw)) == "port-forward rules saved"


# ── 3 ──
def test_a_brand_page_is_not_kept_or_recalled():
    q = "Spectrum router port forwarding app how to forward ports My Spectrum"
    speed = {"kind": "web", "url": "https://www.spectrum.com/internet/speed-test?msockid=3c48",
             "title": "Spectrum Internet Speed Test: Broadband Internet Speed Check", "query": q, "text": "x" * 900}
    assert not _keepable(speed)
    assert not about_its_query("https://www.spectrum.com/internet?msockid=24e1",
                               "Fiber Powered Home Internet & WiFi Service Provider - Spectrum",
                               "Spectrum Advanced WiFi router port forwarding My Spectrum app 2026")
    howto = dict(speed, url="https://www.wikihow.com/Port-Forward-on-Spectrum", title="How to Port Forward on Spectrum")
    assert _keepable(howto)
    assert about_its_query("https://community.roonlabs.com/t/spectrum-internet-wifi-6-router-sax1v1k/204406",
                           "Spectrum Internet WiFi 6 Router SAX1V1K",
                           "Spectrum SAX1V1K port forwarding My Spectrum app steps (kept from this step's research on 2026-10-10)")
