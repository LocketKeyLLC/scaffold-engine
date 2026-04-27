"""Production entrypoint: uvicorn with per-connection TCP keepalive.

Linux default ``tcp_keepalive_time`` is 7200s (2 hours). For SSE streams
running long generators (research agent), a ``kill -9`` on the client
produces a FIN that the server-side kernel may not observe until the
kernel keepalive probe fires — orphaning the request-handling task.

This entrypoint enables aggressive TCP keepalive on every accepted
connection uvicorn hands to its HTTP protocol:

* ``SO_KEEPALIVE`` = 1
* ``TCP_KEEPIDLE``  = 10s (idle before first probe)
* ``TCP_KEEPINTVL`` =  5s (between probes)
* ``TCP_KEEPCNT``   =  3  (probes before declaring dead)

Worst-case dead-client detection: 10 + 3*5 = 25s. On probe failure the
kernel marks the socket dead; the next write raises ``ConnectionError``;
Starlette's task group cancels the SSE generator; the lifecycle wrapper
in ``research_agent`` finalizes the session as ``cancelled`` with
``error_message='client_disconnect'``.

Policy choice: we patch ``uvicorn.protocols.http.h11_impl.H11Protocol.connection_made``
at startup. This is the narrowest hook that sees every accepted HTTP
transport, is called once per connection (so cost is negligible), and
survives uvloop / asyncio / accept4 differences since we act on the
Python-level ``transport`` object rather than the underlying syscall.
"""
from __future__ import annotations

import logging
import socket

import uvicorn
from uvicorn.protocols.http import h11_impl, httptools_impl


logger = logging.getLogger("scaffold.run_server")

# Keepalive tuning constants. See module docstring for rationale.
_KEEPIDLE_S = 10
_KEEPINTVL_S = 5
_KEEPCNT = 3


def _set_keepalive_on_transport(transport) -> None:
    """Apply TCP keepalive options to the accepted socket behind ``transport``."""
    sock = transport.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for level_name, val in (
            ("TCP_KEEPIDLE", _KEEPIDLE_S),
            ("TCP_KEEPINTVL", _KEEPINTVL_S),
            ("TCP_KEEPCNT", _KEEPCNT),
        ):
            opt = getattr(socket, level_name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
    except OSError as e:
        logger.debug("keepalive_setopt_failed: %s", e)


def _install_h11_keepalive_patch() -> None:
    """Wrap both HTTP protocol classes' connection_made to set keepalive.

    Uvicorn picks ``HttpToolsProtocol`` by default and falls back to
    ``H11Protocol`` when ``httptools`` is unavailable. We patch both so the
    server behaves correctly regardless of which protocol is selected.
    """
    print(
        f"[run_server] installing keepalive patch "
        f"(idle={_KEEPIDLE_S}s intvl={_KEEPINTVL_S}s cnt={_KEEPCNT}, "
        f"detect ~{_KEEPIDLE_S + _KEEPCNT * _KEEPINTVL_S}s)",
        flush=True,
    )

    for module, name in (
        (h11_impl, "H11Protocol"),
        (httptools_impl, "HttpToolsProtocol"),
    ):
        cls = getattr(module, name)
        original = cls.connection_made

        def _make_wrapper(orig):
            def connection_made_with_keepalive(self, transport):
                _set_keepalive_on_transport(transport)
                return orig(self, transport)
            return connection_made_with_keepalive

        cls.connection_made = _make_wrapper(original)
        print(f"[run_server] patched {name}.connection_made", flush=True)


def main() -> None:
    _install_h11_keepalive_patch()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
