"""Tests for scripts/flatten_branched_chains.py — the §17.270 sweeper
that flattens pre-§17.269 branched version chains in Milvus.

Coverage:
  - linear chain → no rewrites, no branches found
  - 2-way branch → exactly 1 rewrite, exactly 1 branch found
  - 3-way branch → exactly 2 rewrites, exactly 1 branch found
  - cascading branch (branch under a branch) → multiple rewrites
  - dry-run mode does NOT call collection.upsert
  - --apply mode DOES call collection.upsert
  - empty domain is a no-op
  - cycle is detected and aborted (BFS visit cap)
"""
import re as _re
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ──────────────────────────────────────────────────────────────────────
# Stateful collection mock — captures upserts, walks them for queries.
# ──────────────────────────────────────────────────────────────────────

def _make_stateful_collection(initial_rows: list[dict]):
    """Build a Milvus collection mock seeded with `initial_rows`.

    Supports the queries the flatten script issues:
      - `domain == "X"`                          → full scan, paginated
      - `entry_id == "X" and domain == "Y"`      → single-row fetch
      - `supersedes_id == "X" and ...`           → (not used by flatten,
                                                   present so other tests
                                                   could share this helper)

    Captures `collection.upsert([row])` into `_upserted_rows` AND
    mutates the in-mock row in place so subsequent queries see the
    rewrite — what production Milvus does after upsert+flush.
    """
    collection = MagicMock()
    rows_by_eid: dict[str, dict] = {r["entry_id"]: dict(r) for r in initial_rows}

    def _stateful_query(expr, output_fields=None, limit=None, offset=0, **kw):
        # domain-wide scan
        m_dom_only = _re.fullmatch(r'domain == "([^"]+)"', expr)
        if m_dom_only:
            d = m_dom_only.group(1)
            matched = [r for r in rows_by_eid.values() if r.get("domain") == d]
            matched.sort(key=lambda r: r["entry_id"])  # stable order
            start = offset or 0
            end = (start + limit) if limit else len(matched)
            return matched[start:end]
        # single-row fetch
        m_eid = _re.fullmatch(
            r'entry_id == "([^"]+)" and domain == "([^"]+)"', expr,
        )
        if m_eid:
            eid, d = m_eid.group(1), m_eid.group(2)
            r = rows_by_eid.get(eid)
            return [r] if r and r.get("domain") == d else []
        # supersedes walk (not used by flatten but kept generic)
        m_sup = _re.search(r'supersedes_id == "([^"]+)"', expr)
        if m_sup:
            target = m_sup.group(1)
            matched = [
                r for r in rows_by_eid.values()
                if r.get("supersedes_id") == target
            ]
            return matched[:limit] if limit else matched
        return []

    collection.query.side_effect = _stateful_query

    upserted_rows: list[dict] = []

    def _stateful_upsert(rows):
        for r in rows:
            eid = r["entry_id"]
            upserted_rows.append(dict(r))
            # Mutate the stored row so subsequent queries see the new state.
            if eid in rows_by_eid:
                rows_by_eid[eid].update(r)
            else:
                rows_by_eid[eid] = dict(r)

    collection.upsert.side_effect = _stateful_upsert
    collection._upserted_rows = upserted_rows
    collection._rows_by_eid = rows_by_eid
    return collection


def _row(entry_id, version, supersedes_id, created_at, domain="eng"):
    """Helper for compact test fixtures."""
    return {
        "entry_id": entry_id,
        "version": version,
        "supersedes_id": supersedes_id,
        "created_at": created_at,
        "domain": domain,
        # Minimal extra fields for the fetch-and-upsert round-trip in _apply_rewrite
        "title": f"row-{entry_id}",
        "canonical_text": f"content-{entry_id}",
        "domain_tags": ["t"],
        "confidence_score": 0.8,
        "source_type": "tech_docs",
        "source_url": "",
        "content_hash": f"h-{entry_id}",
        "model_id": "mock-embedder",
        "updated_at": created_at,
        "expires_at": 0,
        "dense_vector": [0.1] * 512,
    }


