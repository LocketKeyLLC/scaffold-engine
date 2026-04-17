"""Smoke tests for /schedule command in pipelines/scaffold_router.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "scaffold_router",
    Path(__file__).resolve().parents[1] / "pipelines" / "scaffold_router.py",
)
sr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sr)


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
        out = pipe._handle_schedule("/schedule help")
        assert "Schedule commands" in out

    def test_unknown_sub_returns_help(self, pipe):
        out = pipe._handle_schedule("/schedule bogus")
        assert "Schedule commands" in out


class TestScheduleList:
    def test_list_empty(self, pipe):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"schedules": []}
        with patch("requests.get", return_value=mock_resp):
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
        with patch("requests.get", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule list")
        assert "k8s news" in out and "| 1 |" in out

    def test_list_http_error(self, pipe):
        with patch("requests.get", side_effect=Exception("boom")):
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
        with patch("requests.post", return_value=mock_resp):
            out = pipe._handle_schedule('/schedule add "0 9 * * 1" kubernetes news')
        assert "Scheduled" in out and "#5" in out

    def test_add_invalid_cron_422(self, pipe):
        mock_resp = MagicMock(status_code=422)
        mock_resp.json.return_value = {"detail": "invalid cron expression: bad"}
        with patch("requests.post", return_value=mock_resp):
            out = pipe._handle_schedule('/schedule add "xx" kubernetes')
        assert "invalid cron" in out


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
        with patch("requests.delete", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule delete 3")
        assert "Deleted" in out and "#3" in out

    def test_delete_404(self, pipe):
        mock_resp = MagicMock(status_code=404)
        with patch("requests.delete", return_value=mock_resp):
            out = pipe._handle_schedule("/schedule delete 999")
        assert "not found" in out
