#!/usr/bin/env bash
# Run this step's commands inside VM 106 through its QEMU guest agent: no ssh, no account, no key, no address.
set -uo pipefail
GID=106
qm status "$GID" | grep -q running || qm start "$GID"
for i in $(seq 1 12); do qm agent "$GID" ping >/dev/null 2>&1 && break; sleep 5; done
qm agent "$GID" ping >/dev/null 2>&1 || { echo "FAILED: the guest agent in VM $GID did not answer within 60 s"; exit 1; }
cat > /tmp/in_vm_106_remote.sh <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
[ -e "/etc/netplan/50-cloud-init.yaml.bak" ] && cp -a "/etc/netplan/50-cloud-init.yaml.bak" "/etc/netplan/50-cloud-init.yaml.bak.bak.$(date +%Y%m%d%H%M%S)"
cp /etc/netplan/50-cloud-init.yaml /etc/netplan/50-cloud-init.yaml.bak
[ -e "/etc/netplan/50-cloud-init.yaml" ] && cp -a "/etc/netplan/50-cloud-init.yaml" "/etc/netplan/50-cloud-init.yaml.bak.$(date +%Y%m%d%H%M%S)"
cat > /etc/netplan/50-cloud-init.yaml <<'EOF'
network:
    version: 2
    ethernets:
        eth0:
            match:
                macaddress: bc:24:11:e8:9f:7a
            set-name: eth0
            dhcp4: true
            nameservers:
                addresses:
                - 192.168.1.1
EOF
netplan apply
REMOTE

# §17.1317 — long work runs DETACHED inside the guest as a transient systemd unit and is
# waited on across several commands, each inside the runner's 180 s budget. Live, T23's
# SteamCMD install (several GB) could never fit one command; `qm guest exec --timeout 110`
# would have cut it the same way ssh did.
UNIT="scaffold-ADD154"
# §17.1319 -- the agent answers JSON {exitcode, out-data}; every read below wants the TEXT inside it. Live, the
# first detached run compared `systemctl is-active` against the JSON blob and `exit`ed on one ("numeric argument required").
ginfo() { qm guest exec "$GID" --timeout 60 -- bash -c "$1" 2>/dev/null | python3 -c "import json,sys
t=sys.stdin.read()
try: d=json.loads(t or '{}')
except Exception: d={}
sys.stdout.write(d.get('out-data') or '')"; }
case "${1:-start}" in
  start)
    qm guest exec "$GID" --timeout 60 --pass-stdin 1 -- bash -c "cat > /root/.scaffold_step.sh" < /tmp/in_vm_106_remote.sh >/dev/null \
      || { echo "FAILED: could not write the script into VM $GID through the agent"; exit 1; }
    ginfo "systemctl stop $UNIT 2>/dev/null; systemctl reset-failed $UNIT 2>/dev/null; rm -f /root/.scaffold_step.log /root/.scaffold_step.err /root/.scaffold_step.reported; true" >/dev/null
    ginfo "systemd-run --unit $UNIT --collect -p StandardOutput=append:/root/.scaffold_step.log -p StandardError=append:/root/.scaffold_step.err bash /root/.scaffold_step.sh" >/dev/null \
      || { echo "FAILED: could not start $UNIT inside $GID (is systemd running in the guest?)"; exit 1; }
    echo "started $UNIT inside $GID"
    ;;&
  start|wait|last)
    deadline=$(( SECONDS + 165 ))
    while [ "$SECONDS" -lt "$deadline" ]; do
      state="$(ginfo "systemctl is-active $UNIT" | tr -d '[:space:]')"
      case "$state" in
        active|activating|reloading|deactivating) sleep 5 ;;
        *) break ;;
      esac
    done
    state="$(ginfo "systemctl is-active $UNIT" | tr -d '[:space:]')"
    if [ "$state" = "active" ] || [ "$state" = "activating" ]; then
      if [ "${1:-start}" = "last" ]; then echo "FAILED: $UNIT is still running after the whole wait budget; the step did not finish"; ginfo "tail -n 20 /root/.scaffold_step.log"; exit 1; fi
      echo "STILL RUNNING: $UNIT inside $GID (waited 165 s more; the next command keeps waiting)"; ginfo "tail -n 3 /root/.scaffold_step.log"; exit 0
    fi
    result="$(ginfo "systemctl show -p Result --value $UNIT" | tr -d '[:space:]')"
    code="$(ginfo "systemctl show -p ExecMainStatus --value $UNIT" | tr -d '[:space:]')"
    # §17.1327 -- the log belongs to the phase that SAW the unit finish. Live, T23's eight
    # wait phases each re-printed the whole SteamCMD tail; the record carried it eight times.
    if [ -z "$(ginfo "cat /root/.scaffold_step.reported 2>/dev/null")" ]; then
      ginfo "echo done > /root/.scaffold_step.reported" >/dev/null
      ginfo "cat /root/.scaffold_step.log 2>/dev/null | tail -n 60"
      ginfo "cat /root/.scaffold_step.err 2>/dev/null | tail -n 20" >&2
    else
      echo "(the log was printed by the phase that saw $UNIT finish)"
    fi
    if [ "$result" = "success" ] || { [ -z "$result" ] && [ "$code" = "0" ]; }; then echo "$UNIT finished (exit ${code:-0})"; exit 0; fi
    echo "FAILED: $UNIT ended with Result=$result exit=${code:-?}"; exit "${code:-1}"
    ;;
  *) echo "unknown phase: $1"; exit 2;;
esac