def _mock_session_for_lock():
    """Mock async_session that accepts the _predecessor_lock SQL."""
    s = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    return s


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_linear_chain_finds_no_branches():
    """Linear chain A → B → C: no branches, no rewrites in any mode."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=3, supersedes_id="B", created_at=300),
    ]
    collection = _make_stateful_collection(rows)
    from scripts.flatten_branched_chains import flatten_domain
    br, rw = await flatten_domain(collection, "eng", apply_mode=False)
    assert br == 0, f"linear chain should find 0 branches; got {br}"
    assert rw == 0, f"linear chain should plan 0 rewrites; got {rw}"
    assert collection._upserted_rows == [], "dry-run must never upsert"


@pytest.mark.asyncio
async def test_two_way_branch_dry_run_reports_one_rewrite():
    """A has two children B and C both at version=2 (the §17.267 race
    result). Dry-run should report 1 branch + 1 planned rewrite, and
    NOT call collection.upsert."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),  # branch
    ]
    collection = _make_stateful_collection(rows)
    from scripts.flatten_branched_chains import flatten_domain
    br, rw = await flatten_domain(collection, "eng", apply_mode=False)
    assert br == 1, f"expected 1 branch; got {br}"
    assert rw == 1, f"expected 1 planned rewrite; got {rw}"
    assert collection._upserted_rows == [], "dry-run must never upsert"


@pytest.mark.asyncio
async def test_two_way_branch_apply_relinks_younger_sibling():
    """A has two children B (created 200) and C (created 210). APPLY:
    B stays linked to A; C re-linked to B with version=3. The flatten
    must acquire the predecessor lock around the rewrite."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()
    with patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br, rw = await flatten_domain(collection, "eng", apply_mode=True)

    assert br == 1
    assert rw == 1
    # Exactly one upsert fired: C re-linked.
    assert len(collection._upserted_rows) == 1, (
        f"expected 1 upsert (C re-linked); got {len(collection._upserted_rows)}"
    )
    rewritten = collection._upserted_rows[0]
    assert rewritten["entry_id"] == "C"
    assert rewritten["supersedes_id"] == "B", (
        f"C should now point at B; got {rewritten['supersedes_id']}"
    )
    assert rewritten["version"] == 3, (
        f"C should be at version=3 (B.version + 1); got {rewritten['version']}"
    )
    # The advisory-lock SQL must have fired with the OLD predecessor (A).
    lock_calls = [
        c for c in session.execute.await_args_list
        if "pg_advisory_xact_lock" in str(c.args[0])
    ]
    assert lock_calls, "expected pg_advisory_xact_lock to fire during rewrite"
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_three_way_branch_apply_produces_linear_chain():
    """A has three children B, C, D all at version=2. APPLY:
    B keeps supersedes=A,v=2; C → supersedes=B,v=3; D → supersedes=C,v=4."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
        _row("D", version=2, supersedes_id="A", created_at=220),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()
    with patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br, rw = await flatten_domain(collection, "eng", apply_mode=True)

    assert br == 1, f"expected 1 branch point (under A); got {br}"
    assert rw == 2, f"expected 2 rewrites (C + D); got {rw}"
    # Check final state in the stateful mock — both C and D re-linked.
    state = collection._rows_by_eid
    assert state["B"]["supersedes_id"] == "A" and state["B"]["version"] == 2
    assert state["C"]["supersedes_id"] == "B" and state["C"]["version"] == 3
    assert state["D"]["supersedes_id"] == "C" and state["D"]["version"] == 4


