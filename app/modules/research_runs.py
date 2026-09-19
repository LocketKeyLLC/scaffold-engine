"""§17.1120 — detached research runs, on the same broker as execution runs.

The streaming ``POST /research`` tied the research session's life to the
response: a client disconnect propagated ``CancelledError`` into the generator
and the session finalized as ``cancelled`` — so a sidebar click killed a
20-minute run (Phase 1 ledger U-6; §17.1116 could only ask first). The
fire-and-forget ``POST /research/start`` survived disconnects but returned no
handle to follow. This module gives research what ``run_broker`` gave
execution in §17.1007: a run keyed by an unguessable ``run_id`` that the SPA
starts, tails, reconnects to, cancels on purpose, and re-attaches to after a
reload — with the broker's bounded backlog + Redis-persisted frames.

The broker is keyed by string; research runs use ``research:<run_id>`` so they
never collide with a job's ``execute/all`` run, and ``reconcile_on_startup``
(jobs only) never touches them.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

from app.modules import run_broker

logger = logging.getLogger("scaffold.research_runs")

#: run_id → {owner, kind, label, started_at}. In-process, like the broker's
#: live runs; after a restart only the Redis frame log survives (see `visible`).
_META: dict[str, dict[str, Any]] = {}
_META_MAX = 500


def key(run_id: str) -> str:
    return f"research:{run_id}"


def start(source_factory: Callable[[], AsyncIterator[str]], *, owner: str | None,
          kind: str = "research", label: str = "") -> str:
    """Start a detached research run; returns its run_id immediately."""
    run_id = str(uuid4())
    run_broker.start(key(run_id), source_factory)
    if len(_META) >= _META_MAX:
        for k in sorted(_META, key=lambda k: _META[k]["started_at"])[: len(_META) - _META_MAX + 1]:
            _META.pop(k, None)
    _META[run_id] = {"owner": owner, "kind": kind, "label": label[:120], "started_at": time.time()}
    logger.info("research_run_started run_id=%s kind=%s owner=%s label=%r", run_id, kind, owner, label[:80])
    return run_id


def meta(run_id: str) -> dict[str, Any] | None:
    return _META.get(run_id)


def visible(run_id: str, principal: Any) -> bool:
    """Ownership gate. A run started by another identity is invisible unless
    the caller is an admin. A run this process has no record of (started
    before a restart; only its Redis frames remain) is readable by any
    authenticated caller — the id is an unguessable uuid and the frames carry
    no more than the session list already shows."""
    m = _META.get(run_id)
    if m is None:
        return True
    if getattr(principal, "role", None) == "admin":
        return True
    return m.get("owner") == getattr(principal, "identity", None)


def is_running(run_id: str) -> bool:
    return run_broker.is_running(key(run_id))


def get_run(run_id: str):
    return run_broker.get_run(key(run_id))


async def replay(run_id: str) -> list[str]:
    return await run_broker.replay(key(run_id))


async def cancel(run_id: str) -> bool:
    ok = await run_broker.cancel(key(run_id))
    logger.info("research_run_cancel run_id=%s cancelled=%s", run_id, ok)
    return ok


def reset() -> None:
    """Test hook."""
    _META.clear()
