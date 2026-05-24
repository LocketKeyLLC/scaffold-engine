#!/usr/bin/env bash
# Scaffold Engine — Tier A golden-set recovery (post-§17.210).
#
# The `scripts/repopulate_kb.sh` runbook restores baseline coverage but
# doesn't target the specific entries `tests/fixtures/golden_set.json`
# expects. After §17.210's --tier=topic rebuild landed corpus 231 → 377
# but `scripts/score_retrieval.py` reported 0/20 coverage on the 2026-05-08
# golden set: every `expected_entry_ids` slug points at content from
# pre-§17.63 batch runs that this session's repopulation didn't restore.
#
# Slug-to-source archaeology (mapped via research_sessions history; see
# the §17.211 OVERVIEW entry for the full table):
#
#   Kahn algorithm / topological sort / truncation     → 1 batched run on 2026-04-18
#   Redis caching patterns / cache invalidation        → 2026-04-16/17 redis runs
#   gRPC vs REST performance                           → 2026-04-18 batch
#   gzip vs brotli HTTP compression                    → 2026-04-18 batch
#   OAuth2 bearer token patterns in FastAPI            → 2026-04-18 batch
#
# Six topic-mode replays at shallow depth. Each takes ~30-55 min on this
# CPU host (see §17.210 for the wall-time analysis); the new 60-min
# curl --max-time cap from §17.210 gives 1.5-2× headroom per source.
#
# Tier B (auth-platform / docker-image / distributed-systems entries:
# keycloak, bitnami/milvus, fastapi-docker-image, minimizing-eventual-
# consistency, vector-telemetry-data-router) is NOT included here — those
# slugs are not in research_sessions history pre-§17.63, so the source
# topic strings are unknown. Speculative re-ingestion would gamble on
# slug match (LLM-generated titles vary per run); held for a separate
# decision once Tier A confirms whether the slug-match assumption works
# at all.
#
# Usage (same shape as repopulate_kb.sh):
#   bash scripts/repopulate_kb_tier_a.sh                 # dry-run (default)
#   bash scripts/repopulate_kb_tier_a.sh --apply         # run all 6 sources
#   bash scripts/repopulate_kb_tier_a.sh --apply --force # bypass running-session guard
#
# Exits non-zero if any ingestion's SSE stream surfaces an error event
# OR the post-run /health milvus.entry_count didn't grow.

# §17.277 — strict mode. -e: exit on unhandled non-zero; -u: error
# on unset vars; -o pipefail: surface non-final pipe failures. Each
# script's existing vars use ${VAR:-default} or explicit checks, so
# adding -u doesn't change semantics.
set -euo pipefail

set -euo pipefail

# ── ANSI helpers ──────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
    C_INFO=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_DIM=""; C_RST=""
fi
ok()   { printf '%s✓%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_RST" "$*"; }
err()  { printf '%sx%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; }
info() { printf '%s>%s %s\n' "$C_INFO" "$C_RST" "$*"; }
hdr()  { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_RST"; }

# ── Args ──────────────────────────────────────────────────────────────
APPLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --force|-f) FORCE=1 ;;
        --help|-h)
            cat <<'USAGE'
Usage: bash scripts/repopulate_kb_tier_a.sh [--dry-run|--apply] [--force]

Run six topic-mode research sources targeting the §17.158 golden-set
recovery gap. Each source maps to a confirmed pre-§17.63 batch run from
the research_sessions history.

  --dry-run   Print plan; do not POST anything (default).
  --apply     Actually run the six sources in series.
  --force     Skip the "already-running session" pre-flight check.
USAGE
            exit 0 ;;
        *) err "unknown arg: $arg"; exit 2 ;;
    esac
done

# ── Source list ───────────────────────────────────────────────────────
# Six topics. Each row: kind|topic|partition|expected-min|description.
# `kind` is fixed `topic` for Tier A (these are all autonomous research
# runs). `partition` is the domain mapping — Kahn/cache/compression/grpc
# all eng-domain per the golden_set; truncation + oauth2 split across
# llm/eng but use eng to match the golden_set's pairs (both pairs flagged
# domain=eng).
TIER_A_SOURCES=(
    "topic|Kahn's algorithm for topological sorting and parallel implementation|eng|45|kahn's-algorithm-* + topological-sorting-process + parallel-implementation-of-kahn's-algorithm"
    "topic|Redis caching patterns: write-through, write-behind, and cache invalidation|eng|45|cache-invalidation + write-through-pattern + write-behind-caching + redis-cache-invalidation-patterns + caching-data-between-runs + storage-quotas-and-eviction"
    "topic|gRPC vs REST API performance benchmarks and tradeoffs|eng|45|grpc-vs-rest-performance + grpc-vs-rest-performance-benchmark"
    "topic|gzip vs brotli HTTP compression tradeoffs and lossless compression|eng|45|compression-dictionary-transport + lossless-compression"
    "topic|OAuth2 bearer token authentication patterns in FastAPI|eng|45|oauth2-proxy"
    "topic|Truncation vs rounding in computer science and mathematics|eng|45|truncation-definition + truncation-in-mathematics-and-computer-science"
)

# ── Helpers ───────────────────────────────────────────────────────────
print_row() {
    local kind="$1" target="$2" part="$3" rt="$4" desc="$5"
    printf '  %s%-7s%s %s%-3sm%s  partition=%-4s  %s\n' \
        "$C_INFO" "$kind" "$C_RST" "$C_DIM" "$rt" "$C_RST" "$part" "$desc"
    printf '          %s%s%s\n' "$C_DIM" "$target" "$C_RST"
}

