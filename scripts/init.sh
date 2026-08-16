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

# ---- access model: single- vs multi-user (§17.807) ------------------
# Single-user (default): the master SCAFFOLD_API_KEY is the only credential.
# Multi-user: sets MULTI_USER_ENABLED=true — on next restart migration 066
# creates the api_keys table and auth ALSO accepts named scoped keys minted
# with `make key-add`. The master key stays valid as the admin key either way,
# so choosing multi-user never locks you out.
hdr "Access model"
EXISTING_MULTI="$(grep -E '^MULTI_USER_ENABLED=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' || true)"
DEFAULT_MULTI="${EXISTING_MULTI:-false}"
say "  ${C_INFO}single${C_RST} — one shared master API key (default)"
say "  ${C_INFO}multi${C_RST}  — master key + revocable named keys (make key-add / key-list / key-revoke)"
if [[ "$DEFAULT_MULTI" == "true" ]]; then
    printf '  Access model [multi]: '
else
    printf '  Access model [single]: '
fi
read -r ACCESS_CHOICE
if [[ -z "$ACCESS_CHOICE" ]]; then
    MULTI_USER="$DEFAULT_MULTI"
else
    case "$ACCESS_CHOICE" in
        multi|multi-user|multiuser) MULTI_USER="true" ;;
        single|single-user|singleuser) MULTI_USER="false" ;;
        *) warn "Unknown choice '$ACCESS_CHOICE' — keeping '${DEFAULT_MULTI}'"; MULTI_USER="$DEFAULT_MULTI" ;;
    esac
fi
if [[ "$MULTI_USER" == "true" ]]; then
    ok "Multi-user enabled — mint keys with 'make key-add LABEL=\"...\"' after restart."
else
    ok "Single-user (master key only)."
fi

# ---- compute profile: CPU-local vs GPU/cloud vLLM (§17.807) ----------
# cpu-local (default): every role runs on local Ollama — the all-local stack.
# gpu-cloud: route the 7 generation/reasoning roles through an OpenAI-compatible
# endpoint (a vLLM preset via OPENAI_BASE_URL — also LocalAI / api.openai.com).
# The embedder stays local Ollama (nomic-embed-text, 512d locked to Milvus) and
# the reranker is always the local CrossEncoder — so no embedding-dim risk.
hdr "Compute profile"
say "  ${C_INFO}cpu-local${C_RST} — all roles on local Ollama (default)"
say "  ${C_INFO}gpu-cloud${C_RST} — generation roles → vLLM / OpenAI-compatible endpoint; embedder stays local"
printf '  Compute profile [cpu-local]: '
read -r COMPUTE_CHOICE
COMPUTE_CHOICE="${COMPUTE_CHOICE:-cpu-local}"
case "$COMPUTE_CHOICE" in
    cpu-local|cpu|local) COMPUTE_PROFILE="cpu-local" ;;
    gpu-cloud|gpu|cloud|vllm) COMPUTE_PROFILE="gpu-cloud" ;;
    *) warn "Unknown profile '$COMPUTE_CHOICE' — keeping 'cpu-local'"; COMPUTE_PROFILE="cpu-local" ;;
esac

# Collect the vLLM / OpenAI-compatible endpoint up front for the gpu-cloud
# preset so the per-role loop below can default the generation roles to openai.
PRESET_OPENAI_CREDS=0
OPENAI_KEY=""
OPENAI_URL_OVERRIDE=""
if [[ "$COMPUTE_PROFILE" == "gpu-cloud" ]]; then
    ok "GPU/cloud — generation roles will default to 'openai' (vLLM preset)."
    EXISTING_URL="$(grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' || true)"
    # vLLM's OpenAI server is commonly http://<host>:8000/v1 on the bridge gateway.
    DEFAULT_URL="${EXISTING_URL:-http://172.18.0.1:8000/v1}"
    printf '  vLLM / OpenAI base URL [%s]: ' "$DEFAULT_URL"
    read -r OPENAI_URL_OVERRIDE
    OPENAI_URL_OVERRIDE="${OPENAI_URL_OVERRIDE:-$DEFAULT_URL}"

    EXISTING_KEY="$(grep -E '^OPENAI_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    # A self-hosted vLLM server usually ignores the key; a sentinel keeps the
    # OpenAI client happy. api.openai.com needs a real sk-... here.
    DEFAULT_KEY="${EXISTING_KEY:-EMPTY}"
    printf '  API key [%s, press Enter to keep]: ' "${DEFAULT_KEY:0:8}"
    read -r OPENAI_KEY
    OPENAI_KEY="${OPENAI_KEY:-$DEFAULT_KEY}"
    PRESET_OPENAI_CREDS=1
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

