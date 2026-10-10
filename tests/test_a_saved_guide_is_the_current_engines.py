"""§17.1444 — a walkthrough saved before the engine's current guide rules is regenerated, not re-served.

Live (ADD4, 2026-10-10 13:55Z): §17.1443 had shipped at 12:12, yet Guide served turn 3141's saved text in
0.15 s — "Add Port Forwarding", no IP reservation, "follow the steps below" — written on pages read from
their first 2000 chars.
"""
import ast
import pathlib
from datetime import datetime, timedelta, timezone

from app.modules import assist_guide as ag

T3141 = datetime(2026, 10, 10, 11, 21, 25, tzinfo=timezone.utc)   # the saved walkthrough re-served at 13:55


def test_the_saved_add4_walkthrough_is_regenerated():
    assert ag._cached_predates_rules({"_generated_at_raw": T3141}) is True
    assert ag._cached_predates_rules({"_generated_at_raw": T3141.isoformat()}) is True


def test_one_written_under_the_current_rules_is_served():
    assert ag._cached_predates_rules({"_generated_at_raw": ag.GUIDE_RULES_EPOCH + timedelta(minutes=1)}) is False
    naive = (ag.GUIDE_RULES_EPOCH + timedelta(hours=1)).replace(tzinfo=None)
    assert ag._cached_predates_rules({"_generated_at_raw": naive}) is False


def test_no_or_bad_timestamp_serves_the_cache():
    assert ag._cached_predates_rules({}) is False
    assert ag._cached_predates_rules({"_generated_at_raw": "not a date"}) is False


def test_both_cache_reads_check_it():
    src = pathlib.Path("app/modules/assist_guide.py").read_text()
    fns = {n.name: ast.get_source_segment(src, n) for n in ast.parse(src).body
           if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("ensure_guidance", "generate_guidance_stream"):   # the non-stream and the stream cache reads
        assert "_cached_predates_rules(cached)" in fns[name], name
