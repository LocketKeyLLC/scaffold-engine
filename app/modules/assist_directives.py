"""System-prompt directive appliers for assist guidance generation.

§17.856 (audit "assist decomposition") — the pure string transforms that pick a
tool's human-facing base prompt (`guide_system_for_tool`) and append the optional
behavioral directives (verbosity, problem-solving discipline, the 👉-next-action
callout, ground-or-ask, screen grounding, the location banner) to it. Extracted
verbatim from `assist_guide.py`; every name is re-exported there so
`assist_guide.<NAME>` and the tests keep resolving. These are leaf functions: they
take a system string + flags and return a string, calling nothing else in assist.
"""

from __future__ import annotations

import re

from app.modules.prompt_assembly import EXECUTION_SYSTEM_RUNBOOK
from app.modules.assist_prompts import (
    GUIDE_SYSTEM_CODEGEN,
    GUIDE_SYSTEM_NONCODE,
    GUIDE_SYSTEM_DECISION,
    _RUNBOOK_HUMAN_FRAMING,
)

# §17.499 — verbosity / skill-level directives appended to the system prompt.
VERBOSITY_LEVELS = ("terse", "normal", "detailed")
_VERBOSITY_DIRECTIVE = {
    "terse": (
        "\n\nVERBOSITY: TERSE — the operator asked for brevity: output only the "
        "commands/steps with a one-line reason each, and omit background and "
        "rationale. Brevity means FEWER WORDS, not assuming expertise — still "
        "spell out any non-obvious sub-task and keep every command copy-paste "
        "ready (the beginner-audience rule above always holds)."
    ),
    "detailed": (
        "\n\nVERBOSITY: DETAILED — assume a less-experienced operator: briefly "
        "explain WHY each step matters and what to watch for, and expand the "
        "verification. Stay concrete and copy-paste-ready — explanation in "
        "addition to the commands, never instead of them."
    ),
}


def apply_verbosity(system: str, verbosity: str | None) -> str:
    """Append the verbosity directive to a system prompt. `normal`/unknown → no
    change (current behavior)."""
    return system + _VERBOSITY_DIRECTIVE.get(verbosity or "normal", "")


def guide_system_for_tool(tool: str, *, is_decision: bool = False) -> str:
    """Pick the human-facing system prompt for a node's tool type.

    Mirrors ``prompt_assembly.system_for_tool`` (shell/codegen/else) but
    targets the human operator rather than the LLM executor. The ``shell``
    variant reuses ``EXECUTION_SYSTEM_RUNBOOK`` verbatim (it already targets a
    human performing host commands) with a one-line operator framing prepended.

    §17.654 — a decision node ALWAYS uses the decision prompt (one choice at a
    time, suggest-don't-decide), regardless of its tool, so the operator is
    never railroaded by a resolved-for-them runbook.
    """
    if is_decision:
        return GUIDE_SYSTEM_DECISION
    t = (tool or "").lower()
    if t == "shell":
        return f"{_RUNBOOK_HUMAN_FRAMING}\n\n{EXECUTION_SYSTEM_RUNBOOK}"
    if t == "codegen":
        return GUIDE_SYSTEM_CODEGEN
    return GUIDE_SYSTEM_NONCODE


