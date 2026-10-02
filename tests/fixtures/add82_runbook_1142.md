## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo rights (the account you log in with at the console).

## Prerequisites

- VM 106 is running (`qm status 106` shows `running`).
- The guest has network access and SSH is reachable once its address is known.
- `sshpass` is installed on the host (install it in step 1 if missing).

## Risk

- None. This step only installs a package inside the guest and enables a service. No data is destroyed.

## Write these files

### /tmp/install_agent_106.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

MAC="bc:24:11:e8:9f:7a"
USER="<PALWORLD_USER>"

# Warm the neighbour table so the guest's MAC appears.
NET=$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p')
for h in $(seq 1 254); do
    ping -c 1 -W 1 "$NET.$h" >/dev/null 2>&1 &
done
wait

# Read the guest's address from its MAC.
ADDR=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -n1)
if [ -z "$ADDR" ]; then
    echo "ERROR: no neighbour entry for MAC $MAC after sweep" >&2
    exit 1
fi
echo "guest address: $ADDR"

# Copy the host key over (password on sshpass's environment, never argv).
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$USER@$ADDR"

# Install and enable the agent. The password reaches sudo on stdin only.
ssh -o BatchMode=yes "$USER@$ADDR" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"

# Confirm the service is active inside the guest.
ssh -o BatchMode=yes "$USER@$ADDR" "systemctl is-active qemu-guest-agent"
```

## Run this

```bash
apt-get install -y sshpass
```

```bash
MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_agent_106.sh
```

## Verify

- The guest agent answers from the host: `qm agent 106 ping` returns a successful reply (no "not running" error).
- The agent service is active inside the guest: `qm agent 106 exec -- systemctl is-active qemu-guest-agent` returns `active`.

## Rollback

- If the script fails before the agent is installed, nothing has changed — fix the address or credentials and re-run the script.
- If the agent was installed but `qm agent 106 ping` still fails, reboot the guest from the host (`qm reboot 106`), wait for it to come back, then re-check. If it still fails, check the guest's logs at the console: `journalctl -u qemu-guest-agent`.
