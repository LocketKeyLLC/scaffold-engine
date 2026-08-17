"""§17.809 — runtime compute profiles ("quick mode" GPU/cloud-fast preset).

A *profile* is a named bundle that re-points the engine for a particular
trade-off. The shipped one is ``quick``: fast Ollama-Cloud tags on every
generation role plus tightened timing/quality knobs, targeting a <5 min
end-to-end run on this CPU-only host (where the local models can't).

Two layers, deliberately split so each reuses machinery that already persists
and reloads correctly:

  * **Models** ride on the existing per-role override table (migration 050,
    :mod:`app.modules.model_overrides`). ``apply_profile`` calls
    ``set_override`` for each role, so the swaps persist AND are replayed onto
    ``settings`` at startup by the existing model-overrides lifespan hook — no
    new reload path for the model half.

  * **Knobs** (``max_retries``, ``node_escalation_enabled``, the
    faithfulness/CoVe checks, the research caps) are plain ``settings``
    attributes. ``apply_profile`` mutates them in-process and records the exact
    values in the singleton ``runtime_profile`` row (migration 067) so
    ``clear_profile`` reverts precisely and ``load_profile_into_settings``
    re-applies them after a restart.

The snapshot stored in ``runtime_profile.applied_settings`` is **self-describing**
(``{"models": {...}, "knobs": {...}}``) so revert never depends on the code-side
registry still containing the profile — a profile deleted from ``PROFILES`` can
still be cleanly turned off.

Per-job ``--quick`` (the ``jobs.quick_mode`` flag) does NOT touch global
settings; it layers :func:`quick_model_map` under a single job's request
overrides and forces :data:`QUICK_RESEARCH_DEPTH`. See ``app/routers`` callers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from app.config import settings, SWITCHABLE_ROLE_FIELDS
from app.database import async_session
from app.modules.model_overrides import set_override, clear_override

logger = logging.getLogger("scaffold.profiles")


@dataclass(frozen=True)
class Profile:
    """A named runtime bundle: per-role model swaps + settings-knob overrides.

    ``models`` keys must be switchable roles (validated at import). ``settings``
    keys must be real ``Settings`` attributes (validated at import). ``research_
    depth`` is the depth a per-job ``--quick`` run forces; it is NOT a global
    settings knob (research depth is a per-request parameter).
    """

    name: str
    description: str
    models: dict[str, str] = field(default_factory=dict)
    settings: dict[str, object] = field(default_factory=dict)
    research_depth: str = "shallow"


# ── Profile registry ───────────────────────────────────────────────────────
# 'quick' — GPU/cloud-fast. Models are fast Ollama-Cloud tags already pulled on
# this host (verified via `ollama list`); provider stays 'ollama' (§17.807's
# gpu-cloud install preset is the vLLM/OpenAI path — orthogonal to this runtime
# toggle). kimi-k2.6:cloud on the verifier is the operator's speed-verified pick.
QUICK_RESEARCH_DEPTH = "shallow"

PROFILES: dict[str, Profile] = {
    "quick": Profile(
        name="quick",
        description=(
            "GPU/cloud-fast preset — fast Ollama-Cloud models on every "
            "generation role + tightened retry/research/quality knobs, "
            "targeting <5 min end-to-end."
        ),
        models={
            "model_general": "deepseek-v4-pro:cloud",
            "model_coder": "qwen3-coder-next:cloud",
            "model_verifier": "kimi-k2.6:cloud",      # operator speed-verified
            "model_router": "gpt-oss:20b-cloud",
            "model_research_extract": "gpt-oss:20b-cloud",
            "model_cloud_alt": "gpt-oss:20b-cloud",
            "model_fallback": "gpt-oss:20b-cloud",
            # model_cloud_heavy intentionally left at its default — node
            # escalation is turned OFF below, so the heavy rung never fires.
        },
        settings={
            # Fewer reruns: one retry, no cloud-heavy escalation rung.
            "max_retries": 1,
            "node_escalation_enabled": False,
            # Skip the post-hoc verification passes that add LLM round-trips.
            "faithfulness_check_enabled": False,
            "citation_faithfulness_check_enabled": False,
            "cove_check_enabled": False,
            # Bound research volume hard regardless of the depth a job picks.
            "research_max_iterations": 3,
            "research_max_urls_shallow": 15,
            "research_max_urls_medium": 20,
            "research_max_urls_deep": 25,
        },
        research_depth=QUICK_RESEARCH_DEPTH,
    ),
}

PROFILE_NAMES = tuple(PROFILES)


# ── Import-time validation + pristine-knob snapshot ─────────────────────────
# Capture the env/config default of every knob any profile can touch, BEFORE a
# profile ever mutates it — the mirror of config._ENV_MODEL_DEFAULTS. Revert
# (clear_profile) restores from here. Also fail FAST on a typo'd role/knob so a
# broken profile can't reach production.
def _build_env_knob_defaults() -> dict[str, object]:
    defaults: dict[str, object] = {}
    for prof in PROFILES.values():
        for role in prof.models:
            if role not in SWITCHABLE_ROLE_FIELDS:
                raise ValueError(
                    f"profile {prof.name!r} references non-switchable role "
                    f"{role!r} (must be one of {sorted(SWITCHABLE_ROLE_FIELDS)})"
                )
        for knob in prof.settings:
            if not hasattr(settings, knob):
                raise ValueError(
                    f"profile {prof.name!r} references unknown settings knob "
                    f"{knob!r}"
                )
            # First profile to name a knob captures the pristine default; a
            # later profile naming the same knob must NOT re-capture (it would
            # capture a possibly-already-mutated value if ordering ever changed).
            defaults.setdefault(knob, getattr(settings, knob))
    return defaults


_KNOB_ENV_DEFAULTS = _build_env_knob_defaults()


# ── Persistence (singleton runtime_profile row, migration 067) ──────────────
_UPSERT_PROFILE = text(
    "INSERT INTO runtime_profile (id, name, applied_settings, updated_at) "
    "VALUES (TRUE, :name, CAST(:applied AS JSONB), now()) "
    "ON CONFLICT (id) DO UPDATE SET "
    "name = EXCLUDED.name, applied_settings = EXCLUDED.applied_settings, "
    "updated_at = now()"
)
_DELETE_PROFILE = text("DELETE FROM runtime_profile WHERE id = TRUE")
_SELECT_PROFILE = text(
    "SELECT name, applied_settings FROM runtime_profile WHERE id = TRUE"
)


def get_profile(name: str) -> Profile:
    """Return the registered :class:`Profile` or raise ``ValueError``."""
    prof = PROFILES.get(name)
    if prof is None:
        raise ValueError(
            f"unknown profile {name!r}; must be one of {list(PROFILE_NAMES)}"
        )
    return prof


def list_profiles() -> list[dict]:
    """Registry snapshot for the ``GET /config/profile`` surface."""
    return [
        {
            "name": p.name,
            "description": p.description,
            "models": dict(p.models),
            "knobs": dict(p.settings),
            "research_depth": p.research_depth,
        }
        for p in PROFILES.values()
    ]


def quick_model_map() -> dict[str, str]:
    """The ``quick`` profile's role→model map — layered under a per-job
    ``--quick`` run's request overrides (does not touch global settings)."""
    return dict(PROFILES["quick"].models)


