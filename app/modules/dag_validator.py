"""DAG tool-pick validator.

Sprint W.3 — Tier 1 / item 3 of the workflow-quality audit.

Given a freshly-generated DAG, runs a second-pass LLM call that audits each
task's ``tool`` selection against the documented rules (the same rules baked
into ``dag_generator.DAG_SYSTEM``) and returns a list of issues. The DAG
generator uses the issue list to drive a strict-prompt retry loop.

Fail-open: if the validator LLM call errors, the response is malformed JSON,
or the return shape is unexpected, this function returns an empty list. That
preserves the legacy single-shot behavior on validator failure rather than
crashing DAG generation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app import model_router
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold.dag_validator")


@dataclass
class ToolIssue:
    """One wrong-tool finding from the validator."""
    node_id: str
    current_tool: str
    proposed_tool: str
    reason: str

    def render(self) -> str:
        return (
            f"- {self.node_id}: tool='{self.current_tool}' should be "
            f"'{self.proposed_tool}'. {self.reason}"
        )


@dataclass
class ValidatorOutcome:
    """Result of one validator pass."""
    issues: list[ToolIssue] = field(default_factory=list)
    error: str | None = None  # populated only when validator failed open


VALIDATOR_SYSTEM = """You are a DAG quality auditor. Given a list of decomposed tasks, identify any tool selections that violate the rules.

TOOL RULES (must match dag_generator):
- Milvus = ALWAYS use when the task involves the knowledge base, KB, internal docs, TOON files, or domain-specific lookup. Any mention of "knowledge base", "KB", "look up from", "retrieve from", or stored/internal knowledge MUST use Milvus, NEVER SearXNG.
- SearXNG = web search for EXTERNAL, current, or live information NOT in the knowledge base.
- CodeGen = the deliverable IS executable code. The node produces a working script, function, module, or class as its primary output. Do NOT use CodeGen for: listing file extensions, naming variables, designing schemas, choosing libraries, writing documentation, listing requirements, or describing what code should do. If the deliverable is a list, plan, decision, design doc, or explanation — even one ABOUT code — use LLM, not CodeGen.
- Shell = the deliverable is an action performed on a host or external system: installing software, configuring services, modifying files on a target machine, enforcing firewall rules, starting/stopping containers, setting up networking. Any task whose verb is install / configure / deploy / set up / enforce / start / stop / restart against a host MUST be Shell, NEVER LLM.
- LLM = general reasoning, summarization, analysis, planning, listing, decision-making, design, explanation, and documentation. LLM produces text only — it cannot execute commands. If a node tagged LLM has a name like "Install X", "Configure Y", "Deploy Z", "Enforce W" — flag it: proposed_tool should be Shell (or CodeGen if a single self-contained script is the natural deliverable).

SCOPE DISCIPLINE (§17.363 + §17.367 — also audit for this):
- A node's `outputs` field must match the literal scope of its `name`. If a
  node named "Install Proxmox VE" has outputs like "fully deployed homelab"
  or "all 4 LXCs running", flag it — that's scope inflation.
- If two or more nodes have `outputs` fields that overlap substantially
  (e.g., both claim to produce "the LXC containers" or both produce "the
  Tailscale setup"), flag the one whose `name` does NOT align with that
  output. The other one keeps the work.
- A node whose `notes` describes work outside its name's scope (a node
  named "Configure VLAN bridges" whose notes mention installing Jellyfin,
  setting up Tailscale, etc.) is scope-inflated — flag it.
- (§17.367 + §17.370) CodeGen verbs follow the same rule. A node named
  "Write CLI interface" produces ONLY the entry-point; outputs like
  "complete CLI tool" or "working extractor" inflate scope into what
  should be sibling parser / test nodes. §17.370: more specifically, a
  "Write CLI" node's output must NOT re-define functions that an
  upstream sibling already exported. If T_parser ("Implement parser")
  exports `extract_blocks`, T_cli's output must IMPORT `extract_blocks`,
  not contain a second `def extract_blocks` or a renamed
  reimplementation. The same applies to LANG_EXT (if a T_decision
  upstream picked the languages, T_cli imports it) and to
  generate_filename (if a T_gen sibling exports it). Flag the CLI as
  scope-inflated when its output contains function definitions whose
  names match upstream sibling exports.
- A node named "Implement <module>" produces a library/module; outputs
  that include `def main()` or `argparse.ArgumentParser` are
  scope-inflated. A node named "Write unit tests for X" produces a
  test file that imports X; outputs that include the implementation of
  X are scope-inflated (tests import; they do not re-stub).

For scope issues, use proposed_tool = current_tool (the tool itself isn't
wrong; the scope is). Put the scope diagnosis in `reason`. The generator
reads the issue list and re-decomposes with tighter scopes.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "issues": [
    {"node_id": "T3", "current_tool": "CodeGen", "proposed_tool": "LLM", "reason": "Task is documentation, not executable code."},
    {"node_id": "T2", "current_tool": "Shell", "proposed_tool": "Shell", "reason": "Scope inflation: name is 'Configure VLAN bridges' but outputs include 'all 4 LXCs running'. Trim outputs to bridges only; LXC creation belongs to the downstream 'Create LXC containers' node."}
  ]
}

Rules for your audit:
- Only flag clear violations grounded in the rules above. If a tool pick is defensible, do NOT flag it.
- "proposed_tool" must be one of: LLM, SearXNG, Milvus, CodeGen, Shell.
- Return an empty issues list if every tool pick is correct AND every node's scope matches its name.
- Return ONLY the JSON object. No preamble, no markdown."""


VALIDATOR_PROMPT = """Audit the tool picks in this DAG:

{dag_json}

