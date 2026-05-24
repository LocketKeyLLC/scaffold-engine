#!/usr/bin/env bash
# Scaffold Engine — strict sync of SCAFFOLD_API_KEY across all 5 places.
# Sprint X.8 (Tier 2 audit row #8). Distinct intent from sync_valves.sh:
#   sync_valves.sh   — WIPES api_key in valves.json so pipelines fall
#                      through to $SCAFFOLD_API_KEY (G.3 design).
#   sync_api_key.sh  — POPULATES the same key in all 5 places. Use when
#                      you don't want env-fallback (e.g. SCAFFOLD_VALVES_
#                      ENV_OVERRIDE off) or when rotating a leaked key.
#
# Five places kept aligned (per OVERVIEW conventions, "API key sync"):
#   1. .env                        SCAFFOLD_API_KEY=...
#   2. pipelines/*/valves.json     api_key field (5 files)
#   3. ~/.bashrc                   export SCAFFOLD_API_KEY=...
#   4. scaffold-orchestrator env   inherited from .env on restart
#   5. open-webui-pipelines env    inherited from .env on restart
#
# (4) and (5) update automatically on the next `docker compose restart` —
# the script prints the reminder rather than restarting (destructive,
# affects the live stack, requires explicit user intent).
#
# Usage:
#   make sync-api-key                          # verify + propagate from .env
#   make sync-api-key KEY=sk-scaffold-...      # set new key everywhere
#
# Test overrides:
#   SCAFFOLD_REPO_ROOT=/tmp/scratch ...        # for sandboxed pytest
#   SCAFFOLD_BASHRC_PATH=/tmp/.bashrc ...
#
# Idempotent: silent on matches, summary on changes.

# §17.277 — strict mode. -e: exit on unhandled non-zero; -u: error
# on unset vars; -o pipefail: surface non-final pipe failures. Each
# script's existing vars use ${VAR:-default} or explicit checks, so
# adding -u doesn't change semantics.
set -euo pipefail

set -euo pipefail

REPO_ROOT="${SCAFFOLD_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BASHRC_PATH="${SCAFFOLD_BASHRC_PATH:-$HOME/.bashrc}"

if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'
    C_WARN=$'\033[1;33m'
    C_INFO=$'\033[1;36m'
    C_DIM=$'\033[2m'
    C_RST=$'\033[0m'
else
    C_OK="" C_WARN="" C_INFO="" C_DIM="" C_RST=""
fi

ENV_FILE="$REPO_ROOT/.env"
KEY="${1:-}"

# ── 1. resolve KEY ─────────────────────────────────────────────────────────
if [[ -z "$KEY" ]]; then
    # No arg: read from .env. This is the "verify and propagate" mode.
    if [[ ! -f "$ENV_FILE" ]]; then
        printf '%sERROR:%s no KEY arg and %s not found.\n' \
            "$C_WARN" "$C_RST" "$ENV_FILE"
        exit 2
    fi
    # Strict grep so we don't match commented-out lines.
    KEY="$(grep -E '^SCAFFOLD_API_KEY=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
    KEY="${KEY%\"}"  # strip optional surrounding quotes
    KEY="${KEY#\"}"
    KEY="${KEY%\'}"
    KEY="${KEY#\'}"
    if [[ -z "$KEY" ]]; then
        printf '%sERROR:%s SCAFFOLD_API_KEY not set in %s.\n' \
            "$C_WARN" "$C_RST" "$ENV_FILE"
        exit 2
    fi
    SOURCE="$ENV_FILE"
else
    SOURCE="argument"
fi

# Sanity check: keys are sk-scaffold-<hex>, ~32+ chars. Catch typos early.
if [[ ! "$KEY" =~ ^sk-scaffold-[A-Za-z0-9]{8,}$ ]]; then
    printf '%sWARN:%s key does not match the expected sk-scaffold-<hex> shape.\n' \
        "$C_WARN" "$C_RST"
    printf '%s     %s proceeding anyway — verify if this was intentional.\n' \
        "$C_DIM" "$C_RST"
fi

printf '%s→%s syncing SCAFFOLD_API_KEY (source: %s)\n' "$C_INFO" "$C_RST" "$SOURCE"
echo

CHANGED=0
NOOP=0

# ── 2. .env (only when KEY came from arg) ──────────────────────────────────
if [[ "$SOURCE" == "argument" ]]; then
    if [[ -f "$ENV_FILE" ]]; then
        existing="$(grep -E '^SCAFFOLD_API_KEY=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
        existing="${existing%\"}"; existing="${existing#\"}"
        if [[ "$existing" == "$KEY" ]]; then
            printf '  %s-%s .env — already up to date\n' "$C_DIM" "$C_RST"
            NOOP=$((NOOP+1))
        else
            # Replace existing line in-place; or append if absent.
            tmp="$ENV_FILE.tmp.$$"
            if grep -qE '^SCAFFOLD_API_KEY=' "$ENV_FILE"; then
                sed -E "s|^SCAFFOLD_API_KEY=.*|SCAFFOLD_API_KEY=$KEY|" "$ENV_FILE" > "$tmp"
            else
                cat "$ENV_FILE" > "$tmp"
                printf 'SCAFFOLD_API_KEY=%s\n' "$KEY" >> "$tmp"
            fi
            mv "$tmp" "$ENV_FILE"
            printf '  %s✓%s .env — updated\n' "$C_OK" "$C_RST"
            CHANGED=$((CHANGED+1))
        fi
    else
        printf 'SCAFFOLD_API_KEY=%s\n' "$KEY" > "$ENV_FILE"
        printf '  %s✓%s .env — created\n' "$C_OK" "$C_RST"
        CHANGED=$((CHANGED+1))
    fi
