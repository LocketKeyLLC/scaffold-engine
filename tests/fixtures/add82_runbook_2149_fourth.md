## Inputs needed

- `<PALWORLD_USER>` — the username inside the palworld-server guest that has sudo rights (the operator logs in with this at the console).

## Run this

The guest has no network presence the host can see (`ip neigh` shows no entry for its NIC), so this step is done at the VM's console by hand. The operator opens the Proxmox web UI, selects VM 106, clicks **Console**, and logs in to the guest.

1. Log in to the guest at the console, then run:

```bash
sudo apt-get update
```

2. Install the agent:

```bash
sudo apt-get install -y qemu-guest-agent
```

3. Enable and start the agent:

```bash
sudo systemctl enable --now qemu-guest-agent
```

4. Confirm the agent is active inside the guest:

```bash
systemctl is-active qemu-guest-agent
```

5. Back on the host shell (the same single session, after the console work), confirm the agent answers:

```bash
qm agent 106 ping
```

## Verify

- `systemctl is-active qemu-guest-agent` inside the guest returns `active`.
- `qm agent 106 ping` on the host returns a successful reply from the guest (no "QEMU guest agent is not running" error).

## Rollback

- If `sudo apt-get update` fails because the guest has no network, stop and report that the guest's network must be fixed first — do not work around it.
- If `qm agent 106 ping` still reports the agent is not running after the install, check inside the guest that the service is active (`systemctl status qemu-guest-agent`) and that the VM config still has `agent: 1` (`qm config 106 | grep agent`).
