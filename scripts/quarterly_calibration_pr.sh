#!/usr/bin/env bash
# Scaffold Engine — quarterly RAG calibration kickoff (local cron).
# Tier-2 audit-tail items #14 + #15.
#
# Replaces the disabled cloud routine (trig_012iZLgbuPuTp9hLHYncWrkJ) so
# the calibration sweep runs in the operator's local laptop context where
# git/gh auth is already wired up. Opens a draft PR with prior-baseline
# context, a runbook checklist, and a placeholder rebaseline entry; the
# operator runs the actual /score_retrieval calibration locally and fills
# in the real numbers before marking the PR ready.
#
# Crontab entry (8am UTC = 4am EST/EDT, 1st of Jan/Apr/Jul/Oct):
#   0 8 1 1,4,7,10 *  /home/aedefruscio/scaffold-engine/scripts/quarterly_calibration_pr.sh >> /tmp/quarterly_calibration.log 2>&1
# §17.357 — path corrected post-§17.214 NVMe migration. The /mnt/adamssd
# path is gone (AM8180 enclosure demoted to cold-backup-only). cron has
# no ~ expansion contract — must be absolute. Next fire: 2026-07-01
# 08:00 UTC (verified via `crontab -l`).
#
# Run manually any time to test:
#   bash scripts/quarterly_calibration_pr.sh

set -euo pipefail

# Resolve repo root from this script's location so the cron entry can
# point straight at the script without needing `cd` first.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── X.26: emit alerts on start / success / failure ──────────────────
# Routes through the orchestrator container's CLI so the alert lands in
# system_alerts (DB) and the configured file sink. If the orchestrator
# container is down, the alert is logged-only and the script continues —
# we never want alerting itself to break the calibration pipeline.
ALERT_CONTAINER="${SCAFFOLD_ALERT_CONTAINER:-scaffold-orchestrator}"

emit_alert() {
    # $1=kind  $2=severity  $3=message  $4=payload (JSON, optional)
    local kind="$1" severity="$2" message="$3" payload="${4:-{}}"
    if ! command -v docker >/dev/null 2>&1; then
        echo "[calibration] docker missing; skipping alert kind=${kind}" >&2
        return 0
    fi
    docker exec "$ALERT_CONTAINER" python -m app.observability.alerts emit \
        --kind "$kind" --severity "$severity" --message "$message" \
        --payload "$payload" >/dev/null 2>&1 \
        || echo "[calibration] alert emit failed (kind=${kind}); continuing" >&2
}

# ERR trap: any failure between here and the success leg fires
# `calibration.failed`. ${BASH_COMMAND} pinpoints the failing step in
# the alert payload so the operator can jump straight to it.
on_err() {
    local rc=$?
    local cmd="${BASH_COMMAND:-unknown}"
    local payload
    payload=$(printf '{"exit_code":%d,"failing_command":%s}' "$rc" \
              "$(printf '%s' "$cmd" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")
    emit_alert "calibration.failed" "critical" \
        "Quarterly calibration script exited ${rc} on: ${cmd}" \
        "$payload"
    exit "$rc"
}
trap on_err ERR

