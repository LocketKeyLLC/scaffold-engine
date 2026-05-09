#!/usr/bin/env bash
# Scaffold Engine — host-level bootstrap (audit I1).
#
# Companion to scripts/bootstrap.sh (which is repo-aware: assumes Docker,
# the ai-network, and base volumes are already reachable). This script
# captures the host-level state §17.63 SSD migration documented in
# OVERVIEW prose but never automated:
#
#   1. /mnt/adamssd ext4 mount (presence + correct fs)
#   2. fstab entry for persistent mount across reboot
#   3. ~/scaffold-engine symlink → /mnt/adamssd/scaffold-engine
#   4. /etc/docker/daemon.json data-root → /mnt/adamssd/docker
#   5. ai-network pinned to 172.18.0.0/16 / 172.18.0.1
#   6. Named-volume ownership (chown_named_volumes.sh, X.28)
#
# Idempotent steps auto-apply. Destructive / sudo / first-time steps
# (disk format, fstab edit, dockerd restart, /var/lib/docker rsync) are
# detected and reported with the exact commands to run; the script
# exits non-zero so a CI guard (or an attentive operator) cannot miss
# an incomplete setup.
#
# Run without sudo for read-only checks. Sudo-required steps are
# explicitly named in their warning output — the script never elevates
# itself.
#
# Usage:
#   bash scripts/bootstrap-host.sh          # check + apply idempotent
#   bash scripts/bootstrap-host.sh check    # check only, no changes

set -euo pipefail

# ── ANSI helpers (same shape as bootstrap.sh) ─────────────────────────
if [[ -t 1 ]]; then
    C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
    C_INFO=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_DIM=""; C_RST=""
fi
ok()   { printf '%s✓%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_RST" "$*"; }
err()  { printf '%sx%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; }
info() { printf '%s>%s %s\n' "$C_INFO" "$C_RST" "$*"; }
hdr()  { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_RST"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RST"; }

# ── Mode parsing ──────────────────────────────────────────────────────
MODE="apply"
for arg in "$@"; do
    case "$arg" in
        check) MODE="check" ;;
        --help|-h)
            cat <<'USAGE'
Usage: bash scripts/bootstrap-host.sh [check]

  (no arg)   Check + apply idempotent steps. Manual / destructive
             steps print instructions and the script exits non-zero
             if any need operator action.
  check      Check only — no changes made; same exit-code contract.
  --help     This message.

What it covers (audit I1, §17.63 follow-up):
  1. /mnt/adamssd ext4 mount + fstab entry (informational; manual)
  2. ~/scaffold-engine symlink (idempotent; auto)
  3. /etc/docker/daemon.json data-root (manual; sudo)
  4. ai-network subnet pin 172.18.0.0/16 (auto when missing; manual when wrong)
  5. Named-volume ownership (auto via chown_named_volumes.sh)

Run on a fresh host BEFORE scripts/bootstrap.sh.
USAGE
            exit 0 ;;
        *) err "Unknown argument: $arg"; exit 2 ;;
    esac
done

REPO_ROOT="/mnt/adamssd/scaffold-engine"
SYMLINK_PATH="$HOME/scaffold-engine"
NEEDS_MANUAL=()

need_manual() {
    NEEDS_MANUAL+=("$1")
}

# ── 1. SSD ext4 mount ─────────────────────────────────────────────────
hdr "1. /mnt/adamssd ext4 mount"

if [[ ! -d /mnt/adamssd ]]; then
    warn "/mnt/adamssd does not exist"
    need_manual "Format and mount the SSD (DESTRUCTIVE — verify the device with \`lsblk\` first):
       sudo wipefs -a /dev/sda
       sudo mkfs.ext4 -L adamssd /dev/sda
       sudo mkdir -p /mnt/adamssd
       sudo mount /dev/sda /mnt/adamssd
       sudo chown \$USER:\$USER /mnt/adamssd"
elif ! findmnt /mnt/adamssd >/dev/null 2>&1; then
    warn "/mnt/adamssd exists but nothing is mounted there"
    need_manual "Mount the SSD: sudo mount LABEL=adamssd /mnt/adamssd  (or use the UUID from blkid)"
