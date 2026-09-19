"""§17.1114 (Phase 1 ledger U-7) — the hub header's status pill is fed by one
event, `scaffold:job-status`, dispatched by whichever view changes a job's
status in place. The listener existed (job_hub.js) and exactly one dispatcher
(assist.js), so an autonomous run finished under a header that still said
"running". This pins the pair: the hub listens, and every view that refreshes
its own status pill after a run also dispatches. Static, ci-tier-0.
"""
from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"
EVENT = "scaffold:job-status"


def _src(name: str) -> str:
    return (_STATIC / "views" / name).read_text(encoding="utf-8")


def test_hub_listens():
    assert f'addEventListener("{EVENT}"' in _src("job_hub.js")
    assert f'removeEventListener("{EVENT}"' in _src("job_hub.js"), "and stops listening on dispose"


def test_every_view_that_updates_a_job_status_pill_dispatches():
    """A view that calls setStatusPill(...) after a run owns the freshest job
    status; the hub header must hear about it."""
    dispatchers = {name for name in ("theater.js", "assist.js")
                   if f'dispatchEvent(new CustomEvent("{EVENT}"' in _src(name)}
    updaters = {js.name for js in (_STATIC / "views").glob("*.js")
                if "setStatusPill(" in js.read_text(encoding="utf-8")}
    updaters |= {"assist.js"}   # its completion card finishes the job in place (§17.1052)
    missing = sorted(updaters - dispatchers)
    assert not missing, f"views that refresh a job status pill but never dispatch {EVENT}: {missing}"
