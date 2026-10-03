"""§17.1290 — templates the drafter fills (design Phase 2a).

The second structural defect of 2026-10-02: the drafter re-invented the script
on every reask, and ~28 refusal markers chased the ways prose can get a known
shape wrong. The shapes recur — reach a VM over ssh, wait for a guest to
appear, run commands inside a container, install an OS unattended — so the
engine owns them here as deterministic, parameterized, PRE-GATED templates.
The model chooses nothing about the shape; it fills the one free parameter a
template has (the commands to run inside the guest), and the engine renders the
runbook in the exact form `frame_run` already parses. Every template passes
every gate — `tests/test_runbook_templates.py` renders each with dummy values
through `frame_run` and asserts ``refused == []`` — so a gate that refuses a
template is a template bug, fixed in one place.

The §17.1288 lessons are baked into the scripts: the guarded start, the
sweep before `ip neigh`, `|| true` on every lookup a wait expects to be empty,
`SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id`, sudo on stdin, the account
as a placeholder named after the guest, no fixed-address wait, the password
by name through the environment.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("scaffold")

TEMPLATE_MARK = "<!-- runbook-template:"
_TEMPLATE_MARK_RE = re.compile(r"<!-- runbook-template: ([a-z_]+) -->")
#: §17.1297 — the OS install is the VERB and the OS within one short span ("Install Ubuntu 22.04 on VM 106",
#: "install an operating system"), not any step whose text has "install" somewhere and "Ubuntu" elsewhere:
#: ADD65 "Verify QEMU Guest Agent responds on VM 106" was templated as an OS install.
_INSTALL_OS_RE = re.compile(
    r"\b(?:re)?install(?:ing|ed)?\s+(?:the\s+|an?\s+|a\s+fresh\s+)?(?:ubuntu|debian|operating system|os\b|cloud image|"
    r"ubuntu server \d\d\.\d\d|server \d\d\.\d\d)", re.I)
#: host-side work ON a guest — the drafter's normal path handles these (a `qm set`, a resize, a start)
_HOST_SIDE_RE = re.compile(
    # §17.1304 — `resize` alone matched "resize2fs" in a step done INSIDE the guest (ADD84) and
    # pushed it off the agent template; host-side is resizing the DISK (`qm resize`, "resize VM 106's disk").
    r"\b(?:(?:qm|pct) resize|resize (?:the )?(?:vm \d+'?s? )?(?:disk|volume)|grow (?:the )?(?:vm \d+'?s? )?disk|boot order|attach|detach|re-attach|passthrough|hostpci|"
    r"snapshot|backup|clone|destroy|delete (?:the )?(?:vm|container)|create (?:a |the )?(?:vm|container|lxc)|"
    r"(?:start|stop|reboot|shutdown) (?:the )?(?:vm|container|ct|lxc)\b|set .*\b(?:cpu|cores|memory|ram)\b)", re.I)
_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
#: §17.1291 — a step about the guest's CONSOLE is host-side work on its serial socket, not an ssh
#: §17.1298 — the console is the TASK ("read VM 106's serial console", "peek at the console"), not a word in
#: the step's history: ADD82's description says "done at the console" (stale, corrected) and was templated as a
#: console read. The verb and the console within one span, or the socket itself.
_CONSOLE_RE = re.compile(
    r"\b(?:read|reading|capture|capturing|watch|watching|peek at|look at|show|print|dump)\b.{0,40}\b(?:serial )?console\b"
    r"|\bconsole (?:output|log|screen)\b|\bserial0: socket\b|\bsocat\b|qemu-server/\d+\.serial", re.I)
#: §17.1292 — "capture the boot", "boot log", "reset … console": the console WHILE the guest boots
_BOOT_WATCH_RE = re.compile(r"\b(?:boot (?:log|console|messages)|capture .{0,40}boot|watch .{0,40}boot|reset .{0,60}console)\b", re.I)
_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.S)


@dataclass
class Param:
    name: str
    source: str            # "subject" | "truth" | "operator" | "model" | "default"
    hint: str = ""
    default: str = ""


@dataclass
class Template:
    name: str
    title: str
    applies: Callable[[dict, Optional[object]], bool]
    params: list[Param]
    files: dict[str, str]              # path -> body, with {NAME} placeholders
    run: str | list[str]               # the command(s) under ## Run this -- each gets the runner's 180 s budget
    verify: list[str] = field(default_factory=list)
    risk: str = ""


# ───── the shared wait: sweep the bridge's /24, read the neighbour table for the MAC, up to 12 × 10 s
WAIT_FOR_ADDRESS = r'''
wait_for_address() {
    # $1 = MAC (any case), $2 = seconds to wait (default 130: a supervised command has 180). Prints the address, or nothing.
    local mac net ip deadline
    mac="$(printf '%s' "$1" | tr 'A-F' 'a-f')"
    deadline=$(( SECONDS + ${2:-130} ))
    net="$(ip -4 route get 1 | sed -n 's/.* src \([0-9.]*\)\.[0-9]*.*/\1/p' || true)"
    while [ "$SECONDS" -lt "$deadline" ]; do
        if command -v nmap >/dev/null 2>&1; then
            # §17.1295 -- nmap's ARP scan uses its OWN raw sockets: it never populates the kernel's
            # neighbour table, so `ip neigh` after it stayed empty while the guest answered (live, VM 106
            # at 192.168.1.106 for an hour). Read nmap's own report for the MAC instead.
            ip="$(nmap -sn "$net.0/24" 2>/dev/null | tr -d '\r' | awk -v m="$mac" '
                /^Nmap scan report for/ { ip=$NF; gsub(/[()]/, "", ip) }
                tolower($0) ~ "mac address: " m { print ip; exit }' || true)"
        else
            for h in $(seq 1 254); do ping -c 1 -W 1 "$net.$h" >/dev/null 2>&1 & done; wait
            ip="$(ip neigh show | grep -i "$mac" | awk '{print $1}' | head -n 1 || true)"
        fi
        if [ -n "$ip" ]; then printf '%s\n' "$ip"; return 0; fi
        sleep 8
    done
    return 1
}
'''.strip("\n")


INSTALL_OS_CLOUDINIT = Template(
    name="install_os_cloudinit",
    title="Install Ubuntu unattended on VM {GID} (cloud image + cloud-init)",
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and bool(_INSTALL_OS_RE.search(_text(node))),
    params=[
        Param("GID", "subject"),
        Param("GUEST_USER", "operator", "the account to create inside the guest (the operator logs in with it; sudo)"),
        Param("IMAGE_URL", "default", default="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"),
        Param("IMAGE_FILE", "default", default="/var/lib/vz/template/jammy-server-cloudimg-amd64.img"),
        Param("DISK_SIZE", "default", default="100G"),
        Param("NAMESERVER", "default", default="192.168.1.1"),
        Param("IPCONFIG", "pin", default="ip=dhcp"),   # §17.1290d — a pinned <GUEST>_IP becomes ip=<addr>/24,gw=<GATEWAY>
        Param("GATEWAY", "default", default="192.168.1.1"),
        Param("PUBKEY", "default", default="/root/.ssh/id_rsa.pub"),
        Param("EXPECT", "default", default="Ubuntu 22.04"),
    ],
    files={"/tmp/install_os_{GID}.sh": r"""#!/usr/bin/env bash
# Unattended Ubuntu install on VM {GID}: cloud image + the Proxmox cloud-init drive. No console at any point.
# §17.1290c -- in PHASES, one command each, because a supervised command has 180 seconds: prepare | swap | boot | check.
set -uo pipefail
GID={GID}
IMAGE_URL="{IMAGE_URL}"
IMAGE_FILE="{IMAGE_FILE}"
DISK_SIZE="{DISK_SIZE}"
USER_NAME="${GUEST_USER}"
PUBKEY="{PUBKEY}"
PHASE="${1:-all}"

""" + WAIT_FOR_ADDRESS + r"""

vm_mac() { qm config "$GID" | sed -n 's/^net0: [a-z0-9]*=\([0-9A-Fa-f:]*\).*/\1/p' | head -n 1 || true; }

