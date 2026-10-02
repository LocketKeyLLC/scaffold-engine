#!/usr/bin/env bash
set -euo pipefail

# Wait for the VM to be up and discoverable on the network
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if qm status 106 | grep -q running; then
        break
    fi
    sleep 5
done

# Sweep the bridge subnet so the host learns the VM's MAC->IP mapping
nmap -sn 192.168.1.0/24 >/dev/null 2>&1 || true

# Find the VM's address from its NIC MAC
MAC=$(qm config 106 | grep -oP 'net0:.*?virtio=\K[0-9A-Fa-f:]+' | head -1)
IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -1)

if [ -z "$IP" ]; then
    echo "ERROR: could not discover IP for VM 106 (MAC $MAC)"
    exit 1
fi

echo "Discovered VM 106 at $IP"

# Install the agent over SSH, feeding the password on stdin to sudo
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"

echo "Agent installed and started"

