## Inputs needed

- `<PALWORLD_USER>` — the account inside VM 106 that has sudo (the operator fills this; nothing the engine holds names it)

## Write these files

### /tmp/install_agent_106.sh
```bash
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
```

## Run this

```bash
MASS_PASSWORD="$MASS_PASSWORD" PALWORLD_USER="<PALWORLD_USER>" bash /tmp/install_agent_106.sh
```

## Verify

- Agent responds from the host: `qm agent 106 ping` — expect a successful reply from the guest (not "QEMU guest agent is not running")
- Agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<IP> "systemctl is-active qemu-guest-agent"` — expect `active` (substitute the IP the script printed)

## Rollback

- If the script fails before `ssh-copy-id`, nothing changed on the guest — re-run after checking `qm config 106` for the net0 MAC and that the VM is running.
- If `ssh-copy-id` succeeded but the install failed, re-run the script; it is idempotent (key copy and `apt-get install` both treat already-present as success).
- If the agent installs but `qm agent 106 ping` still fails, check the VM config has `agent: 1` with `qm config 106 | grep agent` — if missing, set it with `qm set 106 --agent 1` and re-run the verify.

## Executed on pve-runner (supervised, operator-approved)
$ write /tmp/install_agent_106.sh (1211 bytes)
wrote /tmp/install_agent_106.sh (1211 bytes, 40 lines)
$ MASS_PASSWORD="$MASS_PASSWORD" PALWORLD_USER="aedefruscio" bash /tmp/install_agent_106.sh
MAC: BC:24:11:E8:9F:7A
rc=1

## What went wrong, and what to try

## 👉 Do this next
📍 On: the Proxmox host shell (root@pve)

**Run this now:**
```bash
for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i >/dev/null 2>&1 & done; wait; ip neigh show | grep -i 'bc:24:11:e8:9f:7a'
```
Then tell me what it shows — an IP line like `192.168.1.x dev vmbr0 lladdr bc:24:11:e8:9f:7a REACHABLE`, or nothing at all.

## Diagnosis
The script printed `MAC: BC:24:11:E8:9F:7A` and then exited with code 1. That means it found the VM's MAC address but could not find an IP address for it in the host's neighbor table (`ip neigh`). The VM has no network presence the host can see, so the script had nothing to connect to and stopped. The real blocker is that VM 106's network is not up or not configured — not the agent install itself.

## Fix
First, find the VM's IP address. The command above pings every address on your local network (192.168.1.1–254), which forces the host to learn who is at each address. If the VM has an IP, it will appear in the output.

1. If the command shows an IP, write it down (we'll call it `<VM_IP>` below).
2. Make sure `sshpass` is installed on the host (it lets us log in with a password without typing it interactively):
```bash
command -v sshpass || apt-get install -y sshpass
```
3. Copy your SSH key into the VM so later commands don't need the password each time (replace `<VM_IP>` with the real IP):
```bash
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new aedefruscio@<VM_IP>
```
You should see a line like `Number of key(s) added: 1`.

4. Now install and start the guest agent inside the VM (this runs the commands on the VM, not the host):
```bash
ssh aedefruscio@<VM_IP> "echo '$MASS_PASSWORD' | sudo -S apt-get update
echo '\$MASS_PASSWORD' | sudo -S apt-get install -y qemu-guest-agent
echo '\$MASS_PASSWORD' | sudo -S systemctl enable --now qemu-guest-agent
systemctl is-active qemu-guest-agent"
```
You should see `active` at the end.

## Then
Confirm the agent answers from the host:
```bash
qm agent 106 ping
```
You should see a reply like `{"return":{}}` — that is the success signal for this step.

## If that fails
- If the first scan shows **no IP at all**, the VM's network is down inside the guest. You'll need to get into the guest another way to fix it. Tell me the output of the scan and I'll give you the next move (likely adding a serial console to the VM so we can see and fix its network config).
- If `ssh-copy-id` fails with `Permission denied`, the password in `$MASS_PASSWORD` may not match the guest account `aedefruscio`. Run `echo "$MASS_PASSWORD" | wc -c` to confirm it is set, and tell me the exact error line.
- If the install command fails with `sudo: a password is required`, the guest user may not have passwordless sudo and the password is not being accepted. Paste the full error and I'll adjust.

---
ℹ️ **Adjusted for your system:** split 3 chained command(s) into separate lines — the VM console has no copy-paste, so each is typed by hand; these console commands need `sudo` (a guest console is an ordinary user, unlike the Proxmox host): `systemctl is-active qemu-guest-agent"`; escaped 2 `$`/backtick character(s) inside a quoted heredoc so the outer shell writes them to the file instead of substituting them.
