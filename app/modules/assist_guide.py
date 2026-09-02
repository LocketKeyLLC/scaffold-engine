"""Assist Mode guidance layer (§17.486).

When a human claims a DAG step in Assist Mode, the engine should walk them
through *how to do it* — copy-paste terminal commands for shell/codegen work,
numbered step-by-step instructions for non-coding work — rather than handing
them the raw LLM ``prompt_template`` (an execution hint written for a model)
and saying "paste your output."

This module is the generator. It is pure logic + DB persistence; it does no
HTTP and owns no session lifecycle (that stays in ``assist_agent``). The
flow per step:

    1. (optional) research pre-pass — ask the model what facts a human would
       need to look up (versions, current flags, exact package names), then
       confirm each via the existing SearXNG / Milvus grounding and cite them.
    2. generate the walkthrough with a human-facing system prompt selected by
       the node's tool (shell → runbook, codegen → code+run, else → steps).
    3. persist the result on the owning ``assist_steps`` row so a re-view does
       not re-spend an LLM call. ``/assist guide`` regenerates with ``force``.

Why reuse rather than reinvent: the autonomous executor already has a
copy-paste runbook system prompt (``prompt_assembly.EXECUTION_SYSTEM_RUNBOOK``)
and grounding helpers (``execution_agent._searxng_search`` / ``_milvus_search``)
— this module composes them for the human-in-the-loop path. The generation
goes through ``chat_until_nonempty`` because the cloud thinking models can
return ``success=True`` with empty content when reasoning eats the token
budget (§17.465); a generous ``max_tokens`` plus retry-on-empty avoids it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app import model_router
from app.config import settings
from app.modules.prompt_assembly import (
    EXECUTION_SYSTEM_RUNBOOK,
    StepContext,
)
from app.utils.llm_retry import chat_until_nonempty
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.assist_guide")


# §17.855 — human-facing prompts moved to app/modules/assist_prompts.py;
# re-exported here so assist_guide.<NAME> and the tests keep resolving.
from app.modules.assist_prompts import (  # noqa: F401,E402
    _AUDIENCE_FRAMING,
    _PACING_FRAMING,
    _TARGET_SAFETY_FRAMING,
    _RUNBOOK_HUMAN_FRAMING,
    _HEADING_META_RULE,
    GUIDE_SYSTEM_CODEGEN,
    GUIDE_SYSTEM_NONCODE,
    GUIDE_SYSTEM_DECISION,
    GUIDE_SYSTEM_FIX,
    _FIX_USER_TRAILER,
    _GUIDE_USER_TRAILER,
    _GUIDE_DECISION_TRAILER,
)
# §17.771 (deferred, now done) — render-path suggestion validation. A DECISION
# step's first-view walkthrough MUST carry a recommendation; the audit found the
# model can silently drop the "## My suggestion" section with nothing catching it
# (the commit path is now decisive; this makes the render path so too). Present
# = a suggestion/recommendation heading OR a clear lean phrase; absent → enforce.
_SUGGESTION_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s*(?:my\s+)?(?:suggestion|recommendation)\b"
)
_SUGGESTION_LEAN_RE = re.compile(
    r"(?i)\b(?:i'?d\s+(?:lean|go\s+with|recommend|suggest|pick|choose)|"
    r"my\s+recommendation\b|i\s+(?:recommend|suggest)\b|i'?d\s+go\b)"
)


def _has_decision_suggestion(text: str) -> bool:
    """True when a decision walkthrough already carries a recommendation — a
    ``## My suggestion``/``## Recommendation`` heading OR a clear lean phrase."""
    t = text or ""
    return bool(_SUGGESTION_HEADING_RE.search(t) or _SUGGESTION_LEAN_RE.search(t))


_DECISION_SUGGESTION_SYSTEM = (
    "You are a hands-on co-pilot. The operator is on a DECISION step and you were "
    "shown the options already laid out for them. Recommend exactly ONE of those "
    "options and the single main reason it fits THIS operator's system and goal. "
    "It is a suggestion they can reject — decisive, but their call. Pick from the "
    "options given; do not invent a new one. Call recommend_option once."
)

# ── §17.851 — code-enforced placeholder resolution ─────────────────────────
# The §17.850 prompt rules alone did NOT stick (live evidence: the facts held
# "https://192.168.1.156:8006" verbatim and the walkthrough still emitted
# <PROXMOX_HOST_IP>) — the §17.668 lesson again: LLMs ignore prompt rules, so
# enforce in code. After generation: Layer 1 substitutes pinned values
# deterministically; Layer 2 maps leftovers against the facts ledger via one
# small tool-call (known value / suggested free-choice name / unknown);
# resolved values are AUTO-PINNED so the next run is Layer-1 deterministic and
# the operator sees + can edit them in the Pinned values panel. Fail-soft at
# every stage: worst case the original text ships unchanged.







_DECISION_SUGGESTION_TOOL = model_router.Tool(
    name="recommend_option",
    description=(
        "Recommend ONE of the options presented for the operator's decision, with "
        "the single main reason it fits their situation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "leaning": {
                "type": "string",
                "description": "Short label of the recommended option (one of those shown).",
            },
            "why": {
                "type": "string",
                "description": "The ONE main reason it fits, tailored to their system/goal.",
            },
        },
        "required": ["leaning", "why"],
    },
)


async def _generate_decision_suggestion(
    *, title: str, task_prompt: str, options_text: str,
    environment: Optional[dict], role: str,
) -> str:
    """Generate ONLY the ``## My suggestion`` block for a decision whose
    walkthrough omitted it, tailored to the options already produced + the
    operator's system. Returns the markdown block, or "" on any failure (the
    caller then ships the un-enforced walkthrough — fail-soft)."""
    env = render_environment_block(environment)
    user = (
        f"Decision: {title}\n\nWhat to decide:\n{(task_prompt or '')[:1000]}\n\n"
        + (f"{env.strip()}\n\n" if env.strip() else "")
        + f"The options presented to the operator:\n{(options_text or '')[:2000]}\n\n"
        "Recommend ONE of these and the single main reason. Call recommend_option."
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": _DECISION_SUGGESTION_SYSTEM},
                {"role": "user", "content": user},
            ],
            [_DECISION_SUGGESTION_TOOL],
            role=role,
            temperature=0.2,
            max_tokens=256,
            tool_choice="auto",
        )
    except Exception as exc:  # never block the walkthrough on the follow-up call
        logger.warning("assist_decision_suggestion_failed: %s", exc)
        return ""
    if not resp.success or not resp.tool_calls:
        return ""
    args = resp.tool_calls[0].arguments or {}
    leaning = (args.get("leaning") or "").strip()
    if not leaning:
        return ""
    why = (args.get("why") or "").strip().rstrip(". ").strip()
    if why:
        return f"## My suggestion\nI'd lean **{leaning}** — {why}. But it's your call."
    return f"## My suggestion\nI'd lean **{leaning}** — but it's your call."



# §17.856 — the system-prompt directive appliers moved to
# app/modules/assist_directives.py; re-exported here so assist_guide.<NAME> and
# the tests keep resolving. (Private *_DIRECTIVE/_FRAMING constants are re-exported
# too — tests/test_assist_guide.py reads assist_guide._PROBLEM_SOLVING_FRAMING /
# ._NEXT_CALLOUT_DIRECTIVE.)
from app.modules.assist_directives import (  # noqa: F401,E402
    VERBOSITY_LEVELS,
    apply_verbosity,
    guide_system_for_tool,
    apply_problem_solving,
    apply_next_callout,
    apply_ground_or_ask,
    apply_screen_grounding,
    apply_location_callout,
    promote_inline_commands,  # §17.897
    _PROBLEM_SOLVING_FRAMING,
    _NEXT_CALLOUT_DIRECTIVE,
    _GROUND_OR_ASK_DIRECTIVE,
    _SCREEN_GROUNDING_DIRECTIVE,
    _LOCATION_CALLOUT_DIRECTIVE,
)

# §17.856 — the block renderers moved to app/modules/assist_render.py;
# re-exported so assist_guide.<NAME> and the external callers keep resolving.
from app.modules.assist_render import (  # noqa: F401,E402
    render_environment_block,
    render_facts_block,
    render_operator_notes_block,
    _operator_reset_intent,
    render_session_memory,
    _render_memory_or_legacy,
    render_conversation_block,
    render_step_recap_block,
    render_project_recap_block,
    _recap_add,
    parse_recap,
    render_status_panel,
    _RESET_INTENT_PATTERNS,
    _RECAP_LABELS,
)

# §17.856 — placeholder resolution moved to app/modules/assist_placeholders.py;
# re-exported so assist_guide.<NAME> keeps resolving.
from app.modules.assist_placeholders import (  # noqa: F401,E402
    resolve_placeholders,
    find_placeholders,
    extract_substitutions,
    _PLACEHOLDER_TOKEN_RE,
    _UNSAFE_RESOLVER_VALUE_RE,
    _PLACEHOLDER_RESOLVER_TOOL,
    _PLACEHOLDER_RESOLVER_SYSTEM,
    _PLACEHOLDER_RE,
    _LEARN_SUBS_TOOL,
)

# §17.856 — the research subsystem moved to app/modules/assist_research_lib.py;
# re-exported so assist_guide.<NAME> and external callers keep resolving.
from app.modules.assist_research_lib import (  # noqa: F401,E402
    _is_useful_grounding,
    _detect_unknowns,
    _searxng_structured,
    _deep_web_sources,
    _confirm_query,
    _research_prepass,
    _render_research_block,
    research_one,
    _focus_web_query,
    _RESEARCH_SYNTH_SYSTEM,
    _FOCUS_QUERY_SYSTEM,
    _EMPTY_MARKERS,
    _FAILURE_PREFIXES,
    _FLAG_UNKNOWNS_TOOL,
)


# ── Research pre-pass (confirm unknowns) ──────────────────────────────────




























_FACTS_SWEEP_SYSTEM = (
    "You maintain a system-state ledger for a hands-on build. The operator has "
    "just declared a RESET / REBUILD — they are erasing or starting over a part of "
    "the system. Some recorded facts now describe the ABANDONED thing (the machine "
    "being destroyed and its problems, config that was wiped, guest-OS state that "
    "no longer exists) and must be RETRACTED so they stop misleading later steps. "
    "OTHER facts are DURABLE and must be KEPT: physical hardware, the host / "
    "hypervisor configuration, network and storage infrastructure that survives the "
    "rebuild, and any fact about the NEW system being built. Call "
    "report_superseded_facts with the indices of ONLY the superseded "
    "(abandoned-system) facts. Be precise and conservative: when unsure whether a "
    "fact survives the rebuild, KEEP it (do not retract). Never retract a fact "
    "about the host, the network/bridge, storage, or the new build."
)

_REPORT_SUPERSEDED_TOOL = model_router.Tool(
    name="report_superseded_facts",
    description=(
        "Report which recorded facts describe the ABANDONED system the operator's "
        "reset/rebuild supersedes, so they can be retracted from the ledger."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "superseded_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "The 0-based indices of facts that describe the abandoned "
                    "system (empty if none are superseded)."
                ),
            },
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["superseded_indices"],
    },
)


async def classify_superseded_facts(
    *, note_text: str, facts: list[str], role: str = "model_general",
) -> list[int]:
    """§17.755 — given a reset/rebuild note and the numbered facts ledger, return
    the indices of facts that describe the ABANDONED system (to retract). Durable
    host/network/storage/new-build facts are kept. Fail-soft → [] (retract nothing)
    so a flaky classifier never nukes the ledger."""
    facts = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not facts:
        return []
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _FACTS_SWEEP_SYSTEM},
                {"role": "user", "content": (
                    f"The operator just declared a reset/rebuild:\n"
                    f"\"{(note_text or '').strip()[:1500]}\"\n\n"
                    f"Currently recorded system facts (numbered):\n{numbered[:9000]}\n\n"
                    "Call report_superseded_facts with the indices of the "
                    "abandoned-system facts."
                )},
            ],
            tools=[_REPORT_SUPERSEDED_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # §17.583/727 — thinking model reasons before the tool call; a big
            # numbered-facts ledger needs a generous budget or it returns empty args.
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — a flaky sweep must never break note-taking
        logger.warning("assist_facts_sweep_classify_failed: %s", exc)
        return []
    args = read_tool_args(resp)
    idxs = (args or {}).get("superseded_indices") or []
    return sorted({i for i in idxs if isinstance(i, int) and 0 <= i < len(facts)})


_DURABLE_FACTS_SYSTEM = (
    "You maintain a SHARED infrastructure ledger for a multi-component build on ONE "
    "physical system (e.g. a homelab: several VMs/services on one Proxmox host). "
    "Given ONE component's observed facts, return the indices of facts that are "
    "DURABLE, cross-cutting INFRASTRUCTURE that OTHER components on the same system "
    "would reuse: physical hardware (CPU, RAM, disks, storage controllers, GPUs, "
    "PCI devices), host network topology (bridges, physical NICs, subnets, "
    "gateways, NAT, DNS, the host's own IP), storage (pools, volumes, filesystems, "
    "datastores), and host access (hypervisor URL, how to log in, versions). "
    "EXCLUDE: transient states (a link/device currently up or down, 'currently at "
    "screen X', a boot loop, an in-progress error, a value being waited on), and "
    "facts specific to ONE component's own workload (a particular VM's guest-OS "
    "state, one service's internal config) that a sibling would not reuse. When "
    "unsure whether a fact is durable SHARED infrastructure, EXCLUDE it — the goal "
    "is a clean shared baseline, not completeness."
)

_REPORT_DURABLE_TOOL = model_router.Tool(
    name="report_durable_facts",
    description=(
        "Report which of a component's facts are durable, cross-cutting "
        "infrastructure that sibling components on the same physical system reuse."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "durable_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "0-based indices of the durable shared-infrastructure facts "
                    "(empty if none qualify)."
                ),
            },
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["durable_indices"],
    },
)


async def classify_durable_facts(
    *, facts: list[str], role: str = "model_general",
) -> list[int] | None:
    """§17.759 — given a component's facts, return the indices of the DURABLE,
    cross-cutting infrastructure facts (shared host/network/storage/hardware) that
    sibling components should inherit — excluding transient states and
    component-specific detail. Returns ``None`` on a model/parse FAILURE (so the
    caller falls back to sharing all facts, the §17.757 behavior) vs ``[]`` when
    the model genuinely found no durable facts."""
    facts = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not facts:
        return []
    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _DURABLE_FACTS_SYSTEM},
                {"role": "user", "content": (
                    f"A component's observed system facts (numbered):\n{numbered[:9000]}\n\n"
                    "Call report_durable_facts with the indices of the durable "
                    "shared-infrastructure facts."
                )},
            ],
            tools=[_REPORT_DURABLE_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # §17.583/727 — a thinking model (model_general) reasons before the tool
            # call; a big numbered-facts prompt needs a generous budget or it returns
            # EMPTY tool args (observed: 40 facts at 2048 → no args → None).
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — never break sharing on a flaky classifier
        logger.warning("assist_durable_facts_classify_failed: %s", exc)
        return None
    args = read_tool_args(resp)
    if not args or "durable_indices" not in args:
        return None
    idxs = args.get("durable_indices") or []
    return sorted({i for i in idxs if isinstance(i, int) and 0 <= i < len(facts)})








# ── Success verification (§17.487 — did the submitted step actually work?) ─

_JUDGE_OUTCOME_TOOL = model_router.Tool(
    name="judge_step_outcome",
    description=(
        "Judge, from the operator's pasted evidence, whether THIS step's actual "
        "goal (the Task title) was achieved. Four outcomes:\n"
        "- 'succeeded': the evidence shows the step's DELIVERABLE is done (e.g. "
        "for 'Install the OS', a login prompt / `lsb_release` from inside the "
        "installed system; for 'Configure X', the applied config confirmed).\n"
        "- 'failed': a clear failure signal — error, traceback, non-zero exit, "
        "'command not found', 'permission denied', 'No such file'.\n"
        "- 'incomplete': the evidence shows the operator did SETUP or an EARLIER "
        "phase of this step but NOT the step's actual deliverable — e.g. 'Install "
        "the OS' but the evidence only downloads the installer ISO or sits at the "
        "boot/installer menu; 'Install the driver' but the evidence is the same "
        "download / a boot screen. Use this when the goal is affirmatively NOT "
        "reached yet — NOT for a valid alternative that DOES meet the goal when a "
        "hardware/software constraint rules out the literally-named method (that "
        "is 'succeeded').\n"
        "- 'unclear': you genuinely can't tell. \n"
        "Be conservative about 'incomplete' and 'failed': if the deliverable "
        "MIGHT be present, or you can't tell, use 'unclear' (not 'incomplete'). "
        "Judge against the TASK TITLE's goal, not merely 'is there an error'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string",
                        "enum": ["succeeded", "failed", "incomplete", "unclear"]},
            "reason": {"type": "string", "description": "One sentence, citing the signal."},
            "suggestion": {"type": "string",
                           "description": "If failed/incomplete: the concrete next move to finish the step."},
            "goal_met_via_alternative": {
                "type": "boolean",
                "description": (
                    "True ONLY when outcome='succeeded' AND the operator met the "
                    "step's GOAL via a DIFFERENT method than the task literally "
                    "named, because a hardware/software CONSTRAINT made the named "
                    "method impossible (e.g. the board's chip locks PWM to "
                    "automatic, so manual fan curves aren't possible, but "
                    "automatic control keeps temps safe). False for a "
                    "straightforward success done the planned way."),
            },
            "constraint": {
                "type": "string",
                "description": (
                    "If goal_met_via_alternative: the specific constraint that "
                    "ruled out the named method AND how the goal was met instead, "
                    "as one durable sentence the plan should carry forward (e.g. "
                    "'This board's NCT7904D locks pwm_enable to automatic, so "
                    "manual fan curves aren't possible; fan control is automatic "
                    "only and holds temps in range'). Empty otherwise."),
            },
        },
        "required": ["outcome", "reason"],
    },
)


