## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo rights (the account you log into at the console).

## Run this

1. Start the VM if it is not already running:

```bash
qm status 106 | grep -q running || qm start 106
```

2. Wait for the guest to boot and get an address, then warm the neighbour table and read it:

```bash
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ping -c 1 -W 2 192.168.1.156 >/dev/null 2>&1 && break; sleep 5; done; nmap -sn 192.168.1.0/24 >/dev/null 2>&1 || true; ip neigh show | grep -i bc:24:11:e8:9f:7a
```

3. Install the agent inside the guest over SSH, using the address found in step 2:

```bash
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new <PALWORLD_USER>@<PALWORLD_IP>
```

4. Run the install inside the guest:

```bash
SSHPASS="$MASS_PASSWORD" sshpass -e ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "echo '$MASS_PASSWORD' | sudo -S -p '' apt-get update && echo '$MASS_PASSWORD' | sudo -S -p '' apt-get install -y qemu-guest-agent && echo '$MASS_PASSWORD' | sudo -S -p '' systemctl enable --now qemu-guest-agent"
```

## Verify

- Agent responds from the host:

```bash
qm agent 106 ping
```

- Agent service is active inside the guest:

```bash
SSHPASS="$MASS_PASSWORD" sshpass -e ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> "systemctl is-active qemu-guest-agent"
```

## Rollback

- If `qm agent 106 ping` still fails after the install, check the guest's network from the console: log in at the Proxmox web UI console for VM 106 and run `ip a` to confirm the guest has an address on the bridge. If it has no address, fix the guest's network config first, then re-run step 4.
- If SSH is refused (password rejected), the guest account or password differs from `$MASS_PASSWORD` — log in at the console and set the password, or use the console to run the install commands directly.
