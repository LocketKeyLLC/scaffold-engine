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

import logging
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


# Stdlib logging is the runtime logger here (structlog is the formatter only).
logger = logging.getLogger("scaffold.assist_directives")


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


_RECOMMENDATION_DIRECTIVE = (
    "\n\nANSWER THE QUESTION, AND LEAN. When the operator asks something "
    "answerable — a yes/no ('should I delete this VM and start over?'), an "
    "either/or, 'which one', 'what do you recommend' — your FIRST line is the "
    "ANSWER, not a menu: 'Yes — rebuild it.' / 'No, keep it — here's why.' "
    "Then ONE line of reasoning, then the concrete next action. Laying out "
    "balanced options and asking them to choose is a non-answer: they asked "
    "BECAUSE they don't have the basis to choose, and a menu leaves them exactly "
    "as stuck as before.\n"
    "You may recommend a destructive action when it is genuinely the right call "
    "(rebuilding a corrupted install is often faster than salvaging it) — the "
    "operator runs every command themselves, so a clear recommendation costs "
    "them nothing and a dodge costs them time. But SAY what it destroys: name "
    "the resource and what is lost, on its own line, before the command. If the "
    "honest answer is that you cannot tell without more information, say THAT "
    "plainly and name the single piece of information that would decide it — "
    "that is still an answer, and still directive."
)


def apply_recommendation(system: str, *, enabled: bool = True) -> str:
    """§17.903 — append the answer-and-lean discipline.

    The live failure: the operator asked "perhaps we should start over. Delete
    this VM and start over?" and got no answer at all — the question was
    recorded as a note and the turn ended. Even once answering was restored,
    an answer that lays out options without a lean would leave them exactly as
    stuck, because they asked precisely because they lacked the basis to pick.
    """
    if not enabled:
        return system
    return system + _RECOMMENDATION_DIRECTIVE


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


# §17.908 — the model's REASONING ABOUT the operator, rendered TO the operator.
# Live (T23, turns 1399 and 1404):
#   "The operator's \"anything\" is a non-sequitur—they are likely expressing
#    frustration or boredom while the VM is hanging…"
#   "The operator's messages … are a pivot away from the current technical
#    struggle with VM 106. They are expressing boredom or frustration…"
# That is the model talking to ITSELF in the third person, and the operator read
# every word of it — including being told what they were feeling. The answer
# starts at the second paragraph in both cases, so this is a pure preamble
# strip, not a rewrite.
#
# Deliberately narrow: FIRST paragraph only, must OPEN with a third-person
# reference to the operator, never touches a heading/fence/list, and never
# empties the message.
# Must match NARRATION about the operator, not any sentence that happens to
# begin with the word. "The operator error was in the config file" and "The user
# data directory is /var/lib" are ordinary content and were stripped by the
# first cut. Two shapes carry the real signal: the POSSESSIVE ("The operator's
# messages …") and a state/communication verb ("The operator is asking …").
_OPERATOR_META_OPENER_RE = re.compile(
    r"^\s*(?:the\s+)?(?:operator|user)(?:'s|s')\s"
    r"|"
    r"^\s*(?:the\s+)?(?:operator|user)\s+"
    r"(?:is|are|was|were|has|have|had|seems?|appears?|sounds?|wants?|"
    r"means?|intends?|asks?|asking|saying|says?|likely|probably|apparently|"
    r"clearly|evidently)\b",
    re.IGNORECASE,
)


def strip_operator_meta_preamble(text: str) -> str:
    """Drop a leading paragraph that narrates the operator in third person."""
    if not text or not text.strip():
        return text
    parts = re.split(r"(\n\s*\n)", text, maxsplit=1)
    first = parts[0]
    stripped = first.lstrip()
    # Structure is content, never preamble.
    if stripped.startswith(("#", "```", "-", "*", ">", "|", "1.")):
        return text
    if not _OPERATOR_META_OPENER_RE.match(stripped):
        return text
    rest = "".join(parts[2:]) if len(parts) > 2 else ""
    if not rest.strip():
        return text  # it is the WHOLE message — stripping would leave nothing
    logger.info("assist_meta_preamble_stripped chars=%d", len(first))
    return rest.lstrip()


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