async def _sandbox_codegen_check(evidence: str) -> Optional[dict]:
    """§17.491 — run pasted codegen evidence in the scaffold-coderunner sandbox.

    Reuses the executor's `codegen_exec_smoke` classifier (pass | skip | fail).
    Returns ``{verdict, reason}`` or None on any error (fail-soft → the caller
    falls back to the LLM verdict).
    """
    try:
        from app.sandbox.codegen_check import codegen_exec_smoke
        chk = await codegen_exec_smoke(evidence)
        return {"verdict": chk.verdict, "reason": chk.reason}
    except Exception as exc:
        logger.warning("assist_sandbox_check_failed: %s", exc)
        return None


# §17.688 — a DECISION node's deliverable is a CHOICE, not a finished artifact.
# The default verifier judged an operator's decision ("3 vlans", "option a")
# against the node's concrete-artifact task text ("Produce a table: VLAN ID,
# subnet/CIDR, DHCP scope, isolation rules") and returned 'failed' → the user
# who correctly answered the framed question got "⚠️ This may have failed." A
# decision step is judged on whether a clear, on-topic choice was made; the
# concrete artifact is applied by later implementer steps (e.g. T2 "Define VLAN
# plan" decides → T12 "Create VLAN interfaces" / T17 "Configure switch" apply).
_VERIFY_DECISION_SYSTEM = (
    "You verify whether a human operator MADE THE DECISION a planning step asked "
    "of them, from what they wrote. This step's deliverable is a CHOICE or a "
    "stated direction — NOT a finished artifact, command output, or a fully "
    "specified table/config. The concrete implementation (exact IDs, subnets, "
    "commands, config values) is produced by LATER steps that apply this "
    "decision. Return 'succeeded' when the operator expressed a clear, on-topic "
    "choice or direction for THIS decision (picked an option, stated a "
    "count/approach, or gave a constraint that settles it). Return 'failed' ONLY "
    "if the message is empty, off-topic, or explicitly refuses to decide. Return "
    "'unclear' only if you genuinely cannot tell. NEVER return 'failed' merely "
    "because the operator did not reproduce the full concrete artifact named in "
    "the task — for a decision step that is expected and correct."
)


async def verify_step_success(
    *, title: str, task_prompt: str, tool: str, evidence: str,
    environment: Optional[dict] = None, is_decision: bool = False,
) -> dict:
    """Judge whether pasted evidence indicates the step worked. Fail-soft.

    Returns ``{outcome, reason, suggestion, grounded_by}``. On any model/parse
    failure returns ``outcome='unclear'`` so verification never blocks a submit
    it couldn't assess.

    §17.491 — for ``codegen`` steps with the sandbox enabled, the pasted code is
    RUN first: a definite runtime error (`fail`) authoritatively overrides to
    ``failed`` and skips the LLM call; ``pass``/``skip`` fall through to the LLM
    (which judges task-fit — "it runs" is necessary, not sufficient).
    """
    role = settings.assist_guide_model_role

    # Sandbox pre-check (codegen only, sandbox on). Deterministic ground truth.
    # A decision step produces no code, so the sandbox path never applies.
    sandbox: Optional[dict] = None
    if (not is_decision) and (tool or "").lower() == "codegen" \
            and settings.codegen_execution_check_enabled \
            and (settings.coderunner_url or "").strip():
        sandbox = await _sandbox_codegen_check(evidence)
        if sandbox and sandbox["verdict"] == "fail":
            return {
                "outcome": "failed",
                "reason": sandbox["reason"],
                "suggestion": "Fix the runtime error and resubmit — `/assist fix <the error>` can help.",
                "grounded_by": "sandbox",
            }

    # §17.710b — verify grounds on the unified memory (facts help judge against
    # the real system) or the legacy env block, per the valve. No notes here.
    env_block = "\n\n".join(_render_memory_or_legacy(environment, None))
    if is_decision:  # §17.688 — judge the CHOICE, not the downstream artifact
        system = _VERIFY_DECISION_SYSTEM
        user = (
            f"Decision step: {title}\n\n"
            "What this decision is ultimately about (context — NOT a checklist the "
            "operator's answer must fully satisfy; later steps apply the concrete "
            f"details):\n{task_prompt}\n\n"
            + (f"{env_block}\n\n" if env_block else "")
            + f"Operator's decision / message for this step:\n{tail_keep(evidence)}\n\n"
            "Judge whether they made a clear, on-topic decision. Call judge_step_outcome."
        )
    else:
        system = (
            "You verify whether a human operator's step achieved ITS GOAL, from "
            "the output they pasted. Judge against the step's Task title and its "
            "underlying GOAL — not a literal method checklist, and not merely 'is "
            "there an error'. §17.731: if the evidence shows only SETUP or an "
            "earlier phase (e.g. the installer downloaded / the boot menu shown, "
            "but the OS not actually installed), the step is 'incomplete', not "
            "'succeeded'.\n"
            "§17.771 — CREDIT A VALID ALTERNATIVE: return 'succeeded' when the "
            "evidence shows the step's GOAL is met, EVEN IF via a different method "
            "than the task literally names — ESPECIALLY when the evidence also "
            "shows the operator hit a genuine hardware/software CONSTRAINT that "
            "makes the named method impossible (e.g. the task says 'manual PWM fan "
            "control via fancontrol' but the board's sensor chip locks PWM to "
            "automatic, and the evidence shows temperatures held safe under load). "
            "Do NOT return 'incomplete' merely because the exact named tool/method "
            "wasn't used, when the outcome the step exists for is demonstrably "
            "achieved. 'incomplete' is for when the GOAL ITSELF is not yet reached, "
            "NOT for a valid alternative path to the same goal.\n"
            "Stay conservative: 'failed' only on a clear failure signal; "
            "'incomplete' only when the goal is affirmatively NOT reached yet; "
            "'unclear' when you genuinely can't tell (do NOT guess 'incomplete' out "
            "of caution)."
        )
        user = (
            f"Task (this step's goal): {title}\n\n{task_prompt}\n\n"
            + (f"{env_block}\n\n" if env_block else "")
            + f"Operator's pasted evidence / output for this step:\n{tail_keep(evidence)}\n\n"
            "Does the evidence show THIS step's goal was achieved? Call judge_step_outcome."
        )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            [_JUDGE_OUTCOME_TOOL],
            role=role,
            temperature=0.1,
            max_tokens=1024,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.warning("assist_verify_step_failed: %s", exc)
        return {"outcome": "unclear", "reason": "verification unavailable",
                "suggestion": "", "grounded_by": "model"}
    if not resp.success or not resp.tool_calls:
        return {"outcome": "unclear", "reason": "verification unavailable",
                "suggestion": "", "grounded_by": "model"}
    args = resp.tool_calls[0].arguments or {}
    outcome = args.get("outcome")
    if outcome not in ("succeeded", "failed", "incomplete", "unclear"):
        outcome = "unclear"
    reason = (args.get("reason") or "").strip()
    # §17.491 — the code ran cleanly in the sandbox; the LLM judged task-fit.
    # Record that the success is sandbox-backed, not just a text judgment.
    grounded_by = "model"
    if sandbox and sandbox["verdict"] == "pass":
        grounded_by = "sandbox+model"
        if outcome == "succeeded":
            reason = (reason + " (code executed cleanly in the sandbox)").strip()
    # §17.771 — the constraint-adaptation signal (only meaningful on 'succeeded').
    via_alt = bool(args.get("goal_met_via_alternative")) and outcome == "succeeded"
    constraint = (args.get("constraint") or "").strip() if via_alt else ""
    return {
        "outcome": outcome,
        "reason": reason,
        "suggestion": (args.get("suggestion") or "").strip(),
        "grounded_by": grounded_by,
        "goal_met_via_alternative": via_alt and bool(constraint),
        "constraint": constraint,
    }


# ── Multi-turn decision deliberation (§17.689) ─────────────────────────────
# A decision node whose deliverable is a CONCRETE artifact (a VLAN table, a
# partition layout, a config set) is assembled ACROSS turns rather than
# committed on the operator's first partial answer. Each decision turn either
# proposes a specific artifact to confirm/adjust (needs_input) or, once the
# operator has confirmed / the choices fully determine it, emits the complete
# artifact to record (resolved). A simple binary decision resolves in one turn.

_DELIBERATE_TOOL = model_router.Tool(
    name="resolve_or_continue",
    description=(
        "Report whether the operator's decision is now fully settled (resolved) "
        "or needs another round (needs_input), with the operator-facing message "
        "and, when resolved, the concrete decision to record for later steps."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["needs_input", "resolved"]},
            "message": {
                "type": "string",
                "description": (
                    "Markdown shown to the operator. For needs_input: a SPECIFIC "
                    "concrete proposal they can accept or adjust, then one short "
                    "line inviting them to confirm or say what to change. For "
                    "resolved: a brief confirmation of what was decided."
                ),
            },
            "decision_record": {
                "type": "string",
                "description": (
                    "REQUIRED when resolved: the COMPLETE, self-contained concrete "
                    "artifact to persist as this step's output (e.g. the full table "
                    "with every field). Later steps build directly from this. Leave "
                    "empty for needs_input."
                ),
            },
        },
        "required": ["status", "message"],
    },
)

_DELIBERATE_SYSTEM_DECISION = """You help a human operator finalize ONE decision step whose deliverable is a CONCRETE artifact (a table / list / plan / config with specific values). You are given the step's task, the project context, and the conversation so far (including any proposal you already made). Call resolve_or_continue exactly once.

Choose the outcome:

- status="resolved" when the operator's choices now fully determine the concrete deliverable, OR the operator has CONFIRMED a proposal you made (e.g. "looks good", "yes", "go with that", "that works"). Emit `decision_record` = the COMPLETE concrete artifact, assembled from what the operator chose — every row/field the task asks for, self-contained, ready for later steps to build from.

- status="needs_input" when more is needed to make the deliverable concrete. In `message`, make a DECISIVE recommendation: lead with the ONE option you recommend and the single main reason it fits THIS operator's system and goal, then PROPOSE the specific, ready-to-use default that reflects it (real, usable values — take concrete values from the operator environment when given; use a clearly-labeled <PLACEHOLDER> ONLY for something only the operator can supply, e.g. their exact ISP-facing IP). Do not dump a neutral menu of equal options and ask them to pick — commit to a recommendation and let them override it. Frame it as your recommendation, not a settled fact ("I'd go with X — reason. Sound good, or change it?"). If a FOUNDATIONAL choice is still open, recommend one (with the real alternatives named) — do NOT fabricate past a choice the operator has not made.

Rules:
- Resolve as soon as the operator confirms — never force an extra round once they've said yes.
- When a Research section is present, GROUND the recommendation and any versions/package names/flags in it — prefer its current, system-specific facts over anything from memory (§17.729 currency). Do NOT copy the research depth into the message; the operator needs the recommendation, not the sources.
- Do NOT pre-assume a count, topology, or set the operator has not agreed to.
- GROUND on the observed system facts + project context above. NEVER assume the system is fresh, empty, or new: if the facts describe an EXISTING system, decide for THAT system; if a needed detail was not captured or a check was inconclusive (a command errored / returned empty), treat it as UNKNOWN and ask — do NOT invent a "fresh install" assumption to fill the gap. EXCEPTION (§17.714): if the session memory says the operator has CHANGED DIRECTION / chosen to reinstall or rebuild, follow THAT decision — a fresh start is then what they asked for, not an assumption; treat the earlier "observed facts" as describing the abandoned system.
- Never invent values the operator must own (real public IPs, ISP specifics) — use sensible, clearly-labeled defaults they can change.
- Keep `message` tight and skimmable. No preamble, no emoji, no completion checkmarks."""

# §17.690 — the SAME across-turns machinery for a GATHER step: one whose task
# asks the operator to PROVIDE several specific pieces of information (e.g.
# "Operator provides: exact model, disk inventory, GPU(s), NIC models"). The
# operator commonly supplies these ONE PORTION AT A TIME; the step used to
# commit on the first portion (the reported bug — disk inventory pasted, step
# committed with model/GPU/NIC still missing). Now it accumulates across turns
# and only resolves once every requested item is present.
_DELIBERATE_SYSTEM_GATHER = """You help a human operator complete ONE step whose deliverable is SPECIFIC INFORMATION they provide — the task lists exactly which items. You are given the step's task, the project context, and the conversation so far (which may already contain items the operator supplied in EARLIER turns). Call resolve_or_continue exactly once.

The operator often provides the requested items ONE PIECE AT A TIME across several messages. Your job is to accumulate them and know when the set is complete.

Choose the outcome:

- status="resolved" when EVERY item the task asks for has now been provided across the whole conversation (an item the operator marks as absent or unknown — "no GPU", "not sure of the NIC model", "N/A" — COUNTS as provided; do not block on it). Emit `decision_record` = the COMPLETE, self-contained record of ALL the gathered information, every requested item labeled, assembled from the entire conversation — ready for later steps to build from.

- status="needs_input" when one or more requested items are STILL missing. In `message`: briefly acknowledge what you have CAPTURED so far, then clearly list ONLY the specific items still MISSING, and invite the operator to provide the rest. They may answer a piece at a time.

Rules:
- Track the WHOLE conversation: information given in earlier turns still counts — NEVER re-ask for something already provided, and never discard earlier pieces.
- GROUND on the observed system facts + project context above — a fact captured from an earlier step (e.g. the audit) COUNTS as provided; do not re-ask for it, and never assume a fresh/empty system to skip an item.
- Resolve as soon as the full set is present (or explicitly marked absent/unknown) — do not force extra rounds.
- Do NOT invent or assume values the operator must supply — ask for them.
- Keep `message` tight and skimmable. No preamble, no emoji, no completion checkmarks."""

_DELIBERATE_SYSTEMS = {
    "decision": _DELIBERATE_SYSTEM_DECISION,
    "gather": _DELIBERATE_SYSTEM_GATHER,
}


async def deliberate_decision(
    *,
    title: str,
    task_prompt: str,
    environment: Optional[dict] = None,
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    conversation: Optional[str] = None,
    latest_message: str,
    kind: str = "decision",
    research_block: str = "",
) -> dict:
    """§17.689/§17.690 — one turn of a COLLECT step's deliberation.

    ``kind='decision'`` — the deliverable is a choice / concrete artifact the
    operator decides (§17.689). ``kind='gather'`` — the deliverable is specific
    information the operator supplies, often one portion at a time (§17.690).
    Both accumulate across turns and commit only when complete.

    Returns ``{status: 'needs_input'|'resolved'|'error', message, decision_record}``.
    Fail-soft: any model/parse failure returns ``status='error'`` so the caller
    can fall back to the plain single-turn commit rather than trapping the
    operator in a broken loop.
    """
    role = settings.assist_guide_model_role
    system = _DELIBERATE_SYSTEMS.get(kind, _DELIBERATE_SYSTEM_DECISION)
    lead = (
        f"Gather step: {title}\n\n"
        f"The information this step must collect from the operator:\n{task_prompt}"
        if kind == "gather" else
        f"Decision step: {title}\n\n"
        f"What this step must ultimately produce (the concrete deliverable):\n{task_prompt}"
    )
    parts = [lead]
    if job_digest and job_digest.strip():
        parts.append(job_digest.strip())
    # §17.710b — unified session memory (or legacy env + notes) per the valve.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    # §17.771 (Phase 3) — evidence-backed commit: current, system-specific facts
    # so the recommendation isn't drawn from stale model memory (the audit's
    # "commit path has zero fresh research" gap). Decision kind only; "" no-ops.
    if research_block and research_block.strip():
        parts.append(research_block.strip())
    if conversation and conversation.strip():
        parts.append(conversation.strip())
    parts.append(f"## Operator's latest message\n{(latest_message or '').strip()}")
    parts.append(
        "Decide whether this step is now resolved (complete) or needs another "
        "round, and call resolve_or_continue."
    )
    user = "\n\n".join(parts)
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            [_DELIBERATE_TOOL],
            role=role,
            temperature=0.2,
            max_tokens=settings.assist_guide_max_tokens,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — fall back to plain commit
        logger.warning("assist_deliberate_decision_failed: %s", exc)
        return {"status": "error", "message": "", "decision_record": ""}
    args = None
    if resp and resp.success and resp.tool_calls:
        args = resp.tool_calls[0].arguments or {}
    if not args:
        logger.warning("assist_deliberate_decision_unparsed title=%r", title)
        return {"status": "error", "message": "", "decision_record": ""}
    status = args.get("status")
    if status not in ("needs_input", "resolved"):
        return {"status": "error", "message": "", "decision_record": ""}
    return {
        "status": status,
        "message": (args.get("message") or "").strip(),
        "decision_record": (args.get("decision_record") or "").strip(),
    }


