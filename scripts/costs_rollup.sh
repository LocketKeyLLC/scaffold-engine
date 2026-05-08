#!/usr/bin/env bash
# Scaffold Engine — top-N expensive jobs rollup.
# Sprint J.3.c (Tier 2 audit final). Reads from llm_call_logs (J.3.a)
# and groups by job_id with totals from the seeded model_costs table
# (J.3.a). Off-job calls (job_id IS NULL) are aggregated into one row.
#
# Usage:
#   make costs            # default top 10
#   make costs N=20       # top 20
#   make costs N=5        # top 5
#
# Output is whatever psql renders — operators can pipe through
# column / less.

set -euo pipefail

LIMIT="${N:-10}"

# Sanity-check N is a positive integer; otherwise psql will reject and
# the operator gets a confusing error.
if ! [[ "$LIMIT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: N must be a positive integer (got: $LIMIT)" >&2
    exit 2
fi

CONTAINER="${SCAFFOLD_PG_CONTAINER:-scaffold-postgres}"

# Build the SQL with $LIMIT inlined (validated above), then pipe via
# stdin so we don't need to wrestle with nested shell quoting through
# `docker exec bash -c '...'`. Using `psql -v LIMIT=$LIMIT` would also
# work but inlining keeps the script self-contained.
docker exec -i "$CONTAINER" bash -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --pset=footer=off' <<SQL
SELECT
    COALESCE(job_id::text, '(off-job)')         AS job_id,
    COUNT(*)                                     AS calls,
    ROUND(SUM(cost_usd)::numeric, 6)             AS total_cost_usd,
    SUM(prompt_tokens) + SUM(completion_tokens)  AS total_tokens,
    ROUND(AVG(latency_ms)::numeric, 0)           AS avg_latency_ms,
    MAX(created_at)                              AS last_call_at
FROM llm_call_logs
GROUP BY job_id
ORDER BY total_cost_usd DESC NULLS LAST, calls DESC
LIMIT ${LIMIT};
SQL