@pytest.mark.asyncio
async def test_cascading_branch_under_a_branched_node():
    """A has children B and C; C also has its own child D (originally
    v=3, supersedes=C). After flatten:
      A → B (v=2) → C (v=3) → D (v=4)
    D needs re-versioning even though its parent (C) didn't change eid;
    C's version changed from 2 → 3, so D should be 4 (C.version + 1)."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
        _row("D", version=3, supersedes_id="C", created_at=220),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()
    with patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br, rw = await flatten_domain(collection, "eng", apply_mode=True)

    state = collection._rows_by_eid
    assert state["C"]["supersedes_id"] == "B" and state["C"]["version"] == 3
    assert state["D"]["supersedes_id"] == "C" and state["D"]["version"] == 4, (
        f"D should follow re-versioned C; got {state['D']}"
    )
    assert br == 1, f"only one branch point (under A); got {br}"
    # 2 rewrites: C (supersedes change) + D (version change to follow C).
    assert rw == 2, f"expected 2 rewrites; got {rw}"


@pytest.mark.asyncio
async def test_apply_is_idempotent():
    """Run --apply, then run again. Second pass finds zero branches."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()
    with patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br1, rw1 = await flatten_domain(collection, "eng", apply_mode=True)
        # Reset upsert capture so second pass shows it didn't write anything.
        collection._upserted_rows.clear()
        br2, rw2 = await flatten_domain(collection, "eng", apply_mode=True)

    assert (br1, rw1) == (1, 1)
    assert br2 == 0, f"second pass should find 0 branches; got {br2}"
    assert rw2 == 0, f"second pass should do 0 rewrites; got {rw2}"
    assert collection._upserted_rows == [], (
        "second pass must not upsert (chain already linear)"
    )


@pytest.mark.asyncio
async def test_empty_domain_is_no_op():
    """No rows in the domain — script returns (0, 0) cleanly."""
    collection = _make_stateful_collection([])
    from scripts.flatten_branched_chains import flatten_domain
    br, rw = await flatten_domain(collection, "eng", apply_mode=False)
    assert (br, rw) == (0, 0)


@pytest.mark.asyncio
async def test_dry_run_does_not_acquire_lock():
    """Dry-run should NEVER open async_session or fire pg_advisory_xact_lock."""
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()
    with patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br, rw = await flatten_domain(collection, "eng", apply_mode=False)

    assert br == 1 and rw == 1
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_is_aborted_by_bfs_cap():
    """Pathological data with a cycle (A→B, B→A) must not hang. The
    BFS visit cap (4 * row_count) trips and the root is abandoned."""
    rows = [
        _row("A", version=1, supersedes_id="B", created_at=100),  # cycle
        _row("B", version=2, supersedes_id="A", created_at=200),
    ]
    collection = _make_stateful_collection(rows)
    from scripts.flatten_branched_chains import flatten_domain
    # No root (every node has a parent), so BFS never starts — clean exit.
    br, rw = await flatten_domain(collection, "eng", apply_mode=False)
    assert br == 0, f"cycle with no root → no branches; got {br}"
    assert rw == 0, f"cycle with no root → no rewrites; got {rw}"


@pytest.mark.asyncio
async def test_apply_logs_upsert_failure_but_continues(caplog):
    """If collection.upsert raises on one row, the sweep logs and moves
    on rather than aborting the whole pass."""
    import logging
    rows = [
        _row("A", version=1, supersedes_id="",  created_at=100),
        _row("B", version=2, supersedes_id="A", created_at=200),
        _row("C", version=2, supersedes_id="A", created_at=210),
        _row("D", version=2, supersedes_id="A", created_at=220),
    ]
    collection = _make_stateful_collection(rows)
    session = _mock_session_for_lock()

    # Make ONE upsert fail; subsequent upserts succeed.
    real_upsert_side_effect = collection.upsert.side_effect
    call_count = {"n": 0}

    def _failing_then_ok(rows_in):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("milvus transient")
        return real_upsert_side_effect(rows_in)

    collection.upsert.side_effect = _failing_then_ok

    with caplog.at_level(logging.ERROR, logger="scaffold.flatten_branched_chains"), \
         patch("app.modules.rag_pipeline.async_session", return_value=session):
        from scripts.flatten_branched_chains import flatten_domain
        br, rw = await flatten_domain(collection, "eng", apply_mode=True)

    # First rewrite failed, second succeeded → 1 successful rewrite counted.
    assert br == 1
    assert rw == 1, f"expected 1 successful rewrite; got {rw}"
    upsert_err = [
        r for r in caplog.records
        if "flatten_upsert_failed" in r.getMessage()
    ]
    assert upsert_err, f"expected flatten_upsert_failed log; got: {caplog.records}"
