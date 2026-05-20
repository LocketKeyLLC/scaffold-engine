"""§17.174 — extracted from ``app/main.py`` for the same reason as
``app/utils/model_validation.py``: per-domain routers (specifically
``app/routers/research.py``) need ``_sse_with_disconnect_watch`` and
importing from main.py would pull every top-level side effect.

This helper is the keepalive wrapper that makes SSE streams notice
client disconnects within ~1 second. See its docstring for the
detailed mechanism.
"""
import asyncio

from fastapi import Request


async def _sse_with_disconnect_watch(request: Request, source):
    """Interleave SSE keepalive comments to force Starlette to notice
    client disconnect quickly.

    Starlette's ``listen_for_disconnect`` only raises when uvicorn's ASGI
    ``receive`` delivers an ``http.disconnect`` message, which in turn
    only happens when the server-side socket is actively probed. During
    long generator awaits (LLM calls, HTTP fetches), no probe occurs, so
    a ``kill -9`` on the client can go undetected for 30+ minutes.

    Fix: emit an SSE comment line (``: keepalive\\n\\n``) every
    ``KEEPALIVE_INTERVAL`` seconds when the underlying generator is idle.
    Each comment write exercises the socket; a write to a dead socket
    raises ``ConnectionError`` which Starlette surfaces as a cancellation
    into the generator. The lifecycle wrapper in ``research_agent``
    catches the ``CancelledError`` in its ``finally`` block and finalizes
    the session as ``cancelled`` with ``error_message='client_disconnect'``.
    """
    KEEPALIVE_INTERVAL = 2.0  # seconds
    gen = source.__aiter__()
    next_task: asyncio.Task | None = None

    try:
        while True:
            if next_task is None:
                next_task = asyncio.create_task(gen.__anext__())

            done, _pending = await asyncio.wait(
                {next_task}, timeout=KEEPALIVE_INTERVAL,
            )
            if not done:
                # Generator is still computing — emit a socket-probing comment.
                # If the client is gone, this write fails and Starlette cancels us.
                yield ": keepalive\n\n"
                continue

            try:
                chunk = next_task.result()
            except StopAsyncIteration:
                return
            finally:
                next_task = None

            yield chunk
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except BaseException:
                # Best-effort cleanup: swallow CancelledError + any
                # exception the inner generator surfaces during shutdown
                # so we don't mask the outer flow's exit reason.
                pass
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
