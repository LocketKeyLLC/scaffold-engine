"""§17.934 — the pipelines lane must be unable to reach the live engine.

This lane runs with `--noconftest`, so the conftest autouse guard never loads
here. Its only protection is the import-time `install()` in
`tests/_scaffold_router_setup.py`. That is easy to delete by accident during a
refactor, and the failure is SILENT — the lane goes green while writing into
the operator's live assist session, which is exactly what happened twice.
These tests are the tripwire for the tripwire.
"""
import pytest
import requests

from tests import _live_write_guard
from tests._scaffold_router_setup import Pipeline  # noqa: F401 — installs the guard


def test_guard_is_installed_in_this_lane():
    assert _live_write_guard._installed is True, (
        "§17.934: tests/_scaffold_router_setup.py must install the live-write "
        "guard at import. Without it this lane authenticates with the "
        "operator's master key and writes to the real database."
    )


def test_pipeline_cannot_reach_the_orchestrator():
    with pytest.raises(_live_write_guard.LiveEngineWriteBlocked):
        requests.get("http://scaffold-orchestrator:8000/work")


@pytest.mark.parametrize("attr,expected", [
    ("_fetch_work", None),
    ("_classify_command", {"intent": "none", "confidence": "low"}),
])
def test_unconditional_live_probes_are_stubbed(attr, expected):
    """`pipe()` calls these on essentially every turn. Each is stubbed to its
    OWN documented fail-soft value, so the lane exercises the real degrade path
    rather than an invented one."""
    pipe = Pipeline()
    fn = getattr(pipe, attr)
    got = fn() if attr == "_fetch_work" else fn("some message")
    assert got == expected


def test_assist_candidates_probe_is_stubbed():
    from tests._scaffold_router_setup import _mod
    assert _mod._assist.fetch_assist_candidates(Pipeline()) == []
