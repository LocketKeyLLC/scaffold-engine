"""
Unit tests for ``scripts/seed_eng_topologies.py``.

The script's real-world value is being run once against the corpus,
but the contract — entries are well-formed, the CLI doesn't lie
about what it'll do, ``--dry-run`` is genuinely a no-op — is worth
locking down so future content edits can't break the script silently.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

from scripts import seed_eng_topologies as seed


# ---------------------------------------------------------------------------
# Entry-shape parity
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_seeds_have_required_fields():
    """Every SEEDS entry must carry the four fields the ingest path
    expects. A missing field would silently default during ingest and
    distort the corpus."""
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
def test_seeds_cover_lpf_hpf_bpf():
    """The §17.146 use case needs at least one entry per filter family
    so retrieval doesn't bias toward whichever family we over-curated."""
    all_tags = {t for s in seed.SEEDS for t in s["tags"]}
    assert "lowpass" in all_tags
    assert "highpass" in all_tags
    assert "bandpass" in all_tags


@pytest.mark.smoke
def test_seed_titles_are_unique():
    """Duplicate titles confuse downstream attribution and waste
    embedding budget on near-duplicates the dedup pipeline would
    have to reject anyway."""
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
        # Keys ingest_entries / IngestEntry.from_input expects:
        assert "title" in e
        assert "content" in e
        assert "domain_tags" in e
        assert "source_url" in e
        assert e["source_type"] == "curated"
        assert e["confidence"] == 0.90
        # tags should land in domain_tags as a list.
        assert isinstance(e["domain_tags"], list)


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_main_dry_run_does_not_call_ingest(capsys):
    with patch("scripts.seed_eng_topologies.ingest_curated") as ingest_mock, \
         patch("scripts.seed_eng_topologies.ingest_urls") as url_mock:
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
    # First URL should be in the listing.
    assert seed.URLS_FOR_RESEARCH[0] in out


@pytest.mark.smoke
def test_main_live_calls_ingest_curated_once():
    with patch("scripts.seed_eng_topologies.ingest_curated", new_callable=AsyncMock) as ingest_mock:
        ingest_mock.return_value = {
            "new": len(seed.SEEDS), "versioned": 0,
            "rejected": 0, "skipped_hash": 0, "skipped_empty": 0,
        }
        rc = seed.main([])
    assert rc == 0
    ingest_mock.assert_awaited_once()


@pytest.mark.smoke
def test_main_with_urls_calls_url_ingest():
    with patch("scripts.seed_eng_topologies.ingest_curated", new_callable=AsyncMock) as curated, \
         patch("scripts.seed_eng_topologies.ingest_urls", new_callable=AsyncMock) as url:
        curated.return_value = {"new": 0, "versioned": 0, "rejected": 0,
                                "skipped_hash": len(seed.SEEDS), "skipped_empty": 0}
        url.return_value = {u: "ok" for u in seed.URLS_FOR_RESEARCH}
        rc = seed.main(["--with-urls"])
    assert rc == 0
    curated.assert_awaited_once()
    url.assert_awaited_once()
    # Verify URL list passed verbatim — not a copy that could drift.
    url.assert_awaited_with(seed.URLS_FOR_RESEARCH)


@pytest.mark.smoke
def test_main_ingest_failure_exit_2():
    with patch("scripts.seed_eng_topologies.ingest_curated", new_callable=AsyncMock) as ingest_mock:
        ingest_mock.side_effect = RuntimeError("milvus unavailable")
        rc = seed.main([])
    assert rc == 2


@pytest.mark.smoke
def test_main_bad_flag_exit_nonzero():
    """argparse exits with code 2 on bad flags; the wrapper passes it
    through unchanged."""
    rc = seed.main(["--bogus-flag"])
    assert rc != 0


# ---------------------------------------------------------------------------
# Idempotency invariant
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_re_run_yields_same_entries():
    """Running build_entries twice must produce identical output —
    no global state, no time-of-day stamps. The §9.x dedup pipeline
    can only treat re-runs as idempotent if the content hashes
    don't drift across invocations."""
    a = seed.build_entries()
    b = seed.build_entries()
    # Compare as JSON to defeat any dict ordering issues.
    import json
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
