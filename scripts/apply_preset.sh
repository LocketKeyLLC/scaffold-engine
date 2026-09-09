#!/usr/bin/env bash
# §17.1002 — apply a tracked preset's keys into the gitignored .env.
#
# Idempotent: an existing key is REPLACED in place, a missing one appended, and
# every other line of .env is left exactly as it was — .env holds secrets and a
# rewrite would be a good way to lose them. Backs up first, prints a diff, and
# says plainly when nothing changed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRESET_NAME="${1:-}"
ENV_FILE="$REPO_ROOT/.env"

if [[ -z "$PRESET_NAME" ]]; then
    printf 'usage: apply_preset.sh <name>\n\navailable:\n' >&2
    for f in "$REPO_ROOT"/presets/*.env; do
        [[ -e "$f" ]] || continue
        printf '  %s\n' "$(basename "$f" .env)" >&2
    done
    exit 1
fi
PRESET="$REPO_ROOT/presets/$PRESET_NAME.env"
[[ -f "$PRESET" ]] || { printf 'no such preset: %s\n' "$PRESET" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { printf '.env not found — run `make bootstrap` first\n' >&2; exit 1; }

BACKUP="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BACKUP"

changed=0
while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// }" ]] && continue
    key="${line%%=*}"
    [[ "$key" == "$line" ]] && continue
    current="$(grep -E "^${key}=" "$ENV_FILE" | head -1 || true)"
    if [[ "$current" == "$line" ]]; then
        continue
    fi
    if [[ -n "$current" ]]; then
        # python for the substitution: the values contain JSON, colons and
        # slashes, which sed would need escaping gymnastics for.
        KEY="$key" VAL="$line" python3 - "$ENV_FILE" <<'PY'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); key = os.environ["KEY"]; val = os.environ["VAL"]
lines = p.read_text().splitlines(keepends=True)
for i, l in enumerate(lines):
    if l.startswith(key + "="):
        lines[i] = val + "\n"
        break
p.write_text("".join(lines))
PY
        printf '  ~ %s\n' "$key"
    else
        printf '%s\n' "$line" >> "$ENV_FILE"
        printf '  + %s\n' "$key"
    fi
    changed=$((changed + 1))
done < "$PRESET"

if [[ $changed -eq 0 ]]; then
    rm -f "$BACKUP"
    printf '\033[1;32m✓ .env already matches preset "%s" — nothing to do\033[0m\n' "$PRESET_NAME"
    exit 0
fi
printf '\033[1;32m✓ applied %d key(s) from "%s" (backup: %s)\033[0m\n' \
    "$changed" "$PRESET_NAME" "$(basename "$BACKUP")"
printf '  restart to pick them up:  docker compose up -d scaffold-orchestrator\n'
