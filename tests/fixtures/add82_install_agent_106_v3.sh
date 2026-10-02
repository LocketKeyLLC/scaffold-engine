#!/bin/bash
set -euo pipefail

# Start the VM if it is not running
if ! qm status 106 | grep -q running; then
    qm start 106
fi

# Wait for the VM to boot and appear on the network
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if qm status 106 | grep -q running; then
        # Sweep the bridge subnet so the host learns the VM's MAC
        nmap -sn 192.168.1.0/24 >/dev/null 2>&1 || true
        # Look up the VM's MAC from its config
        MAC=$(qm config 106 | grep -oP 'net0:.*?virtio=\K[0-9A-Fa-f:]+' | head -1)
        if [ -n "$MAC" ]; then
            IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -1)
            if [ -n "$IP" ]; then
                break
            fi
        fi
    fi
    sleep 5
done

if [ -z "${IP:-}" ]; then
    echo "ERROR: could not discover VM 106's IP address from its MAC"
    exit 1
fi

echo "VM 106 is at $IP"

# Copy the SSH key into the guest
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new root@"$IP"

# Install and enable qemu-guest-agent inside the guest
ssh -o BatchMode=yes root@"$IP" "apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent && systemctl is-active qemu-guest-agent"

