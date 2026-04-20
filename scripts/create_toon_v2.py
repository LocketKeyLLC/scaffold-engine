"""Create the toon_v2 collection with 512d HNSW_SQ8 and partition key isolation.

Dev/bootstrap script. Reuses schema + index builders from app.utils.milvus_utils
so there is a single source of truth (#42).
"""
from pymilvus import MilvusClient

from app.config import settings
from app.utils.milvus_utils import (
    COLLECTION_NAME as COLLECTION,
    build_toon_v2_schema,
    build_toon_v2_index_params,
)

client = MilvusClient(settings.milvus_uri)

# Drop if exists (dev only)
if client.has_collection(COLLECTION):
    client.drop_collection(COLLECTION)
    print(f"Dropped existing {COLLECTION}")

schema = build_toon_v2_schema()
print(f"Schema: {len(schema.fields)} fields")

index_params = build_toon_v2_index_params(client)

client.create_collection(
    collection_name=COLLECTION,
    schema=schema,
    index_params=index_params,
    num_partitions=settings.milvus_num_partitions,
    properties={"partitionkey.isolation": "true"},
)

print(f"Created {COLLECTION} with HNSW_SQ8 + partition key isolation")

info = client.describe_collection(COLLECTION)
print(f"Collection: {info['collection_name']}, fields: {len(info['fields'])}")
print("Done.")
