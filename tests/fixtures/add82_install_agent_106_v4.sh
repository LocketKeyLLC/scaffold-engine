#!/bin/bash
set -euo pipefail

MAC=$(qm config 106 | sed -n 's/^net0:.*virtio=\([0-9A-Fa-f:]*\).*/\1/p' | head -1)
if [ -z "$MAC" ]; then
    echo "FAIL: could not read net0 MAC from qm config 106"
    exit 1
fi
echo "MAC: $MAC"

qm status 106 | grep -q running || qm start 106

for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    NET=$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p')
    for h in $(seq 1 254); do
        ping -c 1 -W 1 "$NET.$h" >/dev/null 2>&1 &
    done
    wait
    IP=$(ip neigh show | grep -i "$MAC" | grep -oP '^\K[0-9.]+' | head -1)
    if [ -n "$IP" ]; then
        break
    fi
    sleep 5
done

if [ -z "$IP" ]; then
    echo "FAIL: could not find an address for MAC $MAC after waiting"
    exit 1
fi
echo "IP: $IP"

if ! command -v sshpass >/dev/null 2>&1; then
    apt-get install -y sshpass
fi

SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP"

ssh -o BatchMode=yes "$PALWORLD_USER@$IP" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"

ssh -o BatchMode=yes "$PALWORLD_USER@$IP" "systemctl is-active qemu-guest-agent"
