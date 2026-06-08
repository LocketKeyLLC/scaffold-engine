"""
prompt_optimizer.py  --  Step 14
Pipeline:
  raw prompt -> ANALYZE -> OPTIMIZE (LLM) -> VALIDATE (verifier) -> score + return
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional


from app import model_router
from app.config import get_model
from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.prompt_optimizer")

FILLER_PATTERNS: list[tuple[str, str]] = [
    (r"\bplease\b\s*", ""),
    (r"\bkindly\b\s*", ""),
    (r"\bif you (could|can|would)\b\s*", ""),
    (r"\bI (was|am) wondering\b\s*", ""),
    (r"\bI (would|'d) (like|love|appreciate)\b\s*", ""),
    (r"\bfeel free to\b\s*", ""),
    (r"\bdon't hesitate to\b\s*", ""),
    (r"\bwhenever you (get a chance|have time)\b\s*", ""),
    (r"^(Sure|Certainly|Of course|Absolutely)[,!\.]\s*", ""),
    (r"^(As an AI|As a language model)[^\.]*\.\s*", ""),
    (r"^(I'll help you|I can help|Happy to help)[^\.]*\.\s*", ""),
    (r"\bThanks?\b[!\.]*\s*$", ""),
    (r"\bThank you\b[!\.]*\s*$", ""),
]

_FILLER_RE: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE | re.MULTILINE), repl)
    for pat, repl in FILLER_PATTERNS
]

@dataclass
class AnalysisResult:
    token_count: int
    filler_count: int
    hedge_count: int
    has_imperative_structure: bool
    issues: list[str] = field(default_factory=list)
    structured_issues: dict[str, int] = field(default_factory=dict)

@dataclass
class OptimizationResult:
    original_prompt: str
    optimized_prompt: str
    pre_cleaned: str
    token_count_before: int
    token_count_after: int
    token_reduction_pct: float
    clarity_score: float
    intent_preserved: bool
    issues_found: list[str]
    issues_resolved: list[str]
    model_used: str
    verifier_used: str

def _approx_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3)

def _deterministic_strip(text: str) -> str:
    result = text
    for pattern, repl in _FILLER_RE:
        result = pattern.sub(repl, result)
    result = re.sub(r" {2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()

# Imperative opener: case-insensitive verb at start (optionally preceded by
# brief framing like "Please " or "Now,"), allowing 1+ lowercase letters.
# Stop-list rules out common non-imperative leads ("the", "a", "is", "i").
_IMPERATIVE_RE = re.compile(
    r"^\s*(?:please\s+|kindly\s+|now,?\s+)?([a-z]+)\b",
    re.IGNORECASE,
)
_NON_IMPERATIVE_LEADS = frozenset({
    "the", "a", "an", "i", "we", "you", "they", "it",
    "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "my", "our", "your",
    "what", "why", "how", "when", "where", "who",
    "maybe", "perhaps",
})


def _has_imperative_opener(text: str) -> bool:
    m = _IMPERATIVE_RE.match(text)
    if not m:
        return False
    return m.group(1).lower() not in _NON_IMPERATIVE_LEADS


def _analyze(text: str) -> AnalysisResult:
    filler_count = sum(len(pat.findall(text)) for pat, _ in _FILLER_RE)
    hedge_words = ["maybe", "perhaps", "try to", "attempt to", "sort of", "kind of"]
    hedge_count = sum(text.lower().count(w) for w in hedge_words)
    has_imperative = _has_imperative_opener(text)
    over_length = len(text) > 2000
    # Structural form: stable issue_type keys with counts. Used for diffs.
    structured: dict[str, int] = {}
    if filler_count > 0:
        structured["filler"] = filler_count
    if hedge_count > 0:
        structured["hedging"] = hedge_count
    if not has_imperative:
        structured["non_imperative_opener"] = 1
    if over_length:
        structured["over_length"] = 1
    # Human-readable form (back-compat for issues: list[str]).
    issues: list[str] = []
    if filler_count > 0:
        issues.append(f"{filler_count} filler/boilerplate pattern(s) detected")
    if hedge_count > 0:
        issues.append(f"{hedge_count} hedging expression(s) detected")
    if not has_imperative:
        issues.append("Non-imperative opening; consider starting with an action verb")
    if over_length:
        issues.append("Prompt exceeds 2000 chars; consider chunking or abstracting")
    return AnalysisResult(
        token_count=_approx_tokens(text),
        filler_count=filler_count,
        hedge_count=hedge_count,
        has_imperative_structure=has_imperative,
        issues=issues,
        structured_issues=structured,
    )

def _clarity_score(original_tokens, final_tokens, issues_before, issues_after, intent_preserved):
    if original_tokens == 0:
        return 0.0
    reduction_ratio = max(0.0, (original_tokens - final_tokens) / original_tokens)
    issue_ratio = (issues_before - issues_after) / issues_before if issues_before > 0 else 1.0
    intent_score = 1.0 if intent_preserved else 0.0
    score = (0.40 * reduction_ratio) + (0.40 * issue_ratio) + (0.20 * intent_score)
    return round(min(score, 1.0), 3)

OPTIMIZE_SYSTEM = """\
ROLE: Prompt optimizer.

