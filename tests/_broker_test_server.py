"""§17.1007c — a standalone server for the run-broker disconnect test.

Run as a SUBPROCESS, deliberately::

    python -m tests._broker_test_server <port>

The first version of ``test_run_broker_disconnect.py`` started uvicorn inside
the pytest process. Alone, and alongside the grpc-heavy RAG/Milvus files, it
passed. In the full suite it **segfaulted the interpreter** — exit 139, no
failed tests, just a C-extension dump. Running a real server in-process next to
grpc, torch and sklearn is not a thing to do to a test suite, and no amount of
teardown care makes it safe.

Moving the server into its own process removes the interaction entirely, and it
makes the test *more* faithful rather than less: the disconnect under test is
now genuinely cross-process — a real socket between two OS processes, closed
the way a browser closes one — instead of a loopback inside the same event loop
the assertions run in.

Because the broker state now lives over here, everything the test needs to
observe is exposed as an endpoint: ``/live`` for ``is_running`` and
``/emitted`` for how far the background run actually got.

The leading underscore keeps pytest from collecting this file, matching the
convention already used by ``tests/_live_write_guard.py`` and friends.
"""
from __future__ import annotations

import asyncio
import sys

import uvicorn
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from app.modules import run_broker

JOB_ID = "disconnect-test-job"
TOTAL_FRAMES = 12
FRAME_DELAY = 0.05  # ~0.6s total: long enough to abort in the middle of

_emitted: list[int] = []


def build_app() -> FastAPI:
    app = FastAPI()

    async def source():
        for i in range(TOTAL_FRAMES):
            await asyncio.sleep(FRAME_DELAY)
            _emitted.append(i)
            yield f"event: node_done\ndata: {{\"n\": {i}}}\n\n"

    @app.post("/execute/all")
    async def execute_all():
        # The same two lines as the real endpoint in app/routers/workflow.py:
        # start (or attach to) a detached run, then subscribe to it.
        run = run_broker.start(JOB_ID, source)
        return StreamingResponse(
            run_broker.subscribe(run),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/live")
    async def live():
        return {"detached_running": run_broker.is_running(JOB_ID)}

    @app.get("/emitted")
    async def emitted():
        """How far the BACKGROUND run got — the thing a disconnect must not
        affect. Read over HTTP because the run lives in this process now."""
        return {"emitted": list(_emitted), "total": TOTAL_FRAMES}

    @app.post("/cancel")
    async def cancel():
        return {"cancelled": await run_broker.cancel(JOB_ID)}

    return app


def main() -> None:
    port = int(sys.argv[1])
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
