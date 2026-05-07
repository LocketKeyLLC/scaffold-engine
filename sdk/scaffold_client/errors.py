"""Exception hierarchy for the scaffold-engine-client SDK.

All SDK errors derive from ``ScaffoldError`` so callers can catch the base
class once. Specific subclasses let consumers branch on auth failures,
rate limits, and server errors without parsing strings.
"""
from __future__ import annotations


class ScaffoldError(Exception):
    """Base class for every error raised by the SDK."""


class ConnectionError(ScaffoldError):
    """The orchestrator is unreachable (DNS/TCP failure, container down)."""


class TimeoutError(ScaffoldError):
    """The request did not complete within the configured timeout."""


class AuthenticationError(ScaffoldError):
    """HTTP 401 — the API key was missing, invalid, or rejected."""


class PermissionError(ScaffoldError):
    """HTTP 403 — the API key is valid but lacks permission for this call."""


class NotFoundError(ScaffoldError):
    """HTTP 404 — the resource (job, schedule, session, …) does not exist."""


class RateLimitError(ScaffoldError):
    """HTTP 429 — the orchestrator throttled this caller."""


class RequestError(ScaffoldError):
    """HTTP 4xx that does not map to a more specific subclass."""


class OrchestratorError(ScaffoldError):
    """HTTP 5xx — the orchestrator hit an internal error."""
