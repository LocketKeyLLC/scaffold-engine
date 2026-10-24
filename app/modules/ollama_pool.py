"""§17.1727 — the engine's Ollama calls spread over a pool of Ollama servers.

The engine had ONE Ollama address (``settings.ollama_base_url``). Live (job f9ca3ee1, 2026-10-21): the operator
asked for old laptops to add AI compute to the engine's research and programming, with VM 110 and the laptops
"working together as one pool" and the work surviving one laptop being unplugged. The plan built a cluster and a
load-balanced Service — and nothing could ever send the engine's work to it, because the engine had no way to
use a second Ollama at all.

``OLLAMA_POOL_URLS`` (comma-separated) names more Ollama servers beside ``ollama_base_url``. Every model call
(generate/chat, tool calls, streaming) asks ``lease(model)`` for a server:

* only a member that HOLDS the model is chosen (its own ``/api/tags``, re-read every ``_TAGS_TTL_S``) — a
  laptop that cannot hold a 27B model never gets a 27B request; a cloud tag (``…:cloud``) stays on the primary,
  whose daemon carries the account's device key;
* among those, the one with the fewest calls in flight, the primary first on a tie;
* a member that refuses the connection or whose tags read fails is set aside for ``_DOWN_S`` and the call moves
  to the next holder (``on_connect_error``) — one laptop unplugged costs one reconnect, not a failed step.

With ``OLLAMA_POOL_URLS`` empty every call goes to ``ollama_base_url`` exactly as before: no tags read, no
behaviour change. The pool is never consulted for pulls, health of the primary, or cloud-tag management — those
are about the primary daemon by definition.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from app.config import settings
from app.modules.cloud_role_tags import is_cloud_tag

logger = logging.getLogger("scaffold.ollama_pool")

_TAGS_TTL_S = 60.0
_DOWN_S = 30.0
_TAGS_TIMEOUT_S = 4.0


@dataclass
class _Member:
    url: str
    models: frozenset[str] = frozenset()
    read_at: float = 0.0
    down_until: float = 0.0
    in_flight: int = 0
    last_error: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_members: dict[str, _Member] = {}


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/")


def pool_urls() -> list[str]:
    """The extra members from ``OLLAMA_POOL_URLS``, normalised, without the primary or duplicates."""
    primary = _norm(settings.ollama_base_url)
    out: list[str] = []
    for raw in (settings.ollama_pool_urls or "").split(","):
        u = _norm(raw)
        if u and u != primary and u not in out:
            out.append(u)
    return out


def enabled() -> bool:
    return bool(pool_urls())


def _member(url: str) -> _Member:
    m = _members.get(url)
    if m is None:
        m = _members[url] = _Member(url=url)
    return m


def _same_model(name: str, held: frozenset[str]) -> bool:
    """``qwen3:4b`` matches ``qwen3:4b``; a bare ``nomic-embed-text`` matches ``nomic-embed-text:latest``."""
    if name in held:
        return True
    return ":" not in name and f"{name}:latest" in held


async def _refresh(m: _Member) -> None:
    now = time.monotonic()
    if now - m.read_at < _TAGS_TTL_S:
        return
    async with m.lock:
        if time.monotonic() - m.read_at < _TAGS_TTL_S:
            return
        from app.model_router import _get_client
        try:
            resp = await _get_client().get(f"{m.url}/api/tags", timeout=_TAGS_TIMEOUT_S)
            resp.raise_for_status()
            m.models = frozenset(x.get("name", "") for x in resp.json().get("models", []) if x.get("name"))
            m.last_error = ""
        except Exception as exc:  # an unreachable member holds nothing until it answers again
            m.models = frozenset()
            m.down_until = time.monotonic() + _DOWN_S
            m.last_error = str(exc)[:200] or type(exc).__name__
            logger.warning("ollama_pool_member_unreadable: url=%s err=%s", m.url, m.last_error)
        m.read_at = time.monotonic()


async def candidates(model: str) -> list[str]:
    """The servers to try for ``model``, best first. Always ends with the primary when nothing else qualifies,
    so the caller's own error (404 model not found → fallback chain) is unchanged."""
    primary = _norm(settings.ollama_base_url)
    extra = pool_urls()
    if not extra or is_cloud_tag(model):
        return [primary]
    await asyncio.gather(*(_refresh(_member(u)) for u in extra))
    now = time.monotonic()
    holders: list[tuple[int, int, str]] = []
    # The primary is ranked like any member but is never read for tags here: it is the engine's own daemon and
    # stays the default. It joins the ranking as a holder unless it is resting after a refused connection.
    pm = _member(primary)
    if pm.down_until <= now:
        holders.append((pm.in_flight, 0, primary))
    for i, u in enumerate(extra, start=1):
        m = _member(u)
        if m.down_until > now or not _same_model(model, m.models):
            continue
        holders.append((m.in_flight, i, u))
    holders.sort()
    order = [u for _, _, u in holders]
    if primary not in order:
        order.append(primary)
    return order


def on_connect_error(url: str, exc: BaseException) -> None:
    """A member that refused or dropped the connection rests for ``_DOWN_S`` and is re-read before it is used."""
    m = _member(_norm(url))
    m.down_until = time.monotonic() + _DOWN_S
    m.read_at = 0.0
    m.last_error = str(exc)[:200] or type(exc).__name__
    logger.warning("ollama_pool_member_down: url=%s err=%s", m.url, m.last_error)


@contextlib.asynccontextmanager
async def held(url: str) -> AsyncIterator[str]:
    """Count a call in flight on ``url`` for the least-busy choice."""
    m = _member(_norm(url))
    m.in_flight += 1
    try:
        yield m.url
    finally:
        m.in_flight -= 1


