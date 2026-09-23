#!/usr/bin/env python3
"""§17.1077 — the local runner: a read-only MCP server for YOUR machine.

Run this on the box the plan is about (a Proxmox host, a VM). It exposes one
tool, ``run_readonly``, and refuses anything that is not a read-only shell
shape: mutation verbs at any command head, redirects (except /dev/null),
pipes into an interpreter, or anything it cannot parse. The engine applies
the same gate before sending. Nothing here is a default: you register it in
the engine (`POST /mcp/servers`), set `ASSIST_LOCAL_RUNNER_SERVER`, and the
state check stops asking you to paste.

    pip install "mcp>=2.0"        # the only dependency
    python3 local_runner_mcp.py --host 0.0.0.0 --port 8790 --token <secret>

One-paste install (§17.1147, as root on the target): copies this file to
/opt/scaffold-runner, makes a venv there (installing python3-venv with apt if
the box lacks it), installs the dependencies, writes and starts the systemd
unit ``local-runner-mcp`` and checks that the port answers — then prints one
line starting with ``OK:`` or ``FAILED:``. Re-running it re-installs cleanly
(a new --token replaces the old one):

    python3 local_runner_mcp.py --install --port 8790 --token <secret>

Every executed command is echoed to this process's log and recorded by the
engine in the session transcript.

sudo (§17.1078): the runner is unprivileged. A probe that starts with `sudo`
is rewritten in one of two ways — if its command matches a prefix in
``--sudo-allow`` (e.g. ``--sudo-allow "nginx -t" "pct config"``) it runs as
``sudo -n …`` (non-interactive: it fails fast instead of waiting for a
password); otherwise the ``sudo`` is dropped and the command runs as this
user, with a note in the output so the judge knows what it is reading.
For the allowed prefixes to work without a password, add a sudoers line
for the user running this script — one per command, no wildcards:

    runner ALL=(root) NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/pct config *

The allow-list is checked against the SAME read-only gate as everything
else; sudo never widens what may run, only who runs it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("local-runner")

# §17.1151 — bump when the read-only gate changes. The engine compares this
# with the copy it ships (the tool description carries it) and, when the
# helper on the target is older, walks the operator through a one-paste
# refresh instead of feeding itself refusals it cannot act on.
HELPER_VERSION = "6"

# The same verb table as the engine's assist_state_check._MUTATION_RE, applied
# to the head of every simple command.
_MUTATION = re.compile(
    r"^(?:rm|rmdir|mv|cp|dd|mkfs\w*|fdisk|parted|truncate|chmod|chown|chattr|ln|tee|touch|mkdir|kill|pkill|killall|"
    r"reboot|shutdown|poweroff|halt|init|useradd|userdel|passwd|apt(?:-get)?|dpkg|yum|dnf|pacman|zypper|snap|pip3?|npm|"
    r"yarn|cargo|make|wget|visudo|crontab|iptables|nft|wg-quick)$")
_SUB_MUT = {
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "daemon-reload", "edit", "set-property"},
    "service": None, "pct": {"start", "stop", "shutdown", "reboot", "create", "destroy", "set", "resize", "migrate", "clone", "template", "snapshot", "rollback", "delsnapshot", "unlock", "restore", "move", "move_volume"},
    "qm": {"start", "stop", "shutdown", "reboot", "create", "destroy", "set", "resize", "migrate", "clone", "template", "snapshot", "rollback", "delsnapshot", "unlock", "restore", "move_disk", "importdisk"},
    "docker": {"run", "rm", "rmi", "stop", "start", "restart", "kill", "create", "pull", "push", "build", "compose"},
    # §17.1156 — the Proxmox management family beyond pct/qm
    "pvesh": {"create", "set", "delete"}, "pvesm": {"add", "remove", "set", "alloc", "free", "import", "export", "prune-backups"},
    "pveum": {"add", "modify", "delete", "passwd", "useradd", "usermod", "userdel", "groupadd", "groupmod", "groupdel", "roleadd", "rolemod", "roledel", "aclmod", "acldel"},
    "pvecm": {"add", "addnode", "delnode", "create", "expected", "updatecerts"}, "pvenode": {"set", "delete", "startall", "stopall", "migrateall", "wakeonlan"},
    "virsh": {"start", "destroy", "shutdown", "reboot", "define", "undefine", "create"},
    "ufw": {"allow", "deny", "delete", "enable", "disable", "reset"}, "firewall-cmd": None,
    "ip": {"add", "del", "set", "change", "replace", "flush"}, "wg": {"set"}, "nmcli": None,
    "zfs": {"create", "destroy", "set", "snapshot", "rollback", "send", "receive"}, "zpool": {"create", "destroy", "add", "remove"},
    "git": {"clone", "pull", "checkout", "reset", "push"}, "sed": {"-i"}, "curl": {"-X", "--request", "-d", "--data", "-o", "-O", "-T", "--upload-file"},
    "bash": {"-c"}, "sh": {"-c"}, "zsh": {"-c"}, "python": {"-c"}, "python3": {"-c"}, "perl": {"-e", "-i"},
}
_INTERPRETERS = {"sh", "bash", "zsh", "dash", "python", "python3", "perl", "ruby", "node"}

# §17.1150 — READ forms of tools whose HEAD is a mutation verb: `dpkg -l`,
# `iptables -S`, `pip list`, `crontab -l`… The head table stays conservative
# (a bare `iptables` is a write); these exact query shapes are the exception,
# judged on the argv, with an explicit deny list where a read flag can share
# a line with a write flag. THE SAME TABLE lives in app/modules/assist_state_check.py
# (the engine gates before sending); tests/test_local_runner.py asserts parity.
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


def split_segments(cmd: str) -> list[str]:
    """§17.1151 — split on `||`, `&&`, `;`, `|` OUTSIDE quotes. The naive split
    broke `grep -E 'net0|hostpci'` into an unbalanced quote → "unparsable"
    (live, three refusals in a row on the operator's step)."""
    out: list[str] = []
    buf: list[str] = []
    q: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 2; continue
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch; buf.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            buf.append(ch); buf.append(cmd[i + 1]); i += 2; continue
        elif cmd.startswith(("||", "&&"), i):
            out.append("".join(buf)); buf = []; i += 2; continue
        elif ch in (";", "|"):
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [o for o in out if o.strip()]


_SSH_FLAGS_WITH_ARG = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-W", "-b", "-c", "-e", "-I", "-m", "-O", "-Q", "-S", "-w", "-E", "-B"}


# §17.1166 — privilege/environment WRAPPERS are judged on what they RUN.
# The old peel dropped a bare leading `sudo` only: `sudo -n apt install x`
# left `-n` as the head, which matches no mutation verb, so it passed. Mirrors
# `privilege_wrapper_remainder` in app/modules/assist_state_check.py — the two
# gates must agree or the engine sends what the helper then refuses.
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


def privilege_wrapper_remainder(argv: list) -> str | None:
    """The command a wrapper runs. None = not a wrapper; "" = no command
    (an interactive shell — refused)."""
    head = argv[0].rsplit("/", 1)[-1] if argv else ""
    if head not in _PRIV_WRAPPERS:
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
        if head == "env" and "=" in a[1:]:
            i += 1
            continue
        break
    if head == "timeout" and i < len(argv):
        i += 1
    return " ".join(argv[i:]).strip()


def ssh_remote_read_only(argv: list, judge) -> tuple[bool, str]:
    """``ssh [opts] [user@]host <remote command>``: the remote command must pass
    the same gate; no remote command (an interactive login) is refused."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _SSH_FLAGS_WITH_ARG else 1
    if i >= len(argv):
        return False, "ssh without a host"
    remote = " ".join(argv[i + 1:]).strip()
    if not remote:
        return False, "interactive ssh"
    res = judge(remote)
    ok = res[0] if isinstance(res, tuple) else bool(res)
    return (True, "") if ok else (False, "ssh remote command writes")


def mask_quoted(cmd: str) -> str:
    """The command with the INSIDE of quoted strings blanked — so a `>` in a
    quoted regex (`grep -o "<Name>[^<]*</Name>"`, live: refused as 'redirect')
    or a `|` in a quoted pattern is not read as shell syntax."""
    out: list[str] = []
    q: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                out.append("  "); i += 2; continue
            if ch == q:
                q = None; out.append(ch)
            else:
                out.append(" ")
        elif ch in ("'", '"'):
            q = ch; out.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            out.append("  "); i += 2; continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_CONTAINER_EXEC = {"pct": ("exec",), "lxc-attach": (), "qm": ("guest", "exec")}


def container_exec_remainder(argv: list) -> str | None:
    """``pct exec <ct> [--] <cmd…>`` / ``qm guest exec <vm> [--] <cmd…>`` /
    ``lxc-attach -n <ct> -- <cmd…>``: the command that runs INSIDE the guest,
    or None when argv is not such a shape. §17.1152 — the wrapper is a read
    on the host; what matters is what runs inside."""
    head = argv[0].rsplit("/", 1)[-1] if argv else ""
    if head not in _CONTAINER_EXEC:
        return None
    subs = _CONTAINER_EXEC[head]
    if tuple(argv[1:1 + len(subs)]) != subs:
        return None
    rest = argv[1 + len(subs):]
    if "--" in rest:
        return " ".join(rest[rest.index("--") + 1:]).strip() or None
    # no `--`: skip the id / -n <name> and any flags, the rest is the command
    i = 0
    while i < len(rest) and (rest[i].startswith("-") or rest[i].isdigit()):
        i += 2 if rest[i] in ("-n", "--timeout") else 1
    return " ".join(rest[i:]).strip() or None


def extract_substitutions(cmd: str) -> tuple[str, list[str]]:
    """§17.1155 — ``$(…)`` and backtick substitutions, pulled out: returns the
    outer command with each replaced by ``__SUBn__`` and the inner commands.
    Nested ``$(…)`` is balanced; an unbalanced one leaves the text as is (the
    caller then refuses it as unparsable)."""
    inners: list[str] = []
    out: list[str] = []
    i, n = 0, len(cmd)
    while i < n:
        if cmd.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if cmd.startswith("$(", j):
                    depth += 1; j += 2; continue
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            if depth:
                out.append(cmd[i:]); break
            inners.append(cmd[i + 2:j - 1])
            out.append(f"__SUB{len(inners) - 1}__"); i = j; continue
        if cmd[i] == "`":
            j = cmd.find("`", i + 1)
            if j < 0:
                out.append(cmd[i:]); break
            inners.append(cmd[i + 1:j])
            out.append(f"__SUB{len(inners) - 1}__"); i = j + 1; continue
        out.append(cmd[i]); i += 1
    return "".join(out), inners


def read_only(cmd: str, _depth: int = 0) -> tuple[bool, str]:
    if not cmd.strip() or "<<" in cmd:
        return False, "substitution/heredoc" if "<<" in cmd else "empty"
    if _depth > 3:
        return False, "substitution too deep"
    # §17.1155 — a substitution is judged by what it RUNS, like the engine's
    # AST gate: `pvesh get /nodes/$(hostname)/…` reads; `echo $(rm -rf /x)` does not.
    cmd, inners = extract_substitutions(cmd)
    if "$(" in cmd or "`" in cmd:
        return False, "unparsable"
    for inner in inners:
        ok, why = read_only(inner, _depth + 1)
        if not ok:
            return False, f"in $(…): {why}"
    masked = mask_quoted(cmd)
    if re.search(r"(?<![<>])>(?!\s*/dev/null|&\d)", masked) or re.search(r">>", masked):
        return False, "redirect"
    for segment in split_segments(cmd):
        try:
            argv = shlex.split(segment.strip())
        except ValueError:
            return False, "unparsable"
        if not argv:
            continue
        head = argv[0].rsplit("/", 1)[-1]
        wrapped = privilege_wrapper_remainder(argv)   # §17.1166
        if wrapped is not None:
            if not wrapped:
                return False, f"{head} without a command"
            ok, why = read_only(wrapped, _depth + 1)
            if not ok:
                return False, why
            continue
        if head == "ssh":         # §17.1151 — a remote command is judged like a local one; interactive ssh is refused
            ok, why = ssh_remote_read_only(argv, read_only)
            if not ok:
                return False, why
            continue
        inner = container_exec_remainder(argv)      # §17.1152 — pct exec / qm guest exec / lxc-attach
        if inner is not None:
            ok, why = read_only(inner)
            if not ok:
                return False, f"inside the guest: {why}"
            continue
        if head in _INTERPRETERS and len(argv) >= 3 and argv[1] == "-c":   # §17.1152 — `sh -c '<script>'`: judge the script
            ok, why = read_only(argv[2])
            if not ok:
                return False, f"in the -c script: {why}"
            continue
        if read_form(argv):       # §17.1150 — the same READ shapes the engine allows
            continue
        if head in _INTERPRETERS and segment is not None and cmd.find(segment) > 0 and "|" in masked[:cmd.find(segment)]:
            return False, "pipe to interpreter"
        if _MUTATION.match(head):
            return False, f"mutation verb {head}"
        rule = _SUB_MUT.get(head)
        if rule is None and head in _SUB_MUT:
            return False, f"{head} is not read-only here"
        if head in ("curl", "wget"):
            # a download to DISK or a method override writes; `-o /dev/null` is the read-only shape
            outs = [argv[i + 1] for i, a in enumerate(argv) if a in ("-o", "-O", "--output") and i + 1 < len(argv)]
            if any(o != "/dev/null" for o in outs) or any(a in ("-X", "--request", "-d", "--data", "--data-raw", "-T", "--upload-file") for a in argv[1:]):
                return False, f"{head} writes"
            continue
        if rule and any(a in rule for a in argv[1:4]):
            return False, f"{head} subcommand/flag"
    return True, ""


_SUDO_RE = re.compile(r"^\s*sudo\s+(?:-[A-Za-z]+\s+)*")


def apply_sudo_policy(cmd: str, allow: list[str]) -> tuple[str, str]:
    """``(command to run, note)``. A leading ``sudo`` becomes ``sudo -n`` when
    the rest matches an allowed prefix (whole-token match), and is dropped
    otherwise. Only the FIRST segment is considered; a ``sudo`` later in a
    pipeline is left alone and will fail non-interactively like any other."""
    m = _SUDO_RE.match(cmd)
    if not m:
        return cmd, ""
    rest = cmd[m.end():].strip()
    for prefix in allow:
        p = prefix.strip()
        if p and (rest == p or rest.startswith(p + " ")):
            return f"sudo -n {rest}", ""
    return rest, "(ran WITHOUT sudo — not in the runner's --sudo-allow list; output may be partial)\n"


def build_server(token: str | None, sudo_allow: list[str] | None = None):
    from mcp.server import MCPServer
    mcp = MCPServer("scaffold-local-runner")
    allow = list(sudo_allow or [])

    @mcp.tool(description=f"Run ONE read-only shell command on this machine and return its output. "
                          f"Refuses anything that writes. (helper v{HELPER_VERSION})")
    async def run_readonly(command: str, timeout_s: int = 20) -> str:
        ok, why = read_only(command)
        if not ok:
            log.warning("REFUSED (%s): %s", why, command)
            return f"(refused by the local runner: {why})"
        command, note = apply_sudo_policy(command, allow)
        log.info("RUN: %s", command)
        proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return f"(timed out after {timeout_s}s)"
        return note + out.decode("utf-8", errors="replace")[:20000]

    return mcp


# ---------------------------------------------------------------------------
# §17.1147 — one-paste install. The recipe used to hand the operator three
# commands plus a systemd unit to type by hand; the box then had to have
# python3-venv already. Everything below is what a careful admin would do,
# done by the script, with ONE final line that says whether it worked.
# ---------------------------------------------------------------------------

INSTALL_DIR = "/opt/scaffold-runner"
UNIT_NAME = "local-runner-mcp"
DEPS = ('"mcp>=2.0"', "uvicorn", "starlette")


def unit_text(*, python: str, script: str, host: str, port: int, token: str | None,
              sudo_allow: list[str] | None = None) -> str:
    """The systemd unit, as text. Pure: tests read it without a root shell."""
    cmd = [python, script, "--host", host, "--port", str(port)]
    if token:
        cmd += ["--token", token]
    if sudo_allow:
        cmd += ["--sudo-allow", *sudo_allow]
    exec_start = " ".join(shlex.quote(c) for c in cmd)
    return (
        "[Unit]\n"
        "Description=scaffold-engine local runner (read-only MCP helper)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def ensure_venv(venv: str, *, run=_run, apt: bool = True) -> tuple[bool, str]:
    """Create ``venv`` if missing. Debian/Proxmox ships python3 without
    ``ensurepip``; when ``python3 -m venv`` fails for that reason and apt is
    available, install python3-venv and try once more."""
    py = os.path.join(venv, "bin", "python")
    if os.path.exists(py):
        return True, "venv already present"
    r = run([sys.executable, "-m", "venv", venv])
    if r.returncode == 0:
        return True, "venv created"
    out = r.stdout or ""
    if apt and shutil.which("apt-get") and ("ensurepip" in out or "venv" in out.lower()):
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        r2 = run(["apt-get", "install", "-y", "-q", f"python{ver}-venv"])
        if r2.returncode != 0:
            r2 = run(["apt-get", "install", "-y", "-q", "python3-venv"])
        if r2.returncode != 0:
            return False, f"python3 -m venv failed ({out.strip()[-200:]}) and apt-get install python3-venv failed too:\n{(r2.stdout or '')[-400:]}"
        shutil.rmtree(venv, ignore_errors=True)
        r = run([sys.executable, "-m", "venv", venv])
        if r.returncode == 0:
            return True, "venv created after installing python3-venv"
    return False, f"python3 -m venv failed:\n{out.strip()[-400:]}"


def port_answers(host: str, port: int, token: str | None, *, timeout: float = 2.0) -> tuple[bool, str]:
    """Does the runner answer on ``port``? ANY HTTP status is an answer (the
    MCP endpoint rejects a plain GET); a connection refusal is not. 401 means
    the running service has a DIFFERENT token."""
    import urllib.error
    import urllib.request
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/mcp/"
    req = urllib.request.Request(url, headers={"X-Runner-Token": token or "", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "HTTP 401 — the service that is running uses a different token"
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # connection refused / timeout
        return False, str(exc)


def install(args, *, run=_run) -> int:
    """Everything the install step used to ask the operator to type. Prints
    progress lines and ONE verdict line (``OK: …`` / ``FAILED: …``)."""
    if os.geteuid() != 0:
        print("FAILED: run the install as root (sudo python3 local_runner_mcp.py --install …).")
        return 2
    if not args.token:
        print("FAILED: --install needs --token <secret> (the engine generated one for this runner).")
        return 2
    dest_dir = args.install_dir
    os.makedirs(dest_dir, exist_ok=True)
    script = os.path.join(dest_dir, "local_runner_mcp.py")
    src = os.path.abspath(__file__)
    if os.path.abspath(script) != src:
        shutil.copyfile(src, script)
    print(f"[1/4] helper copied to {script}")
    venv = os.path.join(dest_dir, "venv")
    ok, why = ensure_venv(venv, run=run)
    if not ok:
        print(f"FAILED: {why}")
        return 1
    print(f"[2/4] {why}; installing dependencies (10–60 s)…")
    r = run([os.path.join(venv, "bin", "pip"), "install", "-q", "--disable-pip-version-check", *[d.strip('"') for d in DEPS]])
    if r.returncode != 0:
        print(f"FAILED: pip install did not finish:\n{(r.stdout or '')[-600:]}")
        return 1
    unit = unit_text(python=os.path.join(venv, "bin", "python"), script=script, host=args.host, port=args.port,
                     token=args.token, sudo_allow=args.sudo_allow)
    detached = not shutil.which("systemctl")
    if detached:
        # no systemd (a container, a BSD): start it detached and say so plainly.
        # A previous detached helper still holds the port (and the OLD token):
        # stop it first, by the pid this installer recorded.
        pidfile = os.path.join(dest_dir, "runner.pid")
        try:
            old_pid = int(open(pidfile).read().strip())
            os.kill(old_pid, 15)
            time.sleep(1)
        except (OSError, ValueError):
            pass
        cmd = shlex.split(unit.split("ExecStart=", 1)[1].splitlines()[0])
        proc = subprocess.Popen(cmd, stdout=open(os.path.join(dest_dir, "runner.log"), "ab"), stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        with open(pidfile, "w") as fh:
            fh.write(str(proc.pid))
        print(f"[3/4] no systemd here — started the helper detached (pid {proc.pid}, log: {dest_dir}/runner.log); it will NOT survive a reboot")
    else:
        unit_path = f"/etc/systemd/system/{UNIT_NAME}.service"
        with open(unit_path, "w") as fh:
            fh.write(unit)
        for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", UNIT_NAME], ["systemctl", "restart", UNIT_NAME]):
            r = run(cmd)
            if r.returncode != 0:
                print(f"FAILED: {' '.join(cmd)}:\n{(r.stdout or '')[-600:]}")
                return 1
        print(f"[3/4] service {UNIT_NAME} written to {unit_path}, enabled and started")
    deadline = time.time() + 20
    ok, why = False, ""
    while time.time() < deadline:
        ok, why = port_answers(args.host, args.port, args.token)
        if ok or "401" in why:
            break
        time.sleep(1)
    if not ok:
        print(f"FAILED: nothing answered on port {args.port} ({why}).")
        if shutil.which("journalctl"):
            print(run(["journalctl", "-u", UNIT_NAME, "-n", "20", "--no-pager"]).stdout or "")
        return 1
    print(f"[4/4] port {args.port} answers ({why})")
    how = f"detached process, log {dest_dir}/runner.log" if detached else f"service {UNIT_NAME}"
    print(f"OK: local runner active on {args.host}:{args.port}/mcp/ ({how}); the engine can now run its read-only checks here.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="bind address (default 127.0.0.1; --install defaults to 0.0.0.0 so the engine host can reach it)")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--token", default=None, help="shared secret the engine sends as X-Runner-Token")
    ap.add_argument("--stdio", action="store_true", help="speak MCP over stdio instead of HTTP")
    ap.add_argument("--sudo-allow", nargs="*", default=[], metavar="PREFIX",
                    help="command prefixes that may run as `sudo -n` (needs matching NOPASSWD sudoers lines); anything else drops its sudo")
    ap.add_argument("--install", action="store_true",
                    help="§17.1147 — as root: copy to --install-dir, make a venv, install deps, write+start the systemd unit, check the port")
    ap.add_argument("--install-dir", default=INSTALL_DIR)
    args = ap.parse_args()
    if args.host is None:
        args.host = "0.0.0.0" if args.install else "127.0.0.1"
    if args.install:
        return install(args)
    mcp = build_server(args.token, sudo_allow=args.sudo_allow)
    if args.stdio:
        asyncio.run(mcp.run_stdio_async()); return 0
    import contextlib
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount
    # The engine reaches this over a bridge/LAN address, so the SDK's
    # DNS-rebinding Host check (localhost only) must be off; the shared token
    # is the access control.
    from mcp.server.transport_security import TransportSecuritySettings
    sub = mcp.streamable_http_app(
        streamable_http_path="/", host=args.host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # a mounted streamable app's session manager is NOT started by Starlette;
        # run it from the parent lifespan (§17.772 gotcha)
        async with mcp.session_manager.run():
            yield
    if args.token:
        async def guard(scope, receive, send):
            if scope["type"] == "http":
                hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
                if hdrs.get("x-runner-token") != args.token:
                    await JSONResponse({"error": "bad token"}, status_code=401)(scope, receive, send); return
            await sub(scope, receive, send)
        app = Starlette(routes=[Mount("/mcp", app=guard)], lifespan=lifespan)
    else:
        app = Starlette(routes=[Mount("/mcp", app=sub)], lifespan=lifespan)
    log.info("local runner listening on %s:%s/mcp/ (read-only)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
