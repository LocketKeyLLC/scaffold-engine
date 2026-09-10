## 👉 Do this next

**Run this now:**
```bash
pct exec 120 -- cat /etc/caddy/Caddyfile
```
then tell me what it shows.

---

📍 On: the Proxmox host shell (root@pve) for commands; switch to the Proxmox web UI for the firewall step.

## Prerequisites
- LXC 120 (caddy-proxy) is running and Caddy is installed (already done).
- You have a public domain name (or can get a free one from DuckDNS).
- You can log into your home router's admin page.
- You know your home's public IP address (shown on DuckDNS or your router status page).

## Inputs needed
- `<PUBLIC_DOMAIN>` — your public domain (e.g., `myhomelab.duckdns.org`).
- `<CONTROL_PANEL_IP>` — the IP of LXC 111 (control panel). You will discover this in Phase B.
- Router admin credentials (to open ports 80 and 443).

## Run this

### Phase A — Outside setup (domain, DNS, router ports)

1. **Get a public domain.** If you don't have one, go to [DuckDNS](https://www.duckdns.org), sign in with Google/GitHub, create a subdomain (e.g., `myhomelab`), and note the public IP shown on the page. If you already have a domain, skip to step 2.
2. **Point your domain to your home's public IP.** In your domain provider's DNS settings, create an **A record** for your domain (or subdomain) pointing to `<PUBLIC_IP>`. For DuckDNS this is automatic.
3. **Open ports 80 and 443 on your router.** Log into your router's admin page (usually `http://192.168.1.1`). Find **Port Forwarding** (may be under Advanced or NAT). Add two rules:
   - TCP port **80** → `192.168.1.26` port **80**
   - TCP port **443** → `192.168.1.26` port **443**
   Save the rules.

**Checkpoint:** You should see both port-forward rules listed and enabled.

### Phase B — Proxy config (find control panel IP, update Caddyfile, reload)

1. **Find the control panel's IP.** On the Proxmox host shell, run:
```bash
pct exec 111 -- ip -4 addr show eth0 | grep inet
```
Note the IP address (e.g., `192.168.1.XX`). You will use it as `<CONTROL_PANEL_IP>`.

2. **Update the Caddyfile.** Replace `<PUBLIC_DOMAIN>` with your domain and `<CONTROL_PANEL_IP>` with the IP from step 1. Then run:
```bash
pct exec 120 -- sh -c 'cat > /etc/caddy/Caddyfile <<EOF
jellyfin.<PUBLIC_DOMAIN> {
    reverse_proxy 192.168.1.20:8096
}
prowlarr.<PUBLIC_DOMAIN> {
    reverse_proxy 192.168.1.21:9696
}
radarr.<PUBLIC_DOMAIN> {
    reverse_proxy 192.168.1.22:7878
}
sonarr.<PUBLIC_DOMAIN> {
    reverse_proxy 192.168.1.23:8989
}
panel.<PUBLIC_DOMAIN> {
    reverse_proxy <CONTROL_PANEL_IP>:3000
}
EOF'
```

3. **Reload Caddy:**
```bash
pct exec 120 -- systemctl reload caddy
```
You should see no error message.

**Checkpoint:** Caddy reloads without error.

### Phase C — Firewall and verify

1. **Apply the `dmz` security group to LXC 120.** Open the Proxmox web UI at `https://192.168.1.25:8006` and log in. Select container **120 (caddy-proxy)**. Go to the **Firewall** tab. Enable the firewall if it is not already on. Add the security group **`dmz`**. Apply the changes.
2. **Test HTTPS from outside your network.** On a phone using cellular data (or any device not on your home Wi-Fi), open a browser and go to:
```
https://jellyfin.<PUBLIC_DOMAIN>
```
You should see the Jellyfin login page with a padlock icon (valid certificate, no warning).

**Checkpoint:** The page loads over HTTPS with a valid certificate.

## Verify
- Run this on the Proxmox host shell to confirm Caddy obtained certificates:
```bash
pct exec 120 -- journalctl -u caddy --no-pager | grep -i "certificate"
```
You should see lines like `certificate obtained successfully` or `certificate installed`.
- In a browser, visit `https://panel.<PUBLIC_DOMAIN>` — the control panel should load with HTTPS.

## Rollback
If something fails, revert the Caddyfile to the previous HTTP-only config:
```bash
pct exec 120 -- sh -c 'cat > /etc/caddy/Caddyfile <<EOF
jellyfin.local {
    reverse_proxy 192.168.1.20:8096
}
prowlarr.local {
    reverse_proxy 192.168.1.21:9696
}
radarr.local {
    reverse_proxy 192.168.1.22:7878
}
sonarr.local {
    reverse_proxy 192.168.1.23:8989
}
panel.local {
    reverse_proxy 192.168.1.25:3000
}
EOF'
pct exec 120 -- systemctl reload caddy
```
Then remove the port-forward rules from your router.

## Risk
- Opening ports 80 and 443 exposes the Caddy proxy to the internet. The `dmz` security group limits access to only those ports, but ensure it is applied before testing.
- Changing the Caddyfile will temporarily break local `.local` access; the new public domains will work once DNS and ports are set.

## ✅ Done when
1. You can open `https://jellyfin.<PUBLIC_DOMAIN>` from outside your home network and see the Jellyfin login page with a valid padlock (no certificate warning).
2. Press **✓ Done → next step** (or type `next`).
3. If that page does not load or shows a certificate error, paste what you see instead.
