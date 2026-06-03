"""Smoke tests for /schedule command in pipelines/scaffold_router.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROUTER_PATH = Path(__file__).resolve().parents[1] / "pipelines" / "scaffold_router.py"
if not _ROUTER_PATH.exists():
    pytest.skip(
        f"scaffold_router.py not found at {_ROUTER_PATH} — skipping (expected in pipelines/ directory)",
        allow_module_level=True,
    )

SPEC = importlib.util.spec_from_file_location("scaffold_router", _ROUTER_PATH)
sr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sr)

# §17.402 — stub the Ollama embedder probe (matches §17.333 in
# tests/_scaffold_router_setup.py). Pipeline.__init__ POSTs to Ollama at
# 172.18.0.1, unroutable on cloud CI (test.yml) → these tests errored there.
# Live embedder-dim verification lives in /health + tests/integration/.
sr.Pipeline._probe_embedder_dim = lambda self, model=None: (True, "test stub (§17.402)")


@pytest.fixture
def pipe():
    p = sr.Pipeline()
    p.valves.api_key = "test-key"
    p.valves.orchestrator_url = "http://test"
    return p


class TestScheduleHelp:
    def test_no_args_returns_help(self, pipe):
        out = pipe._handle_schedule("/schedule")
        assert "list" in out and "add" in out and "delete" in out

    def test_help_subcommand(self, pipe):
        """§17.312 — help renamed to "Recurring research crons" + a
        table-format reference. The pre-§17.312 heading "Schedule
        commands" is gone. Loosen the assertion to just `/schedule`
        + at least one subcommand row, which both shapes satisfy."""
        out = pipe._handle_schedule("/schedule help")
        assert "/schedule" in out
        # At least one of the canonical subcommands must be named.
        assert "/schedule list" in out
        assert "/schedule add" in out
        assert "/schedule delete" in out

    def test_unknown_sub_returns_help(self, pipe):
        """Unknown sub should emit a short pointer to `/schedule help` rather
        than dumping the full table; regression contract is just that the
        user gets a discoverable next step, not a silent no-op."""
        out = pipe._handle_schedule("/schedule bogus")
        assert "Unknown" in out or "/schedule help" in out


class TestScheduleList:
    def test_list_empty(self, pipe):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"schedules": []}
        with patch.object(sr._HTTP_SESSION, "get", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule list")
        assert "No schedules yet" in out

    def test_list_renders_table(self, pipe):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"schedules": [{
            "id": 1, "topic": "k8s news", "depth": "medium",
            "cron_expression": "0 9 * * 1", "next_run_at": "2026-04-21T09:00:00+00:00",
            "run_count": 0, "failure_count": 0,
        }]}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(sr._HTTP_SESSION, "get", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule list")
        assert "k8s news" in out and "| 1 |" in out

    def test_list_http_error(self, pipe):
        with patch.object(sr._HTTP_SESSION, "get", side_effect=Exception("boom")):
            out = pipe._handle_schedule("/schedule list")
        assert "Failed to list" in out


class TestScheduleAdd:
    def test_add_missing_args(self, pipe):
        out = pipe._handle_schedule("/schedule add")
        assert "Usage" in out

    def test_add_single_token(self, pipe):
        out = pipe._handle_schedule('/schedule add "0 9 * * 1"')
        assert "Usage" in out

    def test_add_success(self, pipe):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "id": 5, "topic": "kubernetes news",
            "cron_expression": "0 9 * * 1", "depth": "medium",
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(sr._HTTP_SESSION, "post", return_value=mock_resp):
            out = pipe._handle_schedule('/schedule add "0 9 * * 1" kubernetes news')
        assert "Scheduled" in out and "#5" in out

    def test_add_invalid_cron_422(self, pipe):
        mock_resp = MagicMock(status_code=422)
        mock_resp.json.return_value = {"detail": "invalid cron expression: bad"}
        with patch.object(sr._HTTP_SESSION, "post", return_value=mock_resp):
            out = pipe._handle_schedule('/schedule add "xx" kubernetes')
        assert "invalid cron" in out

    def test_add_passes_tz_to_endpoint(self, pipe):
        """Sprint U.7 / F4: --tz parsed and forwarded as `timezone` in body.
        Previously the parser only knew --depth, so users passing --tz hit
        a parse error or had it silently absorbed as a positional."""
        captured = {}

        def _capture_post(url, headers=None, timeout=None, json=None):
            captured["json"] = json
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "id": 7, "topic": "ny news",
                "cron_expression": "0 9 * * 1", "depth": "medium",
                "timezone": "America/New_York",
            }
            resp.raise_for_status = MagicMock()
            return resp

        with patch.object(sr._HTTP_SESSION, "post", side_effect=_capture_post):
            out = pipe._handle_schedule(
                '/schedule add "0 9 * * 1" --tz=America/New_York ny news'
            )

        assert captured["json"]["timezone"] == "America/New_York"
        assert captured["json"]["cron_expression"] == "0 9 * * 1"
        assert captured["json"]["topic"] == "ny news"
        assert "America/New_York" in out

    def test_add_default_tz_is_utc(self, pipe):
        """When --tz is omitted, schedule still ships timezone=UTC so the
        server doesn't have to default-handle a missing field."""
        captured = {}

        def _capture_post(url, headers=None, timeout=None, json=None):
            captured["json"] = json
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "id": 8, "topic": "k", "cron_expression": "0 9 * * 1",
                "depth": "medium", "timezone": "UTC",
            }
            resp.raise_for_status = MagicMock()
            return resp

        with patch.object(sr._HTTP_SESSION, "post", side_effect=_capture_post):
            pipe._handle_schedule('/schedule add "0 9 * * 1" k')

        assert captured["json"]["timezone"] == "UTC"


class TestScheduleDelete:
    def test_delete_missing_id(self, pipe):
        out = pipe._handle_schedule("/schedule delete")
        assert "Usage" in out

    def test_delete_non_numeric(self, pipe):
        out = pipe._handle_schedule("/schedule delete abc")
        assert "Usage" in out

    def test_delete_success(self, pipe):
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        with patch.object(sr._HTTP_SESSION, "delete", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule delete 3")
        assert "Deleted" in out and "#3" in out

    def test_delete_404(self, pipe):
        mock_resp = MagicMock(status_code=404)
        with patch.object(sr._HTTP_SESSION, "delete", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule delete 999")
        assert "not found" in out
