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
from scaffold_cli.config import provenance_security_note, resolve_config
from scaffold_cli import project as _project


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------

def _render_next_actions(data: dict) -> None:
    """Render the orchestrator's ``next_actions`` field (audit item 10)
    as a colored bulleted block, written to stdout.

    §17.195 — filter + per-action field selection delegated to the SDK's
    shared helpers (``scaffold_client.next_actions``). The CLI retains
    its own loop so it can color the clickable token with ``click.secho``
    — the SDK's ``format_block`` is markdown/plain text-only.
    """
    from scaffold_client.next_actions import action_clickable, filter_renderable
    renderable = filter_renderable(data.get("next_actions") or [])
    if not renderable:
        return
    click.echo("")
    click.secho("Next steps:", fg="cyan", bold=True)
    for a in renderable:
        clickable, desc = action_clickable(a)
        if clickable is not None:
            click.echo("  • ", nl=False)
            click.secho(clickable, fg="cyan", nl=False)
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
    note = provenance_security_note(cfg)
    if note:
        click.secho(f"  ⚠ {note}", fg="yellow", err=True)


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
    note = provenance_security_note(cfg)
    if note:
        click.secho(f"⚠ {note}", fg="yellow", err=True)
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
  scaffold confirm <job_id>                     Phase 2 only (curl-equivalent)
  scaffold confirm <job_id> use bash            with feedback
  scaffold confirm <job_id> --chain             Phase 2 → DAG → execute_all
  scaffold confirm <job_id> --json              raw JSON of Phase 2 result

