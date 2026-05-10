#!/usr/bin/env bash
# Scaffold Engine — KB repopulation runbook (audit N4).
#
# After the §17.63 SSD migration left the toon_v2 collection empty, this
# script is the canonical path for getting representative content back
# into the KB. Curated source list spans all four populated partitions
# from the pre-migration corpus (eng, llm, rag, spec); the prompt
# partition stays empty by design (per the OVERVIEW retrieval baseline,
# and per the existing per-query skip marks in test_retrieval_golden.py).
#
# Three ingest modes (in order of speed):
#
#   github:<owner>/<repo>   — README + docs/**.md + top-level *.py docstrings
#                             1-5 min; respects RESEARCH_FETCH_CONCURRENCY
#                             rate limits via shared httpx pool.
#   <https://...>            — direct URL ingest (single-page); 3-8 min;
#                             fetches HTML, extracts main content, embeds.
#   <topic>                  — autonomous research loop; 18-27 min shallow,
#                             45-90 min medium. Drives SearXNG search +
#                             extraction over multiple iterations.
#
# Each entry below is tagged with the expected partition and a rough
# upper bound on runtime so the operator can pick a budget. The script
# defaults to --dry-run; pass --apply to actually run the ingestions
# (in series — parallel would dogpile Ollama on this CPU-only host).
#
# Usage:
#   bash scripts/repopulate_kb.sh                 # dry-run (default)
#   bash scripts/repopulate_kb.sh --apply         # run all enabled rows
#   bash scripts/repopulate_kb.sh --apply --tier fast   # only github + url rows
#   bash scripts/repopulate_kb.sh --apply --tier topic  # only autonomous topics
#   bash scripts/repopulate_kb.sh --apply --force # bypass the running-session pre-flight
#
# Exits non-zero if any ingestion's SSE stream surfaces an error event
# OR the orchestrator's post-run /health milvus.entry_count didn't grow.

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
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RST"; }

# ── Args ──────────────────────────────────────────────────────────────
APPLY=0
FORCE=0
TIER="all"  # all | fast | topic
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --force|-f) FORCE=1 ;;
        --tier=fast|--tier=topic|--tier=all) TIER="${arg#--tier=}" ;;
        --tier) shift || true ;;  # consumed below if present
        fast|topic|all)
            # Accept bare positional --tier value: --tier fast
            if [[ "${PREV_ARG:-}" == "--tier" ]]; then TIER="$arg"; fi ;;
        --help|-h)
            cat <<'USAGE'
Usage: bash scripts/repopulate_kb.sh [--dry-run|--apply] [--tier fast|topic|all] [--force]

  --dry-run  (default) Print the curated source list without running anything.
  --apply              Actually invoke /research on each source, in series.
  --tier all (default) Every entry below.
       fast            github: + URL entries only (10-40 min total).
       topic           Autonomous topic research entries (~2-3 hours total).
  --force              Skip the pre-flight stuck-session check on --apply.
                       Use only if you've already inspected the existing
                       running sessions and they're not blockers.

Run from the repo root. Requires the orchestrator stack up + SCAFFOLD_API_KEY
in .env (or exported). Streams each source's SSE events to stdout so progress
is visible in real time. Honors the orchestrator's existing concurrency caps;
parallel would dogpile Ollama on this CPU-only host.

Closes audit N4 (KB repopulation plan post-§17.63).
USAGE
            exit 0 ;;
        *) err "Unknown argument: $arg"; exit 2 ;;
    esac
    PREV_ARG="$arg"
done

# ── Curated source list ──────────────────────────────────────────────
# Format: kind|target|partition|runtime_minutes|description
#
# Partitions reflect topic_to_domain and the auto-detector heuristics in
# research_agent. The 'expected' partition is best-guess — the
# orchestrator's domain detector decides at ingest time, so the actual
# landing partition can shift if the topic surface area straddles two.

# Tier 1: fast (github: + URL — 1-8 min each)
FAST_SOURCES=(
    "github|anthropics/anthropic-cookbook|llm|3|Anthropic API patterns + tool-use examples"
    "github|pytorch/torchtune|llm|4|LLM fine-tuning recipes (RLHF, LoRA, QLoRA)"
    "url|https://en.wikipedia.org/wiki/Test-driven_development|eng|5|TDD principles (golden query: eng-test)"
    "url|https://en.wikipedia.org/wiki/Software_design_pattern|eng|5|Design patterns survey (golden query: eng-pattern)"
    "url|https://en.wikipedia.org/wiki/Vector_database|rag|5|Vector DB primer + hybrid retrieval"
    "url|https://en.wikipedia.org/wiki/Retrieval-augmented_generation|rag|5|RAG architecture overview"
)

# Tier 2: topic (autonomous research — 18-27 min shallow each)
TOPIC_SOURCES=(
    "topic|How does function calling work in LLM tool use?|llm|22|Drives full research loop; depth=shallow"
    "topic|How does hybrid search combine dense and sparse retrieval?|rag|22|RAG-domain seed (golden query)"
    "topic|What is quantization and how does it reduce model size?|llm|22|LLM-domain quantization (golden query)"
)