# ── Natural-language turn classification (§17.626) ─────────────────────────
# When a chat has an ACTIVE assist session, plain text is an intent, not a new
# idea. This classifier maps the message to one of the assist verbs so the
# operator drives the flow by talking. Obvious verbs are matched deterministically
# in the pipeline (no LLM); this handles the substantive/ambiguous messages —
# principally telling "I did it, here's the output" (submit) from "how do I do
# this?" (question) from "it broke with X" (fix), using the current step as context.

ASSIST_INTENTS = (
    "advance", "skip", "submit", "fix", "finalize", "pause",
    "handoff", "status", "explain_plan", "set_env", "set_verbosity",
    "ask", "question", "note",
)

_CLASSIFY_TURN_TOOL = model_router.Tool(
    name="classify_turn",
    description=(
        "Classify what the operator wants to do next in an interactive, "
        "step-by-step assist session. They are currently on ONE specific step "
        "(given). Choose exactly one intent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(ASSIST_INTENTS),
                "description": (
                    "advance = move on to the next step ('next', 'ok what's next', 'move on'). "
                    "skip = skip/pass on the current step. "
                    "submit = they DID the step or are reporting the result / pasting output / "
                    "stating the decision they made — record it and continue. "
                    "fix = they hit an error or something isn't working and want help recovering. "
                    "finalize = finish the whole job and see the compiled result ('show me the result', 'we're all done'). "
                    "pause = stop for now. "
                    "handoff = they want the ENGINE to do this step (or the rest) automatically for them "
                    "('you do it', 'run it for me', 'just handle the rest', 'automate this'). "
                    "status = they want their progress / where they are ('where am I', 'how many left', 'status'). "
                    "explain_plan = they want the whole plan / all the steps / the big picture ('what's the overall plan', 'show me all the steps'). "
                    "set_env = they're telling you about their machine/environment ('I'm on Ubuntu 24.04 with apt', 'my host IP is 10.0.0.5', 'I use bash'). "
                    "set_verbosity = they want more or less detail in the instructions ('explain more', 'be more detailed', 'just give me the commands', 'too verbose'). "
                    "ask = they want a real ANSWER or hands-on HELP with a task — not a re-rendering of the current step. Covers (a) a factual lookup ('what is X', 'latest version', 'is X safe', comparisons), (b) a project/design/planning question about their setup or another part of the plan ('which of my two machines should host WireGuard', 'how should the VLANs be laid out', 'do I need X for Y'), AND (c) a PRACTICAL HOW-TO they need help accomplishing — 'how do I connect the Proxmox server to my laptop', 'how do I set up X', 'help me get Y talking to Z', 'how to configure/connect/install W'. In every case a researched, project-aware answer with the actual steps helps more than re-showing the current step. If they want to DO something, need help, or want a real answer, it is ask — PIVOT and help them, even when it's a sub-task or a task other than the current step. "
                    "question = they want to clarify or ADJUST the WORDING of the CURRENT step as shown — what a part of it means, redo THIS step for a different OS, more detail on one bullet. Use this ONLY when they're asking about the step's own instructions in front of them. A how-to, a request for help DOING something, or a question about the wider project / a different task is ask, NOT question. Do NOT default here for how-to or help requests. "
                    "note = they are RECORDING a new requirement, constraint, preference, or decision to remember for the rest of the project — not asking a question and not reporting they did the current step ('also, I want a DMZ', 'remember I only have 2 NICs', 'note: everything must survive a reboot', 'from now on use 10.x addresses', 'add a backup step later'). These are additions to keep, not the current step's evidence. When in doubt between note and submit: if it states a fact/wish to carry FORWARD rather than the OUTCOME of the current step, it is note."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "When intent=submit: the operator's result to record for the "
                    "step (their pasted output, or a short restatement of the "
                    "decision/action they reported). Omit otherwise."
                ),
            },
            "error_text": {
                "type": "string",
                "description": (
                    "When intent=fix: the error or problem they described, as "
                    "concretely as they gave it. Omit otherwise."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "When intent=ask: the factual question to research, phrased "
                    "as a clear standalone query. Omit otherwise."
                ),
            },
            "note_text": {
                "type": "string",
                "description": (
                    "When intent=note: the requirement / constraint / preference "
                    "/ decision to remember, as a short standalone statement "
                    "(e.g. 'wants a DMZ segment', 'only 2 physical NICs "
                    "available', 'all VMs must survive a host reboot'). Omit otherwise."
                ),
            },
            "note_kind": {
                "type": "string",
                "enum": ["addition", "constraint", "preference", "decision", "note"],
                "description": (
                    "When intent=note: how to tag it — addition (a new thing to "
                    "include), constraint (a hard limit), preference (a soft "
                    "want), decision (a choice already made), or note (generic). "
                    "Omit otherwise."
                ),
            },
        },
        "required": ["intent"],
    },
)

_CLASSIFY_SYSTEM = (
    "You route messages in a hands-on assist session where a human operator is "
    "working through a plan ONE step at a time, with the engine as co-pilot. "
    "Given the current step and the operator's message, decide what they want.\n\n"
    "Key distinctions:\n"
    "- STATES a result/decision ('I picked ZFS', 'done, 0 errors', pasted output) → submit\n"
    "- reports a FAILURE ('it errored with…', 'not working', 'command not found') → fix\n"
    "- wants the ENGINE to do the work ('you do this', 'run it for me', 'handle the rest') → handoff\n"
    "- wants an ANSWER or HANDS-ON HELP with a task → ask. This covers a factual "
    "lookup ('difference between ZFS and LVM', 'latest Proxmox version', 'is ZFS "
    "safe on non-ECC'), a project/design question about THEIR setup or another part "
    "of the plan ('which of my two machines should host WireGuard', 'how should I "
    "lay out the VLANs', 'do I need a separate NIC'), AND a practical HOW-TO they "
    "need help doing ('how do I connect the Proxmox server to my laptop', 'how do I "
    "set up X', 'help me get Y talking to Z', 'how to configure/connect/install "
    "W'). If they want a real answer or help DOING something, it is ask — PIVOT and "
    "help, even for a sub-task or a task other than the current step.\n"
    "- wants to CLARIFY/ADJUST the WORDING of the CURRENT step as shown ('what does "
    "part 2 mean', 'redo this one for macOS', 'more detail on the third bullet') → "
    "question. ONLY when they're asking about the step's own instructions in front "
    "of them. A how-to, a request for help, or a question about a different task is "
    "ask, NOT question — do not default here for how-to/help.\n"
    "- tells you about their MACHINE ('I'm on Ubuntu 24.04', 'IP is 10.0.0.5') → set_env\n"
    "- RECORDS a new requirement/constraint/preference/decision to carry forward "
    "for the rest of the project, not tied to finishing the current step ('also I "
    "want a DMZ', 'remember I only have 2 NICs', 'from now on use 10.x') → note. "
    "Distinguish from submit: note carries a fact/wish FORWARD; submit reports the "
    "current step's OUTCOME.\n"
    "Call classify_turn exactly once."
)


_CLASSIFY_DECISION_HINT = (
    "\n\nTHIS STEP IS A DECISION (its deliverable is a CHOICE the operator "
    "makes). Treat the operator MAKING or CONFIRMING that choice as submit: "
    "picking an option, stating a count / approach / value ('3 vlans', 'go with "
    "ZFS'), OR confirming a proposal you offered ('looks good', 'yes', 'that "
    "works', 'go with that', 'perfect') → submit. Only route to ask when they "
    "want a real answer to a QUESTION (e.g. 'what's the difference between the "
    "options', 'what do you recommend and why') rather than stating their pick."
)


async def classify_turn(
    *, message: str, title: str, task_prompt: str, tool: str,
    role: str | None = None, conversation: str | None = None,
    is_decision: bool = False,
) -> dict:
    """Classify an operator's plain-language turn into an assist intent.

    Returns ``{"intent": <one of ASSIST_INTENTS>, "evidence": str,
    "error_text": str, "query": str}``. Fail-soft: on any model/parse error
    returns ``intent='question'`` so a flaky classifier degrades to the guide/
    refine behavior rather than misfiring a submit/skip/handoff.

    §17.689 — ``is_decision`` biases a made/confirmed CHOICE toward ``submit`` so
    it reaches the server-side decision-deliberation path (which assembles the
    concrete artifact across turns) instead of being read as a bare question.

    §17.687 — ``conversation`` (the recent back-and-forth) lets the classifier
    resolve a message whose intent depends on what was JUST said ("yes, that
    one", "tell me more about the program you suggested") — anaphora it cannot
    read from the current step alone."""
    role = role or settings.assist_classify_model_role
    fallback = {"intent": "question", "evidence": "", "error_text": "", "query": "",
                "note_text": "", "note_kind": "note"}
    convo_block = (
        f"{conversation.strip()}\n\n" if (conversation or "").strip() else ""
    )
    user = (
        f"Current step: {title}\n\n"
        f"What the step asks:\n{(task_prompt or '')[:1500]}\n\n"
        f"Tool for this step: {tool or 'LLM'}\n\n"
        f"{convo_block}"
        f"Operator's message:\n{message[:2000]}\n\n"
        "Call classify_turn."
    )
    system = _CLASSIFY_SYSTEM + (_CLASSIFY_DECISION_HINT if is_decision else "")
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            [_CLASSIFY_TURN_TOOL],
            role=role,
            temperature=0.0,
            max_tokens=512,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — never block the turn
        logger.warning("assist_classify_turn_failed: %s", exc)
        return fallback
    if not resp.success or not resp.tool_calls:
        return fallback
    args = resp.tool_calls[0].arguments or {}
    intent = args.get("intent")
    if intent not in ASSIST_INTENTS:
        return fallback
    note_kind = (args.get("note_kind") or "note").strip()
    return {
        "intent": intent,
        "evidence": (args.get("evidence") or "").strip(),
        "error_text": (args.get("error_text") or "").strip(),
        "query": (args.get("query") or "").strip(),
        "note_text": (args.get("note_text") or "").strip(),
        "note_kind": note_kind if note_kind in
        ("addition", "constraint", "preference", "decision", "note") else "note",
    }


# ── Auto-learn substitutions (§17.490 — concrete values from evidence) ─────








# ── Session facts ledger (§17.709 — durable observed system state) ──────────

_RECORD_FACTS_TOOL = model_router.Tool(
    name="record_facts",
    description=(
        "Record durable FACTS about the operator's ACTUAL system, observed in "
        "the command output they pasted, that LATER steps must know."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short, standalone factual statements about the operator's "
                    "REAL system — e.g. 'Existing Proxmox VE 9.2.6 (not a fresh "
                    "install)', 'Network: vmbr0 = 192.168.1.156/24, gw "
                    "192.168.1.1', 'Two ZFS pools: rpool, tank'. Rules: (1) only "
                    "state what the output actually shows; NEVER guess. (2) If a "
                    "command ERRORED or returned empty because a service was down "
                    "(e.g. 'Connection refused', 'command not found'), record that "
                    "the state is UNKNOWN/unverified — do NOT record 'none' or "
                    "'fresh' from a failed check. (3) Prefer facts about existing "
                    "software/versions, users, VMs/containers, storage/pools, and "
                    "network config. Return [] if the output shows nothing durable."
                ),
            },
            "superseded_facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "§17.725 — KNOWN facts (from the provided list) that this "
                    "output DIRECTLY CONTRADICTS, echoed VERBATIM, character for "
                    "character. Only a real conflict counts (the output shows the "
                    "opposite / a different value for the same thing). A "
                    "refinement, an addition, or a fact the output is silent on "
                    "is NOT superseded. Return [] when unsure."
                ),
            },
        },
        "required": ["facts"],
    },
)

_FACTS_SYSTEM = (
    "You extract durable facts about a human operator's ACTUAL system AND what "
    "they are BUILDING from the command output they pasted, so later steps ground "
    "on reality instead of assuming. Report only what the output shows; never "
    "guess.\n"
    "CAPTURE, specifically:\n"
    "- The BUILD SPEC the operator just created or configured — the concrete "
    "values that define their setup: names (VM/container/host/service names), "
    "resource allocations (RAM, cores, disk sizes), storage/pool choices, network "
    "(bridges, NICs, IPs, gateways, DNS), device assignments (PCI passthrough IDs, "
    "GPUs), OS/image/ISO and version choices, ports, users. E.g. a `qm create` "
    "that succeeds is the fact 'VM 100 (AI-VM) created: 4GB RAM, 2 cores, q35/OVMF, "
    "32G disk on local-lvm, NIC e1000 on DeFruscioBridge, ISO ubuntu-26.04, agent "
    "on'. These are the MOST important facts to keep — they are the operator's "
    "personal build.\n"
    "- Observed system STATE and blockers (what exists, what failed).\n"
    "PARTIAL ERRORS: a trailing or single error does NOT invalidate the parts that "
    "SUCCEEDED — capture the successful configuration AND, separately, record the "
    "error as its own fact (e.g. 'the `qm create` ran but `-bash: scsi0: command "
    "not found` — the boot-order `;` was unescaped so --agent/--boot may be "
    "incomplete'). Only treat an aspect as UNKNOWN when the check for THAT aspect "
    "errored or was blank — never infer a 'fresh'/'empty' system from a failure.\n"
    "If a KNOWN fact list is provided and this output directly contradicts one of "
    "those facts, echo that known fact VERBATIM in superseded_facts so the ledger "
    "can retract it — but only for a real conflict, never for an addition or "
    "refinement. Call record_facts exactly once."
)


def _match_superseded(raw: object, known_facts: list[str] | None) -> list[str]:
    """§17.725 — filter a model's ``superseded_facts`` echo down to entries that
    actually exist in the known ledger (normalized case/whitespace match),
    returning the LEDGER's spelling so retraction is an exact removal. A cap of
    5 bounds the damage a hallucinating model could do in one call."""
    if not raw or not isinstance(raw, list) or not known_facts:
        return []
    by_norm = {str(k).strip().lower(): str(k) for k in known_facts}
    out: list[str] = []
    for s in raw:
        hit = by_norm.get(str(s).strip().lower())
        if hit and hit not in out:
            out.append(hit)
        if len(out) >= 5:
            break
    return out


