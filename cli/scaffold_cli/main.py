"""Click entry point for ``scaffold``.

Sprint H ships the read-mostly + ideate/confirm flows. SSE-streamed
endpoints (``/research``, ``/execute/all``) are deferred to Sprint I
once the streaming-uniformity work lands; for now the CLI prints a
hint pointing users at the OWUI surface for those.
"""
from __future__ import annotations

import json as _json
import sys

import click

from scaffold_cli import __version__
from scaffold_cli.client import CLIError, Client
from scaffold_cli.config import resolve_config


# ---------------------------------------------------------------------------
# Root group — global flags propagate to subcommands via the click context.
# ---------------------------------------------------------------------------
@click.group(help="Terminal client for Scaffold Engine.")
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
# version — never touches the network. Useful for `scaffold version` in
# scripts that want to gate on the installed CLI version.
# ---------------------------------------------------------------------------
@cli.command(help="Print the CLI version and where its config came from.")
@click.pass_context
def version(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"scaffold-cli {__version__}")
    click.echo(f"  api_url: {cfg.api_url}  ({cfg.source})")
    click.echo(f"  api_key: {'set' if cfg.api_key else 'unset'}")


# ---------------------------------------------------------------------------
# doctor — calls /health (no auth required) and renders the per-subsystem
# status. Mirrors `make doctor` for the parts the orchestrator can self-report.
# ---------------------------------------------------------------------------
@cli.command(help="Probe orchestrator /health and print a summary.")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    click.echo(f"Probing {cfg.api_url}/health …")
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            data = c.get("/health")
    except CLIError as exc:
        click.secho(f"FAIL  {exc}", fg="red", err=True)
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
            # Subsystems that report state without a clear up/down keyword
            # (e.g. cache stats) get rendered neutrally — they're informational,
            # not pass/fail.
            color = "yellow"
        latency_str = f"  {latency} ms" if latency is not None else ""
        click.secho(f"  {status:<10}", fg=color, nl=False)
        click.echo(f"{name}{latency_str}")

    if any_down:
        sys.exit(1)


# ---------------------------------------------------------------------------
# ideate — POST /ideate. Halts at awaiting_confirmation; user runs `confirm`.
# ---------------------------------------------------------------------------
@cli.command(help="Submit an idea: orchestrator refines + assesses feasibility.")
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
        click.echo("")
        click.secho(
            f"Next: scaffold confirm {job_id}", fg="cyan",
        )


# ---------------------------------------------------------------------------
# confirm — POST /ideate/confirm. Triggers the long-running research+plan
# pipeline. Today we return as soon as the call returns; once the streaming
# work in Sprint I lands, this should switch to SSE.
# ---------------------------------------------------------------------------
@cli.command(help="Confirm an ideated job to start research + planning.")
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


# ---------------------------------------------------------------------------
# jobs — list + status. The orchestrator endpoints are GET /jobs and
# GET /jobs/<id>. We render a compact table for list and a JSON block for
# status (since jobs carry a wide payload).
# ---------------------------------------------------------------------------
@cli.group(help="List, inspect, and manage orchestrator jobs.")
def jobs() -> None:
    pass


@jobs.command("list", help="List recent jobs (default limit 25).")
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
        return

    # Compact table: id (truncated) | status | title.
    click.echo(f"{'job_id':<38} {'status':<24} title")
    click.echo("-" * 80)
    for r in rows:
        jid = str(r.get("id", ""))[:36]
        st = str(r.get("status", ""))[:22]
        title = (r.get("title") or r.get("idea") or "")[:60]
        click.echo(f"{jid:<38} {st:<24} {title}")


@jobs.command("status", help="Show full status for a single job.")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON response.")
@click.pass_context
def jobs_status(ctx: click.Context, job_id: str, as_json: bool) -> None:
    cfg = ctx.obj["cfg"]
    try:
        with Client(cfg.api_url, cfg.api_key) as c:
            # /exec/status/<id> is the orchestrator's "single job's execution
            # state" endpoint; it returns job metadata + DAG node summary.
            # The earlier CLI shipped GET /jobs/<id> which never existed and
            # silently 404'd into get_or_none() — fixed here.
            data = c.get_or_none(f"/exec/status/{job_id}")
    except CLIError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(1)

    if data is None:
        click.secho(f"job {job_id} not found", fg="yellow", err=True)
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


if __name__ == "__main__":
    cli()
