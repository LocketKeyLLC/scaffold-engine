"""Helpers for tests that depend on live Milvus content.

Audit B3 — the live retrieval tests (`test_rag_query_round_trip`,
`test_golden_retrieval`) hard-failed when the §17.63 SSD migration left Milvus
empty. The per-query skip marks already in `test_retrieval_golden.py` cover
"this partition lacks a specific doc" but not "the whole collection is empty."
This module is the collection-level guard.

§17.947 — UNREACHABLE IS NOT EMPTY.

The first cut swallowed every exception and returned 0, with the docstring
saying so outright: "Returns 0 if the collection doesn't exist, Milvus is
unreachable, or pymilvus isn't importable. Callers treat all those cases the
same as 'collection empty'." That is defensible for the wiped-corpus case it
was written for, and wrong for everything else — because the two conditions
deserve opposite outcomes:

  * an EMPTY collection is a data condition. The corpus really was wiped; the
    test genuinely cannot assert retrieval, and skipping is right.
  * an UNREACHABLE Milvus is an ENVIRONMENT failure. The test could not even
    ask. Skipping there reports "nothing to do" when the truth is "I could not
    look", and the run goes green having tested nothing.

That is not hypothetical. §17.946 blocked Milvus in the unit lane and the suite
reported 0 failures and 8 skips instead of 1 — seven live-retrieval
parametrizations had silently stopped existing rather than failing, and only a
skip-count comparison caught it. A guard that turns a service off must not be
indistinguishable from a corpus that happens to be empty.

So the two are now separated, and unreachable FAILS by default: a test that
needs a service it cannot reach is broken, not inapplicable. Callers that
genuinely want to tolerate an absent Milvus pass ``require_reachable=False``
and get a skip whose message says which condition applied.

Leading underscore in the filename so pytest skips collection — this file holds
helpers, not tests.
"""
from __future__ import annotations

import pytest


class MilvusUnavailable(RuntimeError):
    """Milvus could not be reached — distinct from 'the collection is empty'."""


def get_collection_entry_count(name: str = "toon_v2") -> int:
    """Live ``num_entities`` for the named collection.

    Returns 0 when the collection genuinely holds nothing (including when it
    does not exist — from a retrieval test's point of view those are the same
    data condition).

    Raises `MilvusUnavailable` when Milvus cannot be reached at all: pymilvus
    missing, connection refused, or the §17.944 test guard blocking client
    construction. That case is NOT zero rows; it is no answer.
    """
    try:
        from pymilvus import MilvusClient

        from app.config import settings
    except Exception as exc:  # noqa: BLE001 — import-time failure is unavailable
        raise MilvusUnavailable(f"pymilvus not importable: {exc!r}") from exc

    try:
        # §17.591 — MilvusClient API (ORM connections/utility/Collection removed
        # in PyMilvus 3.1). The client is its own connection handle.
        client = MilvusClient(settings.milvus_uri)
    except Exception as exc:  # noqa: BLE001 — connect/blocked
        raise MilvusUnavailable(
            f"cannot connect to Milvus at {settings.milvus_uri}: {exc!r}") from exc

    try:
        try:
            collections = client.list_collections()
        except Exception as exc:  # noqa: BLE001 — connected object, dead server
            raise MilvusUnavailable(f"Milvus query failed: {exc!r}") from exc
        if name not in collections:
            return 0
        stats = client.get_collection_stats(name)
        return int(stats.get("row_count", 0))
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass


def skip_if_milvus_empty(
    name: str = "toon_v2", *, require_reachable: bool = True,
) -> None:
    """Skip a live-retrieval test when the collection has no rows.

    An EMPTY collection skips — the corpus was wiped (e.g. post-§17.63 SSD
    migration) and there is nothing to retrieve.

    An UNREACHABLE Milvus FAILS by default. §17.947: a test that needs a
    service it cannot reach is broken, not inapplicable, and a skip there is
    how seven parametrizations disappeared without anyone noticing. Pass
    ``require_reachable=False`` to tolerate it — the skip message then says
    plainly that it was unreachable, never "empty".
    """
    try:
        count = get_collection_entry_count(name)
    except MilvusUnavailable as exc:
        if require_reachable:
            pytest.fail(
                f"Milvus is UNREACHABLE, so this live-retrieval test could not "
                f"run: {exc}\n"
                "This is an environment failure, not an empty corpus — the two "
                "are deliberately distinguished (§17.947). If the test guard is "
                "blocking client construction, this test needs the "
                "`integration` marker; if the service is down, start it. To "
                "tolerate an absent Milvus, pass require_reachable=False."
            )
        pytest.skip(f"Milvus UNREACHABLE (not empty) — {exc}")
    else:
        if count == 0:
            pytest.skip(
                f"Milvus collection {name!r} is empty — skipping live retrieval "
                "test (repopulate via /research or wait for ground-truth ingest)"
            )
