"""§17.983-984 — search that returns nothing must say WHY, and planning must
use the same hardened path as everything else.

Both found by the §17.982 end-to-end run against the live box.

The engine's entire research capability was inert and nothing said so. SearXNG
answered HTTP 200 with zero results for every query because every default engine
was suspended:

    brave      Suspended: too many requests
    duckduckgo CAPTCHA
    google     Suspended: access denied
    startpage  Suspended: CAPTCHA

`unresponsive_engines` is in that same response and appeared NOWHERE in the
codebase, so "search is down" and "nothing matched" were indistinguishable —
which is why §17.976 found 19 guided steps with zero sources and no failure
logged against any of them. SearXNG was also the one dependency /health never
checked, because the container's own healthcheck proves only that it listens.

Measured against the live instance at that moment: `bing` returned 10 results
for the same query the default set returned 0 for.
"""
import inspect

import pytest


# ── §17.984 — planning used the blocked default set, with no fallback ─────


def test_planning_search_uses_the_curated_engines_not_categories():
    """§17.712 hardened execution_agent and §17.729 hardened
    assist_research_lib. gt_extractor — the one PLANNING uses — was never done,
    so it still asked for `categories=general`: the broad default set, which is
    both flood-prone and exactly the set that gets CAPTCHA'd."""
    from app.modules import gt_extractor

    src = inspect.getsource(gt_extractor.search_searxng)
    assert '"categories": "general"' not in src
    assert "_engines_for_category" in src


def test_planning_search_retries_on_empty():
    from app.modules import gt_extractor

    src = inspect.getsource(gt_extractor.search_searxng)
    assert "SEARXNG_FALLBACK_ENGINES" in src
    # The retry must be conditional on empty, or every query costs two calls.
    # Anchored on the USE, not the import — the name appears in both, and
    # indexing the first occurrence catches the import line instead.
    assert src.index("if not results:") < src.rindex("SEARXNG_FALLBACK_ENGINES")


def test_no_caller_is_left_on_categories_general():
    """The sweep — this pattern has now been fixed three times in three
    modules, which is how it survived in the fourth."""
    from app.modules import assist_research_lib, execution_agent, gt_extractor

    for mod in (gt_extractor, execution_agent, assist_research_lib):
        src = inspect.getsource(mod)
        for line in src.splitlines():
            s = line.strip()
            if '"categories": "general"' in s and not s.startswith("#"):
                raise AssertionError(f"{mod.__name__}: {s}")


# ── §17.983 — an empty result names the suspended engines ────────────────


@pytest.mark.parametrize("mod_name,fn_name", [
    ("app.modules.gt_extractor", "search_searxng"),
    ("app.modules.research_agent", None),
])
def test_an_empty_search_logs_the_suspended_engines(mod_name, fn_name):
    import importlib

    mod = importlib.import_module(mod_name)
    src = inspect.getsource(getattr(mod, fn_name) if fn_name else mod)
    assert "unresponsive_engines" in src, mod_name
    assert "suspended_engines" in src, mod_name


def test_the_empty_log_is_a_warning_not_an_info():
    """`results=0` was already logged at INFO and read as ordinary. The whole
    point is that this one is not ordinary."""
    from app.modules import gt_extractor

    src = inspect.getsource(gt_extractor.search_searxng)
    i = src.index("gt_searxng_empty")
    assert "logger.warning" in src[max(0, i - 120):i]


# ── §17.983 — /health watches the search backend now ─────────────────────


@pytest.mark.asyncio
async def test_health_reports_degraded_when_every_engine_is_suspended():
    from unittest.mock import AsyncMock, MagicMock, patch

    from app import health

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "results": [],
        "unresponsive_engines": [["brave", "Suspended: too many requests"],
                                 ["duckduckgo", "CAPTCHA"]],
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        out = await health._check_searxng()
    assert out["status"] == "degraded"
    assert out["results"] == 0
    assert ["brave", "Suspended: too many requests"] in out["suspended_engines"]
    assert "hint" in out


@pytest.mark.asyncio
async def test_health_reports_up_when_results_come_back():
    from unittest.mock import AsyncMock, MagicMock, patch

    from app import health

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"url": "x"}], "unresponsive_engines": []}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        out = await health._check_searxng()
    assert out["status"] == "up"


@pytest.mark.asyncio
async def test_health_never_raises_on_a_dead_backend():
    from unittest.mock import MagicMock, patch

    from app import health

    client = MagicMock()
    client.get.side_effect = RuntimeError("connection refused")
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        out = await health._check_searxng()
    assert out["status"] == "down"
    assert "error" in out


def test_searxng_is_in_the_health_payload():
    """It was the one dependency /health never watched — the container reports
    healthy because its own check proves only that it is listening."""
    from app import health

    src = inspect.getsource(health.build_health_response)
    assert '"searxng": searxng' in src


def test_a_degraded_backend_does_not_flip_the_top_level_status():
    """§17.171 precedent: a sidecar's state is surfaced, not escalated. A
    suspended engine set is degraded research, not a dead engine."""
    from app import health

    src = inspect.getsource(health.build_health_response)
    i = src.index('"searxng": searxng')
    # The status computation must not consider it.
    tail = src[i:]
    assert 'searxng' not in tail.split('"status"')[-1][:400]
