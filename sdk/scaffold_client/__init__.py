"""scaffold-engine-client — typed Python client for the Scaffold Engine API.

Pinned to FastAPI app version 1.0.0 (see ``docs/openapi.json``). Both a
synchronous ``Client`` and an asynchronous ``AsyncClient`` are exposed.

Example::

    from scaffold_client import Client

    with Client("http://localhost:8000", api_key="...") as c:
        health = c.health()
        job = c.ideate("Build me a markdown linter")
"""
from __future__ import annotations

from ._version import __version__
from .async_client import AsyncClient
from .client import Client
from .errors import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    OrchestratorError,
    PermissionError,
    RateLimitError,
    RequestError,
    ScaffoldError,
    TimeoutError,
)
from .next_actions import (
    action_clickable,
    filter_renderable,
    format_block,
)

__all__ = [
    "__version__",
    "AsyncClient",
    "AuthenticationError",
    "Client",
    "ConnectionError",
    "NotFoundError",
    "OrchestratorError",
    "PermissionError",
    "RateLimitError",
    "RequestError",
    "ScaffoldError",
    "TimeoutError",
    # §17.195 — shared next_actions formatter
    "action_clickable",
    "filter_renderable",
    "format_block",
]