async def distill_facts(
    *, evidence: str, title: str = "", task_prompt: str = "",
    known_facts: list[str] | None = None, role: str = "model_general",
) -> dict:
    """§17.709 — distill durable facts about the operator's real system from
    their pasted evidence: ``{"facts": [str], "superseded": [str]}``.
    Reasoning/extraction task → ``model_general`` (NOT the verifier; cf.
    §17.677). Fail-soft → empty dict shape. Bounds each fact's length so a
    runaway model can't bloat the ledger. §17.725: when ``known_facts`` is
    given, the model may echo (verbatim) the known facts this output directly
    contradicts; ``superseded`` returns only echoes that match the ledger."""
    empty = {"facts": [], "superseded": []}
    if not (evidence or "").strip():
        return empty
    known_block = ""
    if known_facts:
        known_block = (
            "KNOWN FACTS (already in the ledger — do not repeat; if this output "
            "DIRECTLY CONTRADICTS one, echo it verbatim in superseded_facts):\n"
            + "\n".join(f"- {k}" for k in known_facts) + "\n\n"
        )
    user_msg = (
        (f"STEP: {title}\n" if title else "")
        + (f"TASK: {task_prompt}\n\n" if task_prompt else "\n")
        + known_block
        + f"Operator output:\n{tail_keep(evidence)}\n\nCall record_facts."
    )
    # §17.749 — model_general (deepseek-v4-pro) is a THINKING model: at
    # max_tokens=1024 it spends the whole budget reasoning and returns an EMPTY
    # tool call (§17.465/583/727), so fact capture intermittently recorded
    # NOTHING — a rich `qm create` submit distilled to zero facts, and the
    # operator's build spec never reached the ledger. Give it the 8192 floor
    # big-prompt tool calls need, and RE-DRAW when the model fails to call the
    # tool at all (an empty-content flake) rather than losing the facts silently.
    args = None
    for _draw in range(3):
        try:
            resp = await model_router.tool_call(
                messages=[
                    {"role": "system", "content": _FACTS_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=[_RECORD_FACTS_TOOL],
                role=role,
                temperature=0.0,
                tool_choice="auto",
                max_tokens=8192,
            )
        except Exception as exc:  # noqa: BLE001 — fact capture must never break submit
            logger.warning("assist_distill_facts_failed: %s", exc)
            return empty
        args = read_tool_args(resp)
        if args is not None:  # the model made the tool call (facts may be [])
            break
        logger.info("assist_distill_facts_empty_redraw draw=%d/3", _draw + 1)
    raw = (args or {}).get("facts") or []
    if not isinstance(raw, list):
        return empty
    out: list[str] = []
    for f in raw:
        t = str(f).strip()
        if t:
            out.append(t[:300])
    return {
        "facts": out,
        "superseded": _match_superseded((args or {}).get("superseded_facts"), known_facts),
    }


# ── Per-step progress recap (§17.738 — stay coherent over a long step) ──────

_STEP_RECAP_SYSTEM = (
    "You keep a running recap of ONE step of a hands-on build, so the assistant "
    "and the operator don't lose the thread over a long back-and-forth. You are "
    "given the step's goal and the full transcript of work on it (operator "
    "messages + the assistant's replies). Write a SHORT recap with these labels, "
    "omitting any that are empty:\n"
    "GOAL: one line — what this step must achieve (its done-condition).\n"
    "DONE: sub-tasks already resolved in this step (bullet fragments).\n"
    "OPEN: what's still blocking / not yet working (bullet fragments) — be "
    "specific about the CURRENT blocker.\n"
    "CONSTRAINTS: hard limits on HOW we can proceed that later steps MUST respect "
    "— e.g. no copy-paste in this console, the guest agent is unavailable, "
    "GUI-only, offline-only, a login the operator doesn't have — PLUS approaches "
    "already tried and RULED OUT (don't retry them). One short fragment each; "
    "omit if there are none.\n"
    "NEXT: one line — the single most immediate action to take now, stated as an "
    "OBJECTIVE in plain words: WHAT to achieve and WHY, NOT the specific command "
    "to run. Describe the goal of the action (e.g. 'change VM 100's network card "
    "to the Intel E1000 model so the installer detects it, then restart the "
    "installer'), never a literal shell command line (do NOT write 'run "
    "`qm set …`'). A later step chooses the easiest TOOL for this objective — "
    "often a GUI — so the recap must stay tool-neutral and not bias it toward the "
    "CLI. Omit only if the step is fully done.\n"
    "CONTEXT: key state that's easy to lose — especially WHICH machine the next "
    "commands run on (host vs the VM/guest), IPs, filenames, and values already "
    "chosen.\n"
    "You may also be given the operator's recorded CONSTRAINTS/DECISIONS and "
    "observed system FACTS (from earlier steps / the durable ledger). Fold a hard "
    "limit or ruled-out approach into CONSTRAINTS, and durable system state (build "
    "spec, versions, IPs, what already exists) into CONTEXT — even if this step's "
    "transcript did not repeat it. But DONE/OPEN/NEXT still come ONLY from the "
    "transcript: a stated constraint or a system fact is NOT completed work, so "
    "never turn one into a DONE item or invent progress from it.\n"
    "Ground in the transcript (plus those recorded constraints/facts); never invent "
    "progress. Be terse (a compact status board, not prose). For OPEN and NEXT, "
    "describe blockers and "
    "objectives in tool-neutral plain words — the exact shell commands live in "
    "the transcript, not here; do NOT copy command lines into the recap (that "
    "would wrongly anchor the next answer to the CLI when a GUI is easier). If "
    "almost nothing has happened yet, a one-line GOAL is enough."
)


async def summarize_step_progress(
    *, title: str, transcript: str, role: str = "model_general",
    facts_block: str = "", notes_block: str = "",
) -> str:
    """§17.738 — a compact running recap of one step from its full transcript,
    so fix/guide/research stay on-thread over a long troubleshooting marathon
    (the 6-turn window loses it). Reasoning task → ``model_general``. Fail-soft
    → "" so callers thread it unconditionally.

    §17.752 — ``facts_block``/``notes_block`` (the durable ledgers) let the recap
    ground its CONSTRAINTS/CONTEXT in what the operator stated on EARLIER steps and
    in observed system facts, not just this node's transcript; DONE/OPEN/NEXT stay
    transcript-derived (see ``_STEP_RECAP_SYSTEM``)."""
    if not (transcript or "").strip():
        return ""
    ledger = ""
    if (notes_block or "").strip():
        ledger += f"\nOperator's recorded constraints/decisions:\n{notes_block.strip()}\n"
    if (facts_block or "").strip():
        ledger += f"\n{facts_block.strip()}\n"
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _STEP_RECAP_SYSTEM},
                {"role": "user", "content": (
                    f"Step goal: {title}\n"
                    f"{ledger}\n"
                    f"Transcript of work on this step (oldest first):\n{transcript[:12000]}\n\n"
                    "Write the recap."
                )},
            ],
            {"role": role},
            temperature=0.1,
            max_tokens=2048,   # thinking model clears reasoning before the recap
            draws=2,
            label="assist_step_recap",
            think_off_rescue=True,  # §17.876
        )
    except Exception as exc:  # noqa: BLE001 — a recap must never break the turn
        logger.warning("assist_summarize_step_progress_failed: %s", exc)
        return ""
    if resp and resp.success:
        return (resp.text or "").strip()[:2000]
    return ""




# ── §17.753 — the cross-step "living project recap" (§17.679) ───────────────
# The per-step recap above keeps ONE step coherent; the job digest (§17.650) dumps
# raw done-node outputs. This distills a compact, EVOLVING whole-project state board
# so guidance/pivot on step N reason with the arc — what earlier steps decided, what
# remains, cross-step constraints — instead of a single-node view or a raw dump.

_PROJECT_RECAP_SYSTEM = (
    "You keep a running recap of a WHOLE multi-step build, so the assistant and the "
    "operator never lose the arc across steps. You are given the project goal, the "
    "list of plan steps with their status (done / in-progress / pending / skipped) "
    "and a short summary of what each DONE step produced, plus the operator's "
    "recorded decisions/constraints and observed system facts. Write a SHORT "
    "whole-project state board with these labels, omitting any that are empty:\n"
    "GOAL: one line — the overall deliverable.\n"
    "DONE: what has been ACCOMPLISHED across steps, phase-level (not raw output) — "
    "bullet fragments.\n"
    "IN PROGRESS: the step(s) currently being worked, one fragment each.\n"
    "REMAINING: what still lies ahead, terse — the shape of the rest, not every "
    "step verbatim.\n"
    "DECISIONS: choices already locked in that later steps must stay consistent "
    "with (from the operator's decisions + what done steps established) — bullet "
    "fragments.\n"
    "CONSTRAINTS: hard limits that span the project (from the operator's stated "
    "constraints / preferences / ruled-out approaches). One fragment each.\n"
    "SYSTEM: durable facts about the operator's ACTUAL system (build spec, versions, "
    "hosts/IPs, what already exists) — ground on these; never assume a fresh/empty "
    "system.\n"
    "Ground ONLY in the given step statuses, done-step summaries, and the operator's "
    "decisions/constraints/facts; NEVER invent completion — a step is DONE only if "
    "its status says so. Be terse (a status board, not prose). Stay tool-neutral: "
    "describe WHAT each step achieved, not the specific commands. If almost nothing "
    "has happened yet, a one-line GOAL is enough."
)


async def summarize_project_progress(
    *, goal: str, nodes_block: str, facts_block: str = "", notes_block: str = "",
    role: str = "model_general",
) -> str:
    """§17.753 — a compact running recap of the WHOLE project from its step
    statuses + done-step summaries + the durable ledgers. Reasoning task →
    ``model_general``. Fail-soft → "" so callers thread it unconditionally."""
    if not (nodes_block or "").strip():
        return ""
    ledger = ""
    if (notes_block or "").strip():
        ledger += f"\nOperator's recorded decisions/constraints:\n{notes_block.strip()}\n"
    if (facts_block or "").strip():
        ledger += f"\n{facts_block.strip()}\n"
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            [
                {"role": "system", "content": _PROJECT_RECAP_SYSTEM},
                {"role": "user", "content": (
                    f"Project goal: {goal or '(untitled)'}\n"
                    f"{ledger}\n"
                    f"Plan steps (status + what each DONE step produced):\n{nodes_block[:12000]}\n\n"
                    "Write the whole-project state board."
                )},
            ],
            {"role": role},
            temperature=0.1,
            max_tokens=2048,   # thinking model clears reasoning before the board
            draws=2,
            label="assist_project_recap",
            think_off_rescue=True,  # §17.876
        )
    except Exception as exc:  # noqa: BLE001 — a recap must never break the turn
        logger.warning("assist_summarize_project_progress_failed: %s", exc)
        return ""
    if resp and resp.success:
        return (resp.text or "").strip()[:2500]
    return ""




# ── Operator-facing status panel + "do this next" callout (§17.741) ─────────
# The §17.738 recap above already distills a compact GOAL/DONE/OPEN/NEXT/CONTEXT
# status board — but only into the MODEL's prompt. These helpers turn it into a
# visible "📍 Where we are" panel the operator sees above each walkthrough, so a
# long problem-solving step reads as tracked rather than lost, and mandate a
# leading "👉 Do this next" section so the single immediate action draws the eye.









# ── Draft an inserted step (§17.736 — turn a foundational gap into a step) ──

_DRAFT_STEP_TOOL = model_router.Tool(
    name="draft_step",
    description=(
        "Draft ONE new plan step from the operator's request to add work the "
        "plan doesn't cover (e.g. a setup task a later step depends on)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "A short imperative step title, e.g. 'Configure the VM's "
                    "network for internet access'. No numbering."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "1-3 sentences stating the step's concrete GOAL and how it's "
                    "confirmed done (the deliverable), grounded in the project "
                    "context. No command list — the walkthrough is generated "
                    "separately."
                ),
            },
        },
        "required": ["title", "description"],
    },
)

_DRAFT_STEP_SYSTEM = (
    "You turn an operator's request into ONE concrete plan step for a "
    "human-in-the-loop build. Use the project context (its hosts, addresses, "
    "decisions) so the step is specific to THIS system, not generic. The step "
    "should be the smallest coherent unit that resolves the operator's need "
    "(e.g. 'Configure the VM's network for internet access', with a clear "
    "done-condition like 'the VM can ping an external host'). Call draft_step "
    "exactly once."
)


async def draft_step(
    *, request: str, job_context: str | None = None, role: str = "model_general",
) -> dict:
    """§17.736 — draft ``{title, description}`` for a step the operator asked to
    add (a foundational task the plan doesn't cover). Fail-soft: on any error
    returns a title/description derived from the raw request so the insert still
    succeeds."""
    req = (request or "").strip()
    fallback = {
        "title": (req[:80] or "Additional setup step"),
        "description": req[:400] or "Operator-requested step added mid-assist.",
    }
    if not req:
        return fallback
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _DRAFT_STEP_SYSTEM},
                {"role": "user", "content": (
                    (f"{job_context.strip()}\n\n" if (job_context or "").strip() else "")
                    + f"Operator's request for a new step:\n{req[:1000]}\n\n"
                    "Call draft_step."
                )},
            ],
            tools=[_DRAFT_STEP_TOOL],
            role=role,
            temperature=0.1,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — never block the insert on the draft
        logger.warning("assist_draft_step_failed: %s", exc)
        return fallback
    args = read_tool_args(resp) or {}
    title = str(args.get("title") or "").strip()[:120]
    desc = str(args.get("description") or "").strip()[:600]
    if not title:
        return fallback
    return {"title": title, "description": desc or fallback["description"]}


# ── Ledger consolidation (§17.727 — merge redundant same-truth facts) ───────

_CONSOLIDATE_TOOL = model_router.Tool(
    name="propose_fact_merges",
    description=(
        "Propose merges of REDUNDANT facts in a system-state ledger: groups of "
        "entries that state the same truth about the same subject, each with "
        "one replacement sentence carrying ALL their distinct details."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "merges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "replaces": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Indices (from the numbered list) of 2 or more "
                                "facts that state the SAME truth about the SAME "
                                "subject. Never group facts about different "
                                "subjects, and never group facts that add "
                                "distinct information unless the replacement "
                                "sentence carries every detail."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "ONE replacement sentence containing ALL the "
                                "distinct details from the group — nothing "
                                "dropped, nothing invented."
                            ),
                        },
                    },
                    "required": ["replaces", "text"],
                },
                "description": (
                    "Only clearly-redundant groups. A fact that is unsure, "
                    "unique, or only loosely related stays UNGROUPED (omit it "
                    "entirely — ungrouped facts are kept as-is). Return [] when "
                    "nothing is clearly redundant."
                ),
            }
        },
        "required": ["merges"],
    },
)

_CONSOLIDATE_SYSTEM = (
    "You tidy a ledger of observed facts about a human operator's real system. "
    "Multiple entries often state the SAME truth with different wording or "
    "detail level (e.g. the same storage pool described three ways). Propose "
    "merge groups: each group lists the indices of clearly-redundant entries "
    "plus ONE replacement sentence that preserves EVERY distinct detail from "
    "the group (sizes, versions, addresses, names, states). Rules: never merge "
    "entries about different subjects; never drop a detail; never invent one; "
    "an entry you are not sure about stays ungrouped (it is kept as-is). "
    "Conflicting entries are NOT redundant — leave conflicts ungrouped. Call "
    "propose_fact_merges exactly once."
)


async def consolidate_facts(
    facts: list[str], *, role: str = "model_general",
) -> list[dict]:
    """§17.727 — propose merges of redundant ledger facts. Returns validated
    groups ``[{"replaces": [fact-text, …], "text": merged}]`` (indices resolved
    to the ledger's texts so the caller can apply by VALUE, robust to a ledger
    that changed while the model was thinking). Validation is strict — out-of-
    range/duplicate indices dropped, groups need ≥2 distinct members, no fact
    in two groups, replacement text bounded — and fail-soft → []."""
    if not facts or len(facts) < 2:
        return []
    numbered = "\n".join(f"[{i}] {f}" for i, f in enumerate(facts))
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _CONSOLIDATE_SYSTEM},
                {"role": "user", "content": (
                    f"Ledger:\n{numbered}\n\nCall propose_fact_merges."
                )},
            ],
            tools=[_CONSOLIDATE_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            # A thinking model_general spends output tokens on reasoning BEFORE
            # the tool call, and a 37-fact ledger is a big prompt — 2048 starved
            # every draw into tool_call_empty_redraw (same pitfall as §17.583 /
            # the qwen3.5 empty-content lesson). Be generous here; this call is
            # rare (debounced, threshold-gated).
            max_tokens=8192,
        )
    except Exception as exc:  # noqa: BLE001 — tidying must never break anything
        logger.warning("assist_consolidate_facts_failed: %s", exc)
        return []
    args = read_tool_args(resp)
    raw = (args or {}).get("merges") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    used: set[int] = set()
    for m in raw:
        if not isinstance(m, dict):
            continue
        text_ = str(m.get("text") or "").strip()
        if not text_:
            continue
        idxs: list[int] = []
        for i in (m.get("replaces") or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(facts) and i not in used and i not in idxs:
                idxs.append(i)
        if len(idxs) < 2:
            continue  # a "merge" of one is a rewrite — not allowed
        if len(text_) > 600:
            # A replacement that long can't be truncated without losing the
            # details it exists to preserve — skip the group (originals kept).
            continue
        used.update(idxs)
        out.append({"replaces": [facts[i] for i in idxs], "text": text_})
    return out


# ── Unconditional per-turn derive (§17.715 — review + log EVERY message) ────

_RECORD_TURN_MEMORY_TOOL = model_router.Tool(
    name="record_turn_memory",
    description=(
        "Log any DURABLE, plan-relevant information the operator stated in this "
        "one message, so later steps retain it. Two buckets: plan notes and "
        "system facts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["decision", "constraint", "addition", "preference"],
                            "description": (
                                "decision = a choice/change of direction that "
                                "reshapes the plan ('let's do a fresh install "
                                "instead', 'drop the VPN'); constraint = a hard "
                                "limit ('only 2 NICs', 'must stay under $50'); "
                                "addition = a NEW requirement to fold in ('also add "
                                "a Palworld server'); preference = a soft "
                                "leaning ('I'd rather use WireGuard')."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "One self-contained sentence stating the note in "
                                "the third person ('Operator has decided to …'). "
                                "Standalone — do not reference 'this' or 'that'."
                            ),
                        },
                    },
                    "required": ["kind", "text"],
                },
                "description": (
                    "Plan-affecting statements the operator made THIS message. "
                    "Empty unless they actually decided/constrained/added/preferred "
                    "something new."
                ),
            },
            "facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Durable facts about the operator's REAL system stated in this "
                    "message (e.g. 'Router is a UniFi UDM at 192.168.1.1'). Only "
                    "what they actually said; never guess."
                ),
            },
            "superseded_facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "§17.725 — entries from ALREADY KNOWN FACTS that this message "
                    "DIRECTLY CONTRADICTS, echoed VERBATIM, character for "
                    "character. Only a real conflict counts (the operator states "
                    "the opposite / corrects the value). A refinement, an "
                    "addition, or a fact the message is silent on is NOT "
                    "superseded. Return [] when unsure."
                ),
            },
            "execution_context": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "Shell user, e.g. 'root'."},
                    "host": {"type": "string", "description": "Hostname, e.g. 'DeFruscio-HomeLab'."},
                },
                "description": (
                    "Set ONLY when the operator states, in prose, the shell "
                    "user@host they are NOW running commands on / logged into — "
                    "e.g. 'I'm on root@DeFruscio-HomeLab now', 'switched to "
                    "root@pve2'. This changes where later steps run. OMIT for a "
                    "passing mention, an email address, an example, or the host of "
                    "some OTHER machine they are not operating from. Requires BOTH "
                    "user and host; omit the whole object if either is unclear."
                ),
            },
        },
        "required": ["notes", "facts"],
    },
)

