"""Shared Milvus collection accessor with auto-creation for toon_v2."""
from __future__ import annotations

import logging
import threading
import time

from pymilvus import (
    Collection, MilvusClient, DataType, Function, FunctionType,
    connections, utility,
)

from app.config import settings

logger = logging.getLogger("scaffold.milvus_utils")

COLLECTION_NAME = "toon_v2"
DIM = 512
PRIMARY_FIELD = "entry_id"
VECTOR_FIELD = "dense_vector"
# §17.431 — BM25 sparse field + Function (added only when bm25 is enabled).
BM25_SPARSE_FIELD = "sparse_bm25"
BM25_FUNCTION_NAME = "bm25_canonical_text"


# ---------------------------------------------------------------------------
# get_collection() cache (#40, #41)
# Liveness/has_collection/load RPCs are redundant after first success.
# Cache handle for CACHE_TTL seconds, invalidate on any error.
# Double-checked locking prevents thundering herd on cold load.
# ---------------------------------------------------------------------------
_CACHE_TTL_S = 30.0
_cached_collection: "Collection | None" = None
_cached_at: float = 0.0
_cache_lock = threading.Lock()


def escape_milvus_literal(s: str) -> str:
    """Escape ``\\`` and ``"`` for safe interpolation into a Milvus expr literal.

    §17.409 (arch-review R3) — shared home for the escape that
    ``rag_pipeline._escape_literal`` also performs, so layers that can't import
    the `modules` package (e.g. ``app.utils.staleness``) still build expr
    literals through the documented contract instead of raw f-string interpolation.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _invalidate_cache() -> None:
    """Drop cached Collection so next get_collection() re-verifies.

    Caller MUST NOT hold _cache_lock (threading.Lock is non-reentrant).
    """
    global _cached_collection, _cached_at
    with _cache_lock:
        _cached_collection = None
        _cached_at = 0.0


def _bm25_default(bm25: bool | None) -> bool:
    """Resolve the bm25 flag: explicit arg wins, else settings.rag_bm25_enabled."""
    return settings.rag_bm25_enabled if bm25 is None else bm25


def build_toon_v2_schema(*, bm25: bool | None = None):
    """Build the canonical toon_v2 TOON schema (16 fields, 512d vector).

    §17.431 — when ``bm25`` (or settings.rag_bm25_enabled) is True, also adds
    an analyzer to ``canonical_text``, a ``sparse_bm25`` SPARSE_FLOAT_VECTOR
    field, and a BM25 ``Function`` that auto-generates the sparse vector from
    ``canonical_text`` on insert (17 fields + 1 function). Default (flag off)
    is the unchanged 16-field schema.
    """
    use_bm25 = _bm25_default(bm25)
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("entry_id", DataType.VARCHAR, max_length=128, is_primary=True)
    schema.add_field("title", DataType.VARCHAR, max_length=512)
    # §17.431 — BM25 requires an analyzer on the text input field.
    schema.add_field(
        "canonical_text", DataType.VARCHAR, max_length=65535,
        **({"enable_analyzer": True} if use_bm25 else {}),
    )
    schema.add_field("domain", DataType.VARCHAR, max_length=128, is_partition_key=True)
    schema.add_field("domain_tags", DataType.ARRAY, element_type=DataType.VARCHAR,
                     max_capacity=20, max_length=64)
    schema.add_field("confidence_score", DataType.FLOAT)
    schema.add_field("source_type", DataType.VARCHAR, max_length=64)
    schema.add_field("source_url", DataType.VARCHAR, max_length=2048)
    schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
    schema.add_field("model_id", DataType.VARCHAR, max_length=128)
    schema.add_field("version", DataType.INT32)
    schema.add_field("supersedes_id", DataType.VARCHAR, max_length=128)
    schema.add_field("created_at", DataType.INT64)
    schema.add_field("updated_at", DataType.INT64)
    schema.add_field("expires_at", DataType.INT64)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=DIM)
    if use_bm25:
        schema.add_field(BM25_SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(
            name=BM25_FUNCTION_NAME,
            input_field_names=["canonical_text"],
            output_field_names=[BM25_SPARSE_FIELD],
            function_type=FunctionType.BM25,
        ))
    return schema


def build_toon_v2_index_params(client: MilvusClient, *, bm25: bool | None = None):
    """Build the canonical toon_v2 index params (HNSW_SQ8 + scalar indexes).

    §17.431 — adds a SPARSE_INVERTED_INDEX (metric BM25) on ``sparse_bm25``
    when bm25 is enabled.
    """
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_type="HNSW_SQ",
        metric_type="COSINE",
        params={
            "M": 16,
            "efConstruction": 256,
            "sq_type": "SQ8",
            "refine": True,
            "refine_type": "BF16",
        },
    )
    index_params.add_index(field_name="content_hash", index_type="INVERTED")
    index_params.add_index(field_name="domain_tags", index_type="INVERTED")
    index_params.add_index(field_name="source_type", index_type="BITMAP")
    index_params.add_index(field_name="confidence_score", index_type="INVERTED")
    index_params.add_index(field_name="created_at", index_type="STL_SORT")
    index_params.add_index(field_name="version", index_type="BITMAP")
    if _bm25_default(bm25):
        index_params.add_index(
            field_name=BM25_SPARSE_FIELD,
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
    return index_params


def collection_has_bm25(col: "Collection") -> bool:
    """§17.431 — True if the live collection carries the BM25 sparse field.

    Used by the keyword-search dispatcher to fall back to the LIKE path when
    the flag is on but the collection hasn't been migrated yet (graceful, so
    flipping rag_bm25_enabled before running the migration can't break search).
    """
    try:
        return any(f.name == BM25_SPARSE_FIELD for f in col.schema.fields)
    except Exception:
        return False


def _auto_create_collection() -> None:
    """Create toon_v2 with the canonical TOON schema. Always closes MilvusClient."""
    client = MilvusClient(settings.milvus_uri)
    try:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            schema=build_toon_v2_schema(),
            index_params=build_toon_v2_index_params(client),
            num_partitions=settings.milvus_num_partitions,
            properties={"partitionkey.isolation": "true"},
        )
        logger.info(
            "Auto-created collection '%s' with HNSW_SQ8 + partition key isolation",
            COLLECTION_NAME,
        )
    finally:
        try:
            client.close()
        except Exception as e:
            logger.debug("MilvusClient.close() failed: %s", e)


def _assert_schema_invariants(col: Collection) -> None:
    """Verify dim and primary key invariants on cold load. Raises on mismatch."""
    schema = col.schema
    primaries = [f.name for f in schema.fields if getattr(f, "is_primary", False)]
    if primaries != [PRIMARY_FIELD]:
        raise RuntimeError(
            f"schema invariant violated: expected primary={PRIMARY_FIELD!r}, got {primaries}"
        )
    vec = next((f for f in schema.fields if f.name == VECTOR_FIELD), None)
    if vec is None:
        raise RuntimeError(
            f"schema invariant violated: missing field {VECTOR_FIELD!r}"
        )
    dim = (vec.params or {}).get("dim")
    if dim != DIM:
        raise RuntimeError(
            f"schema invariant violated: expected {VECTOR_FIELD} dim={DIM}, got {dim}"
        )


def get_collection(*, raise_on_missing: bool = False) -> Collection | None:
    """Get the toon_v2 Milvus collection, auto-creating if missing.

    Uses double-checked locking so only one thread performs the cold load
    while others wait on the lock and then pick up the cached handle.

    ⚠️  With ``raise_on_missing=False`` (default), callers MUST check for
    ``None`` before use.
    """
    global _cached_collection, _cached_at

    # Fast path — lock-free cache read
    cached = _cached_collection
    cached_at = _cached_at
    if cached is not None and (time.monotonic() - cached_at) < _CACHE_TTL_S:
        return cached

    # Slow path — serialize to prevent thundering herd on cold load
    with _cache_lock:
        # Double-check: another thread may have populated the cache while we waited
        if _cached_collection is not None and (time.monotonic() - _cached_at) < _CACHE_TTL_S:
            return _cached_collection

        try:
            # Ensure connection
            try:
                utility.list_collections()
            except Exception:
                connections.connect(alias="default", uri=settings.milvus_uri)

            # Auto-create if missing
            if not utility.has_collection(COLLECTION_NAME):
                logger.warning(
                    "Collection '%s' not found — attempting auto-create",
                    COLLECTION_NAME,
                )
                _auto_create_collection()

            # Final presence check
            if not utility.has_collection(COLLECTION_NAME):
                msg = f"Collection '{COLLECTION_NAME}' not found and auto-creation failed"
                _cached_collection = None
                _cached_at = 0.0
                if raise_on_missing:
                    raise RuntimeError(msg)
                logger.error(msg)
                return None

            col = Collection(COLLECTION_NAME)

            # Cold-load schema invariant check (dim==512, primary=='entry_id')
            _assert_schema_invariants(col)

            col.load()

            _cached_collection = col
            _cached_at = time.monotonic()
            return col
        except RuntimeError:
            _cached_collection = None
            _cached_at = 0.0
            raise
        except Exception as e:
            _cached_collection = None
            _cached_at = 0.0
            msg = f"Failed to get Milvus collection: {e}"
            if raise_on_missing:
                raise RuntimeError(msg) from e
            logger.error(msg)
            return None