phase_prepare() {
    # the image, once; resumable, bounded to the command's budget -- an incomplete download is kept and finished on the next run
    if [ -s "$IMAGE_FILE" ] && [ "$(stat -c %s "$IMAGE_FILE")" -gt 300000000 ] && qemu-img info "$IMAGE_FILE" >/dev/null 2>&1; then
        echo "image present: $IMAGE_FILE"; return 0
    fi
    curl -fL -C - --retry 2 --max-time 165 -o "$IMAGE_FILE" "$IMAGE_URL"
    rc=$?
    if [ "$rc" -ne 0 ] || ! qemu-img info "$IMAGE_FILE" >/dev/null 2>&1; then
        echo "download incomplete (curl exit $rc; $(stat -c %s "$IMAGE_FILE" 2>/dev/null || echo 0) bytes kept): run this step again to resume"
        return 1
    fi
    echo "image downloaded: $IMAGE_FILE"
}

phase_swap() {
    # §17.1290d -- idempotency FIRST: a rerun must never stop a guest whose swap is already done (live, it did,
    # mid-first-boot). The cloud-init drive is the last thing this phase writes, so its presence means all of it.
    if qm config "$GID" | grep -q '^ide2: local-lvm:vm-'"$GID"'-cloudinit'; then
        if qm config "$GID" | grep -qxF 'ipconfig0: {IPCONFIG}'; then
            echo "cloud-init drive already present: the swap was done on an earlier run"; return 0
        fi
        # the address plan changed (dhcp -> a pinned static, say): reseed and let the boot phase start it afresh
        qm set "$GID" --ipconfig0 "{IPCONFIG}" >/dev/null || { echo "FAILED: qm set ipconfig0"; return 1; }
        qm status "$GID" | grep -q running && qm stop "$GID"
        echo "cloud-init reseeded with ipconfig0 {IPCONFIG}; the boot phase starts VM $GID again"; return 0
    fi
    # the VM must be stopped to swap its disk
    qm status "$GID" | grep -q running && qm stop "$GID"
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do qm status "$GID" | grep -q stopped && break; sleep 5; done
    OLD_DISK="$(qm config "$GID" | sed -n 's/^scsi0: \([^,]*\),.*/\1/p' | head -n 1 || true)"
    qm importdisk "$GID" "$IMAGE_FILE" local-lvm >/dev/null || { echo "FAILED: qm importdisk"; return 1; }
    NEW_DISK="$(qm config "$GID" | sed -n 's/^unused[0-9]*: \(local-lvm:vm-'"$GID"'-disk-[0-9]*\)$/\1/p' | tail -n 1 || true)"
    if [ -z "$NEW_DISK" ]; then echo "FAILED: the imported disk did not appear as unusedN in qm config $GID"; return 1; fi
    echo "imported $NEW_DISK (old boot disk: ${OLD_DISK:-none})"
    qm set "$GID" --scsihw virtio-scsi-pci --scsi0 "$NEW_DISK" --boot order=scsi0 >/dev/null || { echo "FAILED: qm set scsi0"; return 1; }
    qm resize "$GID" scsi0 "$DISK_SIZE" >/dev/null || { echo "FAILED: qm resize"; return 1; }
    if [ -n "$OLD_DISK" ] && [ "$OLD_DISK" != "$NEW_DISK" ]; then
        OLD_SLOT="$(qm config "$GID" | grep -F ": $OLD_DISK" | grep -oE '^unused[0-9]+' | head -n 1 || true)"
        [ -n "$OLD_SLOT" ] && qm set "$GID" --delete "$OLD_SLOT" >/dev/null
        pvesm free "$OLD_DISK" >/dev/null 2>&1 || true
    fi
    # the cloud-init drive and its seed: account, password by NAME, this host's key, DHCP
    qm set "$GID" --ide2 local-lvm:cloudinit --ciuser "$USER_NAME" --cipassword "$MASS_PASSWORD" \
        --sshkeys "$PUBKEY" --ipconfig0 "{IPCONFIG}" --nameserver {NAMESERVER} --agent 1 --serial0 socket --vga serial0 >/dev/null \
        || { echo "FAILED: qm set cloud-init"; return 1; }
    echo "disk swapped and cloud-init seeded on VM $GID"
}

phase_boot() {
    qm status "$GID" | grep -q running || qm start "$GID" || { echo "FAILED: qm start"; return 1; }
    MAC="$(vm_mac)"
    IP="$(wait_for_address "$MAC" || true)"
    if [ -z "$IP" ]; then echo "FAILED: VM $GID (MAC $MAC) did not appear on the network"; return 1; fi
    echo "VM $GID is at $IP"
}

phase_check() {
    MAC="$(vm_mac)"
    IP="$(wait_for_address "$MAC" 40 || true)"
    if [ -z "$IP" ]; then echo "FAILED: VM $GID (MAC $MAC) is not on the network"; return 1; fi
    OUT=""
    while [ "$SECONDS" -lt 150 ]; do
        OUT="$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" 'lsb_release -ds' 2>/dev/null || true)"
        if [ -n "$OUT" ]; then break; fi
        sleep 10
    done
    case "$OUT" in *"{EXPECT}"*) echo "OK: $OUT on VM $GID at $IP";; *) echo "FAILED: ssh to $USER_NAME@$IP did not answer with {EXPECT} (got: ${OUT:-nothing})"; return 1;; esac
}

case "$PHASE" in
    prepare) phase_prepare;;
    swap)    phase_swap;;
    boot)    phase_boot;;
    check)   phase_check;;
    all)     phase_prepare && phase_swap && phase_boot && phase_check;;
    *)       echo "unknown phase: $PHASE"; exit 2;;