# ── 0. Pre-flight checks ────────────────────────────────────────────────────

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not on PATH. Install it (https://cli.github.com/) then retry." >&2
    exit 2
fi
if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh CLI is not authenticated. Run: gh auth login" >&2
    exit 2
fi

# Refuse to run with a dirty working tree — the script is supposed to land
# a clean placeholder commit, not bundle in whatever else is in flight.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree has uncommitted changes. Commit or stash before running." >&2
    exit 2
fi

# ── 1. Compute YYYY-Q[N] label from today's UTC date ───────────────────────

YEAR=$(date -u +%Y)
MONTH=$(date -u +%-m)            # %-m = no leading zero
QUARTER=$(( (MONTH - 1) / 3 + 1 ))
LABEL="${YEAR}-Q${QUARTER}"
TS_TODAY=$(date -u +%Y-%m-%d)

echo "[calibration] kicking off ${LABEL} (${TS_TODAY} UTC)"
emit_alert "calibration.started" "info" \
    "Quarterly calibration ${LABEL} kicking off (${TS_TODAY} UTC)" \
    "$(printf '{"label":"%s","date":"%s"}' "$LABEL" "$TS_TODAY")"

# ── 2. Sync main + branch ───────────────────────────────────────────────────

git fetch origin main --quiet

BRANCH="calibration/${LABEL}"
# If the branch already exists locally OR on the remote, append -rerun-N.
if git show-ref --quiet "refs/heads/${BRANCH}" \
   || git ls-remote --exit-code --heads origin "${BRANCH}" >/dev/null 2>&1; then
    SUFFIX=2
    while git show-ref --quiet "refs/heads/${BRANCH}-rerun${SUFFIX}" \
          || git ls-remote --exit-code --heads origin "${BRANCH}-rerun${SUFFIX}" >/dev/null 2>&1; do
        SUFFIX=$((SUFFIX + 1))
    done
    BRANCH="${BRANCH}-rerun${SUFFIX}"
    echo "[calibration] base branch existed; using ${BRANCH}"
fi
git checkout -b "${BRANCH}" origin/main --quiet

# ── 3. Read prior baseline + append placeholder via Python ─────────────────

PRIOR_JSON=$(python3 - <<'PY'
"""Read tests/fixtures/golden_set.json, extract the latest rebaseline entry,
append a placeholder for the operator to fill in, and write back. Print the
prior-baseline JSON to stdout so the bash side can stuff it into the PR body.
"""
import json, os, sys, datetime

PATH = "tests/fixtures/golden_set.json"
TODAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

with open(PATH) as f:
    data = json.load(f)

placeholder = {
    "kb_size": "TBD — fill in from psql or /health",
    "ts": TODAY,
    "harness": "HTTP /rag (operator local — placeholder)",
    "metrics": {
        "coverage": 0,
        "mean_recall_at_5": 0,
        "mean_recall_at_10": 0,
        "mean_mrr": 0,
    },
    "note": "Quarterly calibration scaffolding — operator to fill in actual "
            "metrics from local /score_retrieval run.",
}

prior = None
existing = data.get("rebaseline")
if existing is None:
    data["rebaseline"] = [placeholder]
elif isinstance(existing, list):
    if existing:
        prior = existing[-1]
    existing.append(placeholder)
elif isinstance(existing, dict):
    prior = existing
    data["rebaseline"] = [existing, placeholder]
else:
    print(f"ERROR: unexpected rebaseline shape: {type(existing).__name__}", file=sys.stderr)
    sys.exit(2)

with open(PATH, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

# Surface the prior baseline as JSON on stdout for the PR body builder.
print(json.dumps(prior or {}))
PY
)

# ── 4. Build PR body (Markdown) ────────────────────────────────────────────

# Extract prior values via jq (already a dependency of the dev env). Empty
# string when the field is absent; the body template handles that.
prior_field() {
    if [[ -z "$PRIOR_JSON" || "$PRIOR_JSON" == "{}" ]]; then
        echo "—"
        return
    fi
    jq -r --arg path "$1" '
        if has($path) then (.[$path] | tostring)
        elif (.metrics // {}) | has($path) then (.metrics[$path] | tostring)
        else "—"
        end
    ' <<<"$PRIOR_JSON"
}

PRIOR_KB=$(prior_field "kb_size")
PRIOR_COVERAGE=$(prior_field "coverage")
PRIOR_R5=$(prior_field "mean_recall_at_5")
PRIOR_R10=$(prior_field "mean_recall_at_10")
PRIOR_MRR=$(prior_field "mean_mrr")
PRIOR_TS=$(prior_field "ts")
PRIOR_HARNESS=$(prior_field "harness")

PR_BODY_FILE=$(mktemp -t quarterly-calibration-body.XXXXXX.md)
trap 'rm -f "$PR_BODY_FILE"' EXIT

cat > "$PR_BODY_FILE" <<MD
**Quarterly RAG calibration — ${LABEL}**

Tier-2 audit-tail items **#14** (quarterly re-baseline cadence) and **#15** (\`tests/ground_truth.json\` regen review). This is a draft PR; the operator runs \`/score_retrieval\` locally and fills in the placeholder \`rebaseline\` entry that's been added to \`tests/fixtures/golden_set.json\`.

## Prior baseline

| KB size | Coverage | Recall@5 | Recall@10 | MRR | ts | harness |
|---|---|---|---|---|---|---|
| ${PRIOR_KB} | ${PRIOR_COVERAGE} | ${PRIOR_R5} | ${PRIOR_R10} | ${PRIOR_MRR} | ${PRIOR_TS} | ${PRIOR_HARNESS} |

## Runbook (operator)

- [ ] Restart orchestrator container so any post-prior-baseline migrations apply (\`make restart\`)
- [ ] Run \`make eval\` (40-query comprehensive suite via \`tests/eval_retrieval.py\`)
- [ ] Run the W.8-style HTTP harness against \`/rag\` for the live 20-query golden_set
- [ ] Compare metrics against the prior baseline; flag any drift > 5pt
- [ ] If recall has dropped, investigate per-query MISSes; check ground_truth.json staleness (#15)
- [ ] Update the placeholder \`rebaseline\` block in \`tests/fixtures/golden_set.json\` with real numbers
- [ ] If a query has drifted to MISS, decide: regenerate ground_truth.json entry (#15) or accept as legitimate drift
- [ ] Update \`OVERVIEW.md §18\`'s retrieval-quality baseline table with the new row
- [ ] Mark this PR ready-for-review

## Drift signals to watch

- **Embedder model swap** — rare; \`MODEL_EMBEDDER_PIPELINE\` is locked + a Milvus collection rebuild is required.
- **Reranker model swap** — also locked; \`MODEL_RERANKER\` singleton.
- **Partition-key changes** — domain-filter logic lives in \`rag_pipeline._iter_search_domains\`.
- **KB-shape changes** — new ingestion runs can shift entry IDs (W.8 surfaced this; ground_truth.json drifted).
- **Threshold-cluster tuning** — X.1 lowered \`node_orphan_threshold_minutes\` and \`awaiting_confirmation_stale_minutes\`; further tuning could shift recall numbers.
- **Synthesis changes** — W.7 + X.6 added per-job synthesis override; rebaselines should record whether synthesis was on.

## What this PR does NOT do

This script (\`scripts/quarterly_calibration_pr.sh\`) only scaffolds the PR. The actual \`/score_retrieval\` execution + rebaseline numbers are still the operator's responsibility — they need the local Postgres + Milvus + Ollama stack.

🤖 Generated quarterly by \`scripts/quarterly_calibration_pr.sh\` (cron).
MD

# ── 5. Commit + push + PR ──────────────────────────────────────────────────

git add tests/fixtures/golden_set.json
git commit -m "chore(calibration): scaffold ${LABEL} quarterly RAG re-baseline

Tier-2 audit-tail items #14 + #15. Placeholder rebaseline entry added
to tests/fixtures/golden_set.json; operator fills in real metrics from
local /score_retrieval run before marking the PR ready.

Generated by scripts/quarterly_calibration_pr.sh." \
  --quiet

git push -u origin "${BRANCH}" --quiet

PR_URL=$(gh pr create --draft \
    --title "Quarterly RAG calibration — ${LABEL}" \
    --body-file "$PR_BODY_FILE" \
    --base main)

echo "[calibration] done. branch=${BRANCH} commit=$(git rev-parse --short HEAD)"
echo "[calibration] PR: ${PR_URL}"

# Disarm the ERR trap before emitting the success alert so an alert
# emit failure here can't escalate into a spurious 'failed' alert.
trap - ERR
emit_alert "calibration.ok" "info" \
    "Quarterly calibration ${LABEL} completed; PR opened" \
    "$(printf '{"label":"%s","branch":"%s","pr_url":"%s"}' "$LABEL" "$BRANCH" "$PR_URL")"
