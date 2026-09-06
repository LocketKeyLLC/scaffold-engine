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


# ── §17.903 — blocked-on-a-prerequisite detection ────────────────────────────
#
# The live failure: the operator reported "i hit the reboot now and its still
# hung up" while the plan pointer sat on "Install PalWorld server". The engine
# had no representation of "this operator cannot reach the current step at all",
# so the next walkthrough opened with `sudo apt update` on a VM whose own
# Prerequisites section said it must be "fully installed and reachable via
# shell" — the exact thing they had just said was broken.
#
# Deliberately deterministic: a blocker report is a plain, recognisable shape,
# and putting an LLM in front of it would add a call per turn to decide
# something a regex settles. Precision-first — a bare error paste is NOT a
# blocker (that is the §17.874 fix path); a blocker is the operator saying the
# work is not progressing, usually after a prior attempt.
_BLOCKED_RE = re.compile(
    # "it's still hung", "still not working", "still stuck", "still failing"
    r"\bstill\s+(?:hung|hanging|stuck|frozen|freezing|not\s+working|"
    r"broken|failing|fails|failed|down|the\s+same)\b"
    # "it's hung up", "it hangs", "frozen at", "stuck on/at"
    r"|\b(?:is\s+|it'?s\s+|its\s+|has\s+)?hung(?:\s+up)?\b"
    r"|\b(?:stuck|frozen|hanging)\s+(?:on|at|in)\b"
    r"|\bwon'?t\s+(?:boot|start|load|come\s+up|finish|complete)\b"
    r"|\bkeeps?\s+(?:hanging|freezing|failing|crashing|restarting|looping)\b"
    r"|\bnot\s+(?:responding|responsive)\b"
    r"|\bcan'?t\s+(?:get\s+(?:in|past)|proceed|continue|move\s+(?:on|forward))\b"
    r"|\bnothing\s+(?:happens|is\s+happening)\b",
    re.IGNORECASE,
)

# Work that is progressing is not a blocker, however slow. Without this,
# "still downloading" and "still installing" read as stuck.
_BLOCKED_IN_PROGRESS_RE = re.compile(
    r"\bstill\s+(?:going|running|downloading|installing|copying|building|"
    r"working|processing)\b",
    re.IGNORECASE,
)


def looks_like_blocked(msg: str) -> bool:
    """§17.903 — True when the operator reports being STUCK, not merely erroring.

    The distinction that matters: an error paste means "this command failed,
    diagnose it" (§17.874 fix). A blocker means "I cannot get to where your
    step assumes I already am" — which invalidates the step's premise, so
    continuing to guide the step at all is wrong.
    """
    if not msg:
        return False
    m = normalize_punct(msg).strip()
    if _BLOCKED_IN_PROGRESS_RE.search(m):
        return False
    return bool(_BLOCKED_RE.search(m))


