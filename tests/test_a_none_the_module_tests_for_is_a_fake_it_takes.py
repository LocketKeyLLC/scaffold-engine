"""§17.1913 — a value the module tests for None before it acts on it takes None from a test's stand-in.

Live (follow-up 5b4880c0 T9 a1, 2026-11-04 22:49 UTC): `scheduler = get_scheduler()` then `if scheduler is not
None: scheduler.add_job(…)` -- exactly what the install note asks for ("it may return None: then nothing is
scheduled") -- and a test patching `get_scheduler` to return None was refused: "a fake whose call returns None …
and the module calls `.add_job()` on it"."""
from __future__ import annotations

from app.modules.engine_contracts import wrong_fakes

MOD = '''# app/modules/lab_wire.py
from app.scheduler import get_scheduler


def install(app) -> None:
    scheduler = get_scheduler()
    if scheduler is not None:
        scheduler.add_job(print, "cron", hour=9)


def install_unguarded(app) -> None:
    scheduler = get_scheduler()
    scheduler.add_job(print, "cron", hour=9)
'''


def _test(fn: str) -> str:
    return f'''# tests/test_lab_wire.py
from unittest.mock import patch
from app.modules.lab_wire import {fn}


def test_no_scheduler():
    with patch("app.modules.lab_wire.get_scheduler", return_value=None):
        {fn}(object())
'''


def test_none_is_taken_where_the_module_tests_for_it_and_refused_where_it_does_not():
    body = MOD.split("\n", 1)[1]
    guarded, unguarded = body.split("\n\ndef install_unguarded")
    mods = {"lab_wire": guarded}
    assert wrong_fakes({"tests/test_lab_wire.py": _test("install")}, mods, {"lab_wire"}) == []
    # every place the module binds it must test it: one that does not is enough for None to break it
    mods = {"lab_wire": guarded + "\n\ndef install_unguarded" + unguarded}
    got = wrong_fakes({"tests/test_lab_wire.py": _test("install")}, mods, {"lab_wire"})
    assert len(got) == 1 and "returns None" in got[0] and "`.add_job()`" in got[0], got


def test_a_fix_steps_key_is_cited_as_itself_on_a_verdict_line():
    """§17.1913 — live (API_REDDIT_AUDIT a1-a3): "MISSING: ['T6U7']" with every verdict line citing `(T6U7)`."""
    from app.modules.execution_verify import check_validation_citation_coverage
    out = ("## Verdicts\n"
           "- **NOT MET — `paper_reddit_ingestion` (T6U7):** sends no `Authorization: bearer TOKEN` header\n"
           "- MET — `paper_spy_benchmark_tracker` (T5U4): reads bars from data.alpaca.markets\n"
           "- the proposer (T1F2) is mentioned outside any verdict\n")
    assert check_validation_citation_coverage(out, ["T6U7", "T5U4"]) == []
    assert check_validation_citation_coverage(out, ["T6U7", "T1F2", "T7"]) == ["T1F2", "T7"]
    assert check_validation_citation_coverage("- MET — T2 lines 5-15", ["T2", "T3"]) == ["T3"]   # numeric keys as before