else
    fs_type="$(findmnt -no FSTYPE /mnt/adamssd)"
    src="$(findmnt -no SOURCE /mnt/adamssd)"
    if [[ "$fs_type" != "ext4" ]]; then
        err "/mnt/adamssd is mounted as $fs_type (expected ext4)"
        need_manual "Reformat as ext4 (DESTRUCTIVE — see step 1 if the SSD has no data to keep)"
    else
        ok "/mnt/adamssd mounted ext4 from $src"
    fi
fi

# ── 2. fstab entry ────────────────────────────────────────────────────
hdr "2. fstab persistence"

if grep -q "/mnt/adamssd" /etc/fstab 2>/dev/null; then
    line="$(grep "/mnt/adamssd" /etc/fstab | head -1)"
    dim "    $line"
    if echo "$line" | grep -q "nofail"; then
        ok "fstab entry present (nofail set — boot won't stall if SSD detached)"
    else
        warn "fstab entry present but missing 'nofail' — boot will stall if SSD is detached"
        need_manual "Edit /etc/fstab: add 'nofail,x-systemd.device-timeout=10s' to the /mnt/adamssd line"
    fi
else
    warn "no fstab entry for /mnt/adamssd — mount won't persist across reboot"
    uuid="$(findmnt -no UUID /mnt/adamssd 2>/dev/null || echo "<UUID-from-blkid>")"
    need_manual "Append to /etc/fstab (sudo):
       UUID=$uuid  /mnt/adamssd  ext4  defaults,nofail,x-systemd.device-timeout=10s  0  2"
fi

# ── 3. ~/scaffold-engine symlink ──────────────────────────────────────
hdr "3. $SYMLINK_PATH symlink"

if [[ -L "$SYMLINK_PATH" ]]; then
    target="$(readlink -f "$SYMLINK_PATH" 2>/dev/null || echo "")"
    if [[ "$target" == "$REPO_ROOT" ]]; then
        ok "symlink already correct: $SYMLINK_PATH → $REPO_ROOT"
    else
        warn "symlink exists but points at ${target:-<broken>} (expected $REPO_ROOT)"
        if [[ "$MODE" == "apply" ]]; then
            ln -sfn "$REPO_ROOT" "$SYMLINK_PATH"
            ok "symlink rewritten: $SYMLINK_PATH → $REPO_ROOT"
        else
            need_manual "Update symlink: ln -sfn $REPO_ROOT $SYMLINK_PATH"
        fi
    fi
elif [[ -e "$SYMLINK_PATH" ]]; then
    err "$SYMLINK_PATH exists but is NOT a symlink (refusing to clobber)"
    need_manual "Inspect $SYMLINK_PATH; if you want to replace it with a symlink, move it aside first"
else
    if [[ ! -d "$REPO_ROOT" ]]; then
        warn "$REPO_ROOT does not exist — repo isn't on the SSD yet"
        need_manual "Place the repo at $REPO_ROOT (e.g. \`git clone <url> $REPO_ROOT\` or rsync from a prior host)"
    elif [[ "$MODE" == "apply" ]]; then
        ln -s "$REPO_ROOT" "$SYMLINK_PATH"
        ok "symlink created: $SYMLINK_PATH → $REPO_ROOT"
    else
        warn "symlink missing"
        need_manual "ln -s $REPO_ROOT $SYMLINK_PATH"
    fi
fi

# ── 4. Docker daemon.json data-root ───────────────────────────────────
hdr "4. /etc/docker/daemon.json data-root"

if [[ ! -f /etc/docker/daemon.json ]]; then
    warn "/etc/docker/daemon.json does not exist"
    need_manual "Set the data-root and migrate /var/lib/docker (sudo, dockerd-restart):
       echo '{\"data-root\": \"/mnt/adamssd/docker\"}' | sudo tee /etc/docker/daemon.json
       sudo systemctl stop docker
       sudo rsync -aHAX /var/lib/docker/ /mnt/adamssd/docker/   # -aHAX preserves hard links + ACLs + xattrs (needed for image layer dedup + SELinux labels)
       sudo systemctl start docker"
elif grep -q "/mnt/adamssd/docker" /etc/docker/daemon.json; then
    ok "daemon.json data-root → /mnt/adamssd/docker"
else
    warn "daemon.json exists but data-root is not /mnt/adamssd/docker"
    dim "    current: $(cat /etc/docker/daemon.json)"
    need_manual "Update /etc/docker/daemon.json data-root and migrate via rsync (sudo, dockerd-restart) — see step 4 above"
