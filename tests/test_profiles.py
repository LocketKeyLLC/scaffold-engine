"""§17.809 — runtime compute profiles ("quick mode") tests.

Mock-DB level (mirrors test_model_overrides): apply mutates the live settings
singleton + the model_overrides layer + writes a snapshot; clear reverts both
halves from the stored snapshot; load re-applies knobs. The router endpoints are
thin wrappers called directly with an AsyncMock db (no TestClient/services), and
the per-job --quick merge is asserted on the pure helper + a patched session.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app import config
from app.modules import profiles as pf
from app.routers import profiles as rt
from app.schemas import ProfileApplyInput


# ── helpers ─────────────────────────────────────────────────────────────────
def _mock_db(profile_row=None, quick_row=None):
    """AsyncMock db whose execute() returns a result exposing both
    ``.mappings().first()`` (profile SELECT) and ``.first()`` (quick SELECT)."""
    def _execute(_stmt, _params=None):
        res = MagicMock()
        res.mappings.return_value.first.return_value = profile_row
        res.first.return_value = quick_row
        return res
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


def _snapshot_settings(prof: pf.Profile) -> dict:
    """Capture every settings attr a profile touches, for restore in finally."""
    snap = {r: getattr(config.settings, r) for r in prof.models}
    snap.update({k: getattr(config.settings, k) for k in prof.settings})
    return snap


def _restore_settings(snap: dict) -> None:
    for k, v in snap.items():
        setattr(config.settings, k, v)


# ── registry / validation ───────────────────────────────────────────────────
@pytest.mark.smoke
def test_quick_profile_registered_and_valid():
    assert "quick" in pf.PROFILES
    quick = pf.PROFILES["quick"]
    # Every model role is switchable (import-time guard would have raised).
    assert set(quick.models).issubset(config.SWITCHABLE_ROLE_FIELDS)
    # Operator's speed-verified pick landed on the verifier.
    assert quick.models["model_verifier"] == "kimi-k2.6:cloud"
    # Knob snapshot captured a pristine default for each knob the profile sets.
    for knob in quick.settings:
        assert knob in pf._KNOB_ENV_DEFAULTS


@pytest.mark.smoke
def test_quick_profile_bounds_execution():
    """§17.809 — the execution-side levers that get quick mode under 5 min:
    fewer/coarser nodes, a wider frontier, and no per-node reranker/optimize
    overhead. Defaults stay ON everywhere else (flipped only by the profile)."""
    quick = pf.PROFILES["quick"].settings
    assert quick["dag_max_nodes"] == 6                       # cap the build
    assert quick["parallel_execution_max_inflight"] == 4     # wider frontier
    assert quick["execution_rerank_enabled"] is False        # skip ~21s/node CPU rerank
    assert quick["execution_optimize_enabled"] is False      # skip ~6s/node optimize
    # These are real settings attrs with ON defaults (guarded at import).
    assert config.settings.execution_rerank_enabled is True
    assert config.settings.execution_optimize_enabled is True


@pytest.mark.smoke
def test_get_profile_unknown_raises():
    with pytest.raises(ValueError):
        pf.get_profile("nope")


@pytest.mark.smoke
def test_list_profiles_shape():
    listed = pf.list_profiles()
    names = {p["name"] for p in listed}
    assert "quick" in names
    quick = next(p for p in listed if p["name"] == "quick")
    assert quick["knobs"] and quick["models"]


# ── per-job --quick merge ────────────────────────────────────────────────────
@pytest.mark.smoke
def test_merge_quick_overrides_caller_wins():
    merged = pf.merge_quick_overrides({"model_general": "MINE:1b"})
    assert merged["model_general"] == "MINE:1b"          # explicit override wins
    assert merged["model_verifier"] == "kimi-k2.6:cloud"  # from the quick map


@pytest.mark.smoke
def test_merge_quick_overrides_none():
    assert pf.merge_quick_overrides(None) == pf.quick_model_map()


@pytest.mark.smoke
async def test_resolve_job_overrides_passthrough_when_not_quick():
    # quick_row falsy → caller's overrides returned unchanged.
    with patch.object(pf, "async_session") as sess:
        sess.return_value.__aenter__.return_value = _mock_db(quick_row=(False,))
        out = await pf.resolve_job_overrides("job-1", {"model_coder": "x:1b"})
    assert out == {"model_coder": "x:1b"}


@pytest.mark.smoke
async def test_resolve_job_overrides_merges_when_quick():
    with patch.object(pf, "async_session") as sess:
        sess.return_value.__aenter__.return_value = _mock_db(quick_row=(True,))
        out = await pf.resolve_job_overrides("job-1", {"model_coder": "x:1b"})
    assert out["model_coder"] == "x:1b"                    # caller wins
    assert out["model_verifier"] == "kimi-k2.6:cloud"      # quick layered in


@pytest.mark.smoke
async def test_resolve_job_overrides_no_job_id():
    assert await pf.resolve_job_overrides(None, {"a": "b"}) == {"a": "b"}


# ── apply / clear / load (settings mutation + persistence) ───────────────────
@pytest.mark.smoke
async def test_apply_profile_mutates_and_persists():
    quick = pf.PROFILES["quick"]
    snap = _snapshot_settings(quick)
    db = _mock_db()
    try:
        result = await pf.apply_profile("quick", db)
        # Models applied to the live settings singleton.
        assert config.settings.model_verifier == "kimi-k2.6:cloud"
        # Knobs applied.
        assert config.settings.max_retries == 1
        assert config.settings.node_escalation_enabled is False
        # Snapshot written (last execute is the runtime_profile UPSERT).
        assert db.commit.await_count >= 1
        assert result["active"] == "quick"
        assert result["knobs"]["max_retries"] == 1
    finally:
        _restore_settings(snap)


@pytest.mark.smoke
async def test_apply_profile_unknown_raises_before_mutation():
    before = config.settings.max_retries
    db = _mock_db()
    with pytest.raises(ValueError):
        await pf.apply_profile("bogus", db)
    assert config.settings.max_retries == before
    assert db.execute.await_count == 0        # nothing written


@pytest.mark.smoke
async def test_clear_profile_reverts_from_snapshot():
    quick = pf.PROFILES["quick"]
    snap = _snapshot_settings(quick)
    # Pretend quick is active: settings mutated + a stored snapshot row.
    config.settings.max_retries = 1
    config.settings.model_verifier = "kimi-k2.6:cloud"
    row = {
        "name": "quick",
        "applied_settings": {
            "models": {"model_verifier": "kimi-k2.6:cloud"},
            "knobs": {"max_retries": 1},
        },
    }
    db = _mock_db(profile_row=row)
    try:
        out = await pf.clear_profile(db)
        assert out["active"] is None
        # Reverted to env defaults captured at import.
        assert config.settings.max_retries == pf._KNOB_ENV_DEFAULTS["max_retries"]
        assert config.settings.model_verifier == config.env_default_model("model_verifier")
    finally:
        _restore_settings(snap)


@pytest.mark.smoke
async def test_clear_profile_noop_when_none_active():
    db = _mock_db(profile_row=None)
    out = await pf.clear_profile(db)
    assert out["active"] is None
    assert out["reverted"] == {"models": [], "knobs": []}


@pytest.mark.smoke
async def test_load_profile_reapplies_knobs():
    quick = pf.PROFILES["quick"]
    snap = _snapshot_settings(quick)
    row = {
        "name": "quick",
        "applied_settings": {"models": {}, "knobs": {"max_retries": 1}},
    }
    db = _mock_db(profile_row=row)
    try:
        config.settings.max_retries = pf._KNOB_ENV_DEFAULTS["max_retries"]  # reset (restart)
        name = await pf.load_profile_into_settings(db)
        assert name == "quick"
        assert config.settings.max_retries == 1
    finally:
        _restore_settings(snap)


@pytest.mark.smoke
async def test_load_profile_none_when_empty():
    db = _mock_db(profile_row=None)
    assert await pf.load_profile_into_settings(db) is None


@pytest.mark.smoke
def test_coerce_snapshot_accepts_str_and_dict():
    d = {"models": {}, "knobs": {"max_retries": 1}}
    assert pf._coerce_snapshot(d) == d
    assert pf._coerce_snapshot(json.dumps(d)) == d
    assert pf._coerce_snapshot("not-json") == {}
    assert pf._coerce_snapshot(None) == {}


# ── router endpoints (thin wrappers, direct call) ────────────────────────────
@pytest.mark.smoke
async def test_get_endpoint_envelope():
    db = _mock_db(profile_row=None)
    out = await rt.get_profile_endpoint(db)
    assert out["active"] is None
    assert any(p["name"] == "quick" for p in out["available"])


@pytest.mark.smoke
async def test_post_endpoint_unknown_is_404():
    db = _mock_db()
    with pytest.raises(HTTPException) as exc:
        await rt.apply_profile_endpoint(ProfileApplyInput(name="bogus"), db)
    assert exc.value.status_code == 404


@pytest.mark.smoke
async def test_post_endpoint_applies_quick():
    quick = pf.PROFILES["quick"]
    snap = _snapshot_settings(quick)
    db = _mock_db()
    try:
        out = await rt.apply_profile_endpoint(ProfileApplyInput(name="quick"), db)
        assert out["active"] == "quick"
    finally:
        _restore_settings(snap)


@pytest.mark.smoke
async def test_apply_profile_persists_snapshot_before_overrides(monkeypatch):
    """§17.812 (audit M4) — the profile snapshot row is written + committed
    BEFORE any per-role override, so a mid-apply failure leaves a RECOVERABLE
    row rather than settings-mutated-with-no-snapshot (which stranded the engine
    in quick mode with active_profile()=None and clear_profile() a no-op)."""
    quick = pf.PROFILES["quick"]
    snap = _snapshot_settings(quick)
    db = _mock_db()
    boom = AsyncMock(side_effect=RuntimeError("override write failed"))
    monkeypatch.setattr(pf, "set_override", boom)
    try:
        with pytest.raises(RuntimeError):
            await pf.apply_profile("quick", db)
        # The snapshot upsert ran + committed before the override blew up.
        assert db.commit.await_count >= 1
        first = db.execute.await_args_list[0]
        params = first.args[1]
        assert params["name"] == "quick"
        applied = json.loads(params["applied"])
        assert set(applied) == {"models", "knobs"}
        assert applied["knobs"]["max_retries"] == 1
        assert applied["models"]  # full intended model map is persisted
    finally:
        _restore_settings(snap)
