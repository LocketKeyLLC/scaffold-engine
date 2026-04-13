"""Create the toon_v2 collection with 512d HNSW_SQ8 and partition key isolation."""
from pymilvus import MilvusClient, DataType

COLLECTION = "toon_v2"
DIM = 512

client = MilvusClient("http://milvus-standalone:19530")

# Drop if exists (dev only)
if client.has_collection(COLLECTION):
    client.drop_collection(COLLECTION)
    print(f"Dropped existing {COLLECTION}")

# Schema
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

print(f"Schema: {len(schema.fields)} fields")

# Index params
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
# Scalar indexes
index_params.add_index(field_name="content_hash", index_type="INVERTED")
index_params.add_index(field_name="domain_tags", index_type="INVERTED")
index_params.add_index(field_name="source_type", index_type="BITMAP")
index_params.add_index(field_name="confidence_score", index_type="INVERTED")
index_params.add_index(field_name="created_at", index_type="STL_SORT")
index_params.add_index(field_name="version", index_type="BITMAP")

# Create collection with partition key isolation
client.create_collection(
    collection_name=COLLECTION,
    schema=schema,
    index_params=index_params,
    num_partitions=64,
    properties={"partitionkey.isolation": "true"},
)

print(f"Created {COLLECTION} with HNSW_SQ8 + partition key isolation")

# Verify
info = client.describe_collection(COLLECTION)
print(f"Collection: {info['collection_name']}, fields: {len(info['fields'])}")
print("Done.")