RULES:
- Output ONLY the rewritten prompt. No preamble. No explanation. No quotes.
- Imperative block structure. Every instruction starts with an action verb.
- Strip all filler words, hedging language, boilerplate openers/closers.
- Preserve ALL semantic intent and constraints from the original.
- Minimum tokens. Maximum precision. Zero noise.
- Do NOT add new constraints not present in the original.
- Do NOT use escaped quotes (\\"). Use plain double quotes only if needed.
"""

VERIFY_SYSTEM = """\
ROLE: Semantic intent verifier.

TASK: Determine if the OPTIMIZED prompt preserves ALL semantic intent of the ORIGINAL.
- preserved=true if all constraints, goals, and scope are intact
- preserved=false if any intent, constraint, or scope is lost or distorted
"""

# Sprint X.10 — native tool-call schema for the verifier. Replaces the
# legacy "Respond with a single JSON object..." coaxing prose. The wrapper
# parses structured args on native-tool providers and falls back to JSON-
# coaxing internally on non-native providers, so callers always read via
# resp.tool_calls[0].arguments regardless of provider capability.
RECORD_VERIFICATION_TOOL = Tool(
    name="record_verification",
    description=(
        "Report whether the optimized prompt preserves all semantic intent "
        "of the original."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "preserved": {
                "type": "boolean",
                "description": (
                    "True iff every constraint, goal, and scope element from "
                    "the original survives in the optimized prompt"
                ),
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the verdict",
            },
        },
        "required": ["preserved"],
    },
)


async def _llm_optimize(
    pre_cleaned: str,
    *,
    role: str = "model_general",
    overrides: dict | None = None,
) -> str:
    """§17.89 — role-routed; provider chosen via ``provider_for_role``."""
    from app.utils.llm_parsing import strip_think_tags
    messages = [{"role": "user", "content": f"Rewrite this prompt following all rules:\n\n{pre_cleaned}"}]
    messages = [{"role": "system", "content": OPTIMIZE_SYSTEM}] + messages
    resp = await model_router.chat(messages=messages, role=role, overrides=overrides)
    return strip_think_tags(resp.text or "").strip()

async def _llm_verify(
    original: str,
    optimized: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> tuple[bool, str]:
    """Verify the optimized prompt preserves the semantic intent of the original.

    Sprint X.10 — uses model_router.tool_call so structured output is
    parsed by the wrapper (native or coaxing) rather than coaxed via
    prompt prose. §17.89 — dispatch via ``role=`` so the configured
    ``MODEL_VERIFIER_PROVIDER`` is honored. Fail-closed contract is
    preserved: any failure (no tool_calls, missing 'preserved' key,
    dispatch error) returns False to prevent silently accepting a
    corrupted optimization.

    Args:
        original: The original prompt text.
        optimized: The rewritten prompt to verify against the original.
        role: Settings field name for the verifier model (default
            ``model_verifier``). Pass an alternate role if a non-default
            verifier is desired.
        overrides: Per-request ``{role: model_name}`` overrides forwarded
            to ``provider_for_role`` so the public ``optimize_prompt`` API
            can still honor explicit ``model_verifier=...`` arguments.

    Returns:
        Tuple of (preserved, reason). ``preserved`` defaults to False on
        any failure path.
    """
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"ORIGINAL:\n{original}\n\nOPTIMIZED:\n{optimized}"},
    ]
    resp = await model_router.tool_call(
        messages=messages,
        tools=[RECORD_VERIFICATION_TOOL],
        role=role,
        overrides=overrides,
    )
    args = read_tool_args(resp)
    if not args or "preserved" not in args:
        logger.warning(
            "Verifier tool_call returned no preserved verdict; failing closed"
        )
        return False, ""
    preserved = bool(args["preserved"])
    reason = str(args.get("reason", ""))[:200]
    return preserved, reason

async def optimize_prompt(
    prompt: str,
    model_optimizer: Optional[str] = None,
    model_verifier: Optional[str] = None,
    skip_verify: bool = False,
    model_overrides: Optional[dict] = None,
) -> OptimizationResult:
    """Strip filler, LLM-rewrite, verify intent, score clarity.

    Args:
        prompt: Raw prompt text to optimize.
        model_optimizer: Explicit optimizer model tag (overrides role resolution).
        model_verifier: Explicit verifier model tag (overrides role resolution).
        skip_verify: When True, skip the intent-preservation verification pass.
        model_overrides: Per-request role→model mapping. Used only when the
            explicit ``model_optimizer``/``model_verifier`` args are not set.

    Returns:
        OptimizationResult with original, optimized, and pre_cleaned text plus
        token counts, reduction %, clarity score, and preservation verdict.
    """
    # §17.89 Pattern 3 — push role+overrides into the helpers so dispatch
    # routes through provider_for_role. Explicit model_optimizer / model_verifier
    # args are folded into the per-call overrides dict so the public API
    # contract (caller picks model) is preserved without bypassing provider
    # selection.
    opt_overrides = dict(model_overrides or {})
    if model_optimizer:
        opt_overrides["model_general"] = model_optimizer
    ver_overrides = dict(model_overrides or {})
    if model_verifier:
        ver_overrides["model_verifier"] = model_verifier

    opt_model = get_model("model_general", opt_overrides)
    ver_model = get_model("model_verifier", ver_overrides)

    analysis = _analyze(prompt)
    issues_before = len(analysis.issues)
    pre_cleaned = _deterministic_strip(prompt)

    logger.info("Running LLM optimize pass with %s", opt_model)
    optimized = await _llm_optimize(pre_cleaned, overrides=opt_overrides)

    # §17.462 — never let optimization ERASE the prompt. The optimizer role
    # (model_general) is a thinking model since the §17.440 cloud migration; it
    # can return success + empty content (the §17.453 failure mode) or output
    # that's entirely <think> tags stripped to "". An empty "optimized" prompt
    # silently blanks the user message for every downstream caller (e.g. node
    # execution sent an empty prompt → the model complained it had no task →
    # the node failed and blocked the job). Fall back to the deterministically
    # stripped text, which is non-empty and intent-preserving.
    if not optimized.strip():
        logger.warning(
            "llm_optimize_empty: optimizer returned blank; falling back to "
            "deterministically-stripped prompt (model=%s)", opt_model,
        )
        optimized = pre_cleaned if pre_cleaned.strip() else prompt

    intent_preserved = True
    if not skip_verify:
        logger.info("Running verifier with %s", ver_model)
        intent_preserved, reason = await _llm_verify(
            prompt, optimized, overrides=ver_overrides,
        )
        if not intent_preserved:
            # #6.12 — keep intent_preserved=False so callers/clarity score
            # reflect the real verify outcome. Rollback to pre_cleaned is a
            # safety fallback, not evidence the optimized prompt was valid.
            logger.warning("Intent not preserved: %s — falling back to original", reason)
            optimized = prompt

    post_analysis = _analyze(optimized)
    issues_after = len(post_analysis.issues)
    token_before = analysis.token_count
    token_after = post_analysis.token_count
    reduction_pct = round((token_before - token_after) / token_before * 100, 1) if token_before > 0 else 0.0
    clarity = _clarity_score(token_before, token_after, issues_before, issues_after, intent_preserved)
    # Structural diff: an issue is "resolved" when its type either disappeared
    # or its count strictly decreased post-optimization. Avoids exact-string
    # mismatches (e.g., "3 hedging" vs "1 hedging" no longer falsely "unresolved").
    _pre = analysis.structured_issues
    _post = post_analysis.structured_issues
    _resolved_types = {
        t for t, c in _pre.items() if _post.get(t, 0) < c
    }
    _ISSUE_LABELS = {
        "filler": "filler/boilerplate patterns",
        "hedging": "hedging expressions",
        "non_imperative_opener": "non-imperative opener",
        "over_length": "over-length prompt",
    }
    issues_resolved = [_ISSUE_LABELS.get(t, t) for t in sorted(_resolved_types)]

    return OptimizationResult(
        original_prompt=prompt,
        optimized_prompt=optimized,
        pre_cleaned=pre_cleaned,
        token_count_before=token_before,
        token_count_after=token_after,
        token_reduction_pct=reduction_pct,
        clarity_score=clarity,
        intent_preserved=intent_preserved,
        issues_found=analysis.issues,
        issues_resolved=issues_resolved,
        model_used=opt_model,
        verifier_used=ver_model if not skip_verify else "skipped",
    )
