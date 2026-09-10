"""§17.1007c — a REAL client disconnect must not stop a detached run.

Why this is separate from test_run_broker.py
--------------------------------------------
That file simulates a disconnect by calling ``aclose()`` on the subscriber
generator, which proves the broker's own bookkeeping is right. It does not
prove the thing actually claimed, because a browser closing a tab does not call
``aclose()``: the socket dies, uvicorn notices, Starlette cancels the task
running the response, and that cancellation propagates through an anyio task
group. Whether the run survives *that* is a property of the HTTP stack, not of
the broker — and it is exactly where I would expect to be wrong.

Why the server is a subprocess
------------------------------
The first version started uvicorn inside the pytest process. It passed alone
and alongside the grpc-heavy RAG/Milvus files, and **segfaulted the interpreter
in the full suite** — exit 139, no failed tests, just a C-extension dump.
Running a real server in-process beside grpc, torch and sklearn is not
something to do to a test suite.

Moving it out (``tests/_broker_test_server.py``) removes the interaction and
makes the test more faithful at the same time: the disconnect is now genuinely
cross-process, a real socket between two OS processes closed the way a browser
closes one, rather than a loopback inside the event loop the assertions run in.

The stand-in generator replaces ``execute_all_nodes`` deliberately: the
property under test is the disconnect boundary, and a 20-minute inference run
would prove it about one DAG, slowly.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

from tests._broker_test_server import FRAME_DELAY, TOTAL_FRAMES

pytestmark = pytest.mark.timeout(120)

REPO_ROOT = __file__.rsplit("/tests/", 1)[0]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """A broker server in its own process. Yields its base URL."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests._broker_test_server", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="ignore") if proc.stderr else ""
            pytest.fail(f"broker test server exited early: {err[:2000]}")
        try:
            if httpx.get(f"{base}/live", timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("broker test server never became ready")
    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _abort_mid_stream(base: str) -> None:
    """Open the run stream, read one frame, then hard-close the socket —
    what a browser does when the tab goes away."""
    with httpx.Client(timeout=10) as client:
        with client.stream("POST", f"{base}/execute/all") as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                break  # one line, then leave the context: socket closed


def test_a_real_disconnect_does_not_stop_the_run(server):
    """The headline claim of §17.1007, across a process boundary.

    Before the broker this is precisely what killed a run: the response WAS the
    run, so the cancellation Starlette delivers on disconnect reached
    execute_all_nodes and its finally marked the job cancelled.
    """
    _abort_mid_stream(server)

    # Let the server observe the closed socket, then confirm the run is STILL
    # going rather than cancelled.
    time.sleep(FRAME_DELAY * 2)
    live = httpx.get(f"{server}/live", timeout=10).json()
    assert live["detached_running"] is True, (
        "the run stopped when the client went away — the response is still "
        "coupled to the run"
    )

    # And it runs to completion, not merely a little further.
    deadline = time.monotonic() + 30
    emitted: list[int] = []
    while time.monotonic() < deadline:
        emitted = httpx.get(f"{server}/emitted", timeout=10).json()["emitted"]
        if len(emitted) == TOTAL_FRAMES:
            break
        time.sleep(0.05)
    assert emitted == list(range(TOTAL_FRAMES)), (
        f"the run must produce every frame after the disconnect, got {emitted}"
    )


def test_reconnecting_after_a_disconnect_attaches_to_the_same_run(server):
    """Reopening the tab shows the run already in flight — the payoff of
    detaching, and the reason /exec/status carries `detached_running`."""
    _abort_mid_stream(server)
    time.sleep(FRAME_DELAY * 2)

    frames = []
    with httpx.Client(timeout=30) as client:
        with client.stream("POST", f"{server}/execute/all") as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    frames.append(line)

    emitted = httpx.get(f"{server}/emitted", timeout=10).json()["emitted"]
    assert emitted == list(range(TOTAL_FRAMES)), (
        f"the DAG must be walked exactly once across both connections, got {emitted}"
    )
    assert len(frames) == TOTAL_FRAMES, (
        f"the reattached client should see the backlog plus live frames, got {len(frames)}"
    )


def test_cancel_still_stops_a_run_over_http(server):
    """With disconnect demoted to a detach, an explicit cancel is the only way
    to stop a run — so it has to work through the same server."""
    _abort_mid_stream(server)
    time.sleep(FRAME_DELAY)

    assert httpx.post(f"{server}/cancel", timeout=20).json()["cancelled"] is True
    seen = len(httpx.get(f"{server}/emitted", timeout=10).json()["emitted"])
    time.sleep(FRAME_DELAY * 4)
    after = httpx.get(f"{server}/emitted", timeout=10).json()["emitted"]
    assert len(after) == seen, "frames kept coming after an explicit cancel"
    assert httpx.get(f"{server}/live", timeout=10).json()["detached_running"] is False
