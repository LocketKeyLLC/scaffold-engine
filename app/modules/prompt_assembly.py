"""Shared upstream-last prompt assembly.

The autonomous executor (`execution_agent.execute_next_node`) and Assist
Mode (`assist_agent.assemble_step_context`) both produce the same
prompt for the same DAG node — the human walking through assist sees
exactly what the LLM would have seen. This module is the single
source of truth for that shape, so the two paths cannot drift.

Order of assembly (top-to-bottom in the final string):

    Upstream Node Outputs (mandatory, prepended)
    ---
    YOUR TASK (build on the upstream outputs above):
    <base prompt: template + project goal>
    <Tool-specific grounding block: Milvus / SearXNG / generic RAG>

This is the "upstream-last" invariant: the literal task instruction is
the LAST thing the model reads, while upstream context comes first so
the model is forced to ground its output in the actual upstream work
before producing the task deliverable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.config import settings

logger = logging.getLogger("scaffold.prompt_assembly")


EXECUTION_SYSTEM_LLM = """You are executing one node in a planned multi-step workflow.

Output rules:
- Direct, focused prose. No preamble, no recap of the task, no closing pleasantries.
- No markdown tables. No emoji. No horizontal rules. No fenced code blocks.
- Plain bullet lists allowed when listing concrete items. Bold sparingly.
- Headers allowed only when the output has 3+ distinct sections.
- Stay concise — produce only what the task asks for.
- Do not speculate beyond the task. Do not propose alternatives the task did not ask for.
- Do not editorialize ("Here\'s what we\'ll do," "Let me know if...", "Final verdict").

Capability boundary (§17.359):
- You cannot run commands, SSH into hosts, install software, edit files,
  or modify systems. You produce text only.
- If the task describes an action on a host or external system, frame your
  output as instructions for the human reader to perform, not a transcript
  claiming the action was performed. Do NOT write past-tense narration
  such as "Created the file", "Installed the package", "Verified with
  tcpdump that...", "Backup confirmed at /etc/...". If host action is the
  core deliverable, the DAG generator should have routed this to the Shell
  or CodeGen tool — flag the mismatch in your output rather than fabricate
  success.

No-fabrication guard (§17.360):
- Do NOT invent concrete values (IPs, hostnames, MAC addresses, ports,
  auth keys, API tokens, SSH keys, password hashes, container IDs,
  version numbers, dates, file paths, PCI addresses) that are not
  explicitly stated in the task, the project goal, the upstream
  outputs, or the ground truth. Plausible-looking specifics
  (`192.168.10.100`, `tskey-abc123def456ghi789`, `pve01.internal`,
  `0000:01:00.0`) are fabrication, not detail.
- If upstream outputs use a placeholder (`<PROXMOX_HOST_IP>`,
  `${VAR}`, `<...>`), preserve the placeholder verbatim. Do not fill
  it in with an invented example value.
- If a documentation or summary task lists fields that need values
  the brief did not supply, mark them with placeholders or list them
  under an "Inputs needed" section — the operator will fill them in.

If upstream context is provided, build on it. Do not rewrite or contradict upstream work.
If ground truth is provided, treat it as authoritative.

Produce the deliverable the task asks for. Nothing more."""

EXECUTION_SYSTEM_CODEGEN = """You are executing one node in a planned multi-step workflow that produces code.

Output rules:
- Lead with the code in a fenced block. Brief explanation after if needed (under 10 lines).
- No preamble before the code. No "here\'s a script that..." setup.
- One implementation, not multiple alternatives.
- No emoji. No checklists of features. No "let me know if you need..." closers.
- If the code depends on tools/libs, name them in one line before or after the code.