async def apply_profile(name: str, db) -> dict:
    """Activate ``name`` globally: swap models (persisted) + knobs (persisted).

    Raises ``ValueError`` on an unknown profile (before any mutation).
    Idempotent — re-applying the active profile just re-writes the same values.
    """
    prof = get_profile(name)

    # Models → per-role override table (persist + startup replay for free).
    for role, tag in prof.models.items():
        await set_override(role, tag, db)   # validates + commits per role

    # Knobs → live settings + a self-describing snapshot for precise revert.
    applied_knobs: dict[str, object] = {}
    for knob, value in prof.settings.items():
        setattr(settings, knob, value)
        applied_knobs[knob] = value

    snapshot = {"models": dict(prof.models), "knobs": applied_knobs}
    await db.execute(
        _UPSERT_PROFILE, {"name": name, "applied": json.dumps(snapshot)}
    )
    await db.commit()
    logger.info(
        "profile_applied name=%s models=%d knobs=%d",
        name, len(prof.models), len(applied_knobs),
    )
    return {"active": name, "models": dict(prof.models), "knobs": applied_knobs}


async def clear_profile(db) -> dict:
    """Turn off the active profile: revert every model + knob it set.

    Reverts from the STORED snapshot (self-describing), so a profile deleted
    from :data:`PROFILES` can still be cleanly turned off. No-op (returns
    ``{"active": None}``) when nothing is active. Fail-soft per item — a role
    that is no longer switchable / a knob no longer known is skipped + logged
    rather than aborting the revert half-done.
    """
    row = (await db.execute(_SELECT_PROFILE)).mappings().first()
    if row is None:
        return {"active": None, "reverted": {"models": [], "knobs": []}}

    snapshot = _coerce_snapshot(row["applied_settings"])
    reverted_models: list[str] = []
    for role in snapshot.get("models", {}):
        try:
            await clear_override(role, db)   # revert to env default + drop row
            reverted_models.append(role)
        except ValueError as exc:
            logger.warning("profile_clear_skip_model role=%s err=%s", role, exc)

    reverted_knobs: list[str] = []
    for knob in snapshot.get("knobs", {}):
        if knob in _KNOB_ENV_DEFAULTS:
            setattr(settings, knob, _KNOB_ENV_DEFAULTS[knob])
            reverted_knobs.append(knob)
        else:
            logger.warning("profile_clear_skip_knob knob=%s (unknown)", knob)

    await db.execute(_DELETE_PROFILE)
    await db.commit()
    logger.info(
        "profile_cleared name=%s models=%d knobs=%d",
        row["name"], len(reverted_models), len(reverted_knobs),
    )
    return {
        "active": None,
        "reverted": {"models": reverted_models, "knobs": reverted_knobs},
    }


