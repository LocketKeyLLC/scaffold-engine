#!/usr/bin/env bash
# §17.822 (audit C11 / plan 6.4) — full-state backup: Postgres + Milvus + manifest.
#
# Produces .backups/<UTC timestamp>/ containing:
#   pg_scaffold_engine.dump.gz   — pg_dump -Fc of ALL engine state (jobs, DAG
#                                  nodes, sessions, schedules, keys, overrides…)
#   toon_v2.jsonl.gz             — every Milvus entity incl. dense vectors
#                                  (16 canonical fields; BM25 sparse regenerates)
#   manifest.json                — row/entity counts to verify a restore against
#
# Both Postgres and (since §17.855) the Milvus corpus survive `docker compose
# down` on their named volumes — the corpus-loss-on-down bug (embedded etcd on
# the ephemeral overlay) is fixed by pinning etcd onto the volume. Still run me
# BEFORE any upgrade or volume surgery; it's the drill-verified recovery path.
#
# §17.855 — retention: keeps the newest BACKUP_RETENTION (default 7) timestamped
# backups and prunes older ones (.backups shares the data-root fs, which has
# filled before). See the tail of this script.
#
# Usage: scripts/backup.sh            (from the repo root; or `make backup`)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PGUSER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- || true)"
PGUSER="${PGUSER:-scaffold}"

for c in scaffold-postgres scaffold-orchestrator; do
    if ! docker ps --format '{{.Names}}' | grep -qx "$c"; then
        echo "FATAL: $c is not running — start the stack first (docker compose up -d)" >&2
        exit 1
    fi
done

TS="$(date -u +%Y%m%d_%H%M%SZ)"
DEST=".backups/$TS"
mkdir -p "$DEST"

echo "▶ backup → $DEST"

echo "  postgres: pg_dump -Fc scaffold_engine…"
docker exec scaffold-postgres pg_dump -U "$PGUSER" -Fc scaffold_engine \
    | gzip > "$DEST/pg_scaffold_engine.dump.gz"

echo "  milvus: exporting toon_v2 (query_iterator, 16 fields + vectors)…"
# python -c with the host script's source: the orchestrator rootfs is
# read-only (+ /tmp is a tmpfs docker cp can't reach), and this also runs
# the CURRENT script against older baked images.
docker exec scaffold-orchestrator python -c "$(cat scripts/milvus_export.py)" \
    | gzip > "$DEST/toon_v2.jsonl.gz"

echo "  manifest…"
JOBS="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc 'SELECT count(*) FROM jobs')"
NODES="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc 'SELECT count(*) FROM dag_nodes')"
SESSIONS="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc 'SELECT count(*) FROM research_sessions')"
TABLES="$(docker exec scaffold-postgres psql -U "$PGUSER" -d scaffold_engine -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
ENTITIES="$(zcat "$DEST/toon_v2.jsonl.gz" | wc -l)"
cat > "$DEST/manifest.json" << MANIFEST
{
  "created_utc": "$TS",
  "postgres": {
    "database": "scaffold_engine",
    "format": "pg_dump -Fc | gzip",
    "public_tables": $TABLES,
    "jobs": $JOBS,
    "dag_nodes": $NODES,
    "research_sessions": $SESSIONS
  },
  "milvus": {
    "collection": "toon_v2",
    "format": "jsonl (16 canonical fields, dense_vector included) | gzip",
    "entities": $ENTITIES
  }
}
MANIFEST

echo "✓ backup complete:"
ls -lh "$DEST" | tail -n +2 | awk '{printf "    %s  %s\n", $5, $9}'
echo "  manifest: jobs=$JOBS dag_nodes=$NODES research_sessions=$SESSIONS toon_v2=$ENTITIES"
echo "  restore with: make restore BACKUP=$TS"

# §17.855 — retention prune. Keep the newest N timestamped backups (this one
# included), remove older. Matches ONLY the YYYYMMDD_HHMMSSZ dirs so ad-hoc
# exports (e.g. eng_partition_pre_*.json) are never touched.
RETENTION="${BACKUP_RETENTION:-7}"
mapfile -t _old < <(ls -1d .backups/[0-9]*Z/ 2>/dev/null | sort -r | tail -n +$((RETENTION + 1)))
if [ "${#_old[@]}" -gt 0 ]; then
    echo "  retention (keep $RETENTION): pruning ${#_old[@]} older backup(s):"
    for d in "${_old[@]}"; do rm -rf "$d" && echo "    removed ${d%/}"; done
fi
