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
        Param("REMOTE_COMMANDS", "model", "the commands to run inside the guest, as root, one per line"),
        Param("VERIFY_INSIDE", "model", "ONE read-only command to run inside the guest whose output shows this step's "
                                        "goal is met (for example `df -h /`, `systemctl is-active nginx`, `dpkg -l curl`)"),
    ],
    files={"/tmp/in_vm_{GID}_agent.sh": '#!/usr/bin/env bash\n# Run this step\'s commands inside VM {GID} through its QEMU guest agent: no ssh, no account, no key, no address.\nset -uo pipefail\nGID={GID}\nqm status "$GID" | grep -q running || qm start "$GID"\nfor i in $(seq 1 12); do qm agent "$GID" ping >/dev/null 2>&1 && break; sleep 5; done\nqm agent "$GID" ping >/dev/null 2>&1 || { echo "FAILED: the guest agent in VM $GID did not answer within 60 s"; exit 1; }\ncat > /tmp/in_vm_{GID}_remote.sh <<\'REMOTE\'\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n{REMOTE_COMMANDS}\nREMOTE\n# --timeout 110 + the 60 s agent wait stays inside the runner\'s 180 s budget for one command.\nqm guest exec "$GID" --timeout 110 --pass-stdin 1 -- bash -s < /tmp/in_vm_{GID}_remote.sh > /tmp/in_vm_{GID}_agent.out \\\n  || { cat /tmp/in_vm_{GID}_agent.out; echo "FAILED: qm guest exec $GID did not run the script"; exit 1; }\n# the agent answers JSON {exitcode, out-data, err-data}: print the guest\'s output, exit with the guest\'s code\npython3 - /tmp/in_vm_{GID}_agent.out <<\'PYJ\'\nimport json, sys\nd = json.loads(open(sys.argv[1]).read() or "{}")\nsys.stdout.write(d.get("out-data") or "")\nsys.stderr.write(d.get("err-data") or "")\nsys.exit(int(d.get("exitcode", 1)))\nPYJ\n'},
    run="bash /tmp/in_vm_{GID}_agent.sh",
    # §17.1304 — the inside check ALONE: `qm agent ping` prints nothing, and a check that cannot speak to the
    # goal is an `unknown` that keeps §17.1302 from recording an already-met step.
    verify=['qm guest exec {GID} -- bash -c "{VERIFY_INSIDE}"'],
    risk="Runs this step's commands as root inside VM {GID} through its guest agent.",
)


RUN_IN_CONTAINER = Template(
    name="run_in_container",
    title="Run commands inside container {GID}",
    applies=lambda node, truth: _subject_kind(node, truth) == "ct" and not _HOST_SIDE_RE.search(_text(node)),
    params=[Param("GID", "subject"), Param("REMOTE_COMMANDS", "model", "the commands to run inside the container, as root, one per line"),
            # §17.1307 — parity with run_in_vm_via_agent: a check INSIDE the guest is what §17.1302 can read
            Param("VERIFY_INSIDE", "model", "ONE read-only command to run inside the container whose output shows this step's "
                                            "goal is met (for example `caddy validate --config /etc/caddy/Caddyfile`, `systemctl is-active caddy`)")],
    files={"/tmp/in_ct_{GID}.sh": r'''#!/usr/bin/env bash
# Run this step's commands inside container {GID} as root.
set -uo pipefail
GID={GID}
pct status "$GID" | grep -q running || pct start "$GID"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do pct status "$GID" | grep -q running && break; sleep 5; done
cat > /tmp/in_ct_{GID}_remote.sh <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
{REMOTE_COMMANDS}
REMOTE
pct push "$GID" /tmp/in_ct_{GID}_remote.sh /root/.scaffold_step.sh >/dev/null || { echo "FAILED: pct push into $GID"; exit 1; }
pct exec "$GID" -- bash /root/.scaffold_step.sh
'''},
    run="bash /tmp/in_ct_{GID}.sh",
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


TEMPLATES: list[Template] = [INSTALL_OS_CLOUDINIT, WATCH_GUEST_BOOT, READ_GUEST_CONSOLE, RUN_IN_VM_VIA_AGENT, REACH_VM_SSH_AND_RUN, RUN_IN_CONTAINER]


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


def subject_gid(node: dict) -> Optional[str]:
    m = _SUBJECT_RE.search(_text(node))
    return m.group(1) if m else None


def select_template(node: dict, truth) -> Optional[Template]:
    """The first template whose ``applies`` holds for this step and this
    measured guest; None means today's model-written path."""
    if not subject_gid(node):
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
    gid = subject_gid(node) or ""
    vals: dict[str, str] = {"GID": gid, "GUEST_USER_NAME": guest_user_name(node, gid, env)}
    for p in template.params:
        if p.source == "subject":
            vals[p.name] = gid
        elif p.source == "default":
            vals[p.name] = p.default
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
    "guest machine and whose output shows whether this step's goal is already met (df, ls, cat, systemctl "
    "is-active, dpkg -l, ss, ip). Output exactly one ```bash fence containing that one command and nothing else: "
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


async def fill_free_params(template: Template, node: dict, brief_text: str, upstream: str = "",
                           environment: Optional[dict] = None, retry_note: str = "") -> dict:
    """Ask the model for the free parameters only, one short draw each
    (§17.1303: REMOTE_COMMANDS, and for the agent template the ONE read-only
    check inside the guest that shows the goal met)."""
    free = [p for p in template.params if p.source == "model"]
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
    # §17.1308 — the previous attempt's refusal, quoted back: fix exactly that, keep the rest.
    note = (f"THE PREVIOUS ATTEMPT WAS REFUSED BY THE ENGINE'S GATE -- fix exactly this and keep everything else:\n"
            f"{str(retry_note).strip()[:2500]}\n\n") if str(retry_note or "").strip() else ""
    out: dict[str, str] = {}
    for p in free:
        prompt = (f"STEP: {node.get('title') or ''}\n\n{_text(node)}\n\n{brief_text[:4000]}\n\n{context}{note}"
                  f"Write the {p.hint} for this step.")
        resp = await generate_until_nonempty(
            model_router.generate, prompt, {"role": "model_general", "think": False},
            system=(FREE_PARAM_SYSTEM_VERIFY if p.name == "VERIFY_INSIDE" else FREE_PARAM_SYSTEM), temperature=0.1,
            max_tokens=min(1500, int(getattr(settings, "node_generation_max_tokens", 1500) or 1500)),
            draws=2, label=f"template {template.name} {node.get('node_key')} {p.name}",
        )
        text = (getattr(resp, "text", "") or "").strip()
        fenced = model_fence(text)
        out[p.name] = fenced.split("\n", 1)[0].strip() if p.name == "VERIFY_INSIDE" else fenced
    return out
