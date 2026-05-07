"""Thin synchronous httpx wrapper used by every command.

We deliberately translate raw ``httpx`` errors into actionable strings —
the CLI surfaces these directly to the user, so "Connection refused"
becomes "Cannot reach orchestrator at <url>; is it running?". This is the
``friendly errors`` payoff (item 6 of the UX roadmap) on the client side.
"""
from __future__ import annotations

from typing import Any

import httpx


class CLIError(RuntimeError):
    """Raised when an HTTP call fails in a way the user needs to act on.

    The message is already user-ready — the click handler can ``echo`` it
    verbatim and exit non-zero.
    """


class Client:
    """Minimal scaffold-orchestrator client.

    Pre-injects ``X-API-Key`` when a key is configured. Raises ``CLIError``
    with a remediation hint on common failures (connection refused, 401,
    timeouts, 5xx). 404s on existence-check endpoints (``GET /jobs/<id>``)
    are returned as ``None`` instead of raising — the caller decides.
    """

    def __init__(self, api_url: str, api_key: str | None, *, timeout: float = 30.0):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        headers: dict[str, str] = {"User-Agent": "scaffold-cli/0.1"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._http = httpx.Client(
            base_url=self.api_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Verb helpers — return parsed JSON or raise CLIError
    # ------------------------------------------------------------------

    def get(self, path: str, *, params: dict | None = None) -> Any:
        return self._dispatch("GET", path, params=params)

    def post(self, path: str, *, json: dict | None = None) -> Any:
        return self._dispatch("POST", path, json=json)

    def get_or_none(self, path: str) -> Any | None:
        """GET that returns ``None`` on 404 instead of raising. Useful for
        existence checks (``scaffold jobs status <id>``)."""
        try:
            return self._dispatch("GET", path)
        except CLIError as exc:
            if "(404)" in str(exc):
                return None
            raise

    # ------------------------------------------------------------------
    # Internal: error translation
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
            resp = self._http.request(method, path, params=params, json=json)
        except httpx.ConnectError:
            raise CLIError(
                f"Cannot reach orchestrator at {self.api_url}. "
                "Is it running? Try 'make doctor' or check 'docker ps'."
            ) from None
        except httpx.TimeoutException:
            raise CLIError(
                f"Request timed out talking to {self.api_url}. "
                "The orchestrator may be busy; retry, or check container logs."
            ) from None
        except httpx.HTTPError as exc:
            raise CLIError(f"HTTP error talking to {self.api_url}: {exc}") from None

        if resp.status_code == 401:
            raise CLIError(
                "API key rejected (401). "
                "Set SCAFFOLD_API_KEY in your env, .env, or ~/.scaffold/config.toml. "
                "Run 'make doctor' to confirm the orchestrator's expected key."
            )
        if resp.status_code == 403:
            raise CLIError(
                "Access forbidden (403). "
                "The orchestrator rejected the request — check auth config."
            )
        if resp.status_code >= 500:
            detail = self._best_error_detail(resp)
            raise CLIError(
                f"Orchestrator error ({resp.status_code}): {detail}. "
                "Check 'docker logs scaffold-orchestrator' for the stack trace."
            )
        if resp.status_code >= 400:
            detail = self._best_error_detail(resp)
            raise CLIError(f"Request rejected ({resp.status_code}): {detail}")

        try:
            return resp.json()
        except ValueError:
            # Endpoints that return non-JSON (HTML error pages, etc.) — give
            # the raw text back so the caller can render something useful.
            return resp.text

    @staticmethod
    def _best_error_detail(resp: httpx.Response) -> str:
        """FastAPI emits ``{"detail": ...}``; pick that out when present."""
        try:
            data = resp.json()
            if isinstance(data, dict) and "detail" in data:
                detail = data["detail"]
                if isinstance(detail, str):
                    return detail
                return str(detail)
        except Exception:
            pass
        return resp.text[:200] if resp.text else f"HTTP {resp.status_code}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
