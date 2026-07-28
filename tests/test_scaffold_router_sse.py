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
    """#8.2 / §17.675: error SSE events render a clear cause + recovery hint,
    and NEVER dump a raw Python traceback into the chat bubble."""

    def test_error_with_traceback_is_not_dumped(self, pipe):
        failed = []
        data = json.dumps({"message": "boom", "traceback": "Traceback: line 42"})
        out = "".join(pipe._handle_sse_event("error", data, failed))
        assert "Execution error" in out
        assert "boom" in out
        # §17.675 — the raw traceback must NOT reach the chat.
        assert "Traceback: line 42" not in out
        assert "```traceback" not in out
        assert "/results" in out  # actionable recovery hint
        assert len(failed) == 1

    def test_generic_message_falls_back_to_traceback_last_line(self, pipe):
        # §17.675 — when the message is generic, derive a cause from the final
        # traceback line (still no fence, still no full dump).
        failed = []
        data = json.dumps({
            "error": "unknown error",
            "traceback": "Traceback (most recent call last):\n  ...\nValueError: bad port",
        })
        out = "".join(pipe._handle_sse_event("error", data, failed))
        assert "ValueError: bad port" in out
        assert "```traceback" not in out

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
        # §17.262 — fake_streamer now accepts **kwargs (stop_event, r_holder)
        # because consumers thread the early-exit signals through to the
        # reader. The fixture ignores them; production reader uses them.
        def fake_streamer(url, body, q, **kwargs):
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
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
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


@pytest.mark.smoke
class TestSSEEarlyExitTeardown:
    """§17.262 — when the consumer generator exits early (client disconnect
    → GeneratorExit, or any return/raise inside the yield loop), the daemon
    reader thread must shut down via the stop_event + r.close() signals.
    Pre-fix, reader.join was outside try/finally and never ran on early
    exit; reader stayed alive until the 24h SSE timeout. Closes 17.258 yellow #1."""

    def test_consumer_signals_stop_on_generator_close(self, pipe):
        """When the consumer's generator is .close()'d mid-stream, the
        finally must fire — setting stop_event and closing the response."""
        import threading
        captured = {}

        def fake_streamer(url, body, q, **kwargs):
            # Capture the stop_event + r_holder for post-close assertions.
            captured["stop_event"] = kwargs.get("stop_event")
            captured["r_holder"] = kwargs.get("r_holder")
            # Populate r_holder with a fake response so the consumer's
            # finally calls close() on it.
            fake_response = MagicMock()
            kwargs["r_holder"].append(fake_response)
            captured["fake_response"] = fake_response
            # Emit one event so the consumer yields once, then sit idle
            # (the test will .close() the generator before we'd otherwise
            # produce more output).
            q.put(("connected", None, None))
            q.put(("event", "research_started",
                   json.dumps({"depth": "shallow", "max_iterations": 1})))
            # Block on stop_event so the daemon reader emulates a long-running
            # producer that exits only when signalled. reader.join(timeout=5)
            # in the consumer's finally will see this exit.
            if kwargs["stop_event"] is not None:
                kwargs["stop_event"].wait(timeout=10)

        with patch.object(pipe, "_stream_sse_to_queue", side_effect=fake_streamer):
            gen = pipe._research_and_stream_raw("/research", {"topic": "x"})
            # Pull one chunk to drive the generator into the loop, then close.
            chunks = []
            for chunk in gen:
                chunks.append(chunk)
                if "Depth" in chunk:  # research_started rendered
                    break
            gen.close()  # forces GeneratorExit → consumer's finally fires

        # Post-close: stop_event must be set and fake_response.close() called.
        assert captured["stop_event"] is not None, "stop_event must be threaded through"
        assert captured["stop_event"].is_set(), \
            "consumer's finally must signal stop on GeneratorExit"
        assert captured["fake_response"].close.called, \
            "consumer's finally must close r_holder[0] on early exit"

    def test_stream_sse_to_queue_honors_stop_event_in_read_timeout(self, pipe):
        """§17.262 reader-side guard: when stop_event is set, the next
        ReadTimeout cycle must emit ('done', None, None) and return,
        rather than incrementing idle_seconds + emitting heartbeat."""
        import threading
        import requests
        stop_event = threading.Event()
        stop_event.set()  # pre-set: first ReadTimeout cycle must exit

        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            resp = MagicMock(status_code=200)
            # Make iter_lines raise ReadTimeout each call. With stop_event
            # already set, the very first ReadTimeout must short-circuit
            # the loop instead of emitting a heartbeat.
            resp.iter_lines.side_effect = requests.exceptions.ReadTimeout()
            mp.return_value = resp
            q = _queue.Queue()
            pipe._stream_sse_to_queue("http://x/y", {}, q, stop_event=stop_event)

        # Drain queue and check we got ('connected', ...) then ('done', ...)
        # — NOT a 'heartbeat' (which is what the pre-§17.262 path emitted).
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        msg_types = [m[0] for m in msgs]
        assert "done" in msg_types, f"expected 'done' on stop_event short-circuit; got: {msg_types}"
        assert "heartbeat" not in msg_types, \
            f"stop_event short-circuit must skip heartbeat; got: {msg_types}"

