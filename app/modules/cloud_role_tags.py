"""§17.1623 — the engine registers its own cloud role tags on its Ollama daemon.

A ``-cloud`` / ``:cloud`` tag is a few-KB manifest (the model runs at
ollama.com; a pull is ~1.5 s), but Ollama only serves it once it has been
pulled. Nothing pulled them: the guest deploy skipped cloud tags on purpose and
the engine only told the operator to ``ollama pull`` by hand. Live (VM 110,
2026-10-16): model_fallback / model_cloud_heavy / model_cloud_alt all named
``qwen3.5:397b-cloud`` while the daemon listed only the four local tags, so the
cloud fallback could never answer and ``validate_models`` 422'd /research.

``ensure_cloud_role_tags`` pulls every Ollama-routed role whose tag is a cloud
tag the daemon does not list. Local tags are never pulled here (tens of GB —
that stays the deploy's job). A refused pull keeps Ollama's own text (the
"unauthorized" / signin reply names the daemon's key) in ``last_result`` so
/health can say WHY instead of "pull them". Fail-soft everywhere; a retry is
throttled so a per-request caller cannot hammer the daemon.

§17.1624 — a LISTED cloud tag can still be dead: Ollama Cloud retires models
(qwen3.5:397b-cloud, 2026-09-25: "410 Gone … retired … Try glm-5.3-flash:cloud")
and the daemon keeps listing a tag pulled before the retirement, so /health was
silent on the laptop while two roles named it. ``check_cloud_role_tags`` sends
each cloud role tag one 1-token generate (at startup, then from the reaper at
most every CHECK_EVERY_S) and records a DEFINITIVE refusal (HTTP 4xx); /health
reads the record. The same probe explains a refused pull: the pull says only
"file does not exist", the generate says the model was retired and what to use.
"""
from __future__ import annotations

import logging
import time

from app.config import SWITCHABLE_ROLE_FIELDS, settings

logger = logging.getLogger("scaffold")

# Re-attempt a failed pull at most this often (the operator may add the device
# key at ollama.com at any moment — the next caller after the window retries).
RETRY_AFTER_S = 60.0
# §17.1624 — how often the reaper re-probes the cloud role tags (one 1-token
# call per distinct tag; a retirement is not urgent to the minute).
CHECK_EVERY_S = 3600.0

_state: dict = {"at": 0.0, "failed": {}, "checked_at": None, "dead": {}}


def is_cloud_tag(model: str) -> bool:
    return bool(model) and (model.endswith(":cloud") or model.endswith("-cloud"))


def _role_provider(role: str) -> str:
    return getattr(settings, f"{role}_provider", None) or "ollama"


def cloud_role_tags() -> set[str]:
    """Cloud tags the live config routes to Ollama (settings carries the DB
    overrides in place, so this is the effective config)."""
    return {
        tag for role in SWITCHABLE_ROLE_FIELDS
        if _role_provider(role) == "ollama"
        and is_cloud_tag(tag := (getattr(settings, role, "") or ""))
    }


def last_failures() -> dict[str, str]:
    """tag -> Ollama's refusal text from the most recent attempt (read-only)."""
    return dict(_state["failed"])


def dead_tags() -> dict[str, str]:
    """§17.1624 — LISTED cloud role tags Ollama refused on the last check,
    tag -> its text (read-only; only tags some role still names)."""
    live = cloud_role_tags()
    return {t: e for t, e in _state["dead"].items() if t in live}


def roles_naming(tag: str) -> list[str]:
    return sorted(r for r in SWITCHABLE_ROLE_FIELDS
                  if _role_provider(r) == "ollama" and getattr(settings, r, None) == tag)


async def _probe(client, tag: str) -> str | None:
    """One 1-token generate. None = served; else the DEFINITIVE refusal text
    (HTTP 4xx: retired, unknown, unauthorized). A 5xx / timeout is transient
    and returns None — it says nothing about the tag."""
    try:
        r = await client.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={"model": tag, "prompt": "Reply with OK.", "stream": False,
                  "options": {"num_predict": 1}},
            timeout=60.0,
        )
    except Exception:
        return None
    if r.status_code < 400 or r.status_code >= 500:
        return None
    try:
        err = (r.json() or {}).get("error")
    except Exception:
        err = None
    return f"HTTP {r.status_code}: {err or r.text[:300]}"[:500]


