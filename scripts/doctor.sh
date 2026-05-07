#!/usr/bin/env bash
# Scaffold Engine — health audit (Sprint D.3)
#
# Read-only diagnostic. Probes every dependency, verifies the API key
# is in sync between .env and the running orchestrator container, and
# reports schema-migration tip. Exits non-zero if any check fails.
#
# Run from repo root:  bash scripts/doctor.sh   (or: make doctor)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
EXPLAIN=0

for arg in "$@"; do
    case "$arg" in
        --explain|-e) EXPLAIN=1 ;;
        --help|-h)
            cat <<USAGE
Usage: bash scripts/doctor.sh [--explain]

  --explain, -e   Print a one-line explanation under each section
                  describing what's being checked and why. Useful when
                  you're debugging or learning the system.
  --help,    -h   This message.
USAGE
            exit 0 ;;
    esac
done

# ---- ANSI helpers -----------------------------------------------------
if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
    C_INFO=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_DIM=""; C_RST=""
fi

FAIL=0
WARN_COUNT=0

pass()  { printf '  %sPASS%s  %s\n' "$C_OK"   "$C_RST" "$*"; }
fail()  { printf '  %sFAIL%s  %s\n' "$C_ERR"  "$C_RST" "$*"; FAIL=$((FAIL+1)); }
warn()  { printf '  %sWARN%s  %s\n' "$C_WARN" "$C_RST" "$*"; WARN_COUNT=$((WARN_COUNT+1)); }
info()  { printf '  %sINFO%s  %s\n' "$C_INFO" "$C_RST" "$*"; }
hdr()   { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_RST"; }
# Print a one-line section explanation when --explain is set. Each
# call documents what the section is verifying and why a failure
# would matter for normal operation.
explain() {
    if [[ $EXPLAIN -eq 1 ]]; then
        printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RST"
    fi
}

# ---- 1. .env file ----------------------------------------------------
hdr ".env"
explain "Verifies the four required runtime secrets exist and that .env beats valves.json on key rotation."

if [[ ! -f "$ENV_FILE" ]]; then
    fail ".env not found at $ENV_FILE — run 'make bootstrap'"
else
    pass ".env present"
    REQUIRED_VARS=(SCAFFOLD_API_KEY POSTGRES_PASSWORD WEBUI_SECRET_KEY OPENWEBUI_PIPELINES_KEY)
    for v in "${REQUIRED_VARS[@]}"; do
        if grep -qE "^${v}=.+" "$ENV_FILE"; then
            pass "$v set"
        else
            fail "$v missing or empty in .env"
        fi
    done
    if grep -qE "^SCAFFOLD_VALVES_ENV_OVERRIDE=(true|1|yes|on)" "$ENV_FILE"; then
        pass "SCAFFOLD_VALVES_ENV_OVERRIDE=true (env wins over valves.json)"
    else
        warn "SCAFFOLD_VALVES_ENV_OVERRIDE not enabled — valves.json beats .env on rotation"
    fi
fi

# ---- 2. Docker network + volumes ------------------------------------
hdr "Docker network + external volumes"
explain "The ai-network bridge connects all containers; missing it means compose can't bring the stack up. Volumes persist Postgres data, OWUI state, and the Milvus collection across restarts."

for n in ai-network; do
    if docker network inspect "$n" >/dev/null 2>&1; then
        pass "network $n exists"
    else
        fail "network $n missing — run 'make bootstrap'"
    fi
done
for vol in open-webui milvus-data-v2; do
    if docker volume inspect "$vol" >/dev/null 2>&1; then
        pass "volume $vol exists"
    else
        fail "volume $vol missing — run 'make bootstrap'"
    fi
done

# ---- 3. Containers ---------------------------------------------------
hdr "Containers"
explain "All 7 containers should be running. orchestrator hosts the API; postgres holds job state; milvus is the vector store; redis backs the embedding cache; open-webui serves chat; pipelines hosts slash-commands; searxng is the web-search backend for /research."

for c in scaffold-orchestrator scaffold-postgres milvus-standalone scaffold-redis open-webui open-webui-pipelines searxng; do
    state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    case "$state" in
        running) pass "$c $state" ;;
        missing) fail "$c not found — 'docker compose up -d'" ;;
        *)       fail "$c $state" ;;
    esac
done

# ---- 4. Orchestrator health ------------------------------------------
hdr "Orchestrator /health"
explain "The /health endpoint runs concurrent dependency probes (Postgres, Ollama, Milvus, Redis) and is the canonical 'is the system actually working' check. If this fails, all other failures are downstream."

HEALTH_JSON="$(curl -sf --max-time 5 http://localhost:8000/health 2>/dev/null || true)"
if [[ -z "$HEALTH_JSON" ]]; then
    fail "orchestrator /health unreachable at http://localhost:8000"
else
    pass "/health responding"
    # Pretty-parse subsystems if jq present, else grep.
    if command -v jq >/dev/null 2>&1; then
        for sub in postgresql ollama milvus redis; do
            status="$(printf '%s' "$HEALTH_JSON" | jq -r ".checks.${sub}.status // \"?\"" 2>/dev/null)"
            case "$status" in
                ok|healthy|up|true) pass "$sub: $status" ;;
                "?")  warn "$sub: status field absent" ;;
                *)    fail "$sub: $status" ;;
            esac
        done
    else
        info "jq not installed — skipping per-subsystem parse; raw response:"
        printf '         %s\n' "$HEALTH_JSON" | head -c 400
        echo
    fi
fi

# ---- 5. Ollama reachable from host bridge ---------------------------
hdr "Ollama (host)"
explain "Ollama runs on the host (not in a container). Containers reach it via the docker bridge gateway 172.18.0.1:11434. host.docker.internal isn't available on Pop!_OS native Docker, which is why the bridge IP is the right address."

