📍 On: your phone, in the My Spectrum app — you're leaving the Proxmox shell; nothing in this step runs there.

## Goal
Create two port-forwarding rules in the My Spectrum app so internet traffic on ports 80 and 443 reaches the Caddy proxy at 192.168.1.26. This is what lets Let's Encrypt issue the certificate and makes the domain work over HTTPS.

## Steps

This step has 4 short phases.

**Phase 1 — Open the app on cellular data**

1. On your phone, turn OFF WiFi. Use cellular data (4G/5G) instead. Spectrum's app has a known quirk: port-forwarding changes made while connected to the same WiFi often don't save or don't take effect.
2. Open the My Spectrum app and sign in with your Spectrum account.

**Phase 2 — Find port forwarding**

3. Look for a section named "Services", "Router", or "Advanced Settings" — then find "Port Forwarding" or "Port Forward". The exact menu names vary by app version. If you don't see these, tell me what menus you DO see and I'll point you to the right one.
4. You should now see a list of any existing port-forwarding rules. If rules for ports 80 or 443 already exist, note what device or IP they point to — don't delete them yet, just read them back to me.

**Phase 3 — Create the two rules**

5. Tap "Add Port Forward" or the "+" button to create a new rule.
6. The app will ask you to pick a device. Look for `caddy-proxy` or the MAC address `bc:24:11:ac:c9:06` in the device list. If you don't see either, tell me what the app offers instead (a manual IP field? a different list?) — this container has a static IP, so the router may not have it in its device list.
7. For the first rule, set: Protocol = TCP, External/Public port = 80, Internal/Private port = 80, Device/IP = 192.168.1.26 (or the caddy-proxy device you picked).
8. Save the rule. You should see it appear in the rules list.
9. Repeat steps 5–8 for the second rule: Protocol = TCP, External port = 443, Internal port = 443, same device/IP 192.168.1.26.

**Phase 4 — Read back what's saved**

10. Look at the rules list and read back to me, for EACH rule: the protocol, the external port, the internal IP, and the internal port.

## Done when
You've read back both rules exactly as: **TCP 80 → 192.168.1.26 port 80**, and **TCP 443 → 192.168.1.26 port 443** — and they're saved in the app's rules list.