fi

# Verify the running daemon agrees with the on-disk config.
if command -v docker >/dev/null 2>&1; then
    actual_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")"
    if [[ "$actual_root" == "/mnt/adamssd/docker" ]]; then
        ok "running dockerd reports DockerRootDir=/mnt/adamssd/docker"
    elif [[ -n "$actual_root" ]]; then
        warn "running dockerd reports DockerRootDir=$actual_root (config drift — restart pending)"
        need_manual "Restart Docker to pick up the new daemon.json: sudo systemctl restart docker"
    fi
fi

# ── 5. ai-network subnet pin ──────────────────────────────────────────
hdr "5. ai-network subnet pin (172.18.0.0/16, gateway 172.18.0.1)"

if ! command -v docker >/dev/null 2>&1; then
    warn "docker CLI not found — install Docker before this step"
elif docker network inspect ai-network >/dev/null 2>&1; then
    actual="$(docker network inspect ai-network --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)"
    if [[ "$actual" == "172.18.0.0/16" ]]; then
        ok "ai-network present (subnet 172.18.0.0/16)"
    else
        warn "ai-network exists but subnet is '${actual:-<unset>}' (expected 172.18.0.0/16)"
        dim "    Containers reach the host-installed Ollama at 172.18.0.1:11434, hardcoded across compose env."
        need_manual "Stop the stack, recreate the network with the correct pin:
       cd $REPO_ROOT && docker compose down
       docker network rm ai-network
       docker network create --driver bridge --subnet 172.18.0.0/16 --gateway 172.18.0.1 ai-network"
    fi
else
    if [[ "$MODE" == "apply" ]]; then
        info "creating ai-network with subnet pin"
        docker network create \
            --driver bridge \
            --subnet 172.18.0.0/16 \
            --gateway 172.18.0.1 \
            ai-network >/dev/null
        ok "ai-network created (subnet 172.18.0.0/16, gateway 172.18.0.1)"
    else
        warn "ai-network missing"
        need_manual "docker network create --driver bridge --subnet 172.18.0.0/16 --gateway 172.18.0.1 ai-network"
    fi
fi

# ── 6. Named-volume ownership ─────────────────────────────────────────
hdr "6. Named-volume ownership (UID 10001 = scaffold user, X.28)"

CHOWN_SCRIPT="$REPO_ROOT/scripts/chown_named_volumes.sh"
if ! command -v docker >/dev/null 2>&1; then
    warn "docker CLI not found — skipping"
elif [[ ! -f "$CHOWN_SCRIPT" ]]; then
    warn "$CHOWN_SCRIPT not found (repo not at $REPO_ROOT?)"
else
    # Probe one of the chown-target volumes. If it doesn't exist yet,
    # the volume will be created at first compose-up — re-run this
    # script then to confirm ownership.
    v="scaffold-engine_scaffold-logs"
    if docker volume inspect "$v" >/dev/null 2>&1; then
        owner="$(docker run --rm -v "$v:/t" alpine stat -c '%u:%g' /t 2>/dev/null || echo "")"
        if [[ "$owner" == "10001:10001" ]]; then
            ok "$v owned by 10001:10001"
        elif [[ -z "$owner" ]]; then
            warn "could not probe $v ownership"
        else
            warn "$v owned by $owner (expected 10001:10001 — pre-X.28 ownership)"
            if [[ "$MODE" == "apply" ]]; then
                info "running chown_named_volumes.sh"
                bash "$CHOWN_SCRIPT"
                ok "volume ownership corrected"
            else
                need_manual "bash $CHOWN_SCRIPT  (run with the orchestrator stopped)"
            fi
        fi
    else
        dim "    $v does not exist yet — will be created on first \`docker compose up\`"
        dim "    re-run this script after \`make build\` to verify the post-creation ownership"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────
hdr "Summary"

if (( ${#NEEDS_MANUAL[@]} == 0 )); then
    ok "host bootstrap is complete — nothing to do"
    exit 0
fi

err "${#NEEDS_MANUAL[@]} step(s) need manual action:"
for i in "${!NEEDS_MANUAL[@]}"; do
    printf '\n%s[%d]%s %s\n' "$C_WARN" "$((i+1))" "$C_RST" "${NEEDS_MANUAL[$i]}"
done
echo
exit 2
