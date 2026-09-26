"""§17.1050 — the state check: stop the walkthrough, verify what is actually
true on the operator's system against everything the plan believes, then
walk through the repairs.

Why. Live (homelab T37 "validate the entire build"): each paste revealed one
more thing the plan assumed done but that was not — container 111 stopped,
container 120 stopped, VM 110 stopped, the backend never installed as a
service. The engine answered each with one more fix and one more "reply
confirm" offer; the step never converged and the fix path looked like it
had forgotten itself. The missing move was to STOP and re-establish ground
truth as a whole, not one symptom at a time.

Two halves, both grounded and both surfaced through machinery that exists:

1. PROBE — the ledger's claims (committed steps' recorded results, facts,
   pins) become one read-only, copy-paste script. Every probe is prefixed
   with an ``== <claim id> ==`` marker so its output can be attributed
   deterministically; a probe that could write anything is refused by a
   deterministic gate (``read_only_command``) before the operator sees it.
   The pending check is stashed on the session like a completion-confirm.

2. JUDGE — the pasted output is split by the markers; a model records a
   verdict per claim (confirmed / contradicted / unknown, one-line reason);
   a verdict is accepted only for a claim that was probed and whose section
   is non-empty. Contradicted FACTS are retracted from the ledger (§17.725's
   path). Contradicted STEP results are STAGED as plan changes through the
   §17.677 proposal — ``reopen`` (the result is gone, redo the step) or
   ``repair`` (insert a step that puts it right, via §17.736 add_step) — the
   operator confirms, nothing is applied silently.

Entry: the 🩺 button / ``command='verify_state'``, the phrase "verify
state" / "state check", and — the point of it — an automatic offer once
several fixes on one step have not resolved it.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.providers.base import Tool

from app.utils.cost_tracking import tagged_calls

logger = logging.getLogger("scaffold")

MARKER_RE = re.compile(r"^\s*==\s*([A-Z]:[A-Za-z0-9_.-]+)\s*==\s*$", re.M)
_MARKER_ECHO_RE = re.compile(r"echo\s+[\"']?==\s*[A-Z]:[A-Za-z0-9_.-]+\s*==[\"']?\s*$")
#: §17.1138 — "finish with what you have": the operator declines to paste the rest.
STATE_CHECK_SKIP_RE = re.compile(   # never a bare "skip": that is the step command
    r"(?i)^\s*(?:skip|finish|stop|end)\s+(?:the\s+)?(?:rest|remaining(?:\s+checks?)?|checks?|state\s*check)\b"
    r"|^\s*(?:that'?s|thats)\s+(?:all|everything)(?:\s+i\s+have)?\s*[.!]?\s*$")
STATE_CHECK_PHRASE_RE = re.compile(
    r"(?i)^\s*(?:/assist\s+)?(?:verify|check|confirm)\s+(?:the\s+)?(?:current\s+)?(?:state|build|system|where\s+we\s+are)\b"
    r"|^\s*state\s*check\b|^\s*🩺")

# Commands that change state. A probe is READ-ONLY by construction; anything
# matching here is refused before the operator sees it (a safety list, not an
# answer key — like assist_guide._CONSUMING_MARKERS).
_MUTATION_RE = re.compile(
    r"(?<![\w-])(?:rm|rmdir|mv|cp|dd|mkfs\w*|fdisk|parted|truncate|chmod|chown|chattr|ln|tee|touch|mkdir|"
    r"kill|pkill|killall|reboot|shutdown|poweroff|halt|init|useradd|userdel|passwd|"
    r"apt(?:-get)?(?![\w-])|dpkg(?![\w-])|yum|dnf|pacman|zypper|snap|pip3?|npm|yarn|cargo|make|"
    r"git\s+(?:clone|pull|checkout|reset|push)|sed\s+-i|perl\s+-i|"
    r"systemctl\s+(?:start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload|edit|set-property)|"
    r"service\s+\S+\s+(?:start|stop|restart|reload)|"
    r"(?:pct|qm)\s+(?:start|stop|shutdown|reboot|create|destroy|set|resize|migrate|clone|template|snapshot|rollback|delsnapshot|unlock|restore|move\w*|resize)|"
    # §17.1156 — the rest of the Proxmox management family (live: `pvesh create …/firewall/rules` passed as a read)
    r"pvesh\s+(?:create|set|delete)|pvesm\s+(?:add|remove|set|alloc|free|import|export|prune-backups)|"
    r"pveum\s+(?:add|modify|delete|passwd|useradd|usermod|userdel|groupadd|groupmod|groupdel|roleadd|rolemod|roledel|aclmod|acldel)|"
    r"pveum\s+(?:user|group|role|acl|pool|realm|token)\s+(?:add|modify|delete|remove|generate)|pvecm\s+(?:add|addnode|delnode|create|expected|updatecerts)|"
    r"pvenode\s+(?:startall|stopall|migrateall|wakeonlan)|pvenode\s+\S+\s+(?:set|delete)|"
    r"docker\s+(?:run|rm|rmi|stop|start|restart|kill|create|pull|push|build|exec\s+-it|compose\s+(?:up|down|restart|pull|rm))|"
    r"virsh\s+(?:start|destroy|shutdown|reboot|define|undefine|create)|"
    r"iptables|nft|ufw\s+(?:allow|deny|delete|enable|disable|reset)|firewall-cmd\s+--(?:add|remove|reload)|"
    r"ip\s+(?:link|addr|address|route|neigh)\s+(?:add|del|set|change|replace|flush)|"
    r"wg\s+set|wg-quick|nmcli\s+(?:con\w*|dev\w*)\s+(?:up|down|add|del|mod)|"
    r"zfs\s+(?:create|destroy|set|snapshot|rollback|send|receive)|zpool\s+(?:create|destroy|add|remove)|"
    r"crontab\s+-[er]|visudo|update-\w+|"
    r"curl\b[^\n|]*-X\s*(?:POST|PUT|DELETE|PATCH)|wget\b|"
    r"(?:>|>>)\s*(?!/dev/null|&\d)|\|\s*(?:sh|bash|zsh|python\d?)\b)"
)


# §17.1052 — a claim worth probing describes a DESIRED state. A fact that
# records a failure, an attempt, or a one-off past action ("VM 100 destroyed",
# "the Caddyfile was truncated", "the attempt used the wrong path") is history:
# probing it invites the judge to "make it true again" — live, that produced
# repairs that stopped a working backend, emptied a config and proposed
# deleting every VM. Such facts are left out of the claims.
_HISTORY_FACT_RE = re.compile(
    r"(?i)\b(?:fail(?:s|ed|ure)?|error|attempt(?:s|ed)?|cannot|could not|couldn't|not found|no such|wrong|typo|"
    r"refused|denied|missing|broken|crash(?:ed|es)?|"
    r"destroy(?:ed)?|remov(?:ed|al)|delet(?:ed|ion)|purg(?:ed)?|wip(?:ed)?|truncat(?:ed|ion)|clear(?:ed)?|"
    r"backed up|backup (?:of|created)|downloaded|snapshot(?:ted)?|was (?:re)?(?:written|placed|moved|stopped|started)|"
    r"were (?:re)?(?:written|placed|moved|stopped|started)|"
    # §17.1054 — the same shapes on STEP claims (recap DONE lines): a stop, an
    # audit snapshot, a precondition check. Live: "Ran `systemctl stop`" (ADD10,
    # later started again by design), "Collected raw state into …" (T1, an
    # inventory that is stale by construction), "VMID 102 confirmed free" (T13,
    # false the moment the step succeeded) were judged contradicted and the
    # steps reopened — the engine then walked the operator back through
    # stopping and deleting what it had just helped build.
    r"stopp?(?:ed)?\b|collected|gathered|audit(?:ed)?|inventor(?:y|ied)|"
    r"confirmed (?:free|available|unused|absent)|verified (?:free|available|unused))\b")

# A repair may only ADD or START what the claim says should exist. Anything that
# removes, stops or overwrites is never proposed by the engine — the operator
# is told the claim no longer holds and decides.
_DESTRUCTIVE_REPAIR_RE = re.compile(
    r"(?i)\b(?:stop|kill|remove|delete|destroy|purge|wipe|truncate|clear|empty|reset|uninstall|disable|"
    r"tear down|shut ?down|power off|drop|overwrite|revert|roll ?back|rm|format|reinstall|recreate from scratch)\b")


_CLAIM_MAX = 300


def _clip_claim(text_value: str, limit: int = _CLAIM_MAX) -> str:
    """§17.1054 — clip on a word boundary and mark it. A hard ``[:300]`` cut
    the live T17 claim to "… tags: me" (from "tags: media"); the judge then
    read the real output "tags: media" as a contradiction and proposed a step
    to retag the container."""
    t = (text_value or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    ws = cut.rfind(" ")
    if ws > limit // 2:
        cut = cut[:ws]
    return cut.rstrip(" ;,:-") + " …"


def probe_worthy(fact: str) -> bool:
    """False for facts that record history (failures, attempts, one-off past
    actions) rather than a state the system should still be in."""
    return not _HISTORY_FACT_RE.search(fact or "")


def destructive_repair(text_value: str) -> bool:
    return bool(_DESTRUCTIVE_REPAIR_RE.search(text_value or ""))


# §17.1056 — a contradiction argued from ABSENCE in unrelated output ("grep
# found no references to 'DeFruscio Media'; output only lists data files") is
# not evidence the setup is missing — it is evidence the probe looked in the
# wrong place. Live: S:T12 (Jellyfin first-time setup) → a "Complete Jellyfin
# first-time setup" repair the operator was asked to confirm.
_INDIRECT_REASON_RE = re.compile(
    r"(?i)\b(?:grep (?:found|returned|shows?) no|no (?:references?|mention|match(?:es)?|line) (?:to|of|for)\b|"
    r"does not (?:mention|list|show|contain|include)|only lists|not (?:present|found) in (?:the )?output|"
    r"absent from (?:the )?(?:output|listing)|nothing (?:in the output )?(?:mentions|references))")


def indirect_contradiction(verdict: dict) -> bool:
    """True when the judge's reason is an argument from absence."""
    return bool(_INDIRECT_REASON_RE.search(verdict.get("reason") or ""))