if curl -sf --max-time 3 http://172.18.0.1:11434/api/tags >/dev/null 2>&1; then
    pass "Ollama reachable at 172.18.0.1:11434 (bridge gateway)"
elif curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
    warn "Ollama reachable at localhost but not bridge gateway 172.18.0.1 — containers may fail"
else
    warn "Ollama not reachable — install Ollama or set MODEL_*_PROVIDER to a cloud provider"
fi

# ---- 6. OpenAI provider (if configured) -----------------------------
hdr "OpenAI provider"
explain "Only run if any MODEL_*_PROVIDER=openai. Probes /models with the current key. OPENAI_BASE_URL can point at any OpenAI-compatible server (vLLM, LocalAI, Ollama-OpenAI-mode), so a 401 here means key drift, not necessarily that you're talking to api.openai.com."

if [[ -f "$ENV_FILE" ]]; then
    OPENAI_KEY="$(grep -E '^OPENAI_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    OPENAI_URL="$(grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"')"
    OPENAI_URL="${OPENAI_URL:-https://api.openai.com/v1}"

    OPENAI_BOUND_ROLES="$(grep -E '^MODEL_[A-Z_]+_PROVIDER=openai' "$ENV_FILE" | sed -E 's/^(MODEL_[A-Z_]+_PROVIDER)=.*/\1/' || true)"

    if [[ -z "${OPENAI_KEY:-}" ]] && [[ -z "$OPENAI_BOUND_ROLES" ]]; then
        info "OPENAI_API_KEY empty; no role bound to 'openai' — provider unused (OK)"
    elif [[ -z "${OPENAI_KEY:-}" ]] && [[ -n "$OPENAI_BOUND_ROLES" ]]; then
        fail "MODEL_*_PROVIDER=openai is set for: $(echo $OPENAI_BOUND_ROLES | tr '\n' ' ') but OPENAI_API_KEY is empty"
    else
        # Probe /models — read-only, ~150ms when reachable
        HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
            -H "Authorization: Bearer $OPENAI_KEY" \
            "${OPENAI_URL%/}/models" 2>/dev/null || echo 000)"
        case "$HTTP_CODE" in
            200) pass "OpenAI reachable at $OPENAI_URL (key OK)" ;;
            401) fail "OpenAI 401 — OPENAI_API_KEY invalid; rotate at the provider console" ;;
            403) fail "OpenAI 403 — key lacks access to the configured base URL" ;;
            429) warn "OpenAI 429 — rate-limited; key works but quota exhausted" ;;
            000) warn "OpenAI unreachable at $OPENAI_URL (network or DNS)" ;;
            *)   fail "OpenAI returned HTTP $HTTP_CODE at $OPENAI_URL/models" ;;
        esac
        if [[ -n "$OPENAI_BOUND_ROLES" ]]; then
            info "roles routed to openai: $(echo $OPENAI_BOUND_ROLES | tr '\n' ' ')"
        fi
    fi
fi

# ---- 7. API-key sync between .env and orchestrator container --------
hdr "API key sync"
explain "SCAFFOLD_API_KEY lives in 5 places that must stay aligned (.env, valves.json per pipeline, ~/.bashrc, the orchestrator container env, the OWUI pipelines container env). This check verifies the orchestrator container is running with the same value as .env. Drift here is the #1 cause of mysterious 401s."

if [[ -f "$ENV_FILE" ]] && docker ps --format '{{.Names}}' | grep -qx scaffold-orchestrator; then
    ENV_KEY="$(grep -E '^SCAFFOLD_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    CON_KEY="$(docker exec scaffold-orchestrator printenv SCAFFOLD_API_KEY 2>/dev/null || true)"
    if [[ -z "$ENV_KEY" ]]; then
        warn "SCAFFOLD_API_KEY empty in .env"
    elif [[ -z "$CON_KEY" ]]; then
        warn "SCAFFOLD_API_KEY not set in orchestrator container"
    elif [[ "$ENV_KEY" == "$CON_KEY" ]]; then
        pass ".env and orchestrator agree on SCAFFOLD_API_KEY"
    else
        fail ".env SCAFFOLD_API_KEY != orchestrator container value (restart compose to reload)"
    fi
fi

# ---- 8. Schema migrations -------------------------------------------
hdr "Schema migrations"
explain "Reports the highest applied migration. The runner auto-applies new files in db/migrations/ at lifespan startup; opt out with SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=false. Lagging here means startup didn't complete the migration phase."

if docker ps --format '{{.Names}}' | grep -qx scaffold-postgres; then
    PG_USER="${POSTGRES_USER:-scaffold}"
    HIGHEST="$(docker exec scaffold-postgres psql -U "$PG_USER" -d scaffold_engine -tAc \
        "SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "$HIGHEST" ]]; then
        pass "highest applied migration: $HIGHEST"
    else
        warn "could not query schema_migrations (DB unreachable or table missing)"
    fi
fi

# ---- summary ---------------------------------------------------------
hdr "Summary"

if [[ $FAIL -eq 0 && $WARN_COUNT -eq 0 ]]; then
    printf '%sAll checks passed.%s\n' "$C_OK" "$C_RST"
    exit 0
elif [[ $FAIL -eq 0 ]]; then
    printf '%s%d warnings, no failures.%s\n' "$C_WARN" "$WARN_COUNT" "$C_RST"
    exit 0
else
    printf '%s%d failures, %d warnings.%s\n' "$C_ERR" "$FAIL" "$WARN_COUNT" "$C_RST"
    exit 1
fi
