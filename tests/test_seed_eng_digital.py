"""
Unit tests for ``scripts/seed_eng_digital.py`` (§17.154).

Mirror of ``test_seed_eng_topologies.py`` (§17.149) — the script's
contract is "entries well-formed, CLI doesn't lie, --dry-run is a
no-op." Future content edits can't silently break those without a
failing test.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scripts import seed_eng_digital as seed


# ---------------------------------------------------------------------------
# Entry-shape parity
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_seeds_have_required_fields():
    assert seed.SEEDS, "SEEDS must not be empty"
    for s in seed.SEEDS:
        assert "title" in s and s["title"], f"missing title: {s}"
        assert "content" in s and len(s["content"]) >= 200, (
            f"content too short for {s['title']!r}: "
            f"{len(s.get('content', ''))} chars (min 200)"
        )
        assert "source_url" in s and s["source_url"].startswith(("http://", "https://")), (
            f"missing/invalid source_url for {s['title']!r}"
        )
        assert "tags" in s and len(s["tags"]) >= 2, (
            f"too few tags for {s['title']!r}"
        )


@pytest.mark.smoke
def test_seeds_cover_digital_families():
    """§17.154 — every entry should be discoverable as 'digital'
    content via tag filter; the four major families (counter, fifo /
    storage, fsm, arithmetic) should all be represented so retrieval
    for any of them returns something."""
    all_tags = {t for s in seed.SEEDS for t in s["tags"]}
    assert "digital_logic" in all_tags
    assert "counter" in all_tags
    # Storage covers FIFO, RAM, shift register; assert at least one.
    assert {"fifo", "ram", "shift_register"} & all_tags
    assert "fsm" in all_tags
    # Arithmetic — adder OR multiplier OR comparator.
    assert {"adder", "multiplier", "comparator"} & all_tags


@pytest.mark.smoke
def test_every_entry_tagged_digital():
    """The §17.146 retrieval query carries design.kind in the search
    text. Every seed entry needs the ``digital`` and ``digital_logic``
    tags so the LLM's digital-side queries find them."""
    for s in seed.SEEDS:
        assert "digital" in s["tags"] or "digital_logic" in s["tags"], (
            f"{s['title']!r} not tagged digital: tags={s['tags']!r}"
        )


@pytest.mark.smoke
def test_seed_titles_are_unique():
    titles = [s["title"] for s in seed.SEEDS]
    assert len(set(titles)) == len(titles), (
        f"duplicate titles: "
        f"{[t for t in titles if titles.count(t) > 1]}"
    )


# ---------------------------------------------------------------------------
# build_entries() output shape
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_build_entries_translates_to_ingest_shape():
    entries = seed.build_entries()
    assert len(entries) == len(seed.SEEDS)
    for e in entries:
        assert "title" in e
        assert "content" in e
        assert "domain_tags" in e
        assert "source_url" in e
        assert e["source_type"] == "curated"
        assert e["confidence"] == 0.90
        assert isinstance(e["domain_tags"], list)


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_main_dry_run_does_not_call_ingest(capsys):
    with patch("scripts.seed_eng_digital.ingest_curated") as ingest_mock, \
         patch("scripts.seed_eng_digital.ingest_urls") as url_mock:
        ingest_mock.return_value = AsyncMock()
        url_mock.return_value = AsyncMock()
        rc = seed.main(["--dry-run"])
    assert rc == 0
    ingest_mock.assert_not_called()
    url_mock.assert_not_called()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert f"{len(seed.SEEDS)} curated entries" in out


@pytest.mark.smoke
def test_main_dry_run_with_urls_lists_urls(capsys):
    rc = seed.main(["--dry-run", "--with-urls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "via run_research" in out
    assert seed.URLS_FOR_RESEARCH[0] in out


@pytest.mark.smoke
def test_main_live_calls_ingest_curated_once():
    with patch("scripts.seed_eng_digital.ingest_curated", new_callable=AsyncMock) as ingest_mock:
        ingest_mock.return_value = {
            "new": len(seed.SEEDS), "versioned": 0,
            "rejected": 0, "skipped_hash": 0, "skipped_empty": 0,
        }
        rc = seed.main([])
    assert rc == 0
    ingest_mock.assert_awaited_once()


@pytest.mark.smoke
def test_main_with_urls_calls_url_ingest():
    with patch("scripts.seed_eng_digital.ingest_curated", new_callable=AsyncMock) as curated, \
         patch("scripts.seed_eng_digital.ingest_urls", new_callable=AsyncMock) as url:
        curated.return_value = {"new": 0, "versioned": 0, "rejected": 0,
                                "skipped_hash": len(seed.SEEDS), "skipped_empty": 0}
        url.return_value = {u: "ok" for u in seed.URLS_FOR_RESEARCH}
        rc = seed.main(["--with-urls"])
    assert rc == 0
    curated.assert_awaited_once()
    url.assert_awaited_once()
    url.assert_awaited_with(seed.URLS_FOR_RESEARCH)


@pytest.mark.smoke
def test_main_ingest_failure_exit_2():
    with patch("scripts.seed_eng_digital.ingest_curated", new_callable=AsyncMock) as ingest_mock:
        ingest_mock.side_effect = RuntimeError("milvus unavailable")
        rc = seed.main([])
    assert rc == 2


@pytest.mark.smoke
def test_main_bad_flag_exit_nonzero():
    rc = seed.main(["--bogus-flag"])
    assert rc != 0


# ---------------------------------------------------------------------------
# Idempotency invariant
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_re_run_yields_same_entries():
    """build_entries must produce byte-identical output across calls so
    content hashes don't drift between runs — required for §9.x dedup
    to treat re-invocations as idempotent."""
    import json
    a = seed.build_entries()
    b = seed.build_entries()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
