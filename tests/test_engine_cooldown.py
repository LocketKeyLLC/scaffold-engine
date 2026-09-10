"""§17.1003 — an engine that keeps refusing us stops being asked for a while.

SearXNG suspends an engine that refuses it, but the ENGINE kept naming that
engine in every `engines=` list, so each query paid for it again: attempted,
refused, suspension refreshed. §17.991 handled the extreme case by hand —
duckduckgo blackholes this host, so it came out of the lists — but a hand-edit
only helps for a permanent block someone noticed. `mojeek` 403s for days;
`startpage` cycles on its own. Neither deserves a code change; both deserve to
stop being asked every query.
"""
import pytest

from app.modules import research_extractors as rx


@pytest.fixture(autouse=True)
def _clean_tracker():
    rx._engine_streak.clear()
    rx._engine_cooldown_until.clear()
    yield
    rx._engine_streak.clear()
    rx._engine_cooldown_until.clear()


def _fail(engine, times):
    for _ in range(times):
        rx.note_engine_health(rx._GENERAL_BACKBONE, [[engine, "Suspended: access denied"]])


def test_one_bad_query_does_not_drop_an_engine():
    """Engines blip. Dropping on the first refusal would thrash the pool."""
    _fail("mojeek", 1)
    assert "mojeek" in rx._engines_for_category("general")


def test_a_persistent_refuser_is_dropped_from_the_primary_list():
    _fail("mojeek", rx._COOLDOWN_AFTER_STREAK)
    engines = rx._engines_for_category("general")
    assert "mojeek" not in engines
    assert "bing" in engines, "the working engines must survive"


def test_answering_again_clears_the_streak():
    """Recovery matters as much as failure — without this a blip becomes a
    permanent exclusion and the pool only ever shrinks."""
    _fail("mojeek", rx._COOLDOWN_AFTER_STREAK - 1)
    # a query where mojeek is NOT in unresponsive_engines = it answered
    rx.note_engine_health(rx._GENERAL_BACKBONE, [["brave", "too many requests"]])
    assert rx._engine_streak.get("mojeek", 0) == 0
    _fail("mojeek", 1)
    assert "mojeek" in rx._engines_for_category("general")


def test_the_cooldown_expires():
    import time

    _fail("mojeek", rx._COOLDOWN_AFTER_STREAK)
    assert "mojeek" not in rx._engines_for_category("general")
    rx._engine_cooldown_until["mojeek"] = time.monotonic() - 1
    assert "mojeek" in rx._engines_for_category("general"), (
        "a cooled engine must be retried, or an outage becomes permanent")


def test_a_total_outage_does_not_cool_everything_down():
    """The floor. Without it, an upstream failure that hits every engine would
    leave the query naming NOTHING — converting a transient outage into a
    self-inflicted one."""
    # A real total outage lists every engine in the SAME response. (Failing them
    # one at a time does not: each call resets the streak of whichever engines
    # answered, which is the recovery behaviour above working as intended — the
    # first cut of this test got that wrong and blamed the code.)
    everyone = [[e.strip(), "Suspended: access denied"]
                for e in rx._GENERAL_BACKBONE.split(",")]
    for _ in range(rx._COOLDOWN_AFTER_STREAK):
        rx.note_engine_health(rx._GENERAL_BACKBONE, everyone)
    engines = rx._engines_for_category("general")
    assert engines == rx._GENERAL_BACKBONE, (
        "when filtering would drop below the floor, ask for everything")
    assert len(engines.split(",")) >= rx._MIN_ENGINES


def test_the_fallback_net_is_never_narrowed():
    """The 0-results fallback exists to be the widest possible net. Narrowing
    the rescue path is how §17.984 happened."""
    everyone = [[e.strip(), "Suspended: access denied"]
                for e in rx._GENERAL_BACKBONE.split(",")]
    for _ in range(rx._COOLDOWN_AFTER_STREAK):
        rx.note_engine_health(rx._GENERAL_BACKBONE, everyone)
    assert "brave" in rx.SEARXNG_FALLBACK_ENGINES
    assert "startpage" in rx.SEARXNG_FALLBACK_ENGINES


def test_unknown_shapes_do_not_raise():
    """`unresponsive_engines` is upstream JSON — it must never be able to break
    a search."""
    for junk in ([], None, [[]], ["bare-string"], [{"weird": 1}], [[None, None]]):
        rx.note_engine_health(rx._GENERAL_BACKBONE, junk)
    assert rx._engines_for_category("general")


def test_the_planning_path_reports_engine_health():
    """Wiring check: the tracker only learns if the callers feed it."""
    import inspect

    from app.modules import gt_extractor as gt

    assert "note_engine_health" in inspect.getsource(gt.search_searxng)
