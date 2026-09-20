"""§17.1081 — the engine's own optional capabilities, as recipes it walks
the operator through.

Every optional capability (local runner, queue worker, reranker sidecar,
strict step rulebook, sudo on the runner) shipped OFF with a written reason
and a hand-written page telling the operator how to turn it on. That page
was the engine's job done by someone else: assist mode IS the walkthrough
product — plan → per-step guidance → paste verification → state check — and
a page beside it rots, bypasses the verification, and hides every gap the
engine would have exposed on its own setup.

So the recipes live HERE, as briefs the engine turns into a job through the
same door as any operator idea (`create_ideation_job` → Phase 1 refine →
approve with Assist → walkthrough). Each recipe also knows how to DETECT
whether it is already on, so the console shows the truth, not a checklist.
Developer-side chores (dependency PRs, CVE re-scans, CI) are deliberately
not here — they are the maintainer's, not the engine's.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text

from app.modules.job_state import TERMINAL_JOB_STATUSES

logger = logging.getLogger("scaffold")

# Statuses the console renders. "on"/"off" come from detection; "blocked"
# means a prerequisite recipe is off; "in_progress" means a job for it is
# still open; "manual" means the engine cannot observe it (it lives on the
# operator's other machine) and only the walkthrough can confirm it.
STATUSES = ("on", "off", "blocked", "in_progress", "manual")

# (name is historical — these are the statuses a job is NOT open in)
_OPEN_JOB_STATUSES = tuple(sorted(TERMINAL_JOB_STATUSES))  # §17.1107 — one vocabulary


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    summary: str            # one plain sentence: what you get
    why_off: str            # why it ships off, in plain words
    effort: str             # "15 min"
    brief: str              # the idea the engine refines into a plan
    requires: tuple[str, ...] = ()
    detect: Optional[Callable[..., Awaitable[tuple[str, str]]]] = field(default=None, compare=False)
    # §17.1144 — phrases an operator uses when they ask the ASSIST about this capability
    # (lower-case, matched as substrings of the message). Specific on purpose: a bare
    # 'runner' is a CI runner to the world; 'local runner' is ours.
    keywords: tuple[str, ...] = ()
    # §17.1145 — the recipe as PLAN STEPS (title, what to do), inserted into the
    # operator's own plan when they ask the assist for it. No separate job.
    steps: tuple[tuple[str, str], ...] = ()
    # §17.1147 — the engine's own check for the step carrying PROBE_MARK:
    # ``async (db) -> (ok, detail)``. Time-bounded; never touches the target
    # beyond the registered endpoint.
    probe: Optional[Callable[..., Awaitable[tuple[bool, str]]]] = field(default=None, compare=False)


# ---------------------------------------------------------------------------
# Detection — (status, detail). Each reads the live settings + DB; none of
# them call out to the operator's other machines.
# ---------------------------------------------------------------------------

async def _detect_local_runner(db) -> tuple[str, str]:
    from app.config import settings
    from app.modules import assist_local_runner as _lr
    name = (settings.assist_local_runner_server or "").strip()
    try:
        spec = await _lr.runner_spec(db)
    except Exception as exc:
        return "off", f"registry lookup failed: {exc}"
    if spec is None:
        if name:
            return "off", f"ASSIST_LOCAL_RUNNER_SERVER={name} but no enabled MCP server of that name is registered."
        return "off", "no runner is registered — the state check asks you to paste."
    if (spec.description or "").startswith(LOCAL_RUNNER_MARK):
        return "on", f"registered by the assist as '{spec.name}' at {spec.endpoint}; a Verify state proves it answers."
    return "on", f"probes run through '{spec.name}' at {spec.endpoint or spec.command}."


PROBE_TIMEOUT_S = 8.0
TCP_TIMEOUT_S = 4.0
# ports a Proxmox host / any Linux box normally answers on — used to tell
# "the host is down" from "this one port is filtered"
_SIBLING_PORTS = (22, 8006)


async def _tcp(host: str, port: int) -> str:
    """'open' | 'refused' | 'timeout' | 'error: …' — one bounded connect."""
    import asyncio
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=TCP_TIMEOUT_S)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return "open"
    except asyncio.TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as exc:
        return f"error: {exc.strerror or exc}"[:80]


async def diagnose_runner_path(spec) -> dict:
    """§17.1148 — what stands between the engine and the runner, as facts the
    engine can establish on its own: ``{"class", "detail", "checks"}``.
    class ∈ ok | token | port_closed | port_filtered | host_down | not_runner |
    unknown. Bounded: three TCP connects (4 s each) + one tools listing (8 s).
    Live: the operator's install printed OK on pve, the engine's probe said
    "did not answer" and stopped there; the host answered on 22 and 8006 while
    8790 timed out — a filtered port, i.e. the Proxmox host firewall."""
    import asyncio
    from urllib.parse import urlparse
    u = urlparse(spec.endpoint or "")
    host, port = u.hostname or "", u.port or RUNNER_PORT
    checks: dict[str, Any] = {"host": host, "port": port}
    if not host:
        return {"class": "unknown", "detail": f"the registered endpoint {spec.endpoint!r} has no host", "checks": checks}
    checks["runner_port"] = await _tcp(host, port)
    if checks["runner_port"] != "open":
        sib = await asyncio.gather(*[_tcp(host, p) for p in _SIBLING_PORTS])
        checks["siblings"] = dict(zip(_SIBLING_PORTS, sib, strict=True))
        alive = [p for p, r in checks["siblings"].items() if r in ("open", "refused")]
        if checks["runner_port"] == "refused":
            return {"class": "port_closed", "checks": checks,
                    "detail": f"{host} refused the connection on port {port} — nothing is listening there (the service is not running)."}
        if alive:
            return {"class": "port_filtered", "checks": checks,
                    "detail": (f"{host} is reachable from the engine host (it answers on port {', '.join(str(p) for p in alive)}) "
                               f"but port {port} times out — the port is being dropped by a firewall on that host.")}
        return {"class": "host_down", "checks": checks,
                "detail": f"nothing on {host} answers from the engine host (ports {port}, {', '.join(str(p) for p in _SIBLING_PORTS)} all time out) — wrong address, or the host is down/unreachable."}
    try:
        from app.modules import mcp_client
        tools = await asyncio.wait_for(mcp_client.list_tools(spec, use_cache=False), timeout=PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"class": "unknown", "checks": checks,
                "detail": f"port {port} on {host} accepts connections but the MCP handshake did not finish within {PROBE_TIMEOUT_S:.0f} s."}
    except Exception as exc:
        raw = str(exc)
        if "401" in raw or "unauthorized" in raw.lower():
            return {"class": "token", "checks": checks,
                    "detail": f"{host}:{port} answered but rejected the token — the helper that is running was started with a different --token."}
        return {"class": "unknown", "checks": checks, "detail": _probe_failure_words(f"http://{host}:{port}/mcp/", exc)}
    names = [t.get("name") for t in tools]
    checks["tools"] = names
    if "run_readonly" in names:
        return {"class": "ok", "checks": checks,
                "detail": f"reached http://{host}:{port}/mcp/ as '{spec.name}' and found its run_readonly tool"}
    return {"class": "not_runner", "checks": checks,
            "detail": f"reached {host}:{port} but it lists {names or 'no tools'} — that is not the local runner helper."}


def runner_repair_block(diag: dict, *, engine_ip: Optional[str] = None) -> str:
    """The troubleshooting the operator does on the TARGET, per failure class:
    what the engine established, ONE fenced block to paste, and what to do
    with the result. Deterministic; no model."""
    cls = diag.get("class") or "unknown"
    ch = diag.get("checks") or {}
    host, port = ch.get("host") or "<the target's IP>", ch.get("port") or RUNNER_PORT
    if cls == "ok":
        return ""
    if cls == "port_filtered":
        # the engine's own address when known, else the target's /24 (the token still guards the port)
        src = engine_ip or (".".join(str(host).split(".")[:3]) + ".0/24" if str(host).count(".") == 3 else "0.0.0.0/0")
        cmd = ("pve-firewall status; ss -tlnp | grep {port}\n"
               "pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport {port} "
               "--source {src} --enable 1 --comment 'scaffold local runner'\n"
               "sleep 3; pve-firewall status").format(port=port, src=src)
        return (f"**What I checked from the engine host:** {diag['detail']}\n\n"
                f"On Proxmox that is the host firewall. This opens port {port} to {src} only (the helper still requires "
                f"the token), then shows the firewall state:\n\n```bash\n{cmd}\n```\n\n"
                f"If `pve-firewall status` says **disabled**, the drop is elsewhere — paste the output of "
                f"`iptables -S INPUT | head -20; nft list ruleset 2>/dev/null | grep -n {port}` instead.\n\n"
                f"Paste what it printed here and I re-check the connection right away.")
    if cls == "port_closed":
        cmd = f"systemctl status local-runner-mcp --no-pager; journalctl -u local-runner-mcp -n 20 --no-pager; ss -tlnp | grep {port}"
        return (f"**What I checked from the engine host:** {diag['detail']}\n\n"
                f"On the target, this shows whether the service is running and why it stopped:\n\n```bash\n{cmd}\n```\n\n"
                f"Paste what it printed here and I re-check the connection right away.")
    if cls == "host_down":
        cmd = "hostname; ip -4 -brief addr; systemctl is-active local-runner-mcp"
        return (f"**What I checked from the engine host:** {diag['detail']}\n\n"
                f"The engine has this runner registered at **{host}**. On the target, confirm its address and that "
                f"the service is up:\n\n```bash\n{cmd}\n```\n\n"
                f"Paste what it printed here. If the address differs from {host}, tell me the right one and I re-register the runner.")
    if cls == "token":
        return (f"**What I checked from the engine host:** {diag['detail']}\n\n"
                f"Re-run the install line from the previous step (it carries the token the engine registered); the "
                f"installer replaces the running service. Then paste its last line here.")
    if cls == "not_runner":
        return (f"**What I checked from the engine host:** {diag['detail']}\n\n"
                f"Something else is listening on port {port} of {host}. On the target: `ss -tlnp | grep {port}` — paste "
                f"what it shows here.")
    cmd = f"systemctl status local-runner-mcp --no-pager; journalctl -u local-runner-mcp -n 20 --no-pager; ss -tlnp | grep {port}"
    return (f"**What I checked from the engine host:** {diag.get('detail') or 'the connection failed'}\n\n"
            f"On the target:\n\n```bash\n{cmd}\n```\n\nPaste what it printed here and I re-check the connection right away.")


async def probe_local_runner(db) -> dict:
    """§17.1147/1148 — the engine's half of 'Verify the engine reaches the
    runner': ``{"ok", "detail", "class", "repair"}``. Bounded; never touches
    the target beyond the registered endpoint."""
    from app.modules import assist_local_runner as _lr
    try:
        spec = await _lr.runner_spec(db)
    except Exception as exc:
        return {"ok": False, "class": "unknown", "detail": f"registry lookup failed: {exc}", "repair": ""}
    if spec is None:
        return {"ok": False, "class": "unregistered", "repair": "",
                "detail": "no runner is registered on the engine side (ask me for the local runner again and answer yes)."}
    diag = await diagnose_runner_path(spec)
    engine_ip = None
    try:
        engine_ip = await _engine_ip_from_sessions(db)
    except Exception:
        engine_ip = None
    return {"ok": diag["class"] == "ok", "class": diag["class"], "detail": diag["detail"],
            "checks": diag.get("checks") or {}, "repair": runner_repair_block(diag, engine_ip=engine_ip)}


async def _engine_ip_from_sessions(db) -> Optional[str]:
    """The engine's LAN address, when any session learned it (§17.1146
    remember_engine_url). None on this host (the API binds to 127.0.0.1)."""
    from urllib.parse import urlparse
    row = (await db.execute(text("""
        SELECT metadata->'environment'->>'engine_url' AS u FROM assist_sessions
         WHERE metadata->'environment'->>'engine_url' IS NOT NULL ORDER BY updated_at DESC LIMIT 1
    """))).mappings().first()
    host = urlparse((row or {}).get("u") or "").hostname
    return host if host and not _url_is_local(f"http://{host}") else None



def _probe_failure_words(where: str, exc: BaseException) -> str:
    """§17.1147b — live: the first probe printed ``ConnectTimeout: `` (an empty
    message). A transport failure is said in the operator's words: what is
    listening where, and what to check."""
    raw = str(exc)
    low = raw.lower()
    if "connecttimeout" in low or "timed out" in low or "timeout" in low:
        return (f"nothing answered at {where} within a few seconds — the service is not running yet, or port "
                f"{RUNNER_PORT} is not reachable from the engine host.")
    if "refused" in low or "connecterror" in low or "connection error" in low:
        return f"{where} refused the connection — nothing is listening on port {RUNNER_PORT} there yet."
    if "401" in raw or "unauthorized" in low:
        return f"{where} answered but rejected the token — the running helper was started with a different --token; re-run the install line."
    tail = raw.split(":")[-1].strip() or raw.strip()
    return f"{where}: {tail[:200] or exc.__class__.__name__}"


async def _detect_runner_sudo(db) -> tuple[str, str]:
    st, _ = await _detect_local_runner(db)
    if st != "on":
        return "blocked", "Turn on the local runner first."
    return "manual", "Lives on the runner's machine (sudoers + --sudo-allow); the walkthrough confirms it with `sudo -n`."


async def _detect_queue(db) -> tuple[str, str]:
    from app.config import settings
    if settings.queue_enabled:
        return "on", f"the queue worker owns the background chores (cleanup on {settings.queue_cleanup_cron})."
    return "off", "background chores run inside the engine process."


async def _detect_reranker(db) -> tuple[str, str]:
    from app.config import settings
    if (settings.reranker_backend or "local").lower() in ("http", "tei"):
        return "on", f"re-ranking runs in the sidecar at {settings.reranker_url}."
    return "off", "re-ranking runs inside the engine process (~6 s per query, CPU-bound)."


async def _detect_fsm_strict(db) -> tuple[str, str]:
    from app.config import settings
    if settings.assist_step_fsm_strict:
        return "on", "illegal step moves are refused."
    return "off", "illegal step moves are logged (step_fsm_violation) and allowed."


# ---------------------------------------------------------------------------
# The recipes. Briefs are written as the operator's own idea: concrete
# machines, paths, ports and names, so refine → research → plan has facts to
# stand on instead of guessing them. They are the only place these facts
# live; the console renders them and the job carries them.
# ---------------------------------------------------------------------------

_ENGINE_HOST = ("The engine host is this machine: the repo is at ~/scaffold-engine, the engine answers at "
                "http://localhost:8000, its settings live in ~/scaffold-engine/.env (restart after editing with: "
                "cd ~/scaffold-engine && docker compose up -d scaffold-orchestrator), and the API key is the "
                "SCAFFOLD_API_KEY value in that file, sent as the X-API-Key header.")

# §17.1147 — a step carrying this line is checked BY THE ENGINE when the operator
# opens it (render_recipe_guide runs the recipe's probe and reports the result).
PROBE_MARK = "_The engine checks this itself when you open the step._"

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="local_runner",
        keywords=("local runner", "local-runner", "scaffold runner", "the runner on", "run its own checks", "run its own commands", "state check run", "stop asking me to paste", "verify state paste", "local_runner_mcp"),
        steps=(
            ("Install the engine's local runner helper on {target_host}",
             "On {target_host} ({target_user}@{target_host}, this same shell), paste this one line. It downloads the "
             "helper and installs it as the service local-runner-mcp on port {runner_port}, with the token the engine "
             "already generated for it (the helper makes its own Python environment under /opt/scaffold-runner and "
             "installs python3-venv first if the box lacks it). The token {token} is the one the engine registered "
             "under the name {runner_name} for {target_ip}:{runner_port}; the line below already carries it.\n"
             "```bash\n"
             "curl -fsSL {script_url} -o /tmp/local_runner_mcp.py && python3 /tmp/local_runner_mcp.py --install "
             "--port {runner_port} --token {token}\n"
             "```\n"
             "Done when the last line printed starts with OK: local runner active. If it starts with FAILED:, paste "
             "everything it printed."),
            ("Verify the engine reaches the runner",
             "Nothing to type: the engine already registered this runner as {runner_name} at "
             "http://{target_ip}:{runner_port}/mcp/ with that token, and it checks the connection itself when you open "
             "this step. Done when the step reports that it reached the runner and found its run_readonly tool; from "
             "then on Verify state runs the read-only checks through {runner_name} instead of asking you to paste, and "
             "every command it runs is recorded in this transcript marked [local-runner]. If the check fails, paste "
             "the output of: systemctl status local-runner-mcp --no-pager; journalctl -u local-runner-mcp -n 20 --no-pager\n"
             + PROBE_MARK),
        ),
        title="Let the state check run its own commands",
        summary="Verify state runs its read-only checks through a small helper on the target machine instead of asking you to paste.",
        why_off="The engine's rule is that it never touches your machine; this is the one fenced exception, and only you can open it.",
        effort="30 min",
        requires=(),
        detect=_detect_local_runner,
        probe=probe_local_runner,
        brief=(
            "Set up the scaffold-engine local runner so the assist state check can execute its own read-only "
            "probes instead of asking me to paste command output.\n\n"
            + _ENGINE_HOST + "\n\n"
            "The TARGET machine is the one my plans are about (ask me for its IP/hostname and the user I log in as). "
            "Steps:\n"
            "1. On the target machine: copy ~/scaffold-engine/scripts/local_runner_mcp.py from the engine host "
            "(scp is fine), create a Python venv (python3 -m venv ~/runner-venv), install \"mcp>=2.0\" uvicorn "
            "starlette into it, choose a long random secret token, and start the helper with: "
            "~/runner-venv/bin/python local_runner_mcp.py --host 0.0.0.0 --port 8790 --token <secret>. "
            "It must keep running after I log out (a systemd service or nohup is fine). Only the engine host "
            "needs to reach port 8790.\n"
            "2. On the engine host: register it with POST http://localhost:8000/mcp/servers (header X-API-Key) "
            "and JSON body {\"name\":\"pve-runner\",\"transport\":\"streamable_http\","
            "\"endpoint\":\"http://<target-ip>:8790/mcp/\",\"headers\":{\"X-Runner-Token\":\"<secret>\"}}. "
            "Then GET http://localhost:8000/mcp/servers/pve-runner/tools must list a tool named run_readonly.\n"
            "3. On the engine host: add the line ASSIST_LOCAL_RUNNER_SERVER=pve-runner to ~/scaffold-engine/.env, "
            "restart the engine, and confirm curl -s localhost:8000/health reports \"healthy\".\n"
            "4. Verify: in an assist session press Verify state; the reply must say \"Running N read-only checks "
            "through your local runner (pve-runner)\" instead of asking me to paste.\n"
            "The helper refuses anything that is not read-only; nothing here changes the target machine."
        ),
    ),
    Recipe(
        id="runner_sudo",
        steps=(
            ("Decide which read-only commands the runner may run as root",
             "List the exact commands the state check needs root for on {target_host} (typical on Proxmox: pct config, "
             "qm config, pvesm status, nginx -t). Find each one's full path with: which pct qm pvesm nginx. Done when you have "
             "the list with full paths."),
            ("Write the sudoers rule for the runner's user",
             "On the target machine, as root: sudo visudo -f /etc/sudoers.d/scaffold-runner and add ONE line of the form "
             "{target_user} ALL=(root) NOPASSWD: /usr/sbin/pct config *, /usr/sbin/qm config *, /usr/sbin/pvesm status "
             "— full paths, one entry per command, a trailing * only where arguments follow. Done when visudo saves without "
             "a syntax error and sudo -n /usr/sbin/pvesm status (as {target_user}) prints output, not a password prompt. "
             "If the helper already runs as root, nothing here is needed — say so and skip this step."),
            ("Restart the helper with the matching allow-list",
             "On the target machine restart local_runner_mcp.py with the same --host/--port/--token plus "
             "--sudo-allow \"pct config\" \"qm config\" \"pvesm status\" (the same commands as the sudoers line). "
             "Done when a Verify state in this session shows those probes as confirmed or contradicted instead of unknown."),
        ),
        keywords=("runner sudo", "sudo for the runner", "runner administrator", "runner root", "sudo-allow", "runner as root"),
        title="Give the runner administrator rights for specific commands",
        summary="Checks that need root (nginx -t, pct config …) stop coming back as 'unknown'.",
        why_off="Root access is a decision for the machine's owner; the runner is unprivileged until you list exact commands.",
        effort="20 min",
        requires=("local_runner",),
        detect=_detect_runner_sudo,
        brief=(
            "Allow the scaffold-engine local runner (already running on my target machine on port 8790 as an "
            "unprivileged user) to run a short list of READ-ONLY commands as root, so state-check probes like "
            "'nginx -t' and 'pct config <id>' return real output instead of 'a password is required'.\n\n"
            "Two things must agree on the target machine: (a) a sudoers rule for the user that runs the helper, "
            "written with sudo visudo -f /etc/sudoers.d/scaffold-runner, one line of the form "
            "<user> ALL=(root) NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/pct config * — full paths (find them with "
            "which nginx), one entry per command, a trailing * only where arguments follow; and (b) the helper "
            "restarted with the matching allow-list: ~/runner-venv/bin/python local_runner_mcp.py --host 0.0.0.0 "
            "--port 8790 --token <secret> --sudo-allow \"nginx -t\" \"pct config\". Ask me which commands I want "
            "before writing the rule.\n"
            "Verify on the target machine that sudo -n nginx -t prints nginx's 'syntax is ok' lines and not a "
            "password prompt. Anything not on the list still runs unprivileged and the helper says so in its output."
        ),
    ),
    Recipe(
        id="queue_worker",
        steps=(
            ("Create the queue's tables and run one chore through it",
             "On the engine host in ~/scaffold-engine: make queue-schema (safe to repeat), then make queue-once. Done when "
             "every line of the one-shot worker ends with 'ended with status: Success' (the weekly model self-evaluation "
             "can take over ten minutes if it decides it is due)."),
            ("Switch the engine to the queue worker",
             "Add QUEUE_ENABLED=true to ~/scaffold-engine/.env, then: docker compose --profile queue up -d queue and "
             "docker compose up -d scaffold-orchestrator. Done when docker logs scaffold-orchestrator --since 2m 2>&1 | "
             "grep -c delegated_to_queue prints 2 and docker logs scaffold-queue --since 5m shows the worker starting. "
             "Rollback: QUEUE_ENABLED=false, restart the engine, docker compose --profile queue stop queue."),
        ),
        keywords=("queue worker", "procrastinate queue", "background chores", "queue_enabled"),
        title="Move background chores to the queue worker",
        summary="Cleanup, health evaluations, the weekly model self-evaluation and stuck-turn closing run in a separate container with retries and a record of every run.",
        why_off="A second container is a deployment decision; the in-process loops work and the queue path was proven but is new.",
        effort="20 min",
        requires=(),
        detect=_detect_queue,
        brief=(
            "Move scaffold-engine's background chores onto its procrastinate queue worker.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps, all on the engine host in ~/scaffold-engine:\n"
            "1. make queue-schema — creates the queue's own tables in the composed Postgres (safe to repeat).\n"
            "2. make queue-once — defers the cleanup sweep once and runs a one-shot worker; every line must end "
            "with 'ended with status: Success'. Note the weekly model self-evaluation task makes real model calls "
            "and can take over ten minutes if it decides it is due.\n"
            "3. Add QUEUE_ENABLED=true to ~/scaffold-engine/.env, then: docker compose --profile queue up -d queue "
            "(the service queue already exists in ~/scaffold-engine/docker-compose.yml under the queue profile) "
            "and docker compose up -d scaffold-orchestrator.\n"
            "4. Verify: docker logs scaffold-orchestrator --since 2m 2>&1 | grep -c delegated_to_queue prints 2 "
            "(the cleanup loop and the three evaluation jobs both reported delegation), and docker logs "
            "scaffold-queue --since 5m shows the worker starting; within 15 minutes it runs its first chore.\n"
            "Rollback: QUEUE_ENABLED=false, restart the engine, docker compose --profile queue stop queue."
        ),
    ),
    Recipe(
        id="reranker_sidecar",
        steps=(
            ("Start the reranker sidecar container",
             "On the engine host in ~/scaffold-engine: docker compose --profile reranker up -d scaffold-reranker (the first "
             "start loads the model; allow a minute). Done when docker ps shows scaffold-reranker up."),
            ("Point the engine at the sidecar",
             "Add RERANKER_BACKEND=http and RERANKER_URL=http://scaffold-reranker:80 to ~/scaffold-engine/.env, then "
             "docker compose up -d scaffold-orchestrator. Done when curl -s localhost:8000/health | grep -o "
             "'\"reranker\":{[^}]*}' shows \"status\":\"up\" and a knowledge search still returns results. "
             "Rollback: RERANKER_BACKEND=local, restart the engine, docker compose --profile reranker stop scaffold-reranker."),
        ),
        keywords=("reranker sidecar", "reranker container", "search step to its own container", "reranker_backend"),
        title="Move the slow search step to its own container",
        summary="The relevance re-sort (about 6 s a query, all the CPU it can get) stops stalling everything else.",
        why_off="It is a second container on the same host; the in-process reranker works, just slowly.",
        effort="15 min",
        requires=(),
        detect=_detect_reranker,
        brief=(
            "Run scaffold-engine's reranker as its sidecar container instead of inside the engine process.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps on the engine host in ~/scaffold-engine:\n"
            "1. docker compose --profile reranker up -d scaffold-reranker — the service scaffold-reranker already "
            "exists in ~/scaffold-engine/docker-compose.yml under the reranker profile (the engine image with the "
            "same model; the first start loads the model, allow a minute).\n"
            "2. Add RERANKER_BACKEND=http and RERANKER_URL=http://scaffold-reranker:80 to ~/scaffold-engine/.env, "
            "then docker compose up -d scaffold-orchestrator.\n"
            "3. Verify: curl -s localhost:8000/health | grep -o '\"reranker\":{[^}]*}' shows \"status\":\"up\"; "
            "a knowledge search still returns results and the engine stays responsive while one runs.\n"
            "Rollback: RERANKER_BACKEND=local, restart the engine, docker compose --profile reranker stop scaffold-reranker."
        ),
    ),
    Recipe(
        id="step_fsm_strict",
        steps=(
            ("Confirm a week of zero step-rule warnings",
             "On the engine host: docker logs scaffold-orchestrator --since 168h 2>&1 | grep -c step_fsm_violation. Done when "
             "it prints 0. If it does not, STOP and paste the lines (same command without -c): each one is a bug to fix "
             "before going strict, not something to override."),
            ("Switch the rulebook to strict",
             "Add ASSIST_STEP_FSM_STRICT=true to ~/scaffold-engine/.env and restart the engine "
             "(cd ~/scaffold-engine && docker compose up -d scaffold-orchestrator). Done when curl -s localhost:8000/health "
             "reports \"healthy\" and one ordinary assist step (claim → paste → done) still commits normally."),
        ),
        keywords=("step rulebook", "fsm strict", "strict step", "step_fsm_strict"),
        title="Make the step rulebook strict",
        summary="Illegal step-state moves are refused instead of logged and allowed.",
        why_off="Refusing is safer only once the warnings have stayed at zero for a week of real use.",
        effort="5 min, after a week of zero warnings",
        requires=(),
        detect=_detect_fsm_strict,
        brief=(
            "Switch scaffold-engine's assist step state machine to strict mode, but only after confirming it has "
            "logged no violations in a week of normal use.\n\n"
            + _ENGINE_HOST + "\n\n"
            "Steps on the engine host:\n"
            "1. docker logs scaffold-orchestrator --since 168h 2>&1 | grep -c step_fsm_violation — must print 0. "
            "If it is not 0, STOP: show me the lines (same command without -c); each one is a bug to fix before "
            "going strict, not something to override.\n"
            "2. When it is 0: add ASSIST_STEP_FSM_STRICT=true to ~/scaffold-engine/.env and restart the engine.\n"
            "3. Verify: curl -s localhost:8000/health reports \"healthy\", and one ordinary assist step "
            "(claim → paste → done) still commits normally."
        ),
    ),
)

BY_ID: dict[str, Recipe] = {r.id: r for r in RECIPES}


# ---------------------------------------------------------------------------
# Status view + start.
# ---------------------------------------------------------------------------

async def _open_jobs_by_recipe(db) -> dict[str, dict]:
    """{recipe_id: {job_id, status}} for the newest job per recipe, so the
    console can link 'in progress' to the walkthrough and 'off' to the last
    attempt."""
    try:
        rows = (await db.execute(text("""
            SELECT DISTINCT ON (metadata->>'setup_recipe') metadata->>'setup_recipe' AS rid, id::text AS job_id, status
              FROM jobs WHERE metadata ? 'setup_recipe'
             ORDER BY metadata->>'setup_recipe', created_at DESC
        """))).mappings().all()
    except Exception as exc:
        logger.warning("engine_setup_jobs_lookup_failed err=%r", exc)
        return {}
    return {r["rid"]: {"job_id": r["job_id"], "status": r["status"]} for r in rows if r["rid"]}


async def list_recipes(db) -> list[dict]:
    jobs = await _open_jobs_by_recipe(db)
    out: list[dict] = []
    for r in RECIPES:
        status, detail = await r.detect(db) if r.detect else ("manual", "")
        job = jobs.get(r.id)
        if status != "on" and job and job["status"] not in _OPEN_JOB_STATUSES:
            status, detail = "in_progress", f"a walkthrough is open (job {job['job_id'][:8]}…, {job['status']})."
        out.append({
            "id": r.id, "title": r.title, "summary": r.summary, "why_off": r.why_off, "effort": r.effort,
            "requires": list(r.requires), "status": status, "status_detail": detail,
            "job_id": job["job_id"] if job else None, "job_status": job["status"] if job else None,
        })
    return out


async def start_recipe(db, recipe_id: str, *, owner: Optional[str]) -> dict:
    """Open the walkthrough: the recipe's brief goes through the same door as
    any operator idea. Raises KeyError for an unknown id and ValueError when
    a prerequisite recipe is not on."""
    r = BY_ID[recipe_id]
    for dep in r.requires:
        detect = BY_ID[dep].detect
        if detect is None:  # §17.1141 — a prerequisite without a detector cannot be verified
            raise ValueError(f"'{BY_ID[dep].title}' must be on first (no detector to confirm it)")
        st, _ = await detect(db)
        if st != "on":
            raise ValueError(f"'{BY_ID[dep].title}' must be on first")
    from app.modules.idea_refinement import create_ideation_job
    from app.modules.ideation_workflow import spawn_phase1_background
    job_id = await create_ideation_job(r.brief, db, owner=owner)
    await db.execute(text("""
        UPDATE jobs SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :jid
    """), {"m": json.dumps({"setup_recipe": r.id, "prescriptive": True}), "jid": job_id})
    await db.commit()
    spawn_phase1_background(job_id, r.brief)
    logger.info("engine_setup_started recipe=%s job_id=%s", r.id, job_id)
    return {"job_id": job_id, "status": "refining", "recipe": r.id}


async def job_is_prescriptive(db, job_id: str) -> bool:
    """§17.1081b — a PRESCRIPTIVE brief already lists its exact steps: the
    research phase must not branch it into options and the planner must not
    open it with a decision node or an 'inspect/assess' prelude. Live: the
    reranker recipe planned as 'Decide reranker wiring approach' with three
    invented options (one violating its own constraint) and 'Add reranker
    service definition' for a service that already exists. Stamped in
    jobs.metadata by `start_recipe`; any caller may set it."""
    try:
        meta = (await db.execute(text("SELECT metadata FROM jobs WHERE id = :jid"), {"jid": job_id})).scalar()
        if isinstance(meta, str):
            meta = json.loads(meta)
        return bool(isinstance(meta, dict) and meta.get("prescriptive"))
    except Exception as exc:
        logger.warning("prescriptive_lookup_failed job=%s err=%r", job_id, exc)
        return False


PRESCRIBED_BLOCK = """PRESCRIBED PLAN — the operator's brief already lists the exact steps, in order, with the commands to run.
Emit ONE node per listed step, in the brief's order, carrying the brief's own commands and names verbatim.
Do NOT add: decision nodes, "decide/choose/assess/inspect/evaluate options" preludes, alternatives to what
the brief prescribes, or steps that create/download/define things the brief says already exist.
Keep every verification the brief names as its own node. If a listed step is destructive or unclear, keep it
as written and say so in its notes — do not replace it with your own design.

