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

# §17.205 — opening banner so a new operator knows what's about to be
# probed before any section runs. Mirrors the section names declared
# below; needs a manual update when sections are added or renamed,
# but the cost is one line per change and the operator-facing clarity
# is worth it.
printf '%s┌── make doctor ──%s pre-flight diagnostic, 11 sections, read-only.%s\n' \
    "$C_INFO" "$C_RST" "$C_RST"
printf '%s│%s   1. .env                          (required secrets present)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   2. Docker network + volumes      (ai-network, postgres + milvus data)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   3. Containers                    (all 7 services running)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   4. Orchestrator /health          (per-subsystem latencies)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   5. Ollama (host)                 (CPU model registry reachable)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   6. OpenAI provider               (cloud key configured if used)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   7. API key sync                  (.env ↔ container env ↔ bashrc ↔ valves.json)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   8. Auth posture                  (gate enabled / SCAFFOLD_AUTH_DISABLED honored)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s   9. Schema migrations             (highest applied vs db/migrations/)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s  10. Cold-backup mount guard       (no /mnt/adamssd in compose/.env/volumes — §17.213)\n' \
    "$C_INFO" "$C_RST"
printf '%s│%s  11. API key 6-surface sync        (read-side: .env + 5x valves.json + bashrc + 2 containers)\n' \
    "$C_INFO" "$C_RST"
printf '%s└──%s pass --explain to see what each section verifies inline.\n\n' \
    "$C_INFO" "$C_RST"

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

# ---- 7b. Auth posture (§17.96) --------------------------------------
hdr "Auth posture"
explain "SCAFFOLD_AUTH_DISABLED=true bypasses the X-API-Key gate entirely — every endpoint is reachable without a key. The orchestrator surfaces this in /health.auth_enabled so misconfiguration is visible to operators without grepping boot logs. RED if disabled."

if docker ps --format '{{.Names}}' | grep -qx scaffold-orchestrator; then
    AUTH_ENABLED="$(curl -sS --max-time 5 http://localhost:8000/health 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("auth_enabled"))
except Exception: print("")' 2>/dev/null || true)"
    case "$AUTH_ENABLED" in
        True)  pass "API key gate is in force (auth_enabled=true)" ;;
        False) fail "AUTH DISABLED — every endpoint is reachable without an X-API-Key. Set SCAFFOLD_AUTH_DISABLED=false (or unset it) in .env and restart compose." ;;
        *)     warn "could not read /health.auth_enabled — orchestrator may not be ready, or running a pre-§17.96 image" ;;
    esac
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

# ---- 9. Cold-backup mount guard (§17.215 candidate) ------------------
# Repo moved to internal NVMe in §17.214 after the AM8180 USB-NVMe
# enclosure hung the host under write pressure (§17.213). /mnt/adamssd
# is demoted to cold-backup-only; ANY runtime reference to it from
# compose, .env, or a docker volume re-creates that crash risk class.
# This guard fires LOUD so a stray bind-mount can't sneak back in
# unnoticed during a future edit.
hdr "Cold-backup mount guard"
explain "Scans every compose file, every .env*, and every named docker volume for paths under /mnt/adamssd/. Post-§17.214 that path is cold-backup-only — the AM8180 USB-NVMe enclosure that mounts there hangs the host under sustained write load (§17.213). A regression that re-introduces it would silently re-arm the enclosure-crash failure mode."

COLD_PATH="/mnt/adamssd"
COLD_HITS=0

# Compose files
shopt -s nullglob
for cf in "$REPO_ROOT"/docker-compose*.yml; do
    if grep -nE "${COLD_PATH}" "$cf" >/dev/null 2>&1; then
        # Print every offending line with file:line for fast triage
        while IFS=: read -r lineno line; do
            fail "${cf#$REPO_ROOT/}:$lineno references $COLD_PATH — $(echo "$line" | sed 's/^[[:space:]]*//' | head -c 120)"
            COLD_HITS=$((COLD_HITS+1))
        done < <(grep -nE "${COLD_PATH}" "$cf")
    fi
done

