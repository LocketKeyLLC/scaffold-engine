"""§17.1445 — a search full of the brand's own pages is thin, so the keyed backend runs.

Live 2026-10-10 (fixture: the real SearXNG answers that minute — every engine but bing suspended): ADD4's
queries returned Spectrum's billing, packages, channel-lineup and speed-test pages, and for the word-salad
fix query, ten Instagram pages. None is a site's front door, so `useful_count` said 10 and the Ollama
supplement never ran (0 times in 3 h); the step kept and recalled Spectrum's speed-test page as research.
"""
import json
import pathlib

from app.utils.web_search_supplement import useful_count

LIVE = json.loads(pathlib.Path("tests/fixtures/searxng_bing_only_2026_10_10.json").read_text())
JUNK = ["Spectrum Advanced WiFi router port forwarding My Spectrum app 2026",
        "SAX1V1K ES2251 port-forward rules rules saved router app shows allow reservation",
        "Spectrum SAX1V1K port forwarding My Spectrum app steps"]
GOOD = "Spectrum router port forwarding app how to forward ports My Spectrum"


def test_the_brand_pages_are_thin():
    for q in JUNK:
        assert len(LIVE[q]) == 10
        assert useful_count(LIVE[q], q) < 3, q


def test_the_how_to_pages_are_useful():
    assert useful_count(LIVE[GOOD], GOOD) >= 3


def test_no_query_keeps_the_old_count():
    assert useful_count(LIVE[JUNK[0]]) == 5     # the five front doors drop, as before


def test_the_transport_passes_the_query():
    src = pathlib.Path("app/utils/web_search_supplement.py").read_text()
    assert "useful_count(results, q) >= settings.web_search_supplement_min_useful" in src
