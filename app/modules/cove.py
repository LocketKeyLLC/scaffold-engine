"""§17.452 (Phase C) — Chain-of-Verification (CoVe) revision pass.

CoVe (Dhuliawala et al., Meta AI; arXiv 2309.11495): given a draft ANSWER and the
CONTEXT it should be grounded in,
  1. plan verification questions about the draft's factual claims,
  2. answer them INDEPENDENTLY from the context — NOT conditioned on the draft,
     which is the key property that avoids the model self-confirming, and
  3. revise the draft to align with the verified answers, dropping/correcting
     claims the context does not support.

Where B1 faithfulness (§17.448) *scores* a summary's groundedness, CoVe *corrects*
it. Black-box (plain chat + one tool-call), so it works with the cloud/Ollama
models in this stack. **Default-OFF**; **fail-soft** — any error/timeout/empty
step returns ``None`` and the caller keeps the original draft.
"""
from __future__ import annotations

import asyncio
import logging

from app import model_router
from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.cove")

# §17.449 lesson — model_router.tool_call needs a Tool object, NOT a raw dict.
_QUESTIONS_TOOL = Tool(
    name="list_verification_questions",
    description="List targeted verification questions about the factual claims in the answer.",
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-8 verification questions, each checking ONE factual claim.",
            },
        },
        "required": ["questions"],
    },
)

_PLAN_SYSTEM = (
    "Plan verification questions that would expose unsupported factual claims in "
    "the ANSWER. One claim per question. Skip opinions/transitions. Report via the tool."
)
_ANSWER_SYSTEM = (
    "You answer verification questions STRICTLY from the provided context. Do not "
    "use outside knowledge. If the context does not answer a question, say exactly "
    "'not supported by the sources'."
)
_REVISE_SYSTEM = (
    "You revise a draft to remove or correct unsupported claims, guided by "
    "context-grounded verification answers. Keep the same style and length. Do not "
    "add new claims or citations."
)

_MAX_CONTEXT_CHARS = 24_000
_MAX_QUESTIONS = 8
_STEP_TIMEOUT_S = 120
# §17.453 — qwen3.5 (and other thinking models) spend num_predict on reasoning
# FIRST; too tight a budget leaves the actual answer empty (success=True,
# content=""), which fail-soft'd CoVe to None ~half the time (the revise step
# was worst). Give the free-text steps generous room for thinking + output.
# Live-tuned: revise needed 8192 to reliably produce output on this stack.
_QUESTION_TOKENS = 2048
_ANSWER_TOKENS = 8192
_REVISE_TOKENS = 8192


async def _generate_nonempty(prompt, *, role, overrides, system, temperature, max_tokens):
    """§17.453 — generate() with ONE retry. Thinking models intermittently
    return success=True but empty content when reasoning overruns num_predict;
    a second independent draw usually lands. Returns stripped text or None."""
    for _attempt in (1, 2):
        resp = await asyncio.wait_for(
            model_router.generate(
                prompt, role=role, overrides=overrides, system=system,
                temperature=temperature, max_tokens=max_tokens,
            ),
            timeout=_STEP_TIMEOUT_S,
        )
        if getattr(resp, "success", False) and (resp.text or "").strip():
            return resp.text.strip()
    return None


async def cove_revise(
    answer: str,
    context: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> dict | None:
    """Return ``{revised, questions, changed}`` or ``None`` (fail-soft)."""
    if not (answer or "").strip() or not (context or "").strip():
        return None
    ctx = context[:_MAX_CONTEXT_CHARS]
    try:
        # Step 1 — plan verification questions about the draft's claims.
        q_resp = await asyncio.wait_for(
            model_router.tool_call(
                messages=[
                    {"role": "system", "content": _PLAN_SYSTEM},
                    {"role": "user", "content": f"ANSWER:\n{answer}"},
                ],
                tools=[_QUESTIONS_TOOL], role=role, overrides=overrides,
                temperature=0.0, max_tokens=_QUESTION_TOKENS,
            ),
            timeout=_STEP_TIMEOUT_S,
        )
        q_args = read_tool_args(q_resp) if getattr(q_resp, "success", False) else None
        questions = [str(q) for q in ((q_args or {}).get("questions") or []) if str(q).strip()]
        questions = questions[:_MAX_QUESTIONS]
        if not questions:
            return None

        # Step 2 — answer them INDEPENDENTLY from the context (the draft is NOT
        # in this prompt — that independence is what makes CoVe work).
        q_block = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        verified = await _generate_nonempty(
            f"CONTEXT:\n{ctx}\n\nQUESTIONS:\n{q_block}\n\n"
            "Answer each question using ONLY the context.",
            role=role, overrides=overrides, system=_ANSWER_SYSTEM,
            temperature=0.0, max_tokens=_ANSWER_TOKENS,
        )
        if not verified:
            return None

        # Step 3 — revise the draft to align with the verified answers.
        revised = await _generate_nonempty(
            f"ORIGINAL:\n{answer}\n\nVERIFICATION (context-grounded Q&A):\n{verified}\n\n"
            "Rewrite the ORIGINAL so every claim is consistent with the verification. "
            "Remove or correct any claim the verification marks unsupported.",
            role=role, overrides=overrides, system=_REVISE_SYSTEM,
            temperature=0.2, max_tokens=_REVISE_TOKENS,
        )
        if not revised:
            return None
    except asyncio.TimeoutError:
        logger.warning("cove_timeout: budget_s=%d", _STEP_TIMEOUT_S)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cove_error: %s", exc)
        return None

    return {
        "revised": revised,
        "questions": questions,
        "changed": revised.strip() != answer.strip(),
    }