def reopen_refused(verdict: dict) -> bool:
    """§17.1054 — a `reopen` is refused when the step is itself a one-off or
    destructive action (its title says stop/remove/clear/audit…, or its
    recorded result is history): redoing it would undo later work."""
    title = (verdict.get("title") or "")
    claim = (verdict.get("claim") or "")
    return destructive_repair(title) or not probe_worthy(claim) or not probe_worthy(title)


_SUBCOMMAND_HEADS = frozenset({
    "systemctl", "service", "pct", "qm", "docker", "podman", "virsh", "ufw", "firewall-cmd", "ip", "wg", "wg-quick",
    "nmcli", "zfs", "zpool", "git", "sed", "perl", "crontab", "apt", "apt-get", "curl", "wget", "snap", "pip", "pip3",
    "npm", "yarn", "cargo", "make", "kill", "pkill", "nft", "iptables", "update-alternatives", "update-grub", "update-initramfs",
    "pvesh", "pvesm", "pveum", "pvecm", "pvenode",   # §17.1156
})

# §17.1150 — READ forms of tools whose HEAD is a mutation verb: `dpkg -l`,
# `iptables -S`, `pip list`, `crontab -l`… The head table stays conservative
# (a bare `iptables` is a write); these exact query shapes are the exception,
# judged on the argv, with an explicit deny list where a read flag can share
# a line with a write flag. THE SAME TABLE lives in scripts/local_runner_mcp.py
# (the runner re-gates on its side); tests/test_local_runner.py asserts parity.
_READ_FORMS: dict = {
    "dpkg": {"flags": {"-l", "-L", "-s", "-S", "-V", "-p", "--list", "--status", "--listfiles", "--search",
                       "--print-architecture", "--get-selections", "--print-avail", "--verify"}},
    "apt": {"subs": {"list", "show", "policy", "search", "depends", "rdepends"}},
    "apt-get": {"subs": {"check"}},
    "iptables": {"flags": {"-S", "-L", "--list", "--list-rules"},
                 "deny": {"-A", "-I", "-D", "-F", "-X", "-P", "-N", "-R", "-E", "-Z", "-C", "--append", "--insert", "--delete",
                          "--flush", "--policy", "--new-chain", "--delete-chain", "--rename-chain", "--replace", "--zero"}},
    "ip6tables": {"flags": {"-S", "-L", "--list", "--list-rules"},
                  "deny": {"-A", "-I", "-D", "-F", "-X", "-P", "-N", "-R", "-E", "-Z", "-C", "--append", "--insert", "--delete",
                           "--flush", "--policy", "--new-chain", "--delete-chain", "--rename-chain", "--replace", "--zero"}},
    "nft": {"subs": {"list"}},
    "pip": {"subs": {"list", "show", "freeze", "check", "debug"}},
    "pip3": {"subs": {"list", "show", "freeze", "check", "debug"}},
    "npm": {"subs": {"ls", "list", "view", "outdated", "version"}},
    "snap": {"subs": {"list", "info", "find", "version", "changes"}},
    "crontab": {"flags": {"-l"}, "deny": {"-e", "-r", "-i"}},
    "ufw": {"subs": {"status", "version", "show"}},
    "firewall-cmd": {"flag_prefix": ("--list-", "--get-", "--state", "--query-", "--info-", "--version")},
    "update-alternatives": {"flags": {"--display", "--list", "--query", "--get-selections"}},
    "make": {"flags": {"-n", "--dry-run", "-q", "--question", "-p", "--print-data-base", "--version"}},
    "nginx": {"flags": {"-t", "-T", "-v", "-V"}, "deny": {"-s", "-g"}},
    "apache2ctl": {"flags": {"-t", "-S", "-v", "-V", "-M", "configtest"}},
    "httpd": {"flags": {"-t", "-S", "-v", "-V", "-M"}},
    "sshd": {"flags": {"-t", "-T"}},
    "named-checkconf": {"flags": {"-z", "-p"}},
}


def read_form(argv) -> bool:
    """True when ``argv`` is one of the READ shapes in ``_READ_FORMS``."""
    rule = _READ_FORMS.get(argv[0].rsplit("/", 1)[-1]) if argv else None
    if not rule:
        return False
    args = list(argv[1:])
    if any(a in rule.get("deny", ()) for a in args):
        return False
    if "flags" in rule:
        return any(a in rule["flags"] for a in args)
    if "subs" in rule:
        first = next((a for a in args if not a.startswith("-")), None)
        return first in rule["subs"]
    if "flag_prefix" in rule:
        opts = [a for a in args if a.startswith("-")]
        return bool(opts) and all(a.startswith(rule["flag_prefix"]) for a in opts)
    return False


# §17.1171 — an INTERPRETER is never a read on its own; what it RUNS is the
# command, and when the gate cannot see that, it refuses. Audit 2026-09-25
# measured all of these passing BOTH gates: `bash -lc 'rm -rf /'`,
# `sudo bash -lc 'rm -rf /etc'`, `sh -ec 'shutdown -h now'`,
# `perl -le 'unlink "/etc/passwd"'`, `bash /tmp/x.sh`, `source /tmp/x.sh`, and
# a bare `bash` (an interactive shell). The head `bash -lc` matched no mutation
# verb and `shell_ast._SCRIPT_FLAGS` matched `-c` as an exact token, so the
# payload was never judged. Same shape as §17.1166 (`sudo`) and §17.1152
# (`pct exec … -- sh -c`): the wrapper is judged on what it runs.
#
# THE SAME TABLE + HELPER live in scripts/local_runner_mcp.py (the runner
# re-gates on its side); tests/test_local_runner.py pins them byte-equal.
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
# Languages this gate cannot judge. An inline script here is refused outright:
# "read-only Python" is not a question a shell-command gate can answer, and
# recursing the payload through the SHELL gate is unsound in both directions
# (live: `python3 -Ic "import os"` passed because no fragment looked like a
# shell mutation; `python3 -c "import os;os.system('rm -rf /x')"` was refused
# only because tree-sitter happened to surface `rm` as a head).
# NOT awk/sed: both are read-only in the idioms probes actually use
# (`… | awk 'NR==2{print $1}'` is pinned by an existing parity test) and their
# write forms (`awk '…system("rm")…'`, `sed -i`) are the denylist's business.
_OPAQUE_INTERPRETERS = frozenset({
    "python", "python2", "python3", "pypy", "pypy3", "perl", "ruby", "node",
    "nodejs", "php", "lua", "tclsh", "Rscript",
})
# A `source`d / `.`-ed file is an unseen script, exactly like `bash file.sh`.
_SOURCE_BUILTINS = frozenset({"source", "."})
# `-c` in any short-flag bundle: -c, -lc, -ec, -xc, -uc, -Ic …
_SHELL_SCRIPT_FLAG_RE = re.compile(r"^-[A-Za-z]*c$")


def interpreter_script(argv: list) -> str | None:
    """What an interpreter invocation RUNS, or ``None`` when ``argv`` is not one.

    Returns the INLINE script for a shell interpreter given ``-c`` in any
    short-flag bundle (``-c``, ``-lc``, ``-ec``, ``-xc``) or ``--command``.
    Returns ``""`` — meaning REFUSE — for every other interpreter shape: a
    script file, stdin, ``-s``, ``-m``, a bare interactive shell, a ``source``d
    file, or any opaque-language interpreter. ``""`` says the gate cannot see
    what runs, and a command it cannot see is not a read.
    """
    head = argv[0].rsplit("/", 1)[-1] if argv else ""
    if head in _SOURCE_BUILTINS or head in _OPAQUE_INTERPRETERS:
        return ""
    if head not in _SHELL_INTERPRETERS:
        return None
    args = list(argv[1:])
    for i, a in enumerate(args):
        if a == "--":
            break
        if a == "--command" or _SHELL_SCRIPT_FLAG_RE.match(a):
            return " ".join(args[i + 1:i + 2]).strip()
    return ""

