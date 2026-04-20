"""Shared Milvus collection accessor with auto-creation for toon_v2."""
from __future__ import annotations

import logging

from pymilvus import Collection, MilvusClient, DataType, connections, utility

from app.config import settings

logger = logging.getLogger("scaffold.milvus_utils")

COLLECTION_NAME = "toon_v2"
DIM = 512


def _auto_create_collection() -> None:
    """Create toon_v2 with the canonical TOON schema.

    Replicates scripts/create_toon_v2.py exactly.
    """
    client = MilvusClient(settings.milvus_uri)

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("entry_id", DataType.VARCHAR, max_length=128, is_primary=True)
    schema.add_field("title", DataType.VARCHAR, max_length=512)
    schema.add_field("canonical_text", DataType.VARCHAR, max_length=65535)
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

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
        num_partitions=settings.milvus_num_partitions,
        properties={"partitionkey.isolation": "true"},
    )
    logger.info("Auto-created collection '%s' with HNSW_SQ8 + partition key isolation",
                COLLECTION_NAME)


def get_collection(*, raise_on_missing: bool = False) -> Collection | None:
    """Get the toon_v2 Milvus collection, auto-creating if missing.

    Args:
        raise_on_missing: If True and auto-creation fails, raise RuntimeError.
                          If False (default), return None on failure.

    Returns:
        Loaded Collection, or None if unavailable and raise_on_missing is False.

    ⚠️  Pitfall: with ``raise_on_missing=False`` (the default), callers MUST
    check for ``None`` before use. Forgetting this leads to
    ``AttributeError: 'NoneType' object has no attribute 'search'`` at call
    sites that assume a Collection. Pass ``raise_on_missing=True`` in code
    paths where a missing collection is unrecoverable.
    """
    try:
        # Ensure connection
        try:
            utility.list_collections()
        except Exception:
            connections.connect(alias="default", uri=settings.milvus_uri)

        # Auto-create if missing
        if not utility.has_collection(COLLECTION_NAME):
            logger.warning("Collection '%s' not found — attempting auto-create",
                           COLLECTION_NAME)
            _auto_create_collection()

        # Final check after auto-create attempt
        if not utility.has_collection(COLLECTION_NAME):
            msg = f"Collection '{COLLECTION_NAME}' not found and auto-creation failed"
            if raise_on_missing:
                raise RuntimeError(msg)
            logger.error(msg)
            return None

        col = Collection(COLLECTION_NAME)
        col.load()
        return col
    except RuntimeError:
        raise
    except Exception as e:
        msg = f"Failed to get Milvus collection: {e}"
        if raise_on_missing:
            raise RuntimeError(msg) from e
        logger.error(msg)
        return None
