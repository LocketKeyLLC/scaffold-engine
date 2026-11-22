"""§17.1995 — in a CodeGen step's output a float literal is not a version and a subscript is not a citation.

Live (dashboard job 69fe61c4, T5 "Write metrics computation module", every attempt 2026-11-22): the evidence layer
reported "⚠️ Unverified specifics … `0.05` (version), `0.06` (version), `0.10` (version), `0.25` (version)…" -- the
module's thresholds -- and "Weak sourcing — 7 of 7 cited statements are not backed", citation_score 0.0: `rows[1]`-style
subscripts parsed as `[n]` citation markers and sent to the citation judge (a model call each attempt). The report never
touches a CodeGen output (annotate=False, no redraw), but it is kept on `dag_nodes.evidence`, shown on the Run tab, and
its `unsupported` values are FLAGGED for every downstream step (`fetch_upstream_flagged`) and for the compiled
deliverable (`compile_value_check`): a later step that states `0.05` is never credited by the upstream that wrote it.
Measured on the stored corpus: 49 of 118 CodeGen outputs and 9 of 35 failed drafts carried a two-decimal "version"
(`0.05` 23, `0.03` 16, `0.15` 14 …); 68 and 26 carried subscripts the citation parser took for markers.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.modules.execution_agent  # noqa: F401 — binds the real app.database first (see test_execution_evidence)
from app.modules import assist_evidence as ev
from app.modules import execution_evidence as xe


CODE = '''```python
# app/modules/span_metrics.py
import httpx

THRESHOLDS = {"drawdown": 0.10, "alpha": 0.05, "beta": 0.25}
CLIENT_VERSION = "2.31.0"


def latest(rows):
    return rows[1] if len(rows) > 1 else rows[0]


def fetch():
    return httpx.get("https://quotes.vendor-feed.test/v9/bars")
```
'''
SOURCES = [{"kind": "web", "url": "https://docs.vendor-feed.test/", "text": "The bars endpoint returns OHLC rows."}]


@pytest.fixture(autouse=True)
def _valves(monkeypatch):
    monkeypatch.setattr(ev.settings, "assist_answer_verification_enabled", True)
    monkeypatch.setattr(ev.settings, "assist_answer_verification_regenerate", True)
    monkeypatch.setattr(xe.settings, "execution_answer_verification_enabled", True)


def _kw(tool: str):
    return dict(need=None, sources=SOURCES, brief={"title": "Metrics", "description": "Compute portfolio metrics."},
                input_text="", task_text="Write the metrics computation module.", upstream_text="",
                upstream_flagged=set(), tool=tool, node_key="T5", regenerate=AsyncMock(return_value=""))


def test_the_extractor_reads_code_as_code_and_prose_as_before():
    in_code = {f["value"] for f in ev.extract_specifics(CODE, code=True)}
    assert {"0.10", "0.05", "0.25"}.isdisjoint(in_code)
    assert {"2.31.0", "https://quotes.vendor-feed.test/v9/bars"} <= in_code      # real claims still read
    prose = {f["value"] for f in ev.extract_specifics(CODE)}
    assert {"0.10", "0.05", "0.25", "2.31.0"} <= prose                          # prose unchanged


@pytest.mark.asyncio
async def test_a_code_steps_report_flags_no_literal_and_spends_no_citation_judge(monkeypatch):
    judge = AsyncMock(return_value={"score": 0.0, "instances": []})
    monkeypatch.setattr(ev, "citation_report", judge)
    out, rep = await xe.verify_node_output(CODE, **_kw("CodeGen"))
    assert out == CODE
    flagged = {u["value"] for u in rep["unsupported"]}
    assert flagged == {"2.31.0", "https://quotes.vendor-feed.test/v9/bars"}
    assert "(version)" not in rep["footer"].replace("`2.31.0` (version)", "")
    assert "Weak sourcing" not in rep["footer"]
    judge.assert_not_awaited()
    assert xe.evidence_summary(rep)["citation_score"] is None


@pytest.mark.asyncio
async def test_the_same_text_on_a_prose_step_is_still_checked_in_full(monkeypatch):
    judge = AsyncMock(return_value=None)
    monkeypatch.setattr(ev, "citation_report", judge)
    _, rep = await xe.verify_node_output(CODE, **_kw("LLM"))
    assert {"0.10", "0.05", "0.25"} <= {u["value"] for u in rep["unsupported"]}
    judge.assert_awaited()