"""


def runner_nudge() -> str:
    """One line for the state check's paste request when no runner is set:
    the moment the operator feels the cost is the moment to say the engine
    can carry it."""
    from app.config import settings
    if (settings.assist_local_runner_server or "").strip():
        return ""
    return ("\n\n_The engine can run these checks itself through a small helper on your machine — "
            "**Capabilities → Let the state check run its own commands** in the console walks you through it._")


# ---------------------------------------------------------------------------
# §17.1144 — the assist knows the engine's own capabilities.
# Live: "please assist me in setting up the local runner on the proxmox server"
# went to the web ("SAX1V1K ES2251 node Proxmox local runner setup verified
# pve-firewall…") and came back as a plan to build a CI-runner VM. The
# recipes existed behind the Capabilities page; nothing in the turn loop
# knew the words. This is the deterministic bridge: a message that names a
# recipe is answered FROM the recipe (what it is, why it is off, its live
# status) and the walkthrough is offered as a staged yes/no — the same scoped
# affirmative the completion offer uses (§17.951).
# ---------------------------------------------------------------------------

SETUP_OFFER_KEY = "pending_setup_offer"


def _paste_shaped(text: str) -> bool:
    """A shell paste that merely contains a keyword is evidence, not a question."""
    t = text or ""
    if len(t) > 700:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) > 6:
        return True
    return any(ln.lstrip().startswith(("root@", "$ ", "# ", "PS ")) for ln in lines)


def match_recipe(text: str) -> Optional[Recipe]:
    """The recipe an operator message is about, or None. Deterministic, no model."""
    t = " ".join((text or "").lower().split())
    if not t or _paste_shaped(text or ""):
        return None
    for r in RECIPES:
        for k in r.keywords:
            if k in t:
                return r
    return None


RECIPE_STEP_MARK = "Engine capability recipe:"


def recipe_version(recipe: "Recipe") -> str:
    """A short hash of the recipe's step templates: steps stamped with an older
    version are stale (§17.1146 — the first-cut steps carried placeholders)."""
    import hashlib
    return hashlib.sha1("\n".join(f"{t}\n{w}" for t, w in recipe.steps).encode()).hexdigest()[:8]
LOCAL_RUNNER_MARK = "[scaffold local runner]"   # description tag on the mcp_servers row the assist registers
RUNNER_NAME = "pve-runner"
RUNNER_PORT = 8790

# Placeholders shown when a value is not known yet — the guide then asks for
# that ONE thing instead of inventing it.
_UNKNOWN = {
    "target_ip": "<the target machine's IP>", "target_host": "the target machine", "target_user": "<the login user>",
    "engine_url": "http://<the engine host's IP>:8000", "token": "<the token the engine generated>",
    "runner_name": RUNNER_NAME, "runner_port": str(RUNNER_PORT),
    "script_url": "",   # filled by recipe_context: the engine's own route when it is LAN-reachable, else the public repo
}
# Where the target fetches the helper when the engine is not reachable from it (this
# host binds the API to 127.0.0.1 — the browser's requests arrive as localhost, so the
# engine's own URL is unknown to the target). The repo is public; `main` carries the script.
RUNNER_SCRIPT_FALLBACK_URL = "https://raw.githubusercontent.com/LocketKeyLLC/scaffold-engine/main/scripts/local_runner_mcp.py"


def _url_is_local(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    return host in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0")


async def remember_engine_url(db, session_id: str, base_url: str) -> None:
    """§17.1146 — the engine learns its own reachable address from the request
    the operator's browser makes: the ONE fact every setup step needs and that
    no fact ledger records. Loopback addresses are not remembered (they are
    not reachable from the target machine). Fail-soft."""
    url = (base_url or "").rstrip("/")
    if not url or _url_is_local(url):
        return
    try:
        await db.execute(text("""
            UPDATE assist_sessions
               SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{environment,engine_url}',
                                        to_jsonb(CAST(:u AS text)), true)
             WHERE id = :sid AND COALESCE(metadata->'environment'->>'engine_url', '') <> :u
        """), {"sid": session_id, "u": url})
        await db.commit()
    except Exception as exc:
        logger.warning("engine_url_remember_failed sid=%s err=%r", session_id, exc)


async def recipe_context(db, session_id: str) -> dict:
    """What the engine already knows that a recipe step needs: the target
    machine (system map host + the profile's `user@host`), its own reachable
    URL (remembered from the operator's requests), and a fresh token."""
    import re as _re
    import secrets
    ctx = dict(_UNKNOWN)
    ctx["token"] = secrets.token_hex(24)
    try:
        row = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                {"sid": session_id})).mappings().first()
        meta = (row or {}).get("metadata") or {}
        env = meta.get("environment") if isinstance(meta, dict) else {}
        env = env if isinstance(env, dict) else {}
        host = ((env.get("system_state") or {}).get("host") or {})
        ip = str(((host.get("attrs") or {}).get("ip") or "")).strip()
        if ip:
            ctx["target_ip"] = ip
        m = _re.search(r"\b([a-z_][a-z0-9_-]*)@([a-z0-9][a-z0-9.-]*)", str(env.get("profile") or ""), _re.I)
        if m:
            ctx["target_user"], ctx["target_host"] = m.group(1), m.group(2)
        elif ip:
            ctx["target_host"] = ip
        url = str(env.get("engine_url") or "").strip().rstrip("/")
        if url and not _url_is_local(url):
            ctx["engine_url"] = url
    except Exception as exc:
        logger.warning("recipe_context_failed sid=%s err=%r", session_id, exc)
    ctx["script_url"] = (f"{ctx['engine_url']}/setup/runner/local_runner_mcp.py" if known(ctx, "engine_url")
                         else RUNNER_SCRIPT_FALLBACK_URL)
    return ctx