# §17.1173 — the head must be a KNOWN READER. `_MUTATION_RE` is a ~90-verb
# DENYLIST, and the audit of 2026-09-25 measured what a denylist always
# measures: everything it does not name. All of these passed BOTH gates —
#   find -delete / -exec rm, tar -C /, rsync, shred, install, xargs rm,
#   awk 'BEGIN{system("rm -rf /x")}', systemd-run, nsenter, busybox rm,
#   lvremove, mount -o remount,rw /, git clean -fdx, psql -c 'DROP TABLE',
#   chsh, unlink, setfacl, wipefs, blkdiscard, sgdisk, mknod, chroot, …
# — because none of their verbs was on the list. The denylist stays as the
# first gate (it is battle-tested and encodes every §-numbered incident); this
# is the second, and it decides the other way round: a head the engine does
# not KNOW to be a reader is not a read.
#
# The tables were built against the operator's own probe history — 318 real
# commands replayed out of `assist_turns` — so they cover what the engine
# actually generates rather than what a list-writer imagines. That replay is
# also the acceptance test: 318/318 still allowed, 63/63 leak shapes refused.
#
# THE SAME TABLES + HELPER live in scripts/local_runner_mcp.py (the runner
# re-gates on its side); tests/test_local_runner.py pins them byte-equal.
# §17.1173 — shell KEYWORDS. The helper splits on `;`/`|`, so a loop arrives as
# the segments `for ip in A B`, `do <cmd>`, `done`. A loop HEADER runs nothing;
# `do`/`then`/`else` are followed by the real command and must be stripped, not
# trusted — `do rm -rf /` has to reach the verb.
_SHELL_HEADERS = frozenset({"for", "while", "until", "case", "select", "if", "elif"})
_SHELL_PREFIXES = frozenset({"do", "then", "else"})
_SHELL_CLOSERS = frozenset({"done", "fi", "esac", "}", "{", ";;"})

_READ_ONLY_HEADS = frozenset({
    # text and files
    "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "zgrep", "zcat",
    "cut", "tr", "sort", "uniq", "nl", "od", "xxd", "strings", "rev", "fold",
    "join", "comm", "diff", "cmp", "column", "md5sum", "sha1sum", "sha256sum",
    "b2sum", "cksum", "ls", "stat", "file", "readlink", "realpath", "basename",
    "dirname", "du", "df", "findmnt", "mountpoint", "lsattr", "getfacl",
    "namei", "tree",
    # shell meta with no effect
    "echo", "printf", "true", "false", "test", "[", "[[", ":", "seq", "expr",
    "date", "sleep", "which", "type", "pwd", "id", "groups", "whoami",
    "logname", "getent", "locale", "tty", "printenv",
    # system state
    "uname", "hostname", "arch", "nproc", "uptime", "free", "vmstat", "iostat",
    "mpstat", "lscpu", "lsmem", "lsblk", "lsusb", "lspci", "lsmod", "lsof",
    "dmidecode", "sensors", "smartctl", "nvidia-smi", "dmesg", "last", "w",
    "who", "ps", "pgrep", "pidof", "top", "htop",
    # network reads
    "ss", "netstat", "ping", "ping6", "traceroute", "tracepath", "mtr", "arp",
    "dig", "nslookup", "host", "whois", "getconf", "ethtool", "iw", "iwconfig",
    # package and module queries (binaries that cannot install)
    "dpkg-query", "apt-cache", "rpm", "modinfo", "ldconfig", "ldd",
    # LVM / storage display
    "lvs", "vgs", "pvs", "lvdisplay", "vgdisplay", "pvdisplay", "blkid",
    # nc/ncat SCAN (`nc -z host port`) is a read — the engine's own
    # assist_guest_reach.probe_commands emits it. Its LISTENER and EXEC forms
    # are a backdoor, denied below.
    "nc", "ncat", "netcat",
    # data shaping
    "jq", "yq", "base64", "numfmt",
    # find READS; its write ACTIONS are denied below. Dropping it entirely cost
    # 8 real probes (`pct exec 101 -- find /var/lib/jellyfin -iname '*.xml'`).
    "find",
    # curl: its WRITE shapes (-X/-d/--upload-file, -o to anything but /dev/null)
    # are refused twice over above — by `_MUTATION_RE` and by the dedicated
    # curl/wget branch. `wget` is deliberately ABSENT: `_MUTATION_RE` denies it
    # unconditionally and nothing should quietly re-open it here.
    "curl",
})

# Mixed programs: the first NON-FLAG argument must name a read subcommand.
# `_MUTATION_RE`'s write-subcommand patterns still run first, so these two
# tables disagree only in the direction they fail — which is the point.
_READ_SUBCOMMANDS = {
    "systemctl": {"status", "show", "cat", "is-active", "is-enabled", "is-failed",
                  "is-system-running", "list-units", "list-unit-files",
                  "list-timers", "list-sockets", "list-dependencies", "list-jobs",
                  "get-default", "show-environment"},
    "journalctl": set(),        # flag-only: no subcommand vocabulary to gate on
    "firewall-cmd": set(),      # ditto; its write flags are denied above
    "pct":   {"config", "status", "list", "df", "listsnapshot", "pending",
              "cpusets", "exec", "enter"},
    "qm":    {"config", "status", "list", "listsnapshot", "pending", "showcmd",
              "cloudinit", "agent", "guest", "monitor", "terminal"},
    "pvesh": {"get", "ls", "usage"},
    "pvesm": {"status", "list", "path", "scan", "apiinfo"},
    "pveum": {"user", "group", "role", "acl", "pool", "realm", "token"},
    "pvecm": {"status", "nodes", "keygen", "apiver"},
    "pvenode": {"config", "cert", "task"},
    "pveam": {"available", "list", "update"},
    "pve-firewall": {"status", "compile", "localnet", "simulate"},
    "zfs":   {"list", "get", "holds", "version"},
    "zpool": {"list", "status", "get", "history", "iostat", "version"},
    "ip":    {"addr", "address", "a", "link", "l", "route", "r", "neigh", "n",
              "rule", "netns", "maddr", "mroute", "tunnel", "monitor"},
    "bridge": {"link", "fdb", "vlan", "mdb", "monitor"},
    "docker": {"ps", "inspect", "logs", "images", "image", "version", "info",
               "stats", "top", "port", "history", "events", "diff", "context"},
    "podman": {"ps", "inspect", "logs", "images", "version", "info", "stats", "top"},
    "virsh": {"list", "dominfo", "domstate", "dumpxml", "domblklist",
              "domiflist", "nodeinfo", "version", "capabilities", "pool-list",
              "net-list"},
    "git":   {"status", "log", "diff", "show", "branch", "remote", "rev-parse",
              "describe", "ls-files", "ls-remote", "blame", "tag", "cat-file",
              "config", "shortlog", "reflog"},
    "tailscale": {"status", "ip", "netcheck", "version", "whois", "ping", "dns",
                  "licenses"},
    "caddy": {"version", "validate", "fmt", "environ", "list-modules", "adapt"},
    "nmcli": {"device", "dev", "connection", "con", "general", "networking",
              "radio", "monitor"},
    "wg":    {"show", "showconf"},
    "ufw":   {"status", "version", "show", "app"},
    "apt":   {"list", "show", "policy", "search", "depends", "rdepends"},
    "apt-get": {"check"},
    "snap":  {"list", "info", "find", "version", "changes", "connections"},
    "pip":   {"list", "show", "freeze", "check", "debug", "config"},
    "pip3":  {"list", "show", "freeze", "check", "debug", "config"},
    "npm":   {"ls", "list", "view", "outdated", "version", "config", "root", "prefix"},
    "timedatectl": {"status", "show", "list-timezones"},
    "hostnamectl": {"status", "show"},
    "loginctl": {"list-sessions", "list-users", "show-session", "show-user",
                 "session-status"},
    "resolvectl": {"status", "query", "statistics", "dns", "domain"},
    "networkctl": {"status", "list", "lldp"},
    "pm2": {"list", "ls", "status", "show", "describe", "logs", "info", "jlist",
            "prettylist", "env", "report"},
}

# A few allowlisted readers carry ONE write flag. The same shape as the
# _READ_FORMS deny sets, applied from the read side.
# find's write ACTIONS — the C1 leak (`find /tmp -delete`,
# `find / -name '*.log' -exec rm -f {} \;`). Prefix-matched: -exec/-execdir/-ok
# /-okdir/-fprint/-fprintf/-fls all run or write.
_FIND_WRITE_PREFIXES = ("-delete", "-exec", "-ok", "-fprint", "-fls")

