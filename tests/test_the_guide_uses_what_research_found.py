"""§17.1435 — a walkthrough gives the current click path its research found; it does not ask the operator to
describe screens the sources describe.

Live, 2026-10-08, ADD4 (the router walkthrough): the guide said "The app's menus vary, so I won't guess" and
asked the operator to describe the My Spectrum app's first screen. Measured, read-only: the research block
it received held Spectrum's homepage and sign-in page (Bing), a router manual's Wi-Fi-password section and
unrelated KB rows -- because the curated engine list was all-but-suspended (brave, mojeek, stackoverflow,
superuser, askubuntu suspended; startpage failing) and `google cse`, the one engine answering, was not in
it. The same query through `google cse` returns Spectrum's support page with the exact path. A read-only
replay of the guide on the fixed tree gave Services -> Router -> Advanced Settings -> Port Forwarding & IP
Reservations -> Add Port Assignment, with both rules, and no request to describe a screen.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from app.modules import assist_directives as ad
from app.modules import assist_research_lib as arl
from app.modules import execution_agent
from app.modules import research_extractors as rx


def test_google_cse_leads_both_engine_lists():
    assert rx._GENERAL_BACKBONE.split(",")[0] == "google cse"
    assert rx.SEARXNG_FALLBACK_ENGINES.split(",")[0] == "google cse"


def test_the_fallback_runs_when_nothing_relevant_survives(monkeypatch):
    """Ten irrelevant hits from the one engine still answering must not count as results."""
    calls = []

    class Client:
        async def get(self, path, params):
            calls.append(params["engines"])
            if len(calls) == 1:
                rows = [{"title": "Spectrum Internet, TV, Mobile & Home Phone", "content": "no contracts", "url": "https://www.spectrum.com/"}]
            else:
                rows = [{"title": "Advanced WiFi: Advanced Settings | Spectrum Support",
                         "content": "select Router. Scroll down and select Advanced Settings. Select Port Forwarding",
                         "url": "https://www.spectrum.net/support/internet/advanced-wifi-advanced-settings"}]
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"results": rows})

    import app.utils.http_clients as hc
    monkeypatch.setattr(hc, "get_searxng_client", lambda: Client())
    monkeypatch.setattr(rx, "relevant_search_results",
                        lambda q, rows: [r for r in rows if "port forwarding" in (r["title"] + r["content"]).lower()])
    out = asyncio.run(execution_agent._searxng_search("Spectrum port forwarding steps"))
    assert len(calls) == 2 and "advanced-wifi-advanced-settings" in out


def test_the_directives_say_to_use_the_research_and_not_ask():
    src = inspect.getsource(ad)
    assert "USE WHAT THE RESEARCH FOUND" in src
    assert "NEVER a request to describe" in src and "then follow the steps below" in src
    assert "A CLICK PATH IS THE STEPS" in inspect.getsource(arl._render_research_block)