esac
"""},
    run=[f'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<{{GUEST_USER_NAME}}>" bash /tmp/install_os_{{GID}}.sh {ph}'
         for ph in ("prepare", "swap", "boot", "check")],
    verify=["qm config {GID} | grep -E '^(scsi0|ide2|boot):'", "qm status {GID}"],
    risk="Stops VM {GID}, replaces its (never-written) boot disk with the imported cloud image, frees the old volume.",
)


REACH_VM_SSH_AND_RUN = Template(
    name="reach_vm_ssh_and_run",
    title="Run commands inside VM {GID} over ssh",
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and not _INSTALL_OS_RE.search(_text(node))
                                and not _HOST_SIDE_RE.search(_text(node)) and not _CONSOLE_RE.search(_text(node))
                                and not getattr(truth, "agent", False),
    params=[
        Param("GID", "subject"),
        Param("GUEST_USER", "operator", "the account inside the guest that has sudo (the operator logs in with it)"),
        Param("REMOTE_COMMANDS", "model", "the commands to run inside the guest, as root, one per line"),
        Param("NEEDS_KEY", "truth", default="yes"),
    ],
    files={"/tmp/in_vm_{GID}.sh": r'''#!/usr/bin/env bash
# Reach VM {GID} over ssh and run this step's commands inside it as root.
set -uo pipefail
GID={GID}
USER_NAME="${GUEST_USER}"

''' + WAIT_FOR_ADDRESS + r'''

qm status "$GID" | grep -q running || qm start "$GID"
MAC="$(qm config "$GID" | sed -n 's/^net0: [a-z0-9]*=\([0-9A-Fa-f:]*\).*/\1/p' | head -n 1 || true)"
IP="$(wait_for_address "$MAC" || true)"
if [ -z "$IP" ]; then echo "FAILED: VM $GID (MAC $MAC) did not appear on the network"; exit 1; fi
echo "VM $GID is at $IP"
# §17.1299 -- probe key auth FIRST: a cloud-init install already seeded this host's key (--sshkeys) and a cloud
# image refuses password auth, so a blind ssh-copy-id would fail on a guest that is perfectly reachable.
if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" true >/dev/null 2>&1; then
    echo "key auth works for $USER_NAME@$IP"
elif [ "{NEEDS_KEY}" = "yes" ]; then
    command -v sshpass >/dev/null 2>&1 || apt-get install -y sshpass >/dev/null
    SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" >/dev/null \
        || { echo "FAILED: ssh-copy-id to $USER_NAME@$IP was refused (wrong account or password, or password auth is off in the guest)"; exit 1; }
else
    echo "FAILED: key auth to $USER_NAME@$IP was refused and no password path is allowed here"; exit 1
fi
cat > /tmp/in_vm_{GID}_remote.sh <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
{REMOTE_COMMANDS}
REMOTE
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" "sudo -S -p '' bash -s" <<< "$MASS_PASSWORD
$(cat /tmp/in_vm_{GID}_remote.sh)"
'''},
    run='MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<{GUEST_USER_NAME}>" bash /tmp/in_vm_{GID}.sh',
    verify=["qm status {GID}"],
    risk="Runs this step's commands as root inside VM {GID}; copies this host's key into the guest first.",
)


#: §17.1303 — a VM whose guest agent answers is reached through the agent: no ssh, no
#: account, no key, no address. Live, ADD84 (agent up since ADD82) drew an ssh block to
#: `<PALWORLD_USER>@192.168.1.127` -- another guest's old address, with a placeholder no
#: pin filled -- because REACH_VM_SSH_AND_RUN steps aside once the agent is up and
#: nothing owned that shape. The verify runs INSIDE the guest too, so §17.1302 can read it.
RUN_IN_VM_VIA_AGENT = Template(
    name="run_in_vm_via_agent",
    title="Run commands inside VM {GID} through its guest agent",
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and bool(getattr(truth, "agent", False))
                                and not _INSTALL_OS_RE.search(_text(node)) and not _HOST_SIDE_RE.search(_text(node))
                                and not _CONSOLE_RE.search(_text(node)) and not _BOOT_WATCH_RE.search(_text(node)),
    params=[
        Param("GID", "subject"),
        Param("STEP", "node_key"),
        Param("REMOTE_COMMANDS", "model", "the commands to run inside the guest, as root, one per line"),
        Param("VERIFY_INSIDE", "model", "ONE read-only command to run inside the guest whose output shows this step's "
                                        "goal is met (for example `df -h /`, `systemctl is-active nginx`, `dpkg -l curl`)"),
    ],
    files={"/tmp/in_vm_{GID}_agent.sh": '#!/usr/bin/env bash\n# Run this step\'s commands inside VM {GID} through its QEMU guest agent: no ssh, no account, no key, no address.\nset -uo pipefail\nGID={GID}\nqm status "$GID" | grep -q running || qm start "$GID"\nfor i in $(seq 1 12); do qm agent "$GID" ping >/dev/null 2>&1 && break; sleep 5; done\nqm agent "$GID" ping >/dev/null 2>&1 || { echo "FAILED: the guest agent in VM $GID did not answer within 60 s"; exit 1; }\ncat > /tmp/in_vm_{GID}_remote.sh <<\'REMOTE\'\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n{REMOTE_COMMANDS}\nREMOTE\n\n# §17.1317 — long work runs DETACHED inside the guest as a transient systemd unit and is\n# waited on across several commands, each inside the runner\'s 180 s budget. Live, T23\'s\n# SteamCMD install (several GB) could never fit one command; `qm guest exec --timeout 110`\n# would have cut it the same way ssh did.\nUNIT="scaffold-{STEP}"\n# §17.1319 -- the agent answers JSON {exitcode, out-data}; every read below wants the TEXT inside it. Live, the\n# first detached run compared `systemctl is-active` against the JSON blob and `exit`ed on one ("numeric argument required").\nginfo() { qm guest exec "$GID" --timeout 60 -- bash -c "$1" 2>/dev/null | python3 -c "import json,sys\nt=sys.stdin.read()\ntry: d=json.loads(t or \'{}\')\nexcept Exception: d={}\nsys.stdout.write(d.get(\'out-data\') or \'\')"; }\ncase "${1:-start}" in\n  start)\n    qm guest exec "$GID" --timeout 60 --pass-stdin 1 -- bash -c "cat > /root/.scaffold_step.sh" < /tmp/in_vm_{GID}_remote.sh >/dev/null \\\n      || { echo "FAILED: could not write the script into VM $GID through the agent"; exit 1; }\n    ginfo "systemctl stop $UNIT 2>/dev/null; systemctl reset-failed $UNIT 2>/dev/null; rm -f /root/.scaffold_step.log /root/.scaffold_step.err /root/.scaffold_step.reported; true" >/dev/null\n    ginfo "systemd-run --unit $UNIT --collect -p StandardOutput=append:/root/.scaffold_step.log -p StandardError=append:/root/.scaffold_step.err bash /root/.scaffold_step.sh" >/dev/null \\\n      || { echo "FAILED: could not start $UNIT inside $GID (is systemd running in the guest?)"; exit 1; }\n    echo "started $UNIT inside $GID"\n    ;;&\n  start|wait|last)\n    deadline=$(( SECONDS + 165 ))\n    while [ "$SECONDS" -lt "$deadline" ]; do\n      state="$(ginfo "systemctl is-active $UNIT" | tr -d \'[:space:]\')"\n      case "$state" in\n        active|activating|reloading|deactivating) sleep 5 ;;\n        *) break ;;\n      esac\n    done\n    state="$(ginfo "systemctl is-active $UNIT" | tr -d \'[:space:]\')"\n    if [ "$state" = "active" ] || [ "$state" = "activating" ]; then\n      if [ "${1:-start}" = "last" ]; then echo "FAILED: $UNIT is still running after the whole wait budget; the step did not finish"; ginfo "tail -n 20 /root/.scaffold_step.log"; exit 1; fi\n      echo "STILL RUNNING: $UNIT inside $GID (waited 165 s more; the next command keeps waiting)"; ginfo "tail -n 3 /root/.scaffold_step.log"; exit 0\n    fi\n    result="$(ginfo "systemctl show -p Result --value $UNIT" | tr -d \'[:space:]\')"\n    code="$(ginfo "systemctl show -p ExecMainStatus --value $UNIT" | tr -d \'[:space:]\')"\n    # §17.1327 -- the log belongs to the phase that SAW the unit finish. Live, T23\'s eight\n    # wait phases each re-printed the whole SteamCMD tail; the record carried it eight times.\n    if [ -z "$(ginfo "cat /root/.scaffold_step.reported 2>/dev/null")" ]; then\n      ginfo "echo done > /root/.scaffold_step.reported" >/dev/null\n      ginfo "cat /root/.scaffold_step.log 2>/dev/null | tail -n 60"\n      ginfo "cat /root/.scaffold_step.err 2>/dev/null | tail -n 20" >&2\n    else\n      echo "(the log was printed by the phase that saw $UNIT finish)"\n    fi\n    if [ "$result" = "success" ] || { [ -z "$result" ] && [ "$code" = "0" ]; }; then echo "$UNIT finished (exit ${code:-0})"; exit 0; fi\n    echo "FAILED: $UNIT ended with Result=$result exit=${code:-?}"; exit "${code:-1}"\n    ;;\n  *) echo "unknown phase: $1"; exit 2;;\nesac\n'},
    run=['bash /tmp/in_vm_{GID}_agent.sh start', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh wait', 'bash /tmp/in_vm_{GID}_agent.sh last'],   # §17.1317 — start, then wait x7, then last
    # §17.1304 — the inside check ALONE: `qm agent ping` prints nothing, and a check that cannot speak to the
    # goal is an `unknown` that keeps §17.1302 from recording an already-met step.
    verify=['qm guest exec {GID} -- bash -c "{VERIFY_INSIDE}"'],
    risk="Runs this step's commands as root inside VM {GID} through its guest agent.",
)


#: §17.1322 — a Steam dedicated server (Palworld, Valheim, …) is one shape: SteamCMD as a dedicated
#: user, bootstrap, `app_update <id>`, a unit on the start script the download leaves. Live, the agent
#: draw flipped between this and Ubuntu's `steamcmd` package (licence prompt, no game) four times in a
#: row once its own failed attempt was in view. The engine owns the shape; the model fills nothing unless
#: the app id is in nothing the engine holds.
_STEAM_RE = re.compile(r"\b(?:steamcmd|steam\s+(?:dedicated\s+)?server|dedicated\s+server|app_update|palworld|valheim|satisfactory|"
                       r"rust\s+server|ark\s+(?:server|survival)|cs2\s+server|project\s+zomboid|v\s*rising|enshrouded|7\s*days\s+to\s+die)\b", re.I)
_APP_ID_RE = re.compile(r"(?:app_update|app\s*id|appid)\D{0,12}(\d{4,8})", re.I)
_KNOWN_APPS = {"palworld": "2394010", "valheim": "896660", "satisfactory": "1690800", "rust": "258550", "enshrouded": "2278520",
               "ark": "376030", "zomboid": "380870", "v rising": "1829350", "7 days": "294420", "cs2": "730"}
_GAME_RE = re.compile(r"\b(palworld|valheim|satisfactory|rust|enshrouded|ark|zomboid|v\s*rising|7\s*days|cs2)\b", re.I)
_STEAM_TITLE_RE = re.compile(
    r"(?i)(?:\b(?:install|set\s*up|deploy)\b.*\b(?:steamcmd|dedicated\s+server|app_update)\b"
    r"|\b(?:install|set\s*up|deploy)\b.*\b(?:palworld|valheim|satisfactory|rust|enshrouded|ark|zomboid|v\s*rising|7\s*days|cs2)\b.*\bserver\b"
    r"|\b(?:palworld|valheim|satisfactory|rust|enshrouded|ark|zomboid|v\s*rising|7\s*days|cs2)\b.*\bserver\b.*\binstall)")


def derive_param(name: str, node: dict) -> str:
    """§17.1322 — APP_ID / GAME from the step's text, its last attempt's record and its research."""
    texts = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "output_text", "last_verification_reason", "research"))
    if name == "GAME":
        m = _GAME_RE.search(_text(node))
        return re.sub(r"\s+", "", m.group(1).lower()) if m else ""
    if name == "APP_ID":
        m = _APP_ID_RE.search(texts)
        if m:
            return m.group(1)
        g = _GAME_RE.search(_text(node))
        return _KNOWN_APPS.get(re.sub(r"\s+", " ", g.group(1).lower()), "") if g else ""
    return ""


STEAM_REMOTE = '# §17.1320 -- repair what a failed attempt left in dpkg before any apt work\ndpkg --configure -a >/dev/null 2>&1 || true\nfor _p in $(dpkg -l 2>/dev/null | awk \'/^(i[^i ]|[^i ]i|rF|rH|rU)/ {print $2}\'); do dpkg --purge --force-remove-reinstreq "$_p" >/dev/null 2>&1 || true; done\ndpkg --add-architecture i386\napt-get update\napt-get install -y lib32gcc-s1 lib32stdc++6 curl tar\nid -u steam >/dev/null 2>&1 || useradd -m -s /bin/bash steam\ninstall -d -o steam -g steam /opt/steamcmd {INSTALL_DIR}\nchown -R steam:steam {INSTALL_DIR}   # §17.1323 -- an earlier attempt may have left root-owned files under it; the install user writes here\nif [ ! -x /opt/steamcmd/steamcmd.sh ]; then\n  curl -sSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xz -C /opt/steamcmd\n  chown -R steam:steam /opt/steamcmd\nfi\nsu - steam -c \'/opt/steamcmd/steamcmd.sh +quit >/dev/null 2>&1 || true\'   # bootstrap: a fresh SteamCMD updates itself and exits\nsu - steam -c \'/opt/steamcmd/steamcmd.sh +force_install_dir {INSTALL_DIR} +login anonymous +app_update {APP_ID} validate +quit\'\nSTART="$(ls {INSTALL_DIR}/*.sh 2>/dev/null | head -n 1)"\n[ -n "$START" ] || { echo "FAILED: app {APP_ID} installed nothing runnable under {INSTALL_DIR}"; exit 1; }\nchmod +x "$START"\n[ -e /etc/systemd/system/{GAME}.service ] && cp -a /etc/systemd/system/{GAME}.service "/etc/systemd/system/{GAME}.service.bak.$(date +%Y%m%d%H%M%S)"\ncat > /etc/systemd/system/{GAME}.service <<EOF\n[Unit]\nDescription={GAME} dedicated server (Steam app {APP_ID})\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nUser=steam\nGroup=steam\nWorkingDirectory={INSTALL_DIR}\nExecStart=$START\nRestart=on-failure\nRestartSec=10\nLimitNOFILE=65535\n[Install]\nWantedBy=multi-user.target\nEOF\nsystemctl daemon-reload\nsystemctl enable {GAME}.service\necho "installed app {APP_ID} under {INSTALL_DIR}; start script $START; unit {GAME}.service enabled (not started)"'

INSTALL_STEAM_SERVER = Template(
    name="install_steam_server",
    title="Install the {GAME} dedicated server (Steam app {APP_ID}) inside VM {GID}",
    # the TITLE names the install of a game/dedicated server; a description that merely mentions the
    # guest "palworld-server" (ADD82: install the guest agent in it) does not make a step a game install
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and bool(getattr(truth, "agent", False))
                                and bool(_STEAM_TITLE_RE.search(str((node or {}).get("title") or "")))
                                and not _INSTALL_OS_RE.search(_text(node)),
    params=[
        Param("GID", "subject"),
        Param("STEP", "node_key"),
        Param("GAME", "derived", default="steamapp"),
        Param("INSTALL_DIR", "default", default="/opt/{GAME}"),
        Param("APP_ID", "derived", "the Steam app id of the DEDICATED SERVER build (not the game client); for Palworld it is 2394010"),
    ],
    files={"/tmp/in_vm_{GID}_agent.sh": RUN_IN_VM_VIA_AGENT.files["/tmp/in_vm_{GID}_agent.sh"].replace("{REMOTE_COMMANDS}", STEAM_REMOTE)},
    run=RUN_IN_VM_VIA_AGENT.run,
    verify=['qm guest exec {GID} -- bash -c "ls -la {INSTALL_DIR}/*.sh"'],
    risk="Installs SteamCMD and the dedicated server as user steam inside VM {GID}; writes and enables {GAME}.service (not started).",
)


RUN_IN_CONTAINER = Template(
    name="run_in_container",
    title="Run commands inside container {GID}",
    applies=lambda node, truth: _subject_kind(node, truth) == "ct" and not _HOST_SIDE_RE.search(_text(node)),
    params=[Param("GID", "subject"), Param("STEP", "node_key"), Param("REMOTE_COMMANDS", "model", "the commands to run inside the container, as root, one per line"),
            # §17.1307 — parity with run_in_vm_via_agent: a check INSIDE the guest is what §17.1302 can read
            Param("VERIFY_INSIDE", "model", "ONE read-only command to run inside the container whose output shows this step's "
                                            "goal is met (for example `caddy validate --config /etc/caddy/Caddyfile`, `systemctl is-active caddy`)")],
    files={"/tmp/in_ct_{GID}.sh": '#!/usr/bin/env bash\n# Run this step\'s commands inside container {GID} as root.\nset -uo pipefail\nGID={GID}\npct status "$GID" | grep -q running || pct start "$GID"\nfor i in 1 2 3 4 5 6 7 8 9 10 11 12; do pct status "$GID" | grep -q running && break; sleep 5; done\ncat > /tmp/in_ct_{GID}_remote.sh <<\'REMOTE\'\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n{REMOTE_COMMANDS}\nREMOTE\n\n# §17.1317 — long work runs DETACHED inside the guest as a transient systemd unit and is\n# waited on across several commands, each inside the runner\'s 180 s budget. Live, T23\'s\n# SteamCMD install (several GB) could never fit one command; `qm guest exec --timeout 110`\n# would have cut it the same way ssh did.\nUNIT="scaffold-{STEP}"\nginfo() { pct exec "$GID" -- bash -c "$1" 2>/dev/null; }\ncase "${1:-start}" in\n  start)\n    pct push "$GID" /tmp/in_ct_{GID}_remote.sh /root/.scaffold_step.sh >/dev/null || { echo "FAILED: pct push into $GID"; exit 1; }\n    ginfo "systemctl stop $UNIT 2>/dev/null; systemctl reset-failed $UNIT 2>/dev/null; rm -f /root/.scaffold_step.log /root/.scaffold_step.err /root/.scaffold_step.reported; true" >/dev/null\n    ginfo "systemd-run --unit $UNIT --collect -p StandardOutput=append:/root/.scaffold_step.log -p StandardError=append:/root/.scaffold_step.err bash /root/.scaffold_step.sh" >/dev/null \\\n      || { echo "FAILED: could not start $UNIT inside $GID (is systemd running in the guest?)"; exit 1; }\n    echo "started $UNIT inside $GID"\n    ;;&\n  start|wait|last)\n    deadline=$(( SECONDS + 165 ))\n    while [ "$SECONDS" -lt "$deadline" ]; do\n      state="$(ginfo "systemctl is-active $UNIT" | tr -d \'[:space:]\')"\n      case "$state" in\n        active|activating|reloading|deactivating) sleep 5 ;;\n        *) break ;;\n      esac\n    done\n    state="$(ginfo "systemctl is-active $UNIT" | tr -d \'[:space:]\')"\n    if [ "$state" = "active" ] || [ "$state" = "activating" ]; then\n      if [ "${1:-start}" = "last" ]; then echo "FAILED: $UNIT is still running after the whole wait budget; the step did not finish"; ginfo "tail -n 20 /root/.scaffold_step.log"; exit 1; fi\n      echo "STILL RUNNING: $UNIT inside $GID (waited 165 s more; the next command keeps waiting)"; ginfo "tail -n 3 /root/.scaffold_step.log"; exit 0\n    fi\n    result="$(ginfo "systemctl show -p Result --value $UNIT" | tr -d \'[:space:]\')"\n    code="$(ginfo "systemctl show -p ExecMainStatus --value $UNIT" | tr -d \'[:space:]\')"\n    # §17.1327 -- the log belongs to the phase that SAW the unit finish. Live, T23\'s eight\n    # wait phases each re-printed the whole SteamCMD tail; the record carried it eight times.\n    if [ -z "$(ginfo "cat /root/.scaffold_step.reported 2>/dev/null")" ]; then\n      ginfo "echo done > /root/.scaffold_step.reported" >/dev/null\n      ginfo "cat /root/.scaffold_step.log 2>/dev/null | tail -n 60"\n      ginfo "cat /root/.scaffold_step.err 2>/dev/null | tail -n 20" >&2\n    else\n      echo "(the log was printed by the phase that saw $UNIT finish)"\n    fi\n    if [ "$result" = "success" ] || { [ -z "$result" ] && [ "$code" = "0" ]; }; then echo "$UNIT finished (exit ${code:-0})"; exit 0; fi\n    echo "FAILED: $UNIT ended with Result=$result exit=${code:-?}"; exit "${code:-1}"\n    ;;\n  *) echo "unknown phase: $1"; exit 2;;\nesac\n'},
    run=['bash /tmp/in_ct_{GID}.sh start', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh wait', 'bash /tmp/in_ct_{GID}.sh last'],   # §17.1317
    verify=['pct exec {GID} -- bash -c "{VERIFY_INSIDE}"'],   # §17.1307 — inside, alone (see run_in_vm_via_agent)
    risk="Runs this step's commands as root inside container {GID}.",
)


READ_GUEST_CONSOLE = Template(
    name="read_guest_console",
    title="Read VM {GID}'s serial console",
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and bool(_CONSOLE_RE.search(_text(node)))
                                and not _BOOT_WATCH_RE.search(_text(node)),
    params=[Param("GID", "subject")],
    files={},
    # §17.1291 — one newline in (harmless at a login prompt or a boot log), six seconds of the screen out.
    # Live: VM 106 booted Ubuntu from its new disk and had no address under DHCP or a static seed; the
    # console is the only witness the host holds, and `serial0: socket` is how it is read.
    run="printf '\\n' | timeout 8 socat -t 6 - UNIX-CONNECT:/var/run/qemu-server/{GID}.serial0",
    verify=["qm config {GID} | grep -E '^serial0:'"],
    risk="Sends one newline to VM {GID}'s serial console and prints what the guest shows for six seconds. Types nothing else.",
)


WATCH_GUEST_BOOT = Template(
    name="watch_guest_boot",
    title="Reset VM {GID} and capture its boot on the serial console",
    applies=lambda node, truth: _subject_kind(node, truth) == "vm" and bool(_BOOT_WATCH_RE.search(_text(node))),
    params=[Param("GID", "subject"), Param("SECONDS_TO_WATCH", "default", default="140")],
    files={"/tmp/watch_boot_{GID}.sh": r"""#!/usr/bin/env bash
