#!/usr/bin/env bash
# Scaffold Engine — design_circuit end-to-end smoke (§17.156)
#
# Drives the engineering-design pipeline through every persisted stage
# for one canonical spec, then prints the final report header. Use this
# to certify the four engineering tables (specs, topology_selections,
# device_sizings | digital_sizings) write rows under a real /design POST
# rather than via direct stage calls (§17.155's path).
#
# Usage:
#   bash scripts/smoke_design_pipeline.sh --kind analog
#   bash scripts/smoke_design_pipeline.sh --kind digital
#
# Env overrides:
#   SCAFFOLD_URL    — orchestrator base URL (default: http://localhost:8000)
#   SCAFFOLD_API_KEY — overrides the key parsed from .env
#   BRIEF           — custom natural-language brief (skips canonical default)
#   SIZE_TIMEOUT    — curl timeout for the sizing call in seconds (default: 600)
#
# Exits non-zero on any HTTP error, ambiguity response, or non-converged
# sizing. All intermediate JSON is echoed so failures are diagnosable
# from the transcript alone.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

KIND=""
for arg in "$@"; do
    case "$arg" in
        --kind=*) KIND="${arg#*=}" ;;
        --kind) shift; KIND="${1:-}" ;;
        --help|-h)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
    esac
done

if [[ "$KIND" != "analog" && "$KIND" != "digital" ]]; then
    echo "ERROR: --kind must be 'analog' or 'digital' (got: ${KIND:-<empty>})" >&2
    exit 2
fi

SCAFFOLD_URL="${SCAFFOLD_URL:-http://localhost:8000}"
SIZE_TIMEOUT="${SIZE_TIMEOUT:-600}"

if [[ -z "${SCAFFOLD_API_KEY:-}" ]]; then
    if [[ -f "$ENV_FILE" ]]; then
        SCAFFOLD_API_KEY="$(grep -E '^SCAFFOLD_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    fi
fi
if [[ -z "${SCAFFOLD_API_KEY:-}" ]]; then
    echo "ERROR: SCAFFOLD_API_KEY not set and not parseable from $ENV_FILE" >&2
    exit 2
fi

# Canonical briefs — content matches the seed corpus entries §17.155
# used for live convergence. Override with $BRIEF for custom runs.
if [[ "$KIND" == "analog" ]]; then
    DEFAULT_BRIEF="Design a first-order passive RC low-pass filter. Cutoff frequency 1 kHz +/- 5%. Source impedance 50 ohm, load impedance 10 kohm. Supply 3.3V. Passband insertion loss less than 1 dB at DC. Single-ended signal path. Topology: one resistor in series, one capacitor to ground."
else
    DEFAULT_BRIEF="Design a 4-bit synchronous binary counter in SystemVerilog. Clock frequency 100 MHz (10 ns period). Active-low synchronous reset reset_n. The counter must wrap from 4'b1111 back to 4'b0000 every 16 clock cycles. KPI constraint: wrap_count = 16. Inputs: clk, reset_n. Output: 4-bit count. Single clock domain."
fi
BRIEF="${BRIEF:-$DEFAULT_BRIEF}"

# ---------- helpers ----------
hdr() { printf '\n=== %s ===\n' "$*"; }

call() {
    # $1=METHOD $2=PATH $3=BODY ($4=timeout-override)
    local method="$1" path="$2" body="${3:-}" t="${4:-30}"
    local out http
    out="$(mktemp)"
    if [[ -n "$body" ]]; then
        http=$(curl -sS -o "$out" -w '%{http_code}' \
            --max-time "$t" \
            -X "$method" \
            -H "X-API-Key: $SCAFFOLD_API_KEY" \
            -H "Content-Type: application/json" \
            -d "$body" \
            "${SCAFFOLD_URL}${path}")
    else
        http=$(curl -sS -o "$out" -w '%{http_code}' \
            --max-time "$t" \
            -X "$method" \
            -H "X-API-Key: $SCAFFOLD_API_KEY" \
            "${SCAFFOLD_URL}${path}")
    fi
    printf 'HTTP %s\n' "$http"
    if command -v jq >/dev/null 2>&1; then
        jq . < "$out" 2>/dev/null || cat "$out"
    else
        cat "$out"
    fi
    echo
    LAST_BODY="$(cat "$out")"
    LAST_HTTP="$http"
    rm -f "$out"
}

