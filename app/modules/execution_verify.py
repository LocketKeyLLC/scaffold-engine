"""Verifier for execution_agent — checks node output meets task requirements.

Fail-closed: every error path (LLM exception, parse failure, missing schema
key, timeout, no tool call returned) returns ('fail', reason, 0.0). Used by
``execute_next_node`` between node execution and node-status persistence.

Sprint W.6: migrated from chat() + parse_json_object to native tool calling
via model_router.tool_call(). On a provider that supports native tools, the
verdict comes back as resp.tool_calls[0].arguments. On non-tool providers,
the wrapper coaxes JSON via system prompt and synthesizes the same shape,
so the read path is identical.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app import model_router
from app.config import get_model, settings
from app.providers.base import Tool
from app.utils.llm_response_cache import get_verifier_cache

logger = logging.getLogger(__name__)


VERIFY_SYSTEM = """You are a Requirements Satisfaction Checker. Your ONLY job is to confirm that the required functionality is present and correct.

You MUST report your verdict by calling the ``record_verification`` tool. Do NOT respond with prose; the verdict must come from the tool call.

RULES:
- PASS if the output contains what the task requested, even partially.
- A complete implementation that includes the required feature is a PASS.
- Additional content beyond the requirement is acceptable and expected.
- FAIL only if the required functionality is completely missing or fundamentally incorrect.
- confidence: 0.0 to 1.0

Example 1 — PASS (exact match):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort, merge sort, quicksort"
record_verification(pass=true, reason="Three sorting algorithms listed as requested", confidence=0.95)

Example 2 — PASS (exceeds scope, still correct):
TASK: "Define a function signature for merging two sorted lists"
OUTPUT: "def merge_sorted(a, b): ..."
record_verification(pass=true, reason="Function signature present with full implementation — extra detail is acceptable", confidence=0.93)