run_research_topic() {
    local target="$1" partition="$2"
    local payload curl_log

    # jq if available; fallback escapes via sed.
    if command -v jq >/dev/null 2>&1; then
        payload=$(jq -nc --arg t "$target" --arg d "$partition" \
            '{topic:$t,depth:"shallow",domain:$d}')
    else
        local esc; esc="$(printf '%s' "$target" | sed 's/"/\\"/g')"
        payload=$(printf '{"topic":"%s","depth":"shallow","domain":"%s"}' "$esc" "$partition")
    fi

    curl_log="$(mktemp /tmp/repopulate_tier_a_curl.XXXXXX.log)"

    # §17.210 — 60-min curl cap; topic-mode iterations regularly take
    # 30-55 min on this CPU host.
    if ! curl -sS -N \
        --max-time 3600 \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: $SCAFFOLD_API_KEY" \
        -X POST "$ORCHESTRATOR_URL/research" \
        -d "$payload" \
        -o "$curl_log"; then
        err "curl failed for topic:$target (response captured at $curl_log)"
        return 1
    fi

    grep -E '^event:|"event"' "$curl_log" > /tmp/repopulate_tier_a_last.events 2>/dev/null || true
    awk '/^event: (research_started|iteration_started|iteration_complete|research_complete|error)$/||/^data:/{
        print; c++; if (c >= 200) exit
    }' "$curl_log"
    rm -f "$curl_log"

    if grep -q '^event: error$' /tmp/repopulate_tier_a_last.events 2>/dev/null; then
        err "research SSE surfaced an error event for topic:$target"
        return 1
    fi
}

# ── Pre-flight ────────────────────────────────────────────────────────
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:8000}"
SCAFFOLD_API_KEY="${SCAFFOLD_API_KEY:-}"
if [[ -z "$SCAFFOLD_API_KEY" ]]; then
    if [[ -f /mnt/adamssd/scaffold-engine/.env ]]; then
        SCAFFOLD_API_KEY="$(grep ^SCAFFOLD_API_KEY /mnt/adamssd/scaffold-engine/.env | cut -d= -f2-)"
    fi
fi
if [[ -z "$SCAFFOLD_API_KEY" ]]; then
    err "SCAFFOLD_API_KEY not set and not in .env — export it or add to .env first"
    exit 2
fi

PRE_COUNT="$(curl -sS --max-time 5 "$ORCHESTRATOR_URL/health" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["milvus"]["entry_count"])' \
    2>/dev/null || echo "")"
if [[ -z "$PRE_COUNT" ]]; then
    err "could not reach orchestrator /health at $ORCHESTRATOR_URL — is the stack up?"
    exit 2
fi

# ── Header / dry-run ──────────────────────────────────────────────────
hdr "Tier A golden-recovery plan (§17.211 — closes §17.158 expected-slug gap)"
info "orchestrator: $ORCHESTRATOR_URL"
info "current Milvus entry_count: $PRE_COUNT"
info "apply: $([[ $APPLY == 1 ]] && echo yes || echo no)"

hdr "Six topic-mode replays (~45 min each, ~4-5 hr total)"
for row in "${TIER_A_SOURCES[@]}"; do
    IFS='|' read -r kind target part rt desc <<<"$row"
    print_row "$kind" "$target" "$part" "$rt" "$desc"
done

if [[ "$APPLY" == 0 ]]; then
    hdr "Dry-run complete — re-run with --apply to ingest"
    exit 0
fi

# ── Pre-flight: any session already running? ──────────────────────────
if [[ "$FORCE" == 0 ]]; then
    RUNNING_TOPIC="$(curl -sS --max-time 5 "$ORCHESTRATOR_URL/research/sessions?status=running" \
        -H "X-Api-Key: $SCAFFOLD_API_KEY" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["topic"] if d else "")' \
        2>/dev/null || echo "")"
    if [[ -n "$RUNNING_TOPIC" ]]; then
        err "a research session is already running: '$RUNNING_TOPIC'"
        err "wait for it to finish, manually cancel via psql, or pass --force to bypass"
        exit 3
    fi
fi

# ── Apply ─────────────────────────────────────────────────────────────
hdr "Applying ingestions (--apply)"
warn "Running serially — DO NOT cancel mid-flight unless you're prepared to clean up via /research/sessions."

INGESTED=0
FAILED=0
TOTAL="${#TIER_A_SOURCES[@]}"
N=0
for row in "${TIER_A_SOURCES[@]}"; do
    IFS='|' read -r kind target part rt desc <<<"$row"
    N=$((N + 1))
    hdr "[$N/$TOTAL] topic | $target"
    info "$desc (expected ~${rt}m, partition=$part)"

    if run_research_topic "$target" "$part"; then
        ok "ingestion completed: topic | $target (partition=$part)"
        INGESTED=$((INGESTED + 1))
    else
        err "ingestion failed: topic | $target — continuing with next source"
        FAILED=$((FAILED + 1))
    fi
done

# ── Post-flight ───────────────────────────────────────────────────────
POST_COUNT="$(curl -sS --max-time 5 "$ORCHESTRATOR_URL/health" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["milvus"]["entry_count"])' \
    2>/dev/null || echo "")"

hdr "Summary"
info "ingested: $INGESTED  failed: $FAILED  Milvus entry_count: $PRE_COUNT → $POST_COUNT"
if [[ "$POST_COUNT" == "$PRE_COUNT" ]]; then
    err "Milvus entry_count did not grow — every source dedup-skipped or failed"
    exit 4
fi
ok "Tier A pass complete. Run \`scripts/score_retrieval.py\` to confirm golden-set coverage uplift."
