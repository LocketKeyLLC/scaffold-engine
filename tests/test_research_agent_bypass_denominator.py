"""§17.291 — `topic_classifier_bypass` log carries `total_urls` denominator.

§17.280-UX-5 audit-tail concern: ``research_agent._extract_entries``
emitted ``topic_classifier_bypass: bypassed_urls=5 bypassed_entries=12
distill_urls=20`` with no denominator. An operator reading the log
could not tell ``5 bypassed`` apart from ``5 bypassed out of 5 search
results`` (broken classifier — everything eligible) vs ``5 bypassed
out of 100`` (5% rate, normal). Same numerator, different signal.

§17.291 adds ``total_urls=%d`` (input search-results count) to the
log line. ``total_entries`` was considered and rejected — at the log
emission point the distill loop hasn't run yet, so the bypass entry
count is the only "entry" total knowable; emitting it as a "total"
would mislead. ``total_urls`` is the load-bearing denominator.

(Audit text framed this as an "SSE event" — actually a ``logger.info``
line. Same UX gap, narrower fix surface.)
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import research_agent
from app.modules.research_agent import _extract_entries


def _make_resp(success: bool = True, text: str = ""):
    """Fake model_router.generate response."""
    resp = AsyncMock()
    resp.success = success
    resp.text = text
    return resp


@pytest.mark.asyncio
class TestBypassLogIncludesTotalUrls:
    """§17.291 — denominator pin."""

    async def test_log_contains_total_urls_when_bypass_fires(self, caplog):
        """Mock classifier to bypass-tag every URL; assert the log line
        carries ``total_urls=N`` matching the input result count."""
        results = [
            {"title": f"R{i}", "url": f"https://so.example/{i}", "content": ""}
            for i in range(7)
        ]
        fetched = [
            {"url": r["url"], "content": "body content longer than 50 chars " * 3}
            for r in results
        ]

        with patch.object(research_agent, "_fetch_and_extract",
                          new_callable=AsyncMock, return_value=fetched), \
             patch("app.utils.url_classifier.classify_url",
                   return_value="so_answer"), \
             patch("app.utils.url_classifier.should_distill",
                   return_value=False), \
             caplog.at_level(logging.INFO, logger="scaffold"):
            entries = await _extract_entries(results, "any topic")

        # Find the §17.291 log line.
        bypass_records = [
            r for r in caplog.records
            if "topic_classifier_bypass" in r.getMessage()
        ]
        assert len(bypass_records) == 1, (
            "Expected exactly one topic_classifier_bypass log line when "
            "any URL takes the bypass path."
        )
        msg = bypass_records[0].getMessage()
        # The §17.291 denominator must appear with the input count (7).
        assert f"total_urls={len(results)}" in msg, (
            f"§17.291: log line missing or wrong total_urls denominator. "
            f"Saw: {msg!r}"
        )
        # Existing fields preserved.
        assert "bypassed_urls=7" in msg
        # Entries got produced via the bypass path.
        assert all(e.get("source_type") == "so_answer" for e in entries)

    async def test_log_total_matches_input_when_partial_bypass(self, caplog):
        """Only some URLs get classified — total_urls still reflects the
        full input, NOT just the bypass-eligible subset. Pin this
        directional contract."""
        results = [
            {"title": "SO", "url": "https://so.example/1", "content": ""},
            {"title": "Random", "url": "https://random.example/2", "content": ""},
            {"title": "SO2", "url": "https://so.example/3", "content": ""},
        ]
        fetched = [
            {"url": r["url"], "content": "body content longer than 50 chars " * 3}
            for r in results
        ]

        def _classify(url: str):
            # Only the SO URLs classify.
            return "so_answer" if "so.example" in url else None

        with patch.object(research_agent, "_fetch_and_extract",
                          new_callable=AsyncMock, return_value=fetched), \
             patch("app.utils.url_classifier.classify_url", side_effect=_classify), \
             patch("app.utils.url_classifier.should_distill", return_value=False), \
             patch.object(research_agent, "model_router") as mock_mr, \
             caplog.at_level(logging.INFO, logger="scaffold"):
            # Distill loop will fire for the non-SO URL; stub it.
            mock_mr.generate = AsyncMock(return_value=_make_resp(success=False))
            mock_mr.tool_call = mock_mr.generate
            await _extract_entries(results, "any topic")

        bypass_records = [
            r for r in caplog.records
            if "topic_classifier_bypass" in r.getMessage()
        ]
        assert len(bypass_records) == 1
        msg = bypass_records[0].getMessage()
        # 3 total URLs in / 2 SO URLs bypassed / 1 distilled.
        assert "total_urls=3" in msg, (
            f"§17.291: total_urls must reflect input count (3), not "
            f"bypass-only count (2). Saw: {msg!r}"
        )
        assert "bypassed_urls=2" in msg
        # The distill bucket has 1 deduped URL.
        assert "distill_urls=1" in msg

    async def test_no_log_when_no_bypass(self, caplog):
        """When zero URLs get bypassed, no log line is emitted — pre-
        §17.291 behavior preserved (the `if bypass_url_count > 0` guard
        still holds)."""
        results = [
            {"title": "R", "url": "https://random.example/1", "content": ""},
        ]
        fetched = [
            {"url": "https://random.example/1", "content": "body " * 100},
        ]

        with patch.object(research_agent, "_fetch_and_extract",
                          new_callable=AsyncMock, return_value=fetched), \
             patch("app.utils.url_classifier.classify_url", return_value=None), \
             patch("app.utils.url_classifier.should_distill", return_value=True), \
             patch.object(research_agent, "model_router") as mock_mr, \
             caplog.at_level(logging.INFO, logger="scaffold"):
            mock_mr.generate = AsyncMock(return_value=_make_resp(success=False))
            mock_mr.tool_call = mock_mr.generate
            await _extract_entries(results, "any topic")

        bypass_records = [
            r for r in caplog.records
            if "topic_classifier_bypass" in r.getMessage()
        ]
        assert bypass_records == [], (
            "§17.291: the `if bypass_url_count > 0` guard must keep the "
            "log silent when no URL takes the bypass path."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.291 — anchor the source so a drive-by refactor that drops
    the denominator surfaces in tests."""

    def test_log_format_includes_total_urls_token(self):
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "total_urls=%d" in src, (
            "§17.291 regression: the `total_urls=%d` denominator has "
            "been removed from the topic_classifier_bypass log line. "
            "Operators reading the log can no longer tell a broken "
            "classifier (5/5) from a normal bypass rate (5/100)."
        )
        # The format string and the args list must agree — verify
        # len(results) is the first arg.
        assert "len(results), bypass_url_count" in src, (
            "§17.291 regression: `len(results)` is no longer the first "
            "arg to the bypass log call. The format string expects "
            "`total_urls=%d` first; arg order must match."
        )
