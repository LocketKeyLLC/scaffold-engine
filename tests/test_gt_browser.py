"""
tests/test_gt_browser.py — Smoke tests for gt_browser pipeline field mappings.
Verifies that the Prompt 3 fixes (Issues 40, 41) use correct field names.

Run:
    python3 -m pytest tests/test_gt_browser.py --noconftest -v
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

_candidates = [
    Path(__file__).resolve().parent.parent / "pipelines" / "gt_browser.py",
    Path("/app/pipelines/gt_browser.py"),
]
_path = None
for _p in _candidates:
    if _p.exists():
        _path = _p
        break
if _path is None:
    pytest.skip("gt_browser.py not found", allow_module_level=True)

spec = importlib.util.spec_from_file_location("gt_browser", _path)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
Pipeline = _mod.Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


@pytest.mark.smoke
class TestFieldMappings:
    """Verify Prompt 3 fixes: field names match orchestrator schemas."""

    def test_handle_list_uses_title(self, pipe):
        """_handle_list reads 'title', not 'topic'."""
        fake_data = {
            "total": 1, "total_pages": 1,
            "entries": [{"entry_id": "e1", "title": "My Title", "tags": "t1", "snippet": "snip"}],
        }
        with patch.object(pipe, "_call", return_value=fake_data):
            result = pipe._handle_list("1")
        assert "My Title" in result
        # Header says "Topic" (display label) but data reads from "title" key - verified by "My Title" appearing above

    def test_handle_search_uses_title(self, pipe):
        """_handle_search reads 'title', not 'topic'."""
        fake_data = {
            "results": [{"entry_id": "e1", "title": "Search Hit", "score": 0.95, "snippet": "snip"}],
        }
        with patch.object(pipe, "_call", return_value=fake_data):
            result = pipe._handle_search("test query")
        assert "Search Hit" in result

    def test_handle_stats_uses_domains(self, pipe):
        """_handle_stats reads 'domains' and 'source_types', not 'topics'/'source_files'."""
        fake_data = {
            "total_entries": 42,
            "domains": {"eng": 30, "rag": 12},
            "tags": {"python": 5},
            "source_types": {"tech_docs": 20, "news": 22},
        }
        with patch.object(pipe, "_call", return_value=fake_data):
            result = pipe._handle_stats()
        assert "42" in result
        assert "eng" in result
        assert "tech_docs" in result