def known(ctx: dict, key: str) -> bool:
    return bool(ctx.get(key)) and ctx[key] != _UNKNOWN.get(key)


async def register_local_runner(db, ctx: dict) -> bool:
    """§17.1146 — the engine-side half of the recipe, done BY the engine: the
    mcp_servers row for the runner (name, endpoint, token header) so the operator
    only ever touches the target machine. Idempotent; needs the target IP."""
    if not known(ctx, "target_ip"):
        return False
    from app.modules.mcp_registry import McpServerSpec, upsert_server
    spec = McpServerSpec(
        name=ctx["runner_name"], transport="streamable_http",
        endpoint=f"http://{ctx['target_ip']}:{ctx['runner_port']}/mcp/",
        headers={"X-Runner-Token": ctx["token"]}, enabled=True,
        description=f"{LOCAL_RUNNER_MARK} registered by the assist for {ctx['target_host']}",
    )
    spec.validate()
    await upsert_server(db, spec)
    await db.commit()
    try:
        from app.modules import mcp_client
        mcp_client.clear_tool_cache(spec.name)
    except Exception:
        pass
    logger.info("local_runner_registered name=%s endpoint=%s", spec.name, spec.endpoint)
    return True


def recipe_steps(recipe: Recipe, *, with_prerequisites: bool = True, ctx: Optional[dict] = None) -> list[dict]:
    """The recipe (and, when asked, its missing prerequisites first) as
    ``[{title, description}]`` ready for ``assist_notes.add_step(steps=…)``.
    Each description ends with a marker line so the plan can be asked whether
    it already carries this recipe."""
    out: list[dict] = []
    values = {**_UNKNOWN, **(ctx or {})}
    if not values.get("script_url"):
        values["script_url"] = (f"{values['engine_url']}/setup/runner/local_runner_mcp.py"
                                if known(values, "engine_url") else RUNNER_SCRIPT_FALLBACK_URL)
    chain = list(recipe.requires) if with_prerequisites else []
    for rid in chain + [recipe.id]:
        r = BY_ID[rid]
        for i, (title, what) in enumerate(r.steps):
            out.append({"title": title.format_map(values),
                        "description": f"{what.format_map(values)}\n\n_{RECIPE_STEP_MARK} {r.id} v{recipe_version(r)} #{i}_"})
    return out


