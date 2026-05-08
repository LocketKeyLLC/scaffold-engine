"""Shared helper for reading structured args off a tool_call ModelResponse.

Sprint X.13 — consolidation of the `_tool_args` helper that was duplicated
verbatim across `research_agent`, `prompt_optimizer`, `idea_refinement`,
and `gt_extractor` after the W.6 / X.10 / X.11 / X.12 tool-call migration
sweep. The four copies were byte-equal (only docstrings differed); a
shared utility gives the W.6-pattern callers a single read path.

Use this anywhere you've called `model_router.tool_call(...)` and need
to pull the first tool's parsed arguments. Returns None on every failure
mode (success=False, no tool_calls, args not a dict) so callers can
fail-closed with a single `if args is None:` branch.
"""
from __future__ import annotations


def read_tool_args(resp) -> dict | None:
    """Read the first tool call's arguments dict from a ModelResponse.

    Returns ``None`` if any of the following is true:
      - ``resp.success`` is False (dispatch error / retry exhausted)
      - ``resp.tool_calls`` is missing or empty
      - the first tool call's ``arguments`` aren't a dict (pathological
        provider return shape)

    Returns the parsed ``dict`` of arguments otherwise. Callers that
    need to validate keys or types should do so on the returned dict.

    This is the canonical read path for ``model_router.tool_call()``
    callers — equivalent to inline-checking the response shape but
    centralizes the failure-mode contract so every caller fails closed
    the same way.
    """
    if not getattr(resp, "success", False):
        return None
    calls = getattr(resp, "tool_calls", None) or []
    if not calls:
        return None
    args = calls[0].arguments
    return args if isinstance(args, dict) else None
