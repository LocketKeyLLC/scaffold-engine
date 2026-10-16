#!/usr/bin/env bash
# §17.1618 — the engine deploys ITSELF onto a guest: run as root inside the guest, from a checkout of this repo.
#
#   bash scripts/deploy_on_guest.sh /root/scaffold-deploy.env
#
# The settings file holds plain KEY=VALUE lines (no secrets): the engine's .env keys to set (MODEL_*, *_PROVIDER,
# SCAFFOLD_LAN_ADDRESS, SCAFFOLD_LAN_ALLOW, …) plus these deploy-only keys:
#   OLLAMA_COMPOSE_DIR   a compose project that runs Ollama in this guest (e.g. /opt/ollama): it joins ai-network so
#                        the engine reaches it at 172.18.0.1:11434 exactly as on a host-installed Ollama
#   OLLAMA_CONTAINER     the container to `ollama pull` the local models into (default: ollama)
#   SCAFFOLD_LAN_UI      1 → also publish Open WebUI on SCAFFOLD_LAN_ADDRESS:3000 (docker-compose.lan-ui.yml)
#
# Secrets (API key, Postgres password, WebUI/pipelines keys) are generated HERE by bootstrap.sh and never printed.
# Idempotent: a second run keeps the .env secrets and re-applies the settings.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
SETTINGS=${1:?usage: deploy_on_guest.sh SETTINGS_FILE}
[ -f "$SETTINGS" ] || { echo "deploy: no settings file $SETTINGS" >&2; exit 2; }

get() { sed -n "s/^$1=//p" "$SETTINGS" | tail -n 1; }
setkv() {   # set KEY=VALUE in .env, replacing an existing line
    local k=$1 v=$2
    if grep -q "^$k=" .env; then
        python3 - "$k" "$v" <<'PY'
import sys
k, v = sys.argv[1], sys.argv[2]
lines = open(".env").read().splitlines()
open(".env", "w").write("\n".join((f"{k}={v}" if l.startswith(k + "=") else l) for l in lines) + "\n")
PY
    else
        printf '%s=%s\n' "$k" "$v" >> .env
    fi
}

echo "== 1. ai-network (172.18.0.0/16) and the guest's Ollama on it"
OLLAMA_DIR=$(get OLLAMA_COMPOSE_DIR)
if ! docker network inspect ai-network >/dev/null 2>&1; then
    # a compose project's default network may hold 172.18.0.0/16: take it down first so the pin can be made
    if [ -n "$OLLAMA_DIR" ] && [ -f "$OLLAMA_DIR/docker-compose.yml" ]; then (cd "$OLLAMA_DIR" && docker compose down); fi
    docker network create --driver bridge --subnet 172.18.0.0/16 --gateway 172.18.0.1 ai-network >/dev/null
    echo "created ai-network"
fi
if [ -n "$OLLAMA_DIR" ] && [ -f "$OLLAMA_DIR/docker-compose.yml" ]; then
    if [ ! -f "$OLLAMA_DIR/docker-compose.override.yml" ]; then
        printf 'networks:\n  default:\n    name: ai-network\n    external: true\n' > "$OLLAMA_DIR/docker-compose.override.yml"
    fi
    (cd "$OLLAMA_DIR" && docker compose up -d)
fi

echo "== 2. bootstrap (fresh secrets, volumes, first build + start)"
if [ ! -f .env ]; then
    bash scripts/bootstrap.sh --yes
else
    echo ".env present: keeping its secrets"
fi

echo "== 3. this guest's settings"
while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    k=${line%%=*}; v=${line#*=}
    case "$k" in OLLAMA_COMPOSE_DIR|OLLAMA_CONTAINER|SCAFFOLD_LAN_UI) continue ;; esac
    [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "deploy: skipping a malformed line"; continue; }
    setkv "$k" "$v"
done < "$SETTINGS"
files="docker-compose.yml"
if [ -n "$(get SCAFFOLD_LAN_ADDRESS)" ]; then files="$files:docker-compose.lan.yml"; fi
if [ "$(get SCAFFOLD_LAN_UI)" = "1" ]; then files="$files:docker-compose.lan-ui.yml"; setkv SCAFFOLD_LAN_UI 1; fi
setkv COMPOSE_FILE "$files"
echo "COMPOSE_FILE=$files"

echo "== 4. LAN gate (before the ports are published)"
if [ -n "$(get SCAFFOLD_LAN_ADDRESS)" ]; then
    UNIT_DIR=${SCAFFOLD_SYSTEMD_DIR:-/etc/systemd/system}
    sed "s#/home/youruser/scaffold-engine#$REPO#g" scripts/scaffold-lan-gate.service > "$UNIT_DIR/scaffold-lan-gate.service"
    systemctl daemon-reload
    systemctl enable scaffold-lan-gate >/dev/null 2>&1 || true
    bash scripts/scaffold_lan_gate.sh "$REPO/.env"
fi

echo "== 5. local models into Ollama"
OC=$(get OLLAMA_CONTAINER); OC=${OC:-ollama}
if docker inspect "$OC" >/dev/null 2>&1; then
    for m in $(sed -n 's/^MODEL_[A-Z_]*=//p' .env | sort -u) nomic-embed-text; do
        case "$m" in *-cloud|*:cloud|''|cross-encoder/*) continue ;; esac
        if docker exec "$OC" ollama list | awk 'NR>1 {print $1}' | grep -qxF "$m"; then
            echo "present: $m"
        else
            docker exec "$OC" ollama pull "$m"
        fi
    done
fi

echo "== 6. build + start with this guest's settings"
docker compose up -d --build
for i in $(seq 1 60); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then echo "engine healthy"; break; fi
    sleep 5
done
curl -sf http://localhost:8000/health >/dev/null 2>&1 || { echo "deploy: /health did not answer" >&2; exit 1; }
docker compose ps --format '{{.Service}} {{.State}}'
