"""Deterministic assist-routing policy — the SERVER-side source of truth.

§17.855 (audit item: "unified /decide vs the deterministic gate cascade").
The high-precision phrase gates that decide *pivot vs help vs shell-result vs
step-completion* used to live ONLY in the pipeline (`_assist_handlers.py`) and
ran as a client-side cascade — but only on the /decide FALL-THROUGH path. When
`decide_turn` returned a confident decision, the pipeline dispatched it directly
and the deterministic vetoes never got a vote, so a confident-but-wrong LLM call
could override a high-precision deterministic signal (the §17.679 principle —
"deterministic gate over the LLM" — was silently skipped on the fast path).

This module makes the deterministic policy authoritative on the SERVER: after the
LLM produces its decision, `apply_deterministic_overrides` re-applies the same
high-precision gates as a post-filter, so both the confident path and the
fall-through path route consistently. The pipeline keeps its copy purely as the
/decide-unavailable fallback (it runs in a different container and cannot import
`app.*`); this is now the one place the *authoritative* path defines the policy.

The regexes are ported VERBATIM from `_assist_handlers.py` to avoid drift — the
parity test (`tests/test_assist_policy.py`) pins them against the pipeline copy.
"""

from __future__ import annotations

import re

# §17.692 — fold smart punctuation (curly quotes / dashes / ellipsis / nbsp) to
# ASCII first so every gate below (which matches straight apostrophes only) sees
# a normal apostrophe. Verbatim from `_assist_handlers._SMART_PUNCT`.
_SMART_PUNCT = str.maketrans({
    "’": "'", "‘": "'",   # curly single quotes
    "“": '"', "”": '"',   # curly double quotes
    "–": "-", "—": "-",   # en / em dash
    "…": "...",                # ellipsis
    " ": " ",                  # non-breaking space
})


def normalize_punct(s: str) -> str:
    return s.translate(_SMART_PUNCT) if s else s


# ── Pivot detection (§17.679/§17.691) ────────────────────────────────────────
_PIVOT_RE = re.compile(
    r"(^\s*actually\b)"                                        # opens with "actually"
    r"|\b(on second thought|scratch that|never ?mind|"
    r"changed? my mind|change of plan|different (direction|approach)|"
    r"start over|do it differently)\b"
    r"|\b(forget|drop|ditch|ignore|scrap) (the|that|this|all|everything|about|my)\b"
    r"|\b(switch|change|pivot|redo)\s+(it|this|them|the\s+\w+|everything|to|over to)\b"
    r"|\bmake (it|this|them|the whole \w+)\b.{0,60}\binstead\b"
    r"|\b(rather than|instead of)\s+\w+"
    r"|\b\w+\s+instead\b"                                      # "... do X instead"
    r"|\bno longer\b",
    re.IGNORECASE,
)
# A change phrased as applying to the WHOLE deliverable is a plan-reshaping pivot.
_GLOBAL_CHANGE_RE = re.compile(
    r"\b(throughout|everywhere|across (all|the board)|"
    r"all (the )?(steps|emails|sections|parts|pages)|"
    r"every (step|email|section|part|page)|globally|"
    r"the (whole|entire) (thing|sequence|plan|project|document)|overall)\b",
    re.IGNORECASE,
)
# §17.691 — QUESTION-FRAMED pivots ("can't I just wipe it?", "why not just …?").
_QUESTION_PIVOT_RE = re.compile(
    r"\b(?:can'?t|cant|could'?nt|couldn'?t|couldnt)\s+(?:i|we|you)\s+just\b"
    r"|\bwhy\s+(?:not|don'?t|dont|do\s+not|can'?t|cant|wouldn'?t|shouldn'?t)\s+"
    r"(?:i\s+|we\s+|you\s+)?just\b"
    r"|\bwhy\s+not\s+just\b"
    r"|\b(?:isn'?t|wouldn'?t|won'?t)\s+it\s+(?:be\s+)?"
    r"(?:easier|simpler|better|faster|quicker|cleaner|nicer|safer|more\s+\w+)\b"
    r"|\bdo\s+(?:i|we)\s+(?:(?:really|even|actually)\s+need\b|need\s+to\b)"
    r"|\bis\s+there\s+(?:any\s+)?(?:need|reason|point)\s+(?:to|in)\b",
    re.IGNORECASE,
)


