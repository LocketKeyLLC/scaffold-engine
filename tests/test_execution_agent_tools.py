"""Tests for execution_agent — tool-specific error handling (SearXNG, Milvus) + skip_node return shape.

Split from the original test_execution_agent.py (#9.6). Shared imports
and helpers live in _execution_agent_shared.
"""
from tests._execution_agent_shared import *  # noqa: F401, F403

@pytest.mark.smoke
class TestSearXNGSearchErrorHandling:
    """_searxng_search graceful degradation on failures.

    _searxng_search lazy-imports get_searxng_client inside the function,
    so the correct patch target is its source module, not execution_agent.
    """
    @staticmethod
    def _mock_client(*, response=None, side_effect=None):
        client = AsyncMock()
        if side_effect is not None:
            client.get = AsyncMock(side_effect=side_effect)
        else:
            client.get = AsyncMock(return_value=response)
        return client

    async def test_http_error_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "failed" in result.lower()

    async def test_timeout_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        client = self._mock_client(side_effect=httpx.TimeoutException("timed out"))
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "failed" in result.lower()

    async def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "no search results" in result.lower()

    async def test_success_formats_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [
            {"title": "Result 1", "content": "Snippet 1", "url": "https://example.com"},
        ]}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = await _searxng_search("test query")
        assert "[1] Result 1" in result
        assert "Snippet 1" in result


@pytest.mark.smoke
class TestMilvusSearchErrorHandling:
    """_milvus_search graceful degradation on failures."""

    async def test_connection_error_returns_failure_string(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(side_effect=ConnectionError("Milvus unreachable"))

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "failed" in result.lower()

    async def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": []})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "no knowledge base results" in result.lower()

    async def test_success_formats_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": [
            {"title": "RAG Architecture", "content": "Retrieval-augmented generation..."},
            {"title": "Embeddings", "content": "Vector representations..."},
        ]})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = await _milvus_search("test query")
        assert "[1] RAG Architecture" in result
        assert "[2] Embeddings" in result
        assert "Retrieval-augmented" in result


@pytest.mark.smoke
class TestSkipNodeReturnShape:
    """#95: skip_node return dict conforms to ExecutionResult schema."""

    async def test_skipped_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = {"id": "node-uuid-1"}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)
        db.commit = AsyncMock()

        result = await skip_node("job-1", "T1", db)

        validated = ExecutionResult(**result)
        assert validated.status == "skipped"
        assert validated.node_key == "T1"

    async def test_not_found_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)

        result = await skip_node("job-1", "T99", db)

        validated = ExecutionResult(**result)
        assert validated.status == "error"
        assert validated.message is not None
        assert "not found" in validated.message.lower()
