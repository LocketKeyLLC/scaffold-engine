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
# §17.560 — was max_tokens=2048. role=model_verifier (qwen3.5:397b-cloud) is a
# coaxed thinking model; score_research.py (§17.558) measured grounding `n/a`
# on 2/3 topics, and a probe confirmed it's NOT a timeout (failures returned in
# 34-49 s, well under 90 s) — the model intermittently completes the call but
# emits prose with no parseable tool-call JSON (a coax miss, same "first call
# works, rest fail" pattern as §17.556). Fix: more budget so reasoning doesn't
# crowd out the JSON + retry the coax miss (the main lever for intermittency).
_FAITHFULNESS_MAX_TOKENS = 8192
_FAITHFULNESS_ATTEMPTS = 3


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

    # §17.560 — retry the intermittent coax miss (timeout / no-success /
    # no-claims). The thinking model sometimes emits prose instead of the tool
    # call; a re-roll usually lands it. A genuine exception is a hard failure
    # (fail-soft → None, no retry).
    claims = None
    for attempt in range(_FAITHFULNESS_ATTEMPTS):
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
                    max_tokens=_FAITHFULNESS_MAX_TOKENS,
                ),
                timeout=_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("faithfulness_timeout: attempt=%d budget_s=%d", attempt, _TIMEOUT_S)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("faithfulness_error: %s", exc)
            return None

        if not getattr(resp, "success", False):
            logger.warning("faithfulness_no_success: attempt=%d", attempt)
            continue
        args = read_tool_args(resp)
        candidate = args.get("claims") if args else None
        if isinstance(candidate, list) and candidate:
            claims = candidate
            break
        logger.warning(
            "faithfulness_no_claims: attempt=%d (coax miss, no parseable tool-call) — %s",
            attempt, "retrying" if attempt < _FAITHFULNESS_ATTEMPTS - 1 else "giving up",
        )

    if not claims:
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