async def plan_has_recipe(db, session_id: str, recipe_id: str) -> Optional[dict]:
    """The first still-open step of this recipe already in the session's plan
    (``{node_key, title, status}``), or None. Fail-soft."""
    try:
        rows = (await db.execute(text("""
            SELECT d.node_key, d.title, s.status, d.description
              FROM assist_steps s JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid AND (d.description LIKE :mark_v OR d.description LIKE :mark_old)
             ORDER BY d.node_key
        """), {"sid": session_id, "mark_v": f"%{RECIPE_STEP_MARK} {recipe_id} v%",
               "mark_old": f"%{RECIPE_STEP_MARK} {recipe_id}_%"})).mappings().all()
        opened = [dict(r) for r in rows if r["status"] not in ("committed", "skipped", "handed_off")]
        if not opened:
            return None
        # §17.1146 — steps stamped by an OLDER version of the recipe (or by the
        # unversioned first cut) are stale: offer to replace, never guide them.
        want = f"{RECIPE_STEP_MARK} {recipe_id} v{recipe_version(BY_ID[recipe_id])}"
        first = {k: v for k, v in opened[0].items() if k != "description"}
        first["stale"] = any(want not in (r.get("description") or "") for r in opened)
        first["open_keys"] = [r["node_key"] for r in opened]
        return first
    except Exception as exc:
        logger.warning("plan_has_recipe_failed sid=%s recipe=%s err=%r", session_id, recipe_id, exc)
        return None


