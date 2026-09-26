#!/usr/bin/env bash
# §17.1180c — register a GitHub Actions self-hosted runner on the engine host.
#
# READ THIS BEFORE RUNNING IT.
#
# This repository is PUBLIC. GitHub's guidance is not to attach self-hosted
# runners to public repositories, because a fork can open a pull request whose
# workflow then executes on the runner host. This host is the engine box: .env
# holds the database password and the provider API keys, the Docker socket is
# here, and this machine reaches the Proxmox host on the LAN.
#
# What makes it defensible:
#   1. NO self-hosted job is reachable from a `pull_request` workflow. Tier 2
#      lives in `integration.yml`, which has no pull_request trigger at all, and
#      goldens is schedule/dispatch only. That is structural, not an `if:`
#      clause, and `tests/test_infra_scaffolding.py` fails if it regresses.
#   2. Fork-PR approval is set to `all_external_contributors`.
#   3. The default GITHUB_TOKEN is read-only.
#   4. Jobs target the label `scaffold-engine-host`, not bare `self-hosted`.
#
# WHAT THIS DOES NOT GIVE YOU: an unprivileged runner. Both jobs that use it
# drive the system Docker daemon (`docker compose up --build`, and the
# throwaway dev container `make goldens` runs in), so the runner account must be
# in the `docker` group — which is root-equivalent on this machine. A dedicated
# account still keeps the runner out of your login user's files by default, but
# do not mistake it for a sandbox. If that is not acceptable, run
# `make goldens` by hand instead and do not install this.
#
# Usage:   sudo bash scripts/setup_github_runner.sh
#          sudo bash scripts/setup_github_runner.sh --uninstall
set -euo pipefail

REPO="${REPO:-LocketKeyLLC/scaffold-engine}"
RUNNER_USER="${RUNNER_USER:-gh-runner}"
RUNNER_HOME="${RUNNER_HOME:-/opt/gh-runner}"
RUNNER_LABEL="${RUNNER_LABEL:-scaffold-engine-host}"
RUNNER_VERSION="${RUNNER_VERSION:-2.330.0}"
RUNNER_SHA256="${RUNNER_SHA256:-}"   # set to pin; empty = verify against GitHub's published digest

die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[1;36m→ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run me with sudo"
command -v gh >/dev/null || die "the gh CLI is needed to mint a registration token"

# ── preflight: re-assert the security property AT INSTALL TIME ───────────────
# The tests assert this in CI; assert it again here, because this is the moment
# the consequence becomes real.
say "preflight: no self-hosted job may be reachable from a fork pull request"
python3 - "$RUNNER_LABEL" <<'PY' || die "preflight failed — do not install the runner"
import glob, sys, yaml
label = sys.argv[1]
bad = []
for f in sorted(glob.glob(".github/workflows/*.yml")):
    d = yaml.safe_load(open(f)) or {}
    trig = d.get("on") or d.get(True) or {}
    names = set(trig) if isinstance(trig, dict) else {trig}
    if not ({"pull_request", "pull_request_target"} & names):
        continue
    for job, spec in (d.get("jobs") or {}).items():
        ro = str(spec.get("runs-on", ""))
        if "self-hosted" in ro or label in ro:
            bad.append(f"{f}::{job}")
if bad:
    print("  REACHABLE FROM A FORK PR: " + ", ".join(bad))
    sys.exit(1)
print("  none — ok")
PY

if [ "${1:-}" = "--uninstall" ]; then
  say "removing the runner"
  if [ -d "$RUNNER_HOME" ]; then
    ( cd "$RUNNER_HOME" && ./svc.sh stop || true; ./svc.sh uninstall || true
      TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/remove-token" --jq .token)
      sudo -u "$RUNNER_USER" ./config.sh remove --token "$TOKEN" || true )
    rm -rf "$RUNNER_HOME"
  fi
  userdel "$RUNNER_USER" 2>/dev/null || true
  ok "runner removed. Unset SCAFFOLD_SELF_HOSTED and RUN_TIER2_INTEGRATION too."
  exit 0
fi

# ── the account ─────────────────────────────────────────────────────────────
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
  say "creating the service account $RUNNER_USER"
  useradd --system --create-home --home-dir "$RUNNER_HOME" --shell /usr/sbin/nologin "$RUNNER_USER"
else
  ok "account $RUNNER_USER already exists"
fi
# Root-equivalent, and said so at the top. Both jobs drive the system daemon.
usermod -aG docker "$RUNNER_USER"
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0750 "$RUNNER_HOME"

# ── the runner package ──────────────────────────────────────────────────────
TARBALL="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
if [ ! -x "$RUNNER_HOME/config.sh" ]; then
  say "downloading runner $RUNNER_VERSION"
  curl -fsSL -o "/tmp/$TARBALL" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${TARBALL}"
  if [ -n "$RUNNER_SHA256" ]; then
    echo "$RUNNER_SHA256  /tmp/$TARBALL" | sha256sum -c - || die "checksum mismatch"
    ok "checksum verified"
  else
    printf '\033[1;33m⚠ RUNNER_SHA256 not set — skipping checksum verification.\033[0m\n'
    printf '  Pin it: RUNNER_SHA256=<digest from the release page> sudo -E bash %s\n' "$0"
  fi
  tar xzf "/tmp/$TARBALL" -C "$RUNNER_HOME"
  chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_HOME"
  rm -f "/tmp/$TARBALL"
else
  ok "runner package already unpacked"
fi

# ── register ────────────────────────────────────────────────────────────────
say "minting a registration token (valid ~1 h)"
REG_TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)
[ -n "$REG_TOKEN" ] || die "could not mint a registration token (needs repo admin)"

say "configuring: label=$RUNNER_LABEL, no default labels beyond the platform ones"
sudo -u "$RUNNER_USER" env -i HOME="$RUNNER_HOME" PATH=/usr/bin:/bin \
  "$RUNNER_HOME/config.sh" \
    --unattended --replace \
    --url "https://github.com/$REPO" \
    --token "$REG_TOKEN" \
    --name "$(hostname)-scaffold" \
    --labels "$RUNNER_LABEL" \
    --work "$RUNNER_HOME/_work"

# ── service ─────────────────────────────────────────────────────────────────
say "installing the systemd service as $RUNNER_USER"
( cd "$RUNNER_HOME" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start )
sleep 3
( cd "$RUNNER_HOME" && ./svc.sh status ) || true

ok "runner registered and running"
cat <<EOF

Next, turn the jobs on (they stay off until you do):

  gh variable set SCAFFOLD_SELF_HOSTED  --body 1     --repo $REPO   # nightly goldens
  gh variable set RUN_TIER2_INTEGRATION --body true  --repo $REPO   # Tier 2 on trunk pushes

Verify:
  gh api repos/$REPO/actions/runners --jq '.runners[]|{name,status,labels:[.labels[].name]}'
  gh workflow run goldens.yml --repo $REPO && gh run watch --repo $REPO

To reverse all of it:
  sudo bash scripts/setup_github_runner.sh --uninstall
EOF
