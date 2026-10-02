## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo access (the guest's own account, not root@pve)
- `<PALWORLD_IP>` — the guest's IP address, if you already know it (otherwise the script below will discover it from the VM's MAC)

## Run this

### 1. Write the discovery-and-install script

## Write these files

### /tmp/install_guest_agent.sh
```bash
#!/bin/bash
set -euo pipefail

VMID=106
MAC=$(qm config "$VMID" | grep -oP 'net0:.*?virtio=\K[0-9a-fA-F:]{17}' | head -1)
echo "VM $VMID MAC: $MAC"

# Make sure the VM is running
qm status "$VMID" | grep -q running || qm start "$VMID"

# Wait for the VM to boot and appear on the network
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    ping -c 1 -W 2 192.168.1.1 >/dev/null 2>&1 || true
    IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -1)
    if [ -n "$IP" ]; then
        echo "Found guest IP: $IP"
        break
    fi
    sleep 5
done

if [ -z "${IP:-}" ]; then
    echo "ERROR: Could not discover guest IP from MAC $MAC after waiting."
    echo "Sweep the subnet to force the host to learn the MAC:"
    for h in $(seq 1 254); do ping -c 1 -W 1 192.168.1.$h >/dev/null 2>&1 & done
    wait
    IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -1)
    if [ -z "$IP" ]; then
        echo "ERROR: Still no IP for MAC $MAC. Check that the VM has network connectivity."
        exit 1
    fi
    echo "Found guest IP after sweep: $IP"
fi

# Install the agent over SSH
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP"

ssh -o BatchMode=yes "$PALWORLD_USER@$IP" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"

echo "Agent installed and started on $IP"
```

### 2. Run the script

```bash
PALWORLD_USER="<PALWORLD_USER>" MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_guest_agent.sh
```

## Verify

- Confirm the agent responds from the host: `qm agent 106 ping`
- Confirm the agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "systemctl is-active qemu-guest-agent"`

## Rollback

If the script fails at the SSH step with "Permission denied", the guest's SSH server may not be running or the user may not have sudo. Check the guest console directly and confirm SSH is enabled (`systemctl is-active ssh` inside the guest). If the guest has no network, fix its network configuration first — the script will report "Still no IP for MAC" and exit.