print_row() {
    local kind="$1" target="$2" part="$3" rt="$4" desc="$5"
    printf '  %s%-7s%s %s%-3sm%s  partition=%-4s  %s\n' \
        "$C_INFO" "$kind" "$C_RST" "$C_DIM" "$rt" "$C_RST" "$part" "$desc"
    printf '          %s%s%s\n' "$C_DIM" "$target" "$C_RST"
}

run_research() {
    local kind="$1" target="$2"
    local payload
    if [[ "$kind" == "github" ]]; then
        # /research dispatches by topic shape; "github:owner/repo" triggers github mode.
        payload=$(printf '{"topic":"github:%s","depth":"shallow"}' "$target")
    elif [[ "$kind" == "url" ]]; then
        payload=$(printf '{"topic":"%s","depth":"shallow"}' "$target")
    elif [[ "$kind" == "topic" ]]; then
        # Escape quotes in the topic string; jq if available, sed fallback.
        if command -v jq >/dev/null 2>&1; then
            payload=$(jq -nc --arg t "$target" '{topic:$t,depth:"shallow"}')
        else
            local esc
            esc="$(printf '%s' "$target" | sed 's/"/\\"/g')"
            payload=$(printf '{"topic":"%s","depth":"shallow"}' "$esc")
        fi
    else
        err "unknown kind: $kind"
        return 1
    fi

    # Write curl output to a temp file rather than piping through
    # `tee | grep | head -200`. The pipeline form was the §17.83
    # follow-up bug: when `head -200` exited at line 200 it SIGPIPE'd
    # back up through grep → tee → curl, making curl exit non-zero
    # mid-stream. On long sources (URL mode, ~10min wall time) that
    # SIGPIPE could land BEFORE the orchestrator's session-finalize,
    # leaving the research_session row stuck in `running` and tripping
    # the single-running guard for every subsequent source.
    #
    # File-based capture decouples curl from the parse stage entirely.
    # curl runs to its `--max-time` ceiling or natural EOF, then we
    # filter the static file in two follow-on steps.
    local curl_log
    curl_log="$(mktemp /tmp/repopulate_kb_curl.XXXXXX.log)"

    if ! curl -sS -N \
        --max-time 1800 \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: $SCAFFOLD_API_KEY" \
        -X POST "$ORCHESTRATOR_URL/research" \
        -d "$payload" \
        -o "$curl_log"; then
        err "curl failed for $kind:$target (response captured at $curl_log)"
        return 1
    fi

    # Snapshot the events stream for downstream analysis (mirrors the
    # previous tee target so existing /tmp/repopulate_kb_last.events
    # consumers keep working).
    grep -E '^event:|"event"' "$curl_log" > /tmp/repopulate_kb_last.events 2>/dev/null || true

    # Surface non-heartbeat events for the operator. awk's bounded loop
    # runs over a static file so there's no upstream pipeline to
    # SIGPIPE; the cap matches the prior `head -200` cosmetic limit.
    awk '/^event: (research_started|iteration_started|iteration_complete|research_complete|error)$/||/^data:/{
        print
        c++
        if (c >= 200) exit
    }' "$curl_log"

    rm -f "$curl_log"

    if grep -q '^event: error$' /tmp/repopulate_kb_last.events 2>/dev/null; then
        err "research SSE surfaced an error event for $kind:$target — see /tmp/repopulate_kb_last.events"
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

# Snapshot the entry count up front so we can tell whether anything landed.
PRE_COUNT="$(curl -sS --max-time 5 "$ORCHESTRATOR_URL/health" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["milvus"]["entry_count"])' \
    2>/dev/null || echo "")"
if [[ -z "$PRE_COUNT" ]]; then
    err "could not reach orchestrator /health at $ORCHESTRATOR_URL — is the stack up?"
    exit 2
fi

# ── Header / dry-run ──────────────────────────────────────────────────
hdr "KB repopulation plan (audit N4 — post-§17.63 SSD migration)"
info "orchestrator: $ORCHESTRATOR_URL"
info "current Milvus entry_count: $PRE_COUNT"
info "tier: $TIER  apply: $([[ $APPLY == 1 ]] && echo yes || echo no)"

if [[ "$TIER" == "all" || "$TIER" == "fast" ]]; then
    hdr "Tier 1 — fast (github: + URL, 3-5 min each)"
    for row in "${FAST_SOURCES[@]}"; do
        IFS='|' read -r kind target part rt desc <<<"$row"
        print_row "$kind" "$target" "$part" "$rt" "$desc"
    done
fi
if [[ "$TIER" == "all" || "$TIER" == "topic" ]]; then
    hdr "Tier 2 — autonomous topic research (~22 min each)"
    for row in "${TOPIC_SOURCES[@]}"; do
        IFS='|' read -r kind target part rt desc <<<"$row"
        print_row "$kind" "$target" "$part" "$rt" "$desc"
    done
