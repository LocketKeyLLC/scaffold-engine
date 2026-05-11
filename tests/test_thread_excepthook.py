"""Verify the §17.116 thread-exception capture hook in conftest.py.

Deliberately raises from a worker thread; asserts the global
``threading.excepthook`` has been overridden by the autouse fixture and
that calling it appends a traceback to the log file.
"""
from __future__ import annotations

import os
import threading
import time

import pytest


@pytest.mark.smoke
def test_threading_excepthook_overridden():
    """The session-autouse fixture in conftest.py replaces
    ``threading.excepthook`` with the logging variant."""
    hook = threading.excepthook
    # The default hook is ``threading._make_invoke_excepthook``'s output —
    # not a directly nameable identifier, but it lives in the threading
    # module. Our replacement is a closure defined in conftest.py.
    assert hook.__qualname__ != "_make_invoke_excepthook.<locals>.invoke_excepthook", (
        "threading.excepthook still points at the default; "
        "_capture_thread_exceptions fixture didn't install our hook"
    )
    # Loose check: the closure should at least be a callable not in stdlib.
    assert "conftest" in (getattr(hook, "__module__", "") or "") or \
           hook.__name__ == "_hook", \
        f"hook looks foreign: {hook!r}"


@pytest.mark.smoke
def test_thread_exception_lands_in_log():
    """Spawning a thread that raises should append a TRACEBACK block to
    the log file ``/tmp/.pytest_thread_exceptions.log``."""
    log_path = "/tmp/.pytest_thread_exceptions.log"
    before_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    marker = f"§17.116-marker-{time.time_ns()}"

    def _raise():
        raise RuntimeError(marker)

    t = threading.Thread(target=_raise, name=f"test-{marker}")
    t.start()
    t.join(timeout=5.0)

    # Give the excepthook a moment to flush.
    time.sleep(0.05)

    assert os.path.exists(log_path), f"log file not created at {log_path}"
    after_size = os.path.getsize(log_path)
    assert after_size > before_size, "log file size did not grow"
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert marker in content, "thread exception marker missing from log"
    assert "THREAD EXCEPTION" in content
