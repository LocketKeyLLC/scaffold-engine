"""§17.822 (audit C11 / plan 6.4) — restore toon_v2 from JSONL on stdin.

Runs INSIDE the orchestrator container — scripts/restore.sh injects it via
``docker exec -i … python -c "$(cat …)"`` with the dump on stdin (read-only
rootfs; stdin stays free for data). get_client() auto-creates
the collection with the canonical schema if it's missing — the exact
post-`compose down` state this exists for (the Milvus-loses-collections trap,
§17.213-adjacent; README's old claim that down preserves collections was
false). Upserts by entry_id, so re-running against a live collection is
idempotent rather than duplicating.

Usage (from the host):
    docker cp scripts/milvus_import.py scaffold-orchestrator:/tmp/
    docker exec -i scaffold-orchestrator python /tmp/milvus_import.py < toon_v2.jsonl
"""
from __future__ import annotations

import json
import sys

from app.utils.milvus_utils import COLLECTION_NAME, get_client

BATCH = 200


def main() -> int:
    client = get_client(raise_on_missing=False)  # auto-creates if missing
    if client is None:
        print("FATAL: Milvus unreachable — is milvus-standalone healthy?", file=sys.stderr)
        return 1
    total = 0
    buf: list[dict] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        buf.append(json.loads(line))
        if len(buf) >= BATCH:
            client.upsert(collection_name=COLLECTION_NAME, data=buf)
            total += len(buf)
            buf = []
    if buf:
        client.upsert(collection_name=COLLECTION_NAME, data=buf)
        total += len(buf)
    client.flush(COLLECTION_NAME)
    stats = client.get_collection_stats(COLLECTION_NAME)
    print(
        f"imported {total} entities; {COLLECTION_NAME} now reports "
        f"row_count={stats.get('row_count')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
