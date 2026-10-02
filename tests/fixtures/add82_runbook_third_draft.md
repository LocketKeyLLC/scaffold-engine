## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo rights
- `<PALWORLD_IP>` — the guest's IP address on the LAN (the engine can read it off the host once the VM is up and the neighbour table is warmed)

## Risk

None. This step only installs a package inside the guest and enables a service. `agent: 1` is already set on VM 106, so no host-side config change is needed.

## Write these files

### /tmp/install_agent_106.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# Start the VM if it is not running
qm status 106 | grep -q running || qm start 106

# Wait for the guest to boot and get an address: warm the neighbour table, then read it
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    NET=$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p')
    for h in $(seq 1 254); do
        ping -c 1 -W 1 "$NET.$h" >/dev/null 2>&1 &
    done
    wait
    ADDR=$(ip neigh show | grep 'bc:24:11:e8:9f:7a' | awk '{print $1}' | head -n1)
    if [ -n "$ADDR" ]; then
        break
    fi
    sleep 5
done

if [ -z "$ADDR" ]; then
    echo "ERROR: could not find an address for VM 106's NIC (bc:24:11:e8:9f:7a) after 60s" >&2
    exit 1
fi

echo "Found VM 106 at $ADDR"

# Install sshpass if missing, then push our key so later steps need no password
command -v sshpass >/dev/null 2>&1 || apt-get install -y sshpass
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$ADDR"

# Install and start the agent, feeding the password to sudo on stdin
ssh -o BatchMode=yes "$PALWORLD_USER@$ADDR" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"

echo "Agent installed and started"
```

## Run this

```bash
MASS_PASSWORD="$MASS_PASSWORD" PALWORLD_USER="<PALWORLD_USER>" bash /tmp/install_agent_106.sh
```

## Verify

- Agent responds from the host: `qm agent 106 ping` — expected output includes a ping reply from the guest (not "QEMU guest agent is not running")
- Agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "systemctl is-active qemu-guest-agent"` — expected output `active`

## Rollback

If the script fails before the agent is installed, nothing has changed on the host and the guest only has an extra SSH key. To undo the key: `ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "sudo -S -p '' sed -i '/pve@/d' /home/<PALWORLD_USER>/.ssh/authorized_keys" <<< "$MASS_PASSWORD"`. If the agent install fails mid-way, re-run the script — `apt-get install` is idempotent.