_READ_HEAD_DENY_FLAGS = {
    "dmesg": {"-C", "--clear", "-c", "--read-clear"},   # clears the kernel ring buffer
    "ss":    {"-K", "--kill"},                          # kills sockets
    "nc":    {"-l", "-L", "-k", "-e", "-c", "--exec", "--sh-exec", "--lua-exec"},
    "ncat":  {"-l", "-L", "-k", "-e", "-c", "--exec", "--sh-exec", "--lua-exec"},
    "netcat": {"-l", "-L", "-k", "-e", "-c", "--exec", "--sh-exec"},
    "yq":    {"-i", "--inplace", "--in-place"},         # edits in place
    "jq":    {"-i", "--in-place"},
}

# awk reads in every idiom the corpus uses (`… | awk 'NR==2{print $1}'`) and
# WRITES the moment its program calls out: `awk 'BEGIN{system("rm -rf /x")}'`
# was a measured leak. Judge the program text, not the head. Same for sed's
# in-place and `w` forms.
_AWK_WRITE_RE = re.compile(r"\bsystem\s*\(|\bclose\s*\(|>\s*[\"']|\|\s*[\"']|\bENVIRON\b")
_SED_WRITE_RE = re.compile(r"--in-place|(?<![\w-])-i(?![\w-])|(?:^|;)\s*w\s|\bw\s+/")


def head_reads(argv: list) -> bool:
    """True when this command's HEAD is a program that can only read.

    The complement of `_READ_FORMS` (which admits read SHAPES of write-headed
    tools): this admits read HEADS, and refuses everything it does not know.
    """
    if not argv:
        return False
    name = argv[0].rsplit("/", 1)[-1]
    if name == "command":
        return any(a in ("-v", "-V") for a in argv[1:])   # a lookup, not an invocation
    if name in _SHELL_HEADERS or name in _SHELL_CLOSERS:
        return True                   # shell syntax, not a command: nothing runs
    if name in _SHELL_PREFIXES:
        return head_reads(argv[1:])   # judge the command the keyword introduces
    if name == "find":
        return not any(a.startswith(_FIND_WRITE_PREFIXES) for a in argv[1:])
    if name in ("awk", "gawk", "mawk", "nawk"):
        return not any(_AWK_WRITE_RE.search(a) for a in argv[1:])
    if name == "sed":
        return not any(_SED_WRITE_RE.search(a) for a in argv[1:])
    if name in _READ_ONLY_HEADS:
        deny = _READ_HEAD_DENY_FLAGS.get(name)
        return not (deny and any(a in deny for a in argv[1:]))
    subs = _READ_SUBCOMMANDS.get(name)
    if subs is None:
        return False
    if not subs:                      # flag-only tool; its writes are denied above
        return True
    sub = next((a for a in argv[1:] if not a.startswith("-")), "")
    return sub in subs


def _requote(parts: list) -> str:
    """§17.1173 — rebuild a command STRING from an already-split argv without
    losing what the quotes were doing.

    `" ".join(argv)` was the old reconstruction and it re-parses differently:
    `pct exec 111 -- sh -c 'command -v curl; command -v gpg'` arrives as
    `[..., 'sh', '-c', 'command -v curl; command -v gpg']`, re-joins to
    `sh -c command -v curl; command -v gpg`, and re-splits as `sh -c command`
    + `-v curl` + `command -v gpg` — so the gate judged the bare word
    `command`. Measured on 318 real probes: 11 were refused for exactly that,
    and the §17.1173 allowlist turned the rest into hard refusals because the
    mangling invents heads like `-lc`. shlex.quote puts the quoting back.
    """
    return " ".join(shlex.quote(p) for p in parts).strip()


_SSH_FLAGS_WITH_ARG = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-W", "-b", "-c", "-e", "-I", "-m", "-O", "-Q", "-S", "-w", "-E", "-B"}


_CONTAINER_EXEC = {"pct": ("exec",), "lxc-attach": (), "qm": ("guest", "exec")}


def container_exec_remainder(argv: list) -> Optional[str]:
    """``pct exec <ct> [--] <cmd…>`` / ``qm guest exec <vm> [--] <cmd…>`` /
    ``lxc-attach -n <ct> -- <cmd…>``: the command that runs INSIDE the guest,
    or None when argv is not such a shape. §17.1152 — live: `pct exec 111 --
    sh -c 'rm -rf /x'` passed this gate (the wrapper head is a read)."""
    head = argv[0].rsplit("/", 1)[-1] if argv else ""
    if head not in _CONTAINER_EXEC:
        return None
    subs = _CONTAINER_EXEC[head]
    if tuple(argv[1:1 + len(subs)]) != subs:
        return None
    rest = list(argv[1 + len(subs):])
    if "--" in rest:
        return _requote(rest[rest.index("--") + 1:]) or None
    i = 0
    while i < len(rest) and (rest[i].startswith("-") or rest[i].isdigit()):
        i += 2 if rest[i] in ("-n", "--timeout") else 1
    return _requote(rest[i:]) or None


# §17.1166 — privilege/environment WRAPPERS. Live (ADD65, 2026-09-22 23:02):
# `sudo apt install -y qemu-guest-agent` PASSED this gate while the bare
# `apt install -y qemu-guest-agent` was refused — `argv[0]` is `sudo`, which is
# no mutation verb, and the real command was never judged. The engine sent the
# mutation to the runner (only the helper's own gate stopped it: "refused by
# the local runner: mutation verb apt"), and because the block did not read as
# a write, §17.1156's "the first writing block ends the scan" never fired.
# Same shape as `container_exec_remainder` / `ssh_remote_read_only`: a wrapper
# is judged on what it RUNS. Fail closed — a wrapper with nothing after it
# (`sudo -i`, a bare `time`) is an interactive shell, not a look-up.
_PRIV_WRAPPERS = ("sudo", "doas", "env", "nohup", "nice", "ionice", "stdbuf",
                  "command", "time", "timeout", "setsid")