def capability_answer(recipe: Recipe, *, status: str, detail: str, in_plan: Optional[dict] = None,
                      current_step: Optional[str] = None) -> str:
    """The reply: what the capability is, in the recipe's own plain words, its
    live state on this install, and the offer — to add its steps to THIS plan
    and walk through them here. Every line carries content."""
    head = f"## {recipe.title}\n\n"
    body = (f"That is one of the engine's own optional capabilities, not part of your homelab plan itself. "
            f"{recipe.summary} {recipe.why_off}\n\n")
    n_steps = len(recipe_steps(recipe))
    where = f" right before **{current_step}**" if current_step else ""
    if in_plan and current_step and current_step in (in_plan.get("open_keys") or []):
        where = ""   # §17.1147 — the current step IS one of the stale ones; the new steps take its place
    if in_plan and not in_plan.get("stale"):
        state = (f"**Its steps are already in this plan** — the next open one is **{in_plan['node_key']}: "
                 f"{in_plan['title']}**.\n\n")
        offer = f"Say **guide me on {in_plan['node_key']}** (or press Guide me on that step) and we continue there."
    elif status == "on":
        state = f"**It is already on here:** {detail}\n\n"
        offer = ("Nothing to set up — press **Verify state** on any step and the checks run through it. "
                 "Everything it runs is recorded in this transcript, marked `[local-runner]`.")
    elif in_plan and in_plan.get("stale"):
        keys = ", ".join(in_plan.get("open_keys") or [in_plan["node_key"]])
        state = f"**An older version of its steps is in this plan** ({keys}).\n\n"
        offer = (f"Reply **yes** and I will mark those skipped and add the current {n_steps} steps in their place{where}, "
                 f"filled in with what I already know, and walk you through the first one now.")
    elif status == "blocked":
        pre = ", ".join(BY_ID[x].title for x in recipe.requires) or "its prerequisite"
        state = f"**Not available yet:** {detail}\n\n"
        offer = (f"Reply **yes** and I will add the steps for {pre} first and then for this, {n_steps} steps in all, "
                 f"to this plan{where}, and walk you through the first one now.")
    else:
        state = f"**On this install it is off** — {detail}\n\n"
        offer = (f"Reply **yes** and I will add its {n_steps} steps to this plan{where} (about {recipe.effort}) and walk "
                 f"you through the first one now, the same way as any other step. Reply **no** to leave it off.")
    return head + body + state + offer