# .env files (.env, .env.example, .env.local, …)
for ef in "$REPO_ROOT"/.env*; do
    [[ -f "$ef" ]] || continue
    if grep -nE "${COLD_PATH}" "$ef" >/dev/null 2>&1; then
        while IFS=: read -r lineno line; do
            fail "${ef#$REPO_ROOT/}:$lineno references $COLD_PATH — $(echo "$line" | sed 's/^[[:space:]]*//' | head -c 120)"
            COLD_HITS=$((COLD_HITS+1))
        done < <(grep -nE "${COLD_PATH}" "$ef")
    fi
done
shopt -u nullglob

# Docker named volumes — inspect Mountpoint + Options.device of every
# volume. A volume created with `--opt device=/mnt/adamssd/...` is the
# stealth-regression shape: nothing in the repo references the path,
# but the running stack still binds it.
if command -v docker >/dev/null 2>&1; then
    while IFS= read -r vol; do
        [[ -z "$vol" ]] && continue
        # -f templating keeps this fast (~5ms per volume) vs jq.
        offenders="$(docker volume inspect -f '{{.Mountpoint}}{{"\n"}}{{range $k,$v := .Options}}{{$v}}{{"\n"}}{{end}}' "$vol" 2>/dev/null | grep -F "$COLD_PATH" || true)"
        if [[ -n "$offenders" ]]; then
            fail "docker volume '$vol' binds $COLD_PATH ($(echo "$offenders" | tr '\n' ' ' | head -c 120))"
            COLD_HITS=$((COLD_HITS+1))
        fi
    done < <(docker volume ls --format '{{.Name}}' 2>/dev/null)
fi

if [[ $COLD_HITS -eq 0 ]]; then
    pass "no $COLD_PATH references in compose / .env* / docker volumes"
else
    printf '  %s┃%s %sREGRESSION:%s %d reference(s) to %s found — post-§17.214 this path is cold-backup-only.\n' \
        "$C_ERR" "$C_RST" "$C_ERR" "$C_RST" "$COLD_HITS" "$COLD_PATH"
    printf '  %s┃%s Restore by moving the offending mount to internal NVMe (~/scaffold-engine) and recreating the affected service or volume.\n' \
        "$C_ERR" "$C_RST"
fi

# ---- 10. API-key 6-surface read-side sync (§17.35 follow-up) ---------
# `make sync-api-key` is the write path; this is the read-side guard
# that catches drift after a one-off manual edit, a partial sync, or
# a container that wasn't restarted post-rotation. Loud-fails on any
# of the six surfaces disagreeing with .env.
hdr "API key 6-surface sync (read-side)"
explain "Reads SCAFFOLD_API_KEY from all six places it must agree on: .env, every pipelines/*/valves.json, ~/.bashrc, the scaffold-orchestrator container env, and the open-webui-pipelines container env. Section 7 above only covers .env↔orchestrator; this section covers the full §17.35 surface so a stale valves.json or unsourced bashrc shows up here, not as a mysterious 401 mid-job."

if [[ ! -f "$ENV_FILE" ]]; then
    warn ".env missing — cannot establish reference value for 6-surface sync"
