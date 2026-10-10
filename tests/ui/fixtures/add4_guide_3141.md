_The saved walkthrough for this step is from before your recent work on it — writing a fresh one that picks up from where you actually are (this can take a minute or two)…_

## 👉 Do this next
📍 In: the My Spectrum app on your phone
**Do this now:** Tap the **Services** tab at the bottom of the app, then follow the steps below.

## Goal
Create two port-forwarding rules in the My Spectrum app so outside web traffic on ports 80 and 443 reaches the Caddy proxy at 192.168.1.26. This lets Caddy get its Let's Encrypt certificate and serve the panel over HTTPS.

## Steps
1. In the My Spectrum app, tap the **Services** tab at the bottom.
2. Tap **Router**.
3. Tap **Advanced Settings**.
4. Tap **Port Forwarding & IP Reservations**.
5. Tap **Add Port Forwarding** (or **+**).
6. For the first rule, fill in:
   - Device: choose **caddy-proxy** (or the entry showing **bc:24:11:ac:c9:06**)
   - Protocol: **TCP**
   - External port: **80**
   - Internal port: **80**
   - Internal IP: **192.168.1.26** (if the app asks for an IP instead of a device, type this)
7. Save the rule. You should see it appear in the list.
8. Repeat steps 5–7 for the second rule with external port **443** and internal port **443**, same device/IP.
9. Before saving, turn off your phone's Wi-Fi so the app uses cellular data — this avoids a known issue where changes don't take effect while the phone is on the same network. (If you already saved, that's okay; just make sure both rules are saved.)

If your screen differs from these labels, tell me what you see instead.

## ✅ Done when
Both rules are saved and you can read them back: **TCP 80 → 192.168.1.26:80** and **TCP 443 → 192.168.1.26:443**.

Then press **✓ Done → next step**.

If the rules don't appear or the labels differ, paste what you see instead.
