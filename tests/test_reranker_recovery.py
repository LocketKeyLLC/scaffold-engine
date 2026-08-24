"""§17.812 (audit C4) — reranker load-failure visibility + cooldown self-heal.

The CrossEncoder load failure was STICKY for the process lifetime (reset_reranker
had no caller), so a transient boot failure pinned every query to RRF-only until
a container restart. It now exposes reranker_load_failed() (for /health) and
retries the load after a cooldown.
"""
from __future__ import annotations

import sys
import types

import pytest

import app.rerankers as rr


@pytest.fixture(autouse=True)
def _reset_reranker_state():
    """Snapshot + restore the module globals so tests don't leak state."""
    saved = (rr._cross_encoder, rr._load_failed, rr._load_failed_at)
    rr._cross_encoder = None
    rr._load_failed = False
    rr._load_failed_at = 0.0
    yield
    rr._cross_encoder, rr._load_failed, rr._load_failed_at = saved


def _fake_sentence_transformers(cross_encoder_factory):
    """Install a fake sentence_transformers module whose CrossEncoder is the
    given factory (so _get_cross_encoder's function-local import resolves it)."""
    mod = types.ModuleType("sentence_transformers")
    mod.CrossEncoder = cross_encoder_factory
    return mod


@pytest.mark.smoke
def test_load_failed_accessor_reflects_flag(monkeypatch):
    monkeypatch.setattr(rr, "_load_failed", False)
    assert rr.reranker_load_failed() is False
    monkeypatch.setattr(rr, "_load_failed", True)
    assert rr.reranker_load_failed() is True


@pytest.mark.smoke
def test_within_cooldown_stays_down(monkeypatch):
    # Hard-failed just now → still within cooldown → returns None without retry.
    rr._load_failed = True
    rr._load_failed_at = 1_000_000.0
    monkeypatch.setattr(rr.time, "monotonic", lambda: 1_000_000.0 + 10.0)
    assert rr._get_cross_encoder() is None
    assert rr.reranker_load_failed() is True


@pytest.mark.smoke
def test_retries_and_recovers_after_cooldown(monkeypatch):
    # Hard-failed long ago → past cooldown → retry the load, which now succeeds.
    rr._load_failed = True
    rr._load_failed_at = 1_000_000.0
    monkeypatch.setattr(
        rr.time, "monotonic",
        lambda: 1_000_000.0 + rr._LOAD_RETRY_COOLDOWN_S + 5.0,
    )
    sentinel = object()
    fake = _fake_sentence_transformers(lambda *a, **k: sentinel)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    got = rr._get_cross_encoder()
    assert got is sentinel
    assert rr.reranker_load_failed() is False  # cleared on successful recovery


@pytest.mark.smoke
def test_load_failure_sets_cooldown_timestamp(monkeypatch):
    # A load that raises on every attempt marks _load_failed + stamps _load_failed_at.
    def _boom(*a, **k):
        raise RuntimeError("model load boom")

    fake = _fake_sentence_transformers(_boom)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(rr.time, "monotonic", lambda: 2_000_000.0)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)  # no real backoff sleep

    assert rr._get_cross_encoder() is None
    assert rr.reranker_load_failed() is True
    assert rr._load_failed_at == 2_000_000.0