Example 3 — FAIL (genuinely missing):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort is a comparison-based algorithm..."
record_verification(pass=false, reason="Only one algorithm mentioned, task requires three", confidence=0.90)"""


VERIFY_TOOL = Tool(
    name="record_verification",
    description="Record the verifier's verdict on whether the output meets the task's requirements.",
    input_schema={
        "type": "object",
        "properties": {
            "pass": {
                "type": "boolean",
                "description": "True if required functionality is present (even partially); false only if missing or fundamentally incorrect.",
            },
            "reason": {
                "type": "string",
                "description": "One-sentence justification for the verdict.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the verdict, 0.0 to 1.0.",
            },
        },
        "required": ["pass", "reason", "confidence"],
    },
)


async def _verify_output(
    task_title: str,
    output: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> tuple[Literal["pass", "fail"], str, float]:
    """Verify output quality. Fail-closed: any error/parse/timeout => ('fail', ...).

    §17.89 Pattern 3 — dispatch via ``role=`` so the configured
    ``MODEL_VERIFIER_PROVIDER`` is honored. Pre-§17.89 the helper took a
    pre-resolved model string and went through the legacy Ollama-only
    path. The default ``role="model_verifier"`` matches the upstream
    caller's prior `get_model("model_verifier", ...)` resolution.
    """
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"TASK: {task_title}\n\nOUTPUT:\n{output}"},
    ]
    # Cache lookup key needs a concrete model tag, not a role — the same role
    # can resolve to different tags via overrides. Resolution is cheap (dict
    # lookup) and the result is stable for the call.
    resolved_model = get_model(role, overrides)
    cache = get_verifier_cache()
    cache_args = (messages, VERIFY_TOOL.input_schema, resolved_model, 0.0)

    async def _body() -> tuple[Literal["pass", "fail"], str, float]:
        cached = await cache.get(*cache_args)
        if cached is not None:
            return cached
        try:
            resp = await model_router.tool_call(
                messages=messages, tools=[VERIFY_TOOL],
                role=role, overrides=overrides,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning("verify_tool_call_failed: %s", e)
            return "fail", f"verifier call failed: {e}", 0.0

        if not resp.success:
            logger.warning("verify_response_unsuccessful: %s", resp.error)
            return "fail", f"verifier response error: {resp.error}", 0.0

        if not resp.tool_calls:
            # Model declined to call the tool (or coaxing parse failed).
            logger.warning(
                "verify_no_tool_call | text: %s", (resp.text or "")[:300],
            )
            return "fail", "verifier produced no tool call", 0.0

        args = resp.tool_calls[0].arguments
        if not isinstance(args, dict):
            logger.warning("verify_arguments_not_object: %s", str(args)[:200])
            return "fail", "verifier arguments not an object", 0.0
        if "pass" not in args:
            logger.warning("verify_schema_missing_pass: %s", str(args)[:200])
            return "fail", "verifier response missing 'pass' key", 0.0
        try:
            confidence = float(args.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        status: Literal["pass", "fail"] = (
            "pass" if bool(args.get("pass", False)) else "fail"
        )
        reason = str(args.get("reason", ""))
        await cache.put(*cache_args, status, reason, confidence)
        return status, reason, confidence

    try:
        return await asyncio.wait_for(_body(), timeout=settings.verify_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("verify_timeout after %ss", settings.verify_timeout_seconds)
        return "fail", f"verifier timeout ({settings.verify_timeout_seconds}s)", 0.0
    except Exception as e:
        logger.exception("verify_unexpected_error")
        return "fail", f"verifier unexpected error: {e}", 0.0


# ---------------------------------------------------------------------------
# §17.376 — validation-citation guard
# ---------------------------------------------------------------------------

_CITATION_TOKEN_RE = __import__("re").compile(r"\bT([0-9]+)\b")


def _is_validation_llm_node(node_type: str | None, tool: str | None, title: str | None) -> bool:
    """Detect a `type=validation` LLM node.

    §17.376 — the citation guard only applies to validation-shaped LLM
    nodes. Two triggers: dag_nodes.node_type='checkpoint' (the DAG
    generator's mapping for task_type='validation') OR the title
    contains a validation keyword (Validate / Verify / Check / Audit).
    The keyword fallback covers hand-edited rows whose node_type wasn't
    set to checkpoint but whose intent is clearly validation.

    All three args are accepted as Optional so the caller can pass raw
    DB row values without defensive .get() calls.
    """
    if (tool or "").lower() != "llm":
        return False
    if (node_type or "").lower() == "checkpoint":
        return True
    title_lower = (title or "").lower()
    return any(kw in title_lower for kw in ("validate", "verify", "check", "audit"))


def check_validation_citations(
    output: str,
    expected_codegen_keys: list[str],
) -> list[str]:
    """§17.376 substring-presence check. Kept for backward compat with
    `tests/test_validation_citation_guard.py`; the integration call site
    in `execute_next_node` uses the §17.377 per-claim tightening below.

    Returns the code-bearing upstream node_keys that the validation
    output does NOT cite anywhere. Empty list = clean.
    """
    if not expected_codegen_keys:
        return []
    cited_numeric = {m.group(1) for m in _CITATION_TOKEN_RE.finditer(output or "")}
    expected_numeric = {k.lstrip("T") for k in expected_codegen_keys if k.startswith("T")}
    missing_numeric = expected_numeric - cited_numeric
    return sorted(f"T{n}" for n in missing_numeric)


# §17.377 — tighter check. The substring-presence version of §17.376
# was gamed by the fifth mdsplit retry: T7's output contained "decision
# node (T2 or T3)" as a passing aside, satisfying the regex while the
# actual MET claims still cited only T4/T5/T6 as evidence. The tighter
# check requires each expected upstream to appear in at least one
# CLAIM LINE (a line containing MET / NOT MET / UNKNOWN), not just
# anywhere in the prose.
_CLAIM_MARKERS = (": MET", "MET.", "MET ", "NOT MET", "UNKNOWN")


def _is_claim_line(line: str) -> bool:
    """A 'claim line' is a validation report line that contains a
    MET / NOT MET / UNKNOWN verdict — typically a bullet point of the
    form `- <requirement>: MET. <evidence>`.
    """
    s = line.strip()
    if not s:
        return False
    return any(marker in s for marker in _CLAIM_MARKERS)


def check_validation_citation_coverage(
    output: str,
    expected_codegen_keys: list[str],
) -> list[str]:
    """§17.377 per-claim citation coverage check.

    Stricter version of `check_validation_citations`. Returns the
    code-bearing upstream node_keys that do NOT appear in at least
    one MET/NOT MET/UNKNOWN claim line. A passing reference like
    "decision node (T2 or T3)" outside any claim line does NOT count.

    Rationale: §17.376's substring check was satisfied by the fifth
    mdsplit retry's T7 even though T7's 4 MET claims all cited
    T4/T5/T6 as canonical evidence; T2 and T3 appeared only in one
    factually-wrong aside ("decision node (T2 or T3)" — T1 is the
    decision node). §17.377 closes the gaming by requiring per-claim
    attribution rather than substring presence.

    Inputs:
        output: validation node's output_text.
        expected_codegen_keys: list of upstream node_keys whose tool
            was CodeGen and status='done' at the time the validation
            ran.

    Returns:
        Sorted list of missing keys — upstreams that did not appear
        in any claim line. Empty if every expected key appears in at
        least one MET / NOT MET / UNKNOWN line.
    """
    if not expected_codegen_keys:
        return []
    if not output:
        expected_numeric = {k.lstrip("T") for k in expected_codegen_keys if k.startswith("T")}
        return sorted(f"T{n}" for n in expected_numeric)

    cited_in_claims: set[str] = set()
    for line in output.split("\n"):
        if not _is_claim_line(line):
            continue
        for m in _CITATION_TOKEN_RE.finditer(line):
            cited_in_claims.add(m.group(1))

    expected_numeric = {k.lstrip("T") for k in expected_codegen_keys if k.startswith("T")}
    missing_numeric = expected_numeric - cited_in_claims
    return sorted(f"T{n}" for n in missing_numeric)
