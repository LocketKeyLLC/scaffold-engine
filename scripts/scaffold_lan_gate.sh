#!/usr/bin/env bash
# §17.1418 — only SCAFFOLD_LAN_ALLOW may reach the engine on SCAFFOLD_LAN_ADDRESS:8000.
#
# A DOCKER-USER rule, because ufw cannot filter a Docker-published port. It
# matches the ORIGINAL destination (before Docker's DNAT), so loopback use of
# 127.0.0.1:8000 is untouched. Idempotent: run it as often as you like.
# Run by scripts/scaffold-lan-gate.service before docker.service starts.
set -euo pipefail
ENV_FILE=${1:?usage: scaffold_lan_gate.sh /path/to/scaffold-engine/.env}
val() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '"'"'"' '; }
ADDR=$(val SCAFFOLD_LAN_ADDRESS)
ALLOW=$(val SCAFFOLD_LAN_ALLOW)
PORT=8000
if [ -z "$ADDR" ] || [ -z "$ALLOW" ]; then
    echo "scaffold-lan-gate: SCAFFOLD_LAN_ADDRESS / SCAFFOLD_LAN_ALLOW not set in $ENV_FILE -- nothing to gate"
    exit 0
fi
# Docker keeps an existing DOCKER-USER chain; creating it here, before docker.service,
# means the rule is in place before any container publishes the port.
iptables -N DOCKER-USER 2>/dev/null || true
# `--ctdir ORIGINAL`: judge only packets travelling TOWARD the engine. The conntrack ORIGINAL tuple
# is the same for both directions of a connection, so without it the engine's REPLIES (source
# 172.18.x, "not ALLOW") matched too and every connection died -- the allowed one included.
# Measured 2026-10-07: CT 111 timed out, 30 packets on the DROP counter. (§17.1418b)
RULE=(-p tcp -m conntrack --ctdir ORIGINAL --ctorigdst "$ADDR" --ctorigdstport "$PORT" ! -s "$ALLOW" -j DROP)
OLD=(-p tcp -m conntrack --ctorigdst "$ADDR" --ctorigdstport "$PORT" ! -s "$ALLOW" -j DROP)
if iptables -C DOCKER-USER "${OLD[@]}" 2>/dev/null; then
    iptables -D DOCKER-USER "${OLD[@]}"
    echo "scaffold-lan-gate: removed the earlier rule that also dropped the engine's replies"
fi
if iptables -C DOCKER-USER "${RULE[@]}" 2>/dev/null; then
    echo "scaffold-lan-gate: already in place ($ADDR:$PORT, only $ALLOW)"
else
    iptables -I DOCKER-USER 1 "${RULE[@]}"
    echo "scaffold-lan-gate: installed ($ADDR:$PORT, only $ALLOW)"
fi
