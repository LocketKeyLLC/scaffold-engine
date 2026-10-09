"""§17.1436 — an app walkthrough's research reads the pages that answer it; a fresh pass is guided from now.

Live, 2026-10-09, ADD4 (the router walkthrough), after the operator showed the real screen ("Port Forwarding
and IP Reservations" opens on Create IP Reservations — reserve the device first, then add the port
assignment). Traced through the STREAMING guide (the SPA path):
- the screen-grounding rule (§17.758) told every walkthrough on an interactive surface to OPEN by asking
  what is on screen -- a phone app included;
- research used search SNIPPETS (deep=False), cut one line before "Set up an IP reservation";
- with deep fetch on, the pages fetched were the top-ranked results -- Spectrum's homepage, billing and
  sales pages -- because one shared word ("Spectrum") passed the relevance filter and order was kept;
- a re-guided step still opened "📍 Where we are … Next: describe the first screen": its saved recap and
  the transcript's last turns were the previous pass's "describe what you see" guides.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.modules import assist_agent
from app.modules import assist_directives as ad
from app.modules import assist_guide as ag
from app.modules import research_extractors as rx


def test_an_app_step_is_researched_deep():
    ui = SimpleNamespace(tool="LLM", base_prompt="in the My Spectrum app on the router, forward TCP 80")
    shell = SimpleNamespace(tool="Shell", base_prompt="in the app directory run make")
    assert ag._is_ui_walkthrough(ui) and not ag._is_ui_walkthrough(shell)
    src = inspect.getsource(ag)
    assert src.count("deep=_is_ui_walkthrough(ctx)") == 2              # stream AND non-stream (parity)


def test_a_known_entry_point_is_a_confirmed_starting_screen():
    assert "STARTS FROM A KNOWN ENTRY POINT" in ad._SCREEN_GROUNDING_DIRECTIVE


def test_ranking_puts_the_answer_before_the_front_door():
    home = {"title": "Spectrum Internet, TV, Mobile & Home Phone", "content": "no contracts", "url": "https://www.spectrum.com/"}
    sales = {"title": "Spectrum Internet", "content": "speeds up to 1 Gig", "url": "https://www.spectrum.com/internet"}
    answer = {"title": "How to Port Forward on Spectrum", "content": "Open the My Spectrum app and tap Services. "
              "Tap Router. Advanced Settings. Port Forwarding & IP Reservations. Add Port Assignment",
              "url": "https://www.wikihow.com/Port-Forward-on-Spectrum"}
    got = rx.relevant_search_results("Spectrum SAX1V1K port forwarding My Spectrum app steps", [home, sales, answer])
    assert got[0] is answer and home not in got and sales not in got


def test_a_short_query_still_keeps_a_one_word_match():
    r = {"url": "https://zenarmor.com/pricing", "title": "Zenarmor pricing", "content": "..."}
    assert rx.relevant_search_results("Zenarmor free vs paid", [r]) == [r]


def test_the_deep_fetch_ranks_before_it_picks():
    from app.modules import assist_research_lib as arl
    src = inspect.getsource(arl._deep_web_sources)
    assert src.index("results = _rel(query, results) or results") < src.index("_fetch_and_extract(results[:top_n])")


def test_a_fresh_pass_drops_the_previous_pass_and_its_recap():
    db = MagicMock()
    pres = MagicMock(); pres.scalar.return_value = None                           # unclaimed
    olds = MagicMock(); olds.scalars.return_value.all.return_value = ["OLD describe-the-screen guide"]
    db.execute = AsyncMock(side_effect=[pres, olds])
    hist = [{"role": "assistant", "content": "OLD describe-the-screen guide"}, {"role": "user", "content": "ok"}]
    kept, fresh = asyncio.run(assist_agent._drop_previous_pass(hist, session_id="s", nk="ADD4", db=db))
    assert fresh is True and kept == [{"role": "user", "content": "ok"}]
    src = inspect.getsource(assist_agent.assemble_generation_memory)
    assert "recap = None if fresh else await get_step_recap(" in src
    assert "progress_recap=NULL, progress_recap_turns=0" in inspect.getsource(assist_agent._reopen_step_mirrored)


def test_a_claimed_step_keeps_its_history():
    db = MagicMock()
    pres = MagicMock(); pres.scalar.return_value = "2026-10-09T10:32:13Z"
    db.execute = AsyncMock(side_effect=[pres])
    hist = [{"role": "assistant", "content": "this pass's guide"}]
    assert asyncio.run(assist_agent._drop_previous_pass(hist, session_id="s", nk="ADD4", db=db)) == (hist, False)