_WRAPPER_FLAG_WITH_ARG = {
    "sudo": {"-u", "-g", "-p", "-C", "-D", "-h", "-R", "-T", "-U",
             "--user", "--group", "--prompt", "--chdir", "--host",
             "--close-from", "--command-timeout", "--other-user"},
    "doas": {"-u", "-C"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
}


def privilege_wrapper_remainder(argv: list) -> Optional[str]:
    """``sudo`` / ``doas`` / ``env VAR=v`` / ``nohup`` / ``timeout 5s`` …: the
    command the wrapper actually runs. ``None`` when argv is not a wrapper;
    ``""`` when the wrapper carries no command (refuse — see above)."""
    head = argv[0].rsplit("/", 1)[-1] if argv else ""
    if head not in _PRIV_WRAPPERS:
        return None
    # §17.1173 — `command -v X` / `-V X` ASKS whether X exists; it does not run
    # it. Unwrapping it to X made `command -v gpg` a refusal the moment the
    # allowlist landed (gpg is not a reader — but nothing ran it). `which` and
    # `type` are plain readers already; `command` needs the carve-out because it
    # is also a genuine wrapper (`command rm -rf /` DOES run rm).
    if head == "command" and any(a in ("-v", "-V") for a in argv[1:]):
        return None
    flags = _WRAPPER_FLAG_WITH_ARG.get(head, set())
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            i += 2 if a in flags else 1
            continue
        if head == "env" and "=" in a[1:]:   # env NAME=VALUE … cmd
            i += 1
            continue
        break
    if head == "timeout" and i < len(argv):
        i += 1                               # the DURATION, not the command
    return _requote(argv[i:])


def ssh_remote_read_only(argv: list, judge) -> tuple[bool, str]:
    """``ssh [opts] [user@]host <remote command>``: the remote command must pass
    the same gate; no remote command (an interactive login) is refused."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _SSH_FLAGS_WITH_ARG else 1
    if i >= len(argv):
        return False, "ssh without a host"
    # NOT _requote: ssh JOINS its remaining arguments into one command line and
    # hands that to the remote shell, so a plain join is what actually runs.
    # (`pct exec` / the wrappers exec an argv instead — those keep _requote.)
    remote = " ".join(argv[i + 1:]).strip()
    if not remote:
        return False, "interactive ssh"
    res = judge(remote)
    ok = res[0] if isinstance(res, tuple) else bool(res)
    return (True, "") if ok else (False, "ssh remote command writes")


def read_only_command(cmd: str) -> bool:
    """True when the command can only read. Conservative: any mutation verb at
    the HEAD of any command (nested scripts and substitutions included), any
    redirect that is not to /dev/null, any pipe into an interpreter → False.

    §17.1067 — judged on the tree-sitter-bash AST (`shell_ast.analyze`), not
    on the raw text: the regex over the whole line refused read-only probes
    whose ARGUMENTS carried a verb (`getent passwd prowlarr`, `ls /etc/apt`,
    `grep 'rm -rf' /var/log/syslog`; live, 6 of 48 probes) and could not see
    `$(…)` or `bash -c "…"`. The mutation regex still decides which HEADS are
    mutations (first four tokens of each command) — one verb table, applied
    to structure. A parse error fails closed."""
    c = (cmd or "").strip()
    if not c or c.startswith("#"):
        return False
    from app.modules.shell_ast import analyze
    facts = analyze(c)
    if facts.parse_error:
        return False
    if facts.redirect_targets or facts.piped_to_interpreter:
        return False
    if not facts.commands:
        return False
    for argv, head in zip(facts.commands, facts.heads, strict=True):
        if argv[0].rsplit("/", 1)[-1] == "ssh":   # §17.1151 — the remote command is judged like a local one
            ok, _why = ssh_remote_read_only(argv, read_only_command)
            if not ok:
                return False
            continue
        wrapped = privilege_wrapper_remainder(argv)  # §17.1166 — sudo/env/nohup/timeout: judge what it RUNS
        if wrapped is not None:
            if not wrapped or not read_only_command(wrapped):
                return False
            continue
        inner = container_exec_remainder(argv)   # §17.1152 — pct exec / qm guest exec / lxc-attach: judge what runs INSIDE
        if inner is not None:
            if not read_only_command(inner):
                return False
            continue
        script = interpreter_script(argv)   # §17.1171 — an interpreter is judged on what it RUNS
        if script is not None:
            if not script or not read_only_command(script):
                return False
            continue
        if read_form(argv):        # §17.1150 — `dpkg -l`, `iptables -S`, `pip list`…
            continue
        # argv[0] alone decides for most commands; the joined unquoted head
        # (`systemctl start`, `pct destroy`, `ip link add`, `sed -i`) only
        # for the families whose verb is a subcommand or a flag.
        probe = head if argv[0] in _SUBCOMMAND_HEADS else argv[0]
        if _MUTATION_RE.search(" " + probe + " "):
            return False
        if not head_reads(argv):   # §17.1173 — and the head must be a KNOWN reader
            return False
        if argv[0] in ("curl", "wget") and any(a in ("-X", "--request", "-d", "--data", "--data-raw", "--upload-file", "-T", "-o", "-O") for a in argv[1:]):
            # curl/wget that WRITE (a method override, a body, or a download to disk)
            # — `-o /dev/null` is the one read-only shape
            outs = [argv[i + 1] for i, a in enumerate(argv) if a in ("-o", "-O") and i + 1 < len(argv)]
            if not (outs and all(o == "/dev/null" for o in outs) and not any(a in ("-X", "--request", "-d", "--data", "--data-raw", "--upload-file", "-T") for a in argv[1:])):
                return False
    try:
        from app.modules.assist_guide import find_shell_unsafe_commands
        if find_shell_unsafe_commands("```bash\n" + c + "\n```"):
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Claims — what the plan believes, with an id per claim.
# ---------------------------------------------------------------------------

async def build_claims(*, db, session_id: str, max_facts: int = 24) -> dict:
    """``{claims: [{id, kind, node_key?, title?, text}], job_id, environment}``.
    Claims come from committed steps (their recorded result), the facts
    ledger and the operator's pins. Deterministic; no model."""
    from app.modules.assist_environment import _environment_from_metadata
    from app.modules.assist_render import parse_recap
    sess = (await db.execute(text("""
        SELECT job_id, metadata, current_node_key FROM assist_sessions WHERE id = :sid
    """), {"sid": session_id})).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    job_id = str(sess["job_id"])
    env = _environment_from_metadata(sess.get("metadata"))
    rows = (await db.execute(text("""
        SELECT s.node_key, n.title, s.evidence, s.progress_recap, s.committed_at
          FROM assist_steps s JOIN dag_nodes n ON n.job_id = s.job_id AND n.node_key = s.node_key
         WHERE s.session_id = :sid AND s.status = 'committed'
         ORDER BY n.execution_order
    """), {"sid": session_id})).mappings().all()
    claims: list[dict] = []
    skipped_history = 0
    for r in rows:
        done = parse_recap(r.get("progress_recap")).get("done") or []
        # §17.1054 — the evidence head is clipped on a word too: the live T17
        # claim was this exact ``[:240]`` cut landing on "tags: me".
        summary = ("; ".join(str(d) for d in done[:3]) if done
                   else _clip_claim(" ".join((r.get("evidence") or "").split()), 240))
        if not summary:
            summary = f"step completed: {r.get('title') or r['node_key']}"
        # §17.1054 — a step whose recorded result is a one-off action or a
        # snapshot is history, exactly like a history fact: not a state the
        # system should still be in, so not a claim to probe. Its title tells
        # the same story ("Stop …", "Remove …", "Audit …").
        if not probe_worthy(summary) or destructive_repair(r.get("title") or ""):
            skipped_history += 1
            continue
        claims.append({"id": f"S:{r['node_key']}", "kind": "step", "node_key": r["node_key"],
                       "title": r.get("title") or "", "text": _clip_claim(summary)})
    if skipped_history:
        logger.info("state_check_history_steps_skipped session_id=%s n=%d", session_id, skipped_history)
    facts = [str(f).strip() for f in (env.get("facts") or []) if str(f).strip()]
    facts = [f for f in facts if probe_worthy(f)]  # §17.1052 — desired state only
    for i, f in enumerate(facts[-max_facts:]):
        claims.append({"id": f"F:{i + 1}", "kind": "fact", "text": f[:300]})
    for k, v in (env.get("substitutions") or {}).items():
        claims.append({"id": f"K:{k}", "kind": "pin", "text": f"{k} = {v}"})
    return {"claims": claims, "job_id": job_id, "environment": env,
            "current_node_key": sess.get("current_node_key")}


# ---------------------------------------------------------------------------
# Probe — one read-only command per claim, gated, rendered as one script.
# ---------------------------------------------------------------------------

PLAN_PROBES_TOOL = Tool(
    name="plan_state_probes",
    description="For each claim about the operator's system, one READ-ONLY shell command whose output shows whether the claim still holds.",
    input_schema={
        "type": "object",
        "properties": {
            "probes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The claim id, verbatim."},
                        "command": {"type": "string",
                                    "description": "One read-only command (no writes, no service changes, no installs). Empty when the claim is not checkable from this shell."},
                        "expect": {"type": "string", "description": "One line: what the output shows when the claim holds."},
                    },
                    "required": ["id", "command"],
                },
            }
        },
        "required": ["probes"],
    },
)

_PROBE_OPENING = (
    "The operator is partway through a guided build and something the plan assumed done may no longer "
    "be true. Below are the claims the plan currently believes about their system, each with an id. For "
    "each claim, give ONE read-only shell command, runnable from the operator's usual shell, whose output "
    "shows whether the claim still holds now — status queries, listings, config reads, connectivity "
    "checks. Never a command that starts, stops, installs, writes, edits or deletes anything. Leave the "
    "command empty when a claim cannot be checked from a shell. Use the operator's actual context "
    "(host, containers, addresses) exactly as the claims state it."
)


_PROBE_BATCH = 10
_MAX_PROBES = 48


@tagged_calls("assist.state_check.plan_probes")
async def plan_probes(claims: list[dict], environment: Optional[dict], *,
                      model_overrides: Optional[dict] = None, on_progress=None) -> tuple[list[dict], list[dict]]:
    """``(probes, refused)`` — probes the read-only gate accepted, and the
    ones it refused (logged, never shown). Claims go to the model in batches
    of ``_PROBE_BATCH`` (live: 86 claims in one call returned NOTHING) up to
    ``_MAX_PROBES`` probes; pins are context, not probe targets. Fail-soft
    per batch → the other batches still count."""
    targets = [c for c in claims if c.get("kind") != "pin"]
    if not targets:
        return [], []
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    profile = str((environment or {}).get("profile") or "")
    pins = [c["text"] for c in claims if c.get("kind") == "pin"][:20]
    context = ("\n\nOPERATOR CONTEXT:\n" + profile[:600] if profile else "") + \
              ("\n\nKNOWN VALUES (the operator's pins):\n" + "\n".join(f"- {p}" for p in pins) if pins else "")
    by_id = {c["id"]: c for c in targets}
    probes: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    batches = [targets[i:i + _PROBE_BATCH] for i in range(0, len(targets), _PROBE_BATCH)]
    for bi, batch in enumerate(batches, 1):
        if len(probes) >= _MAX_PROBES:
            break
        if on_progress is not None:
            try:
                await on_progress(bi, len(batches), len(probes))
            except Exception:
                pass
        claims_block = "\n".join(f"- {c['id']}: {c['text']}" for c in batch)
        msg = _PROBE_OPENING + context + f"\n\nCLAIMS (give a probe for each of the {len(batch)}, or an empty command):\n" + claims_block
        try:
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": msg}], tools=[PLAN_PROBES_TOOL],
                role="model_general", overrides=model_overrides, temperature=0.0,
                tool_choice="auto", max_tokens=2500)
            raw = ((read_tool_args(resp) or {}).get("probes")) or []
        except Exception as exc:
            logger.warning("state_check_probe_model_failed batch=%d/%d: %r", bi, len(batches), exc)
            raw = []
        got = 0
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            cmd = " ".join(str(item.get("command") or "").split())
            if cid not in by_id or cid in seen or not cmd:
                continue
            seen.add(cid)
            if not read_only_command(cmd):
                refused.append({"id": cid, "command": cmd[:120]})
                continue
            probes.append({"id": cid, "command": cmd[:300], "expect": str(item.get("expect") or "")[:200],
                           "claim": by_id[cid]["text"], "kind": by_id[cid]["kind"],
                           "node_key": by_id[cid].get("node_key")})
            got += 1
            if len(probes) >= _MAX_PROBES:
                break
        logger.info("state_check_probe_batch %d/%d claims=%d probes=%d", bi, len(batches), len(batch), got)
    logger.info("state_check_probes claims=%d targets=%d probes=%d refused=%r", len(claims), len(targets),
                len(probes), [(r["id"], r["command"][:50]) for r in refused][:4])
    return probes, refused


