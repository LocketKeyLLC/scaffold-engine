"""§17.578 — best-of-N candidate selection (_best_of_n_inference).

Generates N candidates concurrently, judges each by grounding (faithfulness vs
upstream evidence), returns the best. Fail-soft. Patches the lazily-imported
score_faithfulness at its source.
"""
from unittest.mock import AsyncMock

import pytest

import app.modules.faithfulness as fa
from app.config import settings
from app.modules import execution_agent as ea


@pytest.fixture(autouse=True)
def _n2(monkeypatch):
    monkeypatch.setattr(settings, "best_of_n_count", 2)


@pytest.mark.asyncio
async def test_picks_highest_grounding(monkeypatch):
    monkeypatch.setattr(settings, "best_of_n_count", 3)
    outs = iter(["cand A", "cand B", "cand C"])

    async def gen():
        return next(outs)

    score_map = {"cand A": 0.4, "cand B": 0.9, "cand C": 0.6}

    async def fake_score(text, evidence, **k):
        return {"score": score_map[text]}

    monkeypatch.setattr(fa, "score_faithfulness", fake_score)
    out = await ea._best_of_n_inference(gen, "evidence", "T1")
    assert out == "cand B"                      # highest grounding wins


@pytest.mark.asyncio
async def test_single_candidate_skips_scoring(monkeypatch):
    calls = {"n": 0}

    async def gen():
        calls["n"] += 1
        if calls["n"] == 1:
            return "good"
        raise RuntimeError("boom")             # 2nd of the N fails

    sf = AsyncMock()
    monkeypatch.setattr(fa, "score_faithfulness", sf)
    out = await ea._best_of_n_inference(gen, "ev", "T1")
    assert out == "good"
    sf.assert_not_awaited()                     # 1 surviving candidate → no judging


@pytest.mark.asyncio
async def test_all_fail_falls_back_to_single(monkeypatch):
    calls = {"n": 0}

    async def gen():
        calls["n"] += 1
        if calls["n"] <= 2:                      # both gather candidates fail
            raise RuntimeError("boom")
        return "fallback"                        # the fallback gen_fn() call

    out = await ea._best_of_n_inference(gen, "ev", "T1")
    assert out == "fallback"
