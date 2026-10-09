"""§17.1437 — a page a step's research found stays in that step's research.

ADD4's router walkthrough asked the operator to describe the My Spectrum app again after §17.1435-1436:
the web search had no answering engine that minute (google cse / brave / startpage suspended, bing
returning homepages), so the pass had no source and the rules sent it to asking.
"""
import ast
import pathlib

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.modules import step_sources
from app.modules.assist_directives import (
    _DONE_CRITERION_DIRECTIVE, _INTERFACE_FIDELITY_DIRECTIVE, _SCREEN_GROUNDING_DIRECTIVE,
)

PAGE = {"kind": "web", "url": "https://www.wikihow.com/Port-Forward-on-Spectrum",
        "title": "How to Port Forward on Spectrum", "date": "2026-04-02",
        "query": "My Spectrum app port forwarding",
        "text": ("Open the My Spectrum app and sign in. Tap Services, then Router, then Advanced Settings. "
                 "Tap Port Forwarding & IP Reservations. Set up an IP reservation for your device first, "
                 "then tap Add Port Assignment and enter the port range and protocol.")}
HOME = {"kind": "web", "url": "https://www.spectrum.net/?msockid=abc", "title": "Spectrum",
        "query": "q", "text": "x" * 400}
SNIPPET = {"kind": "web", "url": "https://example.com/a", "title": "a", "query": "q", "text": "short"}
KB = {"kind": "milvus", "query": "q", "text": "y" * 400}


class _DB:
    """Records the statements; SELECT returns `rows`."""
    def __init__(self, rows=()):
        self.rows, self.sql = list(rows), []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params or {}))
        res = MagicMock()
        res.mappings.return_value.all.return_value = self.rows if str(stmt).lstrip().startswith("SELECT") else []
        return res


@pytest.mark.asyncio
async def test_keeps_a_real_page_and_nothing_else():
    db = _DB()
    out = await step_sources.keep_and_recall(db, session_id="613dd1df-4c92-43f7-a35f-c9519add5701",
                                             node_key="ADD4", sources=[PAGE, HOME, SNIPPET, KB])
    inserts = [p for s, p in db.sql if "INSERT INTO assist_step_sources" in s]
    assert [p["url"] for p in inserts] == [PAGE["url"]], "a homepage, a snippet or a KB row is not a page to keep"
    assert inserts[0]["body"].startswith("Open the My Spectrum app")
    assert out == [PAGE, HOME, SNIPPET, KB]


@pytest.mark.asyncio
async def test_a_pass_with_no_web_source_gets_the_page_found_before():
    row = {"url": PAGE["url"], "title": PAGE["title"], "body": PAGE["text"], "published": "2026-04-02",
           "query": PAGE["query"], "found_at": "2026-10-09 21:18:20+00"}
    db = _DB(rows=[row])
    out = await step_sources.keep_and_recall(db, session_id="613dd1df-4c92-43f7-a35f-c9519add5701",
                                             node_key="ADD4", sources=[KB])
    assert out[0] is KB and len(out) == 2
    kept = out[1]
    assert kept["kind"] == "web" and kept["url"] == PAGE["url"] and "Add Port Assignment" in kept["text"]
    assert "kept from this step's research on 2026-10-09" in kept["query"]
    select = [p for s, p in db.sql if s.lstrip().startswith("SELECT")][0]
    assert select["nk"] == "ADD4" and select["fresh"] == [], "the recall is scoped to the step"


@pytest.mark.asyncio
async def test_a_page_found_again_is_not_doubled():
    db = _DB()
    await step_sources.keep_and_recall(db, session_id="613dd1df-4c92-43f7-a35f-c9519add5701",
                                       node_key="ADD4", sources=[PAGE])
    select = [p for s, p in db.sql if s.lstrip().startswith("SELECT")][0]
    assert select["fresh"] == [PAGE["url"]]


@pytest.mark.asyncio
async def test_fail_soft_and_no_session_is_a_no_op():
    srcs = [PAGE]
    assert await step_sources.keep_and_recall(None, session_id="s", node_key="ADD4", sources=srcs) is srcs
    assert await step_sources.keep_and_recall(_DB(), session_id=None, node_key="ADD4", sources=srcs) is srcs
    broken = MagicMock()
    broken.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await step_sources.keep_and_recall(broken, session_id="s", node_key="ADD4", sources=srcs) is srcs


def test_every_research_generator_keeps_and_recalls():
    """Sibling call sites drift: the stream guide, the non-stream guide and the fix all research a step."""
    src = pathlib.Path("app/modules/assist_guide.py").read_text()
    tree = ast.parse(src)
    fns = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("generate_guidance", "generate_guidance_stream", "generate_fix"):
        assert "keep_and_recall(db, session_id=session_id" in fns[name], name
    assert "session_id=session_id, db=db,  # §17.1437" in fns["ensure_guidance"]
    agent = pathlib.Path("app/modules/assist_agent.py").read_text()
    assert "session_id=session_id, db=db,  # §17.1437" in agent


def test_with_no_source_the_rules_give_the_path_instead_of_asking():
    for rule in (_SCREEN_GROUNDING_DIRECTIVE, _INTERFACE_FIDELITY_DIRECTIVE):
        assert "marked unconfirmed" in rule and "only where their screen differs" in rule
        assert "ask them to describe what they see" not in rule
    assert "WHETHER OR NOT a source" in _SCREEN_GROUNDING_DIRECTIVE
    assert "has NAMED" in _SCREEN_GROUNDING_DIRECTIVE


def test_done_when_is_the_goal_not_a_report():
    assert "never 'you have told me what you see'" in _DONE_CRITERION_DIRECTIVE