fi

if [[ $APPLY != 1 ]]; then
    echo
    dim "Re-run with --apply to actually ingest. Each source streams its SSE events to stdout."
    dim "After --apply finishes, run \`scripts/score_retrieval.py\` to re-baseline retrieval quality."
    exit 0
fi

# ── Pre-flight: running-session guard ────────────────────────────────
# The orchestrator enforces a single-running-research-session invariant
# (uq_research_sessions_single_running, migration 020). If a previous
# run was interrupted (Ctrl+C, container kill, host reboot), its
# session row stays in 'running' until the periodic reaper finds it,
# and every /research call below would error with "Research already
# in progress" — making the runbook look broken when it's just blocked
# behind a stale session.
#
# Pre-flight: query /research/sessions?status=running. If non-empty,
# print the IDs + topics + a one-liner remediation, then exit non-zero
# unless --force is set. Detected once at start; the script doesn't
# re-check between sources because each well-formed run finalizes its
# own session before the next starts.
RUNNING_JSON="$(curl -sS --max-time 5 \
    -H "X-Api-Key: $SCAFFOLD_API_KEY" \
    "$ORCHESTRATOR_URL/research/sessions?status=running" 2>/dev/null || echo '{"total":-1}')"
RUNNING_COUNT="$(printf '%s' "$RUNNING_JSON" | python3 -c \
    'import json,sys
try: print(json.load(sys.stdin)["total"])
except Exception: print(-1)' 2>/dev/null || echo -1)"

if [[ "$RUNNING_COUNT" == "-1" ]]; then
    err "could not query /research/sessions (orchestrator unreachable?) — aborting"
    exit 2
fi

if [[ "$RUNNING_COUNT" -gt 0 ]]; then
    warn "$RUNNING_COUNT existing research session(s) in 'running' state:"
    # Pull-out-then-format avoids backslash-escaped quotes inside an
    # f-string (which would be interpreted as shell escapes when this
    # python -c body is single-quoted from bash).
    printf '%s' "$RUNNING_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for s in d.get("sessions", []):
    sid = s["id"]
    depth = s["depth"]
    updated = s["updated_at"]
    topic = s["topic"][:60]
    print(f"    [{sid}] {depth:14} updated={updated} topic={topic}")
' 2>/dev/null || true
    if [[ $FORCE != 1 ]]; then
        err "every /research call below would block on the single-running guard."
        cat <<'REMEDIATE' >&2
       Resolve before retrying:
       1. If the session is genuinely still active, wait for it to complete.
       2. If it's stuck (no recent updated_at), cancel it via:
            docker exec scaffold-postgres psql -U scaffold -d scaffold_engine -c "
              UPDATE research_sessions SET status='cancelled', updated_at=NOW(),
                     last_activity_at=NOW() WHERE status='running';"
       3. Or re-run with --force to proceed anyway (sources will likely error
          until the running session finalizes).
REMEDIATE
        exit 2
    fi
    warn "--force passed — proceeding despite running session(s)."
fi

# ── Apply ─────────────────────────────────────────────────────────────
hdr "Applying ingestions (--apply)"
warn "Running serially — DO NOT cancel mid-flight unless you're prepared to clean up via /research/sessions."

ROWS_TO_RUN=()
[[ "$TIER" == "all" || "$TIER" == "fast" ]] && ROWS_TO_RUN+=("${FAST_SOURCES[@]}")
[[ "$TIER" == "all" || "$TIER" == "topic" ]] && ROWS_TO_RUN+=("${TOPIC_SOURCES[@]}")

INGESTED=0
FAILED=0
for row in "${ROWS_TO_RUN[@]}"; do
    IFS='|' read -r kind target part rt desc <<<"$row"
    hdr "[$((INGESTED+FAILED+1))/${#ROWS_TO_RUN[@]}] $kind | $target"
    info "$desc (expected ~${rt}m, partition=$part)"
    if run_research "$kind" "$target"; then
        ok "ingestion completed: $kind | $target"
        INGESTED=$((INGESTED+1))
    else
        err "ingestion failed: $kind | $target — continuing with next source"
        FAILED=$((FAILED+1))
    fi
done

# ── Summary ───────────────────────────────────────────────────────────
hdr "Summary"
POST_COUNT="$(curl -sS --max-time 5 "$ORCHESTRATOR_URL/health" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["milvus"]["entry_count"])' \
    2>/dev/null || echo "?")"
info "ingested: $INGESTED  failed: $FAILED  Milvus entry_count: $PRE_COUNT → $POST_COUNT"
if [[ "$POST_COUNT" == "?" ]] || [[ "$POST_COUNT" -le "$PRE_COUNT" ]]; then
    err "Milvus did not grow — check orchestrator logs (\`make logs-research\`)"
    exit 2
fi
ok "KB repopulation pass complete. Run \`scripts/score_retrieval.py\` to re-baseline."
