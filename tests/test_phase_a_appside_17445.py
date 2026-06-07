"""§17.445 (Phase A, app-side) — A2 research attribution + A1 NodeLog reason.

A2: research summaries were un-attributed — per-entry source URLs lived in
state.all_entries but were stripped before the summarizer and absent from the
complete payload. Now surfaced as post-hoc citation (sources[] + a Sources block).

A1: dag_nodes.last_verification_reason was written on failure but never exposed
by any read API. NodeLog now carries failure_reason.
"""
from types import SimpleNamespace

import pytest

from app.modules.research_agent import (
    _build_sources_list, _attach_sources_block, _build_research_complete_payload,
)
from app.routers.status import NodeLog


def _state(entries):
    return SimpleNamespace(
        all_entries=entries, topic="t", domain="eng", depth="medium",
        iteration=1, total_ingested=0, total_new=0, total_versioned=0,
        total_rejected=0, total_skipped_hash=0, url_history=[], search_history=[],
    )


# ───────────────────────────── A2 ─────────────────────────────

def test_sources_dedup_and_rank():
    srcs = _build_sources_list(_state([
        {"source": "http://a", "source_type": "tech_docs", "confidence_score": 0.9},
        {"source": "http://a", "source_type": "tech_docs", "confidence_score": 0.5},  # dup, lower
        {"source": "http://b", "source_type": "news", "confidence_score": 0.7},
        {"source": "", "confidence_score": 0.3},  # no url → skipped
    ]))
    assert [s["url"] for s in srcs] == ["http://a", "http://b"]  # deduped, ranked
    assert srcs[0]["confidence_score"] == 0.9  # keeps the higher-confidence dup


def test_attach_sources_block_appends_attribution():
    out = _attach_sources_block(
        "Summary text.",
        _state([{"source": "http://a", "source_type": "tech_docs", "confidence_score": 0.9}]),
    )
    assert "Summary text." in out
    assert "**Sources**" in out and "http://a" in out


def test_attach_sources_block_noop_when_no_sources():
    assert _attach_sources_block("Summary.", _state([])) == "Summary."


def test_complete_payload_carries_sources():
    payload = _build_research_complete_payload(
        _state([{"source": "http://a", "source_type": "x", "confidence_score": 0.8}]),
        "sess1", mode="topic", duration_ms=10, summary="s",
    )
    assert "sources" in payload
    assert payload["sources"][0]["url"] == "http://a"


# ───────────────────────────── A1 ─────────────────────────────

def test_nodelog_carries_failure_reason():
    n = NodeLog(node_key="T1", title="t", tool="LLM", status="failed",
                failure_reason="verifier: missing import X")
    assert n.failure_reason == "verifier: missing import X"


def test_nodelog_failure_reason_optional():
    n = NodeLog(node_key="T1", title="t", tool="LLM", status="done")
    assert n.failure_reason is None