async def active_profile(db) -> dict | None:
    """The globally-active profile (name + applied snapshot) or ``None``."""
    row = (await db.execute(_SELECT_PROFILE)).mappings().first()
    if row is None:
        return None
    snapshot = _coerce_snapshot(row["applied_settings"])
    return {
        "name": row["name"],
        "models": snapshot.get("models", {}),
        "knobs": snapshot.get("knobs", {}),
    }


async def load_profile_into_settings(db) -> str | None:
    """Startup hook: re-apply the active profile's KNOBS onto ``settings``.

    The model half is already replayed by the model-overrides lifespan hook
    (this profile wrote its swaps through ``set_override``), so this only
    restores the non-model knobs that a restart reset to env defaults. Must run
    AFTER migrations (needs the table) and AFTER the model-overrides replay.
    Fail-soft per knob. Returns the active profile name (or ``None``).
    """
    row = (await db.execute(_SELECT_PROFILE)).mappings().first()
    if row is None:
        return None
    snapshot = _coerce_snapshot(row["applied_settings"])
    applied = 0
    for knob, value in snapshot.get("knobs", {}).items():
        if not hasattr(settings, knob):
            logger.warning("profile_load_skip_knob knob=%s (unknown)", knob)
            continue
        setattr(settings, knob, value)
        applied += 1
    logger.info(
        "profile_loaded name=%s knobs=%d", row["name"], applied
    )
    return row["name"]


# ── Per-job --quick (jobs.quick_mode flag, migration 067) ───────────────────
# A per-JOB opt-in that is independent of the global profile: it layers the
# quick MODEL map under a single job's request overrides across every phase,
# without mutating global settings (so a --quick job never speeds up or slows
# down anyone else). Knob-tightening stays global-profile-only.
_SELECT_QUICK = text("SELECT quick_mode FROM jobs WHERE id = CAST(:job_id AS uuid)")
_MARK_QUICK = text("UPDATE jobs SET quick_mode = TRUE WHERE id = CAST(:job_id AS uuid)")


def merge_quick_overrides(request_overrides: dict | None) -> dict:
    """The quick model map with the caller's EXPLICIT overrides layered on top
    (an explicit per-request override wins over the profile default)."""
    return {**quick_model_map(), **(request_overrides or {})}


async def mark_job_quick(job_id) -> None:
    """Flag a freshly-created job as quick-mode. Fail-soft — a miss just means
    later phases run at normal speed rather than crashing the ideate call."""
    if not job_id:
        return
    try:
        async with async_session() as s:
            await s.execute(_MARK_QUICK, {"job_id": str(job_id)})
            await s.commit()
    except Exception as exc:   # noqa: BLE001 — best-effort flag, never fatal
        logger.warning("mark_job_quick_failed job_id=%s err=%s", job_id, exc)


async def resolve_job_overrides(
    job_id, request_overrides: dict | None
) -> dict | None:
    """Effective model overrides for a job phase: if the job is flagged quick,
    layer the quick model map under the caller's overrides; otherwise pass the
    caller's overrides through unchanged. Fail-soft — any read error returns the
    caller's overrides so a DB hiccup can't wedge the phase."""
    if not job_id:
        return request_overrides
    try:
        async with async_session() as s:
            row = (
                await s.execute(_SELECT_QUICK, {"job_id": str(job_id)})
            ).first()
        if row and row[0]:
            return merge_quick_overrides(request_overrides)
    except Exception as exc:   # noqa: BLE001 — read-only probe, fail open
        logger.warning(
            "resolve_job_overrides_failed job_id=%s err=%s", job_id, exc
        )
    return request_overrides


def _coerce_snapshot(raw) -> dict:
    """asyncpg may hand back JSONB as a dict already or as a JSON string
    depending on the driver/codec — normalize to a dict, fail-soft to empty."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}
