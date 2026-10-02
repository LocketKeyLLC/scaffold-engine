#!/usr/bin/env bash
set -euo pipefail

VMID=106
USERNAME="$PALWORLD_USER"
MAC=""
IP=""

# 1. Make sure the VM is running
if ! qm status "$VMID" | grep -q running; then
    qm start "$VMID"
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        qm status "$VMID" | grep -q running && break
        sleep 5
    done
fi

# 2. Get the VM's NIC MAC from its config
MAC=$(qm config "$VMID" | grep -oP 'net0:.*?virtio=\K[0-9a-fA-F:]{17}' | head -n1)
if [ -z "$MAC" ]; then
    MAC=$(qm config "$VMID" | grep -oP 'net0:.*?=\K[0-9a-fA-F:]{17}' | head -n1)
fi
if [ -z "$MAC" ]; then
    echo "ERROR: could not read MAC for VM $VMID from qm config"
    exit 1
fi
echo "VM $VMID MAC: $MAC"

# 3. Wait for the guest to appear in the neighbor table, then read its IP
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -n1)
    if [ -n "$IP" ]; then
        break
    fi
    sleep 5
done
if [ -z "$IP" ]; then
    echo "ERROR: guest did not appear in ip neigh after waiting; is its network up?"
    exit 1
fi
echo "VM $VMID IP: $IP"

# 4. Install sshpass on the host if missing
if ! command -v sshpass >/dev/null 2>&1; then
    apt-get update
    apt-get install -y sshpass
fi

# 5. Copy the host's root SSH key into the guest (password from $MASS_PASSWORD)
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$USERNAME@$IP"

# 6. Install and enable qemu-guest-agent inside the guest
ssh -o BatchMode=yes "$USERNAME@$IP" 'echo "$MASS_PASSWORD" | sudo -S apt-get update && echo "$MASS_PASSWORD" | sudo -S apt-get install -y qemu-guest-agent && echo "$MASS_PASSWORD" | sudo -S systemctl enable --now qemu-guest-agent'

# 7. Confirm the agent is active inside the guest
ssh -o BatchMode=yes "$USERNAME@$IP" 'systemctl is-active qemu-guest-agent'
