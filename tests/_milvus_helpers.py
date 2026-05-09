"""Helpers for tests that depend on live Milvus content.

Audit B3 — the live retrieval tests (`test_rag_query_round_trip`,
`test_golden_retrieval`) hard-failed when the §17.63 SSD migration left
Milvus empty. The per-query skip marks already in
`test_retrieval_golden.py` cover "this partition lacks a specific doc"
but not "the whole collection is empty." This module is the
collection-level guard.

Leading underscore in the filename so pytest skips collection — this
file holds helpers, not tests.
"""
from __future__ import annotations

import pytest


def get_collection_entry_count(name: str = "toon_v2") -> int:
    """Return live ``num_entities`` for the named collection.

    Returns 0 if the collection doesn't exist, Milvus is unreachable, or
    pymilvus isn't importable. Callers treat all those cases the same as
    "collection empty" — the test should skip rather than fail.
    """
    try:
        from pymilvus import Collection, connections, utility

        from app.config import settings

        try:
            connections.connect(alias="default", uri=settings.milvus_uri)
        except Exception:
            # Idempotent if already connected; if connect fails outright,
            # the list_collections() call below will raise and we land in
            # the broad except.
            pass

        if name not in utility.list_collections():
            return 0
        return Collection(name).num_entities
    except Exception:
        return 0


def skip_if_milvus_empty(name: str = "toon_v2") -> None:
    """``pytest.skip`` if the live collection has zero entries.

    Use as the first line of a live-retrieval test to make it skip
    gracefully when Milvus has been wiped (e.g. post-§17.63 SSD
    migration) instead of hard-failing on ``assert len(docs) > 0``.
    """
    if get_collection_entry_count(name) == 0:
        pytest.skip(
            f"Milvus collection {name!r} is empty — skipping live retrieval test "
            "(repopulate via /research or wait for ground-truth ingest)"
        )