_DONE_CRITERION_DIRECTIVE = (
    "\n\nSAY WHEN THE STEP IS FINISHED. The operator cannot see your plan, so they "
    "cannot tell the difference between 'this command worked' and 'this STEP is "
    "complete'. End the walkthrough with a section titled exactly "
    "`## ✅ Done when` containing:\n"
    "  1. a single OBSERVABLE condition that means this step is finished — something "
    "they can SEE on their own screen (an exact prompt, a status line, a page that "
    "loads, a service reported active). Never 'when it works' or 'when the "
    "configuration is correct'; name what is visible.\n"
    "  2. one line telling them exactly how to move on: press "
    "**✓ Done → next step** (or type `next`).\n"
    "  3. if the observable condition does NOT appear, one line saying to paste what "
    "they see instead so it can be worked.\n"
    "Keep it to those three lines. Do NOT declare the step complete yourself, and do "
    "NOT ask them to keep reporting output after the condition is met — once they can "
    "see it, they should advance."
)


def apply_done_criterion(system: str, *, is_decision: bool, enabled: bool) -> str:
    """§17.932 — append the 'state the finish line' discipline.

    The live complaint was that the engine's instructions to move on were not
    clear, and the transcript showed why: every walkthrough ended on *"then
    tell me what it shows"*, which is an instruction to REPORT, never an
    instruction to ADVANCE. The operator was told what to run and what to paste
    back, but never what finished looks like or which control ends the step, so
    perfectly successful steps stayed open and the session read as stuck.

    No-op for decision nodes (their deliverable is a choice — `no_action_footer`
    and the decision card carry that path) and when the valve is off.
    """
    if not enabled or is_decision:
        return system
    return system + _DONE_CRITERION_DIRECTIVE


_PLAN_AUTHORITY_DIRECTIVE = (
    "\n\nYOU CANNOT CHANGE THE PLAN BY SAYING SO. You are writing a walkthrough; "
    "you have no ability to add, remove, reorder or retire steps, and nothing you "
    "write here alters the project plan. NEVER state that a step 'has been "
    "removed', that 'the plan has been updated', or that you have deleted, "
    "dropped or retired anything — those are assertions about system state you "
    "did not change, and the operator believes them.\n"
    "If the operator has asked for this step to go away, say plainly that it is "
    "STILL in the plan and that replying `skip` is what retires it. If the plan "
    "genuinely needs restructuring, say what should change and that they must "
    "confirm it — do not narrate it as already done."
)


_SINGLE_ACTION_DIRECTIVE = (
    "\n\nONE STEP IS ONE ACTION. The operator performs what you write on a real "
    "machine before they advance, and they cannot see the plan — so a walkthrough "
    "holding several self-contained pieces of work reads as one enormous task with "
    "no clear stopping point, and they lose track of where they are in it.\n"
    "  1. NEVER divide a walkthrough into phases, parts or stages. If you are about "
    "to write `Phase A`, `Part 2`, `Stage 1`, `First half`, or a SECOND `## Run "
    "this` heading — stop. That impulse is the signal that this step covers more "
    "than one action. Write ONLY the first piece of work and end there.\n"
    "  2. Keep `## Run this` to at most {max_steps} numbered actions. A single "
    "action MAY need a few commands (back up, edit, reload, check) — that is one "
    "action and it is fine. {max_steps} SEPARATE objectives is not.\n"
    "  3. ONE place. Every action in a walkthrough happens in the SAME place: one "
    "shell, or one web UI, or one physical device. Never mix a host shell with a "
    "router's admin page, a hypervisor's web console, or 'now go to a phone on "
    "cellular data' in the same step. If the work genuinely changes place, the "
    "step ends where the place changes.\n"
    "  4. Your `👉 Do this next` action and your `✅ Done when` condition must "
    "describe the SAME piece of work. If the finish line is not the visible "
    "result of the action you opened with, you have written more than one step.\n"
    "  5. Work you are NOT covering here: mention it in at most ONE closing line "
    "so the operator knows it is coming ('Publishing this externally comes "
    "next'). Do not write the instructions for it, and do not claim you have "
    "added, created or scheduled a step — you have not changed the plan."
)


def apply_single_action(
    system: str, *, is_decision: bool, enabled: bool, max_steps: int = 5,
) -> str:
    """§17.1011 — hold a walkthrough to ONE action, in one place.

    The live homelab job showed the failure this prevents: node T35
    ("Configure reverse proxy") emitted 4,817 chars over nine sections,
    internally split into ``Phase A`` (register a domain, point DNS, forward
    two router ports), ``Phase B`` (discover an IP, rewrite the Caddyfile,
    reload) and ``Phase C`` (apply a firewall group, then test over HTTPS from
    a phone on cellular). Three phases, eight numbered actions, four different
    execution contexts — presented as one step to finish before advancing.

    Note this is NOT what ``apply_verbosity`` controls: ``terse`` shortens the
    prose of each phase and leaves all three in place, because the defect is
    the step's SCOPE rather than its word count.

    No-op for decision nodes — their deliverable is a choice, not an action,
    and ``GUIDE_SYSTEM_DECISION`` already holds them to one choice at a time.
    """
    if not enabled or is_decision:
        return system
    return system + _SINGLE_ACTION_DIRECTIVE.format(max_steps=max_steps)


