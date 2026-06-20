"""§17.565 — chat surfacing of artifacts: /results section + /artifacts command.

Run: pytest --noconftest tests/test_scaffold_router_artifacts.py
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response


@pytest.fixture
def pipe():
    return Pipeline()


class TestArtifactsSection:
    def test_lists_artifacts(self, pipe):
        body = {"artifacts": [
            {"id": "a1", "artifact_type": "report", "title": "Deliverable",
             "size_bytes": 12},
            {"id": "a2", "artifact_type": "code", "title": "gen", "size_bytes": 5},
        ], "total": 2}
        with patch("scaffold_router._HTTP_SESSION.get",
                   return_value=_make_response(200, body)):
            out = pipe._artifacts_section("job-1")
        assert "📦 Artifacts" in out
        assert "[report]" in out and "[code]" in out
        assert "/artifacts a1" in out

    def test_empty_returns_blank(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get",
                   return_value=_make_response(200, {"artifacts": [], "total": 0})):
            assert pipe._artifacts_section("job-1") == ""

    def test_error_returns_blank(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get",
                   return_value=_make_response(500, "err")):
            assert pipe._artifacts_section("job-1") == ""


class TestHandleArtifacts:
    def test_fetch_single(self, pipe):
        body = {"id": "a1", "artifact_type": "code", "title": "gen",
                "size_bytes": 5, "content": "print(1)"}
        with patch("scaffold_router._HTTP_SESSION.get",
                   return_value=_make_response(200, body)):
            out = pipe._handle_artifacts(["/artifacts", "a1"])
        assert "gen" in out and "print(1)" in out and "```python" in out

    def test_usage_when_no_arg(self, pipe):
        assert "Usage" in pipe._handle_artifacts(["/artifacts"])

    def test_fallback_to_job_list(self, pipe):
        list_body = {"artifacts": [
            {"id": "a1", "artifact_type": "report", "title": "D", "size_bytes": 3},
        ], "total": 1}
        # /artifacts/{id} → 404, then /jobs/{id}/artifacts → 200 list.
        with patch("scaffold_router._HTTP_SESSION.get",
                   side_effect=[_make_response(404, "nf"),
                                _make_response(200, list_body)]):
            out = pipe._handle_artifacts(["/artifacts", "job-1"])
        assert "Artifacts" in out and "/artifacts a1" in out

    def test_artifacts_is_core_command(self):
        from scaffold_router import _CORE_COMMANDS, KNOWN_COMMANDS
        assert "/artifacts" in _CORE_COMMANDS
        assert "/artifacts" in KNOWN_COMMANDS
