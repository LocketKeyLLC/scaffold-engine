#!/usr/bin/env bash
# One-time migration after the X.28 non-root hardening.
#
# Before X.28 the orchestrator ran as root inside the container, so the two
# orchestrator-managed Docker volumes (scaffold-engine_hf-cache and
# scaffold-engine_scaffold-logs) were created with root:root ownership.
# Post-X.28 the container runs as scaffold (UID/GID 10001) and cannot
# write into those volumes until they are chowned.
#
# Run this exactly once, against a STOPPED orchestrator, before the first
# non-root deploy. It is idempotent — safe to re-run.
#
#   docker compose stop scaffold-orchestrator
#   bash scripts/chown_named_volumes.sh
#   docker compose up -d scaffold-orchestrator
#
# Implementation: a throwaway alpine container is launched as root with
# both volumes mounted; it chowns recursively to 10001:10001 and exits.

set -euo pipefail

VOLUMES=(
    scaffold-engine_hf-cache
    scaffold-engine_scaffold-logs
)

for v in "${VOLUMES[@]}"; do
    if ! docker volume inspect "$v" >/dev/null 2>&1; then
        echo "[chown_named_volumes] $v does not exist yet — will be created with correct ownership on first up. Skipping." >&2
        continue
    fi
    echo "[chown_named_volumes] chown 10001:10001 -R $v"
    docker run --rm \
        --user 0:0 \
        -v "$v:/target" \
        alpine:3 \
        chown -R 10001:10001 /target
done

echo "[chown_named_volumes] done. The orchestrator can now be started as scaffold (10001)."
