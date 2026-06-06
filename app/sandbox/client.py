"""Client for the scaffold-coderunner sidecar (§17.433).

Design contract (mirrors the ngspice/Verilator clients):
  * Never raises. Transport error, HTTP error, timeout, and non-zero exit all
    surface as ``CodeRunResult(ok=False, ...)`` so verification loops treat a
    failed/unreachable sandbox as DATA, not an exception.
  * Disabled by default: when ``settings.coderunner_url`` is empty the client
    returns ``ok=False, error='coderunner disabled'`` without any network call,
    so importing/holding a reference is safe before the operator starts the
    sidecar.

The sidecar executes the code; this is just the typed, fail-soft transport.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger("scaffold")


@dataclass
class CodeRunResult:
    ok: bool
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    # Transport/availability error (None when the sidecar actually ran the code,
    # regardless of the code's own exit status).
    error: str | None = None


async def run_code(
    files: dict[str, str],
    command: list[str],
    *,
    timeout_s: float = 30.0,
    cpu_seconds: int = 30,
    mem_mb: int = 512,
    max_output_bytes: int = 256_000,
    base_url: str | None = None,
) -> CodeRunResult:
    """Run ``command`` (argv) against ``files`` in the sandbox; never raises.

    ``base_url`` defaults to ``settings.coderunner_url``; empty => disabled.
    The HTTP read timeout is the requested wall-clock + a margin so the
    sidecar's own timeout fires first and returns a structured result.
    """
    url = (base_url if base_url is not None else settings.coderunner_url or "").strip()
    if not url:
        return CodeRunResult(ok=False, error="coderunner disabled")

    payload = {
        "files": files,
        "command": command,
        "timeout_s": timeout_s,
        "cpu_seconds": cpu_seconds,
        "mem_mb": mem_mb,
        "max_output_bytes": max_output_bytes,
    }
    http_timeout = timeout_s + 15.0
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(f"{url.rstrip('/')}/run", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("coderunner_call_failed: %s", e)
        return CodeRunResult(ok=False, error=f"coderunner call failed: {e}")

    if not isinstance(data, dict) or "ok" not in data:
        return CodeRunResult(ok=False, error="coderunner bad response shape")

    return CodeRunResult(
        ok=bool(data.get("ok", False)),
        exit_code=int(data.get("exit_code", -1)),
        stdout=str(data.get("stdout", "")),
        stderr=str(data.get("stderr", "")),
        duration_ms=int(data.get("duration_ms", 0)),
        timed_out=bool(data.get("timed_out", False)),
        truncated=bool(data.get("truncated", False)),
        error=None,
    )
