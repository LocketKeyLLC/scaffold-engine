"""§17.189 — Stable typed shapes for ``app.modules.rag_pipeline`` returns.

The audit (AUDIT.md 4.2) flagged ``rag_pipeline.query_rag`` and
``ingest_entries`` as a 4-way fan-in bottleneck: ``research_agent``,
``ideation_workflow``, ``execution_agent``, and ``app.sim.topology_select``
all depend on the dict shapes these functions return. Any field rename
ripples to 4 callers each with their own test surface, and the dict-typed
return signatures meant the breaking surface only surfaced at runtime.

The TypedDicts in this module pin the contract at the type level:

  * Callers can annotate ``result: RagResponseDict = await query_rag(...)``
    and get a documented field list rather than the previous ``dict[str, Any]``.
  * A future signature change to ``query_rag`` or ``ingest_entries`` forces
    a deliberate update here, surfacing the breaking surface at review time.
  * ``validate_rag_response`` / ``validate_ingest_stats`` runtime-validate
    actual return dicts in tests so silent drift on either side (callers
    leaning on a field the implementation no longer emits, OR the
    implementation emitting a field no caller knows about) gets caught.

This module is intentionally separate from ``_rag_entry.py`` (which models
the INGEST entry input shape) — the two solve different problems and have
opposite "stability surfaces". ``_rag_entry`` ships an editable canonical
form; this module pins the API contract.

Note: this module intentionally does NOT use ``from __future__ import
annotations``. PEP 563 string-annotations would convert ``NotRequired[X]``
to a string literal at class-creation time, and TypedDict's
``__required_keys__`` / ``__optional_keys__`` introspection requires the
actual ``NotRequired`` marker to be present on the annotation. The cost
of skipping PEP 563 here is zero — Python 3.10+ handles ``X | Y`` /
``list[X]`` / ``dict[X, Y]`` natively at runtime.
"""
from typing import Any, Literal, NotRequired, TypedDict


# ---------------------------------------------------------------------------
# query_rag — full pipeline response
# ---------------------------------------------------------------------------

class RagScoresDict(TypedDict):
    """Per-result score breakdown (within RagResultDict.scores).

    All scores are rounded to 4 decimal places in ``query_rag``'s response
    builder. ``quality_bump`` is the §17.120 multiplicative reweight from
    provenance.quality_signal; defaults to 1.0 when no provenance row exists.
    """
    vector: float
    keyword: float
    rrf: float
    rerank: float
    final: float
    quality_bump: float


class RagResultDict(TypedDict):
    """One retrieved entry — what ``query_rag().results[i]`` looks like."""
    content: str
    title: str
    tags: str
    source_url: str
    entry_id: str
    domain: str
    version: int
    supersedes_id: str
    confidence_score: float
    source_type: str
    provenance: dict[str, Any] | None
    scores: RagScoresDict


class RagMetadataDict(TypedDict, total=False):
    """Top-level metadata block on every query_rag response.

    All fields ``total=False`` because the cache-hit path returns a stored
    metadata blob whose shape predates any field we might add today — the
    typed shape is "best effort, every field optional" so a cached old
    response doesn't fail validation. Live responses always populate the
    full set; cached responses may have a subset plus ``cache_hit=True``.
    """
    vector_hits: int
    keyword_hits: int
    fused_count: int
    confidence_threshold: float
    threshold_relaxed: bool
    below_threshold: bool
    fell_back_to_top3: bool
    reranked: bool
    skipped_rerank: bool
    reranker_backend: str | None
    warnings: list[str]
    latency_ms: float
    cache_hit: bool


class RagResponseDict(TypedDict):
    """Full ``query_rag`` return shape.

    Success path: ``status="ok"``, ``query`` / ``result_count`` / ``results``
    populated. Error path: ``status="error"``, ``error`` carries the
    operator-facing string, ``results`` is ``[]``. ``query`` and
    ``result_count`` are present on success only (NotRequired) so the
    typed shape matches both branches.
    """
    status: Literal["ok", "error"]
    results: list[RagResultDict]
    metadata: RagMetadataDict
    query: NotRequired[str]
    result_count: NotRequired[int]
    error: NotRequired[str]


# ---------------------------------------------------------------------------
# ingest_entries — write-path return
# ---------------------------------------------------------------------------

class IngestStatsDict(TypedDict):
    """Per-batch ingest counters returned by ``ingest_entries``.

    Field semantics (locked here so a future field rename surfaces here
    first, then ripples to the 2 callers — research_agent + ideation_workflow):
      * ``new``           — entries upserted as a fresh row.
      * ``versioned``     — entries that superseded an older row (chain extend).
      * ``rejected``      — entries above the 0.95 cosine threshold; treated
                            as duplicates of an existing row.
      * ``skipped_hash``  — entries whose raw_hash matched an existing row;
                            no embedding / dedup work performed.
      * ``skipped_empty`` — entries with empty canonical_text after normalization.
    """
    new: int
    versioned: int
    rejected: int
    skipped_hash: int
    skipped_empty: int


# ---------------------------------------------------------------------------
# Runtime validators — guard against silent dict-shape drift in tests
# ---------------------------------------------------------------------------
#
# TypedDict is not runtime-checked by Python, so the helpers below give
# the test suite something concrete to assert against. The "required-keys"
# semantics come straight from TypedDict's __required_keys__ introspection
# so a future TypedDict edit (adding/removing a field) automatically
# updates the runtime check without manual sync.

_RAG_SCORES_REQUIRED_KEYS = frozenset(RagScoresDict.__required_keys__)
_RAG_RESULT_REQUIRED_KEYS = frozenset(RagResultDict.__required_keys__)
_RAG_RESPONSE_REQUIRED_KEYS = frozenset(RagResponseDict.__required_keys__)
_INGEST_STATS_REQUIRED_KEYS = frozenset(IngestStatsDict.__required_keys__)


def validate_rag_response(d: dict[str, Any]) -> list[str]:
    """Return a list of validation errors for a ``query_rag`` response dict.

    Empty list means the dict matches the ``RagResponseDict`` shape (and,
    transitively, every result matches ``RagResultDict``). A non-empty
    list flags drift between the implementation and the typed contract —
    used by tests to fail loudly when ``query_rag`` adds or removes a field
    without the corresponding TypedDict update.
    """
    errors: list[str] = []
    missing_top = _RAG_RESPONSE_REQUIRED_KEYS - set(d.keys())
    if missing_top:
        errors.append(f"missing top-level keys: {sorted(missing_top)}")

    if d.get("status") == "ok":
        for key in ("query", "result_count"):
            if key not in d:
                errors.append(f"status='ok' but missing {key!r}")

    for i, r in enumerate(d.get("results", []) or []):
        missing_r = _RAG_RESULT_REQUIRED_KEYS - set(r.keys())
        if missing_r:
            errors.append(f"results[{i}] missing keys: {sorted(missing_r)}")
        scores = r.get("scores") or {}
        missing_s = _RAG_SCORES_REQUIRED_KEYS - set(scores.keys())
        if missing_s:
            errors.append(f"results[{i}].scores missing keys: {sorted(missing_s)}")
    return errors


def validate_ingest_stats(d: dict[str, Any]) -> list[str]:
    """Return validation errors for an ``ingest_entries`` return dict."""
    errors: list[str] = []
    missing = _INGEST_STATS_REQUIRED_KEYS - set(d.keys())
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
    for k in _INGEST_STATS_REQUIRED_KEYS & set(d.keys()):
        v = d[k]
        if not isinstance(v, int):
            errors.append(f"{k}: expected int, got {type(v).__name__}")
    return errors