async def retire_recipe_steps(db, session_id: str, recipe_id: str) -> list[str]:
    """§17.1146 — mark this recipe's still-open steps skipped (an older version
    is being replaced). Returns the node keys retired."""
    from app.modules.assist_step_fsm import check as _fsm_check  # §17.1074 — the oracle runs before every status write
    rows = (await db.execute(text("""
        SELECT s.node_key, s.job_id, s.status FROM assist_steps s JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
         WHERE s.session_id = :sid AND (d.description LIKE :mark_v OR d.description LIKE :mark_old)
           AND s.status NOT IN ('committed', 'skipped', 'handed_off')
    """), {"sid": session_id, "mark_v": f"%{RECIPE_STEP_MARK} {recipe_id} v%",
           "mark_old": f"%{RECIPE_STEP_MARK} {recipe_id}_%"})).mappings().all()
    keys = [r["node_key"] for r in rows]
    for r in rows:
        _fsm_check("retire_recipe_step", src=r["status"], dst="skipped", node_status="skipped",
                   node_key=r["node_key"], trigger="skip")
        await db.execute(text("UPDATE assist_steps SET status = 'skipped', updated_at = NOW() WHERE session_id = :sid AND node_key = :nk"),
                         {"sid": session_id, "nk": r["node_key"]})
        await db.execute(text("UPDATE dag_nodes SET status = 'skipped', updated_at = NOW() WHERE job_id = :jid AND node_key = :nk"),
                         {"jid": r["job_id"], "nk": r["node_key"]})
    if keys:
        await db.commit()
        logger.info("recipe_steps_retired sid=%s recipe=%s keys=%s", session_id, recipe_id, keys)
    return keys


