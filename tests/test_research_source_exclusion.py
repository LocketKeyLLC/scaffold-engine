"""§17.1049 — the engine never cites itself.

Live (operator session 613dd1df, read-only replay): the web search returned
THIS project's own pull-request page — it quoted the operator's question, the
step recap's open item and the engine's earlier wrong answer — and the reply
regressed to that wrong shape. Two rules: a page that quotes the operator's
own words verbatim is an echo of the conversation (generic, on by default),
and a deployment can exclude its own repository by URL pattern.
"""
from __future__ import annotations

import pathlib

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import assist_research_lib as rl

QUESTION = ("On the spectrum app it only gives you the option to fill out primary server and "
            "secondary server, where is the port forwarding section")


def test_a_page_quoting_the_operators_words_is_an_echo():
    page = ("PR body: the operator asked 'On the spectrum app it only gives you the option to fill out "
            "primary server and secondary server' and the answer was generic.")
    assert rl._echoes_operator(page, QUESTION)
    assert not rl._echoes_operator("Configure DNS servers on the router: primary and secondary fields.", QUESTION)
    assert not rl._echoes_operator(page, "short text")  # fewer than eight words: nothing to shingle
    assert not rl._echoes_operator("", QUESTION) and not rl._echoes_operator(page, None)


def test_excluded_url_patterns_come_from_the_valve(monkeypatch):
    monkeypatch.setattr(rl.settings, "research_excluded_url_patterns", ["github.com/example-org/example-repo"])
    assert rl._excluded_url("https://github.com/Example-Org/example-repo/pull/430")
    assert not rl._excluded_url("https://github.com/other/repo/issues/1")
    monkeypatch.setattr(rl.settings, "research_excluded_url_patterns", [])
    assert not rl._excluded_url("https://github.com/Example-Org/example-repo/pull/430")


@pytest.mark.asyncio
async def test_deep_fetch_drops_echoes_and_search_drops_excluded_urls(monkeypatch):
    import app.modules.research_agent as ra
    import app.modules.research_extractors as rx
    monkeypatch.setattr(rl.settings, "research_excluded_url_patterns", ["github.com/example-org/example-repo"])

    async def fake_search(query, max_results=5):
        return [{"title": "echo", "url": "https://blog.example.org/echo", "content": "x", "date": "2026-01-01"},
                {"title": "real", "url": "https://docs.example.org/dns", "content": "y", "date": "2026-02-01"}]
    monkeypatch.setattr(rl, "_searxng_structured", fake_search)

    async def fake_fetch(results):
        return [{"url": "https://blog.example.org/echo",
                 "content": "someone pasted: On the spectrum app it only gives you the option to fill out primary server and secondary server, where"},
                {"url": "https://docs.example.org/dns", "content": "spectrum app primary server secondary server DNS fields explained"}]
    monkeypatch.setattr(ra, "_fetch_and_extract", fake_fetch)
    monkeypatch.setattr(rx, "relevant_search_results", lambda q, pages, **kw: pages)
    out = await rl._deep_web_sources("spectrum app primary server secondary server", top_n=2, operator_text=QUESTION)
    assert [s["url"] for s in out] == ["https://docs.example.org/dns"]


def test_the_ask_and_fix_paths_hand_the_operator_text_to_the_fetch():
    root = pathlib.Path(__file__).resolve().parents[1]
    lib = (root / "app/modules/assist_research_lib.py").read_text()
    r1 = lib[lib.index("async def research_one("):]
    assert "operator_text=question" in r1
    assert "operator_text=operator_text)" in lib[lib.index("async def _documentation_sources("):lib.index("async def research_one(")]
    assert "if _excluded_url(r.get(\"url\", \"\"))" in lib  # before the top-N slice
    guide = (root / "app/modules/assist_guide.py").read_text()
    fx = guide[guide.index("async def generate_fix("):]
    assert "operator_text=error_text" in fx
