"""§17.812 (audit M5) — the two read-your-writes ingest reads pin Strong Milvus
consistency, and the dedup-check failure surfaces (M6).

These are ratchet guards: the actual read-your-writes behavior needs a live Milvus
(the collection default is Bounded ~5s staleness), so a full functional test would
require a real cluster. The source guard keeps the correctness-critical kwarg from
being silently dropped in a future refactor.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import rag_pipeline


@pytest.mark.smoke
def test_ingest_reads_pin_strong_consistency():
    src = inspect.getsource(rag_pipeline)
    # The version-chain walk (inside the predecessor lock) + the exact-hash
    # pre-filter must read their own recent writes, else the chain BRANCHES /
    # duplicates slip past under the Bounded default (audit M5).
    assert src.count('consistency_level="Strong"') >= 2, (
        "the version-walk + exact-hash ingest reads must pin Strong consistency"
    )


@pytest.mark.smoke
def test_semantic_dedup_failure_is_not_silent():
    src = inspect.getsource(rag_pipeline)
    # M6 — the semantic-dedup except must WARN + count, not debug-and-drop.
    assert 'logger.warning("semantic_dedup_failed' in src
    assert 'stats["dedup_errors"]' in src