# §17.903 — a question that wants a DECISION from the engine ("should I…?",
# "do we…?", "…or…?"). These are the ones the operator most needs a lean on,
# and the ones a pivot override was silently swallowing.
_DECISION_QUESTION_RE = re.compile(
    r"^\s*(?:should|shall|do|does|did|can|could|would|will|is|are|was|were|"
    r"ought)\b.*\?\s*$"
    r"|\bshould\s+(?:i|we|you)\b"
    r"|\bdo\s+(?:i|we)\s+(?:need|have)\s+to\b"
    r"|\bor\s+should\s+(?:i|we)\b"
    r"|\bwhich\s+(?:one|option|way)\b"
    r"|\bwhat\s+do\s+you\s+(?:think|recommend|suggest)\b"
    # §17.903 — IMPERATIVE-framed proposals. The live message was "…perhaps we
    # should start over. Delete this VM and start over?" — a yes/no question
    # with no interrogative opener, which the patterns above all miss. These are
    # exactly the questions that most need a lean, because the operator is
    # proposing to throw work away and wants to know if they should.
    r"|\b(?:perhaps|maybe|should)\s+(?:i|we)\b"
    r"|^\s*(?:delete|remove|destroy|rebuild|reinstall|restart|reset|wipe|"
    r"start\s+over|scrap|redo|revert|roll\s*back|switch|try)\b[^?]*\?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def wants_a_recommendation(msg: str) -> bool:
    """§17.903 — True when the operator asked something that deserves a LEAN,
    not a menu. "Delete this VM and start over?" is answerable; laying out
    options without a recommendation is what left them stuck."""
    if not msg:
        return False
    m = normalize_punct(msg).strip()
    if "?" not in m and not _DECISION_QUESTION_RE.search(m):
        return False
    return bool(_DECISION_QUESTION_RE.search(m))


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
    r"no\s+luck|except|but\s+(?:it|the|when|now)|"
    # §17.891b — partial/none wording means the work is NOT complete.
    r"nothing|none|partial(?:ly)?|partly|half(?:way)?|almost|nearly|mostly)\b",
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
    r"|\ball\s+set\b|\bgood\s+to\s+go\b|\bwe(?:'re|\s+are)\s+good\b"
    # §17.891b — CI caught real claim phrasings the noun-list missed ("that
    # whole install is done", "done with this one"). Generic-subject completion
    # states — deliberately WITHOUT working/running here (a download that "is
    # running" is in progress, not complete; those stay it/that/this-only above).
    r"|\b(?:is|are|was|were|has\s+been|have\s+been)\s+(?:all\s+|already\s+|now\s+)?"
    r"(?:done|complete[d]?|finished|installed|configured|set\s+up|in\s+place|"
    r"taken\s+care\s+of)\b"
    # §17.950 — the construction listed only "done"; "finished with that" and
    # "complete with this" are the same claim in the same shape.
    r"|\b(?:done|finished|complete[d]?)\s+with\s+(?:this|that|it|everything|the)\b"
    r"|\b(?:on\s+to|onto)\s+the\s+next\b",
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

# §17.950 — an explicit REQUEST to move on, anywhere in the message.
#
# `_ADVANCE_INTENT_RE` is anchored `^…$`, so it only fires on a message that is
# NOTHING but "next"/"continue". And `looks_like_completion_claim` bails on any
# "?" — correctly, a question is not a claim. Between them, the single most
# natural way an operator says "I finished, move me on" signalled NOTHING:
#
#     "i am connected via SSH! whats next?"      (live, 2026-09-06 01:16:59)
#
# It claims completion AND asks to advance, and scored False on both gates, so
# §17.891 vetoed the tracker's verdict and the operator stayed on the step.
#
# This is deliberately a SEPARATE signal class from a completion claim: it is
# INTENT, and questions are exactly how intent gets phrased. Safe to read
# loosely because `has_advancement_signal` is a VETO, not a decision — the
# tracker must still independently judge `current_step_done` with confidence
# above threshold before anything retires. Restoring the tracker's vote is all
# this does.
#
# "what's the NEXT COMMAND" stays out by construction: the request forms below
# require `next` to follow `what/what's` directly, so an intervening noun
# ("the next command", "the next file") does not match.
_NEXT_REQUEST_RE = re.compile(
    # `next` must END the clause — "whats next?" yes, "what next COMMAND" no.
    # The first cut assumed an article ("the next command") and let the
    # article-less form through, which turned "it failed, what next command
    # should i try" into an advancement signal.
    r"\bwhat(?:'?s|\s+is)?\s+next\b(?!\s+\w)"
    r"|\bwhat\s+now\b"
    r"|\bon\s+to\s+the\s+next\b"
    r"|\bmove\s+on\b"
    r"|\bnext\s+step\b"
    r"|\bwhere\s+to\s+next\b",
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
    # §17.950 — an operator reporting a FAILURE is not asking to move on, however
    # they phrase the rest of the sentence. The disqualifier that already guards
    # completion claims guards this signal class too.
    if _NEXT_REQUEST_RE.search(m) and not _CLAIM_DISQUALIFY_RE.search(m):
        return True
    if looks_like_completion_claim(m):
        return True
    # Lazy import — assist_decide imports this module at load time; by call
    # time it is fully initialized. Reuses the ONE copy of the shell regexes.
    from app.modules import assist_decide
    sig = assist_decide._compute_signals(m, None)
    return bool(sig["shell_paste"] and not sig["shell_error"])


# ── Completion EVIDENCE (§17.915) ────────────────────────────────────────────
# §17.891's `has_advancement_signal` decides whether the pointer may MOVE. It
# accepts three things, and one of them must never close a step: a bare
# next/continue/skip intent means "move on", not "this is done". The tracker
# retire path treated all three alike, marked the node `done` with a fabricated
# note ("Completed by the operator in assist mode"), and wrote NO evidence at
# all — `assist_steps.evidence_kind` stayed NULL.
#
# Live (session 613dd1df, 2026-09-03 20:06:16): ADD5 "Install Ubuntu Server
# 22.04 on VM 106" — the step inserted BECAUSE the operator could not get the OS
# installed — was closed with zero evidence while the OS was not installed. The
# same shape that put T23 wrongly `done` (§17.911 repaired the consequence;
# nothing stopped the close).
#
# `_retire_step_mirrored`'s own docstring already draws the line: "The ⏩ Skip
# verb (deliberate skip, work NOT done) still writes 'skipped' via the submit
# path — the two are semantically different and now recorded differently."
# Retiring on a bare advance intent erases that distinction.
def is_completion_evidence(msg: str) -> bool:
    """§17.915 — True when `msg` can justify marking a step DONE.

    Strictly narrower than `has_advancement_signal`: an explicit completion
    claim (§17.890 — the operator's own word, which outranks a verifier that
    cannot see their machine) or a clean, error-free shell paste. A bare
    next/continue/skip intent is deliberately EXCLUDED — it moves the pointer,
    it does not evidence the work.
    """
    if not msg or not msg.strip():
        return False
    m = normalize_punct(msg).strip()
    if looks_like_completion_claim(m):
        return True
    from app.modules import assist_decide
    sig = assist_decide._compute_signals(m, None)
    return bool(sig["shell_paste"] and not sig["shell_error"])


# ── Completion DENIAL (§17.899) ───────────────────────────────────────────────
# The missing half of §17.890. That change let the operator's word outrank the
# verifier — correctly, since the verifier cannot see their machine. But it gave
# a claim about the WRONG THING the same power, and nothing could take it back.
#
# Live incident (2026-08-31 23:13, HomeLab session): the operator wrote "It
# worked Ubuntu Server is now downloading!" — a genuine completion claim, about
# the OS ISO. It landed on step T23 "Install PalWorld server" and closed it.
# 62 seconds later: "But we have ONLY installed the ubuntu server and have not
# installed anything else." The correction was correctly NOT read as a claim —
# and then nothing listened for it. T23 stayed `done` with the bogus output
# "It worked Ubuntu Server is now downloading!", the plan's PalWorld install
# work silently migrated into T24 "Configure PalWorld service", and T24 could
# never satisfy its own goal. It churned for 22 hours.
#
# So: a denial is not merely "not a claim" — it is an active signal that must
# REOPEN the step the operator is talking about. Precision-first, same guards as
# §17.890: an error report about the CURRENT step is a `fix`, not a denial, and
# the caller additionally bounds this to the step just committed.
_DENIAL_PHRASE_RE = re.compile(
    # "we have not installed anything else", "I haven't done that yet"
    r"\b(?:i|we|you)?\s*(?:have|has|had)?\s*(?:not|n'?t)\s+"
    r"(?:yet\s+)?(?:actually\s+)?"
    r"(?:done|did|finished|completed|installed|configured|created|"
    r"set\s+(?:it\s+)?up|run|ran|started)\b"
    r"|\b(?:haven'?t|hasn'?t|didn'?t|did\s+not)\s+(?:yet\s+)?(?:actually\s+)?"
    r"(?:done|do|finished|finish|completed|complete|installed|install|"
    r"configured|configure|created|create|set\s+up|run|ran|start(?:ed)?)\b"
    # "that isn't done", "this step was not finished", "it's not complete".
    # \s* (not \s+) before the negation: in "isn't" the contraction hangs
    # directly off the verb with no space, and that is the commonest phrasing.
    r"|\b(?:that|this|it|the\s+step|that\s+step|this\s+step)"
    r"(?:'?s|\s+is|\s+was|\s+are|\s+were|\s+has\s+been)?\s*(?:still\s+)?(?:not|n'?t)\s+"
    r"(?:yet\s+)?(?:done|complete[d]?|finished|installed|configured|set\s+up)\b"
    # "nothing was installed", "nothing else has been done"
    r"|\bnothing\s+(?:else\s+)?(?:was|is|has\s+been|got)\s+"
    r"(?:done|installed|configured|created|set\s+up)\b"
    # "we ONLY installed the ubuntu server" — the live phrasing: an assertion
    # that the work done was LESS than what the step claimed.
    r"|\bonly\s+(?:done|did|installed|configured|created|finished|"
    r"set\s+up|got)\b"
    # "not done yet" / "still not done" — FULLY ANCHORED, unlike the branches
    # above which carry their own subject. Unanchored it fired on "the download
    # is not finished yet, still going", which is a progress report about the
    # CURRENT step, not a denial that a closed one happened.
    r"|^\s*(?:still\s+)?not\s+(?:done|complete[d]?|finished)\s*(?:yet)?[.!\s]*$",
    re.IGNORECASE,
)

# Work still in flight is a progress report, not a denial — the step the
# operator is describing is the one they are ON, not one the engine closed.
_DENIAL_IN_PROGRESS_RE = re.compile(
    r"\b(?:still\s+(?:going|running|downloading|installing|working)|"
    r"in\s+progress|currently\s+(?:downloading|installing|running))\b",
    re.IGNORECASE,
)

# A denial is about work NOT happening. These say the work DID happen and
# something then went wrong — that is the §17.874 fix path, not a reopen.
_DENIAL_DISQUALIFY_RE = re.compile(
    r"\b(?:error|traceback|exception|failed\s+to\s+start|command\s+not\s+found|"
    r"permission\s+denied|no\s+such\s+file)\b",
    re.IGNORECASE,
)


def looks_like_completion_denial(msg: str) -> bool:
    """§17.899 — True when `msg` is the operator asserting that work the engine
    believes is DONE was not actually done. The mirror of
    ``looks_like_completion_claim``; the caller decides WHICH step it reopens.
    """
    if not msg:
        return False
    m = normalize_punct(msg).strip()
    if len(m) > 400:            # long messages are evidence/reports
        return False
    if "?" in m:                # "did we install that?" is a question, not a denial
        return False
    if _CLAIM_SHELL_PROMPT_RE.search(m):   # paste-shaped → the evidence path
        return False
    if _DENIAL_DISQUALIFY_RE.search(m):    # an error report → the fix path
        return False
    if _DENIAL_IN_PROGRESS_RE.search(m):   # work in flight → a progress report
        return False
    if looks_like_howto_question(m) or looks_like_help_request(m):
        return False
    return bool(_DENIAL_PHRASE_RE.search(m))


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
        # §17.903 — the note is still recorded, but a pivot framed as a QUESTION
        # must also be ANSWERED. Live failure: "i hit the reboot now and its
        # still hung up, perhaps we should start over. Delete this VM and start
        # over?" was classified `ask` by the model, overridden to `note` here,
        # filed, and the turn ended SILENTLY — the operator's direct question got
        # no answer at all, and the next walkthrough then guided them into a step
        # whose prerequisites they had just reported broken.
        #
        # `answer_query` tells the turn loop to continue into the answer flow
        # after recording. Recording and answering were never in conflict; the
        # override just happened to be written as a replacement.
        params = {
            "note_text": msg.strip(),
            "note_kind": pivot_kind(msg),
            "plan_impact": "surface",
        }
        if "?" in msg or wants_a_recommendation(msg) or looks_like_blocked(msg):
            params["answer_query"] = msg.strip()
        return "note", "pivot", params
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


# ── Completion confirmation (§17.951) ────────────────────────────────────────
# §17.890 already commits on a BARE claim ("it's installed") — the operator's
# word outranks a verifier that cannot see their machine. But that gate is
# deliberately narrow: it rejects paste-shaped input (that is the evidence
# path), anything with a "?", and anything over 280 chars. So the common real
# shape — evidence PLUS an assertion, or a long report ending "all of that was
# downloaded" — took the evidence path, came back `incomplete`, and the
# operator was handed another fix instead of being asked.
#
# The fix is not a looser claim detector; widening §17.890 would let genuine
# not-done reports commit steps. It is to ASK. When a submit is blocked, the
# engine offers the operator the commit on their word, and a plain affirmative
# takes it.
#
# This detector is loose ON PURPOSE and safe only because it is SCOPED: it is
# consulted exclusively when a confirmation is already pending on the session
# (`metadata.pending_completion_confirm`). "yes" means confirm only when the
# engine has just asked something.
_CONFIRM_RE = re.compile(
    r"^\s*(?:"
    r"confirm(?:ed|ing)?|"
    r"yes|yeah|yep|yup|correct|right|affirmative|"
    r"(?:that'?s|thats|that\s+is|it\s+is)\s+(?:right|correct|it|done)|"
    r"i\s+confirm|"
    r"it\s+(?:is|was)\s+(?:done|complete[d]?|finished|installed)|"
    r"(?:all\s+)?(?:done|complete[d]?|finished|installed)|"
    r"mark\s+it\s+(?:done|complete[d]?)|"
    r"commit\s+it"
    r")\b[.! ]*$",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(
    r"^\s*(?:no|nope|not\s+(?:yet|done|quite)|cancel|wait|hold\s+on|"
    r"(?:that'?s|thats)\s+(?:wrong|not\s+right))\b",
    re.IGNORECASE,
)


def looks_like_confirmation(msg: str) -> bool:
    """§17.951 — the operator answering YES to a pending completion offer.

    Only ever consulted while a confirmation is staged on the session, which is
    what makes a bare "yes" safe to read this way.
    """
    if not msg:
        return False
    return bool(_CONFIRM_RE.match(normalize_punct(msg).strip()))


def looks_like_decline(msg: str) -> bool:
    """§17.951 — the operator answering NO. Clears the offer without committing;
    the step stays open and the conversation continues normally."""
    if not msg:
        return False
    return bool(_DECLINE_RE.match(normalize_punct(msg).strip()))


# ── §17.958 — the operator is sitting AT an interactive prompt ─────────────
#
# Live, session 613dd1df/T32 (2026-09-06 14:47): *"its asking whick linter to
# use"*. The reply told them to re-run the whole `npm create vite` command AND,
# in the same breath, *"simply type `eslint` and press Enter"*. Their own paste
# one turn later shows what was actually on screen — a clack SELECT menu:
#
#     o  Which linter to use?
#     |  ESLint
#
# You do not type into that; you arrow to the entry and press Enter. So the
# instruction was wrong for the widget in front of them, and the engine had
# already been shown that widget. It models a command as something that runs to
# completion, and has no notion of one that stops and waits.
#
# Two things follow deterministically and neither needs a model: while a prompt
# is pending the command must NOT be re-issued (they are already inside it), and
# the keystroke advice depends on the prompt KIND.

_PROMPT_CONFIRM_RE = re.compile(
    r"\[y/n\]|\(y/n\)|\[y/n,[^\]]*\]|\bok to proceed\?|\(yes/no\)|\[yes/no\]",
    re.IGNORECASE,
)
# Arrow-key pickers: inquirer/prompts (`❯`), clack (box-drawing rail, flattened
# to `|`/`o` by many terminals on copy), and the explicit hint some emit.
_PROMPT_SELECT_RE = re.compile(
    r"use arrow keys|❯|›\s*$|◆|◇|^\s*[|│]\s{2}\S|^\s*[o○]\s{2}\S.*\?\s*$"
    r"|press <enter> to select|\(use arrow",
    re.IGNORECASE | re.MULTILINE,
)
_PROMPT_TEXT_RE = re.compile(r"^\s*\?\s+\S[^\n]{2,80}[:›]\s*$", re.MULTILINE)
# The operator SAYING they are stuck at one. "asking" is the load-bearing word.
_PROMPT_SPOKEN_RE = re.compile(
    r"\b(?:it'?s?|its|it is)\s+asking\b|\bis\s+asking\s+me\b"
    r"|\basking\s+(?:me\s+)?(?:which|what|for|if|whether)\b"
    r"|\bstuck\s+(?:at|on)\s+a?\s*prompt\b|\bwaiting\s+for\s+(?:my\s+)?input\b",
    re.IGNORECASE,
)


def detect_interactive_prompt(text: str) -> dict | None:
    """Is the operator parked at an interactive prompt right now?

    Returns ``{"kind": "select"|"confirm"|"text"|"unknown", "how": <advice>}``
    or None. `kind` decides the keystroke advice, which is the half the engine
    got wrong live — telling someone to TYPE an option at an arrow-key menu.
    """
    if not (text or "").strip():
        return None
    kind = None
    if _PROMPT_SELECT_RE.search(text):
        kind = "select"
    elif _PROMPT_CONFIRM_RE.search(text):
        kind = "confirm"
    elif _PROMPT_TEXT_RE.search(text):
        kind = "text"
    elif _PROMPT_SPOKEN_RE.search(text):
        kind = "unknown"
    if not kind:
        return None
    return {"kind": kind, "how": _PROMPT_ADVICE[kind]}


_PROMPT_ADVICE = {
    "select": ("an arrow-key menu — move with ↑/↓ to the entry you want and "
               "press Enter. Do NOT type the option's name."),
    "confirm": "a yes/no prompt — press `y` (or `n`) then Enter.",
    "text": "a free-text prompt — type the value, then press Enter.",
    "unknown": ("a prompt of some kind — ask the operator to paste the exact "
                "lines on screen before guessing which keys to press."),
}


# ── §17.959 — answer the question in the interface it was asked about ─────
#
# Live, session 613dd1df/T31 (2026-09-06 14:03), the operator's own words:
#
#     "I need a better explanation on implementing the firewall in the web
#      browser. There are far more options then what you are telling me to add
#      with not enough context."
#
# The reply opened *"Since the Web UI is confusing and the `pct` command doesn't
# support security groups, skip the security groups for now"* and handed over
# `pct start 111`. A direct request for GUI guidance, answered by declining to
# give it. The CLI may well be the better route — but that is an argument to
# make AFTER answering, not a reason to leave the question standing.

_GUI_SCOPE_RE = re.compile(
    r"\bweb\s?(?:browser|ui|interface|gui|console)\b|\bwebui\b|\bgui\b"
    r"|\bin the browser\b|\bwhich button\b|\bwhat button\b|\bwhich (?:menu|tab|field|box)\b"
    r"|\bwhere do i click\b|\bon the (?:screen|dashboard|page)\b|\bproxmox (?:ui|web)\b",
    re.IGNORECASE,
)
_ASK_SHAPE_RE = re.compile(
    r"\?|\bi need\b|\bexplain\b|\bhow do i\b|\bhow to\b|\bwalk me\b|\bshow me\b"
    r"|\bnot enough context\b|\bbetter explanation\b",
    re.IGNORECASE,
)
# Navigation language — what a real GUI answer necessarily contains.
_GUI_NAV_RE = re.compile(
    r"\bclick\b|\bselect\b|\bnavigate\b|\bexpand\b|\btick\b|\buncheck\b|\bcheckbox\b"
    r"|\bdrop-?down\b|\bbutton\b|\btab\b|\bpanel\b|\bsidebar\b|\bdialog\b|→|➜"
    r"|\bunder\s+[A-Z]\w+\b|\bthe\s+\w+\s+field\b",
    re.IGNORECASE,
)


def looks_like_gui_question(msg: str) -> bool:
    """A question explicitly scoped to a graphical interface."""
    if not (msg or "").strip():
        return False
    return bool(_GUI_SCOPE_RE.search(msg) and _ASK_SHAPE_RE.search(msg))


def answer_dodges_the_interface(answer: str, question: str) -> bool:
    """True when a GUI question came back with no GUI in the answer.

    Deliberately generous to the answer: ANY navigation language anywhere
    clears it. This fires only on the shape that actually happened — a GUI
    question answered purely in shell commands.
    """
    if not looks_like_gui_question(question) or not (answer or "").strip():
        return False
    return not _GUI_NAV_RE.search(answer)


# ── §17.962 — the paste never made it into the shell intact ───────────────
#
# Live, session 613dd1df, T34 at 17:56 and again at 19:20 — byte-identical
# corruption both times, at the tail of a ~3.6 KB heredoc:
#
#     ...>Loading Lab StaEOFort default App;>ton onClick={() => handlePower(
#
# Three fragments spliced together: line 48 cut at "Loading Lab Sta", then the
# terminator `EOF`, then the tail of "export default App;", then part of a
# `<button onClick=` line. The engine's own output was CLEAN — verified, lines
# 48 and 77 intact — so this happened in the operator's terminal on the way in.
# Both pastes also open with `^C`: they had to interrupt a hung shell.
#
# The consequence is worse than a failed command, because nothing reports an
# error: `App.jsx` was written CORRUPT, the page came up blank, and the engine
# spent the next turns debugging React against a source file that was garbage.
#
# A mangled paste also means the command never ran as written — so under
# §17.916's rule ("already TRIED must mean the OPERATOR RAN IT") the attempt
# cannot count as tried, and re-issuing the same write is the correct fix
# rather than a repeat.

_TRUNCATED_PASTE_RE = re.compile(
    r"[A-Za-z0-9_)}\]'\"]\s?"
    r"(?:EOF|EOT|XEOF|PYEOF|INNER_EOF|HEREDOC|SCRIPT)"
    r"\s?[A-Za-z0-9_({\[<]"
)
_ABORTED_PASTE_RE = re.compile(r"(?:^|\n)\s*\^C")


# The operator's description of the symptom: "the one command left the user in
# what appeared to be a program requiring the ^C." That is the SECOND half of
# the same failure and worth detecting on its own. When the paste is clipped,
# the terminator never arrives on a line of its own, so bash stays inside the
# heredoc at its `>` continuation prompt — no error, no exit, just a shell that
# looks hung. Ctrl-C is the only way out, and it leaves the file half-written.
_CONTINUATION_PROMPT_RE = re.compile(r"(?:^|\n)>\s?\S", re.MULTILINE)


def detect_truncated_paste(text: str) -> dict | None:
    """Did the operator's terminal mangle the paste on the way in?

    Requires a heredoc somewhere in the text before firing — every signature
    here is only meaningful when a delimiter was in play. Any ONE of three is
    enough, because they are the same event seen from different angles:

    * the delimiter glued INTO a word (`StatuEOFort`) — the clip itself;
    * a `^C` — the operator breaking out of the shell it left hanging;
    * a run of `>` continuation prompts — the hang, still in progress.

    Returns ``{"evidence", "aborted", "hung"}`` or None.
    """
    if not (text or "").strip() or "<<" not in text:
        return None
    spliced = _TRUNCATED_PASTE_RE.search(text)
    aborted = bool(_ABORTED_PASTE_RE.search(text))
    # Two or more: one `>` line is ordinary output (`> npx`, diff context).
    hung = len(_CONTINUATION_PROMPT_RE.findall(text)) >= 2
    if not (spliced or aborted or hung):
        return None
    if spliced:
        evidence = text[max(0, spliced.start() - 40):spliced.end() + 40]
    elif aborted:
        evidence = "the paste was interrupted with Ctrl-C"
    else:
        evidence = "the shell was left at its `>` heredoc continuation prompt"
    return {
        "evidence": evidence.replace("\n", " ").strip(),
        "aborted": aborted,
        "hung": hung or aborted,
    }