# §17.1292 -- the guest's boot, as its serial console shows it: GRUB, the kernel, cloud-init's datasource and
# network lines, the login prompt. Live: VM 106 booted Ubuntu from its new disk and had no address under
# either seed; a quiet console after a newline said only that nobody was listening. The boot log says why.
set -uo pipefail
GID={GID}
LOG=/tmp/console_{GID}.log
: | tee "$LOG" >/dev/null
timeout {SECONDS_TO_WATCH} socat -u UNIX-CONNECT:/var/run/qemu-server/$GID.serial0 STDOUT | tee "$LOG" >/dev/null &
CAP=$!
sleep 2
if qm status "$GID" | grep -q running; then qm reset "$GID"; else qm start "$GID"; fi
wait "$CAP" || true
BYTES="$(wc -c < "$LOG" | tr -d ' ')"
echo "=== VM $GID console, first {SECONDS_TO_WATCH}s after reset ($BYTES bytes) ==="
if [ "${BYTES:-0}" -lt 20 ]; then
    # §17.1294 -- an empty capture is a finding, not a success: the model's redraft of this step was marked done
    # with an empty log. Either nothing listens on the socket, the guest's console is not on serial0, or socat
    # could not connect; the config line says which to look at next.
    echo "FAILED: nothing arrived on VM $GID's serial console in {SECONDS_TO_WATCH}s (socket /var/run/qemu-server/$GID.serial0; $(qm config "$GID" | grep -E '^(serial0|vga):' | tr '\n' ' '))"
    exit 1