if [[ "$COMPUTE_PROFILE" == "gpu-cloud" ]]; then
    info "gpu-cloud preset: generation roles default to 'openai', embedder stays 'ollama'. Override any role below."
    echo
fi

USES_OPENAI=0
for i in "${!ROLE_NAMES[@]}"; do
    role="${ROLE_NAMES[$i]}"
    desc="${ROLE_DESCS[$i]}"
    # Per-role default from the compute preset: gpu-cloud flips the 7
    # generation roles to openai; the embedder is config-locked to local Ollama
    # (512d → Milvus), so it always defaults to ollama.
    role_default="ollama"
    if [[ "$COMPUTE_PROFILE" == "gpu-cloud" && "$role" != "model_embedder_pipeline" ]]; then
        role_default="openai"
    fi
    printf '  %s%s%s — %s\n' "$C_INFO" "$role" "$C_RST" "$desc"
    printf '  Provider [%s]: ' "$role_default"
    read -r choice
    choice="${choice:-$role_default}"
    case "$choice" in
        ollama|openai) ;;
        *) warn "Unknown provider '$choice' — keeping '${role_default}'"; choice="$role_default" ;;
    esac
    ROLE_PROVIDERS[$i]="$choice"
    [[ "$choice" == "openai" ]] && USES_OPENAI=1
    echo
done

# ---- OpenAI key collection (only if any role chose openai) ----------
# The gpu-cloud preset already collected the endpoint + key above
# (PRESET_OPENAI_CREDS=1); only prompt here when a role was manually flipped to
# openai under the cpu-local profile and no creds were captured yet.
if [[ $USES_OPENAI -eq 1 && $PRESET_OPENAI_CREDS -eq 0 ]]; then
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
MANAGED_KEYS=("OPENAI_API_KEY" "OPENAI_BASE_URL" "MULTI_USER_ENABLED")
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
    echo "# ---------- Access model (managed by scripts/init.sh) ----------"
    echo "MULTI_USER_ENABLED=${MULTI_USER}"
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
info "MULTI_USER_ENABLED=${MULTI_USER}"
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

  # These settings live in .env, which the orchestrator loads via env_file.
  # 'make restart' does NOT reload env_file — you must RECREATE the container:
  ${C_INFO}docker compose up -d scaffold-orchestrator${C_RST}          # prod image
  ${C_INFO}make dev-up${C_RST}                                          # dev image (keeps mounts)
  ${C_INFO}make doctor${C_RST}                                          # verify keys, providers, reachability

If you flipped any role away from ollama and removed Ollama models that
the new providers will replace, re-run 'make doctor' first to catch any
stranded references.
NEXT

if [[ "$MULTI_USER" == "true" ]]; then
    cat <<MULTINEXT

  Multi-user is on. After the recreate above (migration 066 creates api_keys):
  ${C_INFO}make key-add LABEL="alice laptop"${C_RST}   # mint a scoped key (shown once)
  ${C_INFO}make key-list${C_RST}                        # list live keys
  ${C_INFO}make key-revoke ID=<n>${C_RST}               # revoke one
  The master SCAFFOLD_API_KEY still works as the admin key.
MULTINEXT
fi
