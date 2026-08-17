"""Render engine endpoint responses as chat text (§17.790).

Each renderer takes a parsed endpoint body and returns a compact markdown block
for the chat surface. Defensive: unknown/missing fields degrade to a short note
rather than raising, so a shape drift never breaks a turn.
"""
from __future__ import annotations

from typing import Any


def _short_id(v: str) -> str:
    return (v or "")[:8]


def health(d: dict[str, Any]) -> str:
    status = d.get("status", "unknown")
    lines = [f"**Engine health: {status}**"]
    for name, chk in (d.get("checks") or {}).items():
        s = chk.get("status", "?")
        lat = chk.get("latency_ms")
        extra = f" ({lat} ms)" if lat is not None else ""
        lines.append(f"- {name}: {s}{extra}")
    return "\n".join(lines)


def status(d: dict[str, Any]) -> str:
    counts = d.get("status_counts") or {}
    active = {k: v for k, v in counts.items() if v and k not in ("completed", "cancelled", "failed")}
    total = d.get("total_jobs", 0)
    lines = [f"**Active jobs** ({total} total)"]
    if active:
        lines.append(", ".join(f"{v} {k}" for k, v in active.items()))
    else:
        lines.append("Nothing running right now.")
    recent = d.get("recent_jobs") or []
    if recent:
        lines.append("\nRecent:")
        for j in recent[:8]:
            lines.append(f"- `{_short_id(j.get('id',''))}` {j.get('title','(untitled)')} — {j.get('status','?')}")
    return "\n".join(lines)


def jobs_list(d: dict[str, Any], *, header: str = "Recent jobs") -> str:
    jobs = d.get("jobs") or []
    if not jobs:
        return f"**{header}:** none found."
    lines = [f"**{header}** ({d.get('total', len(jobs))} total):"]
    for j in jobs[:12]:
        nodes = j.get("node_count")
        nc = f" · {nodes} nodes" if nodes else ""
        lines.append(f"- `{_short_id(j.get('id',''))}` {j.get('title','(untitled)')} — {j.get('status','?')}{nc}")
    return "\n".join(lines)


def rag(d: dict[str, Any]) -> str:
    hits = d.get("results") or d.get("hits") or d.get("matches") or []
    if not hits:
        return "**Knowledge base:** no matching entries."
    lines = [f"**Knowledge base** — {len(hits)} match(es):"]
    for h in hits[:6]:
        text = h.get("text") or h.get("content") or h.get("chunk") or ""
        score = h.get("score") or h.get("rerank_score")
        sc = f" (score {round(score, 3)})" if isinstance(score, (int, float)) else ""
        snippet = " ".join(str(text).split())[:220]
        lines.append(f"- {snippet}{sc}")
    return "\n".join(lines)


def results(d: dict[str, Any]) -> str:
    title = d.get("job_title") or "(untitled)"
    jstatus = d.get("job_status", "?")
    lines = [f"**{title}** — {jstatus}"]
    compiled = d.get("compiled_output")
    if compiled:
        lines.append("\n" + str(compiled).strip())
    else:
        counts = d.get("counts") or {}
        total = d.get("total_nodes")
        if counts or total:
            lines.append(f"Progress: {counts} of {total} nodes.")
        # §17.811 — one-line ETA when a run is in flight.
        prog = d.get("progress") or {}
        if prog.get("eta_human"):
            lines.append(f"📊 {prog['summary']}")
        nxt = d.get("next_node")
        if nxt:
            lines.append(f"Next node: {nxt}")
        err = d.get("error_summary")
        if err:
            lines.append(f"Error: {err}")
        if not compiled and not counts:
            lines.append("No compiled output yet.")
    return "\n".join(lines)


def logs(d: dict[str, Any]) -> str:
    nodes = d.get("nodes") or []
    title = d.get("job_title") or d.get("job_id", "")
    lines = [f"**Logs — {_short_id(d.get('job_id',''))}** ({d.get('job_status','?')}), {d.get('node_count', len(nodes))} nodes"]
    if not nodes:
        lines.append("No per-node log entries.")
    for n in nodes[:15]:
        key = n.get("node_key") or n.get("key") or "?"
        st = n.get("status") or "?"
        t = n.get("title") or ""
        lines.append(f"- {key} [{st}] {t}".rstrip())
    return "\n".join(lines)


def cost(d: dict[str, Any]) -> str:
    lines = [
        f"**Cost — `{_short_id(d.get('job_id',''))}`**",
        f"- ${d.get('total_cost_usd', 0):.4f} · {d.get('call_count', 0)} calls",
        f"- {d.get('total_prompt_tokens', 0)} prompt + {d.get('total_completion_tokens', 0)} completion tokens",
    ]
    for p in (d.get("by_provider") or [])[:6]:
        lines.append(
            f"  · {p.get('provider')}/{p.get('model')}: {p.get('calls')} calls, "
            f"${p.get('cost_usd', 0):.4f}"
        )
    return "\n".join(lines)


def config(d: dict[str, Any]) -> str:
    count = d.get("count")
    redacted = d.get("redacted")
    fields = d.get("fields") or []
    lines = [f"**Engine config** — {count if count is not None else len(fields)} fields"]
    if redacted:
        lines.append(f"({redacted} redacted)")
    return " ".join(lines)


def delete_result(kind: str, ref: str, status_code: int, body: Any) -> str:
    if status_code in (200, 204):
        return f"Deleted {kind} `{ref}`."
    detail = body.get("detail") if isinstance(body, dict) else body
    return f"Could not delete {kind} `{ref}` (HTTP {status_code}): {detail}"


HELP = (
    "**I can drive the engine by plain language.** Try:\n"
    "- \"what's running\" / \"show my jobs\" / \"how did the <name> job turn out\"\n"
    "- \"show the logs for <job>\" / \"what did <job> cost\"\n"
    "- \"search my notes for <topic>\" (knowledge base)\n"
    "- \"research <topic>\" (I'll confirm before the run)\n"
    "- \"delete the <name> job\" (I'll confirm first)\n"
    "- \"engine health\" / \"config\"\n"
    "Describe a build (\"set up proxmox on my box\") and I'll scope it with you instead."
)
