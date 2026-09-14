"""§17.1072 — the instructor-backed decide path (trial, default off).

The routing decision (`assist_decide.decide_turn`) grew four bespoke
retry mechanisms — native-then-coax, redraw on empty args, redraw on empty
payloads, think-off after a starved draw — each added after a live miss.
instructor does the one thing they approximate: validate the model's
output against a Pydantic schema and re-ask WITH the validation error.

Behind ``assist_decide_backend = "instructor"``; on any failure the caller
falls back to the router path, so the trial can never lose a turn. Speaks
to Ollama's OpenAI-compatible endpoint directly (``<ollama_base_url>/v1``),
which bypasses the provider seam: no ``think`` toggle, no llm_call_logs
row — the two costs the A/B (`scripts/decide_ab.py`) has to weigh.
"""
from __future__ import annotations

import logging
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("scaffold")


class Suggestion(BaseModel):
    leaning: str = ""
    why: str = ""


def _decision_model(actions: tuple[str, ...]):
    """Build the Decision schema from the live action vocabulary so the two
    paths can never disagree about what an action is."""
    Action = Literal[actions]  # type: ignore[valid-type]

    class Decision(BaseModel):
        action: Action  # type: ignore[valid-type]
        evidence: str = ""
        error_text: str = ""
        query: str = ""
        note_text: str = ""
        note_kind: Literal["addition", "constraint", "preference", "decision", "note"] = "note"
        plan_impact: Literal["none", "surface", "reshape"] = "none"
        suggestion: Optional[Suggestion] = None
        confidence: Literal["low", "medium", "high"]
        rationale: str = Field(default="", max_length=400)

    return Decision


def decide_via_instructor(messages: list[dict], *, actions: tuple[str, ...], model: str,
                          base_url: str, api_key: str = "ollama", max_retries: int = 2,
                          timeout_s: float = 60.0) -> tuple[dict, dict]:
    """Return ``(decision_dict, meta)``. Raises on a hard failure (the caller
    falls back). ``meta`` carries latency and the retry count for the A/B."""
    import instructor
    from openai import OpenAI

    Decision = _decision_model(actions)
    client = instructor.from_openai(OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s),
                                    mode=instructor.Mode.JSON)
    t0 = time.monotonic()
    out = client.chat.completions.create(model=model, messages=messages, response_model=Decision,
                                         max_retries=max_retries, temperature=0.0)
    meta = {"latency_ms": round((time.monotonic() - t0) * 1000), "backend": "instructor"}
    d = out.model_dump()
    if d.get("suggestion") is not None and not (d["suggestion"].get("leaning") or "").strip():
        d["suggestion"] = None
    return d, meta
