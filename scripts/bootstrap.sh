#!/usr/bin/env bash
# Scaffold Engine — interactive bootstrap (Sprint D.2)
#
# One-command setup for a fresh clone:
#   1. Creates external Docker network + volumes if missing.
#   2. Prompts for required secrets, generates strong defaults.
#   3. Writes .env (refuses to clobber an existing one unless --force).
#   4. Builds + starts the stack.
#   5. Prints health-check URLs + the next-step command.
#
# Run from repo root:  bash scripts/bootstrap.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
FORCE=0
NONINTERACTIVE=0

# ---- ANSI helpers -----------------------------------------------------
if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
    C_INFO=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_DIM=""; C_RST=""
fi
say()   { printf '%s\n' "$*"; }
ok()    { printf '%s✓%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn()  { printf '%s!%s %s\n' "$C_WARN" "$C_RST" "$*"; }
err()   { printf '%sx%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; }
info()  { printf '%s>%s %s\n' "$C_INFO" "$C_RST" "$*"; }
hdr()   { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_RST"; }

# ---- argument parsing ------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        --yes|-y)   NONINTERACTIVE=1 ;;
        --help|-h)
            cat <<USAGE
Usage: bash scripts/bootstrap.sh [--force] [--yes]

  --force, -f   Overwrite an existing .env (default: refuse).
  --yes,   -y   Non-interactive: auto-accept generated secrets.
                Manual values (e.g. GITHUB_TOKEN) get left blank.
  --help,  -h   This message.
USAGE
            exit 0 ;;
        *) err "Unknown argument: $arg"; exit 2 ;;
    esac
done

# ---- preflight -------------------------------------------------------
hdr "Preflight"

command -v docker >/dev/null 2>&1 || { err "docker not found in PATH"; exit 1; }
ok "docker present"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    err "docker compose plugin not found"; exit 1
fi
ok "$COMPOSE available"

command -v openssl >/dev/null 2>&1 || { err "openssl required for secret generation"; exit 1; }
ok "openssl present"

# Ollama on host (required for default all-local stack; the orchestrator
# reaches host Ollama via the bridge gateway 172.18.0.1:11434).
# Skip the check if SCAFFOLD_BOOTSTRAP_SKIP_OLLAMA=1 — useful when every
# MODEL_*_PROVIDER points at OpenAI (or compat) and Ollama isn't needed.
if [[ "${SCAFFOLD_BOOTSTRAP_SKIP_OLLAMA:-0}" != "1" ]]; then
    if command -v ollama >/dev/null 2>&1; then
        if ollama list >/dev/null 2>&1; then
            ok "ollama present and running"
            # Warn about default models that aren't pulled. Hard-coded to
            # match the README's quick-start list.
            DEFAULT_MODELS=(qwen3:4b qwen2.5:7b qwen2.5-coder:7b qwen3-embedding:8b qwen3.5:latest)
            installed="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
            missing=()
            for m in "${DEFAULT_MODELS[@]}"; do
                if ! grep -qx "$m" <<<"$installed"; then
                    missing+=("$m")
                fi
            done
            if (( ${#missing[@]} > 0 )); then
                warn "ollama running but missing default models:"
                for m in "${missing[@]}"; do printf '       - %s\n' "$m"; done
                printf '       Pull them with: %sollama pull %s%s\n' \
                    "$C_DIM" "${missing[*]}" "$C_RST"
                printf '       (You can continue bootstrap; missing models surface as 422s at request time.)\n'
            else
                ok "all default models pulled (qwen3:4b, qwen2.5:7b, qwen2.5-coder:7b, qwen3-embedding:8b, qwen3.5:latest)"
            fi
        else
            warn "ollama installed but daemon not running. Try: 'ollama serve' (foreground) or check the systemd unit."
            printf '       %sBootstrap will continue; the stack will boot but ideate/research will fail until Ollama is reachable.%s\n' \
                "$C_DIM" "$C_RST"
        fi
    else
        warn "ollama not found in PATH. Install from https://ollama.ai before running ideate/research."
        printf '       %sSet SCAFFOLD_BOOTSTRAP_SKIP_OLLAMA=1 to skip this check (only safe if every MODEL_*_PROVIDER will route to OpenAI / compatible).%s\n' \
            "$C_DIM" "$C_RST"
    fi
fi

# Network + volumes that compose declares as external must exist.
ensure_network() {
    # Args: name [subnet] [gateway]
    # When subnet+gateway are given, the network is created with the explicit
    # IPAM pin. If the network already exists with a different subnet, warn —
    # don't recreate (would require detaching every running container). The
    # ai-network pin is load-bearing: containers reach the host-installed
    # Ollama at the bridge gateway 172.18.0.1, hardcoded in compose env and
    # operator memory. Without the pin a fresh bootstrap on any host lands on
    # the next free 172.X.0.0/16 and silently breaks Ollama reachability.
    local name="$1"
    local subnet="${2:-}"
    local gateway="${3:-}"
    if docker network inspect "$name" >/dev/null 2>&1; then
        if [[ -n "$subnet" ]]; then
            local actual
            actual="$(docker network inspect "$name" \
                --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)"
            if [[ -n "$actual" && "$actual" != "$subnet" ]]; then
                warn "network '$name' exists but subnet '$actual' differs from expected '$subnet'"
                printf '       %sto fix: stop the stack, run \`docker network rm %s\`, re-run bootstrap%s\n' \
                    "$C_DIM" "$name" "$C_RST"
            else
                ok "network '$name' present (subnet ${actual:-unset})"
            fi
        else
            ok "network '$name' present"
        fi
    else
        info "creating network '$name'"
        if [[ -n "$subnet" && -n "$gateway" ]]; then
            docker network create \
                --driver bridge \
                --subnet "$subnet" \
                --gateway "$gateway" \
                "$name" >/dev/null
            ok "network '$name' created (subnet $subnet, gateway $gateway)"
        else
            docker network create "$name" >/dev/null
            ok "network '$name' created"
        fi
    fi
}
ensure_volume() {
    if docker volume inspect "$1" >/dev/null 2>&1; then
        ok "volume '$1' present"
    else
        info "creating volume '$1'"
        docker volume create "$1" >/dev/null
        ok "volume '$1' created"
    fi
}

ensure_network "ai-network" "172.18.0.0/16" "172.18.0.1"
ensure_volume "open-webui"
ensure_volume "milvus-data-v2"

# ---- .env handling ---------------------------------------------------
hdr ".env"

if [[ -f "$ENV_FILE" && $FORCE -eq 0 ]]; then
    warn "$ENV_FILE already exists. Re-run with --force to overwrite."
    info "Skipping .env generation; using existing values."
    SKIP_ENV=1
else
    SKIP_ENV=0
fi

prompt() {
    # prompt VAR DESCRIPTION DEFAULT
    local var="$1" desc="$2" default="${3:-}"
    if [[ $NONINTERACTIVE -eq 1 ]]; then
        eval "$var=\"\${default}\""
        return
    fi
    local input
    if [[ -n "$default" ]]; then
        printf '  %s\n  %s[default: %s]%s ' "$desc" "$C_DIM" "${default:0:8}…" "$C_RST"
    else
        printf '  %s\n  ' "$desc"
    fi
    read -r input
    eval "$var=\"\${input:-\$default}\""
}

if [[ $SKIP_ENV -eq 0 ]]; then
    info "Generating fresh secrets where applicable. Press Enter to accept defaults."
    echo

    GEN_API_KEY="$(openssl rand -hex 32)"
    GEN_PG_PASS="$(openssl rand -hex 24)"
    GEN_WEBUI_KEY="$(openssl rand -hex 32)"
    GEN_PIPES_KEY="$(openssl rand -hex 24)"

    prompt SCAFFOLD_API_KEY    "Orchestrator API key (X-API-Key header)"      "$GEN_API_KEY"
    prompt POSTGRES_PASSWORD   "Postgres password"                            "$GEN_PG_PASS"
    prompt WEBUI_SECRET_KEY    "Open WebUI session-cookie key"                "$GEN_WEBUI_KEY"
    prompt OPENWEBUI_PIPELINES_KEY "OWUI pipelines auth key"                  "$GEN_PIPES_KEY"
    prompt GITHUB_TOKEN        "GitHub PAT (optional, for /research github:; blank to skip)" ""

    cat > "$ENV_FILE" <<EOF
# Generated by scripts/bootstrap.sh at $(date -Iseconds)
# Edit values as needed and re-run \`make doctor\` to verify.

# ---------- Required ----------
SCAFFOLD_API_KEY=$SCAFFOLD_API_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
WEBUI_SECRET_KEY=$WEBUI_SECRET_KEY
OPENWEBUI_PIPELINES_KEY=$OPENWEBUI_PIPELINES_KEY

# ---------- Single-source-of-truth precedence ----------
# .env beats valves.json for managed string fields. Recommended for prod.
SCAFFOLD_VALVES_ENV_OVERRIDE=true

# ---------- Optional ----------
GITHUB_TOKEN=$GITHUB_TOKEN
SCAFFOLD_AUTH_DISABLED=0
POSTGRES_USER=scaffold
LOG_LEVEL=info
SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=true
SCAFFOLD_PREWARM_RERANKER=true
EOF

    chmod 600 "$ENV_FILE"
    ok "wrote $ENV_FILE (chmod 600)"
fi

# ---- bring up the stack ---------------------------------------------
hdr "Stack"

info "building + starting containers (this may take several minutes on first run)"
( cd "$REPO_ROOT" && $COMPOSE up -d --build )
ok "compose up complete"

# ---- post-start sanity check ----------------------------------------
hdr "Sanity check"

# Wait briefly for orchestrator to bind :8000
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        ok "orchestrator responding at http://localhost:8000"
        break
    fi
    sleep 2
done
if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    warn "orchestrator not yet responding; check logs with: docker logs scaffold-orchestrator"
fi

# ---- final health audit ---------------------------------------------
# Run doctor as the final bootstrap step so the user sees a complete
# pass/fail summary rather than an "I think it's up" message. doctor
# is read-only and exits non-zero on any failure; we capture and
# report so bootstrap's exit code reflects overall health.
hdr "Final health audit"

if bash "$REPO_ROOT/scripts/doctor.sh"; then
    DOCTOR_OK=1
else
    DOCTOR_OK=0
fi

# ---- next steps ------------------------------------------------------
hdr "Done"

cat <<NEXT
Stack is up. Quick links:

  ${C_INFO}Open WebUI${C_RST}   http://localhost:3000      (chat UI)
  ${C_INFO}Health${C_RST}       http://localhost:8000/health
  ${C_INFO}Logs${C_RST}         docker logs -f scaffold-orchestrator

Next steps:
  1. Open http://localhost:3000 and create an admin account.
  2. The model selector should already show 'scaffold_router'.
  3. Type an idea or '/help' to see the command surface.
  4. Run 'make doctor' if anything looks off later. (Just ran above.)

NEXT

if [[ $DOCTOR_OK -eq 0 ]]; then
    warn "doctor reported failures above — review them before using the system."
    exit 1
fi