async def add_recipe_to_plan(db, session_id: str, recipe: Recipe, *, before_node_key: Optional[str],
                             replace: bool = False) -> dict:
    """Insert the recipe's steps (prerequisites first) into the session's plan
    before the current step, FILLED IN with what the engine already knows
    (target machine, its own URL, a generated token), through the ordinary
    add_step persist path (no model call), and point the session at the first.
    The engine-side half (registering the runner) is done here, by the engine."""
    from app.modules import assist_notes
    ctx = await recipe_context(db, session_id)
    retired: list[str] = []
    if replace:
        retired = await retire_recipe_steps(db, session_id, recipe.id)
        # §17.1147 — live: the anchor was the retired step itself (ADD70 was the
        # current step), and add_step REOPENS its anchor, so the placeholder step
        # came back as pending behind the new ones. Anchor on the step the old
        # ones ran before instead.
        if retired and (not before_node_key or before_node_key in retired):
            before_node_key = await successor_anchor(db, session_id, retired) or None
    registered = False
    if recipe.id in ("local_runner", "runner_sudo"):
        try:
            registered = await register_local_runner(db, ctx)
        except Exception as exc:
            logger.warning("local_runner_register_failed sid=%s err=%r", session_id, exc)
    res = await assist_notes.add_step(
        session_id=session_id, request=recipe.title, before_node_key=before_node_key,
        steps=recipe_steps(recipe, ctx=ctx), db=db,
    )
    res = dict(res or {})
    res["retired"] = retired
    res["registered"] = registered
    res["known"] = {k: known(ctx, k) for k in ("target_ip", "target_user", "engine_url")}
    logger.info("recipe_added_to_plan sid=%s recipe=%s known=%s registered=%s retired=%s",
                session_id, recipe.id, res["known"], registered, retired)
    return res


async def stage_setup_offer(*, session_id: str, recipe_id: str, db, replace: bool = False) -> None:
    from datetime import datetime, timezone
    offer = {"recipe_id": recipe_id, "replace": bool(replace), "staged_at": datetime.now(timezone.utc).isoformat()}
    await db.execute(text("""
        UPDATE assist_sessions
           SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb), updated_at = NOW()
         WHERE id = :sid
    """), {"sid": session_id, "patch": json.dumps({SETUP_OFFER_KEY: offer})})
    await db.commit()
    logger.info("setup_offer_staged session_id=%s recipe=%s", session_id, recipe_id)


async def get_pending_setup_offer(*, session_id: str, db) -> Optional[dict]:
    try:
        row = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                {"sid": session_id})).mappings().first()
        meta = (row or {}).get("metadata")
        offer = meta.get(SETUP_OFFER_KEY) if isinstance(meta, dict) else None
        if isinstance(offer, dict) and offer.get("recipe_id") in BY_ID:
            return offer
    except Exception as exc:
        logger.warning("setup_offer_read_failed sid=%s err=%r", session_id, exc)
    return None


async def clear_pending_setup_offer(*, session_id: str, db) -> None:
    try:
        await db.execute(text(
            "UPDATE assist_sessions SET metadata = COALESCE(metadata, '{}'::jsonb) - "
            f"'{SETUP_OFFER_KEY}', updated_at = NOW() WHERE id = :sid"), {"sid": session_id})
        await db.commit()
    except Exception as exc:
        logger.warning("setup_offer_clear_failed sid=%s err=%r", session_id, exc)


async def successor_anchor(db, session_id: str, retired: list[str]) -> Optional[str]:
    """§17.1147 — the first still-open NON-recipe step that depends on any of
    the retired recipe steps: the step the recipe was inserted before. None
    when nothing depends on them (add_step then anchors on the session's
    pointer, which the caller has already ruled out)."""
    if not retired:
        return None
    try:
        rows = (await db.execute(text("""
            SELECT d.node_key FROM dag_nodes d JOIN assist_steps s ON s.job_id = d.job_id AND s.node_key = d.node_key
             WHERE s.session_id = :sid AND d.depends_on && CAST(:keys AS text[])
               AND d.description NOT LIKE :mark AND s.status NOT IN ('committed', 'skipped', 'handed_off')
             ORDER BY d.execution_order NULLS LAST, d.node_key
        """), {"sid": session_id, "keys": list(retired), "mark": f"%{RECIPE_STEP_MARK}%"})).mappings().all()
    except Exception as exc:
        logger.warning("successor_anchor_failed sid=%s err=%r", session_id, exc)
        return None
    return rows[0]["node_key"] if rows else None


# ---------------------------------------------------------------------------
# §17.1147 — recipe steps are guided BY THE RECIPE, not by the model. Live: the
# guide for the install step spent 42 s, two web searches and five model calls
# to re-derive a step the engine had written itself — and opened with an
# `ls` check of its own invention before the one line the operator had to
# paste. The engine knows exactly what these steps say; it renders them.
# ---------------------------------------------------------------------------

_MARK_RE = None


def recipe_of_node(description: Optional[str]) -> Optional[Recipe]:
    """The recipe a plan step belongs to, from its marker line — only for the
    CURRENT version of its steps (older stamps are stale and get replaced,
    never guided from a template they no longer match)."""
    global _MARK_RE
    import re as _re
    if _MARK_RE is None:
        _MARK_RE = _re.compile(_re.escape(RECIPE_STEP_MARK) + r" ([a-z_]+) v([0-9a-f]{8})(?: #(\d+))?_")
    m = _MARK_RE.search(description or "")
    if not m:
        return None
    r = BY_ID.get(m.group(1))
    if r is None or recipe_version(r) != m.group(2):
        return None
    return r