fi
tr -d '\r' < "$LOG" | grep -vE '^\s*$' | tail -n 120
"""},
    run="bash /tmp/watch_boot_{GID}.sh",
    verify=["qm status {GID}"],
    risk="Hard-resets VM {GID} (it has no state worth keeping until it reaches the network) and prints its console for {SECONDS_TO_WATCH} seconds.",
)


TEMPLATES: list[Template] = [INSTALL_OS_CLOUDINIT, WATCH_GUEST_BOOT, READ_GUEST_CONSOLE, INSTALL_STEAM_SERVER, RUN_IN_VM_VIA_AGENT, REACH_VM_SSH_AND_RUN, RUN_IN_CONTAINER]


def _text(node: dict) -> str:
    return " ".join(str((node or {}).get(k) or "") for k in ("title", "description"))


def _subject_kind(node: dict, truth) -> Optional[str]:
    if truth is not None and getattr(truth, "kind", None):
        return getattr(truth, "kind")
    return None


_INSIDE_RE = re.compile(r"\b(?:inside|in|into|on)\s+(?:the\s+)?(?:vm|container|ct|lxc|guest)\b|\bin\s+LXC\s+\d+|\binside\b", re.I)


def intent_of(node: dict) -> Optional[str]:
    """§17.1293 — the template a step's TEXT alone points at, with no measured
    guest yet: the hands-on classifier asks this, because a step the engine owns
    a shape for is hands-on by construction. Live, ADD119 ("reset VM 106 and
    capture its serial console during boot") carried no fenced command, so
    `step_commands` found nothing, the step ran as an LLM task, the model wrote
    "No open write channel … run the following as root@pve" three times and the
    node failed -- while `WATCH_GUEST_BOOT.applies` would have said yes."""
    if not subject_gid(node):
        return None
    text = _text(node)
    if _INSTALL_OS_RE.search(text):
        return INSTALL_OS_CLOUDINIT.name
    if _BOOT_WATCH_RE.search(text):
        return WATCH_GUEST_BOOT.name
    if _CONSOLE_RE.search(text):
        return READ_GUEST_CONSOLE.name
    if _HOST_SIDE_RE.search(text):
        return None
    if _INSIDE_RE.search(text):
        return "guest_work"            # reach_vm_ssh_and_run or run_in_container, once the guest's kind is measured
    return None


def subject_gid(node: dict, truth=None) -> Optional[str]:
    m = _SUBJECT_RE.search(_text(node))
    if m:
        return m.group(1)
    return str(getattr(truth, "gid", "") or "") or None       # §17.1316 — the pause measured the guest the text names by name


def select_template(node: dict, truth) -> Optional[Template]:
    """The first template whose ``applies`` holds for this step and this
    measured guest; None means today's model-written path."""
    if not subject_gid(node, truth):
        return None
    for t in TEMPLATES:
        try:
            if t.applies(node, truth):
                return t
        except Exception:
            continue
    return None


def guest_user_name(node: dict, gid: str, env: Optional[dict]) -> str:
    """`PALWORLD_USER` for the palworld-server guest (§17.1288h's word)."""
    from app.modules.supervised_runs import _guest_word
    return f"{_guest_word(_text(node), gid, env)}_USER"


def values_for(template: Template, node: dict, truth, env: Optional[dict], model_values: Optional[dict] = None) -> dict:
    gid = subject_gid(node, truth) or ""
    vals: dict[str, str] = {"GID": gid, "GUEST_USER_NAME": guest_user_name(node, gid, env)}
    for p in template.params:
        if p.source == "subject":
            vals[p.name] = gid
        elif p.source == "default":
            vals[p.name] = p.default
        elif p.source == "node_key":
            vals[p.name] = re.sub(r"[^A-Za-z0-9]", "", str((node or {}).get("node_key") or "step")) or "step"   # §17.1317 — the unit name
        elif p.source == "derived":
            # §17.1322 — read off what the engine already holds (the step's text, its last attempt's
            # record, the research); the model is asked only when nothing holds it (fill_free_params).
            vals[p.name] = derive_param(p.name, node) or str((model_values or {}).get(p.name) or "").strip() or p.default
        elif p.source == "truth":
            if p.name == "NEEDS_KEY":
                vals[p.name] = "no" if getattr(truth, "key_known_by", None) else "yes"
            else:
                vals[p.name] = p.default
        elif p.source == "model":
            vals[p.name] = str((model_values or {}).get(p.name) or "").strip()
        elif p.source == "operator":
            vals[p.name] = ""          # stays a placeholder: the operator fills it on the frame
        elif p.source == "pin":
            vals[p.name] = p.default
            if p.name == "IPCONFIG":
                subs = (env or {}).get("substitutions") or {}
                word = vals["GUEST_USER_NAME"].rsplit("_USER", 1)[0]
                pinned = next((str(v) for k, v in (subs.items() if isinstance(subs, dict) else [])
                               if str(k).upper() in (f"{word}_IP", f"VM{gid}_IP")), "")
                if pinned:
                    gw = next((p2.default for p2 in template.params if p2.name == "GATEWAY"), "192.168.1.1")
                    vals[p.name] = f"ip={pinned}/24,gw={gw}"
    # §17.1322 — a default may name another value (INSTALL_DIR=/opt/{GAME}); resolve it here too, so
    # callers that read values (not just the rendered runbook) see the real path.
    for _ in range(2):
        for k, val in list(vals.items()):
            for k2, v2 in vals.items():
                if k2 != k and isinstance(val, str) and "{" + k2 + "}" in val:
                    val = val.replace("{" + k2 + "}", str(v2))
            vals[k] = val
    return vals


def render(template: Template, values: dict) -> str:
    """The runbook, in the exact shape `frame_run` parses: a marker line, the
    inputs, the risk, the files, the run command, the verify checks. Operator
    params are left as ``<NAME>`` placeholders; ``{GUEST_USER}`` inside a
    script is the ENVIRONMENT variable the run command sets from that placeholder."""
    v = dict(values)
    user_ph = f"<{v.get('GUEST_USER_NAME', 'GUEST_USER')}>"

    def fill(s: str) -> str:
        out = s
        for _ in range(2):                          # §17.1322 — a default may name another value (INSTALL_DIR=/opt/{GAME})
            for k, val in v.items():
                if k == "GUEST_USER":
                    continue
                out = out.replace("{" + k + "}", str(val))
        return out

    inputs = []
    for p in template.params:
        if p.source == "operator":
            inputs.append(f"- `{user_ph}` — {p.hint}")
    lines = [f"{TEMPLATE_MARK} {template.name} -->", "", f"## {fill(template.title)}", ""]
    if inputs:
        lines += ["## Inputs needed", "", *inputs, ""]
    if template.risk:
        lines += ["## Risk", "", fill(template.risk), ""]
    lines += ["## Write these files", ""]
    for path, body in template.files.items():
        lines += [f"### {fill(path)}", "```bash", fill(body).rstrip("\n"), "```", ""]
    runs = template.run if isinstance(template.run, list) else [template.run]
    lines += ["## Run this", "", "```bash", *[fill(r) for r in runs], "```", "", "## Verify", ""]
    lines += [f"- `{fill(c)}`" for c in template.verify]
    return "\n".join(lines) + "\n"


def template_of(runbook: str) -> Optional[str]:
    m = _TEMPLATE_MARK_RE.search(runbook or "")
    return m.group(1) if m else None


def model_fence(text: str) -> str:
    """The commands the model wrote for the one free parameter: the first
    fenced block, stripped of prompts and comments."""
    m = _FENCE_RE.search(text or "")
    body = m.group(1) if m else (text or "")
    out = []
    for ln in body.split("\n"):
        ln = re.sub(r"^\s*\$\s+", "", ln.rstrip())
        # §17.1310 — a fence marker is never content. Live, ADD100's draw opened a
        # second fence the regex did not pair; the raw text kept a "```bash" line,
        # the template wrote it INTO the script's fenced block, `file_writes` closed
        # the file there (9 of 29 lines), bash warned "here-document … delimited by
        # end-of-file", exited 0, and the step was recorded done having done nothing.
        if ln.strip().startswith("```"):
            continue
        if ln.strip() and not ln.strip().startswith("#"):
            out.append(ln)
    return "\n".join(out)


def plan_context(upstream: str = "", environment: Optional[dict] = None) -> str:
    """§17.1306 — what earlier steps established and what the facts/pins say,
    for the free-parameter draw. Capped; secrets travel by name only, and the
    environment holds none (the runner's store does)."""
    parts: list[str] = []
    up = str(upstream or "").strip()
    if up:
        parts.append("WHAT EARLIER STEPS ESTABLISHED (reproduce content they specify; do not invent substitutes):\n" + up[:6000])
    env = environment or {}
    facts = [str(f.get("text") if isinstance(f, dict) else f) for f in (env.get("facts") or [])]
    facts = [f.strip() for f in facts if f and f.strip()][:40]
    if facts:
        parts.append("KNOWN FACTS ABOUT THIS SYSTEM:\n" + "\n".join(f"- {f[:220]}" for f in facts))
    subs = env.get("substitutions") or {}
    if isinstance(subs, dict) and subs:
        parts.append("PINNED VALUES (use these, never a placeholder like example.com):\n"
                     + "\n".join(f"- {k} = {v}" for k, v in list(subs.items())[:40]))
    return ("\n\n".join(parts) + "\n\n") if parts else ""


FREE_PARAM_SYSTEM_VERIFY = (
    "You fill ONE parameter of a fixed, already-approved script: a single READ-ONLY command that runs inside a "
    "guest machine and whose output shows whether THIS step's goal is already met (df, ls, cat, systemctl "
    "is-active, dpkg -l, ss, ip). Check what this step itself leaves behind: for an install, the binary, "
    "directory or file it installs (`ls -la /opt/palworld/PalServer.sh`), not a service a later step creates. "
    "Output exactly one ```bash fence containing that one command and nothing else: "
    "no sudo, no writes, no pipes to files, no placeholders, no comments."
)

FREE_PARAM_SYSTEM = (
    "You fill ONE parameter of a fixed, already-approved script: the commands that run INSIDE a guest "
    "machine, as root, non-interactively. Output exactly one ```bash fence containing those commands and "
    "nothing else: no sudo prefix (they already run as root), no prompts (apt-get with -y, "
    "DEBIAN_FRONTEND is set), no placeholders, no comments, no explanations outside the fence. "
    "Package installs and service enables are the usual content; do not start, stop, resize or "
    "reconfigure the VM or container itself -- that is the host's business and the script's. "
    "Content the step specifies (a config file, a unit, a key) is reproduced from what earlier steps and "
    "the facts established -- never a stand-in such as example.com, admin@example.com or a sample site: "
    "a value you do not have is a <NAME> placeholder the operator fills. A config file written whole begins "
    "with a truncating write (tee FILE <<'EOF'); only your own later blocks append with tee -a."
)


#: §17.1311 — the host-side wrapper the template ALREADY supplies around the guest's commands.
_GUEST_WRAPPER_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:pct\s+exec\s+\d+|qm\s+guest\s+exec\s+\d+|lxc-attach\s+-n\s+\d+)(?:\s+--?[\w-]+(?:\s+\S+)?)*\s+--\s+", re.I)
_SHELL_C_RE = re.compile(r"^\s*(?:bash|sh)\s+-c\s+(['\"])(.*)\1\s*$", re.S)


