#!/usr/bin/env bash
# Scaffold Engine — wipe baked-in API keys from valves.json (Sprint G.3)
#
# After this script runs, every pipelines/<name>/valves.json has an empty
# api_key field. Pipelines fall through to $SCAFFOLD_API_KEY (env) at boot.
# Combined with SCAFFOLD_VALVES_ENV_OVERRIDE=true in .env, this makes .env
# the single visible source of truth — no more 5-place key sync.
#
# Idempotent: re-running on already-empty valves is a no-op.
#
# Run from repo root:  bash scripts/sync_valves.sh   (or: make sync-valves)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'; C_INFO=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_INFO=""; C_DIM=""; C_RST=""
fi

WIPED=0
NOOP=0

for valves in "$REPO_ROOT"/pipelines/*/valves.json; do
    name="$(basename "$(dirname "$valves")")"
    result="$(python3 - <<PY
import json
path = "$valves"
with open(path) as f:
    data = json.load(f)
if data.get("api_key", "") != "":
    data["api_key"] = ""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    import os
    os.replace(tmp, path)
    print("WIPED")
else:
    print("NOOP")
PY
)"
    case "$result" in
        WIPED) printf '  %s✓%s wiped api_key in pipelines/%s/valves.json\n' "$C_OK" "$C_RST" "$name"; WIPED=$((WIPED+1)) ;;
        NOOP)  printf '  %s-%s pipelines/%s/valves.json — api_key already empty\n' "$C_DIM" "$C_RST" "$name"; NOOP=$((NOOP+1)) ;;
    esac
done

echo
printf '%sDone.%s wiped=%d, already-empty=%d\n' "$C_INFO" "$C_RST" "$WIPED" "$NOOP"
echo
echo "Pipelines now resolve api_key from \$SCAFFOLD_API_KEY (env)."
echo "  - Verify:  make doctor"
echo "  - Apply :  make restart   (or: docker compose up -d)"