def _split_step_text(description: str) -> tuple[str, list[str], str, str]:
    """``(lead, code_blocks, done_when, tail)`` from a step description written
    in the recipe layout: prose, ```fenced``` command blocks, a sentence that
    starts with 'Done when', then the marker lines (dropped)."""
    import re as _re
    body = description or ""
    body = body.replace(PROBE_MARK, "")
    body = _re.sub(r"_?" + _re.escape(RECIPE_STEP_MARK) + r"[^\n]*", "", body).strip()
    blocks = _re.findall(r"```(?:bash|sh)?\n(.*?)```", body, _re.S)
    prose = _re.sub(r"```(?:bash|sh)?\n.*?```", "", body, flags=_re.S)
    prose = _re.sub(r"[ \t]*\n[ \t]*", "\n", prose).strip()
    m = _re.search(r"Done when\b", prose)
    lead, done = (prose[:m.start()].strip(), prose[m.end():].strip()) if m else (prose, "")
    if done:
        done = done[0].upper() + done[1:]
    tail = ""
    m2 = _re.search(r"\bIf (?:it|the check) (?:starts with|fails)[^\n]*", done)
    if m2:
        done, tail = done[:m2.start()].strip(), done[m2.start():].strip()
    return lead, [b.strip() for b in blocks], done, tail


async def render_recipe_guide(node_description: Optional[str], *, db) -> Optional[dict]:
    """The walkthrough for a recipe step, rendered from the step itself:
    ``{"text", "meta"}`` or None when the step is not a current recipe step.
    A step carrying PROBE_MARK is checked by the engine right here (the
    recipe's ``probe``), and the result IS the walkthrough."""
    r = recipe_of_node(node_description)
    if r is None:
        return None
    lead, blocks, done, tail = _split_step_text(node_description or "")
    meta: dict[str, Any] = {"recipe": r.id, "deterministic": True, "status": "ready"}
    parts: list[str] = []
    if PROBE_MARK in (node_description or "") and r.probe is not None:
        pr = await r.probe(db)
        ok, detail = bool(pr.get("ok")), str(pr.get("detail") or "")
        meta["probe"] = {"ok": ok, "detail": detail, "class": pr.get("class")}
        if ok:
            parts.append(f"## ✅ The engine reached your runner\n\n{detail[0].upper() + detail[1:]}.\n\n"
                         f"{lead}\n\n{done}\n\nNothing to paste — press **✓ Done → next step** (or type `next`).")
        else:
            repair = pr.get("repair") or (tail or "Fix that on the target, then press Guide me on this step to check again.")
            parts.append(f"## ❌ The engine could not reach the runner yet\n\n{repair}\n\n"
                         f"_{lead}_\n\n"
                         f"(Or press **Guide me** on this step to re-check without pasting.)")
        return {"text": "\n".join(parts), "meta": meta}
    parts.append("## 👉 Do this next\n")
    if lead:
        parts.append(lead + "\n")
    for b in blocks:
        parts.append(f"**Run this now:**\n\n```bash\n{b}\n```\n")
    if done:
        parts.append(f"## ✅ Done when\n\n{done}\n")
    if tail:
        parts.append(tail + "\n")
    parts.append("Paste what it printed here, or press **✓ Done → next step** (or type `next`) once it says OK.")
    return {"text": "\n".join(parts), "meta": meta}


def recipe_step_index(description: Optional[str]) -> Optional[int]:
    """Which of the recipe's steps this node is (from the ``#n`` in its marker);
    None for markers stamped before §17.1148."""
    recipe_of_node(description)   # compiles _MARK_RE
    m = _MARK_RE.search(description or "") if _MARK_RE else None
    return int(m.group(3)) if m and m.group(3) else None


_INSTALL_MARKERS = ("[1/4]", "[2/4]", "[3/4]", "[4/4]", "OK:", "FAILED:")


def check_install_output(evidence: str) -> Optional[dict]:
    """§17.1148 — the install step's own Done-when, read by the engine: the
    installer prints ONE verdict line. ``None`` when the paste is not the
    installer's output at all (the ordinary verifier judges it)."""
    ev = evidence or ""
    if not any(m in ev for m in _INSTALL_MARKERS):
        return None
    lines = [ln.strip() for ln in ev.splitlines() if ln.strip()]
    ok = [ln for ln in lines if ln.startswith("OK: local runner active")]
    failed = [i for i, ln in enumerate(lines) if ln.startswith("FAILED:")]
    if ok:
        return {"outcome": "success", "reason": ok[-1], "summary": "the installer printed its OK line"}
    if failed:
        i = failed[-1]
        return {"outcome": "failed", "summary": lines[i],
                "reason": "The installer stopped:\n\n```\n" + "\n".join(lines[i:i + 8]) + "\n```\n\n"
                          "Paste is recorded — let's fix that and re-run the same install line."}
    return {"outcome": "incomplete", "summary": "the installer's verdict line is missing",
            "reason": ("The install is not finished, or the paste stopped early: the installer ends with ONE line "
                       "starting with `OK:` or `FAILED:` and that line is not here. Let it finish (the dependency "
                       "step can take a minute), then paste from `[1/4]` through the last line.")}


async def verify_recipe_submit(*, db, session_id: str, node_key: str, evidence: str) -> Optional[dict]:
    """§17.1148 — a recipe step is verified BY THE RECIPE, before (instead of)
    the model verifier: the verify step re-runs the engine's probe; the install
    step reads the installer's verdict line. Returns the verdict dict the
    submit endpoint already understands (``outcome`` success | incomplete |
    failed, ``reason``, ``summary``) plus ``recipe``; None when the step is not
    a current recipe step or the recipe has nothing to say."""
    try:
        desc = (await db.execute(text("""
            SELECT d.description FROM assist_steps s JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid AND s.node_key = :nk
        """), {"sid": session_id, "nk": node_key})).scalar()
    except Exception as exc:
        logger.warning("verify_recipe_submit_lookup_failed sid=%s nk=%s err=%r", session_id, node_key, exc)
        return None
    r = recipe_of_node(desc)
    if r is None:
        return None
    if PROBE_MARK in (desc or "") and r.probe is not None:
        pr = await r.probe(db)
        if pr.get("ok"):
            v = {"outcome": "success", "reason": pr.get("detail") or "the engine reached the runner",
                 "summary": "the engine reached the runner"}
        else:
            v = {"outcome": "incomplete", "summary": pr.get("detail") or "the engine could not reach the runner",
                 "reason": (f"**The engine still cannot reach the runner** — {pr.get('detail') or ''}\n\n"
                            + (pr.get("repair") or ""))}
        v.update({"recipe": r.id, "probe_class": pr.get("class")})
        logger.info("recipe_submit_verified sid=%s nk=%s recipe=%s outcome=%s class=%s",
                    session_id, node_key, r.id, v["outcome"], pr.get("class"))
        return v
    if r.id == "local_runner" and recipe_step_index(desc) in (0, None) and "--install" in (desc or ""):
        v = check_install_output(evidence)
        if v is None:
            return None
        v["recipe"] = r.id
        logger.info("recipe_submit_verified sid=%s nk=%s recipe=%s outcome=%s", session_id, node_key, r.id, v["outcome"])
        return v
    return None
