"""§17.448 (Phase B / B1) — RAGAS-inspired faithfulness scoring.

Given a generated ANSWER and the CONTEXT it should be grounded in, extract the
answer's factual claims and judge each as supported/unsupported by the context,
returning a groundedness ratio (RAGAS faithfulness = |supported| / |total
claims|, arXiv 2309.15217 — 0.95 human agreement on WikiEval).

A single **black-box** LLM tool-call, so it works with this stack's cloud/Ollama
models — the stronger white-box methods (MIRAGE/SABER/FRANQ) need hidden-state
access we don't have. Default-OFF (``settings.faithfulness_check_enabled``) and
**fail-soft**: every error path returns ``None`` so it never breaks the calling
research flow.
"""
from __future__ import annotations

import asyncio
import logging

from app import model_router
from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.faithfulness")

_FAITHFULNESS_SYSTEM = (
    "You are a strict groundedness checker. Given an ANSWER and the CONTEXT it "
    "was written from, extract each distinct factual claim made in the ANSWER and "
    "decide whether the CONTEXT supports it. A claim is SUPPORTED only if the "
    "context directly states it or clearly entails it. Opinions, transitions, "
    "hedges, and meta-statements are not claims and should be skipped. Be "
    "conservative: if a claim is not clearly grounded in the context, mark it "
    "unsupported. Report every claim via the tool."
)

_FAITHFULNESS_TOOL = Tool(
    name="report_faithfulness",
    description="Report per-claim groundedness of the answer against the context.",
    input_schema={
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string",
                                  "description": "One atomic factual claim from the answer."},
                        "supported": {"type": "boolean",
                                      "description": "True iff the context supports it."},
                    },
                    "required": ["claim", "supported"],
                },
            },
        },
        "required": ["claims"],
    },
)

_MAX_CONTEXT_CHARS = 24_000
_TIMEOUT_S = 90


async def score_faithfulness(
    answer: str,
    context: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> dict | None:
    """Return ``{score, supported, total, unsupported_claims}`` or ``None``.

    ``None`` on empty input, LLM/transport failure, timeout, or a no-claims
    result — callers treat ``None`` as "not scored" and carry on.
    """
    if not (answer or "").strip() or not (context or "").strip():
        return None
    ctx = context[:_MAX_CONTEXT_CHARS]
    try:
        resp = await asyncio.wait_for(
            model_router.tool_call(
                messages=[
                    {"role": "system", "content": _FAITHFULNESS_SYSTEM},
                    {"role": "user",
                     "content": f"CONTEXT:\n{ctx}\n\nANSWER:\n{answer}"},
                ],
                tools=[_FAITHFULNESS_TOOL],
                role=role,
                overrides=overrides,
                temperature=0.0,
                max_tokens=2048,
            ),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("faithfulness_timeout: budget_s=%d", _TIMEOUT_S)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("faithfulness_error: %s", exc)
        return None

    if not getattr(resp, "success", False):
        return None
    args = read_tool_args(resp)
    if not args:
        return None
    claims = args.get("claims")
    if not isinstance(claims, list) or not claims:
        return None

    total = len(claims)
    supported = sum(
        1 for c in claims if isinstance(c, dict) and c.get("supported") is True
    )
    unsupported = [
        str(c.get("claim", ""))[:200]
        for c in claims
        if isinstance(c, dict) and not c.get("supported")
    ]
    return {
        "score": round(supported / total, 2),
        "supported": supported,
        "total": total,
        "unsupported_claims": unsupported[:10],
    }