def apply_plan_authority(system: str, *, enabled: bool = True) -> str:
    """§17.937 — forbid the model from narrating plan mutations it cannot perform.

    Live (session 613dd1df / ADD3): asked to guide a step the operator wanted
    gone, the model answered *"The project plan has been updated to remove this
    step. No further action is required."* — four times over five days, while
    the node sat `pending` the entire time. The operator reasonably believed it
    and stopped acting; the step stayed in their plan for a week.

    This is the prevention half. `claims_plan_mutation` +
    `false_plan_claim_banner` in assist_guide are the enforcement half, because
    a prompt rule is a request and this codebase has a long record of them being
    ignored.
    """
    if not enabled:
        return system
    return system + _PLAN_AUTHORITY_DIRECTIVE


# §17.958 — a command that stops and waits is not a command that failed.
_INTERACTIVE_PROMPT_DIRECTIVE = (
    "\n\nTHE OPERATOR IS SITTING AT AN INTERACTIVE PROMPT RIGHT NOW. Their last "
    "message shows a command that STOPPED and is waiting for input — it has not "
    "failed and it has not finished.\n"
    "- Do NOT re-issue that command, or any variation of it. They are already "
    "inside it; running it again would abandon the run they are in and start "
    "over.\n"
    "- Your immediate action is a KEYSTROKE, not a shell command. Lead with "
    "exactly what to press.\n"
    "- The prompt in front of them is {how}\n"
    "- If you cannot tell from their paste which option is correct, say which "
    "one you recommend and why, in one line. Do not list every option.\n"
    "- Only after the prompt is answered does a shell command make sense again."
)


def apply_interactive_prompt(system: str, *, prompt: dict | None,
                             enabled: bool = True) -> str:
    """§17.958 — steer the reply to keystrokes while a prompt is pending.

    Live: an arrow-key menu answered with "simply type `eslint` and press
    Enter", alongside an instruction to re-run the command they were already
    halfway through. The keystroke advice is computed deterministically from
    the prompt KIND, so the model is told the right keys rather than guessing.
    """
    if not enabled or not prompt:
        return system
    return system + _INTERACTIVE_PROMPT_DIRECTIVE.format(
        how=prompt.get("how") or "a prompt waiting for input.")


# §17.959 — answer the question in the interface it was asked about.
_INTERFACE_FIDELITY_DIRECTIVE = (
    "\n\nTHE OPERATOR ASKED ABOUT A GRAPHICAL INTERFACE. Answer it THERE, in "
    "that interface, before anything else.\n"
    "- Give the exact navigation path, naming each thing they click in order "
    "(e.g. `Datacenter` → `Firewall` → `Security Group` → `Add`).\n"
    "- Name EVERY field on the screen they must fill or change, say what to put "
    "in each, and say plainly which of the remaining options to leave alone — "
    "'there are far more options than you are telling me about' is the "
    "complaint this rule exists to answer.\n"
    "- Do NOT substitute a command-line workaround for the answer, and do not "
    "tell them to skip the interface because it is confusing. If you believe "
    "the CLI or a config file is the better route, ANSWER THE GUI QUESTION "
    "FIRST, completely, and then offer the alternative as a choice they can "
    "make.\n"
    "- If you genuinely do not know the current layout of that screen, say so "
    "and ask them to paste or describe what they see. That is a real answer; "
    "redirecting them to a different interface is not."
)


def apply_interface_fidelity(system: str, *, gui: bool,
                             enabled: bool = True) -> str:
    """§17.959 — a GUI question gets a GUI answer.

    Live: *"I need a better explanation on implementing the firewall in the web
    browser… not enough context"* was answered with "the Web UI is confusing,
    skip the security groups for now" plus a `pct start`. The CLI may be the
    better route; declining to answer is still declining to answer.
    """
    if not enabled or not gui:
        return system
    return system + _INTERFACE_FIDELITY_DIRECTIVE


