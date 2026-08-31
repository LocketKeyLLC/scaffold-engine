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


# ── Completion-claim detection (§17.890) ─────────────────────────────────────
# The live failure this exists for: the operator told the engine — repeatedly —
# that they had completed the step ("I did that already", "it's installed"),
# decide routed to submit, and the §17.731 success verifier judged the bare
# claim AS IF it were pasted evidence: a claim shows no deliverable, so the
# verdict came back 'incomplete', the blocking valve refused the commit, and
# the §17.884 continuation walked the operator into a fix flow for a step they
# had already finished. The verifier exists to judge EVIDENCE, not to overrule
# the human: a bare completion claim (short, no shell paste, no error signal,
# no negation) is the operator's explicit word and must commit.
#
# Precision-first like every gate in this file: a long message or anything
# paste-shaped stays on the evidence path, questions are never claims, and any
# negation/failure wording disqualifies. Recall has backstops (the decide
# prompt, and a conservative verifier returning 'unclear' → unblocked).
_CLAIM_SHELL_PROMPT_RE = re.compile(  # same shape as _assist_handlers'
    r"(?m)^\s*[A-Za-z_][\w.-]*@[\w.-]+:[^\n#$]*[#$]")  # _SHELL_PROMPT_LINE_RE
_CLAIM_DISQUALIFY_RE = re.compile(
    r"\b(?:not|never|haven'?t|hasn'?t|isn'?t|wasn'?t|aren'?t|didn'?t|don'?t|"
    r"doesn'?t|can'?t|cannot|couldn'?t|won'?t|wouldn'?t|unable|"
    r"fail(?:ed|s|ing)?|error(?:s|ed)?|broke(?:n)?|stuck|trouble|issue|problem|"
    r"no\s+luck|except|but\s+(?:it|the|when|now))\b",
    re.IGNORECASE,
)
_CLAIM_PHRASE_RE = re.compile(
    r"(?:^\s*(?:done|finished|complete[d]?|all\s+done)[.!\s]*$)"
    r"|\bi(?:'?ve|\s+have|\s+just)?\s+(?:already\s+|just\s+)?"
    r"(?:did|done|completed|finished|installed|configured|ran|handled|"
    r"took\s+care\s+of|set(?:\s+\w+)?\s+up)\b"
    r"|\b(?:it|that|this|step|task|everything)(?:'?s|\s+is|\s+was|\s+has\s+been)?"
    r"\s+(?:all\s+|already\s+|now\s+)?"
    r"(?:done|complete[d]?|finished|installed|configured|working|running|"
    r"up\s+and\s+running|set\s+up|in\s+place|taken\s+care\s+of|handled)\b"
    r"|\balready\s+(?:did|done|completed|finished|installed|handled)\b"
    r"|\b(?:it|that)\s+worked\b"
    r"|\bwork(?:s|ed|ing)\s+(?:now|fine|great|perfectly)\b"
    r"|\ball\s+set\b|\bgood\s+to\s+go\b|\bwe(?:'re|\s+are)\s+good\b",
    re.IGNORECASE,
)


def looks_like_completion_claim(msg: str) -> bool:
    """§17.890 — True when `msg` is the operator's bare ASSERTION that the
    current step is complete (vs pasted evidence, a question, or an error
    report). Deterministic, precision-first: used to (a) route such messages to
    submit and (b) exempt them from the §17.731 incomplete/failed hard-block —
    the operator's explicit word outranks a verifier that, by construction,
    cannot see their machine."""
    if not msg:
        return False
    m = normalize_punct(msg).strip()
    if len(m) > 280:            # long messages are evidence/reports, not claims
        return False
    if "?" in m:                # a question is never a completion claim
        return False
    if re.match(  # question-shaped without the "?" ("is it done", "did you…")
            r"^\s*(?:is|are|was|were|does|do|did|has|have|had|will|would|"
            r"should|shall|can|could|why|how|what|when|where|who|which)\b",
            m, re.IGNORECASE):
        return False
    if _CLAIM_SHELL_PROMPT_RE.search(m):   # paste-shaped → the evidence path
        return False
    if re.match(r"^\s*(?:when|once|after|until|if)\b", m, re.IGNORECASE):
        return False            # "once it's done…" is a plan, not a claim
    if looks_like_howto_question(m) or looks_like_help_request(m):
        return False            # "how do I know it's done" is help-seeking
    if _CLAIM_DISQUALIFY_RE.search(m):     # negation / failure wording
        return False
    return bool(_CLAIM_PHRASE_RE.search(m))


# ── Advancement signal (§17.891) ─────────────────────────────────────────────
# The mirror image of §17.890. Live incident (2026-08-31 02:40): the §17.754
# tracker — confidence above threshold, current_step_done=true — retired
# "Create PalWorld VM" off the message "I want to build a markdown linter".
# The tracker's LLM verdict alone must never retire a step: a retire needs a
# deterministic ADVANCEMENT SIGNAL in the operator's own words.
_ADVANCE_INTENT_RE = re.compile(
    r"^\s*(?:next(?:\s+step)?|continue|move\s+on|proceed|"
    r"skip(?:\s+(?:it|this|that|(?:this\s+)?step))?)[.!\s]*$",
    re.IGNORECASE,
)


def has_advancement_signal(msg: str) -> bool:
    """§17.891 — True when `msg` deterministically supports RETIRING the
    current step: an explicit completion claim (§17.890), an explicit
    next/skip/continue intent, or a clean (error-free) shell paste. Everything
    else — questions, new-project asks, notes, noise — must never close a step,
    no matter how confident the tracker's verdict is."""
    if not msg:
        return False
    m = normalize_punct(msg).strip()
    if _ADVANCE_INTENT_RE.match(m):
        return True
    if looks_like_completion_claim(m):
        return True
    # Lazy import — assist_decide imports this module at load time; by call
    # time it is fully initialized. Reuses the ONE copy of the shell regexes.
    from app.modules import assist_decide
    sig = assist_decide._compute_signals(m, None)
    return bool(sig["shell_paste"] and not sig["shell_error"])


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
    # 3. §17.890 — an explicit completion CLAIM is a submit, no matter what the
    #    model said (live: "I did that already" was routed to question/ask →
    #    tracker "isn't sure" → dead end, while the operator repeated themselves).
    #    Terminal-ish routes are left alone: submit is already right, fix can't
    #    co-occur (the error guard disqualifies the claim), skip/pause/finalize
    #    are the operator's explicit verbs the model chose for a reason.
    if action in ("question", "ask", "note", "status", "advance") \
            and looks_like_completion_claim(msg):
        return "submit", "completion_claim", {"evidence": msg.strip()}
    # 4. A declarative or question-framed pivot reshapes the plan → note (§17.679/
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
    # 5. An explicit help / how-to question is help-seeking, not a step-completion
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