_TURN_MEMORY_SYSTEM = (
    "You are a memory scribe for a human-in-the-loop build session. Read ONE "
    "operator message and extract only DURABLE, plan-relevant information worth "
    "carrying into later steps: decisions / changes of direction, hard "
    "constraints, new requirements, stated preferences (as notes), and concrete "
    "facts about their real system (as facts). Be conservative and precise:\n"
    "- Return EMPTY notes and facts for a PURE question, request for help, "
    "acknowledgement ('ok', 'thanks'), small talk, or a refinement that does not "
    "change the plan.\n"
    "- A question can still CARRY durable information — when the operator states "
    "or corrects the state of their system inside a question (\"no, i am "
    "currently installing it, and have three options — which do I choose?\"), "
    "record that stated/corrected state as a fact even though the message asks "
    "something. Only the pure ask itself is not memory.\n"
    "- Do NOT restate anything already listed under ALREADY KNOWN — only NEW "
    "information the operator added in this message.\n"
    "- If the message DIRECTLY CONTRADICTS one of the ALREADY KNOWN FACTS (the "
    "operator states the opposite or corrects the value), echo that known fact "
    "verbatim in superseded_facts so the ledger can retract it. Only a real "
    "conflict — never an addition or refinement.\n"
    "- Never guess or infer beyond what the message says.\n"
    "Call record_turn_memory exactly once."
)


async def distill_turn_memory(
    *, message: str, known_notes: list[str] | None = None,
    known_facts: list[str] | None = None, role: str = "model_general",
) -> dict:
    """§17.715 — extract durable, plan-relevant memory from ONE operator message:
    ``{"notes": [{"kind","text"}], "facts": [str]}``. This is the unconditional
    review the trigger-gated pivot/note/facts paths miss. Reasoning/extraction →
    ``model_general`` (cf. §17.677). Conservative (empty for questions / chit-
    chat / step-refinements). ``known_*`` are folded into the prompt so the model
    does not restate standing memory. Fail-soft → ``{"notes": [], "facts": []}``."""
    empty = {"notes": [], "facts": [], "superseded": []}
    if not (message or "").strip():
        return empty
    known_lines = ""
    if known_notes:
        known_lines += "ALREADY KNOWN NOTES (do not repeat):\n" + "\n".join(
            f"- {k}" for k in known_notes
        ) + "\n\n"
    if known_facts:
        # §17.725 — facts listed separately so a contradiction can be echoed
        # verbatim into superseded_facts for retraction.
        known_lines += (
            "ALREADY KNOWN FACTS (do not repeat; if this message DIRECTLY "
            "CONTRADICTS one, echo it verbatim in superseded_facts):\n"
            + "\n".join(f"- {k}" for k in known_facts) + "\n\n"
        )
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _TURN_MEMORY_SYSTEM},
                {"role": "user", "content": (
                    known_lines
                    + f"Operator message:\n{message[:4000]}\n\nCall record_turn_memory."
                )},
            ],
            tools=[_RECORD_TURN_MEMORY_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — a scribe must never break the turn
        logger.warning("assist_distill_turn_memory_failed: %s", exc)
        return empty
    args = read_tool_args(resp)
    if not args:
        return empty
    notes_out: list[dict] = []
    for n in (args.get("notes") or []):
        if not isinstance(n, dict):
            continue
        t = str(n.get("text") or "").strip()
        k = str(n.get("kind") or "").strip().lower()
        if t and k in ("decision", "constraint", "addition", "preference"):
            notes_out.append({"kind": k, "text": t[:400]})
    facts_out: list[str] = []
    for f in (args.get("facts") or []):
        t = str(f).strip()
        if t:
            facts_out.append(t[:300])
    out = {
        "notes": notes_out,
        "facts": facts_out,
        # §17.725 — known facts this message directly contradicts (verbatim
        # ledger spellings only), for retraction at fold time.
        "superseded": _match_superseded(args.get("superseded_facts"), known_facts),
    }
    # §17.716 — an explicit prose statement of the operator's current shell host
    # (what the anchored prompt-line sensor can't see). Only when BOTH parts are
    # present; the caller re-validates + applies under the §17.703 retention rules.
    ec = args.get("execution_context")
    if isinstance(ec, dict):
        u, h = str(ec.get("user") or "").strip(), str(ec.get("host") or "").strip()
        if u and h:
            out["execution_context"] = {"user": u, "host": h}
    return out


# ── Grounding gate (§17.710c — warn when a result contradicts memory) ───────

_RECORD_GROUNDING_TOOL = model_router.Tool(
    name="record_grounding",
    description=(
        "Report whether the operator's step result CONTRADICTS what's already "
        "known about their system (the session memory)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "contradicts": {
                "type": "boolean",
                "description": (
                    "True ONLY if the result states or assumes something that "
                    "conflicts with a KNOWN fact — e.g. it assumes a fresh/empty "
                    "system when memory says an existing one, or names a value "
                    "that conflicts with a provided value. Adding NEW information, "
                    "normal progress, or anything memory doesn't speak to is NOT "
                    "a contradiction."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One short sentence naming the conflict. Omit if not contradicts.",
            },
        },
        "required": ["contradicts"],
    },
)

_GROUNDING_SYSTEM = (
    "You check whether a human operator's step result is CONSISTENT with what is "
    "already known about their system. You are given the session memory (observed "
    "facts, provided values, notes) and the operator's result for this step. Flag "
    "a contradiction ONLY when the result conflicts with a known fact — most "
    "often assuming a fresh/empty system when memory shows an existing one. Be "
    "conservative: adding new info or anything memory is silent on is NOT a "
    "contradiction; default to contradicts=false when unsure. §17.714: if the "
    "memory says the operator has CHANGED DIRECTION / chosen to reinstall or "
    "rebuild, a result that assumes a fresh system is CONSISTENT, not a "
    "contradiction. Call record_grounding once."
)


async def check_grounding(
    *, evidence: str, environment: dict | None,
    operator_notes: list[dict] | None = None, role: str = "model_general",
) -> dict:
    """§17.710c — warn-only grounding check. Does ``evidence`` contradict the
    session memory? Returns ``{contradicts: bool, reason: str}``. A reasoning
    task → ``model_general`` (not the verifier; cf. §17.677). Fail-soft →
    ``{contradicts: False}``; no-op (same) when there's no memory to check
    against."""
    memory = render_session_memory(environment, operator_notes)
    if not memory or not (evidence or "").strip():
        return {"contradicts": False}
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _GROUNDING_SYSTEM},
                {"role": "user", "content": (
                    f"{memory}\n\n## Operator's result for this step\n"
                    f"{tail_keep(evidence)}\n\nCall record_grounding."
                )},
            ],
            tools=[_RECORD_GROUNDING_TOOL],
            role=role,
            temperature=0.0,
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — a flaky gate must never block a submit
        logger.warning("assist_grounding_check_failed: %s", exc)
        return {"contradicts": False}
    args = read_tool_args(resp)
    if not args or "contradicts" not in args:
        return {"contradicts": False}
    return {
        "contradicts": bool(args.get("contradicts")),
        "reason": (args.get("reason") or "").strip(),
    }


# ── Destructive-command safety gate (§17.492) ──────────────────────────────

# High-confidence, command-context-anchored patterns only — a destructive
# verb in prose ("this removes the file") must NOT trip the gate; only an
# actual command form does. (compiled regex, human-readable why).
#
# §17.644 — two groups. COMMAND patterns anchor to the START of the command
# (via re.match after the prompt/sudo/env prefix is stripped): a real command
# leads with its tool name, so `parted /dev/sdb` fires but the prose/heading
# lines "Create a single partition with parted", "1. Open parted…", "4. Exit
# parted:" do NOT. The pre-§17.644 patterns used `\b<tool>\b` (word anywhere),
# so a bare mention of parted/fdisk/mkfs/dd/etc. in a sentence tripped the
# banner — crying wolf on nearly every shell step, which blunts the real
# warning (the §17.613 lesson, generalized from `rm` to every command verb).
# CONTENT patterns are not command-led (redirects, SQL, fork bomb), so they
# stay search-anywhere.
_DESTRUCTIVE_CMD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # §17.613 (audit #7) — require an actual dash-flag bearing r/f/R. The old
    # pattern needed no leading dash, so `rm file.conf` and even the safe
    # `rm -i file` tripped the gate — crying wolf trains operators to ignore it.
    (re.compile(r"rm\s+(-\S*\s+)*-\S*[rfR]"), "recursive/forced file deletion (rm -rf)"),
    (re.compile(r"dd\s+(if|of)="), "raw disk write (dd)"),
    (re.compile(r"mkfs(\.\w+)?\b"), "format filesystem (mkfs)"),
    (re.compile(r"wipefs\b"), "wipe filesystem signatures (wipefs)"),
    (re.compile(r"shred\b"), "secure file wipe (shred)"),
    (re.compile(r"(fdisk|parted|sgdisk)\b"), "partition-table edit"),
    (re.compile(r"chmod\s+-R\s+0?777\b"), "world-writable recursive chmod"),
    (re.compile(r"git\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+(-f|--force))"),
     "destructive git (hard reset / force push / clean -f)"),
    (re.compile(r"docker\s+(system\s+prune|volume\s+(rm|prune)|rm\s+-f)"), "docker resource removal"),
    (re.compile(r"kubectl\s+delete\b"), "kubectl delete"),
]
_DESTRUCTIVE_CONTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"--no-preserve-root"), "rm targeting / (--no-preserve-root)"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)"), "overwrite a block device"),
    (re.compile(r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE(\s+TABLE)?)\b", re.IGNORECASE),
     "destructive SQL (DROP/TRUNCATE)"),
    (re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE), "unfiltered SQL DELETE (no WHERE)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
]

# Leading prefixes that precede the actual command verb (so `sudo parted …` and
# `FOO=bar dd …` still anchor on parted/dd). Stripped before the CMD match.
_CMD_PREFIX_RE = re.compile(r"^(sudo\s+|doas\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def scan_destructive(text: str) -> list[dict]:
    """Deterministic scan for high-confidence destructive commands.

    Returns ``[{line, why}]`` (deduped by line; line truncated). Strips leading
    prompt/fence chars so ``$ rm -rf x`` matches. No LLM. Best-effort — this
    informs the operator, it does not block.

    §17.644 — command patterns anchor to the start of the command (after any
    prompt/`sudo`/env-var prefix) so a destructive verb appearing mid-sentence
    in prose or a numbered heading does not trip the banner.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("$#>` ").strip()
        if not line or line in seen:
            continue
        matched: Optional[str] = None
        # Command-led: strip sudo/doas/env prefixes, then require the tool at
        # the start (re.match anchors at position 0).
        cmd_line = _CMD_PREFIX_RE.sub("", line)
        for rx, why in _DESTRUCTIVE_CMD_PATTERNS:
            if rx.match(cmd_line):
                matched = why
                break
        # Content-led: high-confidence tokens that aren't command-first.
        if matched is None:
            for rx, why in _DESTRUCTIVE_CONTENT_PATTERNS:
                if rx.search(line):
                    matched = why
                    break
        if matched is not None:
            out.append({"line": line[:200], "why": matched})
            seen.add(line)
    return out


# ── Generation ───────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_guide_user_prompt(
    ctx: StepContext, node_description: Optional[str],
    sources: list[dict], refine_hint: Optional[str],
    environment: Optional[dict] = None,
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
) -> str:
    """Compose the user message: the same upstream-last task the executor
    would see, plus the operator environment, a project-wide digest of work
    already completed on the job (§17.650), the operator's captured notes &
    additions (§17.654), the recent conversation (§17.687), a confirmed-research
    block, and a human-walkthrough trailer (a decision-framing trailer when the
    node is a decision).
    """
    parts: list[str] = [ctx.assembled_prompt]
    if node_description and node_description.strip() and node_description.strip() not in ctx.assembled_prompt:
        parts.append(f"Task description: {node_description.strip()}")
    if job_digest and job_digest.strip():
        parts.append(job_digest.strip())
    # §17.710b — one injection path (unified session memory) or the legacy
    # env + notes blocks, per the assist_umem_inject valve.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    if conversation and conversation.strip():  # §17.687 — recent back-and-forth
        parts.append(conversation.strip())
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    parts.append(_GUIDE_DECISION_TRAILER if is_decision else _GUIDE_USER_TRAILER)
    if refine_hint and refine_hint.strip():
        parts.append(
            f"Operator refinement — apply this to the walkthrough: {refine_hint.strip()}"
        )
    return "\n\n".join(parts)


async def generate_guidance(
    *,
    ctx: StepContext,
    failed_commands: str = "",
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    node_key: str,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
) -> dict:
    """Generate (do not persist) the human walkthrough for one step.

    Returns ``{"guidance": str, "guidance_meta": dict, "status": str}``.
    ``status`` is ``"ready"`` when non-empty content was produced, else
    ``"failed"`` — never raises for an LLM/research failure (the caller shows
    a graceful fallback to the raw prompt).
    """
    role = settings.assist_guide_model_role

    sources: list[dict] = []
    if research:
        sources = await _research_prepass(
            task_text=ctx.base_prompt,
            tool=ctx.tool,
            role=role,
            max_queries=settings.assist_guide_max_research_queries,
            node_key=node_key,
            domain=domain,
            # §17.771 (Phase 3) — ground the option research in the operator's
            # actual system so a DECISION step's options are system-specific, not
            # a generic textbook list. render_environment_block folds in the
            # §17.709 facts ledger; "" when unknown (fail-soft, generic queries).
            environment_block=render_environment_block(environment),
        )

    system = apply_verbosity(
        guide_system_for_tool(ctx.tool, is_decision=is_decision), verbosity
    )
    system = apply_next_callout(  # §17.741 — lead with the immediate action
        system, is_decision=is_decision,
        enabled=settings.assist_next_callout_enabled,
    )
    system = apply_problem_solving(  # §17.742 — don't thrash on tangled steps
        system, enabled=settings.assist_problem_solving_enabled,
    )
    system = apply_ground_or_ask(  # §17.756 — placeholder + ask, never guess a value
        system, is_decision=is_decision,
        enabled=settings.assist_ground_or_ask_enabled,
    )
    system = apply_screen_grounding(  # §17.758 — confirm the on-screen state first
        system, is_decision=is_decision,
        enabled=settings.assist_screen_grounding_enabled,
    )
    system = apply_location_callout(  # §17.852 — say WHERE, announce switches
        system, is_decision=is_decision,
        enabled=settings.assist_location_callout_enabled,
    )
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
        job_digest=job_digest, operator_notes=operator_notes, is_decision=is_decision,
        conversation=conversation,
    )

    gen_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    resp = await chat_until_nonempty(
        model_router.chat,
        gen_messages,
        {"role": role},
        temperature=0.3,
        max_tokens=settings.assist_guide_max_tokens,
        draws=3,
        label="assist_guide",
        think_off_rescue=True,  # §17.876
    )

    text_out = (resp.text or "").strip() if (resp and resp.success) else ""
    # §17.893 — banned-value enforcement: redraw once with the violation named;
    # a still-dirty draft gets the visible flag below.
    banned_meta: list[dict] = []
    reskind_meta: list[dict] = []  # §17.898
    if text_out:
        text_out, banned_meta, _redrew = await enforce_banned_values(
            text_out=text_out, environment=environment, messages=gen_messages,
            role=role, label="assist_guide",
        )
        if banned_meta:
            text_out += banned_values_warning(banned_meta)
        # §17.898 — a VM addressed with `pct` (or a container with `qm`) is
        # always wrong and the confirmed facts already say which is which.
        text_out, reskind_meta, _rk = await enforce_resource_kinds(
            text_out=text_out, environment=environment, messages=gen_messages,
            role=role, label="assist_guide",
        )
        if reskind_meta:
            text_out += resource_kind_warning(reskind_meta)
    # §17.771 (deferred, now done) — render-path suggestion validation: on a
    # DECISION step, guarantee a recommendation. If the model laid out options but
    # dropped the "## My suggestion" lean, generate just that block from the
    # options it produced and append it. Only on a miss; fail-soft (append is ""
    # → ship the un-enforced walkthrough). Off by default (tests/fresh installs).
    if text_out:
        warn = guide_integrity_warning(text_out, user, failed_commands)
        if warn:
            logger.warning("assist_guide_integrity_flag node_key=%s", node_key)
            text_out += warn
    suggestion_enforced = False
    if (is_decision and text_out
            and settings.assist_decision_suggestion_enforce
            and not _has_decision_suggestion(text_out)):
        block = await _generate_decision_suggestion(
            title=ctx.title, task_prompt=ctx.base_prompt, options_text=text_out,
            environment=environment, role=role,
        )
        if block:
            text_out = f"{text_out}\n\n{block}"
            suggestion_enforced = True
            logger.info("assist_decision_suggestion_enforced node_key=%s", node_key)
    # §17.897 — every command the operator is handed must be copy-pasteable,
    # whichever path produced it. The fenced-block mandate is a prompt rule and
    # prompt rules get ignored; only a fenced block gets a ⧉ copy button.
    text_out = promote_inline_commands(text_out)
    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": getattr(resp, "model", "") if resp else "",
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "suggestion_enforced": suggestion_enforced,
        "status": status,
        "generated_at": _utcnow_iso(),
        # §17.492 — destructive-command safety gate.
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
        # §17.893 — banned values that survived the redraw (visibly flagged).
        "banned_value_violations": banned_meta,
        # §17.898 — wrong-resource-type commands that survived the redraw.
        "resource_kind_violations": reskind_meta,
    }
    if status == "failed":
        meta["error"] = (getattr(resp, "error", None) if resp else None) or "empty model output"
        logger.warning(
            "assist_guide_generation_empty node_key=%s tool=%s error=%s",
            node_key, ctx.tool, meta["error"],
        )
    return {"guidance": text_out, "guidance_meta": meta, "status": status}



def tail_keep(text_: str, cap: int = 6000) -> str:
    """§17.886 (audit #5) — the ONE evidence-truncation rule: keep the TAIL.
    Outcomes, tracebacks, and final states live at the END of pasted output;
    head-keep at these sites made the verifier judge download noise and miss
    the 'Installation complete' (or the closing traceback) entirely."""
    t = text_ or ""
    if len(t) <= cap:
        return t
    return "(earlier output truncated)\n…" + t[-cap:]


_LOCAL_HOST_RE = None


def _normalized_commands(text_: str) -> set[str]:
    """§17.882 — fenced commands + bare URLs from a walkthrough, normalized
    (whitespace-collapsed) for repeat detection.

    §17.882b — URLs on the operator's OWN hosts (localhost/127.x/RFC1918) are
    EXCLUDED from URL-level matching: they're verification endpoints (`curl
    localhost:7878`) that legitimately recur in every fix. Live false positive:
    an otherwise-correct method-changing regen got a repeat warning because it
    re-checked the same local health URL. External URLs (the guessed dead
    download hosts — the real signal) still match; identical whole fenced
    blocks still match regardless."""
    global _LOCAL_HOST_RE
    import re as _re
    if _LOCAL_HOST_RE is None:
        _LOCAL_HOST_RE = _re.compile(
            r"^https?://(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
            r"192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|\[::1\])"
            r"(?=[:/]|$)", _re.I,
        )
    out: set[str] = set()
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_ or "", _re.S):
        b = " ".join(block.split())
        if b:
            out.add(b)
    for url in _re.findall(r"https?://[^\s\"'`\)\]]+", text_ or ""):
        u = url.rstrip(".,;")
        if not _LOCAL_HOST_RE.match(u):
            out.add(u)
    return out


