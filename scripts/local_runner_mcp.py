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

Every executed command is echoed to this process's log and recorded by the
engine in the session transcript.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shlex
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("local-runner")

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
    "virsh": {"start", "destroy", "shutdown", "reboot", "define", "undefine", "create"},
    "ufw": {"allow", "deny", "delete", "enable", "disable", "reset"}, "firewall-cmd": None,
    "ip": {"add", "del", "set", "change", "replace", "flush"}, "wg": {"set"}, "nmcli": None,
    "zfs": {"create", "destroy", "set", "snapshot", "rollback", "send", "receive"}, "zpool": {"create", "destroy", "add", "remove"},
    "git": {"clone", "pull", "checkout", "reset", "push"}, "sed": {"-i"}, "curl": {"-X", "--request", "-d", "--data", "-o", "-O", "-T", "--upload-file"},
    "bash": {"-c"}, "sh": {"-c"}, "zsh": {"-c"}, "python": {"-c"}, "python3": {"-c"}, "perl": {"-e", "-i"},
}
_INTERPRETERS = {"sh", "bash", "zsh", "dash", "python", "python3", "perl", "ruby", "node"}


def read_only(cmd: str) -> tuple[bool, str]:
    if not cmd.strip() or "$(" in cmd or "`" in cmd or "<<" in cmd:
        return False, "substitution/heredoc"
    if re.search(r"(?<![<>])>(?!\s*/dev/null|&\d)", cmd) or re.search(r">>", cmd):
        return False, "redirect"
    for segment in re.split(r"\|\||&&|;|\|", cmd):
        try:
            argv = shlex.split(segment.strip())
        except ValueError:
            return False, "unparsable"
        if not argv:
            continue
        head = argv[0].rsplit("/", 1)[-1]
        if head == "sudo" and len(argv) > 1:
            argv = argv[1:]; head = argv[0].rsplit("/", 1)[-1]
        if head in _INTERPRETERS and segment is not None and cmd.find(segment) > 0 and "|" in cmd[:cmd.find(segment)]:
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


def build_server(token: str | None):
    from mcp.server import MCPServer
    mcp = MCPServer("scaffold-local-runner")

    @mcp.tool()
    async def run_readonly(command: str, timeout_s: int = 20) -> str:
        """Run ONE read-only shell command on this machine and return its output. Refuses anything that writes."""
        ok, why = read_only(command)
        if not ok:
            log.warning("REFUSED (%s): %s", why, command)
            return f"(refused by the local runner: {why})"
        log.info("RUN: %s", command)
        proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return f"(timed out after {timeout_s}s)"
        return out.decode("utf-8", errors="replace")[:20000]

    return mcp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--token", default=None, help="shared secret the engine sends as X-Runner-Token")
    ap.add_argument("--stdio", action="store_true", help="speak MCP over stdio instead of HTTP")
    args = ap.parse_args()
    mcp = build_server(args.token)
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