def render_probe_script(probes: list[dict]) -> str:
    lines = []
    for p in probes:
        lines.append(f'echo "== {p["id"]} =="')
        lines.append(p["command"])
    return "\n".join(lines)


def _runner_offer(n: int) -> str:
    """§17.1105 — at the moment of the paste-back toil, offer the engine's own
    local runner (it exists but was never suggested). Shown only when no runner
    is configured; once it is, assist_turn runs the probes itself (§17.1077)."""
    return (
        f"\n\n💡 **Don't want to paste {n} command{'s' if n != 1 else ''}?** I can run these "
        "read-only checks for you — set up the local runner once (**Capabilities → "
        "\"Let the state check run its own commands\" → Walk me through it**) and from then on "
        "I run the state check myself, no copy-paste."
    )


def render_continuation_message(judged: list[dict], remaining: list[dict], *, missing_ids: list[str],
                                deferred: int, total: int) -> str:
    """§17.1138 — after a PARTIAL paste: what was judged so far, which ids the
    paste did not cover, and the next script (the uncovered ids first, then the
    probes deferred by the per-batch budget)."""
    n = {"confirmed": 0, "contradicted": 0, "unknown": 0}
    for v in judged:
        n[v["verdict"]] = n.get(v["verdict"], 0) + 1
    head = (f"🩺 **State check — {len(judged)} of {total} answered so far: "
            f"✅ {n['confirmed']} confirmed · ❌ {n['contradicted']} contradicted · ❔ {n['unknown']} unclear.**")
    miss = (f"\nThe paste did not cover: {', '.join(f'`{i}`' for i in missing_ids[:12])}"
            f"{'…' if len(missing_ids) > 12 else ''} — "
            "the markers for those were absent (a partial run, or the `== … ==` lines got dropped)."
            if missing_ids else "")
    nxt = (f"\n\nNext {len(remaining)} check{'s' if len(remaining) != 1 else ''}"
           f"{f' ({deferred} of them were held back from the first script)' if deferred else ''} — "
           "same rule, every command only reads. Paste the output back, or say **skip the rest** "
           "to finish with what we have:\n\n```bash\n" + render_probe_script(remaining) + "\n```")
    return head + miss + nxt


def render_probe_message(probes: list[dict], *, checked: int, unchecked: int,
                         offer_runner: bool = False) -> str:
    if not probes:
        if unchecked:
            return (f"🩺 **State check** — I could not build the checks this time: the model returned no usable "
                    f"command for any of the {unchecked} claims (a generation miss, not a verdict on your system). "
                    "Press 🩺 Verify state again to retry, or tell me in your own words what is and is not working.")
        return ("🩺 **State check** — the plan has recorded nothing I can verify from a shell yet. Tell me in your "
                "own words what is and is not working and I will take it from there.")
    what = "\n".join(f"- `{p['id']}` {p['claim'][:110]}" for p in probes[:20])
    return (
        f"🩺 **State check — {len(probes)} thing{'s' if len(probes) != 1 else ''} the plan believes about your system"
        f"{f' ({checked} claims checked)' if checked and checked != len(probes) else ''}.**\n"
        "Nothing here changes anything: every command only reads. Paste the whole block into your usual "
        "shell, then paste ALL of the output back here (keep the `== … ==` lines).\n\n"
        "```bash\n" + render_probe_script(probes) + "\n```\n\n"
        "What each line checks:\n" + what
        + (f"\n\n({unchecked} claim{'s' if unchecked != 1 else ''} cannot be checked from a shell and are left as they are.)" if unchecked else "")
        + (_runner_offer(len(probes)) if offer_runner else "")
    )


# ---------------------------------------------------------------------------
# Judge — attribute output by marker, model verdicts, deterministic gates.
# ---------------------------------------------------------------------------

def attribute_sections(pasted: str) -> dict[str, str]:
    """``{claim id: output text}`` split on the ``== id ==`` markers the
    script printed. Text before the first marker is ignored."""
    out: dict[str, str] = {}
    # The operator's shell echoes `echo "== id =="` (prompt + command) before
    # the marker itself prints; that echo belongs to no section.
    cleaned = "\n".join(ln for ln in (pasted or "").splitlines()
                        if not _MARKER_ECHO_RE.search(ln))
    parts = MARKER_RE.split(cleaned)
    # parts: [prefix, id1, text1, id2, text2, ...]
    for i in range(1, len(parts) - 1, 2):
        cid, body = parts[i].strip(), parts[i + 1]
        body_lines = [ln for ln in body.splitlines() if ln.strip()]
        out[cid] = ("\n".join(body_lines)).strip()
    return out


RECORD_VERDICTS_TOOL = Tool(
    name="record_state_verdicts",
    description="One verdict per probed claim, from its command output only.",
    input_schema={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["confirmed", "contradicted", "unknown"]},
                        "reason": {"type": "string", "description": "One line quoting the decisive output."},
                        "repair": {"type": "string",
                                   "description": "For a contradicted claim: what has to be done to make it true again, as a one-line task for the operator (empty when the step must simply be redone)."},
                        "caused_by": {"type": "string",
                                      "description": "For a contradicted claim that is a CONSEQUENCE of another contradicted claim (a service unreachable because its container is gone): that claim's id. Empty when this claim is its own cause."},
                    },
                    "required": ["id", "verdict"],
                },
            }
        },
        "required": ["verdicts"],
    },
)

_JUDGE_OPENING = (
    "For each claim below, decide from ITS command output only whether the claim still holds: "
    "confirmed (the output shows it), contradicted (the output shows it is not so — stopped, missing, "
    "refused, absent), or unknown (the output does not settle it, or errored for an unrelated reason). "
    "Quote the decisive part of the output in the reason. For a contradicted claim, say in one line what "
    "the operator must do to make it true again, using only what the output and the claim state. When "
    "several contradicted claims share ONE cause (a container that no longer exists makes its port, its "
    "mounts and its HTTP response all fail), name the root claim's id in `caused_by` on the others, so "
    "there is one repair, not three. A repair only ADDS or STARTS what should exist; never propose "
    "stopping, removing, emptying or deleting anything — if the only way to satisfy a claim is destructive, "
    "leave `repair` empty. A claim about a one-off past action (something destroyed, backed up, "
    "downloaded, rewritten) is CONFIRMED when its effect is present and is never repaired by repeating it. "
    "Such a claim is contradicted ONLY if the specific object it names is back (the destroyed VM listed "
    "again, the truncated file's old content restored); other things existing or running now do not "
    "contradict it. A claim ending in an ellipsis (…) was clipped — judge only the part you can read. "
    "Judge only what the output DIRECTLY shows: a setting, an account, a completed wizard is UNKNOWN "
    "unless the output states it — a string being absent from a listing of unrelated files is not a "
    "contradiction."
)