# §17.742 — problem-solving discipline for TANGLED, multi-attempt steps. Appended
# to fix/guide/ask so the engine stops thrashing (re-proposing ruled-out
# approaches, asking for output the operator can't produce) and instead honors
# the confirmed constraints, commits to ONE path once approaches have failed, and
# matches the operator's real capability. Kept generic; the concrete constraints
# come from the recap's CONSTRAINTS section (injected alongside).
_PROBLEM_SOLVING_FRAMING = (
    "Problem-solving discipline (for tangled, multi-attempt situations):\n"
    "1. HONOR THE CONFIRMED CONSTRAINTS. Treat anything the running recap lists "
    "under CONSTRAINTS — and any limit the operator has stated (no copy-paste in "
    "this console, the guest agent is down, GUI-only, offline-only, a login they "
    "don't have) — as a HARD limit. Never give an instruction a constraint rules "
    "out: if they cannot copy-paste, give short commands to TYPE by hand and ask "
    "them to read the result off the screen — do not ask them to paste output "
    "they cannot copy; if a path is unavailable (guest agent, network, a service) "
    "do not route through it. If unsure whether a limit still holds, ask one "
    "short yes/no question instead of assuming.\n"
    "2. WHEN SEVERAL APPROACHES HAVE ALREADY FAILED, STOP CYCLING. If the recap's "
    "DONE/CONSTRAINTS or the transcript shows approaches already tried and failed "
    "(e.g. guestmount, virt-customize, the guest agent), do NOT propose another "
    "variant of a ruled-out approach. Step back: in one or two lines name what has "
    "been ruled out and why, then COMMIT to the single most robust path that fits "
    "the constraints and see it through to the goal — do not re-open the whole "
    "strategy every turn. One coherent path beats five half-tried ones.\n"
    "3. MATCH THE OPERATOR'S ACTUAL CAPABILITY. If they are in a limited "
    "environment — a console/GUI with no copy-paste, hand-typing at a boot menu "
    "or editor — drop to the smallest possible steps: ONE key or ONE short line at "
    "a time, state the EXACT key/text to enter and what they should SEE on screen "
    "right after, and ask them to describe what is on screen (or read the last "
    "line) rather than paste. Keep them oriented — never dump a long branch of "
    "alternatives when they are stuck.\n"
    "4. PREFER THE EASIEST TOOL THE OPERATOR ALREADY HAS — do NOT default to "
    "hand-typed CLI. Look at what is in front of them: if they are working in a "
    "management web UI or console (the Proxmox web UI, a cloud/hosting provider "
    "console, a NAS/router/device admin page, a desktop app) that can do the task, "
    "LEAD with that GUI path — name the exact place to go and control to use (e.g. "
    "'in the Proxmox web UI: select VM 100 → Hardware → Add → PCI Device', or "
    "'→ Options → set Machine to q35', or use the built-in Create-VM wizard / "
    "Console / mount-ISO buttons). A beginner clicking a labeled control is faster "
    "and far less error-prone than typing a long command — it avoids whole classes "
    "of typos and mistakes (like picking the wrong machine type in a hand-written "
    "`qm create`). Give a CLI equivalent only as a brief one-line alternative, or "
    "when the UI genuinely cannot do it. When you diagnose or choose the move, say "
    "in ONE line which tool is simplest here — and pick it.\n"
    "5. HISTORY IS NOT A MANDATE — AND FOLLOW THE OPERATOR'S DIRECTION. The "
    "recorded facts and earlier turns describe what has ALREADY BEEN TRIED — often "
    "a long chain of shell commands — but that is HISTORY, not a reason to keep "
    "using the CLI (or the same failed approach). Choose the best tool for the "
    "NEXT move regardless of how earlier steps happened to be done, and do not let "
    "a wall of past `qm`/shell facts anchor you to the shell. If the operator "
    "DIRECTS a path or tool — 'why aren't we using the web UI', 'let's use the "
    "GUI', 'I want copy-paste', 'isn't there an easier way', 'let's start over' — "
    "treat it as an INSTRUCTION: switch to it immediately and lead with it; do NOT "
    "explain why the current/CLI path is fine and then keep going down it. Web "
    "search results skewing toward CLI is likewise not a reason to hand back CLI — "
    "mine them for the facts and re-express the procedure in the chosen tool.\n"
    "6. DON'T INVENT SYSTEM-SPECIFIC VALUES — VERIFY OR PLACEHOLDER. Never hardcode "
    "a concrete filename, version number, path, disk, device/PCI id, or interface "
    "name you have NOT actually seen in the operator's own output, facts, or recap "
    "(e.g. an ISO like `ubuntu-24.04.1-live-server-amd64.iso`, a disk `/dev/sdX`, a "
    "NIC `ens3`). These depend on THEIR system, not general knowledge, and a wrong "
    "guess makes the whole command fail (exactly what happened when a made-up ISO "
    "filename broke `qm create`). If you need such a value and it is not already "
    "known, either (a) put a quick DISCOVERY step FIRST — list it (`pvesm list "
    "local --content iso`, `lsblk`, `ip a`) and use the REAL result — or (b) write "
    "it as a clearly-marked <PLACEHOLDER> and tell them exactly what to substitute. "
    "For a one-shot command that fails on a wrong value, prefer discover-then-use.\n"
    "7. ADDRESS EVERYTHING THE OPERATOR RAISED — do NOT tunnel-vision on the error. "
    "If their message contains more than one thing — a numbered list, or a question "
    "/ observation ALONGSIDE an error (e.g. 'my prompt now shows just `$` instead of "
    "`user$`' sitting next to a failed command) — acknowledge and answer EACH point, "
    "however briefly, THEN give the fix / next step. A one-line answer to the side "
    "question ('the bare `$` is just your shell prompt style — cosmetic, not the "
    "cause of the error') is far better than silently ignoring it. Never drop a "
    "point they took the time to raise.\n"
    "8. STAY SCOPED TO THIS STEP AND THEIR ASK. Answer THIS step and what they "
    "actually asked. Do NOT volunteer unrelated pending project goals or tack on "
    "'by the way, X is still pending' reminders (e.g. surfacing Tesla-P40 fan-curve "
    "tuning inside a software-install answer). The project context / recap is there "
    "to GROUND your answer accurately, not to pull in tangents. Only raise another "
    "goal if the operator asks about the overall plan, or it directly blocks or is "
    "required by this step.\n"
    "9. WHEN THE LITERAL APPROACH IS BLOCKED BY A HARD CONSTRAINT BUT THE GOAL IS "
    "MET, ACCEPT IT AND MOVE ON. If the operator has shown the step's named method "
    "is impossible on THEIR system — a chip / board / driver / firmware / OS "
    "limitation they have hit and confirmed (e.g. the sensor chip locks PWM to "
    "automatic so manual fan curves can't be set) — AND the step's underlying GOAL "
    "is achieved another way (e.g. automatic fan control holds temperatures safe "
    "under load), treat the step as DONE: say so plainly ('your NCT7904D can't do "
    "manual PWM, but automatic control is keeping the P40 in range — that meets "
    "the goal of this step, so we're done here'), and move to the next step. Do "
    "NOT keep proposing variants of the ruled-out method, and do NOT hold the step "
    "open waiting for a deliverable the hardware cannot produce. The GOAL is what "
    "matters, not the specific tool the plan happened to name. If the operator "
    "keeps asking 'how do we proceed' / 'what's next' after a good-enough outcome, "
    "that is your signal to CONFIRM completion and advance, not to loop."
)


