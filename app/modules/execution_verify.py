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
from app.config import settings
from app.providers.base import Tool

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
    model: str,
) -> tuple[Literal["pass", "fail"], str, float]:
    """Verify output quality. Fail-closed: any error/parse/timeout => ('fail', ...)."""
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"TASK: {task_title}\n\nOUTPUT:\n{output}"},
    ]

    async def _body() -> tuple[Literal["pass", "fail"], str, float]:
        try:
            resp = await model_router.tool_call(
                messages=messages, tools=[VERIFY_TOOL], model=model,
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
        return (
            "pass" if bool(args.get("pass", False)) else "fail",
            str(args.get("reason", "")),
            confidence,
        )

    try:
        return await asyncio.wait_for(_body(), timeout=settings.verify_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("verify_timeout after %ss", settings.verify_timeout_seconds)
        return "fail", f"verifier timeout ({settings.verify_timeout_seconds}s)", 0.0
    except Exception as e:
        logger.exception("verify_unexpected_error")
        return "fail", f"verifier unexpected error: {e}", 0.0