Without `--chain`, this matches the orchestrator's curl behavior: Phase 2
runs and stops. Subsequent steps require `scaffold dag` + `scaffold exec`,
or another tool. With `--chain`, the full OWUI-style auto-chain runs
(Phase 2 → /dag → /execute/all SSE), often 30+ minutes on CPU.
"""


@cli.command(
    help="Confirm an ideated job to start research + planning.",
    epilog=CONFIRM_EPILOG,
)
@click.argument("job_id")
@click.argument("feedback", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.option("--chain", is_flag=True,
              help="After Phase 2, also generate the DAG and execute it (SSE-streamed). "
                   "Mirrors the OWUI /confirm auto-chain.")
@click.pass_context
def confirm(
    ctx: click.Context,
    job_id: str,
    feedback: tuple[str, ...],
    as_json: bool,
    chain: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    payload: dict = {"job_id": job_id}
    if feedback:
        payload["feedback"] = " ".join(feedback)

    if chain and as_json:
        raise click.UsageError(
            "--json and --chain are incompatible: chain streams progress to "
            "stdout. Use one or the other."
        )

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

    if chain:
        _confirm_chain_continue(cfg, job_id)
        return

    _hint(f"scaffold jobs status {job_id}")


def _confirm_chain_continue(cfg, job_id: str) -> None:
    """Continue the OWUI-style auto-chain after Phase 2 returned.

    Phase 2 already left the job in `planning`. Generate the DAG, then
    stream `/execute/all` to completion. Each step prints a banner so
    a user watching tail-f-style knows where they are.
    """
    # Phase 3 — generate DAG.
    click.echo("")
    click.secho("→ generating DAG …", fg="cyan", bold=True)
    try:
        with Client(cfg.api_url, cfg.api_key, timeout=1800.0) as c:
            dag_data = c.post("/dag", json={"job_id": job_id})
    except CLIError as exc:
        click.secho(f"DAG generation failed: {exc}", fg="red", err=True)
        sys.exit(1)
    if isinstance(dag_data, dict):
        n_nodes = dag_data.get("node_count") or len(dag_data.get("nodes") or [])
        if n_nodes:
            click.echo(f"  → {n_nodes} nodes generated")

    # Phase 4 — stream execute_all.
    click.echo("")
    click.secho("→ executing DAG …", fg="cyan", bold=True)
    import asyncio
    from scaffold_client import AsyncClient, ScaffoldError

    final_status: str | None = None
    last_node: str | None = None

    async def _run() -> None:
        nonlocal final_status, last_node
        async with AsyncClient(cfg.api_url, api_key=cfg.api_key, timeout=3600.0) as ac:
            try:
                async for evt in ac.aiter_execute_all(job_id):
                    name = evt.get("event", "?")
                    data = evt.get("data") or {}
                    if isinstance(data, dict):
                        node_key = data.get("node_key")
                        if node_key:
                            last_node = node_key
                        snippet_keys = ("node_key", "status", "reason", "error")
                        snippet = " ".join(
                            f"{k}={data[k]}" for k in snippet_keys if k in data
                        )
                    else:
                        snippet = str(data)[:60]
                    click.secho(f"[{name}] ", fg="cyan", nl=False)
                    click.echo(snippet)
                    if name in ("all_complete", "complete", "done"):
                        final_status = "completed"
                        break
                    if name in ("failed", "all_failed", "blocked"):
                        final_status = name
                        break
            except ScaffoldError as exc:
                click.secho(f"execute_all failed: {exc}", fg="red", err=True)
                raise

    try:
        asyncio.run(_run())
    except ScaffoldError:
        sys.exit(1)
    except KeyboardInterrupt:
        click.secho("\ninterrupted (orchestrator will finalize as cancelled)",
                    fg="yellow")
        sys.exit(130)

    click.echo("")
    if final_status == "completed":
        click.secho(f"✓ chain complete: {job_id}", fg="green", bold=True)
        _hint(f"scaffold jobs status {job_id}    # see compiled_output")
    else:
        click.secho(
            f"chain ended with status={final_status or 'unknown'}"
            + (f", last node={last_node}" if last_node else ""),
            fg="yellow",
        )
        _hint(f"scaffold logs {job_id}    # inspect node-level state")


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
@click.option("--synthesized/--no-synthesized", "synthesized_filter",
              default=None,
              help="Filter to jobs whose compiled output was (or was not) "
                   "LLM-synthesized via the W.7 pass. Omit to see all. (X.9)")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def jobs_list(
    ctx: click.Context,
    limit: int,
    status_filter: str | None,
    synthesized_filter: bool | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit}
    if status_filter:
        params["status"] = status_filter
    if synthesized_filter is not None:
        # X.9 — orchestrator accepts ?synthesized=true|false; pass the
        # bool directly (httpx serializes to lowercase JSON-style true/false).
        params["synthesized"] = "true" if synthesized_filter else "false"
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
@click.option("--costs", "show_costs", is_flag=True,
              help="Append cost + latency rollup (totals + per-(provider, model) breakdown).")
@click.pass_context
def jobs_status(ctx: click.Context, job_id: str, as_json: bool, show_costs: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get_or_none(f"/exec/status/{job_id}")
            costs_data = c.get_or_none(f"/jobs/{job_id}/costs") if show_costs else None
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if data is None:
        click.secho(f"job {job_id} not found", fg="yellow", err=True)
        _hint("scaffold jobs list to see what's available.")
        sys.exit(1)

    if as_json:
        # J.3.c — when --costs is set, embed the costs payload alongside
        # the status under a top-level `costs` key. Existing /exec/status
        # already returns a `costs` totals block (J.3.b); --costs adds
        # the breakdown. Keep both keys when present so consumers that
        # care about the breakdown see it without a separate request.
        if show_costs and costs_data is not None:
            data = dict(data)
            data["costs_breakdown"] = costs_data
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

    # J.3.c — cost rollup when --costs is set. Falls back to the totals
    # block that /exec/status already includes if the breakdown call
    # failed (e.g. transient orchestrator issue) so the operator still
    # sees something useful.
    if show_costs:
        _render_cost_rollup(data, costs_data)


def _render_cost_rollup(status_data: dict, costs_data: dict | None) -> None:
    """J.3.c — print cost totals + per-(provider, model) breakdown.

    Reads totals from the costs endpoint when available; falls back to
    the lightweight totals block on /exec/status (always present
    post-J.3.b) so the operator still gets numbers when the breakdown
    request failed.
    """
    totals = (
        costs_data
        if isinstance(costs_data, dict) and "total_cost_usd" in costs_data
        else (status_data.get("costs") or {})
    )
    if not totals:
        return

    click.echo()
    click.echo("costs:")
    # §17.289 — `data_source` was added in §17.284 so consumers can
    # distinguish "no calls yet" from "the rollup query failed and the
    # zeros are a fallback". Surface the error case here as a single-line
    # warning above the numbers — pre-§17.289 a busy job with a telemetry
    # outage rendered identically to a fresh job with no LLM calls.
    if totals.get("data_source") == "error":
        click.echo("  ⚠ telemetry query failed; figures may be stale or incomplete")
    cost = float(totals.get("total_cost_usd") or 0.0)
    click.echo(f"  total:    ${cost:.4f}")
    click.echo(f"  calls:    {totals.get('call_count', 0)}")
    click.echo(
        f"  tokens:   prompt={totals.get('total_prompt_tokens', 0)} "
        f"completion={totals.get('total_completion_tokens', 0)}"
    )
    latency_ms = int(totals.get("total_latency_ms") or 0)
    click.echo(f"  latency:  {latency_ms} ms ({latency_ms/1000:.1f}s)")

    breakdown = (costs_data or {}).get("by_provider") or []
    if breakdown:
        click.echo()
        click.echo("  by provider/model:")
        # Column widths: provider 12, model 24, calls 5, cost 10
        click.echo(
            f"    {'provider':<12} {'model':<24} "
            f"{'calls':>5}  {'cost':>10}  {'latency':>10}"
        )
        for row in breakdown:
            provider = (row.get("provider") or "")[:12]
            model = (row.get("model") or "")[:24]
            calls = row.get("calls", 0)
            row_cost = float(row.get("cost_usd") or 0.0)
            row_latency = int(row.get("latency_ms") or 0)
            click.echo(
                f"    {provider:<12} {model:<24} "
                f"{calls:>5}  ${row_cost:>9.4f}  {row_latency:>7} ms"
            )


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


@jobs.command(
    "synthesis",
    help="Set the per-job W.7 synthesis override (--on / --off / --auto).",
)
@click.argument("job_id")
@click.option("--on", "decision", flag_value="on",
              help="Force synthesis ON for this job.")
@click.option("--off", "decision", flag_value="off",
              help="Force synthesis OFF for this job.")
@click.option("--auto", "decision", flag_value="auto",
              help="Clear the override (job inherits the global setting).")
@click.pass_context
def jobs_synthesis(ctx: click.Context, job_id: str, decision: str | None) -> None:
    """X.6 — flip a job's per-call synthesis override.

    Maps to ``PATCH /jobs/{id}/synthesis``. The orchestrator stores the
    bool/null on ``jobs.compile_synthesis_override``; the next compile
    pass for this job consults it before falling back to the global
    ``settings.compile_synthesis_enabled`` flag.
    """
    if decision is None:
        raise click.UsageError(
            "exactly one of --on, --off, or --auto is required"
        )
    override: bool | None
    if decision == "on":
        override = True
    elif decision == "off":
        override = False
    else:  # "auto"
        override = None
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.patch(f"/jobs/{job_id}/synthesis", json={"override": override})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    rendered = (
        "auto (inherits global)" if data.get("override") is None
        else ("on" if data.get("override") else "off")
    )
    click.secho(
        f"synthesis override for {data.get('job_id', job_id)[:8]}: ",
        nl=False, fg="green",
    )
    click.echo(rendered)


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


@jobs.command("cleanup", help="Sweep stale jobs (calls /jobs/cleanup).")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def jobs_cleanup(ctx: click.Context, yes: bool, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    if not yes:
        click.confirm(
            "Run stale-job reaper now? (resets orphans, cancels long-idle jobs)",
            abort=True,
        )
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/jobs/cleanup", json={})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if isinstance(data, dict):
        counts = {k: v for k, v in data.items() if isinstance(v, int)}
        if counts:
            click.secho("reaped:", fg="green", bold=True)
            for k, v in sorted(counts.items()):
                click.echo(f"  {k}: {v}")
        else:
            click.echo(_json.dumps(data, indent=2))


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


@research.command("reply", help="Resume a paused autonomous research session.")
@click.argument("session_id")
@click.argument("message", nargs=-1, required=True)
@click.pass_context
def research_reply(
    ctx: click.Context, session_id: str, message: tuple[str, ...],
) -> None:
    """Stream /research/reply — sends a follow-up message to a session
    that paused for clarification."""
    import asyncio
    from scaffold_client import AsyncClient, ScaffoldError

    cfg = ctx.obj["cfg"]
    reply_text = " ".join(message).strip()
    if not reply_text:
        raise click.UsageError("reply message is required")
    click.echo(f"replying to {session_id}: {reply_text[:80]}")

    async def _run() -> None:
        async with AsyncClient(cfg.api_url, api_key=cfg.api_key, timeout=3600.0) as c:
            try:
                async for evt in c.aiter_research_reply(session_id, reply_text):
                    name = evt.get("event", "?")
                    data = evt.get("data", {})
                    snippet = ""
                    if isinstance(data, dict):
                        first_key = next(iter(data), None)
                        if first_key:
                            snippet = f"{first_key}={str(data[first_key])[:60]}"
                    click.secho(f"[{name}] ", fg="cyan", nl=False)
                    click.echo(snippet or str(data)[:80])
                    if name in ("convergence", "complete", "done"):
                        break
            except ScaffoldError as exc:
                click.secho(f"reply failed: {exc}", fg="red", err=True)
                raise

    try:
        asyncio.run(_run())
    except ScaffoldError:
        sys.exit(1)
    except KeyboardInterrupt:
        click.secho("\ninterrupted", fg="yellow")
        sys.exit(130)


@research.command("pdf", help="Ingest a PDF document (multipart upload, streamed).")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--extractor", type=click.Choice(["auto", "pypdf", "plumber"]),
              default="auto", show_default=True)
@click.option("--domain", default=None, help="Optional Milvus partition hint.")
@click.pass_context
def research_pdf(
    ctx: click.Context, path: str, extractor: str, domain: str | None,
) -> None:
    """Streams /research/pdf — multipart upload + ingestion events."""
    import asyncio
    from scaffold_client import AsyncClient, ScaffoldError

    cfg = ctx.obj["cfg"]
    click.echo(f"ingesting pdf: {path}  (extractor={extractor})")

    async def _run() -> None:
        async with AsyncClient(cfg.api_url, api_key=cfg.api_key, timeout=3600.0) as c:
            try:
                async for evt in c.aiter_research_pdf(
                    path, extractor=extractor, domain=domain,
                ):
                    name = evt.get("event", "?")
                    data = evt.get("data", {})
                    snippet = ""
                    if isinstance(data, dict):
                        first_key = next(iter(data), None)
                        if first_key:
                            snippet = f"{first_key}={str(data[first_key])[:60]}"
                    click.secho(f"[{name}] ", fg="cyan", nl=False)
                    click.echo(snippet or str(data)[:80])
                    if name in ("ingested", "complete", "done"):
                        break
            except ScaffoldError as exc:
                click.secho(f"pdf ingest failed: {exc}", fg="red", err=True)
                raise

    try:
        asyncio.run(_run())
    except ScaffoldError:
        sys.exit(1)
    except KeyboardInterrupt:
        click.secho("\ninterrupted", fg="yellow")
        sys.exit(130)


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


class _RagGroup(click.Group):
    """Routes bare ``scaffold rag <text...>`` to the ``query`` subcommand
    so the U.7 form keeps working after we promoted ``rag`` to a group.

    Heuristic: if the first non-flag argument doesn't match a known
    subcommand, prepend ``query``. Flags (``--top-k``, ``--json``, ``-h``)
    pass through unchanged because they belong to the subcommand.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        first_positional = next((a for a in args if not a.startswith("-")), None)
        if first_positional is not None and first_positional not in self.commands:
            args = ["query"] + args
        return super().parse_args(ctx, args)


@cli.group(cls=_RagGroup, help="Query or audit the Milvus knowledge base.",
           epilog=RAG_EPILOG)
def rag() -> None:
    pass


@rag.command("query", help="Query the knowledge base.")
@click.argument("query", nargs=-1, required=True)
@click.option("--top-k", type=int, default=5, show_default=True)
@click.option("--domain", default=None, help="Restrict to one Milvus partition.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rag_query(
    ctx: click.Context,
    query: tuple[str, ...],
    top_k: int,
    domain: str | None,
    as_json: bool,
) -> None:
    _run_rag_query(ctx, query, top_k, domain, as_json)