def apply_problem_solving(system: str, *, enabled: bool) -> str:
    """§17.742 — append the tangled-situation discipline to a system prompt when
    the valve is on. No-op otherwise."""
    if not enabled:
        return system
    return system + "\n\n" + _PROBLEM_SOLVING_FRAMING


_NEXT_CALLOUT_DIRECTIVE = (
    "\n\nLEAD WITH THE ACTION — make it impossible to miss. Begin your reply with a "
    "section titled exactly `## 👉 Do this next` containing ONLY the single most "
    "immediate action the operator should take right now: a bold one-line imperative "
    "(e.g. **Run this now:**) immediately followed by the exact command in its OWN "
    "fenced code block (```), then a one-line 'then tell me what it shows'. Keep this "
    "section to a few lines and put NOTHING before it. THEN continue with the full "
    "walkthrough using the section headings defined above. Throughout, keep the "
    "instructions PRESENT and scannable: every command or exact text to type goes in "
    "its own fenced code block on its own line — never buried inside a paragraph — "
    "and number a multi-step sequence so the operator follows one action at a time. "
    "Do not invent a concrete value the task/context/research did not give you (use "
    "a <PLACEHOLDER> as elsewhere)."
)


def apply_next_callout(system: str, *, is_decision: bool, enabled: bool) -> str:
    """§17.741 — append the 'lead with the immediate action' directive so a
    walkthrough opens with a prominent 👉 Do this next callout. No-op for
    decision nodes (the deliverable is a choice, not an action) and when the
    valve is off."""
    if not enabled or is_decision:
        return system
    return system + _NEXT_CALLOUT_DIRECTIVE