Return ONLY the JSON object."""


async def validate_tool_picks(
    tasks: list[dict],
    *,
    model_role: str = "model_general",
    model_overrides: dict | None = None,
    max_tokens: int = 3072,
    empty_redraws: int = 2,
) -> ValidatorOutcome:
    """Run the validator LLM and return findings.

    Args:
        tasks: parsed DAG tasks (each must have ``id`` and ``tool``).
        model_role: model_router role to dispatch under. Defaults to
            model_general so the validator shares the DAG generator's
            session-valve choice.
        model_overrides: optional per-call model override dict.
        max_tokens: response cap. Validator output is short JSON; 1024 is
            generous.

    Returns:
        ValidatorOutcome with ``issues`` populated and ``error`` None on
        success; or empty ``issues`` and a non-None ``error`` on any
        failure (LLM call, JSON parse, schema mismatch).
    """
    if not tasks:
        return ValidatorOutcome()

    # Build a stripped-down view of the DAG for the validator. We send only
    # the fields it needs to judge: id, name, tool, notes (intent hints).
    audit_view = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "tool": t.get("tool"),
            "notes": t.get("notes", ""),
        }
        for t in tasks
    ]

    import json
    prompt = VALIDATOR_PROMPT.format(dag_json=json.dumps(audit_view, indent=2))

    route_kwargs = {"role": model_role}
    if model_overrides:
        route_kwargs["overrides"] = model_overrides

    # §17.665 — retry-on-empty. The validator role (model_general →
    # qwen3.5:397b-cloud) is a *thinking* model that can return success=True with
    # EMPTY content (budget spent on the <think> block); parse_json_object then
    # yields None and the validator silently fails open (skips the audit). Mirror
    # the §17.463 generator fix: re-draw up to `empty_redraws`+1 times on an
    # empty/unparseable SUCCESSFUL response. A hard failure (success=False) or a
    # call exception is surfaced immediately — no wasted draws on a down model.
    draws = max(1, int(empty_redraws) + 1)
    parsed = None
    resp = None
    for d in range(draws):
        try:
            resp = await model_router.generate(
                prompt,
                system=VALIDATOR_SYSTEM,
                temperature=0.1,
                max_tokens=max_tokens,
                **route_kwargs,
            )
        except Exception as exc:
            logger.warning("dag_validator_call_failed: %s", exc)
            return ValidatorOutcome(error=f"call_failed: {exc}")

        if not resp.success:
            logger.warning("dag_validator_response_unsuccessful: %s", resp.error)
            return ValidatorOutcome(error=f"response_unsuccessful: {resp.error}")

        parsed = parse_json_object(resp.text)
        if parsed is not None:
            break
        logger.warning(
            "dag_validator_redraw_on_empty: draw=%d/%d text_len=%d "
            "(thinking-model empty content, §17.665)",
            d + 1, draws, len(resp.text or ""),
        )

    if parsed is None:
        logger.warning(
            "dag_validator_json_parse_failed: raw=%r",
            (resp.text[:200] if resp and resp.text else ""),
        )
        return ValidatorOutcome(error="json_parse_failed")

    raw_issues = parsed.get("issues")
    if not isinstance(raw_issues, list):
        logger.warning(
            "dag_validator_schema_mismatch: parsed=%r", parsed,
        )
        return ValidatorOutcome(error="schema_mismatch")

    valid_tools = {"LLM", "SearXNG", "Milvus", "CodeGen", "Shell"}
    issues: list[ToolIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id", "")).strip()
        current = str(raw.get("current_tool", "")).strip()
        proposed = str(raw.get("proposed_tool", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        if not node_id or proposed not in valid_tools:
            # Skip ill-formed entries rather than failing the whole batch.
            continue
        if current == proposed:
            # §17.363 — same-tool suggestions are scope issues (tool is
            # correct; node's scope/outputs/notes are wrong). Keep them
            # only when the reason explicitly diagnoses scope, so the
            # retry loop sees the diagnosis and re-decomposes with tighter
            # outputs. Pre-§17.363 the no-op filter dropped every
            # scope finding silently because the validator can't propose
            # a different tool for scope inflation.
            #
            # Filter for positive diagnosis markers — a bare substring
            # check on "scope" matches negations like "no scope diagnosis"
            # or unrelated mentions ("out of scope"), so the rescue list
            # is the concrete phrases the VALIDATOR_SYSTEM teaches the
            # model to emit.
            reason_lower = reason.lower()
            scope_markers = (
                "scope inflation",
                "scope mismatch",
                "scope issue",
                "scope drift",
                "outside its scope",
                "outside the scope",
                "name mismatch",
            )
            if not any(m in reason_lower for m in scope_markers):
                continue
        issues.append(ToolIssue(
            node_id=node_id,
            current_tool=current,
            proposed_tool=proposed,
            reason=reason,
        ))

    return ValidatorOutcome(issues=issues)


def issue_set_signature(issues: list[ToolIssue]) -> tuple:
    """Stable signature of an issue set for circuit-breaker comparison.

    Two validator passes that find the *same* set of node_id+proposed_tool
    pairs mean the regenerator isn't taking the hint — break the loop.
    """
    return tuple(sorted((i.node_id, i.proposed_tool) for i in issues))


def render_corrections_block(issues: list[ToolIssue], attempt: int) -> str:
    """Build the strict-retry correction block prepended to DAG_PROMPT."""
    lines = [
        f"## Tool corrections needed (attempt {attempt})",
        "",
        "Your previous DAG had these tool-pick errors. Fix them in the next attempt:",
        "",
    ]
    lines.extend(i.render() for i in issues)
    lines.append("")
    lines.append("Re-decompose the same brief, applying the corrections above.")
    return "\n".join(lines)