@tagged_calls("assist.state_check.judge_outputs")
async def judge_outputs(probes: list[dict], pasted: str, *,
                        model_overrides: Optional[dict] = None) -> list[dict]:
    """Per-claim verdicts. A verdict is kept only for a probed claim whose
    section is non-empty; missing sections are ``unknown``. Fail-soft."""
    sections = attribute_sections(pasted)
    probed = {p["id"]: p for p in probes}
    verdicts: dict[str, dict] = {}
    for cid, p in probed.items():
        verdicts[cid] = {"id": cid, "verdict": "unknown", "reason": "no output pasted for this check",
                         "repair": "", "kind": p["kind"], "node_key": p.get("node_key"), "claim": p["claim"],
                         "title": p.get("title") or ""}
    # A marker that is PRESENT with nothing under it is evidence too ("docker
    # ps … returns no rows"); only an absent marker is "no output pasted".
    with_output = {cid: (sections[cid] or "(no output — the command printed nothing)")
                   for cid in probed if cid in sections}
    if not with_output:
        return list(verdicts.values())
    # Deterministic pre-pass: the probe said what the output looks like when
    # the claim holds; when that text is literally there, it is confirmed.
    to_judge: dict[str, str] = {}
    for cid, txt in with_output.items():
        exp = " ".join((probed[cid].get("expect") or "").split()).lower()
        if len(exp) >= 6 and exp in " ".join(txt.split()).lower():
            verdicts[cid].update({"verdict": "confirmed", "reason": f"output contains the expected '{exp[:80]}'"})
        else:
            verdicts[cid]["reason"] = "the judge returned no verdict for this check"
            to_judge[cid] = txt
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    ids = list(to_judge)
    for start in range(0, len(ids), _JUDGE_BATCH):  # small batches: a long list gets truncated verdicts
        batch = ids[start:start + _JUDGE_BATCH]
        block = "\n\n".join(
            f"[{cid}] CLAIM: {probed[cid]['claim']}\nCOMMAND: {probed[cid]['command']}\n"
            f"EXPECTED WHEN TRUE: {probed[cid].get('expect') or '(unspecified)'}\nOUTPUT:\n{to_judge[cid][:1200]}"
            for cid in batch)
        try:
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": _JUDGE_OPENING + f"\n\nGive one verdict for EACH of the {len(batch)} claims, in order.\n\n" + block}],
                tools=[RECORD_VERDICTS_TOOL], role="model_general", overrides=model_overrides,
                temperature=0.0, tool_choice="auto", max_tokens=3000)
            raw = ((read_tool_args(resp) or {}).get("verdicts")) or []
        except Exception as exc:
            logger.warning("state_check_judge_model_failed: %r", exc)
            raw = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            v = str(item.get("verdict") or "").strip()
            if cid not in to_judge or v not in ("confirmed", "contradicted", "unknown"):
                continue
            repair = str(item.get("repair") or "")[:300]
            if repair and destructive_repair(repair):  # §17.1052 — never proposed by the engine
                logger.info("state_check_repair_refused id=%s repair=%r", cid, repair[:80])
                verdicts[cid]["needs_decision"] = True
                repair = ""
            verdicts[cid].update({"verdict": v, "reason": str(item.get("reason") or "")[:300],
                                  "repair": repair,
                                  "caused_by": str(item.get("caused_by") or "").strip()})
    return list(verdicts.values())


_JUDGE_BATCH = 5


def render_verdicts(verdicts: list[dict]) -> str:
    n = {"confirmed": 0, "contradicted": 0, "unknown": 0}
    for v in verdicts:
        n[v["verdict"]] = n.get(v["verdict"], 0) + 1
    lines = [f"🩺 **State check result — {n['confirmed']} confirmed, {n['contradicted']} contradicted, {n['unknown']} unknown.**"]
    icon = {"confirmed": "✅", "contradicted": "❌", "unknown": "❔"}
    for v in sorted(verdicts, key=lambda x: ("contradicted", "unknown", "confirmed").index(x["verdict"])):
        lines.append(f"- {icon[v['verdict']]} `{v['id']}` {v['claim'][:90]}" + (f" — {v['reason'][:140]}" if v.get("reason") and v["verdict"] != "confirmed" else "")
                     + (" — *this step recorded a one-off action, so I am not reopening it; you decide*"
                        if v.get("needs_decision") == "reopen" else
                        " — *the check only showed absence in unrelated output, so I am not proposing a fix; look for yourself*"
                        if v.get("needs_decision") == "indirect" else
                        " — *the only repair would remove or stop something, so I am not proposing one; you decide*"
                        if v.get("needs_decision") else ""))
    bad = [v for v in verdicts if v["verdict"] == "contradicted"]
    if bad:
        lines.append("\nContradicted facts have been retracted from what I believe. The step results that no longer hold "
                     "are in the plan-change proposal below — confirm it and I will walk you through putting them right, "
                     "one step at a time, before we go on.")
    else:
        lines.append("\nEverything I could check still holds. Continuing from where we were.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration — stage, resolve, and hand structural changes to the confirm
# surface.
# ---------------------------------------------------------------------------

async def start_state_check(*, db, session_id: str, node_key: Optional[str], on_progress=None) -> dict:
    """Build claims → probes → stash ``pending_state_check`` → return the
    message for the operator. Raises ValueError on a bad session."""
    built = await build_claims(db=db, session_id=session_id)
    claims = built["claims"]
    all_probes, refused = await plan_probes(claims, built["environment"], on_progress=on_progress)
    # §17.1138 — a per-script budget: the operator gets a block they can run in
    # one go; the rest queue behind it and come out after each paste.
    from app.config import settings as _settings
    cap = max(1, int(getattr(_settings, "assist_state_check_max_probes", 24) or 24))
    probes, deferred = all_probes[:cap], all_probes[cap:]
    pending = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node_key": node_key or built.get("current_node_key"),
        "probes": probes,
        "deferred": deferred,
        "verdicts": [],           # accumulated across partial pastes
        "probes_total": len(all_probes),
        "claims_total": len(claims),
    }
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id, "patch": json.dumps({"pending_state_check": pending})})
    await db.commit()
    logger.warning("state_check_started session_id=%s node_key=%s claims=%d probes=%d deferred=%d refused=%d",
                   session_id, pending["node_key"], len(claims), len(probes), len(deferred), len(refused))
    targets = sum(1 for c in claims if c.get("kind") != "pin")
    # §17.1105 — offer the local runner iff it isn't configured (when it is,
    # assist_turn runs the probes itself and this paste message is never shown).
    _runner_off = True
    try:
        from app.modules import assist_local_runner as _lr
        _runner_off = (await _lr.runner_spec(db)) is None
    except Exception:
        _runner_off = True
    msg = render_probe_message(probes, checked=len(all_probes),
                               unchecked=max(0, targets - len(all_probes)),
                               offer_runner=_runner_off)
    if deferred:
        msg += (f"\n\n(This is the first {len(probes)} of {len(all_probes)} checks — "
                f"after you paste, I hand you the next {len(deferred)}.)")
    return {"message": msg, "probes": probes, "deferred": deferred, "claims_total": len(claims),
            "refused": refused, "node_key": pending["node_key"]}


async def get_pending_state_check(*, db, session_id: str) -> Optional[dict]:
    try:
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        p = (meta or {}).get("pending_state_check") if isinstance(meta, dict) else None
        return p if isinstance(p, dict) and p.get("probes") else None
    except Exception as exc:
        logger.warning("state_check_pending_read_failed sid=%s err=%r", session_id, exc)
        return None


async def clear_pending_state_check(*, db, session_id: str) -> None:
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = COALESCE(metadata, '{}'::jsonb) - 'pending_state_check', updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id})
    await db.commit()


def looks_like_probe_output(text_value: str) -> bool:
    return bool(MARKER_RE.search(text_value or ""))


