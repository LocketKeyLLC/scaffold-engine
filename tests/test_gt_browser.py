"""
tests/test_gt_browser.py — Smoke tests for gt_browser pipeline.

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


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    resp.text = text
    return resp


@pytest.mark.smoke
class TestFieldMappings:
    def test_handle_list_uses_title(self, pipe):
        fake = {"total": 1, "total_pages": 1,
                "entries": [{"entry_id": "e1", "title": "My Title", "tags": "t1", "snippet": "snip"}]}
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_list("1")
        assert "My Title" in result

    def test_handle_search_uses_title(self, pipe):
        fake = {"results": [{"entry_id": "e1", "title": "Search Hit", "score": 0.95, "snippet": "snip"}]}
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_search("q")
        assert "Search Hit" in result

    def test_handle_stats_uses_domains(self, pipe):
        fake = {"total_entries": 42, "domains": {"eng": 30},
                "tags": {"python": 5}, "source_types": {"tech_docs": 20}}
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_stats()
        assert "42" in result
        assert "eng" in result
        assert "tech_docs" in result


@pytest.mark.smoke
class TestDuplicateSourceUrl:
    def test_detail_renders_source_url_once(self, pipe):
        fake = {
            "entry_id": "TOON-042",
            "title": "Test Entry",
            "tags": "a, b",
            "source_url": "https://example.com/unique-url-token",
            "content": "Body text here.",
        }
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_detail("TOON-042")
        assert result.count("https://example.com/unique-url-token") == 1, result
        assert "**URL:**" not in result
        assert "**Source:**" in result


@pytest.mark.smoke
class TestNotFoundCascade:
    def test_detail_404_returns_clean_message(self, pipe):
        err = {"_error": "HTTP 404: entry bogus not found", "_status_code": 404}
        with patch.object(pipe, "_call", return_value=err):
            result = pipe._handle_detail("bogus")
        assert "Entry not found" in result
        assert "bogus" in result
        assert "Traceback" not in result

    def test_detail_500_still_surfaces_error(self, pipe):
        err = {"_error": "HTTP 500: backend on fire", "_status_code": 500}
        with patch.object(pipe, "_call", return_value=err):
            result = pipe._handle_detail("anything")
        assert "Entry not found" not in result
        assert "500" in result


@pytest.mark.smoke
class TestPaginationHints:
    def _fake(self, per_page, total, total_pages):
        return {"total": total, "total_pages": total_pages,
                "entries": [{"entry_id": f"e{i}", "title": f"T{i}", "tags": "", "snippet": ""}
                            for i in range(per_page)]}

    def test_page1_no_previous_hint(self, pipe):
        fake = {"total": 5, "total_pages": 1,
                "entries": [{"entry_id": f"e{i}", "title": f"T{i}", "tags": "", "snippet": ""}
                            for i in range(5)]}
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_list("1")
        assert "Previous" not in result
        assert "Next" not in result

    def test_page2_shows_previous_and_next(self, pipe):
        pp = pipe.valves.per_page
        with patch.object(pipe, "_call", return_value=self._fake(pp, pp * 3, 3)):
            result = pipe._handle_list("2")
        assert "Previous" in result and "/gt list 1" in result
        assert "Next" in result and "/gt list 3" in result

    def test_next_hint_when_more_exist(self, pipe):
        pp = pipe.valves.per_page
        with patch.object(pipe, "_call", return_value=self._fake(pp, pp * 2 + 5, 3)):
            result = pipe._handle_list("1")
        assert "Next" in result and "/gt list 2" in result

    def test_last_page_no_next(self, pipe):
        pp = pipe.valves.per_page
        with patch.object(pipe, "_call", return_value=self._fake(pp, pp * 2, 2)):
            result = pipe._handle_list("2")
        assert "Next" not in result
        assert "Previous" in result


@pytest.mark.smoke
class TestPerPageValve:
    def test_per_page_valve_exists_with_default(self, pipe):
        assert hasattr(pipe.valves, "per_page")
        assert pipe.valves.per_page == 20

    def test_per_page_valve_flows_to_api_params(self, pipe):
        pipe.valves.per_page = 5
        captured = {}

        def fake_call(method, path, params=None, json_body=None):
            captured["params"] = params
            return {"total": 20, "total_pages": 4,
                    "entries": [{"entry_id": "e", "title": "T", "tags": "", "snippet": ""}]}

        with patch.object(pipe, "_call", side_effect=fake_call):
            pipe._handle_list("1")
        assert captured["params"]["per_page"] == 5

    def test_per_page_valve_drives_offset_math(self, pipe):
        pipe.valves.per_page = 5
        fake = {"total": 100, "total_pages": 20,
                "entries": [{"entry_id": "e1", "title": "T1", "tags": "", "snippet": ""}]}
        with patch.object(pipe, "_call", return_value=fake):
            result = pipe._handle_list("3")
        assert "| 11 |" in result


@pytest.mark.smoke
class TestHttpLibrary:
    def test_uses_requests_get(self, pipe):
        fake_resp = _mock_response(200, {"total": 0, "total_pages": 0, "entries": []})
        with patch.object(_mod._HTTP_SESSION, "get", return_value=fake_resp) as mock_get:
            pipe._call("GET", "/gt/list", params={"page": 1, "per_page": 20})
        mock_get.assert_called_once()

    def test_uses_requests_post(self, pipe):
        fake_resp = _mock_response(200, {"results": []})
        with patch.object(_mod._HTTP_SESSION, "post", return_value=fake_resp) as mock_post:
            pipe._call("POST", "/gt/search", json_body={"query": "x", "top_k": 10})
        mock_post.assert_called_once()

    def test_non_json_response_handled(self, pipe):
        fake_resp = _mock_response(502, json_data=None, text="<html>Bad Gateway</html>")
        with patch.object(_mod._HTTP_SESSION, "get", return_value=fake_resp):
            data = pipe._call("GET", "/gt/stats")
        assert "_error" in data
        assert "non-JSON" in data["_error"]
        assert "502" in data["_error"]

    def test_404_returns_status_code(self, pipe):
        fake_resp = _mock_response(404, {"detail": "entry not found"})
        with patch.object(_mod._HTTP_SESSION, "get", return_value=fake_resp):
            data = pipe._call("GET", "/gt/detail/bogus")
        assert data.get("_status_code") == 404
        assert "_error" in data

    def test_no_httpx_in_source(self):
        src = _path.read_text()
        assert "import httpx" not in src
        assert "httpx." not in src
