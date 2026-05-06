#!/usr/bin/env bash
# Scaffold Engine — provider configuration wizard (Sprint G.2)
#
# Asks per-role provider choice + collects provider-specific API keys,
# then updates .env in place. Complements scripts/bootstrap.sh:
#
#   1. Run 'make bootstrap' first — generates secrets + brings up stack.
#   2. Run 'make init' to layer in per-role provider routing.
#   3. Run 'make restart' to reload the new env into the running stack.
#   4. Run 'make doctor' to verify keys + reachability.
#
# Run from repo root:  bash scripts/init.sh   (or: make init)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# ---- ANSI helpers (matches bootstrap.sh / doctor.sh) ----------------
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

# ---- preflight ------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    err ".env not found at $ENV_FILE"
    info "Run 'make bootstrap' first to generate secrets, then re-run 'make init'."
    exit 1
fi

# ---- detect Ollama --------------------------------------------------
hdr "Provider configuration"

# Read OLLAMA_BASE_URL from .env if set; fall back to the bridge default.
ENV_OLLAMA="$(grep -E '^OLLAMA_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
OLLAMA_URL="${ENV_OLLAMA:-http://172.18.0.1:11434}"

if curl -sf --max-time 3 "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    ok "Ollama reachable at $OLLAMA_URL"
    if command -v jq >/dev/null 2>&1; then
        MODELS_LIST="$(curl -sf --max-time 3 "${OLLAMA_URL}/api/tags" \
            | jq -r '.models[].name' 2>/dev/null || true)"
        if [[ -n "$MODELS_LIST" ]]; then
            info "Installed Ollama models:"
            echo "$MODELS_LIST" | sed 's/^/    /'
        fi
    fi
elif curl -sf --max-time 3 "http://localhost:11434/api/tags" >/dev/null 2>&1; then
    warn "Ollama reachable at localhost:11434 but not bridge gateway $OLLAMA_URL"
    info "Containers reach Ollama through the bridge — fix OLLAMA_BASE_URL in .env."
else
    warn "Ollama not detected. You can still configure roles for openai-only use."
fi

# ---- per-role provider prompts --------------------------------------
echo
say "For each role, choose a provider: ${C_INFO}ollama${C_RST} (local) or ${C_INFO}openai${C_RST} (cloud or any OpenAI-compatible endpoint)."
say "Press Enter to keep ${C_INFO}ollama${C_RST} (the default)."
echo

# Parallel arrays — bash 3 compatible (no associative arrays).
ROLE_NAMES=(
    "model_general"
    "model_verifier"
    "model_coder"
    "model_router"
    "model_fallback"
    "model_cloud_heavy"
    "model_cloud_alt"
    "model_embedder_pipeline"
)
ROLE_DESCS=(
    "analysis, planning, distill"
    "verify, decompose, extract"
    "CodeGen tasks"
    "cheap/fast routing"
    "fallback when primary fails"
    "designated heavy cloud model"
    "cloud alternative"
    "RAG embeddings (512d locked, see USER_GUIDE)"
)
ROLE_PROVIDERS=()

USES_OPENAI=0
for i in "${!ROLE_NAMES[@]}"; do
    role="${ROLE_NAMES[$i]}"
    desc="${ROLE_DESCS[$i]}"
    printf '  %s%s%s — %s\n' "$C_INFO" "$role" "$C_RST" "$desc"
    printf '  Provider [ollama]: '
    read -r choice
    choice="${choice:-ollama}"
    case "$choice" in
        ollama|openai) ;;
        *) warn "Unknown provider '$choice' — keeping 'ollama'"; choice="ollama" ;;
    esac
    ROLE_PROVIDERS[$i]="$choice"
    [[ "$choice" == "openai" ]] && USES_OPENAI=1
    echo
done

# ---- OpenAI key collection (only if any role chose openai) ----------
OPENAI_KEY=""
OPENAI_URL_OVERRIDE=""

