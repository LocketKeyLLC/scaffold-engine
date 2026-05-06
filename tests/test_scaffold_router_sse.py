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
        # The connect timeout stays at 30s; the read timeout now tracks
        # ``valves.keepalive_interval`` (with a 30s lower bound) so each
        # ReadTimeout cycle covers exactly one heartbeat window. With the
        # default keepalive_interval=10, that bottoms out at 30.
        with patch("scaffold_router.requests.post") as mp:
            resp = MagicMock(status_code=200)
            resp.iter_lines.return_value = iter([])
            mp.return_value = resp
            q = _queue.Queue()
            pipe._stream_sse_to_queue("http://x/y", {}, q)
            kw = mp.call_args.kwargs
            connect, read = kw["timeout"]
            assert connect == 30
            assert read == max(30, pipe.valves.keepalive_interval)
            assert kw["stream"] is True