fi

# ── 3. pipelines/*/valves.json ─────────────────────────────────────────────
shopt -s nullglob
for valves in "$REPO_ROOT"/pipelines/*/valves.json; do
    name="$(basename "$(dirname "$valves")")"
    # §17.250 — skip `_*`-prefixed subdirs. The OWUI pipelines loader
    # treats `_*` as the vendor-helper naming convention (per §17.212,
    # `pipelines/_vendor/*.py` is where shared modules live; top-level
    # subdirs prefixed with `_` are loader-leftover state from before
    # §17.212 and should NOT receive a writable api_key. The §17.249
    # cleanup removed the two existing such dirs; this guard prevents
    # them from being re-written if they reappear (e.g. an operator
    # manually creates one, or an old image revives the loader state).
    if [[ "$name" == _* ]]; then
        printf '  %s↷%s pipelines/%s/valves.json — skipped (vendor-helper dir; §17.250)\n' \
            "$C_DIM" "$C_RST" "$name"
        continue
    fi
    result="$(KEY="$KEY" python3 - "$valves" <<'PY'
import json, os, sys
path = sys.argv[1]
key = os.environ["KEY"]
with open(path) as f:
    data = json.load(f)
existing = data.get("api_key", "")
if existing == key:
    print("NOOP")
else:
    data["api_key"] = key
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    print("CHANGED")
PY
)"
    case "$result" in
        CHANGED)
            printf '  %s✓%s pipelines/%s/valves.json — updated\n' \
                "$C_OK" "$C_RST" "$name"
            CHANGED=$((CHANGED+1))
            ;;
        NOOP)
            printf '  %s-%s pipelines/%s/valves.json — already up to date\n' \
                "$C_DIM" "$C_RST" "$name"
            NOOP=$((NOOP+1))
            ;;
    esac
done

# ── 4. ~/.bashrc ───────────────────────────────────────────────────────────
EXPORT_LINE="export SCAFFOLD_API_KEY=$KEY"
MARKER="# scaffold-engine: SCAFFOLD_API_KEY (managed by sync_api_key.sh)"

if [[ -f "$BASHRC_PATH" ]]; then
    if grep -qE '^export SCAFFOLD_API_KEY=' "$BASHRC_PATH"; then
        existing_line="$(grep -E '^export SCAFFOLD_API_KEY=' "$BASHRC_PATH" | tail -n1)"
        if [[ "$existing_line" == "$EXPORT_LINE" ]]; then
            printf '  %s-%s %s — already up to date\n' \
                "$C_DIM" "$C_RST" "$BASHRC_PATH"
            NOOP=$((NOOP+1))
        else
            tmp="$BASHRC_PATH.tmp.$$"
            sed -E "s|^export SCAFFOLD_API_KEY=.*|$EXPORT_LINE|" \
                "$BASHRC_PATH" > "$tmp"
            mv "$tmp" "$BASHRC_PATH"
            printf '  %s✓%s %s — updated\n' "$C_OK" "$C_RST" "$BASHRC_PATH"
            CHANGED=$((CHANGED+1))
        fi
    else
        # Append with a marker so future updates can find/replace cleanly.
        {
            echo ""
            echo "$MARKER"
            echo "$EXPORT_LINE"
        } >> "$BASHRC_PATH"
        printf '  %s✓%s %s — appended export line\n' \
            "$C_OK" "$C_RST" "$BASHRC_PATH"
        CHANGED=$((CHANGED+1))
    fi
else
    printf '  %s-%s %s — not present, skipped\n' \
        "$C_DIM" "$C_RST" "$BASHRC_PATH"
fi

# ── summary ────────────────────────────────────────────────────────────────
echo
printf '%sDone.%s changed=%d, already-aligned=%d\n' \
    "$C_INFO" "$C_RST" "$CHANGED" "$NOOP"
echo

if [[ "$CHANGED" -gt 0 ]]; then
    printf '%sNext steps:%s\n' "$C_WARN" "$C_RST"
    echo "  1. Restart containers so env-vars (4) + (5) refresh from .env:"
    echo "       docker compose restart scaffold-orchestrator open-webui-pipelines"
    echo "  2. (this shell) source $BASHRC_PATH    # to refresh \$SCAFFOLD_API_KEY"
    echo "  3. Verify with:  make doctor"
fi