def strip_guest_wrappers(text: str) -> str:
    """The commands as they run INSIDE the guest. Live, the model wrote
    `pct exec 120 -- caddy validate …` for the agent/container template's
    VERIFY_INSIDE; the template wrapped it again and the container answered
    `pct: command not found`, so the §17.1302 read saw nothing. The same model
    habit puts `pct exec N --` in front of REMOTE_COMMANDS lines."""
    out = []
    for ln in str(text or "").split("\n"):
        prev = None
        while prev != ln:
            prev = ln
            ln = _GUEST_WRAPPER_RE.sub("", ln, count=1)
            m = _SHELL_C_RE.match(ln)
            if m:
                ln = m.group(2)
        out.append(ln)
    return "\n".join(out)


class ContentCut(RuntimeError):
    """§17.1312 — the model's content for a free parameter was cut mid-line (its
    fence never closed) twice, at the normal cap and at three times it."""


def content_is_cut(raw_text: str) -> bool:
    """An opening fence with no closing fence is a draw that ran out of tokens.
    Live, ADD100's 139-line server.js ended `const runRes = await axios.post(
    SCAFFOLD_ENGINE_URL` -- the template closed its own heredoc after it, every
    gate passed, and the file would have replaced a working backend with
    half a program. §17.1314 — so is a heredoc the content opens and never
    closes (the ran-live record: `tee server.js <<'EOF'` with no `EOF`; bash
    warned "here-document at line 19 delimited by end-of-file" and wrote the
    rest of the script into the file)."""
    text = str(raw_text or "")
    if text.count("```") % 2 == 1:
        return True
    from app.modules.supervised_runs import unterminated_heredoc
    return unterminated_heredoc(model_fence(text)) is not None


