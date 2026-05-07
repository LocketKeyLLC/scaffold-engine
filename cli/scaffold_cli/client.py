"""Thin shim over ``scaffold_client.Client`` (Sprint J.1.e).

The CLI used to ship its own httpx wrapper. As of Sprint J.1, the
typed-client logic lives in the ``scaffold-engine-client`` SDK; this
module is now a click-friendly translator that catches the SDK's
``ScaffoldError`` subclasses and re-raises them as ``CLIError`` with
the longer, CLI-specific remediation hints (``make doctor``, the
config-source list, etc.) that don't belong in a library.

The public surface (``Client.get``, ``Client.post``, ``Client.get_or_none``,
``CLIError``, the ``_http`` attribute used by tests) is preserved so that
``cli/scaffold_cli/main.py`` and the existing test suite pass unchanged.
"""
from __future__ import annotations

from typing import Any

from scaffold_client import (
    AuthenticationError,
    Client as _SDKClient,
    ConnectionError as _SDKConnectionError,
    NotFoundError,
    PermissionError as _SDKPermissionError,
    ScaffoldError,
    TimeoutError as _SDKTimeoutError,
)


class CLIError(RuntimeError):
    """Raised when an HTTP call fails in a way the user needs to act on.

    The message is already user-ready — the click handler can ``echo`` it
    verbatim and exit non-zero.
    """


class Client:
    """CLI-facing wrapper around ``scaffold_client.Client``.

    Pre-injects ``X-API-Key`` (via the SDK) and translates the SDK's
    typed exceptions into ``CLIError`` with the remediation hints the
    CLI users expect. 404s on ``get_or_none`` return ``None`` rather
    than raising — used by ``scaffold jobs status`` for existence checks.
    """

    def __init__(self, api_url: str, api_key: str | None, *, timeout: float = 30.0):
        self._inner = _SDKClient(api_url, api_key=api_key, timeout=timeout)
        self.api_url = self._inner.base_url
        self.api_key = api_key
        # Tests in cli/tests/test_client.py patch ``c._http.request``; the
        # SDK's underlying httpx client is the natural mock target now too.
        self._http = self._inner._http

    # ------------------------------------------------------------------
    # Verb helpers — return parsed JSON or raise CLIError
    # ------------------------------------------------------------------

    def get(self, path: str, *, params: dict | None = None) -> Any:
        return self._dispatch("GET", path, params=params)

    def post(self, path: str, *, json: dict | None = None) -> Any:
        return self._dispatch("POST", path, json=json)

    def get_or_none(self, path: str) -> Any | None:
        """``GET`` that returns ``None`` on 404 instead of raising. Used by
        existence checks (``scaffold jobs status <id>``)."""
        try:
            return self._dispatch("GET", path)
        except CLIError as exc:
            if "(404)" in str(exc):
                return None
            raise

    # ------------------------------------------------------------------
    # Internal: SDK exception → CLIError translation
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        try:
            return self._inner.request(method, path, params=params, json=json)
        except _SDKConnectionError:
            raise CLIError(
                f"Cannot reach orchestrator at {self.api_url}. "
                "Is it running? Try 'make doctor' or check 'docker ps'."
            ) from None
        except _SDKTimeoutError:
            raise CLIError(
                f"Request timed out talking to {self.api_url}. "
                "The orchestrator may be busy; retry, or check container logs."
            ) from None
        except AuthenticationError:
            raise CLIError(
                "API key rejected (401). "
                "Set SCAFFOLD_API_KEY in your env, .env, or ~/.scaffold/config.toml. "
                "Run 'make doctor' to confirm the orchestrator's expected key."
            ) from None
        except _SDKPermissionError:
            raise CLIError(
                "Access forbidden (403). "
                "The orchestrator rejected the request — check auth config."
            ) from None
        except NotFoundError as exc:
            # Preserve the SDK detail in a 404-tagged form so ``get_or_none``
            # can detect it via the ``(404)`` substring without a structured
            # exception channel.
            raise CLIError(f"Resource not found (404): {exc}") from None
        except ScaffoldError as exc:
            # OrchestratorError / RequestError / RateLimitError — the SDK
            # already formats these as "Orchestrator error (500): boom.
            # Check 'docker logs scaffold-orchestrator' ..." and "Request
            # rejected (422): missing field 'idea'." Pass the message
            # through; it already carries the phrases the tests assert on.
            raise CLIError(str(exc)) from None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