require_2xx() {
    if [[ "$LAST_HTTP" -lt 200 || "$LAST_HTTP" -ge 300 ]]; then
        echo "FAIL: expected 2xx, got $LAST_HTTP at step '$1'" >&2
        exit 1
    fi
}

extract() {
    # $1=jq filter
    jq -r "$1" <<<"$LAST_BODY"
}

# ---------- 1. POST /design ----------
hdr "1. POST /design (kind=$KIND)"
echo "brief: $BRIEF"
echo
BODY_JSON="$(jq -nc --arg b "$BRIEF" '{brief:$b}')"
call POST /design "$BODY_JSON" 120
require_2xx "POST /design"

AMBIG_COUNT="$(extract '.ambiguities | length')"
ERR_COUNT="$(extract '.errors | length')"
if [[ "$AMBIG_COUNT" != "0" ]]; then
    echo "FAIL: extractor returned $AMBIG_COUNT ambiguity questions — refine the brief and retry." >&2
    exit 1
fi
if [[ "$ERR_COUNT" != "0" ]]; then
    echo "FAIL: extractor returned errors — see above." >&2
    exit 1
fi

JOB_ID="$(extract '.job_id')"
SPEC_ID="$(extract '.spec_id')"
echo "job_id=$JOB_ID  spec_id=$SPEC_ID"

# ---------- 2. POST /specs/{spec_id}/confirm ----------
hdr "2. POST /specs/$SPEC_ID/confirm"
call POST "/specs/$SPEC_ID/confirm" "" 30
require_2xx "POST /specs/{spec_id}/confirm"

# ---------- 3. POST /specs/{spec_id}/topology-select ----------
hdr "3. POST /specs/$SPEC_ID/topology-select"
call POST "/specs/$SPEC_ID/topology-select" "" 180
require_2xx "POST /specs/{spec_id}/topology-select"
SEL_ID="$(extract '.id')"
CAND_COUNT="$(extract '.candidates | length')"
echo "topology_selection_id=$SEL_ID  candidates=$CAND_COUNT"

# ---------- 4. POST /topology-selections/{sel_id}/size ----------
hdr "4. POST /topology-selections/$SEL_ID/size  (timeout=${SIZE_TIMEOUT}s)"
call POST "/topology-selections/$SEL_ID/size?candidate_idx=0" "" "$SIZE_TIMEOUT"
require_2xx "POST /topology-selections/{sel_id}/size"
SIZING_ID="$(extract '.id')"
CONVERGED="$(extract '.converged')"
ITERS="$(extract '.iterations')"
echo "sizing_id=$SIZING_ID  converged=$CONVERGED  iterations=$ITERS"
if [[ "$CONVERGED" != "true" ]]; then
    echo "WARN: sizing did NOT converge — report stage will still run on the persisted attempt." >&2
fi

# ---------- 5. GET report ----------
if [[ "$KIND" == "analog" ]]; then
    REPORT_PATH="/device-sizings/$SIZING_ID/report"
else
    REPORT_PATH="/digital-sizings/$SIZING_ID/report"
fi
hdr "5. GET $REPORT_PATH"
call GET "$REPORT_PATH" "" 60
require_2xx "GET report"

# ---------- summary ----------
hdr "summary"
echo "kind:           $KIND"
echo "job_id:         $JOB_ID"
echo "spec_id:        $SPEC_ID"
echo "selection_id:   $SEL_ID"
echo "sizing_id:      $SIZING_ID"
echo "converged:      $CONVERGED"
echo "iterations:     $ITERS"
echo "report.kind:    $(extract '.kind')"
echo "report.design:  $(extract '.design_name')"

if [[ "$CONVERGED" == "true" ]]; then
    echo
    echo "PASS: $KIND design_circuit smoke run completed end-to-end."
    exit 0
else
    echo
    echo "PARTIAL: pipeline persisted all rows but sizing did not converge." >&2
    exit 3
fi
