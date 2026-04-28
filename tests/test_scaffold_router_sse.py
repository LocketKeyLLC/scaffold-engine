"""Tests for scaffold_router.py — SSE streaming edge cases (error events, stalled streams).

Split from the original test_scaffold_router.py (#9.6).
Shared module-loading logic lives in _scaffold_router_setup.py.
"""
import json
import queue as _queue
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import _mod, Pipeline


@pytest.fixture
def pipe():
    """Fresh Pipeline instance per test."""
    return Pipeline()


@pytest.mark.smoke
class TestSSEErrorEventRendering:
    """#8.2: error SSE events render with error prefix and optional traceback fence."""

    def test_error_with_traceback(self, pipe):
        failed = []
        data = json.dumps({"message": "boom", "traceback": "Traceback: line 42"})
        out = "".join(pipe._handle_sse_event("error", data, failed))
        assert "Execution error" in out
        assert "boom" in out
        assert "Traceback: line 42" in out
        assert "```traceback" in out
        assert len(failed) == 1

    def test_error_without_traceback(self, pipe):
        failed = []
        data = json.dumps({"error": "something went wrong"})
        out = "".join(pipe._handle_sse_event("error", data, failed))
        assert "something went wrong" in out
        assert "```traceback" not in out


@pytest.mark.smoke
class TestSSEStreamStalled:
    """#8.12: Stream stall detected; reader emits stream_stalled event."""

    def test_stream_stalled_event_renders_warning(self, pipe):
        def fake_streamer(url, body, q):
            q.put(("connected", None, None))
            q.put(("event", "stream_stalled",
                   json.dumps({"idle_seconds": 50, "max_idle": 50})))
            q.put(("done", None, None))

        with patch.object(pipe, "_stream_sse_to_queue", side_effect=fake_streamer):
            out = "".join(pipe._execute_and_stream("job_test", 0))
        assert "stalled" in out.lower()
        assert "50" in out

    def test_per_read_timeout_tuple_passed_to_requests(self, pipe):
        # The connect/read timeouts in _stream_sse_to_queue are currently
        # hardcoded as (30, 120) on line 660 of scaffold_router.py. This test
        # asserts that pair so any unintended change is caught.
        #
        # TODO(future): wire (30, 120) to valves (request_timeout for connect,
        # a new sse_read_timeout for read) so admins can tune without editing
        # source. Keep this test in lockstep when that lands.
        with patch("scaffold_router.requests.post") as mp:
            resp = MagicMock(status_code=200)
            resp.iter_lines.return_value = iter([])
            mp.return_value = resp
            q = _queue.Queue()
            pipe._stream_sse_to_queue("http://x/y", {}, q)
            kw = mp.call_args.kwargs
            assert kw["timeout"] == (30, 120)
            assert kw["stream"] is True

