#!/usr/bin/env bash
# §17.822 (audit C11 / plan 6.4) — restore Postgres + Milvus from a scripts/backup.sh dir.
#
# Usage:
#   scripts/restore.sh                    # newest .backups/<ts>, interactive confirm
#   scripts/restore.sh 20260825_120000Z   # a specific backup
#   scripts/restore.sh <ts> --yes         # skip the confirm (scripted use)
#
# Postgres: pg_restore --clean --if-exists (drops + recreates objects, so the
# database ends exactly at the dump's state). Milvus: upsert by entry_id into
# toon_v2 (auto-created with the canonical schema if missing — the
# post-`compose down` empty-collection state this tool exists for).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PGUSER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- || true)"
PGUSER="${PGUSER:-scaffold}"

ARG="${1:-}"
YES="${2:-}"
[[ "$ARG" == "--yes" ]] && { YES="--yes"; ARG=""; }

if [[ -n "$ARG" ]]; then
    DEST=".backups/$ARG"
else
    DEST="$(ls -d .backups/*/ 2>/dev/null | sort | tail -1 | sed 's:/$::')"
fi
if [[ -z "${DEST:-}" || ! -d "$DEST" ]]; then
    echo "FATAL: no backup found (${DEST:-.backups/ empty}) — run 'make backup' first" >&2
    exit 1
fi
for f in pg_scaffold_engine.dump.gz toon_v2.jsonl.gz manifest.json; do
    [[ -f "$DEST/$f" ]] || { echo "FATAL: $DEST/$f missing — incomplete backup" >&2; exit 1; }
done

for c in scaffold-postgres scaffold-orchestrator; do
    if ! docker ps --format '{{.Names}}' | grep -qx "$c"; then
        echo "FATAL: $c is not running — start the stack first (docker compose up -d)" >&2
        exit 1
    fi
done

echo "▶ restoring from $DEST"
sed 's/^/    /' "$DEST/manifest.json"

if [[ "$YES" != "--yes" ]]; then
    printf 'This OVERWRITES current Postgres state and upserts the Milvus corpus. Continue? [y/N] '
    read -r reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "aborted"; exit 1; }
fi

echo "  postgres: pg_restore --clean --if-exists…"
# --clean emits harmless "does not exist" notices on a fresh DB; -e would abort
# mid-restore on them, so rely on pg_restore's own exit status without -e.
zcat "$DEST/pg_scaffold_engine.dump.gz" \
    | docker exec -i scaffold-postgres pg_restore -U "$PGUSER" -d scaffold_engine \
        --clean --if-exists --no-owner

echo "  milvus: upserting toon_v2…"
# python -c (not docker cp): read-only rootfs + tmpfs /tmp; stdin stays free
# for the data stream.
zcat "$DEST/toon_v2.jsonl.gz" \
    | docker exec -i scaffold-orchestrator python -c "$(cat scripts/milvus_import.py)"

echo "  verifying against manifest…"
JOBS="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc 'SELECT count(*) FROM jobs')"
NODES="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc 'SELECT count(*) FROM dag_nodes')"
WANT_JOBS="$(grep -o '"jobs": [0-9]*' "$DEST/manifest.json" | grep -o '[0-9]*')"
WANT_NODES="$(grep -o '"dag_nodes": [0-9]*' "$DEST/manifest.json" | grep -o '[0-9]*')"
STATUS=0
if [[ "$JOBS" == "$WANT_JOBS" && "$NODES" == "$WANT_NODES" ]]; then
    echo "✓ postgres matches manifest (jobs=$JOBS dag_nodes=$NODES)"
else
    echo "✗ POSTGRES MISMATCH: jobs=$JOBS (want $WANT_JOBS) dag_nodes=$NODES (want $WANT_NODES)" >&2
    STATUS=1
fi
echo "  (milvus row_count printed above by the importer; compare to manifest"
echo "   entities — Milvus stats can lag briefly after flush)"
echo "▶ restart the orchestrator so in-process caches re-read restored state:"
echo "    docker restart scaffold-orchestrator"
exit $STATUS