def _run_rag_query(
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


@rag.command("dedup", help="Show the near-duplicate rejection log.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rag_dedup(
    ctx: click.Context, limit: int, offset: int, as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/rag/dedup", params={"limit": limit, "offset": offset})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    rows = (data or {}).get("entries", []) if isinstance(data, dict) else []
    total = (data or {}).get("total", len(rows))
    if not rows:
        click.echo("(no dedup entries)")
        return
    click.echo(f"{len(rows)} of {total}:")
    click.echo(f"{'action':<12} {'similarity':>10}  {'existing entry'}")
    click.echo("-" * 90)
    for r in rows:
        # Live shape uses `action_taken` + `similarity_score`; older test
        # fixtures used `action` + `similarity`. Accept both for symmetry
        # with `/status` field fallbacks.
        action = str(r.get("action_taken") or r.get("action", "?"))[:10]
        sim = r.get("similarity_score")
        if sim is None:
            sim = r.get("similarity")
        sim_s = f"{sim:>10.3f}" if isinstance(sim, (int, float)) else f"{'-':>10}"
        existing = str(r.get("existing_entry_id") or r.get("url") or "")[:60]
        click.echo(f"{action:<12} {sim_s}  {existing}")


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
  scaffold model list                            current per-role model assignments
  scaffold model available                       models loaded on Ollama
  scaffold model providers                       which providers are registered + key status
  scaffold model set general claude-haiku-4-5 --provider anthropic
                                                 write MODEL_GENERAL + MODEL_GENERAL_PROVIDER to .env
                                                 AND model_general to pipelines/scaffold_router/valves.json
  scaffold model set router qwen3-vl:235b-instruct-cloud
                                                 swap a role's model; provider unchanged
  scaffold model unset coder                     remove MODEL_CODER override; orchestrator + pipeline
                                                 fall back to their Settings/template defaults
  scaffold model set general --dry-run ...       print the edits without writing

§17.347. Writes to BOTH .env and pipelines/scaffold_router/valves.json
so orchestrator and OWUI agree on restart. Restart is NOT automatic —
the command prints the exact `docker restart ...` line and you decide
when to apply (in case a job is mid-flight).

Locked roles (`embedder`, `reranker`) are config-locked per the invariants
in OVERVIEW.md §15 — set/unset on those will error.
"""

# §17.347 — tunable model roles. Keep in sync with app/config.py Settings
# and pipelines/scaffold_router.py Valves. Roles in PIPELINE_HAS_VALVE
# also get written to pipelines/scaffold_router/valves.json; roles only
# in TUNABLE_ROLES write to .env only (the pipeline has no per-role
# valve for them — currently just `cloud_heavy`).
TUNABLE_ROLES: tuple[str, ...] = (
    "general", "verifier", "coder", "router",
    "fallback", "cloud_alt", "cloud_heavy",
)
PIPELINE_HAS_VALVE: frozenset[str] = frozenset({
    "general", "verifier", "coder", "router", "fallback", "cloud_alt",
})
LOCKED_ROLES: frozenset[str] = frozenset({"embedder", "reranker", "embedder_pipeline"})
KNOWN_PROVIDERS: tuple[str, ...] = ("ollama", "openai", "anthropic")


def _resolve_repo_root(cwd: "Path | None" = None) -> "Path | None":
    """Find the directory holding the orchestrator's .env (walk up from cwd).

    Returns None if no .env found within 6 levels — caller decides whether
    that's fatal. The lookup mirrors ``cli/scaffold_cli/config.py``'s
    ``_walk_for_dotenv`` so behavior is consistent across the CLI.
    """
    from pathlib import Path
    cur = (cwd or Path.cwd()).resolve()
    for _ in range(6):
        if (cur / ".env").is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _update_env_var(env_path: "Path", key: str, value: str) -> str:
    """Write or update a ``KEY=value`` line in .env. Preserves all other
    lines verbatim (including comments and blank lines). Returns a
    short human-readable change description.

    If KEY is present (commented out OR active) the first active match is
    replaced. If KEY is only present commented-out, we append a new active
    line at the end (don't uncomment — the comment may carry intent).
    If KEY is absent entirely, append at the end.
    """
    lines = env_path.read_text().splitlines()
    new_line = f"{key}={value}"
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        existing_key, _, _ = stripped.partition("=")
        if existing_key.strip() == key:
            if ln == new_line:
                return f"{key} already set to {value!r}"
            lines[i] = new_line
            env_path.write_text("\n".join(lines) + ("\n" if env_path.read_text().endswith("\n") else ""))
            return f"updated {key}={value!r}"
    # Not present as active line — append.
    text = env_path.read_text()
    sep = "" if text.endswith("\n") else "\n"
    env_path.write_text(text + sep + new_line + "\n")
    return f"added {key}={value!r}"


def _remove_env_var(env_path: "Path", key: str) -> str:
    """Remove the first active ``KEY=...`` line from .env. Comments
    referencing the key are left intact (they document intent).
    Returns a short human-readable change description.
    """
    lines = env_path.read_text().splitlines()
    keep: list[str] = []
    removed = False
    for ln in lines:
        stripped = ln.lstrip()
        if not removed and not stripped.startswith("#") and "=" in stripped:
            existing_key, _, _ = stripped.partition("=")
            if existing_key.strip() == key:
                removed = True
                continue  # drop this line
        keep.append(ln)
    if not removed:
        return f"{key} was not set (no change)"
    env_path.write_text("\n".join(keep) + ("\n" if env_path.read_text().endswith("\n") else ""))
    return f"removed {key}="


def _update_pipeline_valve(valves_path: "Path", key: str, value: str) -> str:
    """Set ``key`` to ``value`` in the OWUI pipeline's live valves.json.

    Preserves all other keys verbatim. Returns a short change description.
    Creates the file with the single key if it doesn't exist (operator
    deployed before bootstrap ran).
    """
    if valves_path.is_file():
        try:
            data = _json.loads(valves_path.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get(key) == value:
        return f"pipeline valve {key!r} already {value!r}"
    prev = data.get(key)
    data[key] = value
    valves_path.parent.mkdir(parents=True, exist_ok=True)
    valves_path.write_text(_json.dumps(data, indent=2) + "\n")
    if prev is None:
        return f"added pipeline valve {key}={value!r}"
    return f"updated pipeline valve {key}={value!r} (was {prev!r})"


def _remove_pipeline_valve(valves_path: "Path", key: str) -> str:
    """Remove ``key`` from the OWUI pipeline's live valves.json. The
    valve falls back to the template default on next pipeline restart.
    """
    if not valves_path.is_file():
        return f"pipeline valves file missing — no change"
    try:
        data = _json.loads(valves_path.read_text())
    except Exception:
        return f"pipeline valves file unreadable — no change"
    if not isinstance(data, dict) or key not in data:
        return f"pipeline valve {key!r} not set — no change"
    prev = data.pop(key)
    valves_path.write_text(_json.dumps(data, indent=2) + "\n")
    return f"removed pipeline valve {key}= (was {prev!r})"


def _check_compose_shadow(repo_root: "Path", env_key: str) -> str | None:
    """§17.349 — catch the §17.348 silent-shadow class.

    Grep ``docker-compose.yml`` for an unparameterized ``<env_key>: <literal>``
    line. Docker Compose's ``environment:`` block wins over ``env_file:``,
    so if compose hardcodes the var, writing to ``.env`` has no effect —
    the operator restarts, sees no change, and the CLI looks broken.

    Returns a warning string when a shadow is found, or ``None`` if clean.
    Matches ONLY the ``KEY: bareword`` pattern; ``KEY: ${VAR:-default}`` is
    the correct form and is intentionally not flagged.
    """
    compose = repo_root / "docker-compose.yml"
    if not compose.is_file():
        return None
    try:
        text = compose.read_text()
    except Exception:
        return None
    # Match `<spaces>KEY: <value>` where value is NOT `$...` (the
    # parameterized form). The `KEY` must match env_key exactly to avoid
    # false positives on similar-named vars.
    import re
    pattern = re.compile(
        rf"^\s+{re.escape(env_key)}:\s+([^\s$\"'#].*?)\s*$",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        value = m.group(1).strip()
        # Skip parameterized forms — defensive in case the regex matches
        # something weird.
        if value.startswith("$"):
            continue
        return (
            f"docker-compose.yml hardcodes {env_key}={value!r} in an "
            f"environment: block. Compose env wins over .env, so this "
            f"change will not take effect until you also parameterize "
            f"the compose line (see §17.348 for the pattern)."
        )
    return None


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


@model.command("providers", help="Show registered providers + API-key status (§17.347).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def model_providers(ctx: click.Context, as_json: bool) -> None:
    """Hardcoded list of providers shipped with scaffold-engine plus a
    best-effort key/health check pulled from the orchestrator's /config.

    Keeps logic out of a new HTTP endpoint — the provider list changes
    rarely (Anthropic landed in §17.345; before that, a year between
    additions). When that cadence changes, replace this with a
    ``/config/providers`` query.
    """
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/config")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    fields = data.get("fields", []) if isinstance(data, dict) else []
    by_name = {f["name"]: f for f in fields}

    rows: list[dict[str, str]] = []
    for prov in KNOWN_PROVIDERS:
        key_field = {
            "ollama": None,  # no auth
            "openai": "openai_api_key",
            "anthropic": "anthropic_api_key",
        }.get(prov)
        if key_field is None:
            status, detail = "ready", "no auth required"
        else:
            f = by_name.get(key_field, {})
            val = str(f.get("value") or "").strip()
            # Pydantic SecretStr usually serializes as "**********" when set,
            # empty string when not. Treat any non-empty, non-asterisk as set.
            is_set = bool(val) and not all(c == "*" for c in val)
            # Asterisks-only is the SecretStr-set sentinel.
            if all(c == "*" for c in val) and val:
                is_set = True
            status = "ready" if is_set else "no API key"
            detail = f"{key_field} present" if is_set else f"set {key_field.upper()}"
        rows.append({"provider": prov, "status": status, "detail": detail})

    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    click.echo(f"{'provider':<12} {'status':<14} {'detail':<32}")
    click.echo("-" * 60)
    for r in rows:
        color = "green" if r["status"] == "ready" else "yellow"
        click.echo(f"{r['provider']:<12} ", nl=False)
        click.secho(f"{r['status']:<14}", fg=color, nl=False)
        click.echo(f" {r['detail']:<32}")


def _print_restart_hint(touched_pipeline: bool) -> None:
    """Print the canonical restart line. Operator decides timing —
    a `set` during a running job would interrupt work, so we never
    auto-restart (per the §17.347 design decision)."""
    containers = "scaffold-orchestrator"
    if touched_pipeline:
        containers += " open-webui-pipelines"
    click.echo("")
    click.secho("Next: ", fg="cyan", nl=False)
    click.echo(f"restart to apply — `docker restart {containers}`")
    click.secho("      ", nl=False)
    click.echo("(skip if no job is mid-flight; otherwise wait for it to finish)")


@model.command("set", help="Set a role's model + optional provider (§17.347).")
@click.argument("role")
@click.argument("model_name")
@click.option("--provider", "provider", default=None,
              help="Optional provider to set MODEL_<ROLE>_PROVIDER (ollama|openai|anthropic).")
@click.option("--repo-root", "repo_root", default=None,
              type=click.Path(file_okay=False, dir_okay=True, exists=True, resolve_path=True),
              help="Override .env discovery (defaults to walking up from cwd).")
@click.option("--dry-run", is_flag=True,
              help="Print the edits without writing them.")
def model_set(role: str, model_name: str, provider: str | None,
              repo_root: str | None, dry_run: bool) -> None:
    """Write to both .env (orchestrator) and pipelines/scaffold_router/valves.json
    (OWUI pipeline) so both surfaces agree on restart."""
    from pathlib import Path

    role = role.lower().lstrip("-").replace("-", "_")
    if role in LOCKED_ROLES:
        click.secho(
            f"role {role!r} is config-locked (see OVERVIEW.md §15 — embedder dim "
            f"is locked at 512; reranker is a CrossEncoder singleton outside the "
            f"provider system). Refusing to write.", fg="red", err=True,
        )
        sys.exit(2)
    if role not in TUNABLE_ROLES:
        click.secho(
            f"unknown role {role!r}. Tunable roles: {', '.join(TUNABLE_ROLES)}",
            fg="red", err=True,
        )
        sys.exit(2)
    if provider is not None and provider not in KNOWN_PROVIDERS:
        click.secho(
            f"unknown provider {provider!r}. Known: {', '.join(KNOWN_PROVIDERS)}",
            fg="red", err=True,
        )
        sys.exit(2)

    root = Path(repo_root) if repo_root else _resolve_repo_root()
    if root is None:
        click.secho(
            "no .env found in cwd or parents (up to 6 levels). Run from the "
            "scaffold-engine repo root, or pass --repo-root.", fg="red", err=True,
        )
        sys.exit(2)
    env_path = root / ".env"
    valves_path = root / "pipelines" / "scaffold_router" / "valves.json"
    env_key = f"MODEL_{role.upper()}"
    pipeline_key = f"model_{role}"

    edits: list[str] = []
    if dry_run:
        edits.append(f"would set {env_key}={model_name!r} in {env_path}")
        if provider:
            edits.append(f"would set {env_key}_PROVIDER={provider!r} in {env_path}")
        if role in PIPELINE_HAS_VALVE:
            edits.append(f"would set {pipeline_key}={model_name!r} in {valves_path}")
        for e in edits:
            click.echo(f"  - {e}")
        click.echo("(dry-run — no files changed)")
        return

    edits.append(_update_env_var(env_path, env_key, model_name))
    if provider:
        edits.append(_update_env_var(env_path, f"{env_key}_PROVIDER", provider))
    touched_pipeline = False
    if role in PIPELINE_HAS_VALVE:
        edits.append(_update_pipeline_valve(valves_path, pipeline_key, model_name))
        touched_pipeline = True
    for e in edits:
        click.echo(f"  ✓ {e}")

    # §17.349 — warn if compose shadows this env var (closes the §17.348
    # silent-failure class on any future host that introduces such a line).
    for key_to_check in [env_key] + ([f"{env_key}_PROVIDER"] if provider else []):
        warning = _check_compose_shadow(root, key_to_check)
        if warning:
            click.echo("")
            click.secho(f"⚠ {warning}", fg="yellow")

    _print_restart_hint(touched_pipeline)


@model.command("unset", help="Remove a role override; reset to Settings/template default (§17.347).")
@click.argument("role")
@click.option("--repo-root", "repo_root", default=None,
              type=click.Path(file_okay=False, dir_okay=True, exists=True, resolve_path=True))
@click.option("--keep-provider", is_flag=True,
              help="Don't remove MODEL_<ROLE>_PROVIDER (only remove the model override).")
@click.option("--dry-run", is_flag=True)
def model_unset(role: str, repo_root: str | None,
                keep_provider: bool, dry_run: bool) -> None:
    """Inverse of `set`. Removes the override from .env and clears the
    pipeline valve so both surfaces fall back to their built-in defaults
    on restart (Settings defaults for the orchestrator, template defaults
    for the pipeline)."""
    from pathlib import Path

    role = role.lower().lstrip("-").replace("-", "_")
    if role in LOCKED_ROLES:
        click.secho(
            f"role {role!r} is config-locked; nothing to unset.", fg="yellow",
        )
        sys.exit(2)
    if role not in TUNABLE_ROLES:
        click.secho(
            f"unknown role {role!r}. Tunable roles: {', '.join(TUNABLE_ROLES)}",
            fg="red", err=True,
        )
        sys.exit(2)

    root = Path(repo_root) if repo_root else _resolve_repo_root()
    if root is None:
        click.secho(
            "no .env found. Run from the scaffold-engine repo root or pass --repo-root.",
            fg="red", err=True,
        )
        sys.exit(2)
    env_path = root / ".env"
    valves_path = root / "pipelines" / "scaffold_router" / "valves.json"
    env_key = f"MODEL_{role.upper()}"
    pipeline_key = f"model_{role}"

    edits: list[str] = []
    if dry_run:
        edits.append(f"would remove {env_key}= from {env_path}")
        if not keep_provider:
            edits.append(f"would remove {env_key}_PROVIDER= from {env_path}")
        if role in PIPELINE_HAS_VALVE:
            edits.append(f"would remove {pipeline_key}= from {valves_path}")
        for e in edits:
            click.echo(f"  - {e}")
        click.echo("(dry-run — no files changed)")
        return

    edits.append(_remove_env_var(env_path, env_key))
    if not keep_provider:
        edits.append(_remove_env_var(env_path, f"{env_key}_PROVIDER"))
    touched_pipeline = False
    if role in PIPELINE_HAS_VALVE:
        edits.append(_remove_pipeline_valve(valves_path, pipeline_key))
        touched_pipeline = True
    for e in edits:
        click.echo(f"  ✓ {e}")
    _print_restart_hint(touched_pipeline)


# ---------------------------------------------------------------------------
# assist group — Assistant Mode (human-in-the-loop) parity with OWUI
# ---------------------------------------------------------------------------

ASSIST_EPILOG = """
\b
Examples:
  scaffold assist start <job_id>                            open a session
  scaffold assist next <session_id>                         claim next step
  scaffold assist submit <sid> <node> --output "ran ok"     record evidence
  scaffold assist submit <sid> <node> --file diff.patch     evidence from file
  scaffold assist submit <sid> <node> -                     read evidence from stdin
  scaffold assist skip <sid> <node>                         skip a step
  scaffold assist handoff <sid> <node> --mode single        let executor take it
  scaffold assist pause <sid>                               pause a session
  scaffold assist resume <sid>                              resume a session
  scaffold assist abandon <sid>                             abandon (--yes to skip prompt)
  scaffold assist friction add <sid> <node> "took 3 tries"  log a note
  scaffold assist friction list <sid>                       show all notes
  scaffold assist status <sid>                              session + step rollup

The OWUI ``/assist`` chat surface is stateless — paste the session_id in every
subcommand. CLI mirrors that contract for parity.
"""


@cli.group(help="Assistant Mode — drive a human-in-the-loop session.",
           epilog=ASSIST_EPILOG)
def assist() -> None:
    pass


@assist.command("start", help="Open an assist session for a planned job.")
@click.argument("job_id")
@click.option("--handoff-policy",
              type=click.Choice(["manual", "auto_on_skip", "auto_all_remaining"]),
              default=None,
              help="Default: manual (server-side default).")
@click.option("--replan-policy",
              type=click.Choice(["context_only", "selective", "full", "disabled"]),
              default=None,
              help="Default: context_only. Use 'disabled' for tests.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def assist_start(
    ctx: click.Context,
    job_id: str,
    handoff_policy: str | None,
    replan_policy: str | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    body: dict = {"job_id": job_id}
    if handoff_policy:
        body["handoff_policy"] = handoff_policy
    if replan_policy:
        body["replan_policy"] = replan_policy
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/assist/start", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    sid = data.get("session_id") if isinstance(data, dict) else None
    click.secho(f"session: {sid}", fg="green")
    if isinstance(data, dict):
        if (status := data.get("status")):
            click.echo(f"  status: {status}")
        if (counts := data.get("step_counts")):
            click.echo(f"  steps: {counts}")
    if sid:
        _hint(f"scaffold assist next {sid}")


@assist.command("status", help="Session + step rollup (alias of `assist get`).")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def assist_status(ctx: click.Context, session_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/assist/{session_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return
    click.echo(f"session: {data.get('id') or session_id}")
    click.echo(f"  job:    {data.get('job_id', '?')}")
    click.echo(f"  status: {data.get('status', '?')}")
    counts = data.get("step_counts") or {}
    if counts:
        click.echo(f"  steps:  {counts}")


@assist.command("next", help="Claim the next pending step + assembled prompt.")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def assist_next(ctx: click.Context, session_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/assist/{session_id}/next")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return
    node_key = data.get("node_key")
    if node_key is None:
        click.echo(f"(no claimable step — session status: {data.get('status', '?')})")
        if (counts := data.get("step_counts")):
            click.echo(f"  steps: {counts}")
        return
    click.secho(f"node: {node_key}", fg="green")
    if (prompt := data.get("prompt")):
        click.echo("---")
        click.echo(prompt)
        click.echo("---")
    _hint(
        f'scaffold assist submit {session_id} {node_key} --output "<your evidence>"'
    )


def _read_evidence(output: str | None, file: str | None) -> str:
    """Resolve evidence text from --output / --file / stdin (`-`)."""
    if file:
        if file == "-":
            return sys.stdin.read()
        with open(file, "r", encoding="utf-8") as f:
            return f.read()
    if output is not None:
        return output
    return ""


@assist.command("submit", help="Record evidence for a step.")
@click.argument("session_id")
@click.argument("node_key")
@click.option("--output", default=None, help="Inline evidence string.")
@click.option("--file", default=None,
              help="Read evidence from file (use '-' for stdin).")
@click.option("--evidence-kind",
              type=click.Choice([
                  "text", "command_output", "file_diff",
                  "screenshot_ref", "url", "none",
              ]),
              default="text", show_default=True)
@click.option("--friction", "friction_note", default=None,
              help="Optional friction note recorded with the submit.")
@click.pass_context
def assist_submit(
    ctx: click.Context,
    session_id: str,
    node_key: str,
    output: str | None,
    file: str | None,
    evidence_kind: str,
    friction_note: str | None,
) -> None:
    cfg = ctx.obj["cfg"]
    evidence = _read_evidence(output, file)
    if not evidence and evidence_kind != "none":
        raise click.UsageError(
            "evidence is required — pass --output / --file / `--file -` (stdin)"
        )
    body: dict = {
        "node_key": node_key,
        "output": evidence,
        "evidence_kind": evidence_kind,
        "action": "submit",
    }
    if friction_note:
        body["friction_note"] = friction_note
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post(f"/assist/{session_id}/submit", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"submitted {node_key}", fg="green")
    if isinstance(data, dict):
        if (st := data.get("status")):
            click.echo(f"  step status: {st}")
        if data.get("divergence"):
            click.secho("  divergence detected", fg="yellow")
    _hint(f"scaffold assist next {session_id}")


@assist.command("skip", help="Skip a step (records action='skip').")
@click.argument("session_id")
@click.argument("node_key")
@click.pass_context
def assist_skip(ctx: click.Context, session_id: str, node_key: str) -> None:
    cfg = ctx.obj["cfg"]
    body = {
        "node_key": node_key,
        "evidence_kind": "none",
        "action": "skip",
    }
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.post(f"/assist/{session_id}/submit", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"skipped {node_key}", fg="green")
    _hint(f"scaffold assist next {session_id}")


@assist.command("handoff", help="Hand a step (or rest of DAG) to the autonomous executor.")
@click.argument("session_id")
@click.argument("node_key")
@click.option("--mode", type=click.Choice(["single", "all_remaining"]),
              default="single", show_default=True)
@click.pass_context
def assist_handoff(
    ctx: click.Context,
    session_id: str,
    node_key: str,
    mode: str,
) -> None:
    """Streams SSE node events from /assist/{sid}/handoff."""
    import asyncio
    from scaffold_client import AsyncClient, ScaffoldError

    cfg = ctx.obj["cfg"]
    click.echo(f"handoff: {node_key} (mode={mode})")

    async def _run() -> None:
        async with AsyncClient(cfg.api_url, api_key=cfg.api_key, timeout=3600.0) as c:
            try:
                async for evt in c.aiter_assist_handoff(
                    session_id, node_key, mode=mode,
                ):
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
                    if name in ("complete", "done", "node_completed", "all_complete"):
                        break
            except ScaffoldError as exc:
                click.secho(f"handoff failed: {exc}", fg="red", err=True)
                raise

    try:
        asyncio.run(_run())
    except ScaffoldError:
        sys.exit(1)
    except KeyboardInterrupt:
        click.secho("\ninterrupted", fg="yellow")
        sys.exit(130)
    _hint(f"scaffold assist status {session_id}")


@assist.command("pause", help="Pause an active session.")
@click.argument("session_id")
@click.pass_context
def assist_pause(ctx: click.Context, session_id: str) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.post(f"/assist/{session_id}/pause")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"paused {session_id}", fg="green")
    _hint(f"scaffold assist resume {session_id}")


@assist.command("resume", help="Resume a paused session.")
@click.argument("session_id")
@click.pass_context
def assist_resume(ctx: click.Context, session_id: str) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.post(f"/assist/{session_id}/resume")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"resumed {session_id}", fg="green")
    _hint(f"scaffold assist next {session_id}")


@assist.command("abandon", help="Abandon a session (DELETE).")
@click.argument("session_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def assist_abandon(ctx: click.Context, session_id: str, yes: bool) -> None:
    cfg = ctx.obj["cfg"]
    if not yes:
        click.confirm(f"Abandon assist session {session_id}?", abort=True)
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.delete(f"/assist/{session_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"abandoned {session_id[:8]}", fg="green")


@assist.group("friction", help="Append or list friction notes for a session.")
def assist_friction_group() -> None:
    pass


@assist_friction_group.command("add", help="Record a friction note on a step.")
@click.argument("session_id")
@click.argument("node_key")
@click.argument("note", nargs=-1, required=True)
@click.pass_context
def assist_friction_add(
    ctx: click.Context,
    session_id: str,
    node_key: str,
    note: tuple[str, ...],
) -> None:
    cfg = ctx.obj["cfg"]
    note_text = " ".join(note).strip()
    if not note_text:
        raise click.UsageError("note text is required")
    body = {"node_key": node_key, "note": note_text}
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            c.post(f"/assist/{session_id}/friction", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho("friction recorded", fg="green")


@assist_friction_group.command("list", help="List every friction note for a session.")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def assist_friction_list(
    ctx: click.Context, session_id: str, as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/assist/{session_id}/friction")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    notes = (data or {}).get("friction", []) if isinstance(data, dict) else []
    if not notes:
        click.echo("(no friction notes)")
        return
    click.echo(f"{len(notes)} note(s):")
    for n in notes:
        node = n.get("node_key", "?")
        ts = n.get("created_at", "")
        text = n.get("note", "")
        click.echo(f"  [{node}] {ts}  {text}")


# ---------------------------------------------------------------------------
# U.8.E — prompts + gt groups (CLI shims over existing SDK resources)
# ---------------------------------------------------------------------------

PROMPTS_EPILOG = """
\b
Examples:
  scaffold prompts list <job_id>                       all node prompts (preview)
  scaffold prompts get <job_id> <node_key>             one node's full prompt
  scaffold prompts history <job_id> <node_key>         revision audit trail
  scaffold prompts update <job_id> <node_key> --file new.txt
  cat new.txt | scaffold prompts update <id> <node> --file -

Updates create a new revision; the previous prompt stays in history.
"""


@cli.group(help="Read and edit per-node prompts + their revision history.",
           epilog=PROMPTS_EPILOG)
def prompts() -> None:
    pass


@prompts.command("list", help="List every node's current prompt for a job.")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def prompts_list(ctx: click.Context, job_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/prompts/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return
    nodes = data.get("nodes") or []
    total = data.get("node_count", len(nodes))
    click.echo(f"job: {data.get('job_id', job_id)}  ({len(nodes)} of {total} nodes)")
    if not nodes:
        click.echo("(no nodes — job may not have a DAG yet)")
        return
    click.echo(f"{'key':<10} {'rev':>4}  preview")
    click.echo("-" * 90)
    for n in nodes:
        key = str(n.get("node_key", ""))[:9]
        rev = n.get("revision")
        rev_s = f"{rev:>4}" if isinstance(rev, int) else f"{'-':>4}"
        prompt = (n.get("prompt") or n.get("text") or "")
        preview = prompt[:60].replace("\n", " ")
        click.echo(f"{key:<10} {rev_s}  {preview}")


@prompts.command("get", help="Show one node's full current prompt.")
@click.argument("job_id")
@click.argument("node_key")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def prompts_get(
    ctx: click.Context, job_id: str, node_key: str, as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/prompts/{job_id}/{node_key}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if isinstance(data, dict):
        click.echo(f"job: {data.get('job_id', job_id)}  node: {data.get('node_key', node_key)}")
        if (rev := data.get("revision")) is not None:
            click.echo(f"revision: {rev}")
        click.echo("---")
        click.echo(data.get("prompt") or data.get("text") or "")


@prompts.command("history", help="Show revision history for a node's prompt.")
@click.argument("job_id")
@click.argument("node_key")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def prompts_history(
    ctx: click.Context, job_id: str, node_key: str, as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/prompts/{job_id}/{node_key}/history")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    revs = (data or {}).get("revisions", []) if isinstance(data, dict) else []
    if not revs:
        click.echo("(no revisions)")
        return
    click.echo(f"{len(revs)} revision(s) for {node_key}:")
    click.echo(f"{'rev':>4}  {'created_at':<24} preview")
    click.echo("-" * 90)
    for r in revs:
        rev = r.get("revision")
        rev_s = f"{rev:>4}" if isinstance(rev, int) else f"{'-':>4}"
        ts = str(r.get("created_at") or "")[:23]
        prompt = (r.get("prompt") or r.get("text") or "")[:50].replace("\n", " ")
        click.echo(f"{rev_s}  {ts:<24} {prompt}")


@prompts.command("update", help="Set a node's prompt (creates a new revision).")
@click.argument("job_id")
@click.argument("node_key")
@click.option("--file", "file", required=True,
              help="Read prompt from file (use '-' for stdin). Multi-line OK.")
@click.pass_context
def prompts_update(
    ctx: click.Context, job_id: str, node_key: str, file: str,
) -> None:
    cfg = ctx.obj["cfg"]
    if file == "-":
        prompt_text = sys.stdin.read()
    else:
        with open(file, "r", encoding="utf-8") as f:
            prompt_text = f.read()
    if not prompt_text.strip():
        raise click.UsageError("prompt text is empty")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post(f"/prompts/{job_id}/{node_key}", json={"prompt": prompt_text})
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"updated prompt for {node_key}", fg="green")
    if isinstance(data, dict) and (rev := data.get("revision")) is not None:
        click.echo(f"  new revision: {rev}")


# ---- gt group ------------------------------------------------------------

GT_EPILOG = """
\b
Examples:
  scaffold gt stats                              corpus summary
  scaffold gt list --domain rag --per-page 10    paginated browse
  scaffold gt search "hybrid retrieval"          semantic search
  scaffold gt detail <entry_id>                  full content
  scaffold gt extract "kubernetes pod lifecycle" --queries "lifecycle hooks"

`extract` runs SearXNG → LLM distill → ingest as TOON entries (slow).
"""


@cli.group(help="Browse, search, and extract ground-truth corpus entries.",
           epilog=GT_EPILOG)
def gt() -> None:
    pass


@gt.command("stats", help="Domain + source-type counts across the corpus.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def gt_stats(ctx: click.Context, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/gt/stats")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return
    total = data.get("total_entries", 0)
    click.secho(f"GT corpus — {total} total entries", bold=True)
    domains = data.get("domains") or {}
    if domains:
        click.echo("\ndomains:")
        for d, n in sorted(domains.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {d:<10} {n}")
    sources = data.get("source_types") or {}
    if sources:
        click.echo("\nsource types:")
        for s, n in sorted(sources.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {s:<22} {n}")


@gt.command("list", help="List TOON entries (paginated).")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--per-page", type=int, default=20, show_default=True)
@click.option("--domain", default=None, help="Filter to one domain.")
@click.option("--include-history", is_flag=True,
              help="Include superseded entries from the version chain.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def gt_list(
    ctx: click.Context,
    page: int,
    per_page: int,
    domain: str | None,
    include_history: bool,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"page": page, "per_page": per_page}
    if domain:
        params["domain"] = domain
    if include_history:
        params["include_history"] = True
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/gt/list", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    rows = (data or {}).get("entries", []) if isinstance(data, dict) else []
    total = (data or {}).get("total", len(rows))
    total_pages = (data or {}).get("total_pages", 1)
    if not rows:
        click.echo("(no entries)")
        return
    click.echo(f"page {page}/{total_pages}, {len(rows)} of {total}:")
    click.echo(f"{'entry_id':<28} {'domain':<8} {'conf':>5}  title")
    click.echo("-" * 100)
    for r in rows:
        eid = str(r.get("entry_id", ""))[:26]
        dom = str(r.get("domain", ""))[:6]
        conf = r.get("confidence")
        conf_s = f"{conf:>5.2f}" if isinstance(conf, (int, float)) else f"{'-':>5}"
        title = (r.get("title") or "")[:50]
        click.echo(f"{eid:<28} {dom:<8} {conf_s}  {title}")


@gt.command("search", help="Semantic search across GT entries.")
@click.argument("query", nargs=-1, required=True)
@click.option("--top-k", type=int, default=10, show_default=True)
@click.option("--domain", default=None, help="Restrict to one domain.")
@click.option("--include-history", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def gt_search(
    ctx: click.Context,
    query: tuple[str, ...],
    top_k: int,
    domain: str | None,
    include_history: bool,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    q = " ".join(query).strip()
    body: dict = {"query": q, "top_k": top_k}
    if domain:
        body["domain"] = domain
    if include_history:
        body["include_history"] = True
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post("/gt/search", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    results = (data or {}).get("results", []) if isinstance(data, dict) else []
    if not results:
        click.echo("(no results)")
        return
    for i, r in enumerate(results, 1):
        score = r.get("score", 0.0)
        eid = r.get("entry_id", "")
        title = (r.get("title") or "")[:60]
        click.secho(f"#{i}  ", fg="cyan", nl=False)
        click.echo(f"score={score:.3f}  entry={eid}")
        if title:
            click.echo(f"     {title}")
        snippet = (r.get("snippet") or r.get("text") or "")[:200].replace("\n", " ")
        if snippet:
            click.echo(f"     {snippet}…")


@gt.command("detail", help="Show full content of one GT entry.")
@click.argument("entry_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def gt_detail(ctx: click.Context, entry_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/gt/detail/{entry_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return
    click.secho(f"entry: {data.get('entry_id', entry_id)}", bold=True)
    for k in ("title", "domain", "source_type", "confidence", "url"):
        v = data.get(k)
        if v is not None:
            click.echo(f"  {k}: {v}")
    body = data.get("content") or data.get("text") or data.get("toon") or ""
    if body:
        click.echo("---")
        click.echo(body)


@gt.command("extract", help="Extract GT entries via SearXNG + LLM (slow).")
@click.argument("topic", nargs=-1, required=True)
@click.option("--query", "queries", multiple=True,
              help="Extra search query (repeatable).")
@click.option("--push-to-github", is_flag=True,
              help="Also push the resulting TOON to ground_truths/ via gh.")
@click.option("--target-file", default=None,
              help="Override the target file path under ground_truths/.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def gt_extract(
    ctx: click.Context,
    topic: tuple[str, ...],
    queries: tuple[str, ...],
    push_to_github: bool,
    target_file: str | None,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    topic_text = " ".join(topic).strip()
    body: dict = {"topic": topic_text}
    if queries:
        body["queries"] = list(queries)
    if push_to_github:
        body["push_to_github"] = True
    if target_file:
        body["target_file"] = target_file
    try:
        # GT extraction loops through SearXNG + LLM distill — long-running.
        with Client(cfg.api_url, cfg.api_key, timeout=1800.0) as c:
            data = c.post("/gt", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if isinstance(data, dict):
        click.secho(f"extracted {data.get('extracted', 0)} entries", fg="green", bold=True)
        for k in ("ingested", "rejected", "superseded", "target_file"):
            if (v := data.get(k)) is not None:
                click.echo(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# U.8.B — small CLI verbs (logs, exec retry, status, dag)
# ---------------------------------------------------------------------------

LOGS_EPILOG = """
\b
Examples:
  scaffold logs <job_id>                       per-node state + output preview
  scaffold logs <job_id> --limit 200           paginate (default 50 nodes)
  scaffold logs <job_id> --include-output      full output text, not preview
  scaffold logs <job_id> --include-compiled    add the job's compiled_output
  scaffold logs <job_id> --json                machine-readable

The /logs/{id} endpoint returns the DAG-node history with each node's
status, confidence, and output preview — it is NOT a line-by-line log
stream. For container-level logs, use `make logs` / `make logs-jobs`.
"""


@cli.command(help="Show per-node execution state + output for a job.",
             epilog=LOGS_EPILOG)
@click.argument("job_id")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--include-output", is_flag=True,
              help="Show full output_text instead of a 500-char preview.")
@click.option("--include-compiled", is_flag=True,
              help="Also include jobs.compiled_output in the response.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def logs(
    ctx: click.Context,
    job_id: str,
    limit: int,
    offset: int,
    include_output: bool,
    include_compiled: bool,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit, "offset": offset}
    if include_output:
        params["include_output"] = True
    if include_compiled:
        params["include_compiled"] = True
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/logs/{job_id}", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return

    nodes = data.get("nodes") or []
    total = data.get("node_count", len(nodes))
    job_status = data.get("job_status", "?")
    click.echo(f"job: {data.get('job_id', job_id)}  status: {job_status}  ({len(nodes)} of {total} nodes)")

    if not nodes:
        click.echo("(no DAG nodes — job may not have been planned yet)")
        return

    # §17.447 (Phase B / B2) — "verify" labels the column as the verifier's
    # confidence in each node's output (not a retrieval/feasibility score).
    click.echo(f"{'key':<10} {'status':<10} {'verify':>6} {'tool':<10} preview")
    click.echo("-" * 100)
    for n in nodes:
        key = str(n.get("node_key", ""))[:9]
        st = str(n.get("status", ""))[:9]
        conf = n.get("confidence")
        conf_s = f"{conf:>5.2f}" if isinstance(conf, (int, float)) else f"{'-':>5}"
        tool = str(n.get("tool", ""))[:9]
        # §17.445 — the API field is `output_preview` (NodeLog); reading
        # `output_text` always yielded blank (latent bug fixed here).
        out = (n.get("output_preview") or "")[:60].replace("\n", " ")
        click.echo(f"{key:<10} {st:<10} {conf_s}  {tool:<10} {out}")
        # §17.445 (Phase A / A1) — show WHY a node failed/blocked.
        reason = n.get("failure_reason")
        if reason and n.get("status") in ("failed", "blocked"):
            click.secho(f"{'':<10}↳ why: {str(reason)[:96]}", fg="yellow")

    compiled = data.get("compiled_output")
    if compiled:
        click.echo("")
        click.secho("compiled_output:", fg="cyan", bold=True)
        click.echo(compiled if include_compiled else (compiled[:500] + "…" if len(compiled) > 500 else compiled))


# ---- exec group (retry) ---------------------------------------------------

EXEC_EPILOG = """
\b
Examples:
  scaffold exec retry <job_id> <node_key>      reset a failed node to pending
  scaffold exec retry abc T2                   ↑ shorthand UUIDs work too

Run `scaffold logs <job_id>` to see the failure context first.
"""


@cli.group("exec", help="Node-level execution control (retry, etc.).",
           epilog=EXEC_EPILOG)
def exec_() -> None:
    """Python identifier ``exec_`` keeps us off the ``exec`` builtin;
    Click publishes the group as ``scaffold exec`` via the ``name`` arg."""


@exec_.command("retry", help="Retry a failed/blocked DAG node.")
@click.argument("job_id")
@click.argument("node_key")
@click.pass_context
def exec_retry(ctx: click.Context, job_id: str, node_key: str) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.post(
                "/exec/retry", json={"job_id": job_id, "node_key": node_key},
            )
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    click.secho(f"retried {node_key} on {job_id[:8]}", fg="green")
    if isinstance(data, dict) and (status := data.get("status")):
        click.echo(f"  job status now: {status}")
    _hint(f"scaffold jobs status {job_id}")


# ---- status (multi-job) ---------------------------------------------------

STATUS_EPILOG = """
\b
Examples:
  scaffold status                          counts + recent jobs
  scaffold status --filter blocked         only one state
  scaffold status --limit 50               extend recent list
  scaffold status --json                   machine-readable

`scaffold whatnow` filters this to actionable jobs only.
"""


@cli.command(help="Multi-job state view (calls /status).",
             epilog=STATUS_EPILOG)
@click.option("--filter", "status_filter", default=None,
              help="Restrict the recent-jobs list to one status.")
@click.option("--limit", type=int, default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def status(
    ctx: click.Context,
    status_filter: str | None,
    limit: int,
    as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    params: dict = {"limit": limit}
    if status_filter:
        params["status_filter"] = status_filter
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/status", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    if not isinstance(data, dict):
        click.echo(repr(data))
        return

    counts = data.get("status_counts") or data.get("counts") or {}
    if counts:
        click.secho("status counts:", fg="cyan", bold=True)
        for k in sorted(counts.keys()):
            click.echo(f"  {k:<24} {counts[k]}")
        click.echo("")

    rows = (
        data.get("recent_jobs")
        or data.get("jobs")
        or data.get("recent")
        or []
    )
    if not rows:
        click.echo("(no recent jobs)")
        return
    click.secho(f"recent jobs ({len(rows)}):", fg="cyan", bold=True)
    click.echo(f"{'job_id':<10} {'status':<22} title")
    click.echo("-" * 90)
    for r in rows:
        jid = str(r.get("id") or r.get("job_id", ""))[:8]
        st = str(r.get("status", ""))[:20]
        title = (r.get("title") or "")[:58]
        click.echo(f"{jid:<10} {st:<22} {title}")

    # Surface the most-actionable recent job's next_actions block — same
    # pattern OWUI's /status renderer uses since U.7 (F2).
    most_actionable = next(
        (r for r in rows if r.get("status") not in ("completed", "cancelled")),
        None,
    )
    if most_actionable and most_actionable.get("next_actions"):
        _render_next_actions(most_actionable)


# ---- dag (read DAG structure) --------------------------------------------

DAG_EPILOG = """
\b
Examples:
  scaffold dag <job_id>                    table view (default)
  scaffold dag <job_id> --mermaid          markdown ```mermaid block
  scaffold dag <job_id> --json             raw response

Read-only; the autonomous chain regenerates DAGs through `/confirm`.
"""


@cli.command(help="Show a job's DAG structure.", epilog=DAG_EPILOG)
@click.argument("job_id")
@click.option("--mermaid", is_flag=True,
              help="Emit a fenced ```mermaid block for paste-into-docs use.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def dag(
    ctx: click.Context, job_id: str, mermaid: bool, as_json: bool,
) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/dag/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    if not isinstance(data, dict):
        click.echo(repr(data))
        return

    nodes = data.get("nodes") or data.get("dag_nodes") or []
    if mermaid:
        click.echo("```mermaid")
        click.echo("graph TD")
        for n in nodes:
            key = n.get("node_key") or n.get("key") or "?"
            title = (n.get("title") or "")[:30]
            click.echo(f"  {key}[{key}: {title}]")
        for n in nodes:
            key = n.get("node_key") or n.get("key") or "?"
            for dep in (n.get("depends_on") or []):
                click.echo(f"  {dep} --> {key}")
        click.echo("```")
        return

    if not nodes:
        click.echo("(no DAG nodes — job may not be in planning yet)")
        return
    click.echo(f"job: {data.get('job_id', job_id)}  status: {data.get('job_status', '?')}")
    click.echo(f"{'key':<10} {'status':<10} {'depends_on':<22} model")
    click.echo("-" * 90)
    for n in nodes:
        key = str(n.get("node_key") or n.get("key", ""))[:9]
        st = str(n.get("status", ""))[:9]
        deps = ",".join(n.get("depends_on") or [])[:20]
        model = str(n.get("assigned_model") or n.get("model", ""))[:30]
        click.echo(f"{key:<10} {st:<10} {deps:<22} {model}")


ERRORS_EPILOG = """
\b
Examples:
  scaffold errors resolve <error_id>                    mark resolved, no note
  scaffold errors resolve <error_id> --note "fixed by Q.4 retry bump"

Closes the §17.69-deferred operator surface for the M4 PATCH endpoint.
The error_id is the UUID printed by `GET /observability/errors`.
"""


@cli.group(help="Triage operator-side error_logs (resolve / un-resolve).",
           epilog=ERRORS_EPILOG)
def errors() -> None:
    pass


@errors.command(
    "resolve",
    help="Mark an error_log row resolved (or --unresolve to re-open it).",
)
@click.argument("error_id")
@click.option("--note", default=None,
              help="Free-form triage note stored on the row.")
@click.option("--unresolve", is_flag=True,
              help="Re-open the row instead of resolving it.")
@click.pass_context
def errors_resolve(
    ctx: click.Context,
    error_id: str,
    note: str | None,
    unresolve: bool,
) -> None:
    """§17.88 — flip ``error_logs.resolved`` for a single row.

    Maps to ``PATCH /observability/errors/{id}``. Default action is to
    mark the row resolved + stamp ``resolved_at = NOW()``; ``--unresolve``
    sends ``resolved=false`` which clears ``resolved_at``. The optional
    ``--note`` is stored on the row's ``resolution`` column for the next
    operator looking at the audit trail.
    """
    cfg = ctx.obj["cfg"]
    body: dict[str, Any] = {"resolved": not unresolve, "resolution": note}
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.patch(f"/observability/errors/{error_id}", json=body)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    state = "resolved" if data.get("resolved") else "un-resolved"
    click.secho(f"{state} {str(data.get('error_id', error_id))[:8]}", fg="green")
    if data.get("resolution"):
        click.echo(f"  note: {data['resolution']}")


@errors.command("list", help="List recent error_logs rows (oncall view).")
@click.option("--resolved/--unresolved", "resolved", default=None,
              help="Filter by resolved flag. Default shows all; "
                   "--unresolved = what's still broken.")
@click.option("--since", type=int, default=None, metavar="MIN",
              help="Only rows from the last MIN minutes.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def errors_list(ctx, resolved, since, limit):
    """§17.446 (Phase B / B4) — read the error_logs the orchestrator records.

    Closes the gap where `scaffold errors resolve` needed a UUID the CLI
    couldn't list. Maps to ``GET /observability/errors``.
    """
    cfg = ctx.obj["cfg"]
    params: dict[str, Any] = {"limit": limit}
    if resolved is not None:
        params["resolved"] = str(resolved).lower()
    if since is not None:
        params["since_minutes"] = since
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/observability/errors", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    rows = data if isinstance(data, list) else (data.get("errors") or data.get("rows") or [])
    if not rows:
        click.echo("no error_logs rows match.")
        return
    click.echo(f"{'id':<10}{'type':<22}{'job':<10}{'res':<5}message")
    click.echo("-" * 92)
    for r in rows:
        eid = str(r.get("id", ""))[:8]
        etype = str(r.get("error_type", "") or "")[:21]
        job = str(r.get("job_id", "") or "")[:8]
        res = "yes" if r.get("resolved") else "no"
        msg = str(r.get("error_message", "") or "").replace("\n", " ")[:48]
        click.secho(f"{eid:<10}{etype:<22}{job:<10}{res:<5}{msg}",
                    fg=None if r.get("resolved") else "yellow")


@cli.group(help="Read system alerts (oncall view).")
def alerts() -> None:
    pass


@alerts.command("list", help="List recent system_alerts rows.")
@click.option("--kind", default=None, help="Filter by alert kind (exact match).")
@click.option("--since", type=int, default=None, metavar="MIN",
              help="Only alerts from the last MIN minutes.")
@click.option("--limit", type=int, default=100, show_default=True)
@click.pass_context
def alerts_list(ctx, kind, since, limit):
    """§17.446 (Phase B / B4) — read system_alerts. Maps to
    ``GET /observability/alerts`` (previously had no CLI reader)."""
    cfg = ctx.obj["cfg"]
    params: dict[str, Any] = {"limit": limit}
    if kind:
        params["kind"] = kind
    if since is not None:
        params["since_minutes"] = since
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/observability/alerts", params=params)
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    rows = data if isinstance(data, list) else (data.get("alerts") or data.get("rows") or [])
    if not rows:
        click.echo("no alerts match.")
        return
    _sev_color = {"critical": "red", "warning": "yellow", "info": None}
    click.echo(f"{'id':<10}{'severity':<10}{'kind':<34}message")
    click.echo("-" * 92)
    for r in rows:
        aid = str(r.get("id", ""))[:8]
        sev = str(r.get("severity", "") or "")
        knd = str(r.get("kind", "") or "")[:33]
        msg = str(r.get("message", "") or "").replace("\n", " ")[:44]
        click.secho(f"{aid:<10}{sev:<10}{knd:<34}{msg}", fg=_sev_color.get(sev))


# §17.565 — artifacts: typed deliverables persisted per job.
@cli.group(help="List and fetch a job's persisted artifacts (deliverables).")
def artifacts() -> None:
    pass


@artifacts.command("list", help="List a job's artifacts.")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def artifacts_list(ctx: click.Context, job_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/jobs/{job_id}/artifacts")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    arts = (data or {}).get("artifacts") or []
    if not arts:
        click.echo("No artifacts for this job.")
        return
    click.echo(f"{'id':<10}{'type':<10}{'bytes':>8}  title")
    click.echo("-" * 60)
    for a in arts:
        click.echo(
            f"{str(a.get('id',''))[:8]:<10}{a.get('artifact_type','?'):<10}"
            f"{a.get('size_bytes',0):>8}  {a.get('title','')}"
        )
    click.echo(f"\n{len(arts)} artifact(s). Fetch one: scaffold artifacts get <id>")


@artifacts.command("get", help="Fetch one artifact's content.")
@click.argument("artifact_id")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def artifacts_get(ctx: click.Context, artifact_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get(f"/artifacts/{artifact_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.secho(
        f"# {data.get('title','')}  [{data.get('artifact_type','?')}]  "
        f"{data.get('size_bytes',0)} bytes",
        fg="cyan",
    )
    click.echo(data.get("content") or "")


if __name__ == "__main__":
    cli()