def looks_like_pivot(msg: str) -> bool:
    """§17.679/§17.691 — True when `msg` changes direction / reshapes the plan
    (vs asking about or refining the current step). Deterministic (no LLM)."""
    if not msg:
        return False
    msg = normalize_punct(msg)
    return (bool(_PIVOT_RE.search(msg))
            or bool(_GLOBAL_CHANGE_RE.search(msg))
            or bool(_QUESTION_PIVOT_RE.search(msg)))


def pivot_kind(msg: str) -> str:
    """A whole-deliverable change is a `preference` (fan out to all steps); a
    directional change is a `decision`. Both are plan-affecting → §17.677 runs."""
    return "preference" if _GLOBAL_CHANGE_RE.search(normalize_punct(msg or "")) else "decision"


# ── Help / how-to detection (§17.733/§17.763/§17.768) ─────────────────────────
_HOWTO_QUESTION_RE = re.compile(
    r"\b(?:"
    r"how\s+(?:do|can|would|should|to)\b|"
    r"am\s+i\s+(?:supposed|meant)\s+to\b|are\s+we\s+(?:supposed|meant)\s+to\b|"
    r"should\s+i\b|should\s+we\b|do\s+i\s+(?:need|have)\s+to\b|"
    r"which\s+(?:one|option|selection|.*\bshould)\b|"
    r"what\s+(?:do|should)\s+(?:i|we)\b|what'?s\s+the\s+best\s+way\b|"
    r"best\s+way\s+to\b|is\s+it\s+better\s+to\b|is\s+there\s+a\s+way\b|"
    r"why\s+(?:is|does|won'?t|can'?t|isn'?t)\b"
    r")",
    re.I,
)


def looks_like_howto_question(msg: str) -> bool:
    """§17.733 — True when `msg` is a help-seeking how-to/which/should-I question
    that deserves a researched answer, not a re-render of the current step."""
    if not msg:
        return False
    return bool(_HOWTO_QUESTION_RE.search(normalize_punct(msg)))


_HELP_REQUEST_RE = re.compile(
    r"\b(?:"
    r"help\s+me\b|help\s+(?:with|out|addressing)\b|"
    r"(?:can|could|would|will)\s+(?:you|u)\s+help\b|"
    r"i\s+need\s+(?:some\s+|your\s+)?help\b|(?:i\s+)?need\s+(?:a\s+)?hand\b|"
    r"give\s+me\s+a\s+hand\b|lend\s+me\s+a\s+hand\b|"
    r"walk\s+me\s+through\b|guide\s+me\b|show\s+me\s+how\b|"
    r"i'?m\s+stuck\b|i\s+am\s+stuck\b|(?:i'?m\s+)?stuck\s+(?:on|with|at)\b|"
    r"having\s+(?:trouble|issues|a\s+hard\s+time|difficulty)\b|trouble\s+with\b|"
    r"struggling\s+(?:with|to)\b|i\s+(?:don'?t|do\s+not)\s+know\s+how\b|"
    r"not\s+sure\s+how\b|can'?t\s+(?:figure|work)\s+(?:this\s+|it\s+)?out\b|"
    r"assist\s+me\b|need\s+(?:some\s+|your\s+)?assistance\b|"
    r"(?:can|could)\s+you\s+(?:assist|walk)\b"
    r")",
    re.I,
)


def looks_like_help_request(msg: str) -> bool:
    """§17.763 — True when `msg` is an explicit request for hands-on help with the
    current task (not a plan change). Deliberately narrow so a genuine pivot —
    caught upstream by `looks_like_pivot` — still wins."""
    if not msg:
        return False
    return bool(_HELP_REQUEST_RE.search(normalize_punct(msg)))


# ── The post-filter ───────────────────────────────────────────────────────────
# `_TEXT_FILL_FIELDS` are filled from the message ONLY when the LLM left them
# blank (it may have extracted a cleaner value); routing fields are always set.
_TEXT_FILL_FIELDS = ("evidence", "error_text", "query", "note_text")


