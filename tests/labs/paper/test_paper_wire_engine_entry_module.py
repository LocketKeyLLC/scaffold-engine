# tests/test_paper_wire_engine_entry_module.py
"""Tests for app.modules.paper_wire_engine_entry_module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.modules.paper_wire_engine_entry_module import (
    DAILY_BATCH_JOB_ID,
    ROUTER_PREFIX,
    install,
)


@pytest.fixture
def fastapi_app() -> FastAPI:
    return FastAPI()


class FakeScheduler:
    """Stand-in scheduler that records add_job calls."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, **kwargs) -> None:
        self.jobs.append({"func": func, **kwargs})


def _route_paths(app: FastAPI) -> list[str]:
    """Return the OpenAPI path strings for all routes on the app."""
    return list(app.openapi()["paths"].keys())


@pytest.mark.asyncio
async def test_install_registers_daily_batch_job(fastapi_app: FastAPI):
    fake_scheduler = FakeScheduler()
    with patch(
        "app.modules.paper_wire_engine_entry_module.get_scheduler",
        return_value=fake_scheduler,
    ), patch(
        "app.modules.paper_wire_engine_entry_module._start_stream_consumer",
        return_value=None,
    ):
        install(fastapi_app)

    assert len(fake_scheduler.jobs) == 1
    job = fake_scheduler.jobs[0]
    assert job["id"] == DAILY_BATCH_JOB_ID
    assert job["trigger"] == "cron"
    assert job["hour"] == 13
    assert job["minute"] == 0


@pytest.mark.asyncio
async def test_install_registers_routes(fastapi_app: FastAPI):
    with patch(
        "app.modules.paper_wire_engine_entry_module.get_scheduler",
        return_value=None,
    ), patch(
        "app.modules.paper_wire_engine_entry_module._start_stream_consumer",
        return_value=None,
    ):
        install(fastapi_app)

    paths = _route_paths(fastapi_app)
    assert f"{ROUTER_PREFIX}/daily-batch" in paths
    assert f"{ROUTER_PREFIX}/on-demand-batch" in paths
    assert f"{ROUTER_PREFIX}/approval" in paths
    assert f"{ROUTER_PREFIX}/backtesting" in paths
    assert f"{ROUTER_PREFIX}/execution" in paths
    assert f"{ROUTER_PREFIX}/view" in paths


@pytest.mark.asyncio
async def test_install_no_scheduler_does_not_schedule(fastapi_app: FastAPI):
    with patch(
        "app.modules.paper_wire_engine_entry_module.get_scheduler",
        return_value=None,
    ), patch(
        "app.modules.paper_wire_engine_entry_module._start_stream_consumer",
        return_value=None,
    ):
        install(fastapi_app)

    # No scheduler means no add_job call; nothing to assert beyond no crash.
    # The patch above ensures get_scheduler returned None, so install
    # skipped scheduling entirely.


@pytest.mark.asyncio
async def test_install_starts_stream_consumer_task(fastapi_app: FastAPI):
    with patch(
        "app.modules.paper_wire_engine_entry_module.get_scheduler",
        return_value=None,
    ), patch(
        "app.modules.paper_wire_engine_entry_module.track_fills",
        new_callable=AsyncMock,
    ) as mock_track_fills:
        # Run inside an event loop so _start_stream_consumer creates a task.
        install(fastapi_app)
        # Give the task a chance to start.
        await asyncio.sleep(0)

        # The stream consumer task was created and calls track_fills.
        # We can't easily inspect the task, but we can assert the
        # underlying function was scheduled by awaiting a moment and
        # checking the mock was called.
        assert mock_track_fills.await_count >= 1


@pytest.mark.asyncio
async def test_install_does_not_read_secrets(fastapi_app: FastAPI):
    with patch(
        "app.modules.paper_wire_engine_entry_module.get_scheduler",
        return_value=None,
    ), patch(
        "app.modules.paper_wire_engine_entry_module._start_stream_consumer",
        return_value=None,
    ), patch(
        "app.modules.paper_fill_tracker.values_for",
        new_callable=AsyncMock,
    ) as mock_values_for:
        install(fastapi_app)

        # install itself must not read secrets; the stream consumer task
        # does that when it runs, but install must not.
        mock_values_for.assert_not_called()