_GROUND_OR_ASK_DIRECTIVE = (
    "\n\nGROUND OR ASK — never GUESS an operator-specific value. Any value tied to "
    "THIS operator's system — a username, password, hostname, IP/MAC address, disk "
    "or path, filename, SSH key, port, VM/host name — that you were NOT given in "
    "the confirmed facts / operator environment above MUST be written as a "
    "<SCREAMING_SNAKE_CASE> placeholder, never a concrete guess, and surfaced in the "
    "walkthrough's values-to-provide / inputs section so the operator supplies it. "
    "Do NOT lift such a value from the recent dialogue and present it as known: the "
    "conversation may carry values from an ABANDONED earlier attempt (an old "
    "username, an old IP) that are now WRONG — especially after a reset/rebuild. A "
    "confident-looking wrong value is worse than a placeholder plus a quick question."
)


def apply_ground_or_ask(system: str, *, is_decision: bool, enabled: bool) -> str:
    """§17.756 — append the ground-or-ask discipline so guidance emits a placeholder
    and asks for any operator-specific value it wasn't actually given, instead of
    hardcoding a stale guess pulled from the transcript (the `ai-defruscio` username
    leak). No-op for decision nodes and when the valve is off."""
    if not enabled or is_decision:
        return system
    return system + _GROUND_OR_ASK_DIRECTIVE


_SCREEN_GROUNDING_DIRECTIVE = (
    "\n\nCONFIRM THE STARTING STATE — do NOT assume what is on screen. If this step "
    "means navigating an INTERACTIVE surface (an OS installer, a TUI/menu, a "
    "BIOS/boot menu, a noVNC/serial console, a web-UI wizard) and you cannot "
    "confirm the operator's CURRENT screen from the confirmed facts / running "
    "recap, do NOT assume which screen, prompt, or menu they are on — screens "
    "change faster than the plan tracks, and a walkthrough that starts on the wrong "
    "screen sends every keystroke to the wrong place. Instead OPEN by asking them "
    "to tell you what is on screen right now (paste the prompt, or describe the "
    "visible title / options), and make the FIRST action conditional on their "
    "answer ('if you see X do…; if you see Y do…'). Give a straight-line sequence "
    "of navigation steps only once the starting screen is confirmed. This does NOT "
    "apply to an ordinary shell step: a command whose output you ask them to report "
    "back is already self-confirming."
)


def apply_screen_grounding(system: str, *, is_decision: bool, enabled: bool) -> str:
    """§17.758 — append the confirm-the-screen discipline so a walkthrough for an
    interactive surface (installer / TUI / console / web wizard) whose current state
    isn't confirmed OPENS by asking what's on screen, instead of assuming a screen
    and sending keystrokes to the wrong place (the storage-screen assumption). No-op
    for decision nodes and when the valve is off."""
    if not enabled or is_decision:
        return system
    return system + _SCREEN_GROUNDING_DIRECTIVE


# §17.852 — every command block must say WHERE it runs, and every device/
# shell/console switch must be announced (operator: guidance hopped from the
# pve host shell to the workstation terminal mid-step with no callout).
_LOCATION_CALLOUT_DIRECTIVE = (
    "\n\nLOCATION BANNER (mandatory): open with ONE line naming exactly where "
    "these commands run — machine, shell/console/UI, and user when known, e.g. "
    "`📍 On: the Proxmox host shell (root@pve)` / `📍 On: your workstation "
    "terminal (aedefruscio@pop-os)` / `📍 In: the Proxmox web UI at "
    "https://…`. When this location differs from where the operator's LAST "
    "pasted command ran (read the execution context and the conversation), "
    "announce the SWITCH explicitly before any command: what they're leaving, "
    "what they're moving to, and how to get there (new terminal? ssh from "
    "where? browser tab?). If the step itself moves between locations, label "
    "each command block with its own `📍 On:` line. A command block's location "
    "must never be implicit."
)


def apply_location_callout(system: str, *, is_decision: bool, enabled: bool) -> str:
    """§17.852 — append the location-banner discipline (see directive above).
    No-op for decision nodes (no commands) and when the valve is off."""
    if not enabled or is_decision:
        return system
    return system + _LOCATION_CALLOUT_DIRECTIVE


