## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest (the account you log in as at its console)
- `<PALWORLD_IP>` — the guest's IP address, if you already know it from the guest's console or router app

If you do not know the guest's IP, the script below will find it from the VM's MAC address (`bc:24:11:e8:9f:7a`) after starting the VM and sweeping the subnet.

## Prerequisites

- VM 106 exists and is stopped (`qm status 106` shows `status: stopped`)
- `sshpass` is installed on the Proxmox host (`apt-get install -y sshpass` if missing)
- The guest has SSH enabled and accepts password auth for `<PALWORLD_USER>`

## Write these files

### /tmp/install_guest_agent.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

VMID=106
MAC="bc:24:11:e8:9f:7a"
USER="${PALWORLD_USER:?PALWORLD_USER is required}"
PASS="${MASS_PASSWORD:?MASS_PASSWORD is required}"

# 1. Start the VM if it is not running
if ! qm status "$VMID" | grep -q running; then
    qm start "$VMID"
fi

# 2. Wait for the VM to boot and get an address
IP=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    # Warm the neighbour table: sweep the bridge subnet
    NET=$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p')
    for h in $(seq 1 254); do
        ping -c 1 -W 1 "$NET.$h" >/dev/null 2>&1 &
    done
    wait

    # Read the neighbour table for the VM's MAC
    IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -n1)
    if [ -n "$IP" ]; then
        break
    fi
    sleep 5
done

if [ -z "$IP" ]; then
    echo "ERROR: could not find an IP for MAC $MAC after waiting"
    exit 1
fi

echo "Found palworld-server at $IP"

# 3. Install sshpass if missing (host side)
if ! command -v sshpass >/dev/null 2>&1; then
    apt-get update
    apt-get install -y sshpass
fi

# 4. Copy the host's SSH key into the guest (password auth, one time)
SSHPASS="$PASS" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$USER@$IP"

# 5. Install and enable qemu-guest-agent inside the guest
ssh -o BatchMode=yes "$USER@$IP" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$PASS"

# 6. Confirm the agent answers from the host
qm agent "$VMID" ping
```

## Run this

```bash
apt-get install -y sshpass
```

```bash
PALWORLD_USER="<PALWORLD_USER>" MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_guest_agent.sh
```

## Verify

- The guest agent responds from the host: `qm agent 106 ping` — expect a successful reply (no "not running" error)
- The agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "systemctl is-active qemu-guest-agent"` — expect `active`
- The VM config has the agent enabled: `qm config 106 | grep ^agent` — expect `agent: 1`

## Rollback

- If the script fails at "could not find an IP": the guest may not have network. Open the VM's console in the Proxmox web UI, log in, and check `ip a` — fix networking there, then re-run the script.
- If `ssh-copy-id` fails with "Permission denied": the username or password is wrong. Confirm `<PALWORLD_USER>` and `$MASS_PASSWORD` match what you use at the guest's console.
- If SSH is refused entirely (connection refused, no route): the guest has no SSH server. Install `openssh-server` at the guest's console, then re-run the script.
- If the agent still does not answer after the script succeeds: reboot the guest once (`qm reboot 106`), wait 30 seconds, then run `qm agent 106 ping` again.