def is_connect_error(exc: BaseException) -> bool:
    """Only a connection that never carried the request moves to the next member: a timeout or an HTTP error
    after the request was sent means the member was working on it, and re-sending would run it twice."""
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError))


async def status() -> dict:
    """For ``/health``: each extra member, whether it answers, and the models it holds."""
    out = []
    for u in pool_urls():
        m = _member(u)
        m.read_at = 0.0
        await _refresh(m)
        out.append({"url": u, "status": "down" if m.last_error else "up", "in_flight": m.in_flight,
                    "models": sorted(m.models), **({"error": m.last_error} if m.last_error else {})})
    return {"members": out, "up": sum(1 for x in out if x["status"] == "up"), "total": len(out)}


def _reset_for_tests() -> None:
    _members.clear()


# ---------------------------------------------------------------------------
# What the planner is told (the engine must know its own capability).
# ---------------------------------------------------------------------------

_AI_RE = r"\b(?:ollama|inference|ai comput\w*|llm|models?)\b"
_MORE_RE = (r"\b(?:cluster|worker nodes?|workers|compute power|computing power|"
            r"(?:more|extra|additional|increase\w*|add\w*)\s+(?:ai\s+)?comput\w*)\b")


def planner_note_applies(brief: dict | str) -> bool:
    """A brief about adding AI compute/serving capacity: names the AI side (Ollama, inference, models) AND
    added capacity (a cluster, workers, more compute). Measured over every stored brief on 2026-10-21: only the
    Kubernetes-laptops job matches; the engine move (Ollama + inference, no added capacity) does not."""
    import json as _json
    import re
    text = (brief if isinstance(brief, str) else _json.dumps(brief or {})).lower()
    return bool(re.search(_AI_RE, text)) and bool(re.search(_MORE_RE, text))


def plan_misses_pool(tasks: list[dict], brief: dict | str) -> list[str]:
    """§17.1807 — what a plan for a brief that adds AI compute leaves out of PLANNER_NOTE's shape: Ollama on each
    added machine, and the engine-host step that sets OLLAMA_POOL_URLS. Live (job 700dd9c0, the laptop cluster
    re-planned 2026-10-24): the note was in the prompt and the plan had "research worker pods", "coding worker pods"
    and "configure the server engine to dispatch tasks" -- no Ollama anywhere, no OLLAMA_POOL_URLS. A note is advice;
    nothing checked that the plan took it."""
    import re
    if not planner_note_applies(brief):
        return []
    texts = [" ".join(str(t.get(k) or "") for k in ("name", "notes", "description")) for t in tasks or []]
    text = " ".join(texts)
    # a step that puts Ollama ON the added machines -- not one that only mentions the engine's own ("keeping Ollama
    # on VM 110 responsive", live 700dd9c0 T21)
    runs_ollama = re.compile(
        r"\b(?:deploy|install|run|start|pull)\w*\b[^.\n]{0,40}\bollama\b[^.\n]{0,40}\b(?:on|to|per|across)\b"
        r"[^.\n]{0,20}\b(?:each|every|all|worker|laptop|node|machine|member)s?\b"
        r"|\bollama\b[^.\n]{0,20}\b(?:daemonset|per[- ]node|on\s+(?:each|every)\b)", re.I)
    missing = []
    if not any(runs_ollama.search(x) for x in texts):
        missing.append("no step runs Ollama on each added machine (its own address, port 11434)")
    if "OLLAMA_POOL_URLS" not in text:
        missing.append("no step on the engine host sets OLLAMA_POOL_URLS to those addresses, redeploys the engine and "
                       "reads /health showing every member up")
    return missing


def render_pool_corrections(missing: list[str], next_attempt: int) -> str:
    return (f"## Engine-compute corrections needed (attempt {next_attempt})\n\nThis brief adds AI compute for the "
            "engine, and the plan does not use the engine's Ollama pool (see ENGINE CAPABILITY above):\n"
            + "\n".join(f"- {m}" for m in missing)
            + "\n\nReplace vague 'worker pods' / 'dispatch tasks to the cluster' steps with: each added machine runs "
            "its own Ollama and pulls the role models that fit its RAM; then ONE engine-host step sets "
            "OLLAMA_POOL_URLS, redeploys the engine and reads /health; the lose-one-machine check comes after it.")


PLANNER_NOTE = """ENGINE CAPABILITY — an Ollama pool (§17.1727). The scaffold engine can spread its OWN model calls over
more Ollama servers: the setting OLLAMA_POOL_URLS (comma-separated http://<address>:11434, one entry per server)
in the engine's .env, applied by redeploying the engine. Each call goes to a member that already holds that
call's model, least busy first, beside the engine's own Ollama (OLLAMA_BASE_URL); a member that is down is
skipped, so one machine unplugged does not fail the engine's work; cloud tags stay on the engine's own Ollama.
/health lists the members under checks.ollama.pool. So when the brief adds compute FOR THE ENGINE:
- each added machine runs its own Ollama, reachable at its OWN address on port 11434 (a single load-balanced
  Service hides which member holds which model and which one is dead — the engine needs each address);
- each machine pulls the engine's role models (MODEL_* settings, or the engine's GET /models) that fit its RAM;
  a model too big for a machine stays on the engine's own Ollama — say which model goes where;
- the plan ENDS with a step on the engine host that sets OLLAMA_POOL_URLS to those addresses, redeploys the
  engine, and reads /health showing every pool member "up" with the models it holds;
- a "keeps working when one machine is lost" check comes AFTER that step and proves it on the ENGINE: one
  member stopped, an engine model call still answers and /health names that member down.

"""