#: §17.1314 — a line in the model's commands that REPLACES a file: `tee PATH <<`, `tee PATH <`,
#: `cat > PATH`, `cp SRC PATH`, `mv SRC PATH`, `install … PATH`. Appends (`tee -a`, `>>`) keep the old content.
_OVERWRITE_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:tee\s+(?!-a\b)(?P<tee>/[^\s<>|;&]+)\s*(?:<<|<)"
    r"|cat\s*>\s*(?P<cat>/[^\s<>|;&]+)"
    r"|(?:cp|mv)\s+(?:-\S+\s+)*\S+\s+(?P<cpmv>/[^\s<>|;&]+)\s*$"
    r"|install\s+(?:-\S+\s+\S+\s+|-\S+\s+)*\S+\s+(?P<inst>/[^\s<>|;&]+)\s*$)")


def keep_a_copy_before_overwrites(remote_commands: str) -> str:
    """Before every line that replaces a file, a line that keeps the old one.

    Live, ADD100's draw wrote `tee /opt/control-panel-backend/server.js <<'EOF'`
    over the ONLY copy of a working backend (1,669 bytes from September, in no
    record anywhere) with a file cut mid-line by the draw's token cap. The old
    program survives only in the memory of the process that loaded it; its next
    restart loads the broken file. A ledger needs the pre-image (§17.1047) --
    so does a disk. `cp -a PATH PATH.bak.<stamp>` costs nothing and is the
    engine's shape, not the model's to remember."""
    out: list[str] = []
    in_heredoc: Optional[str] = None
    for ln in str(remote_commands or "").split("\n"):
        if in_heredoc is not None:
            out.append(ln)
            if ln.strip() == in_heredoc:
                in_heredoc = None
            continue
        m = _OVERWRITE_RE.match(ln)
        if m and not ln.lstrip().startswith("#"):
            path = next(v for v in (m.group("tee"), m.group("cat"), m.group("cpmv"), m.group("inst")) if v)
            out.append(f'[ -e "{path}" ] && cp -a "{path}" "{path}.bak.$(date +%Y%m%d%H%M%S)"')
        out.append(ln)
        hd = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", ln)
        if hd and not ln.lstrip().startswith("#"):
            in_heredoc = hd.group(1)
    return "\n".join(out)


_STEAMCMD_RE = re.compile(r"(?P<q>['\"]?)(?P<path>(?:[^\s'\"]*/)?steamcmd(?:\.sh)?)(?P=q)\s+(?=.*\+app_update)")   # §17.1321 — the path, never its quotes


_APT_RE = re.compile(r"(?<![\w-])(?:apt-get|apt|aptitude|dpkg)(?![\w-])")
APT_REPAIR_PRELUDE = [
    "# §17.1320 -- repair what a failed attempt left in dpkg before any apt work (a half-installed package aborts every later install)",
    "dpkg --configure -a >/dev/null 2>&1 || true",
    "for _p in $(dpkg -l 2>/dev/null | awk '/^(i[^i ]|[^i ]i|rF|rH|rU)/ {print $2}'); do dpkg --purge --force-remove-reinstreq \"$_p\" >/dev/null 2>&1 || true; done",
]


def repair_apt_state_first(remote_commands: str) -> str:
    """§17.1320 — when the model's commands touch apt/dpkg, the engine's own
    prelude runs first: finish pending configuration, then purge what is still
    half-installed/half-configured. Live, the operator-approved apt-steamcmd draft
    left `steamcmd:i386` in state `in` (declined licence) and the next correct
    draft's `apt-get install lib32gcc-s1 …` would have aborted on it with
    `E: Sub-process /usr/bin/dpkg returned an error code (1)` -- the engine's own
    mess, blocking the engine's own next step."""
    text = str(remote_commands or "")
    if not _APT_RE.search(text) or APT_REPAIR_PRELUDE[1] in text:
        return text
    return "\n".join(APT_REPAIR_PRELUDE) + "\n" + text