Capability boundary (§17.359):
- The fenced code block is the deliverable; you are NOT running it. Do not
  write past-tense narration as if the script had been executed ("Ran the
  script and got X", "Output confirmed Y"). The reader is the executor.

If upstream context is provided, build on it. Match its conventions.
If ground truth is provided, treat it as authoritative.

Produce working code that solves the task. Nothing more."""

EXECUTION_SYSTEM_RUNBOOK = """You are executing one node in a planned multi-step workflow whose deliverable is a runbook the human will perform on a host.

You do not have shell access. You produce instructions only. The human is the executor.

Output structure (in this order, omit sections that don\'t apply):
- ## Prerequisites — one bullet per requirement (already-installed package, env var, file present).
- ## Run this — numbered list of copy-paste-ready commands or file edits, one step per item. Use fenced code blocks for commands. Include only commands the human types; no commentary inside the block.
- ## Verify — one bullet per check, each pairing an expected outcome with the exact command the human runs to confirm it.
- ## Rollback — what to do if a step fails. Concrete commands, not advice.

Hard rules:
- Never write past-tense narration ("Created…", "Installed…", "Verified…", "tcpdump shows…", "Backup confirmed at…"). You have not done any of this.
- Never claim outputs you did not see ("Returned NVIDIA GPU", "Confirmed empty config").
- Never use checkmarks, success emoji, or "✅ Step N complete" — the human marks completion, not you.
- If the task requires information you don\'t have (host IP, current state, model name), say so explicitly under a "## Inputs needed" section rather than inventing it.
- If a step requires destructive action (rm, dd, format, drop database), call it out under "## Risk" before the Run this block.

If upstream context is provided, build on it. Do not rewrite or contradict upstream work.
If ground truth is provided, treat it as authoritative.

Produce the runbook the task asks for. Nothing more."""


def system_for_tool(tool: str) -> str:
    """Pick the appropriate system prompt for a node tool type.

    §17.359 — ``Shell`` joins the dispatch alongside ``CodeGen``. Case-
    insensitive: a hand-edited row carrying ``"shell"`` lands the same as
    canonical ``"Shell"``. The mirror in
    ``execution_agent._system_for_tool`` must stay in lockstep.
    """
    t = (tool or "").lower()
    if t == "codegen":
        return EXECUTION_SYSTEM_CODEGEN
    if t == "shell":
        return EXECUTION_SYSTEM_RUNBOOK
    return EXECUTION_SYSTEM_LLM


def truncate_output(content: str, max_chars: int) -> str:
    """Preserve first/last 20% with a marker in the middle. Bytes-safe."""
    if len(content) <= max_chars:
        return content
    keep = max_chars
    head_len = int(keep * 0.2)
    tail_len = int(keep * 0.2)
    removed = len(content) - head_len - tail_len
    return (
        content[:head_len]
        + f"\n[...truncated {removed} chars...]\n"
        + content[-tail_len:]
    )


def build_base_prompt(node: dict, brief: dict) -> str:
    """The bare task prompt, before grounding or upstream injection."""
    template = node.get("prompt_template") or ""
    title = node.get("title") or ""
    goal = (brief or {}).get("description", "") if brief else ""
    if not goal and brief:
        goals = brief.get("goals", [])
        goal = goals[0] if goals else ""
    if template:
        return f"{template}\n\nContext: {goal}"
    return (
        f"Execute this task: {title}\n\n"
        f"Project goal: {goal}\n\n"
        f"Produce a complete, actionable output for this task. "
        f"Base your response on the ground truth provided above where relevant."
    )


async def fetch_upstream_outputs(
    db, job_id: str, depends_on: list[str]
) -> dict[str, str]:
    """Map node_key -> output_text for completed upstream nodes."""
    if not depends_on:
        return {}
    rows = await db.execute(
        text(
            "SELECT node_key, output_text FROM dag_nodes "
            "WHERE job_id = :jid AND node_key = ANY(:keys) AND status = 'done'"
        ),
        {"jid": job_id, "keys": depends_on},
    )
    return {r.node_key: (r.output_text or "") for r in rows.fetchall()}


def truncate_upstream_outputs(
    upstream_outputs: dict[str, str],
    max_total_chars: int | None = None,
    min_chunk: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Proportionally truncate upstream outputs to fit within max_total_chars.

    Returns the (possibly truncated) dict and a list of node_keys that
    were truncated. Pure function — does not mutate the input dict.
    """
    if not upstream_outputs:
        return upstream_outputs, []
    cap = max_total_chars if max_total_chars is not None else settings.max_upstream_chars
    chunk_min = min_chunk if min_chunk is not None else settings.compile_output_min_chunk
    total = sum(len(v) for v in upstream_outputs.values())
    if total <= cap:
        return dict(upstream_outputs), []
    out = {}
    truncated = []
    for nk, txt in upstream_outputs.items():
        share = max(chunk_min, int(cap * len(txt) / total))
        if len(txt) > share:
            out[nk] = truncate_output(txt, share)
            truncated.append(nk)
        else:
            out[nk] = txt
    return out, truncated


def render_upstream_block(upstream_outputs: dict[str, str]) -> str:
    """Format upstream outputs as a header-section the LLM/human will read first.

    Returns "" when upstream is empty so callers can no-op the prepend.
    """
    if not upstream_outputs:
        return ""
    parts = [f"### {nk}\n{upstream_text}" for nk, upstream_text in upstream_outputs.items()]
    return (
        "## Upstream Node Outputs (MANDATORY CONTEXT — your output MUST build on and "
        "be consistent with this work)\n"
        + "\n\n".join(parts)
        + "\n\n---\n\n## YOUR TASK (build on the upstream outputs above — do NOT "
          "rewrite or contradict them):\n"
    )


@dataclass(frozen=True)
class StepContext:
    """All the pieces a human (or LLM) needs to execute one DAG node.

    `assembled_prompt` is the canonical upstream-last string the
    autonomous executor would feed to the model. `base_prompt`,
    `upstream_outputs`, `grounding`, and `system_prompt` are the
    components, broken out so Assist Mode can render them as separate
    chat sections rather than one wall of text.
    """
    node_key: str
    title: str
    tool: str
    domain: str | None
    system_prompt: str
    base_prompt: str
    upstream_outputs: dict[str, str]   # truncated copy
    upstream_truncated_keys: list[str]
    grounding: str                      # Milvus / SearXNG / generic RAG block
    grounding_kind: str | None          # "milvus" | "searxng" | "rag" | None
    assembled_prompt: str               # the upstream-last string


async def assemble_step_context(
    *,
    db,
    job_id: str,
    node: dict,
    brief: dict,
    fetch_grounding: Any | None = None,
) -> StepContext:
    """Build the complete upstream-last prompt context for one DAG node.

    `fetch_grounding` is an optional async callable that takes
    (tool, title, node_key, domain, brief) and returns
    (grounding_text, grounding_kind). Passing None skips grounding
    entirely (the assist-mode default — humans already have the
    knowledge in their head; surfacing the grounding pre-fetched would
    just be context noise unless explicitly requested).

    The autonomous executor passes a real fetch_grounding implementation
    so the assembled prompt matches what the LLM would have seen.
    """
    node_key = node["node_key"]
    title = node["title"]
    tool = node.get("tool", "LLM")
    domain = node.get("domain")
    depends_on = node.get("depends_on") or []

    upstream = await fetch_upstream_outputs(db, job_id, depends_on)
    upstream, truncated_keys = truncate_upstream_outputs(upstream)

    base_prompt = build_base_prompt(node, brief)

    grounding = ""
    grounding_kind = None
    if fetch_grounding is not None:
        grounding, grounding_kind = await fetch_grounding(
            tool=tool, title=title, node_key=node_key, domain=domain, brief=brief,
        )

    # Compose: base + grounding + upstream-prepend.
    body = base_prompt
    if grounding:
        if grounding_kind == "milvus":
            body = f"{body}\n\n## Knowledge Base Results\n{grounding}"
        elif grounding_kind == "searxng":
            body = f"{body}\n\n## Web Search Results\n{grounding}"
        else:
            body = (
                f"{body}\n\n"
                f"GROUND TRUTH (use this as authoritative reference):\n{grounding}"
            )

    upstream_block = render_upstream_block(upstream)
    assembled = upstream_block + body if upstream_block else body

    return StepContext(
        node_key=node_key,
        title=title,
        tool=tool,
        domain=domain,
        system_prompt=system_for_tool(tool),
        base_prompt=base_prompt,
        upstream_outputs=upstream,
        upstream_truncated_keys=truncated_keys,
        grounding=grounding,
        grounding_kind=grounding_kind,
        assembled_prompt=assembled,
    )
