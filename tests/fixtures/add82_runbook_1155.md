## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo rights (the operator logs in with this at the console; it is not `root` unless the guest was set up that way)
- `<PALWORLD_PASSWORD>` — that user's password (used once to install the agent; it is the same `$MASS_PASSWORD` if the guest was built with the mass password, but the runbook must not assume that)

## Risk

- The guest's network must be up for `apt` to reach the mirrors. If the guest has no working network, the install fails — fix the guest's network first and say so here rather than working around it.
- The guest is currently stopped. Starting it is required and is not destructive.

## Write these files

### /tmp/install_agent_106.sh
```bash
#!/usr/bin/env bash
set -u

MAC="bc:24:11:e8:9f:7a"
USERNAME="$1"
PASSWORD="$2"

qm status 106 | grep -q running || qm start 106

for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    ping -c 1 -W 2 192.168.1.1 >/dev/null 2>&1 && break
    sleep 5
done

NET=$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p')
for h in $(seq 1 254); do
    ping -c 1 -W 1 "$NET.$h" >/dev/null 2>&1 &
done
wait

IP=$(ip neigh show | grep -i "$MAC" | awk '{print $1}' | head -1)

if [ -z "$IP" ]; then
    echo "FAILED: could not find guest address for MAC $MAC"
    exit 1
fi

echo "Guest address: $IP"

SSHPASS="$PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$USERNAME@$IP"

ssh -o BatchMode=yes "$USERNAME@$IP" "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$PASSWORD"

ssh -o BatchMode=yes "$USERNAME@$IP" "systemctl is-active qemu-guest-agent"
```

## Run this

```bash
apt-get install -y sshpass
```

```bash
bash /tmp/install_agent_106.sh <PALWORLD_USER> <PALWORLD_PASSWORD>
```

## Verify

- Agent responds from the host: `qm agent 106 ping` returns a successful reply from the guest.
- Guest agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<GUEST_IP> "systemctl is-active qemu-guest-agent"` returns `active`.

## Rollback

- If the script fails before `ssh-copy-id`, the guest is left running but unchanged — stop it with `qm stop 106` if you want to return to the prior state.
- If `apt-get install` fails because the guest has no network, fix the guest's network configuration first, then re-run the script — it is idempotent (`ssh-copy-id` and the install both treat "already done" as success).
- If `qm agent 106 ping` still fails after the install, check that `agent: 1` is set on the VM config (`qm config 106 | grep agent`) — it is already set per the measured state, so no change should be needed.