# §17.962 — the paste was clipped on the way into the shell.
_TRUNCATED_PASTE_DIRECTIVE = (
    "\n\nTHE OPERATOR'S LAST PASTE WAS TRUNCATED BY THEIR TERMINAL — not by "
    "anything wrong with the command. Evidence: {evidence}\n"
    "- The heredoc terminator never arrived on a line of its own, so the shell "
    "stayed inside the heredoc at its `>` continuation prompt. That is why it "
    "looked like a hung program and needed Ctrl-C.\n"
    "- The file on disk is therefore INCOMPLETE or EMPTY. Any symptom "
    "downstream of it — a blank page, a parse error, a service that will not "
    "start — is explained by that, and debugging the application instead is "
    "chasing a ghost.\n"
    "- Do NOT treat the command as wrong and do NOT switch approach. It was "
    "never executed as written.\n"
    "- Your next action is to have them CHECK the file (a byte count or the "
    "first and last lines), then re-send the write in SMALL pieces they paste "
    "one at a time. Never re-send the whole file as one block.\n"
    "- Say plainly, in one line, that their terminal clipped the paste, so they "
    "are not left thinking they mistyped something."
)


def apply_truncated_paste(system: str, *, truncated: dict | None,
                          enabled: bool = True) -> str:
    """§17.962 — name the real failure instead of debugging its shadow.

    Live: a ~3.6 KB heredoc arrived spliced, `App.jsx` was written corrupt, the
    page came up blank, and the following turns debugged React against garbage
    while the operator asked "did you read through the whole pasted command
    sequence?".
    """
    if not enabled or not truncated:
        return system
    return system + _TRUNCATED_PASTE_DIRECTIVE.format(
        evidence=(truncated.get("evidence") or "the paste is incomplete")[:200])


# §17.965 — the file on disk is not the file the engine wrote.
_FILE_MISMATCH_DIRECTIVE = (
    "\n\nA FILE THIS SESSION WROTE IS THE WRONG SIZE ON DISK. {detail}\n"
    "- This is arithmetic, not a judgement: the engine composed that file, so "
    "the expected byte count is exact. A different number means the write did "
    "not land whole — almost always a paste clipped by the operator's "
    "terminal.\n"
    "- Everything downstream of that file is therefore unexplained, not "
    "broken: a blank page, a syntax error, a service that will not start are "
    "all consistent with a half-written file. Do NOT debug the application, "
    "the framework, the dependencies or the config until the file is correct.\n"
    "- Do NOT ask the operator to investigate the symptom. Rewrite the file, "
    "in small pieces they paste one at a time, and finish with the byte count "
    "again.\n"
    "- Tell them in one line why, so they know it was the paste and not "
    "something they did."
)


def apply_file_mismatch(system: str, *, mismatches: list[dict] | None,
                        enabled: bool = True) -> str:
    """§17.965 — make a measured size difference the dominant fact of the turn.

    Live (T34): `App.jsx` sat at ~1000 of 2287 bytes with `export default App;`
    missing entirely, and the engine spent two turns on React while the answer
    was a subtraction it had all the inputs for.
    """
    if not enabled or not mismatches:
        return system
    detail = "; ".join(
        h["detail"] if h.get("detail") else (
            f"`{h['path']}` should be {h['expected']} bytes but measures "
            f"{h['observed']}"
            + (f" — {h['missing']} bytes missing" if h.get("short") else ""))
        for h in mismatches[:3])
    return system + _FILE_MISMATCH_DIRECTIVE.format(detail=detail)


# §17.968 — two files the engine wrote disagree with each other.
_CONTRACT_CONFLICT_DIRECTIVE = (
    "\n\nTWO FILES THIS SESSION WROTE CONTRADICT EACH OTHER. {detail}\n"
    "- This was read from their actual contents, not inferred from the "
    "symptom. It is a fact about the code, and it is the most likely cause of "
    "whatever is not working.\n"
    "- Rewriting either file unchanged cannot fix it, and neither can "
    "restarting anything. One side has to change to match the other.\n"
    "- Lead with the smaller change, say plainly which side you changed and "
    "why that side, and do not touch anything else in the same step.\n"
    "- Do not ask the operator to investigate the symptom. You wrote both of "
    "these files; the disagreement is yours to resolve."
)


def apply_contract_conflict(system: str, *, conflicts: list[dict] | None,
                            enabled: bool = True) -> str:
    """§17.968 — make a contradiction between the engine's own artefacts the
    dominant fact of the turn.

    Live: `server.js` returned an object from `/status` while `App.jsx` called
    `.find()` on it. React threw, the page went blank, and the engine spent
    three turns rewriting a third file that was already correct — with both
    contradicting files sitting in its own transcript.
    """
    if not enabled or not conflicts:
        return system
    detail = " ".join(c.get("detail", "") for c in conflicts[:2]).strip()
    return system + _CONTRACT_CONFLICT_DIRECTIVE.format(detail=detail)
