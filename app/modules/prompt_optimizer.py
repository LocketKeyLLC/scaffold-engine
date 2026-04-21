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
from app.config import get_model, settings

logger = logging.getLogger(__name__)

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
    (r"\bsomewhat\b\s*", ""),
    (r"\brather\b\s*", ""),
    (r"\bquite\b\s*", ""),
    (r"\bvery\b\s*", ""),
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

def _analyze(text: str) -> AnalysisResult:
    filler_count = sum(len(pat.findall(text)) for pat, _ in _FILLER_RE)
    hedge_words = ["maybe", "perhaps", "try to", "attempt to", "sort of", "kind of"]
    hedge_count = sum(text.lower().count(w) for w in hedge_words)
    has_imperative = bool(re.match(r"^[A-Z][a-z]+(?:\s+[a-z])", text))
    issues = []
    if filler_count > 0:
        issues.append(f"{filler_count} filler/boilerplate pattern(s) detected")
    if hedge_count > 0:
        issues.append(f"{hedge_count} hedging expression(s) detected")
    if not has_imperative:
        issues.append("Non-imperative opening; consider starting with an action verb")
    if len(text) > 2000:
        issues.append("Prompt exceeds 2000 chars; consider chunking or abstracting")
    return AnalysisResult(
        token_count=_approx_tokens(text),
        filler_count=filler_count,
        hedge_count=hedge_count,
        has_imperative_structure=has_imperative,
        issues=issues,
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

Respond with a single JSON object. No preamble. No markdown fences.

Schema: {"preserved": true|false, "reason": "<one sentence>"}

TASK: Determine if the OPTIMIZED prompt preserves ALL semantic intent of the ORIGINAL.
- true = all constraints, goals, and scope are intact
- false = any intent, constraint, or scope is lost or distorted
"""

async def _llm_optimize(pre_cleaned: str, model: str) -> str:
    messages = [{"role": "user", "content": f"Rewrite this prompt following all rules:\n\n{pre_cleaned}"}]
    messages = [{"role": "system", "content": OPTIMIZE_SYSTEM}] + messages
    resp = await model_router.chat(messages=messages, model=model)
    return resp.text.strip()

async def _llm_verify(original: str, optimized: str, model: str) -> tuple[bool, str]:
    """Verify the optimized prompt preserves the semantic intent of the original.

    Parsing strategy (fail-closed):
      1. Primary: parse_json_object — handles markdown fences, partial JSON, etc.
      2. Fallback: strict regex match on \\bpreserved\\s*[:=]\\s*(true|false)\\b
      3. Both fail: return (False, <raw[:120]>) — default deny, not allow

    Args:
        original: The original prompt text.
        optimized: The rewritten prompt to verify against the original.
        model: Verifier model tag.

    Returns:
        Tuple of (preserved, reason). ``preserved`` defaults to False on any
        parse/LLM failure to prevent accepting a corrupted optimization.
    """
    import re
    from app.utils.llm_parsing import parse_json_object, strip_think_tags

    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"ORIGINAL:\n{original}\n\nOPTIMIZED:\n{optimized}"},
    ]
    resp = await model_router.chat(messages=messages, model=model)
    raw = strip_think_tags(resp.text or "")

    # 1. Primary: structured JSON parse
    data = parse_json_object(raw)
    if isinstance(data, dict) and "preserved" in data:
        return bool(data["preserved"]), str(data.get("reason", ""))[:200]

    # 2. Fallback: strict regex for preserved: true|false
    match = re.search(r"\bpreserved\s*[:=]\s*(true|false)\b", raw, re.IGNORECASE)
    if match:
        logger.warning("Verifier JSON parse failed; used regex fallback: %s", raw[:120])
        return match.group(1).lower() == "true", raw[:120]

    # 3. Fail closed — neither parser succeeded
    logger.warning("Verifier unparseable, defaulting to not-preserved: %s", raw[:200])
    return False, raw[:120]

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
    opt_model = model_optimizer or get_model("model_verifier", model_overrides)
    ver_model = model_verifier or get_model("model_verifier", model_overrides)

    analysis = _analyze(prompt)
    issues_before = len(analysis.issues)
    pre_cleaned = _deterministic_strip(prompt)

    logger.info("Running LLM optimize pass with %s", opt_model)
    optimized = await _llm_optimize(pre_cleaned, opt_model)

    intent_preserved = True
    if not skip_verify:
        logger.info("Running verifier with %s", ver_model)
        intent_preserved, reason = await _llm_verify(prompt, optimized, ver_model)
        if not intent_preserved:
            # #6.12 — keep intent_preserved=False so callers/clarity score
            # reflect the real verify outcome. Rollback to pre_cleaned is a
            # safety fallback, not evidence the optimized prompt was valid.
            logger.warning("Intent not preserved: %s — falling back to pre_cleaned", reason)
            optimized = pre_cleaned

    post_analysis = _analyze(optimized)
    issues_after = len(post_analysis.issues)
    token_before = analysis.token_count
    token_after = post_analysis.token_count
    reduction_pct = round((token_before - token_after) / token_before * 100, 1) if token_before > 0 else 0.0
    clarity = _clarity_score(token_before, token_after, issues_before, issues_after, intent_preserved)
    issues_resolved = [i for i in analysis.issues if i not in post_analysis.issues]

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