else
    REF_KEY="$(grep -E '^SCAFFOLD_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [[ -z "$REF_KEY" ]]; then
        warn "SCAFFOLD_API_KEY empty in .env — cannot reference-check downstream surfaces"
    else
        # Render a short fingerprint for log readability (full key never printed)
        ref_fp="${REF_KEY:0:11}…${REF_KEY: -4}"
        info "reference (.env): $ref_fp"
        SYNC_MISMATCH=0

        # (a) pipelines/*/valves.json — every Pipeline valves file. We
        # only check files that declare an `api_key` field; vendor
        # subdirs like pipelines/_next_actions and pipelines/_sse_events
        # ship `{}` valves.json (no Pipeline class, no api_key surface)
        # and aren't part of the 5-place sync invariant.
        shopt -s nullglob
        for valves in "$REPO_ROOT"/pipelines/*/valves.json; do
            name="$(basename "$(dirname "$valves")")"
            # has_key returns "PRESENT" or "ABSENT"; vkey is "" when absent.
            read -r has_key vkey < <(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("ABSENT", "")
    sys.exit(0)
if "api_key" in data:
    print("PRESENT", data.get("api_key", ""))
else:
    print("ABSENT", "")
' "$valves" 2>/dev/null)
            if [[ "$has_key" != "PRESENT" ]]; then
                # Vendor / non-Pipeline valves — silently skip.
                continue
            fi
            if [[ -z "$vkey" ]]; then
                fail "pipelines/$name/valves.json — api_key empty (run: make sync-api-key)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            elif [[ "$vkey" != "$REF_KEY" ]]; then
                fail "pipelines/$name/valves.json — api_key drift (${vkey:0:11}…${vkey: -4} ≠ $ref_fp)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            else
                pass "pipelines/$name/valves.json matches .env"
            fi
        done
        shopt -u nullglob

        # (b) ~/.bashrc — operator-shell surface. We grep verbatim;
        # `source ~/.bashrc` is the operator's responsibility, but the
        # written value must match so a fresh shell picks the right key.
        BASHRC="${HOME}/.bashrc"
        if [[ ! -f "$BASHRC" ]]; then
            warn "~/.bashrc not found — operator-shell surface unverifiable"
        else
            bkey="$(grep -E '^export SCAFFOLD_API_KEY=' "$BASHRC" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
            if [[ -z "$bkey" ]]; then
                fail "~/.bashrc — no 'export SCAFFOLD_API_KEY=' line (run: make sync-api-key)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            elif [[ "$bkey" != "$REF_KEY" ]]; then
                fail "~/.bashrc — api_key drift (${bkey:0:11}…${bkey: -4} ≠ $ref_fp)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            else
                pass "~/.bashrc matches .env"
            fi
        fi

        # (c) scaffold-orchestrator container env. Section 7 above also
        # checks this; repeating here keeps the 6-surface report
        # self-contained and lets `make doctor | grep -A1 '6-surface'`
        # show the full picture in one block.
        if docker ps --format '{{.Names}}' | grep -qx scaffold-orchestrator; then
            okey="$(docker exec scaffold-orchestrator printenv SCAFFOLD_API_KEY 2>/dev/null || true)"
            if [[ -z "$okey" ]]; then
                fail "scaffold-orchestrator container — SCAFFOLD_API_KEY unset"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            elif [[ "$okey" != "$REF_KEY" ]]; then
                fail "scaffold-orchestrator container — api_key drift (${okey:0:11}…${okey: -4} ≠ $ref_fp; restart compose)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            else
                pass "scaffold-orchestrator container matches .env"
            fi
        else
            warn "scaffold-orchestrator container not running — env unverifiable"
        fi

        # (d) open-webui-pipelines container env. OWUI pipelines read
        # the key when they call the orchestrator over the bridge; drift
        # here surfaces as 401s on /ideate, /research, /gt, /optimize.
        if docker ps --format '{{.Names}}' | grep -qx open-webui-pipelines; then
            pkey="$(docker exec open-webui-pipelines printenv SCAFFOLD_API_KEY 2>/dev/null || true)"
            if [[ -z "$pkey" ]]; then
                fail "open-webui-pipelines container — SCAFFOLD_API_KEY unset"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            elif [[ "$pkey" != "$REF_KEY" ]]; then
                fail "open-webui-pipelines container — api_key drift (${pkey:0:11}…${pkey: -4} ≠ $ref_fp; restart compose)"
                SYNC_MISMATCH=$((SYNC_MISMATCH+1))
            else
                pass "open-webui-pipelines container matches .env"
            fi
        else
            warn "open-webui-pipelines container not running — env unverifiable"
        fi

        if [[ $SYNC_MISMATCH -eq 0 ]]; then
            pass "all 6 surfaces agree on SCAFFOLD_API_KEY ($ref_fp)"
        else
            printf '  %s┃%s %sDRIFT:%s %d surface(s) disagree with .env. Fix: %smake sync-api-key%s (no arg → propagate .env value).\n' \
                "$C_ERR" "$C_RST" "$C_ERR" "$C_RST" "$SYNC_MISMATCH" "$C_INFO" "$C_RST"
        fi
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