_VERSIONISH_RE = None


def _url_skeleton(url: str) -> tuple:
    """§17.883 — a URL's identity modulo version guessing: (host,
    frozenset(path segments with version-ish segments masked)). The live
    guess-cycle: releases/latest/download/R.tar.gz → download/v5.3.3/… →
    download/v5.3.0/… — three 'different' URLs, one failing endpoint family.
    Masking version segments and ignoring order makes them EQUAL."""
    global _VERSIONISH_RE
    import re as _re
    from urllib.parse import urlparse
    if _VERSIONISH_RE is None:
        _VERSIONISH_RE = _re.compile(r"^(v?\d[\w.\-]*|latest|master|main|stable|current)$", _re.I)
    try:
        p = urlparse(url)
        segs = frozenset(
            "~V~" if _VERSIONISH_RE.match(s) else s.lower()
            for s in p.path.split("/") if s
        )
        return (p.netloc.lower(), segs)
    except Exception:  # noqa: BLE001
        return ("", frozenset([url]))


def find_repeated_failed(text_out: str, failed_commands: str) -> list[str]:
    """§17.882/883 — deterministic repeat detection: which already-failed
    commands or URLs does this new walkthrough prescribe AGAIN — exactly, or
    as a version-guess VARIATION of the same failing endpoint family? The
    §17.882 exact matcher blocked identical repeats and the model responded by
    mutating the version tag three times; prompt rules are guidance, this is
    enforcement."""
    import re as _re
    if not (text_out or "").strip() or not (failed_commands or "").strip():
        return []
    new_cmds = _normalized_commands(text_out)
    old_cmds = _normalized_commands(f"```\n{failed_commands}\n```")
    hits = {c for c in new_cmds if c in old_cmds}
    # §17.883 — version-masked skeleton match on URLs only.
    old_urls = {u for u in old_cmds if u.startswith("http")}
    old_skels = {_url_skeleton(u) for u in old_urls}
    for c in new_cmds:
        for u in _re.findall(r"https?://[^\s\"'`\)\]]+", c):
            u = u.rstrip(".,;")
            if _url_skeleton(u) in old_skels and c not in hits:
                hits.add(c)
    return sorted(hits)


_CONSUMING_MARKERS = (" -o ", " -O", "wget ", "| sh", "| bash", "|sh", "|bash",
                      "git clone", "dpkg -i", "apt install", "apt-get install",
                      "pip install", "sh -c", "> /", "tee /")


def find_novel_urls(text_out: str, grounding_corpus: str) -> list[str]:
    """§17.883 — external URLs the draft tells the operator to CONSUME (download
    to disk, pipe to a shell, install from) that appear NOWHERE in its
    grounding (research block, playbook, conversation, the operator's own
    pasted output, the step task). A consumed URL with no provenance is a
    GUESS — the root disease behind today's cycles (radarr.video, then three
    invented GitHub version tags). READ-ONLY inspection URLs (a `curl -s` API
    query whose output the operator pastes back) are exempt — discovery is
    self-verifying and is exactly the behavior the gate's regeneration
    directive demands; flagging it (the first live proof-run did) would punish
    the cure. Local/RFC1918 URLs are exempt (the operator's own services)."""
    global _LOCAL_HOST_RE
    import re as _re
    if not (text_out or "").strip():
        return []
    _normalized_commands("")  # ensure _LOCAL_HOST_RE is built
    corpus = grounding_corpus or ""
    novel: list[str] = []
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_out, _re.S):
        flat = " ".join(block.split())
        if not any(m in flat for m in _CONSUMING_MARKERS):
            continue  # read-only / discovery command — exempt
        for url in _re.findall(r"https?://[^\s\"'`\)\]]+", block):
            u = url.rstrip(".,;")
            if _LOCAL_HOST_RE.match(u):
                continue
            if u not in corpus and u.rstrip("/") not in corpus and u not in novel:
                novel.append(u)
    return novel


def _error_focus_query(title: str, error_text: str) -> str:
    """§17.882 — a DETERMINISTIC research query from the actual error, so fix
    grounding never depends on the query-generator model's diligence (live: it
    emitted the same generic 'official installation guide' query 5 times).
    Step title (the program/context) + the first error-looking line."""
    import re as _re
    line = ""
    for ln in (error_text or "").splitlines():
        s = ln.strip()
        if s and _re.search(
            r"error|fail|not found|not in |unable|denied|refus|timeout|invalid|"
            r"cannot|no such|returned status|unexpected|corrupt|E:|curl: \(",
            s, _re.I,
        ):
            line = s
            break
    if not line:
        tail = [s.strip() for s in (error_text or "").splitlines() if s.strip()]
        line = tail[-1] if tail else ""
    line = _re.sub(r"\s+", " ", line)[:90]
    title_part = " ".join((title or "").split()[:6])
    return f"{title_part} {line}".strip()[:130]


def find_banned_values(text_out: str, banned: list | None) -> list[dict]:
    """§17.893 — deterministic banned-value detection. `banned` is the
    session's ``environment.banned_values`` list of {value, reason}: concrete
    identifiers the operator has explicitly ruled out for new use (live
    incident: 'DarthSidious' is the HP SWITCH's hostname; with the §17.892 pin
    removed and the constraint verbatim IN the prompt, the model still copied
    the name from the prior walkthrough in its conversation window into
    `qm create --name` — §17.882's lesson again: prompts are guidance, this is
    enforcement). Word-boundary, case-insensitive; values under 3 chars are
    ignored (too collision-prone to enforce)."""
    import re as _re
    if not (text_out or "").strip() or not banned:
        return []
    hits: list[dict] = []
    for b in banned:
        v = str((b or {}).get("value") or "").strip()
        if len(v) < 3:
            continue
        if _re.search(rf"(?<![\w-]){_re.escape(v)}(?![\w-])", text_out, _re.I):
            hits.append({"value": v, "reason": str((b or {}).get("reason") or "").strip()})
    return hits


# ── §17.898 — VM-vs-container resource-kind gate ─────────────────────────
#
# Proxmox addresses a VM with `qm` and an LXC container with `pct`, by numeric
# ID. Using the wrong verb is always an error, and the engine committed it live:
# facts recorded "VM 106 (palworld-server)", yet the guide prescribed `pct enter
# 106` three times across an ask and two guides. The session's other five
# resources (102-105) really are containers, so the container-shaped context
# out-voted the one fact that mattered. The confirmed facts are ground truth and
# this makes them binding — the §17.893 lesson: prompts are guidance,
# enforcement is code.
_VM_FACT_RE = None
_CT_FACT_RE = None
_VM_CMD_RE = None
_CT_CMD_RE = None


def resource_kinds_from_facts(environment: Optional[dict]) -> dict[str, str]:
    """Map Proxmox resource id → ``'vm'`` / ``'ct'`` from the confirmed facts.

    An id the facts describe BOTH ways is ambiguous and is dropped: a
    half-remembered fact must never become an enforcement rule."""
    global _VM_FACT_RE, _CT_FACT_RE
    import re as _re
    if _VM_FACT_RE is None:
        # "VM 106", "VM 106 (palworld-server)" — but not "VM/LXC 106".
        _VM_FACT_RE = _re.compile(r"(?<![\w/])VM\s+(\d{2,5})\b")
        # "container 103", "LXC container 104", "CT 107".
        _CT_FACT_RE = _re.compile(r"(?<![\w/])(?:LXC\s+)?(?:container|CT)\s+(\d{2,5})\b",
                                  _re.I)
    kinds: dict[str, str] = {}
    conflict: set[str] = set()
    for fact in (environment or {}).get("facts") or []:
        s = str(fact or "")
        for rid in _VM_FACT_RE.findall(s):
            if kinds.setdefault(rid, "vm") != "vm":
                conflict.add(rid)
        for rid in _CT_FACT_RE.findall(s):
            if kinds.setdefault(rid, "ct") != "ct":
                conflict.add(rid)
    for rid in conflict:
        kinds.pop(rid, None)
    return kinds


def find_resource_kind_violations(
    text_out: str, kinds: dict[str, str] | None,
) -> list[dict]:
    """§17.898 — fenced commands that address a resource with the WRONG verb.

    Returns ``[{id, used, correct, command}]``. Only fenced blocks are scanned:
    prose legitimately says "`pct` is for containers" while explaining the very
    mistake this gate catches, and flagging that would punish the correction."""
    global _VM_CMD_RE, _CT_CMD_RE
    import re as _re
    if not (text_out or "").strip() or not kinds:
        return []
    if _VM_CMD_RE is None:
        # `qm start 106`, `qm set 106 --…`, `qm resize 106 scsi0 +60G`
        _VM_CMD_RE = _re.compile(r"\bqm\s+(?:[a-z][\w-]*\s+)?(\d{2,5})\b")
        _CT_CMD_RE = _re.compile(r"\bpct\s+(?:[a-z][\w-]*\s+)?(\d{2,5})\b")
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_out, _re.S):
        for line in block.splitlines():
            for rx, used in ((_VM_CMD_RE, "vm"), (_CT_CMD_RE, "ct")):
                for rid in rx.findall(line):
                    want = kinds.get(rid)
                    if want and want != used and (rid, used) not in seen:
                        seen.add((rid, used))
                        hits.append({
                            "id": rid, "used": used, "correct": want,
                            "command": line.strip()[:120],
                        })
    return hits


def _kind_word(k: str) -> str:
    return "a VM (use `qm`)" if k == "vm" else "an LXC container (use `pct`)"


def resource_kind_warning(hits: list[dict]) -> str:
    """§17.898 — the visible flag when a redraw still addresses a resource with
    the wrong verb (the §17.883 honesty contract: gates guarantee VISIBILITY,
    not correctness)."""
    if not hits:
        return ""
    return ("\n\n---\n⚠️ **Wrong resource type:** this reply uses "
            + "; ".join(
                f"`{h['used']}` commands for {h['id']}, which your confirmed "
                f"facts record as {_kind_word(h['correct'])}"
                for h in hits[:3])
            + ". Do not run those commands as written.")


async def enforce_resource_kinds(
    *, text_out: str, environment: Optional[dict], messages: list[dict],
    role: str, label: str,
) -> tuple[str, list[dict], bool]:
    """§17.898 — redraw-once enforcement of the VM/container verb against the
    confirmed facts. Same contract as ``enforce_banned_values``: returns
    ``(text, remaining_hits, redrew)`` and fail-soft on any model failure."""
    kinds = resource_kinds_from_facts(environment)
    hits = find_resource_kind_violations(text_out, kinds)
    if not hits:
        return text_out, [], False
    logger.warning(
        "assist_resource_kind_violation label=%s hits=%s (regenerating)",
        label, ",".join(f"{h['id']}:{h['used']}->{h['correct']}" for h in hits),
    )
    directive = (
        "REGENERATION NOTICE: your draft addressed the wrong RESOURCE TYPE. "
        "Proxmox uses `qm` for VMs and `pct` for LXC containers; these ids are "
        "recorded in the confirmed facts as:\n"
        + "\n".join(
            f"- {h['id']} is {_kind_word(h['correct'])} — you wrote "
            f"`{h['command']}`" for h in hits[:5])
        + "\nRewrite every affected command with the correct tool for that "
          "resource type. If a step genuinely needs the other type, say so and "
          "explain — do not silently switch verbs. Output the corrected reply "
          "in full."
    )
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            list(messages) + [
                {"role": "assistant", "content": text_out},
                {"role": "user", "content": directive},
            ],
            {"role": role},
            temperature=0.3, max_tokens=settings.assist_guide_max_tokens,
            draws=2, label=f"{label}_reskind_regen", think_off_rescue=True,
        )
        new = (resp.text or "").strip() if (resp and resp.success) else ""
    except Exception:  # noqa: BLE001 — enforcement must not sink generation
        new = ""
    if not new:
        return text_out, hits, False
    return new, find_resource_kind_violations(new, kinds), True


def banned_values_warning(hits: list[dict]) -> str:
    """§17.893 — the visible flag when a redraw still carries a banned value
    (the §17.883 honesty contract: gates guarantee VISIBILITY, not
    correctness)."""
    if not hits:
        return ""
    return ("\n\n---\n⚠️ **Reserved value:** this walkthrough uses "
            + "; ".join(f"`{h['value']}`" + (f" ({h['reason']})" if h["reason"] else "")
                        for h in hits[:3])
            + " — the operator has ruled this value out for new use. Substitute "
              "your own value everywhere it appears before running anything.")


async def enforce_banned_values(
    *, text_out: str, environment: Optional[dict], messages: list[dict],
    role: str, label: str,
) -> tuple[str, list[dict], bool]:
    """§17.893 — redraw-once enforcement: if the draft uses a banned value,
    regenerate with the violation NAMED; a still-dirty redraw returns its
    remaining hits for the caller to flag visibly. Returns
    ``(text, remaining_hits, redrew)``. Fail-soft: any model failure keeps the
    original draft with its hits."""
    banned = (environment or {}).get("banned_values") or []
    hits = find_banned_values(text_out, banned)
    if not hits:
        return text_out, [], False
    logger.warning("assist_banned_value_violation label=%s values=%s (regenerating)",
                   label, ",".join(h["value"] for h in hits))
    directive = (
        "REGENERATION NOTICE: your draft used value(s) the operator has "
        "EXPLICITLY ruled out for new use:\n"
        + "\n".join(f"- `{h['value']}`" + (f" — {h['reason']}" if h["reason"] else "")
                    for h in hits[:5])
        + "\nReplace EVERY occurrence — commands, verification checks, prose — "
          "with a correct or freshly-chosen value that fits this step. Output "
          "the corrected walkthrough in full."
    )
    try:
        resp = await chat_until_nonempty(
            model_router.chat,
            list(messages) + [
                {"role": "assistant", "content": text_out},
                {"role": "user", "content": directive},
            ],
            {"role": role},
            temperature=0.3, max_tokens=settings.assist_guide_max_tokens,
            draws=2, label=f"{label}_banned_regen", think_off_rescue=True,
        )
        new = (resp.text or "").strip() if (resp and resp.success) else ""
    except Exception:  # noqa: BLE001 — enforcement must not sink generation
        new = ""
    if not new:
        return text_out, hits, False
    return new, find_banned_values(new, banned), True


