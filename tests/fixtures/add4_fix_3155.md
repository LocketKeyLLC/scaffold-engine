## 👉 Do this next
📍 In: the My Spectrum app on your phone (cellular data, not Wi-Fi)
**Tap:** Services → Router → Advanced Settings → Port Forwarding & IP Reservations → Add Port Assignment. Fill in: Name `Caddy HTTP`, Protocol **TCP**, External port **80**, Internal port **80**, Device **caddy-proxy (192.168.1.127)**, then tap the checkmark to save. Then repeat for `Caddy HTTPS` with ports **443**.

## Fix
The reservation succeeded — now add the two port-forward rules.

1. In the app, go to **Services → Router → Advanced Settings → Port Forwarding & IP Reservations**. You should see the reservation for caddy-proxy at **192.168.1.127**.
2. Find the **Port Forwarding** section. If it lists **HTTP** and **HTTPS** with toggles, turn both on and select the reserved device. If it has **Add Port Assignment**, tap that.
3. For the first rule, fill in:
   - Name: `Caddy HTTP`
   - Protocol: **TCP**
   - External port: **80**
   - Internal port: **80**
   - Device: **caddy-proxy (192.168.1.127)**
   Tap the checkmark / save.
4. Repeat for the second rule:
   - Name: `Caddy HTTPS`
   - Protocol: **TCP**
   - External port: **443**
   - Internal port: **443**
   - Device: **caddy-proxy (192.168.1.127)**
   Tap save.
5. You should now see both rules in the list.

## Then
Read back exactly what the app shows for both rules. It should be:
- **TCP 80 → 192.168.1.127:80**
- **TCP 443 → 192.168.1.127:443**

If it matches, this step is done — press **Done → next step**. The next step will check the certificate.

## If that fails
- If the app only shows **HTTP** and **HTTPS** toggles and no **Add Port Assignment**, use the toggles — that creates the same two rules.
- If **Add Port Assignment** is greyed out or the device doesn't appear, force-close the app, reopen on cellular data, and pull down to refresh the device list.
- If it says port 80 or 443 is already in use, look for an existing rule and remove it first.

---
ℹ️ **No official documentation was retrieved for this** — the screen labels, menu paths and setting levels above come from general knowledge, not from a page fetched for this question. Verify them against the screen in front of you.

---
🧭 **Still on this step after 5 replies.** Its definition of done is:
> Done when Caddy in container 120 can successfully obtain a Let's Encrypt certificate for the domain (e.
If any part of that is already true on your machine, say so and I will close the step. If the current approach is not getting you there, say "different approach" and I will work the goal from another angle instead of continuing this thread.
