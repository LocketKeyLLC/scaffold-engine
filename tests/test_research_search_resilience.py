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


# ---------------------------------------------------------------------------
# §17.985 — the probe must report on the engines research ACTUALLY queries,
# and must not eat the container healthcheck's budget.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_probes_the_engine_set_research_actually_queries():
    """§17.985 — it sent a bare /search with NO `engines`, i.e. the default set
    §17.984 had just stopped using, so it graded engines no caller queries.

    Measured live, same query same minute: default set 0 results, the backbone
    10 — /health said "research will return nothing" while planning research
    was returning results_found=30.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from app import health
    from app.modules.research_extractors import _engines_for_category

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"url": "x"}], "unresponsive_engines": []}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        await health._check_searxng()

    params = client.get.await_args.kwargs["params"]
    assert params["engines"] == _engines_for_category("general")
    # The default set is what got CAPTCHA'd; asking for it by omission is the bug.
    assert "categories" not in params


@pytest.mark.asyncio
async def test_health_probe_is_bounded_by_a_total_budget():
    """§17.985 — httpx applies `timeout=` PER PHASE, so a slow connect plus a
    slow read can exceed it. Measured with searxng paused, the old 8.0 per-phase
    timeout put /health at 8.03s against the healthcheck's 10s budget.
    """
    import asyncio as _a
    from unittest.mock import MagicMock, patch

    from app import health

    async def _never_answers(*a, **kw):
        await _a.sleep(30)

    client = MagicMock()
    client.get = _never_answers
    with patch("app.utils.http_clients.get_searxng_client", return_value=client), \
            patch.object(health, "_SEARXNG_PROBE_BUDGET_S", 0.05):
        out = await _a.wait_for(health._check_searxng(), timeout=5)

    assert out["status"] == "down"
    assert "timeout" in out["error"]


def test_health_probe_runs_concurrently_with_the_other_checks():
    """§17.985 — it was `await _check_searxng()` AFTER the gather, so its
    latency ADDED to the endpoint rather than overlapping."""
    from app import health

    src = inspect.getsource(health.build_health_response)
    gather = src[src.index("await asyncio.gather("):]
    gather = gather[:gather.index("return_exceptions=True")]
    assert "_check_searxng()" in gather, "probe must be gathered, not awaited after"
    assert "searxng = await _check_searxng()" not in src


def test_health_probe_hint_does_not_overclaim():
    """§17.985 — the old hint asserted "research will return nothing until this
    clears", which the live run disproved: both callers retry on the wider
    SEARXNG_FALLBACK_ENGINES net, a superset of the backbone this probes."""
    from app import health

    src = inspect.getsource(health._check_searxng)
    assert "research will return nothing until this clears" not in src


@pytest.mark.asyncio
async def test_health_probe_error_string_has_no_dangling_colon():
    """§17.985 — several httpx errors carry an empty str(), which rendered in
    the operator's face as a bare "ReadTimeout: "."""
    from unittest.mock import MagicMock, patch

    import httpx

    from app import health

    client = MagicMock()
    client.get = MagicMock(side_effect=httpx.ReadTimeout(""))
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        out = await health._check_searxng()
    assert out["status"] == "down"
    assert out["error"] == "ReadTimeout"
