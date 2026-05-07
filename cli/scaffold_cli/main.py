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


if __name__ == "__main__":
    cli()
