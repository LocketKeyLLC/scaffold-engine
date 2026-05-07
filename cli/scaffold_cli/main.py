"""Click entry point for ``scaffold``.

Sprint H ships the read-mostly + ideate/confirm flows. Sprint U.3 adds
output legibility: every command's --help has an Examples: block, every
output ends with a "Next:" line where it makes sense, and `jobs status`
renders the orchestrator's `next_actions` field as a copy-pasteable list.
"""
from __future__ import annotations

import json as _json
import sys
from typing import Any

import click

from scaffold_cli import __version__
from scaffold_cli.client import CLIError, Client
from scaffold_cli.config import resolve_config
from scaffold_cli import project as _project


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------

def _render_next_actions(data: dict) -> None:
    """Render the orchestrator's ``next_actions`` field (audit item 10)
    as a markdown-flavored bulleted block, written to stdout.

    Filters out wait-style actions (which are noise on a CLI surface)
    and pretty-prints commands so a user can copy-paste the next step.
    No-op when the response carries no actions.
    """
    actions = data.get("next_actions") or []
    renderable = [a for a in actions if a.get("action") != "wait"]
    if not renderable:
        return
    click.echo("")
    click.secho("Next steps:", fg="cyan", bold=True)
    for a in renderable:
        cmd = a.get("command")
        endpoint = a.get("endpoint")
        method = a.get("method", "GET")
        desc = a.get("description", "")
        if cmd:
            click.echo(f"  • ", nl=False)
            click.secho(cmd, fg="cyan", nl=False)
            click.echo(f"   — {desc}")
        elif endpoint:
            click.echo(f"  • ", nl=False)
            click.secho(f"{method} {endpoint}", fg="cyan", nl=False)
            click.echo(f"   — {desc}")
        else:
            click.echo(f"  • {desc}")


def _hint(text: str) -> None:
    """Print a single 'Next:' hint line in the conventional cyan."""
    click.echo("")
    click.secho(f"Next: {text}", fg="cyan")


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

ROOT_EPILOG = """
\b
Examples:
  scaffold version                          show CLI version + config source
  scaffold doctor                           probe orchestrator /health
  scaffold ideate "build a markdown linter" submit an idea (Phase 1)
  scaffold confirm <job_id>                 approve and run Phase 2 + execute
  scaffold jobs list --limit 10             recent jobs
  scaffold jobs status <job_id>             one job's full state + next steps

Configuration priority:
  --api-url / --api-key flags  >  $SCAFFOLD_API_URL / $SCAFFOLD_API_KEY env  >
  ~/.scaffold/config.toml  >  walked-up .env  >  default http://localhost:8000
"""


@click.group(
    help="Terminal client for Scaffold Engine.",
    epilog=ROOT_EPILOG,
)
@click.option(
    "--api-url", envvar="SCAFFOLD_API_URL_FLAG", default=None,
    help="Orchestrator base URL (overrides env / config / .env).",
)
@click.option(
    "--api-key", envvar="SCAFFOLD_API_KEY_FLAG", default=None,
    help="Orchestrator API key (overrides env / config / .env).",
)
@click.pass_context
def cli(ctx: click.Context, api_url: str | None, api_key: str | None) -> None:
    cfg = resolve_config(flag_url=api_url, flag_key=api_key)
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

VERSION_EPILOG = """
\b
Examples:
  scaffold version                  show version + where config came from
"""


@cli.command(
    help="Print the CLI version and where its config came from.",
    epilog=VERSION_EPILOG,
)
@click.pass_context
def version(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"scaffold-cli {__version__}")
    click.echo(f"  api_url: {cfg.api_url}  ({cfg.source})")
    click.echo(f"  api_key: {'set' if cfg.api_key else 'unset'}")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

DOCTOR_EPILOG = """
\b
Examples:
  scaffold doctor                       probe orchestrator at default URL
  scaffold --api-url http://h:8000 doctor   probe a remote orchestrator
"""


