"""§17.495 — unit tests for the model A/B harness scoring/aggregation.

Pure logic only (no model calls); `score_codegen` reuses the real
`check_golden` structural checker, so these also pin that integration.
"""
from __future__ import annotations

import pytest

from scripts.model_ab import _avg, _summarize, score_codegen


_GOLDEN = {
    "id": "module-function",
    "brief": "Write generate_filename(prefix, ext).",
    "must_parse": True,
    "must_define": ["generate_filename"],
    "must_not_contain": ["argparse", "__main__"],
}
_GOOD = "```python\ndef generate_filename(prefix: str, ext: str) -> str:\n    return f'{prefix}.{ext}'\n```"


def test_score_codegen_pass():
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="pass")
    assert s["passed"] is True
    assert s["structural_failures"] == []


def test_score_codegen_structural_fail_blocks_pass():
    bad = "```python\ndef other(): pass\n```"  # missing generate_filename
    s = score_codegen(_GOLDEN, bad, exec_verdict="pass")
    assert s["passed"] is False
    assert any("generate_filename" in f for f in s["structural_failures"])


def test_score_codegen_exec_fail_blocks_pass():
    # structural ok, but the sandbox said the code does not run
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="fail")
    assert s["passed"] is False


def test_score_codegen_exec_skip_does_not_block():
    # skip = couldn't run standalone (e.g. sibling import) — not held against it
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="skip")
    assert s["passed"] is True


def test_score_codegen_empty_output_fails():
    s = score_codegen(_GOLDEN, "", exec_verdict="skip")
    assert s["passed"] is False
    assert s["structural_failures"] == ["empty output"]


def test_summarize_aggregates_pass_error_metrics():
    rows = [
        {"model": "A", "ok": True, "passed": True, "wall_s": 2.0, "tokens_per_sec": 30.0, "ttft_ms": 800},
        {"model": "A", "ok": True, "passed": False, "wall_s": 4.0, "tokens_per_sec": 20.0, "ttft_ms": 1200},
        {"model": "A", "ok": False, "error": "boom"},
        {"model": "B", "ok": True, "passed": True, "wall_s": 1.0, "tokens_per_sec": 50.0, "ttft_ms": 400},
    ]
    s = _summarize(rows)
    assert s["A"]["trials"] == 3 and s["A"]["passed"] == 1 and s["A"]["errors"] == 1
    assert _avg(s["A"]["wall_s"]) == 3.0
    assert s["B"]["passed"] == 1 and s["B"]["errors"] == 0


def test_avg_empty_is_zero():
    assert _avg([]) == 0.0


# ── §17.495 — the critical correctness property: never score a fallback ──────


@pytest.mark.asyncio
async def test_run_one_rejects_fallback_used(monkeypatch):
    """generate() always computes a smart-fallback; an unavailable candidate
    that fell back must be reported unavailable, NEVER scored as the candidate."""
    from app import model_router
    from app.providers.base import ModelResponse
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model="qwen3.5:397b-cloud",
                             success=True, fallback_used=True)
    monkeypatch.setattr(model_router, "generate", _gen)

    r = await _run_one("qwen3-coder:480b-cloud", _GOLDEN,
                       system="s", temperature=0.2, max_tokens=512)
    assert r["ok"] is False and r["passed"] is False
    assert "fell back" in r["error"]


@pytest.mark.asyncio
async def test_run_one_rejects_model_mismatch(monkeypatch):
    """Even without fallback_used, a resolved model that differs is rejected."""
    from app import model_router
    from app.providers.base import ModelResponse
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model="some-other-model",
                             success=True, fallback_used=False)
    monkeypatch.setattr(model_router, "generate", _gen)

    r = await _run_one("candidate:cloud", _GOLDEN,
                       system="s", temperature=0.2, max_tokens=512)
    assert r["ok"] is False and "fell back" in r["error"]


@pytest.mark.asyncio
async def test_run_one_scores_matching_model(monkeypatch):
    """The requested model actually answered → it gets scored."""
    from app import model_router
    import app.sandbox.codegen_check as cc
    from app.providers.base import ModelResponse
    from app.sandbox.codegen_check import ExecCheckResult
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model=model, success=True, fallback_used=False)

    async def _exec(output, **k):
        return ExecCheckResult("pass", "ran cleanly")

    monkeypatch.setattr(model_router, "generate", _gen)
    monkeypatch.setattr(cc, "codegen_exec_smoke", _exec)

    r = await _run_one("qwen3.5:397b-cloud", _GOLDEN,
                       system="s", temperature=0.2, max_tokens=512)
    assert r["ok"] is True and r["passed"] is True
    assert r["resolved_model"] == "qwen3.5:397b-cloud"
