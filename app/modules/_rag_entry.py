"""Canonical ingest-entry shape for the RAG pipeline (audit item 6).

The RAG ingest path historically accepted entries written with EITHER of
two key names for several fields — the TOON LLM-output format uses short
keys (``topic``, ``content``, ``tags``, ``source``) and Milvus storage
uses long keys (``canonical_text``, ``domain_tags``, ``source_url``).
The dual-accept is a deliberate translation layer between those two
formats; the audit's "drop fallbacks" prescription would force every
caller to migrate together.

This module replaces the scattered ``entry.get("X") or entry.get("Y")``
chains in ``rag_pipeline.py`` with a single typed model. The model
preserves the legacy first-non-empty-alias-wins behavior via an explicit
``from_input(...)`` constructor — Pydantic's stock ``AliasChoices``
picks the first PRESENT alias regardless of value, which would silently
prefer ``content=""`` over a populated ``canonical_text``. The explicit
constructor avoids that footgun.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IngestEntry(BaseModel):
    """Canonical, typed shape for a single RAG ingest entry.

    Construct with:

    - ``IngestEntry.from_input(entry_dict)`` — accepts either TOON-shaped
      or Milvus-shaped input dicts; resolves dual aliases with first-
      non-empty-wins semantics (matches the legacy `or`-chain behavior).
    - Direct Pydantic construction with canonical names for callers that
      already have a clean shape: ``IngestEntry(title=..., content=..., …)``.

    Round-trip to Milvus storage shape via ``.to_milvus()``.
    """
    model_config = ConfigDict(extra="ignore")

    title: str = "unknown"
    content: str = ""
    domain_tags: list[str] = Field(default_factory=list)
    source_url: str = "scaffold-engine"
    source_type: str = "ai_generated"
    confidence: float = 0.60

    @classmethod
    def from_input(cls, entry: Any) -> "IngestEntry":
        """Build from an arbitrary input dict.

        Resolves dual-name aliases with first-non-empty-wins semantics:

        - ``content`` ← ``content`` else ``canonical_text``
        - ``title``   ← ``title``   else ``topic``
        - ``domain_tags`` ← ``tags`` else ``domain_tags`` (str or list)
        - ``source_url`` ← ``source`` else ``source_url``
        - ``source_type`` ← ``source_type`` (no alias)
        - ``confidence`` ← ``confidence_score`` (legacy key name)

        Accepts non-dict input gracefully (returns model with defaults).
        """
        if not isinstance(entry, dict):
            return cls()

        # First-non-empty-wins (preserves legacy `or`-chain behavior).
        content = entry.get("content") or entry.get("canonical_text") or ""
        title_raw = entry.get("title") or entry.get("topic") or "unknown"

        tags_raw = (
            entry.get("tags")
            if entry.get("tags") is not None
            else entry.get("domain_tags", "")
        )
        if isinstance(tags_raw, str):
            domain_tags = [t.strip() for t in tags_raw.split(",") if t.strip()][:20]
        elif isinstance(tags_raw, list):
            domain_tags = list(tags_raw)[:20]
        else:
            domain_tags = []

        source_url = entry.get("source") or entry.get("source_url") or "scaffold-engine"

        try:
            confidence = float(entry.get("confidence_score", 0.60))
        except (TypeError, ValueError):
            confidence = 0.60

        return cls(
            title=str(title_raw).strip() or "unknown",
            content=content,
            domain_tags=domain_tags,
            source_url=source_url,
            source_type=entry.get("source_type") or "ai_generated",
            confidence=confidence,
        )

    @classmethod
    def from_milvus(cls, row: Any) -> "IngestEntry":
        """Build from a Milvus row (always uses long-name keys).

        Thin wrapper around ``from_input`` since the long-name path
        through that constructor handles Milvus rows correctly. Kept as
        a named entry point so call sites that read from Milvus document
        their intent.
        """
        return cls.from_input(row)

    def to_milvus(self) -> dict[str, Any]:
        """Serialize to the Milvus-storage shape (long-name keys)."""
        return {
            "canonical_text": self.content,
            # §17.606 — the toon_v2 schema field is "title" (milvus_utils.py);
            # the old "topic" key is not a schema field, and with
            # enable_dynamic_field=False a real upsert would reject the row
            # (and drop the title). Latent today — only the round-trip test
            # consumed this — but wrong for any direct-upsert caller.
            "title": self.title,
            "domain_tags": self.domain_tags,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "confidence_score": self.confidence,
        }

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the dict shape that legacy ``_normalize_entry`` produced.

        Lets ``rag_pipeline._normalize_entry`` become a one-liner without
        breaking any of its many consumers that index the result by key.
        """
        return {
            "content": self.content,
            "title": self.title,
            "domain_tags": self.domain_tags,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "confidence": self.confidence,
        }