@cli.command(
    help="Probe orchestrator /health and print a per-subsystem summary.",
    epilog=DOCTOR_EPILOG,
)
@click.pass_context
def doctor(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"Probing {cfg.api_url}/health …")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/health")
    except CLIError as exc:
        click.secho(f"FAIL  {exc}", fg="red", err=True)
        _hint("scaffold doctor again once the orchestrator is up; or `make doctor` for the full host-side audit.")
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(f"unexpected response: {data!r}")
        sys.exit(1)

    checks = data.get("checks", {})
    if not checks:
        click.echo(_json.dumps(data, indent=2))
        return

    UP = {"up", "ok", "healthy", "true"}
    DOWN = {"down", "fail", "error", "unhealthy"}
    any_down = False
    for name, info in checks.items():
        status = info.get("status", "?") if isinstance(info, dict) else str(info)
        latency = info.get("latency_ms") if isinstance(info, dict) else None
        if status.lower() in UP:
            color = "green"
        elif status.lower() in DOWN:
            color = "red"
            any_down = True
        else:
            color = "yellow"
        latency_str = f"  {latency} ms" if latency is not None else ""
        click.secho(f"  {status:<10}", fg=color, nl=False)
        click.echo(f"{name}{latency_str}")

    if any_down:
        _hint("`make doctor --explain` for what each subsystem does and why it might be down.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# ideate
# ---------------------------------------------------------------------------

IDEATE_EPILOG = """
\b
Examples:
  scaffold ideate "build a markdown linter"
  scaffold ideate "build me a CLI tool that gzips files older than 7 days"
  scaffold ideate --domain eng "optimize my python build pipeline"
  scaffold ideate --json "build X" | jq -r '.job_id'

After this returns, the job is in `awaiting_confirmation`. Approve it with
`scaffold confirm <job_id>` to start research + execution.
"""


@cli.command(
    help="Submit an idea: orchestrator refines + assesses feasibility.",
    epilog=IDEATE_EPILOG,
)
@click.argument("idea", nargs=-1, required=True)
@click.option(
    "--domain", default=None,
    help="Optional domain hint (eng/llm/rag/spec/prompt).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def ideate(
    ctx: click.Context,
    idea: tuple[str, ...],
    domain: str | None,
    as_json: bool,
) -> None:
    idea_text = " ".join(idea).strip()
    if not idea_text:
        raise click.UsageError("idea text is required")

    cfg = ctx.obj["cfg"]
    payload: dict = {"idea": idea_text}
    if domain:
        payload["domain"] = domain

    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/ideate", json=payload)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    job_id = data.get("job_id") if isinstance(data, dict) else None
    status = data.get("status") if isinstance(data, dict) else None
    click.echo(f"job_id: {job_id}")
    click.echo(f"status: {status}")
    feasibility = data.get("feasibility") if isinstance(data, dict) else None
    if isinstance(feasibility, dict):
        verdict = "feasible" if feasibility.get("feasible") else "blocked"
        confidence = feasibility.get("confidence")
        click.echo(f"feasibility: {verdict}  (confidence={confidence})")
        summary = feasibility.get("summary")
        if summary:
            click.echo(f"  {summary}")

    if status == "awaiting_confirmation" and job_id:
        _hint(f"scaffold confirm {job_id}")


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

CONFIRM_EPILOG = """
\b
Examples:
  scaffold confirm <job_id>
  scaffold confirm <job_id> use bash instead of python
  scaffold confirm <job_id> --json | jq -r '.workflow_summary'

Confirm runs synchronously (HTTP-blocking). For long jobs (often 10–25 min
on CPU), expect the call to take a while; check progress in another shell
with `scaffold jobs status <job_id>`.
"""


@cli.command(
    help="Confirm an ideated job to start research + planning.",
    epilog=CONFIRM_EPILOG,
)
@click.argument("job_id")
@click.argument("feedback", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def confirm(
    ctx: click.Context,
    job_id: str,
    feedback: tuple[str, ...],
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    payload: dict = {"job_id": job_id}
    if feedback:
        payload["feedback"] = " ".join(feedback)

    click.echo(f"Confirming job {job_id} (this may take a few minutes) …")
    try:
        with Client(cfg.api_url, cfg.api_key, timeout=3600.0) as c:
            data = c.post("/ideate/confirm", json=payload)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    status = data.get("status") if isinstance(data, dict) else None
    click.echo(f"status: {status}")
    summary = data.get("workflow_summary") if isinstance(data, dict) else None
    if isinstance(summary, str) and summary:
        click.echo("")
        click.echo(summary)

    _hint(f"scaffold jobs status {job_id}")


# ---------------------------------------------------------------------------
# jobs group
# ---------------------------------------------------------------------------

JOBS_EPILOG = """
\b
Examples:
  scaffold jobs list                         most recent 25
  scaffold jobs list --limit 50              50 most recent
  scaffold jobs list --status failed         filter by status
  scaffold jobs status <job_id>              full state + next steps

Job status reference:
  pending → refining → awaiting_confirmation → researching → planning →
  executing → running → completed | failed | cancelled | blocked
"""


@cli.group(help="List, inspect, and manage orchestrator jobs.", epilog=JOBS_EPILOG)
def jobs() -> None:
    pass


JOBS_LIST_EPILOG = """
\b
Examples:
  scaffold jobs list
  scaffold jobs list --limit 10
  scaffold jobs list --status running
  scaffold jobs list --json | jq '.total'
"""


@jobs.command("list", help="List recent jobs (default limit 25).", epilog=JOBS_LIST_EPILOG)
@click.option("--limit", default=25, type=int, show_default=True)
@click.option("--status", "status_filter", default=None,
              help="Filter by job status (e.g. completed, awaiting_confirmation).")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def jobs_list(
    ctx: click.Context,
    limit: int,
    status_filter: str | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit}
    if status_filter:
        params["status"] = status_filter
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/jobs", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    rows = data.get("jobs", []) if isinstance(data, dict) else []
    if not rows:
        click.echo("(no jobs)")
        _hint('scaffold ideate "your idea here" to start one.')
        return

    click.echo(f"{'job_id':<38} {'status':<24} title")
    click.echo("-" * 80)
    for r in rows:
        jid = str(r.get("id", ""))[:36]
        st = str(r.get("status", ""))[:22]
        title = (r.get("title") or r.get("idea") or "")[:60]
        click.echo(f"{jid:<38} {st:<24} {title}")

    # Highlight a likely "next thing to do" — first job in awaiting_confirmation
    # is the most actionable; otherwise the first non-terminal one.
    NON_TERMINAL = {
        "pending", "refining", "awaiting_confirmation", "researching",
        "planning", "executing", "running", "blocked",
        "assisted_executing", "assisted_running", "assisted_paused",
    }
    awaiting = next((r for r in rows if r.get("status") == "awaiting_confirmation"), None)
    actionable = awaiting or next(
        (r for r in rows if r.get("status") in NON_TERMINAL), None,
    )
    if actionable:
        _hint(f"scaffold jobs status {actionable.get('id')}")


JOBS_STATUS_EPILOG = """
\b
Examples:
  scaffold jobs status <job_id>
  scaffold jobs status <job_id> --json
  scaffold jobs status <job_id> --json | jq '.next_actions'

The `Next steps:` block at the bottom is generated by the orchestrator's
recovery registry — every status maps to a list of valid next-step
commands with concrete job_id and node_key already filled in.
"""


@jobs.command("status", help="Show full status for a single job.", epilog=JOBS_STATUS_EPILOG)
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def jobs_status(ctx: click.Context, job_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get_or_none(f"/exec/status/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if data is None:
        click.secho(f"job {job_id} not found", fg="yellow", err=True)
        _hint("scaffold jobs list to see what's available.")
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    if not isinstance(data, dict):
        click.echo(_json.dumps(data, indent=2))
        return

    click.echo(f"job_id: {data.get('job_id', job_id)}")
    if (title := data.get("job_title")):
        click.echo(f"title:  {title}")
    if (status := data.get("job_status")):
        click.echo(f"status: {status}")

    counts = data.get("counts") or {}
    if counts:
        rendered = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        click.echo(f"nodes:  {data.get('total_nodes', sum(counts.values()))} ({rendered})")

    if (nxt := data.get("next_node")):
        click.echo(
            f"next:   {nxt.get('node_key')} — {nxt.get('title', '')[:60]}"
        )

    if (compiled := data.get("compiled_output")):
        click.echo("\ncompiled_output:")
        click.echo(compiled[:2000] + ("\n[… truncated]" if len(compiled) > 2000 else ""))

    # Audit item 10's structured next-step guidance, rendered as a copy-
    # pasteable bulleted block. Pulled from the response's `next_actions`
    # field (orchestrator's recovery registry, populated server-side).
    _render_next_actions(data)


# ---------------------------------------------------------------------------
# project — convenience wrappers (Sprint U.4)
# Higher-level commands that combine multiple endpoint calls and resolve
# nicknames to UUIDs. Every wrapper PRINTS the underlying raw command it's
# running so the user learns the long form too.
# ---------------------------------------------------------------------------

PROJECT_EPILOG = """
\b
Examples:
  scaffold project new "build a markdown linter"
                                start a project; assigns a friendly nickname
  scaffold project resume <nickname-or-uuid>
                                read job state, dispatch to next valid action
  scaffold project list         show projects with their nicknames

Nicknames are stored locally at ~/.scaffold/nicknames.json (or
$XDG_CONFIG_HOME/scaffold/nicknames.json). They map to job UUIDs;
either form works wherever a job_id is accepted.
"""


@cli.group(help="High-level project commands (with friendly nicknames).", epilog=PROJECT_EPILOG)
def project() -> None:
    pass


PROJECT_NEW_EPILOG = """
\b
Examples:
  scaffold project new "build a markdown linter"
  scaffold project new "make a script that gzips files older than 7 days"

Equivalent to:
  scaffold ideate "<text>"   (then bookkeep the nickname locally)
"""


@project.command("new", help="Submit an idea and assign a friendly nickname.",
                 epilog=PROJECT_NEW_EPILOG)
@click.argument("idea", nargs=-1, required=True)
@click.option("--domain", default=None,
              help="Optional domain hint (eng/llm/rag/spec/prompt).")
@click.option("--dry-run", is_flag=True,
              help="Print what would be sent without calling the orchestrator.")
@click.pass_context
def project_new(
    ctx: click.Context,
    idea: tuple[str, ...],
    domain: str | None,
    dry_run: bool,
) -> None:
    idea_text = " ".join(idea).strip()
    if not idea_text:
        raise click.UsageError("idea text is required")

    cfg = ctx.obj["cfg"]
    payload: dict[str, Any] = {"idea": idea_text}
    if domain:
        payload["domain"] = domain

    # Print what the equivalent direct call would be — keeps the user
    # learning the underlying surface.
    click.secho(f"→ scaffold ideate {' '.join(['--domain', domain]) if domain else ''} \"{idea_text}\"".replace("  ", " "),
                fg="bright_black")

    if dry_run:
        click.echo("(dry-run — no call made)")
        return

    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/ideate", json=payload)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    job_id = data.get("job_id") if isinstance(data, dict) else None
    status = data.get("status") if isinstance(data, dict) else None

    if not job_id:
        click.echo(_json.dumps(data, indent=2))
        click.secho("warning: orchestrator response missing job_id; nickname not stored",
                    fg="yellow", err=True)
        return

    nickname = _project.make_nickname(idea_text, job_id)
    _project.add_nickname(nickname, job_id)

    click.echo(f"job_id:   {job_id}")
    click.echo(f"nickname: {nickname}")
    click.echo(f"status:   {status}")

    feasibility = data.get("feasibility") if isinstance(data, dict) else None
    if isinstance(feasibility, dict):
        verdict = "feasible" if feasibility.get("feasible") else "blocked"
        confidence = feasibility.get("confidence")
        click.echo(f"feasibility: {verdict}  (confidence={confidence})")
        summary = feasibility.get("summary")
        if summary:
            click.echo(f"  {summary}")

    if status == "awaiting_confirmation":
        _hint(f"scaffold project resume {nickname}")


PROJECT_RESUME_EPILOG = """
\b
Examples:
  scaffold project resume markdown-linter-a4f2
  scaffold project resume <uuid>
  scaffold project resume <name> --dry-run   show what it would do, don't run

Resume reads the job's current status, looks up the orchestrator's
recommended next action (the same registry that powers `jobs status`'s
"Next steps:" block), and dispatches it. For `awaiting_confirmation`
that's `confirm`; for `failed`/`blocked` it's `retry`/`skip` (you'll
be prompted to pick); for `completed` it prints the compiled output.
"""


@project.command("resume", help="Read a job's state and run the next valid action.",
                 epilog=PROJECT_RESUME_EPILOG)
@click.argument("name_or_uuid")
@click.option("--dry-run", is_flag=True,
              help="Print what would be done without calling the orchestrator.")
@click.pass_context
def project_resume(ctx: click.Context, name_or_uuid: str, dry_run: bool) -> None:
    cfg = ctx.obj["cfg"]
    job_id = _project.resolve(name_or_uuid)
    if not job_id:
        click.secho(f"unknown nickname or UUID: {name_or_uuid}", fg="red", err=True)
        _hint("scaffold jobs list to see available jobs.")
        sys.exit(1)

    nickname_label = (
        name_or_uuid if not _project.looks_like_uuid(name_or_uuid)
        else (_project.reverse_lookup(job_id) or "(no nickname)")
    )

    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get_or_none(f"/exec/status/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if data is None or not isinstance(data, dict):
        click.secho(f"job {job_id} not found on the orchestrator.", fg="red", err=True)
        sys.exit(1)

    status = data.get("job_status") or "unknown"
    actions = [a for a in (data.get("next_actions") or []) if a.get("action") != "wait"]

    click.echo(f"job:    {nickname_label}  ({job_id})")
    click.echo(f"status: {status}")

    if not actions:
        click.echo("(nothing actionable from this state)")
        if status == "completed" and (compiled := data.get("compiled_output")):
            click.echo("\ncompiled_output:")
            click.echo(compiled[:2000] + ("\n[… truncated]" if len(compiled) > 2000 else ""))
        return

    # Pick the first non-wait action as the default "resume" target.
    # If there are multiple, list them and bail to the user — we don't
    # auto-pick destructive actions like delete.
    primary = actions[0]
    primary_action = primary.get("action")
    primary_command = primary.get("command")

    click.echo("")
    click.secho("would run:", fg="cyan", bold=True)
    if primary_command:
        click.echo(f"  {primary_command}")
    else:
        click.echo(f"  {primary.get('method','GET')} {primary.get('endpoint','')}")
    click.echo(f"  ({primary.get('description','')})")

    if len(actions) > 1:
        click.echo("")
        click.echo("other valid actions:")
        for a in actions[1:]:
            cmd = a.get("command") or f"{a.get('method','GET')} {a.get('endpoint','')}"
            click.echo(f"  • {cmd}   — {a.get('description','')}")

    if dry_run:
        click.echo("\n(dry-run — no call made)")
        return

    # Translate the primary action into an actual SDK call. Today we
    # support the common cases — confirm, retry_node, skip_node — and
    # bail to the user for anything else (delete, view_output, assist
    # subcommands) since those are state-altering or interactive.
    if primary_action == "confirm":
        click.echo(f"\nConfirming {job_id} (this may take a few minutes) …")
        try:
            with Client(cfg.api_url, cfg.api_key, timeout=3600.0) as c:
                resp = c.post("/ideate/confirm", json={"job_id": job_id})
            click.echo(f"status: {resp.get('status') if isinstance(resp, dict) else '?'}")
            _hint(f"scaffold project resume {nickname_label}")
        except CLIError as exc:
            click.secho(str(exc), fg="red", err=True)
            sys.exit(1)
    elif primary_action == "view_output":
        if (compiled := data.get("compiled_output")):
            click.echo("\ncompiled_output:")
            click.echo(compiled)
        else:
            click.echo("(no compiled output stored)")
    else:
        click.echo(
            f"\nThis action ({primary_action}) is destructive or interactive. "
            "Run the command above manually if you intend it."
        )


PROJECT_LIST_EPILOG = """
\b
Examples:
  scaffold project list
  scaffold project list --status awaiting_confirmation

Same data as `scaffold jobs list`, but rows are annotated with their
local nicknames where one is registered.
"""


@project.command("list", help="List jobs with their local nicknames.",
                 epilog=PROJECT_LIST_EPILOG)
@click.option("--limit", default=25, type=int, show_default=True)
@click.option("--status", "status_filter", default=None)
@click.pass_context
def project_list(ctx: click.Context, limit: int, status_filter: str | None) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit}
    if status_filter:
        params["status"] = status_filter
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/jobs", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    rows = data.get("jobs", []) if isinstance(data, dict) else []
    if not rows:
        click.echo("(no jobs)")
        _hint('scaffold project new "your idea here"')
        return

    click.echo(f"{'nickname':<28} {'status':<22} {'job_id':<38} title")
    click.echo("-" * 110)
    for r in rows:
        jid = str(r.get("id", ""))
        nick = _project.reverse_lookup(jid) or "—"
        st = str(r.get("status", ""))[:20]
        title = (r.get("title") or r.get("idea") or "")[:30]
        click.echo(f"{nick[:26]:<28} {st:<22} {jid[:36]:<38} {title}")


# ---------------------------------------------------------------------------
# config — fetch and render orchestrator's loaded Settings (Sprint U.5)
# ---------------------------------------------------------------------------

CONFIG_EPILOG = """
\b
Examples:
  scaffold config show                       every setting (table)
  scaffold config show --filter model        only fields matching "model"
  scaffold config show --non-defaults        only overridden fields
  scaffold config show --json                machine-readable

Sensitive values (anything looking like a key/secret/token/password)
are redacted to (set) / (unset). For the actual values, read .env
directly or `docker exec scaffold-orchestrator env`.
"""


@cli.group(help="Inspect orchestrator configuration.", epilog=CONFIG_EPILOG)
def config() -> None:
    pass


@config.command("show", help="List every setting with current value, default, and description.")
@click.option("--filter", "filter_str", default=None,
              help="Substring filter on field name (case-insensitive).")
@click.option("--non-defaults", is_flag=True,
              help="Only show fields whose runtime value differs from the default.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def config_show(
    ctx: click.Context,
    filter_str: str | None,
    non_defaults: bool,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/config")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    if not isinstance(data, dict) or "fields" not in data:
        click.echo(_json.dumps(data, indent=2))
        return

    fields = data["fields"]
    if filter_str:
        f_lower = filter_str.lower()
        fields = [f for f in fields if f_lower in f["name"].lower()]
    if non_defaults:
        fields = [f for f in fields if not f.get("is_default", False)]

    if not fields:
        click.echo("(no fields match the filter)")
        return

    click.echo(f"{'name':<38} {'value':<30} {'default':<22}")
    click.echo("-" * 92)
    for f in fields:
        name = f["name"][:36]
        value = str(f["value"])[:28]
        default = str(f["default"])[:20]
        marker = " " if f.get("is_default", False) else "*"
        click.echo(f"{marker} {name:<36} {value:<30} {default:<22}")
    click.echo("")
    click.echo(f"Total: {len(fields)} fields  (* = overridden from default)")
    if not non_defaults and not filter_str:
        click.echo(f"Of the {data['count']} settings, {len(data.get('redacted', []))} are redacted (keys/secrets/tokens).")


# ---------------------------------------------------------------------------
# whatnow — global "what should I do next" view (Sprint U.6)
# ---------------------------------------------------------------------------

WHATNOW_EPILOG = """
\b
Examples:
  scaffold whatnow                       every job that needs attention
  scaffold whatnow --limit 5             cap at 5 most-recent
  scaffold whatnow --json                machine-readable

Lists every job in a non-terminal status (anything that's not
completed/cancelled) and prints each with its recommended next action
from the local STATUS_EXPLAIN registry. Run `scaffold project resume
<nickname-or-uuid>` for the actual server-rendered next-step (with
concrete job_id and node_key already substituted).
"""

# Statuses where the user has nothing useful to do — filtered out of whatnow.
_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})


@cli.command(help="Show every job that needs attention plus its recommended next step.",
             epilog=WHATNOW_EPILOG)
@click.option("--limit", default=10, type=int, show_default=True,
              help="Cap on the number of non-terminal jobs to show.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw structured response.")
@click.pass_context
def whatnow(ctx: click.Context, limit: int, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/jobs", params={"limit": 50})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    rows = data.get("jobs", []) if isinstance(data, dict) else []
    actionable = [r for r in rows if r.get("status") not in _TERMINAL_STATUSES][:limit]

    if as_json:
        # Enrich each row with the local recommended action lookup.
        out = []
        for r in actionable:
            status = r.get("status", "")
            info = _project.STATUS_EXPLAIN.get(status, {})
            out.append({
                "job_id": r.get("id"),
                "title": r.get("title"),
                "status": status,
                "headline": info.get("headline", ""),
                "valid_actions": info.get("valid_actions", []),
                "nickname": _project.reverse_lookup(r.get("id") or ""),
            })
        click.echo(_json.dumps(out, indent=2))
        return

    if not actionable:
        click.echo("Nothing needs your attention right now.")
        click.echo("")
        click.echo("All jobs are either completed or cancelled.")
        _hint('scaffold project new "your idea here"  to start something.')
        return

    click.secho(f"{len(actionable)} job(s) need attention:", bold=True)
    click.echo("")

    for r in actionable:
        jid = r.get("id", "")
        nick = _project.reverse_lookup(jid) or "—"
        title = (r.get("title") or "")[:50]
        status = r.get("status", "?")
        info = _project.STATUS_EXPLAIN.get(status, {})
        headline = info.get("headline", "(unknown status)")
        actions = info.get("valid_actions", [])

        # Pick the most "actionable" valid action — skip wait/view_output
        # which are passive — to prioritize ones the user actually needs.
        action_priority = ["confirm", "next_step", "submit", "retry_node",
                           "skip_node", "resume", "delete", "abandon"]
        primary = next(
            (a for a in action_priority if a in actions),
            actions[0] if actions else None,
        )

        click.secho(f"  {nick}", fg="cyan", nl=False)
        if nick == "—":
            click.echo(f" ({jid[:8]}…)", nl=True)
        else:
            click.echo(f"  ({jid[:8]}…)")
        click.echo(f"    title:   {title}")
        click.secho(f"    status:  {status}", fg="yellow")
        click.echo(f"    why:     {headline}")
        if primary:
            target = nick if nick != "—" else jid
            cmd_map = {
                "confirm": f"scaffold project resume {target}",
                "retry_node": f"scaffold project resume {target}  (registry will pick the failed node)",
                "skip_node":  f"scaffold project resume {target}  (registry will pick the blocked node)",
                "next_step":  f"scaffold project resume {target}",
                "submit":     f"scaffold project resume {target}",
                "resume":     f"scaffold project resume {target}",
                "delete":     f"scaffold jobs delete {jid}",
                "abandon":    f"# (manual) /assist done {jid}",
            }
            click.secho(f"    next:    {cmd_map.get(primary, primary)}", fg="green")
        click.echo("")


# ---------------------------------------------------------------------------
# explain — local lookup, no network call
# ---------------------------------------------------------------------------

EXPLAIN_EPILOG = """
\b
Examples:
  scaffold explain awaiting_confirmation
  scaffold explain failed
  scaffold explain                       list every status

Plain-English description + valid next-step actions for a given job
status. Local lookup; no orchestrator call required.
"""


@cli.command(help="Explain what a job status means and what you can do from it.",
             epilog=EXPLAIN_EPILOG)
@click.argument("status", required=False)
def explain(status: str | None) -> None:
    if not status:
        click.echo("Known job statuses:")
        for name in _project.STATUS_EXPLAIN:
            entry = _project.STATUS_EXPLAIN[name]
            click.secho(f"  {name:<26}", fg="cyan", nl=False)
            click.echo(entry["headline"])
        click.echo("")
        click.echo("Run `scaffold explain <status>` for full details.")
        return

    info = _project.STATUS_EXPLAIN.get(status)
    if info is None:
        click.secho(f"unknown status: {status}", fg="red", err=True)
        click.echo("Known statuses: " + ", ".join(_project.STATUS_EXPLAIN.keys()))
        sys.exit(1)

    click.secho(f"{status}", fg="cyan", bold=True)
    click.echo(f"  {info['headline']}")
    click.echo("")
    click.echo("What happens next:")
    click.echo(f"  {info['what_happens']}")
    click.echo("")
    click.echo("Valid actions from this state:")
    for a in info["valid_actions"]:
        click.echo(f"  • {a}")


# ---------------------------------------------------------------------------
# Sprint U.7 — CLI parity sweep: extend `jobs`, add `research`, `schedule`,
# `rag`, `optimize`, `skip`, `model`. Closes the gap with the OWUI surface
# so anything reachable in chat is also reachable from the terminal.
# ---------------------------------------------------------------------------

# ---- jobs find / rename / delete (extending the existing `jobs` group) ----

JOBS_FIND_EPILOG = """
\b
Examples:
  scaffold jobs find linter              jobs whose title contains "linter"
  scaffold jobs find "build a"           multi-word search (use quotes)
  scaffold jobs find --json kube         machine-readable
"""


@jobs.command("find", help="Search jobs by title substring.", epilog=JOBS_FIND_EPILOG)
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=25, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def jobs_find(
    ctx: click.Context,
    query: tuple[str, ...],
    limit: int,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    q = " ".join(query).strip()
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/jobs", params={"q": q, "limit": limit})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    rows = data.get("jobs", []) if isinstance(data, dict) else []
    total = data.get("total", 0)
    if not rows:
        click.echo(f"(no jobs match '{q}')")
        return

    click.echo(f"{len(rows)} of {total} matching '{q}':")
    click.echo(f"{'job_id':<38} {'status':<24} title")
    click.echo("-" * 80)
    for r in rows:
        jid = str(r.get("id", ""))[:36]
        st = str(r.get("status", ""))[:22]
        title = (r.get("title") or "")[:60]
        click.echo(f"{jid:<38} {st:<24} {title}")


JOBS_RENAME_EPILOG = """
\b
Examples:
  scaffold jobs rename <job_id> "markdown linter — final"
  scaffold jobs rename <job_id> renamed via CLI
"""


@jobs.command("rename", help="Rename a job (set its title).", epilog=JOBS_RENAME_EPILOG)
@click.argument("job_id")
@click.argument("title", nargs=-1, required=True)
@click.pass_context
def jobs_rename(ctx: click.Context, job_id: str, title: tuple[str, ...]) -> None:
    cfg = ctx.obj["cfg"]
    new_title = " ".join(title).strip()
    if not new_title:
        raise click.UsageError("title is required")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.patch(f"/jobs/{job_id}", json={"title": new_title})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"renamed {data.get('id', job_id)[:8]}: ", nl=False, fg="green")
    click.echo(data.get("title", new_title))


JOBS_DELETE_EPILOG = """
\b
Examples:
  scaffold jobs delete <job_id>          confirmation prompt first
  scaffold jobs delete <job_id> --yes    skip confirmation (scripts/CI)

Hard-delete is final — cascades to dag_nodes, execution_logs, artifacts,
error_logs. Knowledge-base entries are NOT removed.
"""


@jobs.command("delete", help="Hard-delete a job (cascades to its DAG + logs).",
              epilog=JOBS_DELETE_EPILOG)
@click.argument("job_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def jobs_delete(ctx: click.Context, job_id: str, yes: bool) -> None:
    cfg = ctx.obj["cfg"]
    if not yes:
        click.confirm(f"Delete job {job_id}? (cascades to DAG + logs)", abort=True)
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.delete(f"/jobs/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"deleted {job_id[:8]}", fg="green")


# ---- skip (top-level; needs both job_id and node_key per docs) ----

SKIP_EPILOG = """
\b
Examples:
  scaffold skip <job_id> <node_key>      mark a stuck node as skipped
  scaffold skip <job_id> verify_design   downstream nodes proceed

`/results <job_id>` for a failed/blocked job pre-fills both arguments.
"""


@cli.command(help="Mark a DAG node as skipped to unblock downstream execution.",
             epilog=SKIP_EPILOG)
@click.argument("job_id")
@click.argument("node_key")
@click.pass_context
def skip(ctx: click.Context, job_id: str, node_key: str) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/skip", json={"job_id": job_id, "node_key": node_key})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"skipped {node_key} on {job_id[:8]}", fg="green")
    if isinstance(data, dict) and (status := data.get("status")):
        click.echo(f"  job status now: {status}")
    _hint(f"scaffold jobs status {job_id}")


# ---- research group ------------------------------------------------------

RESEARCH_EPILOG = """
\b
Examples:
  scaffold research topic "kubernetes pod lifecycle" --depth medium
  scaffold research url https://en.wikipedia.org/wiki/Embedding
  scaffold research github anthropics/anthropic-sdk-python
  scaffold research openapi https://petstore3.swagger.io/api/v3/openapi.json
  scaffold research list
  scaffold research find "kubernetes"

Subcommands:
  topic / url / github / openapi   start an ingest run (streams progress)
  list / find / rename / delete    manage saved sessions
"""


@cli.group(help="Run autonomous research or manage saved sessions.",
           epilog=RESEARCH_EPILOG)
def research() -> None:
    pass


def _stream_research(api_url: str, api_key: str | None, payload: dict, path: str = "/research") -> None:
    """POST to a streaming research endpoint and print event names + brief data."""
    import asyncio
    from scaffold_client import AsyncClient

    async def _run() -> None:
        async with AsyncClient(api_url, api_key=api_key, timeout=3600.0) as c:
            try:
                if path == "/research":
                    stream = c.aiter_research(**payload)
                else:
                    stream = c._aiter_sse(path, json=payload)  # generic fallback
                async for evt in stream:
                    name = evt.get("event", "?")
                    data = evt.get("data", {})
                    if isinstance(data, dict):
                        first_key = next(iter(data), None)
                        snippet = ""
                        if first_key:
                            v = data[first_key]
                            snippet = f"{first_key}={str(v)[:60]}"
                        click.secho(f"[{name}] ", fg="cyan", nl=False)
                        click.echo(snippet)
                    else:
                        click.secho(f"[{name}] ", fg="cyan", nl=False)
                        click.echo(str(data)[:80])
                    if name in ("convergence", "complete", "done"):
                        break
            except Exception as exc:
                click.secho(f"stream error: {exc}", fg="red", err=True)
                raise

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.secho("\ninterrupted (orchestrator will finalize as cancelled)", fg="yellow")
        sys.exit(130)


@research.command("topic", help="Autonomous research on a topic — search → distill → ingest.")
@click.argument("topic_text", nargs=-1, required=True)
@click.option("--depth", type=click.Choice(["shallow", "medium", "deep"]),
              default="medium", show_default=True)
@click.option("--domain", default=None, help="Optional Milvus partition hint.")
@click.pass_context
def research_topic(
    ctx: click.Context,
    topic_text: tuple[str, ...],
    depth: str,
    domain: str | None,
) -> None:
    cfg = ctx.obj["cfg"]
    topic = " ".join(topic_text).strip()
    if not topic:
        raise click.UsageError("topic is required")
    payload = {"topic": topic, "depth": depth}
    if domain:
        payload["domain"] = domain
    click.echo(f"researching: {topic}  (depth={depth})")
    _stream_research(cfg.api_url, cfg.api_key, payload)


@research.command("url", help="Ingest a single web page (no search step).")
@click.argument("url")
@click.pass_context
def research_url(ctx: click.Context, url: str) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"ingesting: {url}")
    _stream_research(cfg.api_url, cfg.api_key, {"topic": url, "depth": "shallow"})


@research.command("github", help="Ingest a GitHub repo's docs (README + docs/**).")
@click.argument("owner_repo")
@click.pass_context
def research_github(ctx: click.Context, owner_repo: str) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"ingesting github:{owner_repo}")
    _stream_research(cfg.api_url, cfg.api_key, {"topic": f"github:{owner_repo}", "depth": "shallow"})


@research.command("openapi", help="Ingest an OpenAPI/Swagger spec (one entry per endpoint).")
@click.argument("spec_url")
@click.pass_context
def research_openapi(ctx: click.Context, spec_url: str) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"ingesting openapi:{spec_url}")
    _stream_research(cfg.api_url, cfg.api_key, {"topic": f"openapi:{spec_url}", "depth": "shallow"})


@research.command("list", help="List recent research sessions.")
@click.option("--limit", default=25, type=int, show_default=True)
@click.option("--status", "status_filter", default=None,
              help="Filter by session status (running, completed, failed, ...).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def research_list(
    ctx: click.Context,
    limit: int,
    status_filter: str | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit}
    if status_filter:
        params["status"] = status_filter
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/research/sessions", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    rows = data.get("sessions", []) if isinstance(data, dict) else []
    total = data.get("total", 0)
    if not rows:
        click.echo("(no sessions)")
        return
    click.echo(f"{len(rows)} of {total}:")
    click.echo(f"{'session_id':<38} {'status':<14} {'depth':<8} {'entries':>8}  topic")
    click.echo("-" * 100)
    for r in rows:
        sid = str(r.get("id", ""))[:36]
        st = str(r.get("status", ""))[:12]
        dp = str(r.get("depth", ""))[:6]
        ent = r.get("total_entries_ingested", 0)
        topic = (r.get("topic") or "")[:50]
        click.echo(f"{sid:<38} {st:<14} {dp:<8} {ent:>8}  {topic}")


@research.command("find", help="Search research sessions by topic substring.")
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=25, type=int, show_default=True)
@click.pass_context
def research_find(ctx: click.Context, query: tuple[str, ...], limit: int) -> None:
    cfg = ctx.obj["cfg"]
    q = " ".join(query).strip()
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/research/sessions", params={"q": q, "limit": limit})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    rows = data.get("sessions", []) if isinstance(data, dict) else []
    total = data.get("total", 0)
    if not rows:
        click.echo(f"(no sessions match '{q}')")
        return
    click.echo(f"{len(rows)} of {total} matching '{q}':")
    for r in rows:
        click.echo(f"  {str(r.get('id',''))[:8]}  {r.get('status',''):<14}  {(r.get('topic') or '')[:60]}")


@research.command("rename", help="Rename a research session (set its topic).")
@click.argument("session_id")
@click.argument("topic", nargs=-1, required=True)
@click.pass_context
def research_rename(ctx: click.Context, session_id: str, topic: tuple[str, ...]) -> None:
    cfg = ctx.obj["cfg"]
    new_topic = " ".join(topic).strip()
    if not new_topic:
        raise click.UsageError("topic is required")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.patch(f"/research/sessions/{session_id}", json={"topic": new_topic})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"renamed {data.get('id', session_id)[:8]}: ", nl=False, fg="green")
    click.echo(data.get("topic", new_topic))


@research.command("delete", help="Hard-delete a research session (KB entries are kept).")
@click.argument("session_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def research_delete(ctx: click.Context, session_id: str, yes: bool) -> None:
    cfg = ctx.obj["cfg"]
    if not yes:
        click.confirm(f"Delete research session {session_id}? "
                      "(KB entries already in Milvus stay.)", abort=True)
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.delete(f"/research/sessions/{session_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"deleted research session {session_id[:8]}", fg="green")


# ---- schedule group ------------------------------------------------------

SCHEDULE_EPILOG = """
\b
Examples:
  scaffold schedule list
  scaffold schedule add "0 9 * * 1" "kubernetes news" --depth medium
  scaffold schedule add "0 9 * * 1" "ny news" --tz America/New_York
  scaffold schedule delete 3

Cron format: minute hour day-of-month month day-of-week.
"""


@cli.group(help="Manage recurring research schedules.", epilog=SCHEDULE_EPILOG)
def schedule() -> None:
    pass


@schedule.command("list", help="List every saved schedule.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def schedule_list(ctx: click.Context, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/schedule")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    rows = data.get("schedules", []) if isinstance(data, dict) else []
    if not rows:
        click.echo("(no schedules)")
        _hint('scaffold schedule add "0 9 * * 1" "your topic"')
        return
    click.echo(f"{'id':<5} {'cron':<16} {'depth':<8} {'tz':<22} {'runs':>5}  topic")
    click.echo("-" * 90)
    for r in rows:
        click.echo(f"{r.get('id',''):<5} "
                   f"{r.get('cron_expression',''):<16} "
                   f"{r.get('depth',''):<8} "
                   f"{r.get('timezone',''):<22} "
                   f"{r.get('run_count',0):>5}  {(r.get('topic') or '')[:40]}")


@schedule.command("add", help="Create a new recurring research schedule.")
@click.argument("cron_expression")
@click.argument("topic", nargs=-1, required=True)
@click.option("--depth", type=click.Choice(["shallow", "medium", "deep"]),
              default="medium", show_default=True)
@click.option("--tz", "timezone", default="UTC", show_default=True,
              help="IANA timezone (e.g. America/New_York).")
@click.pass_context
def schedule_add(
    ctx: click.Context,
    cron_expression: str,
    topic: tuple[str, ...],
    depth: str,
    timezone: str,
) -> None:
    cfg = ctx.obj["cfg"]
    topic_text = " ".join(topic).strip()
    if not topic_text:
        raise click.UsageError("topic is required")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/schedule", json={
                "topic": topic_text,
                "cron_expression": cron_expression,
                "depth": depth,
                "timezone": timezone,
            })
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"scheduled #{data.get('id')}: ", fg="green", nl=False)
    click.echo(data.get("topic", topic_text))
    click.echo(f"  cron: {data.get('cron_expression')} ({data.get('timezone','UTC')})  depth: {data.get('depth')}")
    if (next_run := data.get("next_run_at")):
        click.echo(f"  next run: {next_run}")


@schedule.command("delete", help="Remove a saved schedule.")
@click.argument("schedule_id", type=int)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def schedule_delete(ctx: click.Context, schedule_id: int, yes: bool) -> None:
    cfg = ctx.obj["cfg"]
    if not yes:
        click.confirm(f"Delete schedule #{schedule_id}?", abort=True)
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.delete(f"/schedule/{schedule_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"deleted schedule #{schedule_id}", fg="green")


# ---- rag (knowledge-base query) -----------------------------------------

RAG_EPILOG = """
\b
Examples:
  scaffold rag "kubernetes pod lifecycle"
  scaffold rag --top-k 10 "embedding similarity"
  scaffold rag --json "milvus index" | jq '.results[0]'
"""


@cli.command(help="Query the Milvus knowledge base.", epilog=RAG_EPILOG)
@click.argument("query", nargs=-1, required=True)
@click.option("--top-k", type=int, default=5, show_default=True)
@click.option("--domain", default=None, help="Restrict to one Milvus partition.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rag(
    ctx: click.Context,
    query: tuple[str, ...],
    top_k: int,
    domain: str | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    q = " ".join(query).strip()
    payload: dict = {"query": q, "top_k": top_k}
    if domain:
        payload["domain"] = domain
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/rag", json=payload)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        click.echo("(no results)")
        return
    for i, r in enumerate(results, 1):
        score = r.get("score", 0.0)
        click.secho(f"#{i}  ", fg="cyan", nl=False)
        click.echo(f"score={score:.3f}  domain={r.get('domain','?')}")
        text_preview = (r.get("text") or "")[:200].replace("\n", " ")
        click.echo(f"     {text_preview}…")


# ---- optimize -----------------------------------------------------------

OPTIMIZE_EPILOG = """
\b
Examples:
  scaffold optimize "Please could you maybe write a function that..."
  scaffold optimize --skip-verify "rewrite this prompt to be terse"
"""


@cli.command(help="Optimize a prompt — strip filler, rewrite, verify.",
             epilog=OPTIMIZE_EPILOG)
@click.argument("prompt", nargs=-1, required=True)
@click.option("--skip-verify", is_flag=True,
              help="Skip the LLM verification step (faster, looser).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize(
    ctx: click.Context,
    prompt: tuple[str, ...],
    skip_verify: bool,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    p = " ".join(prompt).strip()
    if not p:
        raise click.UsageError("prompt is required")
    try:
        with Client(cfg.api_url, cfg.api_key, timeout=120.0) as c:
            data = c.post("/optimize", json={"prompt": p, "skip_verify": skip_verify})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if isinstance(data, dict):
        click.secho("optimized:", fg="green", bold=True)
        click.echo(data.get("optimized_prompt", ""))
        if (score := data.get("clarity_score")) is not None:
            click.echo("")
            click.echo(f"clarity score: {score}")
        if (verified := data.get("intent_verified")) is not None:
            click.echo(f"intent verified: {verified}")


# ---- model group --------------------------------------------------------

MODEL_EPILOG = """
\b
Examples:
  scaffold model list                    current per-role model assignments
  scaffold model available               models loaded on Ollama

Per-role overrides are session-only when set in OWUI valves. To persist,
edit MODEL_<ROLE> in .env and restart. (`make init` for the wizard.)
"""


@cli.group(help="Inspect model role assignments and Ollama availability.",
           epilog=MODEL_EPILOG)
def model() -> None:
    pass


@model.command("list", help="Show current per-role model assignments (from /config).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def model_list(ctx: click.Context, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/config")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    fields = data.get("fields", []) if isinstance(data, dict) else []
    rows = [f for f in fields if f["name"].startswith("model_")]

    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no model_* settings exposed by /config)")
        return
    click.echo(f"{'role':<32} {'value':<48} {'default?':<8}")
    click.echo("-" * 92)
    for r in rows:
        name = r["name"][:30]
        val = str(r["value"])[:46]
        is_default = "yes" if r.get("is_default") else "no"
        click.echo(f"{name:<32} {val:<48} {is_default:<8}")


@model.command("available", help="List models currently loaded on Ollama (via /health).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def model_available(ctx: click.Context, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/health")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    ollama = (data or {}).get("checks", {}).get("ollama", {})
    models = ollama.get("models_loaded", [])
    if as_json:
        click.echo(_json.dumps(models, indent=2))
        return
    if not models:
        click.echo("(no models loaded — Ollama may be down)")
        return
    click.echo(f"{len(models)} models loaded on Ollama:")
    for m in sorted(models):
        click.echo(f"  {m}")


if __name__ == "__main__":
    cli()