def bootstrap_steamcmd_first(remote_commands: str) -> str:
    """§17.1317 — a fresh SteamCMD must run once (`+quit`) before an `app_update`
    works; live, T23's first `+app_update 2394010` ended `Failed installing AppID
    2394010 (Missing configuration)` with an empty /opt/palworld. One bootstrap
    line before the first install, and only once."""
    out: list[str] = []
    done = False
    for ln in str(remote_commands or "").split("\n"):
        m = _STEAMCMD_RE.search(ln) if not done and not ln.lstrip().startswith("#") else None
        if m:
            # §17.1320 — as the SAME user as the install: SteamCMD bootstraps per home directory, so a
            # root bootstrap does nothing for `sudo -u palworld steamcmd.sh …`.
            su = re.match(r"\s*(sudo\s+-u\s+\S+\s+|su\s+(?:-\s+|-l\s+)?\S+\s+-c\s+|runuser\s+-u\s+\S+\s+--\s+)", ln)
            prefix = su.group(1).strip() + " " if su else ""
            boot = f"{m.group('path')} +quit >/dev/null 2>&1 || true"
            # §17.1321 — live, the install line was `su - palworld -c '/opt/…/steamcmd.sh +…'`: the path
            # capture swallowed the opening quote and the bootstrap line never closed it -- the whole
            # remote script failed to parse. A `-c` prefix gets the bootstrap as ONE quoted argument.
            if su and " -c " in prefix:
                out.append(f"{prefix}'{boot}'   # bootstrap: a fresh SteamCMD updates itself and exits")
            else:
                out.append(f"{prefix}{boot}   # bootstrap: a fresh SteamCMD updates itself and exits")
            done = True
        out.append(ln)
    return "\n".join(out)


_ABS_PATH_RE = re.compile(r"(?<![\w.])(/(?:[\w.@+-]+/)*[\w.@+-]+)")


def check_looks_where_the_work_went(remote_commands: str, verify_inside: str) -> str:
    """§17.1321 — the reason the one-line check does NOT fit the commands, or ``""``.
    Live, the install went to `/opt/palworld/server` (`+force_install_dir`) and the
    check read `ls -la /opt/palworld/PalServer.sh`: a correct install would have been
    judged a failure. A check that names an absolute path must name a path the
    commands write to, or a parent of one."""
    paths = _ABS_PATH_RE.findall(str(verify_inside or ""))
    if not paths:
        return ""
    body = str(remote_commands or "")
    body_paths = set(_ABS_PATH_RE.findall(body))
    for v in paths:
        if v in ("/", "/dev/null"):
            continue
        parent = v.rsplit("/", 1)[0] or "/"
        if any(b == v or b.startswith(v.rstrip("/") + "/") or v.startswith(b.rstrip("/") + "/") for b in body_paths):
            continue
        if parent in body_paths:
            continue
        return (f"the check looks at `{v}`, which the commands never write to or under -- they work in "
                f"{', '.join(sorted(b for b in body_paths if b.count('/') >= 2)[:6]) or 'other paths'}; check there")
    return ""


async def fill_free_params(template: Template, node: dict, brief_text: str, upstream: str = "",
                           environment: Optional[dict] = None, retry_note: str = "") -> dict:
    """Ask the model for the free parameters only, one short draw each
    (§17.1303: REMOTE_COMMANDS, and for the agent template the ONE read-only
    check inside the guest that shows the goal met)."""
    free = [p for p in template.params if p.source == "model"
            or (p.source == "derived" and not p.default and not derive_param(p.name, node))]   # §17.1322 — ask only for what nothing holds and nothing defaults
    if not free:
        return {}                                   # §17.1290b — no draw, no import, for a template with no free parameter
    from app import model_router                    # §17.1290b — live: importing it from app.modules was an ImportError
    from app.config import settings
    from app.utils.llm_retry import generate_until_nonempty
    # §17.1306 — the model path's prompt carries the upstream outputs and the
    # facts; the template draw carried neither, so ADD88's "Caddyfile with the
    # 17-line content … five handle_path blocks" came back as a stub with
    # `admin@example.com` and `:80 { respond … }`. Same reader, same evidence.
    context = plan_context(upstream, environment)
    # §17.1318 — parity with the model path, the rest of it: what the LAST attempt at
    # this step ran and why it stopped (§17.1247 `attempt_feedback`, recovered from the
    # pre-image after a reset), and the step's research (§17.1262). Live, T23's agent
    # draft installed Ubuntu's `steamcmd` package and never downloaded the game: the
    # draw had the title "Install PalWorld server", an empty description, and nothing
    # about the ssh attempt an hour earlier that had the method right (SteamCMD app
    # 2394010) and failed on the first-run quirk.
    try:
        from app.modules import supervised_runs as _sr
        prior = _sr.attempt_feedback(node) or ""
        research = await _sr.research_for_step(node, environment) if environment is not None else ""
    except Exception as exc:                             # fail-soft: the draw without them is the old behaviour
        logger.warning("template_draw_context_failed node=%s err=%r", node.get("node_key"), exc)
        prior, research = "", ""
    context = context + (prior.strip() + "\n\n" if prior.strip() else "") + (research.strip() + "\n\n" if research.strip() else "")
    # §17.1308 — the previous attempt's refusal, quoted back: fix exactly that, keep the rest.
    note = (f"THE PREVIOUS ATTEMPT WAS REFUSED BY THE ENGINE'S GATE -- fix exactly this and keep everything else:\n"
            f"{str(retry_note).strip()[:2500]}\n\n") if str(retry_note or "").strip() else ""
    out: dict[str, str] = {}
    base_cap = min(1500, int(getattr(settings, "node_generation_max_tokens", 1500) or 1500))
    for p in free:
        prompt = (f"STEP: {node.get('title') or ''}\n\n{_text(node)}\n\n{brief_text[:4000]}\n\n{context}{note}"
                  f"Write the {p.hint} for this step.")
        text = ""
        # §17.1312 — a draw whose fence never closes was cut by the cap; try once
        # more at three times the cap, then refuse rather than ship half a file.
        for cap in (base_cap, base_cap * 3):
            resp = await generate_until_nonempty(
                model_router.generate, prompt, {"role": "model_general", "think": False},
                system=(FREE_PARAM_SYSTEM_VERIFY if p.name == "VERIFY_INSIDE" else FREE_PARAM_SYSTEM), temperature=0.1,
                max_tokens=cap,
                draws=2, label=f"template {template.name} {node.get('node_key')} {p.name}",
            )
            text = (getattr(resp, "text", "") or "").strip()
            if not content_is_cut(text):
                break
            logger.warning("template_param_cut template=%s node=%s param=%s cap=%d chars=%d", template.name,
                           node.get("node_key"), p.name, cap, len(text))
        else:
            raise ContentCut(f"the model's {p.name} for {node.get('node_key')} was cut mid-line at {base_cap * 3} tokens "
                             f"({len(text)} chars): the step's content is too large for one draw -- split the step, or write the "
                             f"file in parts")
        fenced = strip_guest_wrappers(model_fence(text))                     # §17.1311
        if p.source == "derived":
            m = re.search(r"\d{4,8}", fenced) if p.name == "APP_ID" else re.search(r"[A-Za-z0-9_-]+", fenced)
            out[p.name] = m.group(0) if m else ""
            continue
        if p.name == "VERIFY_INSIDE":
            check = fenced.split("\n", 1)[0].strip()
            # §17.1321 — a check that looks where the work did not go is redrawn once, told where it went.
            why = check_looks_where_the_work_went(out.get("REMOTE_COMMANDS", ""), check)
            if why:
                logger.warning("template_check_redrawn template=%s node=%s why=%s", template.name, node.get("node_key"), why[:160])
                resp2 = await generate_until_nonempty(
                    model_router.generate, prompt + f"\n\nYOUR PREVIOUS CHECK WAS REFUSED: {why}. The commands are:\n{out.get('REMOTE_COMMANDS', '')[:3000]}",
                    {"role": "model_general", "think": False}, system=FREE_PARAM_SYSTEM_VERIFY, temperature=0.1,
                    max_tokens=base_cap, draws=2, label=f"template {template.name} {node.get('node_key')} {p.name} redraw")
                check2 = strip_guest_wrappers(model_fence((getattr(resp2, "text", "") or "").strip())).split("\n", 1)[0].strip()
                if check2 and not check_looks_where_the_work_went(out.get("REMOTE_COMMANDS", ""), check2):
                    check = check2
            out[p.name] = check
        else:
            out[p.name] = repair_apt_state_first(bootstrap_steamcmd_first(keep_a_copy_before_overwrites(fenced)))   # §17.1314, §17.1317, §17.1320
    return out