def guide_integrity_warning(text_out: str, user_prompt: str, failed_commands: str) -> str:
    """§17.887 (audit #8) — the §17.882/883 gates for GUIDE output. Returns a
    warning block to append ("" when clean). Guides get flag-don't-regen: a
    visible warning beats doubled latency on every walkthrough, and the live
    radarr.video guessed-URL incident entered through a guide."""
    if not (text_out or "").strip():
        return ""
    bits = []
    if (failed_commands or "").strip():
        hits = find_repeated_failed(text_out, failed_commands)
        if hits:
            bits.append("re-prescribes something that already FAILED in this "
                        "session's troubleshooting (" +
                        "; ".join(f"`{h[:70]}`" for h in hits[:2]) + ")")
    novel = find_novel_urls(text_out, (user_prompt or "") + "\n" + (failed_commands or ""))
    if novel:
        bits.append("contains download URL(s) not traceable to research, the "
                    "playbook, or your own output (" +
                    "; ".join(f"`{n[:70]}`" for n in novel[:2]) + ") — verify before running")
    if not bits:
        return ""
    return ("\n\n---\n⚠️ **Integrity check:** this walkthrough " +
            " and ".join(bits) + ". Reply \"different approach\" to force a method change.")


async def generate_fix(
    *,
    ctx: StepContext,
    error_text: str,
    research: bool,
    environment: Optional[dict] = None,
    node_key: str,
    domain: Optional[str] = None,
    verbosity: str = "normal",
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    conversation: Optional[str] = None,
    failure_streak: int = 0,
    failed_commands: Optional[str] = None,
    prescribed_commands: Optional[str] = None,  # §17.898
) -> dict:
    """Diagnose an operator-reported error on a step and produce corrected steps.

    Conversational (not persisted). Reuses the research pre-pass with the error
    folded into the task text so unknown-detection surfaces error-specific
    lookups. Returns ``{"fix": str, "guidance_meta": dict, "status": str}``
    (``fix`` key so it can't be confused with persisted guidance). Fail-soft.
    """
    role = settings.assist_guide_model_role
    # §17.873 — cap the pasted error like every other evidence site ([:6000]
    # house pattern) but keep the TAIL: shell errors live at the end of long
    # output. Live incident: a 9.9k-char paste interpolated UNTRUNCATED (twice
    # — prepass task_text + the prompt section) starved the thinking model into
    # 3/3 empty draws (§17.465) → "(no fix returned)".
    if len(error_text or "") > 6000:
        error_text = "(earlier output truncated)\n…" + error_text[-6000:]

    # §17.881 — repeat-failure escalation. At the streak threshold the current
    # METHOD is failing, not just its last command: floor the research budget
    # (a single generic query re-fed the same weak grounding three fixes in a
    # row live) and demand a materially different approach below.
    escalated = failure_streak >= settings.assist_fix_streak_threshold
    sources: list[dict] = []
    if research:
        max_q = settings.assist_guide_max_research_queries
        if escalated:
            max_q = max(3, max_q)
        sources = await _research_prepass(
            task_text=f"{ctx.base_prompt}\n\nOperator hit this error:\n{error_text}"
                      + ("\n\n(Note: this is a REPEATED failure — previous fixes did "
                         "not resolve it; look up the current OFFICIAL method, not "
                         "variations of the failing one.)" if escalated else ""),
            tool=ctx.tool,
            role=role,
            max_queries=max_q,
            node_key=node_key,
            domain=domain,
            deep=True,  # §17.500 — troubleshooting wants real doc content, not snippets
        )
        # §17.882 — one DETERMINISTIC error-derived query, always. The
        # LLM query generator emitted the same generic query five fixes in a
        # row live; grounding on the ACTUAL error must not depend on it.
        try:
            from app.modules.assist_research_lib import _confirm_query
            eq = _error_focus_query(ctx.title, error_text)
            if eq and eq not in {s.get("query") for s in sources}:
                sources.extend(await _confirm_query(
                    eq, node_key=node_key, domain=domain, deep=True,
                ))
        except Exception as exc:  # noqa: BLE001 — extra grounding is fail-soft
            logger.debug("assist_fix_error_query_failed: %s", exc)

    parts = [ctx.assembled_prompt]
    if job_digest and job_digest.strip():   # §17.653 — project-wide context
        parts.append(job_digest.strip())
    # §17.745 — same unified session memory (facts + provided values + operator
    # notes, with §17.714 reset supersession) that guide/ask/decision inject.
    # Previously /fix rendered only the legacy env block (facts, no notes), so a
    # captured pivot or tool preference never reached the fix walkthrough.
    parts.extend(_render_memory_or_legacy(environment, operator_notes))
    if conversation and conversation.strip():  # §17.687 — recent back-and-forth
        parts.append(conversation.strip())
    parts.append(f"## Error the operator hit\n{error_text.strip()}")
    research_block = _render_research_block(sources)
    if research_block:
        parts.append(research_block)
    if (prescribed_commands or "").strip():
        # §17.898 — self-attribution. Without this the fix reconstructs blame
        # from the operator's paste alone and lands on "the error happened
        # because YOU used `pct enter`" for a command the ENGINE prescribed two
        # turns earlier. Owning the mistake is not politeness: a model that
        # thinks the operator improvised looks for operator error, while one
        # that knows it mis-prescribed looks at its own assumption — which is
        # where the actual bug (VM addressed as a container) was.
        parts.append(
            "## Commands YOU (the engine) prescribed on this step\n"
            "These came from your own earlier replies, newest first, tagged with "
            "the reply kind that issued them. If the operator's error is from one "
            "of these, it is YOUR command that failed: say so plainly ('the "
            "`pct enter` I gave you was wrong — 106 is a VM, not a container') "
            "and correct YOUR assumption. NEVER write 'the error happened because "
            "you used X' or 'you ran X' about a command in this list — the "
            "operator ran what you asked them to run. Equally, do NOT assume the "
            "operator did anything you never asked for: if a resource, service, "
            "or container does not appear in these commands or in the confirmed "
            "facts, it was never created, started, or configured.\n\n```\n"
            + prescribed_commands.strip()[:3000] + "\n```"
        )
    if failure_streak >= 1 and (failed_commands or "").strip():
        # §17.881/882 — from the FIRST repeat (the operator returning with an
        # error IS the proof the prior fix failed), the commands already tried
        # are placed LAST before the trailer (recency) with a hard directive.
        parts.append(
            "## Fixes already prescribed for THIS problem that did NOT resolve it\n"
            "The operator ran these (or was given them) and still hit the error. "
            "Do NOT prescribe them again, and do not prescribe a trivial variation "
            "of them. Produce a MATERIALLY DIFFERENT approach: prefer the session "
            "playbook's proven-here methods; if the research or playbook shows the "
            "whole method is wrong for this system, CHANGE THE METHOD — do not "
            "retune the failing command. After repeated failures your FIRST "
            "command must be a DISCOVERY command that PRINTS the ground truth "
            "(query the release API for the real URL, list the actual assets, "
            "curl -I the endpoint) — never another guessed download attempt; "
            "URLs you prescribe must appear VERBATIM in the research, the "
            "playbook, or the operator's own output.\n\n```\n"
            + failed_commands.strip()[:3000] + "\n```"
        )
    parts.append(_FIX_USER_TRAILER)
    user = "\n\n".join(parts)

    fix_system = apply_location_callout(  # §17.852
        apply_screen_grounding(  # §17.758
            apply_ground_or_ask(  # §17.756
                apply_problem_solving(  # §17.742
                    apply_next_callout(  # §17.741
                        apply_verbosity(GUIDE_SYSTEM_FIX, verbosity),
                        is_decision=False, enabled=settings.assist_next_callout_enabled),
                    enabled=settings.assist_problem_solving_enabled),
                is_decision=False, enabled=settings.assist_ground_or_ask_enabled),
            is_decision=False, enabled=settings.assist_screen_grounding_enabled),
        is_decision=False, enabled=settings.assist_location_callout_enabled)

    async def _draw_fix(messages):
        return await chat_until_nonempty(
            model_router.chat, messages, {"role": role},
            temperature=0.3, max_tokens=settings.assist_guide_max_tokens,
            draws=3, label="assist_fix", think_off_rescue=True,  # §17.876
        )

    resp = await _draw_fix([
        {"role": "system", "content": fix_system},
        {"role": "user", "content": user},
    ])
    text_out = (resp.text or "").strip() if (resp and resp.success) else ""

    # §17.882/883 — CODE-ENFORCED integrity gate, two deterministic checks:
    #   repeats    — already-failed commands/URLs, exactly OR as version-guess
    #                variations of the same endpoint family (§17.883 skeleton);
    #   novel URLs — external URLs with NO PROVENANCE (absent from research,
    #                playbook, conversation, the operator's output, the task).
    #                A URL from nowhere is a GUESS — the root disease behind
    #                today's cycles (radarr.video, then three invented GitHub
    #                version tags that each "passed" the exact-repeat check).
    # Violation → ONE regeneration with the violations NAMED and a
    # diagnose-first directive → still dirty → a visible warning leads the
    # answer so the operator is never silently handed a guess.
    repeat_meta: list[str] = []
    banned_meta_fix: list[dict] = []  # §17.893
    novel_meta: list[str] = []
    reskind_meta_fix: list[dict] = []  # §17.898

    _banned_list = (environment or {}).get("banned_values") or []
    _kinds = resource_kinds_from_facts(environment)  # §17.898

    def _gate(draft: str) -> tuple[list[str], list[str], list[dict], list[dict]]:
        hits_ = (find_repeated_failed(draft, failed_commands)
                 if failure_streak >= 1 and (failed_commands or "").strip() else [])
        novel_ = (find_novel_urls(draft, user + "\n" + (failed_commands or ""))
                  if failure_streak >= settings.assist_fix_streak_threshold else [])
        # §17.893 — banned values are banned at ANY streak.
        banned_ = find_banned_values(draft, _banned_list)
        # §17.898 — wrong resource verb is wrong at ANY streak, and a FIX is
        # exactly where it surfaced live (the fix that "corrected" pct enter
        # still had to be told 106 was a VM).
        reskind_ = find_resource_kind_violations(draft, _kinds)
        return hits_, novel_, banned_, reskind_

    if text_out:
        hits, novel, banned_hits, reskind_hits = _gate(text_out)
        if hits or novel or banned_hits or reskind_hits:
            logger.warning(
                "assist_fix_gate_violation node_key=%s repeats=%d novel_urls=%d "
                "banned=%d reskind=%d (regenerating)",
                node_key, len(hits), len(novel), len(banned_hits), len(reskind_hits),
            )
            directive = ["\n\n---\nREGENERATION NOTICE:"]
            if reskind_hits:
                directive.append(
                    "Your previous draft addressed the wrong RESOURCE TYPE "
                    "(`qm` is for VMs, `pct` is for LXC containers) — the "
                    "confirmed facts record:\n"
                    + "\n".join(
                        f"- {h['id']} is {_kind_word(h['correct'])} — you wrote "
                        f"`{h['command']}`" for h in reskind_hits[:5]))
            if banned_hits:
                directive.append(
                    "Your previous draft used value(s) the operator has EXPLICITLY "
                    "ruled out for new use — replace every occurrence:\n"
                    + "\n".join(f"- `{b['value']}`" + (f" — {b['reason']}" if b["reason"] else "")
                                for b in banned_hits[:5]))
            if hits:
                directive.append(
                    "Your previous draft prescribed command(s)/URL(s) that ALREADY "
                    "FAILED for this operator (exactly or as a version-guess "
                    "variation of the same failing endpoint):\n"
                    + "\n".join(f"- {h}" for h in hits[:5]))
            if novel:
                directive.append(
                    "Your previous draft prescribed URL(s) that appear NOWHERE in "
                    "the research, the playbook, or the operator's own output — "
                    "you may have invented them:\n"
                    + "\n".join(f"- {n}" for n in novel[:5]))
            directive.append(
                "STOP guessing. Lead with a DISCOVERY command whose OUTPUT prints "
                "the ground truth (e.g. query the project's release API and print "
                "the real download URL, `curl -I` the endpoint, list the actual "
                "assets) and have the operator paste it back — OR use only URLs "
                "that appear VERBATIM in the research/playbook/operator output.")
            regen = await _draw_fix([
                {"role": "system", "content": fix_system},
                {"role": "user", "content": user + "\n".join(directive)},
            ])
            regen_text = (regen.text or "").strip() if (regen and regen.success) else ""
            if regen_text:
                rehits, renovel, rebanned, rereskind = _gate(regen_text)
                if not rehits and not renovel and not rebanned and not rereskind:
                    text_out = regen_text
                else:
                    repeat_meta, novel_meta, banned_meta_fix = rehits, renovel, rebanned
                    reskind_meta_fix = rereskind
                    warn_bits = []
                    if rereskind:  # §17.898
                        warn_bits.append(
                            "addresses the wrong resource type ("
                            + "; ".join(
                                f"`{h['used']}` for {h['id']}, which is "
                                f"{_kind_word(h['correct'])}" for h in rereskind[:2])
                            + ")")
                    if rehits:
                        warn_bits.append(
                            "repeats something that already failed ("
                            + "; ".join(f"`{h[:70]}`" for h in rehits[:2]) + ")")
                    if renovel:
                        warn_bits.append(
                            "contains unverified URL(s) the engine could not trace "
                            "to any source (" + "; ".join(f"`{n[:70]}`" for n in renovel[:2]) + ")")
                    if rebanned:  # §17.893
                        warn_bits.append(
                            "uses a value the operator has ruled out ("
                            + "; ".join(f"`{b['value']}`" for b in rebanned[:2]) + ")")
                    text_out = (
                        "⚠️ **Caution:** this fix " + " and ".join(warn_bits)
                        + " — flagged twice by the integrity gate. Prefer its "
                        "diagnostic commands over its download commands, and reply "
                        "\"different approach\" to force a method change.\n\n"
                        + regen_text
                    )
            else:
                repeat_meta, novel_meta, banned_meta_fix = hits, novel, banned_hits
                reskind_meta_fix = reskind_hits  # §17.898
                text_out = (
                    "⚠️ **Caution:** this fix includes "
                    + ("a wrong-resource-type command " if reskind_hits else "")
                    + ("already-failed command(s) " if hits else "")
                    + ("unverified URL(s) " if novel else "")
                    + ("a ruled-out value " if banned_hits else "")
                    + "the integrity gate flagged. Treat with suspicion; reply "
                    "\"different approach\" to force a method change.\n\n" + text_out
                )
    # §17.897 — every command the operator is handed must be copy-pasteable,
    # whichever path produced it. The fenced-block mandate is a prompt rule and
    # prompt rules get ignored; only a fenced block gets a ⧉ copy button.
    text_out = promote_inline_commands(text_out)
    status = "ready" if text_out else "failed"
    meta: dict[str, Any] = {
        "model": getattr(resp, "model", "") if resp else "",
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "status": status,
        "generated_at": _utcnow_iso(),
        # §17.492 — destructive-command safety gate (fixes can carry rm/dd too).
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
        # §17.882/883 — violations that survived the regeneration gate (visible
        # warning was prepended; recorded here for triage/telemetry).
        "repeat_violations": repeat_meta,
        "novel_url_violations": novel_meta,
        "banned_value_violations": banned_meta_fix,  # §17.893
        "resource_kind_violations": reskind_meta_fix,  # §17.898
    }
    if status == "failed":
        meta["error"] = (getattr(resp, "error", None) if resp else None) or "empty model output"
    return {"fix": text_out, "guidance_meta": meta, "status": status}


# ── Persistence (cache write + idempotent read) ──────────────────────────


async def persist_guidance(
    *, session_id: str, node_key: str, guidance: str,
    guidance_meta: dict, status: str, db,
) -> None:
    await db.execute(
        text("""
            UPDATE assist_steps
               SET guidance = :g,
                   guidance_meta = CAST(:m AS jsonb),
                   guidance_status = :s,
                   guidance_generated_at = CASE
                       WHEN :s = 'ready' THEN NOW()
                       ELSE guidance_generated_at END,
                   updated_at = NOW()
             WHERE session_id = :sid AND node_key = :nk
        """),
        {
            "g": guidance or None,
            "m": json.dumps(guidance_meta),
            "s": status,
            "sid": session_id,
            "nk": node_key,
        },
    )
    await db.commit()


