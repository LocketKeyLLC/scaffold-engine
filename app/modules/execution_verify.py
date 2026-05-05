"""Verifier for execution_agent — checks node output meets task requirements.

Fail-closed: every error path (chat exception, parse failure, missing schema
key, timeout) returns ('fail', reason, 0.0). Used by ``execute_next_node``
between node execution and node-status persistence.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app import model_router
from app.config import settings

logger = logging.getLogger(__name__)


VERIFY_SYSTEM = """You are a Requirements Satisfaction Checker. Your ONLY job is to confirm that the required functionality is present and correct.

Respond with ONLY a JSON object in this format:
{"pass": true, "reason": "one sentence", "confidence": 0.95}

RULES:
- PASS if the output contains what the task requested, even partially.
- A complete implementation that includes the required feature is a PASS.
- Additional content beyond the requirement is acceptable and expected.
- FAIL only if the required functionality is completely missing or fundamentally incorrect.
- confidence: 0.0 to 1.0

Example 1 — PASS (exact match):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort, merge sort, quicksort"
{"pass": true, "reason": "Three sorting algorithms listed as requested", "confidence": 0.95}

Example 2 — PASS (exceeds scope, still correct):
TASK: "Define a function signature for merging two sorted lists"
OUTPUT: "def merge_sorted(a, b):\n    result = []\n    while a and b:\n        if a[0] <= b[0]: result.append(a.pop(0))\n        else: result.append(b.pop(0))\n    return result + a + b"
{"pass": true, "reason": "Function signature present with full implementation — extra detail is acceptable", "confidence": 0.93}

Example 3 — PASS (broad answer to narrow task):
TASK: "Handle empty list edge case"
OUTPUT: "The function checks if either list is empty and returns the other list directly. It also handles the general merge case for non-empty lists."
{"pass": true, "reason": "Empty list handling is addressed as requested", "confidence": 0.90}

Example 4 — FAIL (genuinely missing):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort is a comparison-based algorithm that repeatedly steps through the list"
{"pass": false, "reason": "Only one algorithm mentioned, task requires three", "confidence": 0.90}

Respond with ONLY the JSON object."""


async def _verify_output(
    task_title: str,
    output: str,
    model: str,
) -> tuple[Literal["pass", "fail"], str, float]:
    """Verify output quality. Fail-closed: any error/parse/timeout => ('fail', ...)."""
    from app.utils.llm_parsing import parse_json_object
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"TASK: {task_title}\n\nOUTPUT:\n{output}"},
    ]

    async def _body() -> tuple[Literal["pass", "fail"], str, float]:
        try:
            resp = await model_router.chat(messages=messages, model=model)
        except Exception as e:
            logger.warning("verify_chat_failed: %s", e)
            return "fail", f"verifier chat failed: {e}", 0.0
        raw = (resp.text or "").strip()
        if not raw:
            return "fail", "verifier returned empty response", 0.0
        data = parse_json_object(raw)
        if not isinstance(data, dict):
            logger.warning("verify_parse_failed | raw: %s", raw[:300])
            return "fail", "verifier output unparseable", 0.0
        if "pass" not in data:
            logger.warning("verify_schema_missing_pass: %s", str(data)[:200])
            return "fail", "verifier response missing 'pass' key", 0.0
        return (
            "pass" if bool(data.get("pass", False)) else "fail",
            str(data.get("reason", "")),
            float(data.get("confidence", 0.0)),
        )

    try:
        return await asyncio.wait_for(_body(), timeout=settings.verify_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("verify_timeout after %ss", settings.verify_timeout_seconds)
        return "fail", f"verifier timeout ({settings.verify_timeout_seconds}s)", 0.0
    except Exception as e:
        logger.exception("verify_unexpected_error")
        return "fail", f"verifier unexpected error: {e}", 0.0