# §17.867 — pure orientation asks ("whats next??", "what now", "where are we").
# Live incident: the /decide model routed "whats next??" to NOTE — the question
# was recorded into the notes ledger and nothing moved. The phrasing is
# unambiguous and fully anchored (a longer question like "what's next after I
# configure X" does NOT match), so it overrides ANY model action except the
# shell-evidence gate above it. Maps to `status` — orient, never close a step
# (advance would commit work the operator hasn't reported).
_WHATS_NEXT_RE = re.compile(
    r"^\s*(?:(?:so|ok(?:ay)?)[,!\s]+)*"
    r"(?:what(?:'?s)?\s+(?:is\s+)?next"
    r"|what\s+(?:do|should)\s+(?:i|we)\s+do(?:\s+(?:now|next))?"
    r"|what\s+now|now\s+what"
    r"|where\s+(?:are\s+we|am\s+i)(?:\s+at)?"
    r"|next\s+steps?)"
    r"\s*[?!.\s]*$",
    re.IGNORECASE,
)


def looks_like_whats_next(message: str) -> bool:
    return bool(_WHATS_NEXT_RE.match(message or ""))


def _override(action: str, message: str, signals: dict) -> tuple[str, str | None, dict]:
    """Return (new_action, reason|None, patch). Precedence mirrors the pipeline
    cascade: shell-result (fix > submit) → pivot → help/how-to. A `None` reason
    means the LLM's decision stands unchanged."""
    msg = message or ""
    # 1. A pasted shell prompt line IS the operator reporting this step's result.
    #    An error / mid-fix paste is a diagnostic reply → fix (do NOT advance past
    #    a broken command, §17.748/§17.749); a clean paste → submit (§17.705).
    if signals.get("shell_paste"):
        if signals.get("shell_error") or signals.get("last_assistant_was_fix"):
            if action != "fix":
                return "fix", "shell_error", {"error_text": msg.strip()}
            return action, None, {}
        if action not in ("submit", "fix"):
            return "submit", "shell_result", {"evidence": msg.strip()}
        return action, None, {}
    # 2. §17.867 — a pure orientation ask maps to `status` no matter what the
    #    model said (live: "whats next??" was confidently routed to NOTE and
    #    recorded as ledger junk). Never `advance` — orientation must not close
    #    a step the operator hasn't reported on.
    if looks_like_whats_next(msg):
        if action != "status":
            return "status", "whats_next", {}
        return action, None, {}
    # 3. A declarative or question-framed pivot reshapes the plan → note (§17.679/
    #    §17.691). Fires on skip/question/ask: the live A/B (§17.855) showed the
    #    /decide model routes QUESTION-FRAMED pivots ("can't we just … instead?")
    #    to `ask` more often than the old client classifier did, so gating on
    #    skip/question alone (as the cascade does) let real pivots escape to
    #    research. `_QUESTION_PIVOT_RE` is anchored on pivot framing ("can't I
    #    just", "why not just", "isn't it easier", "do I even need"), distinct
    #    from a plain how-to, so widening to `ask` stays precise. A confident
    #    submit is still left alone (a completion is not a pivot).
    if action in ("skip", "question", "ask") and looks_like_pivot(msg):
        return "note", "pivot", {
            "note_text": msg.strip(),
            "note_kind": pivot_kind(msg),
            "plan_impact": "surface",
        }
    # 4. An explicit help / how-to question is help-seeking, not a step-completion
    #    or a plan change → ask (research, §17.733/§17.763/§17.768). Pivot already
    #    won above, so a help request that also states a pivot still re-plans.
    if action == "question" and (looks_like_howto_question(msg)
                                 or looks_like_help_request(msg)):
        return "ask", "help_howto", {"query": msg.strip()}
    return action, None, {}


def apply_deterministic_overrides(decision: dict, message: str) -> dict:
    """Post-filter a `decide_turn` Decision with the deterministic gates. Returns
    the decision unchanged when no gate fires; otherwise returns a new dict with
    the overridden `action` (+ filled params), `confidence='high'` so the caller
    dispatches it, and an `override` reason stamped for observability."""
    signals = decision.get("signals") or {}
    action = decision.get("action")
    new_action, reason, patch = _override(action, message or "", signals)
    if reason is None:
        return decision
    out = dict(decision)
    for k, v in patch.items():
        if k in _TEXT_FILL_FIELDS:
            if not (out.get(k) or "").strip():
                out[k] = v
        else:
            out[k] = v
    out["action"] = new_action
    out["confidence"] = "high"
    out["override"] = reason
    out["rationale"] = f"[deterministic:{reason}] " + (out.get("rationale") or "")
    return out