async def read_cached_guidance(
    *, session_id: str, node_key: str, db,
) -> Optional[dict]:
    """Return cached guidance only when a prior generation succeeded."""
    row = (await db.execute(
        text("""
            SELECT guidance, guidance_meta, guidance_status, guidance_generated_at
              FROM assist_steps
             WHERE session_id = :sid AND node_key = :nk
        """),
        {"sid": session_id, "nk": node_key},
    )).mappings().first()
    if not row or row["guidance_status"] != "ready" or not row["guidance"]:
        return None
    meta = row["guidance_meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    gen_at = row["guidance_generated_at"]
    return {
        "guidance": row["guidance"],
        "guidance_meta": meta or {},
        "status": "ready",
        "cached": True,
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else gen_at,
        "_generated_at_raw": gen_at,
    }


async def cached_guidance_is_stale(
    *, session_id: str, node_key: str, generated_at, db,
) -> bool:
    """§17.877 — cached guidance predating operator work on the step is stale.

    The live incident: "Guide me" hours into a troubleshooting marathon
    re-served the walkthrough cached at step-claim time ("pct start 102" for an
    already-running container) — the engine looked like it was repeating itself
    and ignoring everything the operator had done since. Any operator turn on
    the node NEWER than ``guidance_generated_at`` means the cached text was
    written without knowledge of that work. Fail-soft → False (serve cache).

    §17.894 — that probe was NODE-scoped, which misses the costlier failure:
    guidance written for a step that is reached much *later*, after the rest of
    the project moved on. Live incident — T23 ("Install PalWorld server") was
    guided at 02:44 while the plan still called for an LXC container; the plan
    was then re-planned so T22 created a QEMU **VM**; 20h later T23 was reached
    and the cached walkthrough was served verbatim, opening with
    ``pct enter 106`` against a container that never existed. There were no
    operator turns on T23 in that window, so node-scoped staleness saw nothing
    to invalidate and the engine looked like it had forgotten the VM it had
    just walked the operator through building.

    Two deterministic session-level signals are therefore checked as well:

    * **advanced** — another node on the job reached ``done``/``skipped`` after
      the guide was written, so the completed-work digest the guide was built
      from is out of date.
    * **replanned** — this node, or one of its ``depends_on``, was edited after
      the guide was written, so the guide may encode a superseded plan.

    Both are pure SQL over columns the assist path already maintains (verified
    on the live job: presenting a step does not touch ``dag_nodes.updated_at``,
    so a step being viewed does not self-invalidate). Instant re-views survive
    — with no operator turns, no commits and no plan edits since, the cache
    still hits."""
    if not generated_at or not settings.assist_guide_stale_cache_refresh:
        return False
    try:
        row = (await db.execute(
            text("""
                SELECT
                  (SELECT count(*) FROM assist_turns t
                    WHERE t.session_id = :sid AND t.node_key = :nk
                      AND t.role = 'operator' AND t.created_at > :gen)
                    AS operator_turns,
                  (SELECT count(*) FROM dag_nodes n
                    WHERE n.job_id = s.job_id AND n.node_key <> :nk
                      AND n.status IN ('done', 'skipped')
                      AND COALESCE(n.completed_at, n.updated_at) > :gen)
                    AS advanced,
                  (SELECT count(*) FROM dag_nodes n
                    WHERE n.job_id = s.job_id AND n.updated_at > :gen
                      AND (n.node_key = :nk
                           OR n.node_key = ANY(
                                COALESCE(cur.depends_on, ARRAY[]::text[]))))
                    AS replanned
                  FROM assist_sessions s
                  LEFT JOIN dag_nodes cur
                    ON cur.job_id = s.job_id AND cur.node_key = :nk
                 WHERE s.id = :sid
            """),
            {"sid": session_id, "nk": node_key, "gen": generated_at},
        )).mappings().first()
        if not row:
            return False
        on_advance = settings.assist_guide_stale_on_advance
        if row["operator_turns"]:
            reason = "operator_turns"
        elif on_advance and row["advanced"]:
            reason = "project_advanced"
        elif on_advance and row["replanned"]:
            reason = "plan_changed"
        else:
            return False
        logger.info(
            "assist_guide_cache_stale node_key=%s reason=%s "
            "operator_turns=%s advanced=%s replanned=%s",
            node_key, reason,
            row["operator_turns"], row["advanced"], row["replanned"],
        )
        return True
    except Exception as exc:  # noqa: BLE001 — staleness probe must not break guide
        logger.warning("assist_guide_staleness_check_failed node_key=%s: %s", node_key, exc)
        return False


async def ensure_guidance(
    *,
    session_id: str,
    node_key: str,
    ctx: StepContext,
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    force: bool = False,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
    db,
) -> dict:
    """Return guidance, generating + persisting only when needed.

    ``force=False`` (the auto-guide / re-view path) returns a cached ``ready``
    row without spending an LLM call. ``force=True`` (``/assist guide``) always
    regenerates.
    """
    if not force:
        cached = await read_cached_guidance(
            session_id=session_id, node_key=node_key, db=db,
        )
        if cached:
            # §17.877 — never re-serve a walkthrough that predates operator
            # work on the step; regenerate from current session memory instead.
            stale = await cached_guidance_is_stale(
                session_id=session_id, node_key=node_key,
                generated_at=cached.get("_generated_at_raw"), db=db,
            )
            if not stale:
                cached.pop("_generated_at_raw", None)
                return cached
            logger.info("assist_guide_cache_stale_regen node_key=%s", node_key)
    failed_cmds = ""
    try:  # §17.887(#8) — guides see the step's failed-command history too
        from app.modules.assist_agent import _fix_failure_streak
        _stk, failed_cmds = await _fix_failure_streak(
            session_id=session_id, node_key=node_key, db=db)
    except Exception:  # noqa: BLE001
        pass
    res = await generate_guidance(
        ctx=ctx,
        failed_commands=failed_cmds,
        node_description=node_description,
        research=research,
        refine_hint=refine_hint,
        node_key=node_key,
        domain=domain,
        environment=environment,
        verbosity=verbosity,
        job_digest=job_digest,
        operator_notes=operator_notes,
        is_decision=is_decision,
        conversation=conversation,
    )
    # §17.851 — code-enforced placeholder resolution (see resolve_placeholders).
    from app.config import settings as _settings
    if res.get("guidance") and _settings.assist_placeholder_resolver_enabled:
        resolved, resolved_map = await resolve_placeholders(
            text=res["guidance"], session_id=session_id, environment=environment,
            step_title=ctx.title, db=db, node_key=node_key,  # §17.892
        )
        res["guidance"] = resolved
        meta = res.setdefault("guidance_meta", {})
        meta["placeholders_resolved"] = resolved_map
        # §17.854 (audit C3) — generate_guidance scanned the PRE-resolution text,
        # so a value substituted in here (e.g. a pinned "local-lvm; wipefs …")
        # was never re-scanned. Re-run the destructive scan on the resolved text
        # so the meta the SPA reads reflects what will actually be shown/run.
        if _settings.assist_destructive_scan:
            meta["destructive"] = scan_destructive(resolved)
    await persist_guidance(
        session_id=session_id,
        node_key=node_key,
        guidance=res["guidance"],
        guidance_meta=res["guidance_meta"],
        status=res["status"],
        db=db,
    )
    res["cached"] = False
    return res


# ── Streaming generation (§17.493) ─────────────────────────────────────────


async def generate_guidance_stream(
    *,
    session_id: str,
    node_key: str,
    ctx: StepContext,
    node_description: Optional[str] = None,
    research: bool,
    refine_hint: Optional[str] = None,
    force: bool = False,
    domain: Optional[str] = None,
    environment: Optional[dict] = None,
    verbosity: str = "normal",
    job_digest: Optional[str] = None,
    operator_notes: Optional[list[dict]] = None,
    is_decision: bool = False,
    conversation: Optional[str] = None,
    db,
):
    """Stream a walkthrough as it generates. Yields event dicts:

      ``{"type": "delta", "text": ...}``  — one per content chunk
      ``{"type": "done", "status": ..., "guidance_meta": {...}, "cached": bool}``

    - Cache hit (``force=False``) → a single delta + a ``done(cached=True)``, no
      model stream (re-views stay instant).
    - Empty stream → falls back to the non-streamed ``chat_until_nonempty`` so
      streaming cannot regress the §17.465 thinking-model empty-content guard.
    - Persists the full accumulated text + meta (destructive scan, sources)
      before the ``done`` event — same record a non-streamed generate produces.
    """
    role = settings.assist_guide_model_role

    # (a) cache short-circuit — no stream. §17.877: a cached walkthrough that
    # predates operator work on the step is stale — regenerating (with a
    # graceful explanation delta) replaces the "engine repeats itself" replay.
    if not force:
        cached = await read_cached_guidance(
            session_id=session_id, node_key=node_key, db=db,
        )
        if cached:
            stale = await cached_guidance_is_stale(
                session_id=session_id, node_key=node_key,
                generated_at=cached.get("_generated_at_raw"), db=db,
            )
            if not stale:
                yield {"type": "delta", "text": cached["guidance"]}
                yield {"type": "done", "status": "ready",
                       "guidance_meta": cached.get("guidance_meta") or {}, "cached": True}
                return
            logger.info("assist_guide_cache_stale_regen node_key=%s (stream)", node_key)
            yield {"type": "delta", "text": (
                "_The saved walkthrough for this step is from before your recent "
                "work on it — writing a fresh one that picks up from where you "
                "actually are (this can take a minute or two)…_\n\n"
            )}

    # (b) research pre-pass (awaited, non-streamed).
    sources: list[dict] = []
    if research:
        sources = await _research_prepass(
            task_text=ctx.base_prompt, tool=ctx.tool, role=role,
            max_queries=settings.assist_guide_max_research_queries,
            node_key=node_key, domain=domain,
            # §17.854 (audit C1) — the STREAM path had dropped the §17.771
            # environment grounding the non-stream path passes, so a streamed
            # DECISION step (the SPA path) researched generic textbook options
            # instead of system-specific ones. Restored to parity.
            environment_block=render_environment_block(environment),
        )

    system = apply_verbosity(
        guide_system_for_tool(ctx.tool, is_decision=is_decision), verbosity
    )
    system = apply_next_callout(  # §17.741 — lead with the immediate action
        system, is_decision=is_decision,
        enabled=settings.assist_next_callout_enabled,
    )
    system = apply_problem_solving(  # §17.742 — don't thrash on tangled steps
        system, enabled=settings.assist_problem_solving_enabled,
    )
    system = apply_ground_or_ask(  # §17.756 — placeholder + ask, never guess a value
        system, is_decision=is_decision,
        enabled=settings.assist_ground_or_ask_enabled,
    )
    system = apply_screen_grounding(  # §17.758 — confirm the on-screen state first
        system, is_decision=is_decision,
        enabled=settings.assist_screen_grounding_enabled,
    )
    system = apply_location_callout(  # §17.852 — say WHERE, announce switches
        system, is_decision=is_decision,
        enabled=settings.assist_location_callout_enabled,
    )
    user = _build_guide_user_prompt(
        ctx, node_description, sources, refine_hint, environment=environment,
        job_digest=job_digest, operator_notes=operator_notes, is_decision=is_decision,
        conversation=conversation,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    # (c) stream content deltas.
    stream_broken = False
    chunks: list[str] = []
    model_used = role
    try:
        async for delta in model_router.stream_chat(
            messages, role=role, temperature=0.3,
            max_tokens=settings.assist_guide_max_tokens,
        ):
            if delta:
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
    except Exception as exc:
        # §17.887 (audit #7) — a mid-stream failure with partial chunks was
        # persisted status='ready' and REPLAYED from cache forever ("half an
        # answer" on every re-view). Mark broken → persist non-ready below so
        # the cache never serves a walkthrough that didn't finish cleanly.
        logger.warning("assist_guide_stream_failed: %s", exc)
        stream_broken = True

    text_out = "".join(chunks).strip()
    if stream_broken and text_out:
        yield {"type": "delta", "text": "\n\n_⚠️ The stream broke mid-walkthrough — the steps above may be incomplete. Press Guide again for a fresh full version._"}

    # (d) empty-guard fallback — preserve §17.465 (stream yielded nothing).
    if not text_out:
        resp = await chat_until_nonempty(
            model_router.chat, messages, {"role": role},
            temperature=0.3, max_tokens=settings.assist_guide_max_tokens,
            draws=3, label="assist_guide_stream_fallback",
            think_off_rescue=True,  # §17.876
        )
        text_out = (resp.text or "").strip() if (resp and resp.success) else ""
        model_used = getattr(resp, "model", role) if resp else role
        if text_out:
            yield {"type": "delta", "text": text_out}

    # §17.893 — banned-value enforcement on the stream path: the operator
    # already watched the dirty draft stream, so a successful redraw is shown
    # as an explicit correction block (and becomes the durable copy the client
    # reloads); a still-dirty redraw gets the visible flag.
    banned_meta: list[dict] = []
    if text_out:
        _new, banned_meta, _redrew = await enforce_banned_values(
            text_out=text_out, environment=environment, messages=messages,
            role=role, label="assist_guide_stream",
        )
        if _redrew and _new != text_out and not banned_meta:
            text_out = _new
            yield {"type": "delta", "text":
                   "\n\n---\n♻️ **Corrected walkthrough** (the draft above used a "
                   "reserved value — this version replaces it; the corrected copy "
                   "is what gets saved):\n\n" + _new}
        elif banned_meta:
            _bwarn = banned_values_warning(banned_meta)
            text_out += _bwarn
            yield {"type": "delta", "text": _bwarn}

    # §17.887(#8) — guide integrity gate on the stream path too.
    if text_out:
        _failed_cmds = ""
        try:
            from app.modules.assist_agent import _fix_failure_streak
            _stk, _failed_cmds = await _fix_failure_streak(
                session_id=session_id, node_key=node_key, db=db)
        except Exception:  # noqa: BLE001
            pass
        _warn = guide_integrity_warning(text_out, user, _failed_cmds)
        if _warn:
            logger.warning("assist_guide_integrity_flag node_key=%s (stream)", node_key)
            text_out += _warn
            yield {"type": "delta", "text": _warn}
    # §17.854 (audit C1) — decision-suggestion enforcement was ONLY on the
    # non-stream path, so a streamed DECISION walkthrough that dropped the
    # "## My suggestion" lean shipped un-enforced even with the valve on. Run the
    # same guard here; the appended block is yielded as a delta AND folded into
    # the durable copy the client reloads post-stream. Fail-soft.
    suggestion_enforced = False
    if (is_decision and text_out
            and settings.assist_decision_suggestion_enforce
            and not _has_decision_suggestion(text_out)):
        block = await _generate_decision_suggestion(
            title=ctx.title, task_prompt=ctx.base_prompt, options_text=text_out,
            environment=environment, role=role,
        )
        if block:
            yield {"type": "delta", "text": f"\n\n{block}"}
            text_out = f"{text_out}\n\n{block}"
            suggestion_enforced = True
            logger.info("assist_decision_suggestion_enforced(stream) node_key=%s", node_key)

    # §17.851 — code-enforced placeholder resolution before persist: the
    # DURABLE copy (what load() re-renders and future reads serve) carries
    # concrete values; the live-streamed raw text is replaced on the client's
    # post-stream reload.
    resolved_map: dict = {}
    if text_out and settings.assist_placeholder_resolver_enabled:
        text_out, resolved_map = await resolve_placeholders(
            text=text_out, session_id=session_id, environment=environment,
            step_title=ctx.title, role=role, db=db, node_key=node_key,  # §17.892
        )

    status = "ready" if (text_out and not stream_broken) else "failed"  # §17.887(#7)
    meta: dict[str, Any] = {
        "model": model_used,
        "tool": ctx.tool,
        "research_sources": [{"query": s["query"], "kind": s["kind"]} for s in sources],
        "refine_hint": refine_hint,
        "suggestion_enforced": suggestion_enforced,
        "status": status,
        "generated_at": _utcnow_iso(),
        "placeholders_resolved": resolved_map,
        "banned_value_violations": banned_meta,  # §17.893
        # §17.854 (audit C3) — scan runs on the POST-resolution text (matches the
        # ensure_guidance rescan on the non-stream path), so substituted values
        # that introduce a destructive command are caught.
        "destructive": scan_destructive(text_out) if settings.assist_destructive_scan else [],
    }
    if status == "failed":
        meta["error"] = "empty model output"
    await persist_guidance(
        session_id=session_id, node_key=node_key, guidance=text_out,
        guidance_meta=meta, status=status, db=db,
    )
    yield {"type": "done", "status": status, "guidance_meta": meta, "cached": False}


# ── Explicit one-off research (/assist research <question>) ───────────────






