## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo access (the account you log in with at the guest console)

## Run this

```bash
qm status 106 | grep -q running || qm start 106
```

```bash
qm config 106 | grep -E '^net0:'
```

```bash
nmap -sn 192.168.1.0/24 >/dev/null 2>&1 || true
```

```bash
ip neigh show | grep -i 'bc:24:11:e8:9f:7a'
```

```bash
SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new <PALWORLD_USER>@<GUEST_IP>
```

```bash
ssh -o BatchMode=yes <PALWORLD_USER>@<GUEST_IP> "sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'" <<< "$MASS_PASSWORD"
```

## Verify

- Guest agent responds from the host: `qm agent 106 ping` returns a successful reply (no "not running" error)
- Agent service is active inside the guest: `ssh -o BatchMode=yes <PALWORLD_USER>@<GUEST_IP> "systemctl is-active qemu-guest-agent"` returns `active`

## Rollback

- If `qm start 106` fails or the VM won't boot, check `qm status 106` and `qm config 106` for the boot order; the VM was previously stopped, so a start failure likely indicates a config or storage issue — report the exact error output.
- If the sweep finds no MAC entry after the `ip neigh show` command, re-run the sweep and the `ip neigh show` once more; if still absent, the guest may not have a NIC attached or may not be booting — check `qm config 106` for a `net0:` line and `qm status 106` for boot progress.
- If `ssh-copy-id` fails with "Connection refused" or "No route to host", the guest is not up yet or the address is wrong — wait 30 seconds and re-run the sweep and `ip neigh show` to re-read the address, then retry.
- If `ssh-copy-id` fails with "Permission denied", the password for `<PALWORLD_USER>` does not match `$MASS_PASSWORD` — confirm the guest account's password or use a different account with sudo.
- If the install command fails partway, re-run it — `apt-get install` is idempotent; the `systemctl enable --now` will report already-active if the agent is already running.