async def _tags(client) -> set[str]:
    r = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5.0)
    r.raise_for_status()
    return {m.get("name", "") for m in (r.json().get("models") or [])}


async def ensure_cloud_role_tags(*, force: bool = False, tags: set[str] | None = None) -> dict:
    """Pull the missing cloud role tags. Returns {pulled, failed, skipped}.

    ``tags`` overrides which tags to ensure (the role-set API passes the one
    tag being applied); default is every cloud role tag in the live config.
    ``skipped`` is True when throttled or the daemon is unreachable.
    """
    from app.model_router import _get_client

    wanted = set(tags) if tags is not None else cloud_role_tags()
    wanted = {t for t in wanted if is_cloud_tag(t)}
    now = time.monotonic()
    if not wanted:
        return {"pulled": [], "failed": {}, "skipped": False}
    if not force and _state["failed"] and now - _state["at"] < RETRY_AFTER_S:
        return {"pulled": [], "failed": last_failures(), "skipped": True}
    try:
        client = _get_client()
        present = await _tags(client)
    except Exception as exc:
        logger.warning("cloud_role_tags: ollama unreachable: %s", exc)
        return {"pulled": [], "failed": {}, "skipped": True}

    pulled: list[str] = []
    failed: dict[str, str] = {}
    for tag in sorted(wanted - present):
        try:
            r = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/pull",
                json={"model": tag, "stream": False}, timeout=120.0,
            )
            body = r.json() if r.content else {}
            if r.status_code < 400 and body.get("status") == "success":
                pulled.append(tag)
                logger.info("cloud_role_tag_pulled: tag=%s", tag)
            else:
                err = body.get("error") or f"HTTP {r.status_code}: {r.text[:300]}"
                if body.get("signin_url"):
                    err = f"{err} (sign in / add the daemon's key: {body['signin_url']})"
                # §17.1624 — "file does not exist" hides WHY; the generate says it
                # (live: "qwen3.5:397b was retired on September 25, 2026. Try …").
                if "does not exist" in str(err):
                    err = (await _probe(client, tag)) or err
                failed[tag] = str(err)[:500]
        except Exception as exc:
            failed[tag] = repr(exc)[:300]
        if tag in failed:
            logger.warning("cloud_role_tag_pull_failed: tag=%s err=%s", tag, failed[tag])

    # A full sweep replaces the record (a role re-pointed away from a dead tag
    # stops being reported); a single-tag call only replaces its own entries.
    kept = {} if tags is None else {
        t: e for t, e in _state["failed"].items() if t not in wanted}
    kept.update(failed)
    _state.update(at=now, failed=kept)
    return {"pulled": pulled, "failed": failed, "skipped": False}


async def check_cloud_role_tags(*, force: bool = False) -> dict | None:
    """§17.1624 — probe every LISTED cloud role tag; record the definitive
    refusals for /health. Throttled to CHECK_EVERY_S unless ``force``.
    Returns {checked, dead} or None when throttled / daemon unreachable."""
    from app.model_router import _get_client

    now = time.monotonic()
    last = _state["checked_at"]
    if not force and last is not None and now - last < CHECK_EVERY_S:
        return None
    try:
        client = _get_client()
        present = await _tags(client)
    except Exception as exc:
        logger.warning("cloud_role_tags_check: ollama unreachable: %s", exc)
        return None
    checked = sorted(cloud_role_tags() & present)
    dead: dict[str, str] = {}
    for tag in checked:
        err = await _probe(client, tag)
        if err:
            dead[tag] = err
            logger.warning("cloud_role_tag_refused: tag=%s roles=%s err=%s",
                           tag, ",".join(roles_naming(tag)), err)
    _state.update(checked_at=now, dead=dead)
    return {"checked": checked, "dead": dead}
