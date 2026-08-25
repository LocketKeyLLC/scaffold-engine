"""§17.822 (audit C11 / plan 6.4) — dump toon_v2 to JSONL on stdout.

Runs INSIDE the orchestrator container so it reuses the app's Milvus client
config — scripts/backup.sh injects it via ``docker exec … python -c "$(cat
…)"`` (the rootfs is read-only and /tmp is a tmpfs docker cp can't reach).
Emits one JSON object per entity with the 16 explicit schema fields — the
BM25 sparse vector is deliberately excluded (it is function-generated from
canonical_text and regenerates on insert; exporting it would also break
imports into a bm25-off collection).

Usage (from the host):
    docker exec scaffold-orchestrator python -c "$(cat scripts/milvus_export.py)" > toon_v2.jsonl
"""
from __future__ import annotations

import json
import sys

from app.utils.milvus_utils import COLLECTION_NAME, get_client

# The 16 canonical fields (build_toon_v2_schema). dense_vector included —
# re-embedding 3.6k+ entries on restore would take hours on CPU.
FIELDS = [
    "entry_id", "title", "canonical_text", "domain", "domain_tags",
    "confidence_score", "source_type", "source_url", "content_hash",
    "model_id", "version", "supersedes_id", "created_at", "updated_at",
    "expires_at", "dense_vector",
]


def main() -> int:
    client = get_client(raise_on_missing=True)
    total = 0
    it = client.query_iterator(
        collection_name=COLLECTION_NAME,
        batch_size=500,
        output_fields=FIELDS,
    )
    try:
        while True:
            batch = it.next()
            if not batch:
                break
            for row in batch:
                # pymilvus returns non-JSON container types: numpy floats in
                # the vector, protobuf RepeatedScalarContainer for ARRAY
                # fields (domain_tags). Coerce to plain lists/floats.
                vec = row.get("dense_vector")
                if vec is not None:
                    row["dense_vector"] = [float(x) for x in vec]
                tags = row.get("domain_tags")
                if tags is not None:
                    row["domain_tags"] = [str(t) for t in tags]
                print(json.dumps({f: row.get(f) for f in FIELDS}, ensure_ascii=False))
                total += 1
    finally:
        it.close()
    print(f"exported {total} entities from {COLLECTION_NAME}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
