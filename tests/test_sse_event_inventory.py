"""§17.190 — drift guards for app/sse_events.py + pipelines/_sse_events.py.

Two distinct guards:

  1. ``test_sse_events_byte_equal`` — parallel to ``test_sdk_schema_parity``.
     Enforces that the vendored OWUI-side copy is byte-equal to the source.
     Backstop for ``make check-sse-events`` which is the fast-fail CI gate
     (§17.190 CI step). The in-suite test catches a developer who pushes
     past the gate (broken CI, direct push to a feature branch).

  2. ``test_emitter_event_names_are_in_inventory`` /
     ``test_consumer_event_names_are_in_inventory`` — scans the orchestrator
     emitter files (execution_agent / assist_agent / research_agent /
     design_pipeline) for ``_sse("name", ...)`` literals and the consumer
     (scaffold_router) for ``event_type == "name"`` literals, then asserts
     every name is present in ``ALL_EVENT_NAMES``. A new event added on
     either side without updating the constants module fires these tests.

The pre-§17.190 audit found ``pipelines/scaffold_router.py`` matching
``"node_started"`` / ``"node_completed"`` — dead-code branches because the
orchestrator emits ``node_start`` / ``node_done``. The consumer scan
would have caught that drift; the test now stands guard against
re-introduction.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.sse_events import ALL_EVENT_NAMES


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "app" / "sse_events.py"
VENDORED = REPO_ROOT / "pipelines" / "_sse_events.py"

EMITTER_FILES = [
    REPO_ROOT / "app" / "modules" / "execution_agent.py",
    REPO_ROOT / "app" / "modules" / "assist_agent.py",
    REPO_ROOT / "app" / "modules" / "research_agent.py",
    REPO_ROOT / "app" / "sim" / "design_pipeline.py",
]
CONSUMER_FILE = REPO_ROOT / "pipelines" / "scaffold_router.py"


# ---------------------------------------------------------------------------
# Byte-equal vendor guard (defense-in-depth alongside `make check-sse-events`)
# ---------------------------------------------------------------------------

def test_sse_events_files_exist():
    # §17.207: the orchestrator's Docker image intentionally omits
    # ``pipelines/`` (the OWUI pipelines container is a separate service
    # with its own bind-mounts); when this test runs INSIDE that image,
    # the vendored copy isn't present and that's expected. The primary
    # CI gate for vendor drift is ``make check-sse-events`` (a host-side
    # ``diff -q`` step in ci.yml), which always runs regardless of which
    # container the test suite lives in. Skip here so the in-suite
    # defense-in-depth doesn't false-fail in container-only test runs.
    if not VENDORED.is_file():
        import pytest
        pytest.skip(
            f"{VENDORED.relative_to(REPO_ROOT)} not present in this image "
            "(expected when running inside the orchestrator container); "
            "the host-side `make check-sse-events` gate covers drift."
        )
    assert SOURCE.is_file(), f"missing {SOURCE}"


def test_sse_events_byte_equal():
    if not VENDORED.is_file():
        import pytest
        pytest.skip(
            f"{VENDORED.relative_to(REPO_ROOT)} not present in this image; "
            "drift is covered by the host-side `make check-sse-events` gate."
        )
    src = SOURCE.read_bytes()
    vendored = VENDORED.read_bytes()
    if src != vendored:
        raise AssertionError(
            f"{VENDORED.relative_to(REPO_ROOT)} has drifted from "
            f"{SOURCE.relative_to(REPO_ROOT)}. Run `make sync-sse-events` "
            "to refresh the vendored copy, then re-run the suite."
        )


# ---------------------------------------------------------------------------
# Emitter / consumer inventory scans
# ---------------------------------------------------------------------------

# Match ``_sse("event_name", ...)`` — captures the literal between the
# parens. Tolerates whitespace around the ``(`` and single or double quotes.
_EMITTER_RE = re.compile(r"""_sse\s*\(\s*["']([a-z_]+)["']""")

# Match ``event_type == "name"`` — the consumer-side string comparison.
_CONSUMER_RE = re.compile(r"""event_type\s*==\s*["']([a-z_]+)["']""")


def _scan(paths: list[Path], pattern: re.Pattern[str]) -> dict[str, list[Path]]:
    """Return {event_name: [paths_where_it_appears]} for the given regex."""
    hits: dict[str, list[Path]] = {}
    for p in paths:
        if not p.exists():
            continue
        for name in pattern.findall(p.read_text()):
            hits.setdefault(name, []).append(p)
    return hits


def test_emitter_event_names_are_in_inventory():
    """Every ``_sse("...")`` literal emitted by the orchestrator must be
    declared in ``app/sse_events.py::ALL_EVENT_NAMES``. A new emitter that
    invents a name without registering it here fails this test, forcing
    the author to either add the constant or use an existing one."""
    emitted = _scan(EMITTER_FILES, _EMITTER_RE)
    unknown = {
        name: [str(p.relative_to(REPO_ROOT)) for p in sorted(set(paths))]
        for name, paths in emitted.items()
        if name not in ALL_EVENT_NAMES
    }
    assert unknown == {}, (
        "Orchestrator emits SSE event names not declared in "
        "app/sse_events.py::ALL_EVENT_NAMES — add the constants or "
        f"rename the literals. Unregistered: {unknown}"
    )


def test_consumer_event_names_are_in_inventory():
    """Every ``event_type == "name"`` match in scaffold_router.py must be
    declared in ``app/sse_events.py::ALL_EVENT_NAMES`` — catches a
    consumer rendering a string the orchestrator never emits (e.g. the
    pre-§17.190 ``node_started`` / ``node_completed`` dead-branch drift)."""
    consumed = _scan([CONSUMER_FILE], _CONSUMER_RE)
    unknown = {
        name: [str(p.relative_to(REPO_ROOT)) for p in sorted(set(paths))]
        for name, paths in consumed.items()
        if name not in ALL_EVENT_NAMES
    }
    assert unknown == {}, (
        "scaffold_router.py matches event_type strings not declared in "
        "app/sse_events.py::ALL_EVENT_NAMES. Likely dead branches from "
        f"renamed events. Unregistered: {unknown}"
    )


def test_inventory_is_non_empty():
    """Sanity check — a future ``ALL_EVENT_NAMES = frozenset()`` regression
    would trivially pass both unknown-name tests above."""
    assert len(ALL_EVENT_NAMES) >= 30, (
        f"ALL_EVENT_NAMES has only {len(ALL_EVENT_NAMES)} entries — "
        "expected at least 30 across emitter modules"
    )


# ---------------------------------------------------------------------------
# Drift parity — every consumed name should have at least one emitter
# ---------------------------------------------------------------------------
# Not asserted as a hard requirement (generic events like ``error`` /
# ``heartbeat`` are produced by the SSE wrapper rather than emitted via
# ``_sse(...)``), but worth surfacing as a list for review.

def test_consumed_names_are_emitter_aware_or_generic():
    """Document the relationship between consumed names and the emitter set.

    A consumed name that has no _sse(...) emitter AND isn't one of the
    allowed documented exceptions should be flagged — likely the same
    drift pattern that the pre-§17.190 ``node_started`` /
    ``node_completed`` bug had.

    Allowed exceptions are explicit:
      * Generic control events (ERROR / WARNING / HEARTBEAT / DONE / QUEUED)
        — produced by the SSE wrapper, not via _sse(...).
      * BLOCKED — emitted from execution_agent via _sse(status, ...) where
        status is a variable; the regex can't see the literal.
      * STREAM_STALLED — synthesized inside scaffold_router itself when N
        keepalives elapse with no real event; consumer-only by design.
    """
    from app import sse_events as ev
    consumed = set(_scan([CONSUMER_FILE], _CONSUMER_RE).keys())
    emitted = set(_scan(EMITTER_FILES, _EMITTER_RE).keys())
    allowed = {
        ev.ERROR, ev.WARNING, ev.HEARTBEAT, ev.DONE, ev.QUEUED,
        ev.BLOCKED, ev.STREAM_STALLED,
    }
    orphaned = consumed - emitted - allowed
    assert orphaned == set(), (
        "scaffold_router.py matches event names that are neither emitted "
        "via _sse(...) nor in the documented allowed-exceptions set. "
        "Likely dead branches from drift — see test docstring for the "
        f"allowed-exception list. Orphaned: {sorted(orphaned)}"
    )
