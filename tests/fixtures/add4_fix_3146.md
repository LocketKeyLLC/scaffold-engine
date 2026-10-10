## 👉 Do this next

**Run this now:**

```bash
pct exec 120 -- ip a
```

Then tell me what it prints — specifically whether `eth0` has an `inet` line, and what address it shows.

📍 On: the Proxmox host shell (root@pve)

---

## Diagnosis

The container was switched to DHCP and rebooted, but `ip -4 addr show eth0` printed nothing. That usually means one of two things: the interface name inside the container is not `eth0` (Debian 12 sometimes names it differently), or the container hasn't finished requesting a DHCP lease yet. The `Systemd 252 detected` warning is harmless — it's about nesting, not networking.

## Fix

1. List all interfaces and their addresses inside the container:

```bash
pct exec 120 -- ip a
```

2. If you see an interface with a different name (like `eth1` or `enp0s1`) holding an `inet` address, that's the real interface name — note it.

3. If no interface has an `inet` address at all, force a DHCP request:

```bash
pct exec 120 -- dhclient -v
```

4. Check again:

```bash
pct exec 120 -- ip a
```

## Then

Once you have the IP address (it should be something like `192.168.1.x`), go to the My Spectrum app:

1. Tap **Services** → **Router** → **Advanced Settings** → **Port Forwarding & IP Reservations**.
2. Find `bc:24:11:ac:c9:06` — it should now show the IP you just found.
3. Reserve that IP.
4. Create two port-forward rules, both pointing at that device:
   - **TCP 80 → 80**
   - **TCP 443 → 443**
5. Turn off your phone's Wi-Fi before saving (use cellular data) so the changes take effect.

Read back both rules to confirm they're saved.

## If that fails

If `ip a` shows no `inet` address even after `dhclient`, the container's DHCP client may not be installed or the bridge isn't handing out leases. Run:

```bash
pct exec 120 -- cat /etc/network/interfaces
pct exec 120 -- which dhclient
```

Tell me what both print.