if [[ $USES_OPENAI -eq 1 ]]; then
    hdr "OpenAI"
    EXISTING_KEY="$(grep -E '^OPENAI_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    if [[ -n "$EXISTING_KEY" ]]; then
        printf '  OpenAI API key [%s…, press Enter to keep]: ' "${EXISTING_KEY:0:8}"
    else
        printf '  OpenAI API key (sk-...): '
    fi
    read -r OPENAI_KEY
    OPENAI_KEY="${OPENAI_KEY:-$EXISTING_KEY}"

    if [[ -z "$OPENAI_KEY" ]]; then
        err "OPENAI_API_KEY is required when any role uses 'openai'. Aborting (no .env changes)."
        exit 1
    fi

    EXISTING_URL="$(grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    DEFAULT_URL="${EXISTING_URL:-https://api.openai.com/v1}"
    printf '  OpenAI base URL [%s]: ' "$DEFAULT_URL"
    read -r OPENAI_URL_OVERRIDE
    OPENAI_URL_OVERRIDE="${OPENAI_URL_OVERRIDE:-$DEFAULT_URL}"
fi

# ---- atomic .env update ---------------------------------------------
# Strategy: build the new file in a tmp path, then mv. Existing keys we
# manage are filtered out before the new lines are appended so re-running
# init.sh produces idempotent results.
hdr "Writing .env"

# Collect every key we manage so we filter them in one pass.
MANAGED_KEYS=("OPENAI_API_KEY" "OPENAI_BASE_URL")
for role in "${ROLE_NAMES[@]}"; do
    var="$(echo "${role}" | tr '[:lower:]' '[:upper:]')_PROVIDER"
    MANAGED_KEYS+=("$var")
done

# Build a grep -E pattern that excludes those exact key= lines.
EXCLUDE_PATTERN="$(printf '^(%s)=' "$(IFS='|'; echo "${MANAGED_KEYS[*]}")")"

# Filter, then append fresh values.
TMP_ENV="${ENV_FILE}.tmp"
grep -vE "$EXCLUDE_PATTERN" "$ENV_FILE" > "$TMP_ENV" || true

{
    echo ""
    echo "# ---------- Per-role provider routing (managed by scripts/init.sh) ----------"
    for i in "${!ROLE_NAMES[@]}"; do
        role="${ROLE_NAMES[$i]}"
        provider="${ROLE_PROVIDERS[$i]}"
        var="$(echo "${role}" | tr '[:lower:]' '[:upper:]')_PROVIDER"
        echo "${var}=${provider}"
    done
    if [[ $USES_OPENAI -eq 1 ]]; then
        echo ""
        echo "# ---------- OpenAI provider (managed by scripts/init.sh) ----------"
        echo "OPENAI_API_KEY=$OPENAI_KEY"
        echo "OPENAI_BASE_URL=$OPENAI_URL_OVERRIDE"
    fi
} >> "$TMP_ENV"

mv "$TMP_ENV" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Echo what was written (mask the key)
for i in "${!ROLE_NAMES[@]}"; do
    role="${ROLE_NAMES[$i]}"
    provider="${ROLE_PROVIDERS[$i]}"
    var="$(echo "${role}" | tr '[:lower:]' '[:upper:]')_PROVIDER"
    info "${var}=${provider}"
done
if [[ $USES_OPENAI -eq 1 ]]; then
    info "OPENAI_API_KEY=${OPENAI_KEY:0:8}…  (masked)"
    info "OPENAI_BASE_URL=$OPENAI_URL_OVERRIDE"
fi

ok ".env updated"

# ---- next steps -----------------------------------------------------
hdr "Next steps"
cat <<NEXT

  ${C_INFO}make restart${C_RST}   # reload orchestrator + pipelines with the new env
  ${C_INFO}make doctor${C_RST}    # verify keys, providers, reachability

If you flipped any role away from ollama and removed Ollama models that
the new providers will replace, re-run 'make doctor' first to catch any
stranded references.
NEXT
