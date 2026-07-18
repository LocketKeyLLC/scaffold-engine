"""Tests for app/modules/_rag_entry.py — the IngestEntry Pydantic model
that centralizes the TOON↔Milvus dual-name conversion (audit item 6).

The model must preserve legacy ``or``-chain semantics: when an input
dict carries BOTH alias names for a field, the first non-empty value
wins (not the first-present one). This protects against the case where
a TOON producer emits an empty `content` alongside a populated
`canonical_text`.
"""
from __future__ import annotations

from app.modules._rag_entry import IngestEntry


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_from_input_handles_toon_short_names():
    e = IngestEntry.from_input({
        "topic": "Partition keys",
        "content": "Milvus uses partition keys for tenant isolation.",
        "tags": "rag,milvus,partitions",
        "source": "https://example.com/post",
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    })
    assert e.title == "Partition keys"
    assert e.content == "Milvus uses partition keys for tenant isolation."
    assert e.domain_tags == ["rag", "milvus", "partitions"]
    assert e.source_url == "https://example.com/post"
    assert e.source_type == "tech_docs"
    assert e.confidence == 0.85


def test_from_input_handles_milvus_long_names():
    e = IngestEntry.from_input({
        "title": "Reranker",
        "canonical_text": "CrossEncoder reranks fused results.",
        "domain_tags": ["rag", "reranker"],
        "source_url": "https://example.com/r",
        "source_type": "tech_docs",
        "confidence_score": 0.90,
    })
    assert e.title == "Reranker"
    assert e.content == "CrossEncoder reranks fused results."
    assert e.domain_tags == ["rag", "reranker"]


def test_first_non_empty_wins_for_content():
    """Empty ``content`` falls through to ``canonical_text`` — matches
    the legacy `or`-chain behavior, NOT Pydantic's stock alias resolution."""
    e = IngestEntry.from_input({
        "content": "",
        "canonical_text": "real text",
    })
    assert e.content == "real text"


def test_first_non_empty_wins_for_title_and_source():
    e = IngestEntry.from_input({
        "title": "",
        "topic": "fallback title",
        "source": "",
        "source_url": "https://example.com/u",
    })
    assert e.title == "fallback title"
    assert e.source_url == "https://example.com/u"


def test_tags_parsed_from_comma_string():
    e = IngestEntry.from_input({"tags": "alpha, beta , gamma "})
    assert e.domain_tags == ["alpha", "beta", "gamma"]


def test_tags_capped_at_20():
    e = IngestEntry.from_input({
        "tags": ",".join(f"t{i}" for i in range(50)),
    })
    assert len(e.domain_tags) == 20
    assert e.domain_tags[0] == "t0"
    assert e.domain_tags[-1] == "t19"


def test_tags_preserves_list_input():
    e = IngestEntry.from_input({"tags": ["x", "y", "z"]})
    assert e.domain_tags == ["x", "y", "z"]


def test_tags_falls_back_to_domain_tags_when_tags_none():
    e = IngestEntry.from_input({"tags": None, "domain_tags": ["dt1", "dt2"]})
    assert e.domain_tags == ["dt1", "dt2"]


def test_title_strip_and_unknown_default():
    e = IngestEntry.from_input({"title": "  spaces  "})
    assert e.title == "spaces"
    e2 = IngestEntry.from_input({"title": "   "})
    assert e2.title == "unknown"


def test_defaults_applied_for_missing_fields():
    e = IngestEntry.from_input({})
    assert e.title == "unknown"
    assert e.content == ""
    assert e.domain_tags == []
    assert e.source_url == "scaffold-engine"
    assert e.source_type == "ai_generated"
    assert e.confidence == 0.60


def test_non_dict_input_yields_defaults():
    """Defensive: non-dict inputs (None, list, …) shouldn't crash."""
    assert IngestEntry.from_input(None).title == "unknown"
    assert IngestEntry.from_input("not a dict").content == ""
    assert IngestEntry.from_input(42).source_url == "scaffold-engine"


def test_invalid_confidence_falls_back_to_default():
    e = IngestEntry.from_input({"confidence_score": "not a number"})
    assert e.confidence == 0.60


def test_extra_unknown_fields_ignored():
    """The model is not strict — unknown keys silently dropped so
    callers can pass richer dicts without breakage."""
    e = IngestEntry.from_input({
        "title": "OK",
        "garbage_field": "ignored",
        "another": [1, 2, 3],
    })
    assert e.title == "OK"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_milvus_uses_long_names():
    e = IngestEntry(
        title="T", content="C", domain_tags=["a"],
        source_url="S", source_type="news", confidence=0.5,
    )
    # §17.606 — schema field is "title", not "topic".
    assert e.to_milvus() == {
        "canonical_text": "C",
        "title": "T",
        "domain_tags": ["a"],
        "source_url": "S",
        "source_type": "news",
        "confidence_score": 0.5,
    }


def test_to_canonical_dict_matches_legacy_normalize_entry_shape():
    """The legacy _normalize_entry returned a dict with these specific
    keys. to_canonical_dict() must match exactly so callers can swap."""
    e = IngestEntry(
        title="T", content="C", domain_tags=["a", "b"],
        source_url="S", source_type="news", confidence=0.7,
    )
    d = e.to_canonical_dict()
    assert set(d.keys()) == {
        "content", "title", "domain_tags", "source_url",
        "source_type", "confidence",
    }
    assert d["content"] == "C"
    assert d["confidence"] == 0.7


def test_round_trip_preserves_data():
    """from_input → to_milvus → from_milvus = identity (modulo defaults)."""
    original = {
        "title": "Partition keys",
        "canonical_text": "Some text body.",
        "domain_tags": ["rag", "milvus"],
        "source_url": "https://example.com/x",
        "source_type": "tech_docs",
        "confidence_score": 0.88,
    }
    a = IngestEntry.from_input(original)
    b = IngestEntry.from_milvus(a.to_milvus())
    assert a == b


def test_from_milvus_alias_for_from_input():
    """from_milvus is just a documentation-named wrapper."""
    row = {"topic": "T", "canonical_text": "C", "domain_tags": ["x"]}
    assert IngestEntry.from_milvus(row).title == "T"
    assert IngestEntry.from_milvus(row).content == "C"


# ---------------------------------------------------------------------------
# §17.606 — to_milvus() emits the schema field "title" (not "topic")
# ---------------------------------------------------------------------------
def test_to_milvus_emits_schema_title_key():
    """The toon_v2 schema field is 'title'; 'topic' is not a schema field, so
    a direct upsert with enable_dynamic_field=False would reject the row."""
    e = IngestEntry.from_input({"title": "Reranker", "content": "c"})
    row = e.to_milvus()
    assert row["title"] == "Reranker"
    assert "topic" not in row