async def resolve_state_check(*, db, session_id: str, pasted: str, finish: bool = False) -> dict:
    """Judge the pasted output, retract contradicted facts, stage the
    structural proposal, record the check on the session. Returns
    ``{message, verdicts, proposal, retracted}``."""
    pending = await get_pending_state_check(db=db, session_id=session_id)
    if not pending:
        return {"message": "", "verdicts": [], "proposal": None, "retracted": []}
    probes = pending.get("probes") or []
    prior: list[dict] = [v for v in (pending.get("verdicts") or []) if isinstance(v, dict)]
    deferred: list[dict] = [p for p in (pending.get("deferred") or []) if isinstance(p, dict)]
    total = int(pending.get("probes_total") or (len(prior) + len(probes) + len(deferred)))
    if finish:
        # §17.1138 — "skip the rest": everything not pasted stays unknown, by the operator's choice
        fresh = [{"id": p["id"], "verdict": "unknown", "reason": "skipped by the operator", "kind": p.get("kind"),
                  "claim": p.get("claim"), "node_key": p.get("node_key")} for p in probes + deferred]
        verdicts = prior + fresh
        sections_present = 0
    else:
        fresh = await judge_outputs(probes, pasted)
        present_ids = set(attribute_sections(pasted))
        sections_present = sum(1 for p in probes if p["id"] in present_ids)
        missing = [p for p in probes if p["id"] not in present_ids]
        answered = [v for v in fresh if v["id"] in present_ids]
        if sections_present and (missing or deferred):
            # §17.1138 — a PARTIAL paste keeps the check open: judge what came
            # back, queue the uncovered ids + the deferred probes as the next
            # script. Before this, 47 probes → 4 pasted → 43 "unknown" and done.
            remaining = missing + deferred
            nxt = {**pending, "probes": remaining, "deferred": [], "verdicts": prior + answered,
                   "partial_pastes": int(pending.get("partial_pastes") or 0) + 1}
            await db.execute(text("""
                UPDATE assist_sessions
                   SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb), updated_at = NOW()
                 WHERE id = :sid
            """), {"sid": session_id, "patch": json.dumps({"pending_state_check": nxt})})
            await db.commit()
            logger.warning("state_check_partial session_id=%s node_key=%s answered=%d of=%d missing=%d deferred=%d",
                           session_id, pending.get("node_key"), len(prior) + len(answered), total, len(missing), len(deferred))
            return {"message": render_continuation_message(prior + answered, remaining,
                                                           missing_ids=[p["id"] for p in missing],
                                                           deferred=len(deferred), total=total),
                    "verdicts": prior + answered, "proposal": None, "retracted": [], "pending": True}
        verdicts = prior + fresh
    contradicted = [v for v in verdicts if v["verdict"] == "contradicted"]
    retracted: list[str] = []
    if contradicted:
        facts = [v["claim"] for v in contradicted if v["kind"] == "fact"]
        if facts:
            try:
                from app.modules.assist_environment import set_environment
                await set_environment(session_id=session_id, db=db, retract_facts=facts,
                                      facts=[f"State check at {pending.get('node_key') or '?'}: NOT true any more — {v['claim'][:120]} ({v['reason'][:100]})"
                                             for v in contradicted if v["kind"] == "fact"])
                retracted = facts
            except Exception as exc:
                logger.warning("state_check_retract_failed sid=%s err=%r", session_id, exc)
    proposal = None
    affected = proposals_from_verdicts(verdicts, anchor_node_key=pending.get("node_key"))
    if affected:
        try:
            from app.modules.assist_notes import _stage_replan_proposal
            structural = [v for v in contradicted if v["kind"] == "step" or (v.get("repair") or "").strip()]
            note_text = (f"State check at {pending.get('node_key') or '?'}: "
                         + "; ".join(f"{v['id']} {v['reason'][:80]}" for v in structural)[:400])
            proposal = await _stage_replan_proposal(
                session_id=session_id, note_text=note_text, note_kind="state_check", affected=affected, db=db)
        except Exception as exc:
            logger.warning("state_check_stage_failed sid=%s err=%r", session_id, exc)
    # record the check on the session (for the automatic-offer guard) and clear the pending
    unknown_v = [v for v in verdicts if v["verdict"] == "unknown"]
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "node_key": pending.get("node_key"),
              "confirmed": sum(1 for v in verdicts if v["verdict"] == "confirmed"),
              "contradicted": len(contradicted),
              "unknown": len(unknown_v),
              # §17.1138 — WHY they stayed unknown (the 09-19 check recorded 43 and no reason)
              "unknown_no_output": sum(1 for v in unknown_v if "no output pasted" in (v.get("reason") or "")),
              "unknown_skipped": sum(1 for v in unknown_v if "skipped by the operator" in (v.get("reason") or "")),
              "unknown_judge": sum(1 for v in unknown_v if "no output pasted" not in (v.get("reason") or "")
                                   and "skipped by the operator" not in (v.get("reason") or "")),
              "probes_total": total, "partial_pastes": int(pending.get("partial_pastes") or 0),
              "sections_last_paste": sections_present}
    await db.execute(text("""
        -- §17.1180 (audit M11) — CAPPED at 30, the same bound and the same
        -- idiom `reopen_preimages` already uses. This array was appended to on
        -- every resolved state check and never pruned: one of three unbounded
        -- jsonb arrays, of which exactly one was bounded. Measured on this
        -- database before the cap: max 11 entries / 1.7 KB, so this is not yet
        -- harm — but §17.1168 was the same shape arriving as a real problem,
        -- and the row that grows without limit is the one nobody is watching.
        UPDATE assist_sessions
           SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'pending_state_check')
                          || jsonb_build_object('state_checks',
                               (SELECT COALESCE(jsonb_agg(e), '[]'::jsonb) FROM (
                                   SELECT e FROM jsonb_array_elements(
                                       COALESCE(metadata->'state_checks', '[]'::jsonb) || CAST(:r AS jsonb)) e
                                   ORDER BY (e->>'ts') DESC NULLS LAST LIMIT 30) t)),
               updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id, "r": json.dumps(record)})
    await db.commit()
    logger.warning("state_check_resolved session_id=%s node_key=%s confirmed=%d contradicted=%d unknown=%d "
                   "(no_output=%d skipped=%d judge=%d) probes_total=%d partial_pastes=%d retracted=%d proposal=%s",
                   session_id, record["node_key"], record["confirmed"], record["contradicted"], record["unknown"],
                   record["unknown_no_output"], record["unknown_skipped"], record["unknown_judge"],
                   record["probes_total"], record["partial_pastes"], len(retracted), bool(proposal))
    return {"message": render_verdicts(verdicts), "verdicts": verdicts, "proposal": proposal, "retracted": retracted}


def proposals_from_verdicts(verdicts: list[dict], *, anchor_node_key: Optional[str]) -> list[dict]:
    """The structural changes a state check proposes, for the confirm surface:
    a contradicted STEP result → ``repair`` (a step that puts it right, when
    the judge named one) or ``reopen`` (redo the step); a contradicted FACT
    with a named repair → ``repair`` anchored on the current step (the fact
    was distilled from the operator's own work, and the work is what has to
    be redone). A contradicted fact with no repair is only retracted."""
    out: list[dict] = []
    contradicted_ids = {v.get("id") for v in verdicts or [] if v.get("verdict") == "contradicted"}
    for v in verdicts or []:
        if v.get("verdict") != "contradicted":
            continue
        # a consequence of another contradicted claim is repaired by that claim's repair
        cb = (v.get("caused_by") or "").strip()
        if cb and cb != v.get("id") and cb in contradicted_ids:
            continue
        repair = (v.get("repair") or "").strip()
        if indirect_contradiction(v):
            # §17.1056 — no proposal from an argument-from-absence; the
            # operator is told to look themselves.
            v["needs_decision"] = "indirect"
            logger.info("state_check_indirect_contradiction id=%s reason=%r", v.get("id"), (v.get("reason") or "")[:80])
            continue
        if v.get("kind") == "step":
            if repair:
                out.append({"node_key": v.get("node_key"), "action": "repair",
                            "current_assumption": (v.get("claim") or "")[:300],
                            "proposed_change": repair[:400], "reason": (v.get("reason") or "")[:300]})
            elif reopen_refused(v):
                # §17.1054 — redoing a one-off / destructive step IS the
                # destructive repair §17.1052 refuses, in different clothes.
                # Live: reopening "Remove old containers and VMs", "Stop the
                # control-panel backend", "Stop caddy-proxy and clear its
                # Caddyfile" — the plan would have walked the operator through
                # undoing what later steps built on purpose.
                v["needs_decision"] = "reopen"
                logger.info("state_check_reopen_refused id=%s title=%r", v.get("id"), (v.get("title") or "")[:80])
            else:
                out.append({"node_key": v.get("node_key"), "action": "reopen",
                            "current_assumption": (v.get("claim") or "")[:300],
                            "proposed_change": f"Redo this step — {(v.get('reason') or '')[:200]}"})
        elif repair and anchor_node_key:
            out.append({"node_key": anchor_node_key, "action": "repair",
                        "current_assumption": (v.get("claim") or "")[:300],
                        "proposed_change": repair[:400], "reason": (v.get("reason") or "")[:300]})
    # §17.1052 — belt and braces: nothing destructive leaves this function.
    out = [p for p in out if not (p["action"] == "repair" and destructive_repair(p["proposed_change"]))]
    # one repair per cause: fold repairs whose distinctive terms mostly overlap
    uniq: list[dict] = []
    for p in out:
        terms = _repair_terms(p["proposed_change"])
        dup = False
        for q in uniq:
            if q["action"] == p["action"] == "repair":
                qt = _repair_terms(q["proposed_change"])
                # overlap coefficient (against the smaller set): two wordings of
                # one repair share most of the shorter one's identifiers
                if terms and qt and len(terms & qt) / min(len(terms), len(qt)) >= 0.5:
                    dup = True
                    break
            elif q["action"] == p["action"] and q.get("node_key") == p.get("node_key") and q["proposed_change"] == p["proposed_change"]:
                dup = True
                break
        if not dup:
            uniq.append(p)
    return uniq


_REPAIR_STOP = frozenset({"the", "and", "with", "from", "that", "this", "then", "into", "using", "make", "sure",
                          "again", "true", "host", "port", "container", "service", "create", "start", "restart",
                          "run", "running", "listening", "returns", "mapped", "bound", "image", "named", "volume"})


def _repair_terms(text_value: str) -> set[str]:
    """Identifier-ish tokens; hyphenated names also contribute their parts
    ("uptime-kuma" → uptime, kuma) so "Uptime Kuma" and "uptime-kuma" agree."""
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9][a-z0-9_.:/-]{2,}", (text_value or "").lower()):
        if t in _REPAIR_STOP:
            continue
        out.add(t)
        for part in re.split(r"[-/:]", t):
            if len(part) >= 4 and part not in _REPAIR_STOP and not part.isdigit():
                out.add(part)
    return {t for t in out if len(t) >= 4}


async def state_check_done_on_step(*, db, session_id: str, node_key: Optional[str]) -> bool:
    try:
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        checks = (meta or {}).get("state_checks") if isinstance(meta, dict) else None
        return any(isinstance(c, dict) and c.get("node_key") == node_key for c in (checks or []))
    except Exception:
        return False


def offer_text(streak: int) -> str:
    return (f"🩺 We've tried {streak} fixes on this step without closing it. That usually means something the plan "
            "assumes is done has changed on the machine. Press **🩺 Verify state** (or reply `verify state`) and "
            "I'll check what is actually running against what the plan believes, then walk you through the "
            "repairs one step at a time.")