# ── §17.897 — code-enforced copy-paste format ────────────────────────────
#
# Prompt rules are guidance; this is enforcement (the §17.882/§17.893 lesson).
# Only FENCED blocks get a ⧉ copy button in the UI (util.js `mdToHtml`), so a
# command the model emitted as an inline `code span` cannot be copied — the
# operator has to hand-retype it, which is exactly the complaint that opened
# this fix ("not giving commands in the copy and paste format").
#
# The rule is deliberately narrow: promote a span ONLY when it is the ENTIRE
# line. That is the shape the model actually produced ("**Run this command:**"
# on one line, `ssh root@…` alone on the next) and it leaves every mid-sentence
# mention (…set `scsi0` to…, the `radarr` user) untouched. A verb check on the
# first token then rejects a lone identifier or path standing on its own line.
_COMMAND_VERBS = frozenset("""
apt apt-get aptitude dnf yum zypper pacman apk brew snap flatpak
systemctl service journalctl systemd-analyze loginctl timedatectl hostnamectl
docker docker-compose podman kubectl helm crictl
qm pct pvesm pveam pvecm vzdump ha-manager
ssh scp sftp rsync curl wget nc telnet ping traceroute dig nslookup host
ip ifconfig route iptables nft ufw firewall-cmd ss netstat arp ethtool
mkdir rmdir cp mv rm ln touch chmod chown chgrp mount umount df du ls find
cat tac head tail less more grep egrep sed awk sort uniq cut tr wc tee xargs
tar gzip gunzip zip unzip bzip2 xz zstd
git make cmake gcc g++ cargo go npm npx pnpm yarn pip pip3 python python3
node deno ruby gem perl php java javac mvn gradle dotnet
useradd usermod userdel groupadd passwd chpasswd adduser deluser su sudo doas
lvcreate lvextend lvresize pvcreate vgcreate resize2fs xfs_growfs mkfs fdisk
parted lsblk blkid growpart e2fsck partprobe swapon swapoff
nano vim vi emacs echo printf export source bash sh zsh env printenv
crontab at systemd-run nohup kill killall pkill ps top htop free uptime uname
lsmod modprobe dmesg lspci lsusb lscpu dmidecode sensors nvidia-smi
openssl ssh-keygen ssh-copy-id gpg certbot
steamcmd wine winetray
reboot shutdown poweroff halt
""".split())

# A leading `$`/`#`/`>` prompt marker, or `sudo`/`doas`/`env`/`time` wrappers,
# are stripped before the verb check so `sudo apt update` still resolves.
_CMD_PREFIXES = ("sudo", "doas", "env", "time", "nohup", "exec")
_INLINE_ONLY_LINE_RE = re.compile(r"^(\s*)`([^`]{2,300})`\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def _looks_like_command(span: str) -> bool:
    """True when an inline span is a runnable shell command, not a reference."""
    s = span.strip().lstrip("$#>").strip()
    if not s or "\n" in s:
        return False
    # A bare identifier/path with no argument is a REFERENCE (`scsi0`,
    # `/opt/Radarr`, `radarr.service`), never a command worth its own block.
    if " " not in s:
        return False
    # Shell prompt pastes ("root@pve:~# qm start 106") are operator OUTPUT
    # echoed back, not an instruction to run — leave them alone.
    if re.match(r"^\S+@\S+:.*[#$]\s", s):
        return False
    toks = s.split()
    i = 0
    while i < len(toks) - 1 and toks[i] in _CMD_PREFIXES:
        i += 1
    head = toks[i]
    if head.startswith(("./", "/", "~/")):
        return True  # an explicit path invocation
    # Strip an env-assignment prefix (FOO=bar cmd …) before matching.
    if "=" in head and i + 1 < len(toks):
        head = toks[i + 1]
    return head in _COMMAND_VERBS


def promote_inline_commands(text: str) -> str:
    """§17.897 — rewrite whole-line inline command spans as fenced ```bash
    blocks so every command the engine hands the operator has a copy button.

    Content inside an existing fenced block is never touched. Fail-soft by
    construction: anything that does not match the narrow whole-line +
    command-verb shape is returned byte-for-byte unchanged."""
    if not text or "`" not in text:
        return text
    out: list[str] = []
    in_fence = False
    changed = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = _INLINE_ONLY_LINE_RE.match(line)
        if m and _looks_like_command(m.group(2)):
            cmd = m.group(2).strip().lstrip("$#>").strip()
            out.extend(["```bash", cmd, "```"])
            changed = True
            continue
        out.append(line)
    return "\n".join(out) if changed else text
