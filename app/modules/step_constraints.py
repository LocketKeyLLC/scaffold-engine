"""§17.1236 — a step that forbids asking must not come back with a question.

The operator's standing instruction, since the engine was built: *assume the user
knows nothing about what it's talking about.* §17.1219 put that in the prompts and
§17.1221 moved the questions to the node that needs them. What nothing enforced
is the other half: when a step's own text says **decide it and build it, do not
ask**, the generated output must not be a question.

Live, ADD100 ("Rebuild the control panel to do what was chosen in ADD99"). Its
description carried the operator's corrections verbatim and said, in as many
words, that they are not expected to know the technology, not to ask them to
choose between a reverse proxy and a VPN in those terms, and to pick the approach
and say in plain words what it means for them. What came back:

    ## What the tech choice decides — why I am asking
    I cannot build the panel until this is chosen, because the choice changes
    what the panel is. That is why I am asking now instead of guessing.
    ## The question: how should the panel be built?
    **Option A — A ready-made dashboard …**

The general verifier did fail it ("it explicitly defers the build and instead
re-asks new questions about tech stack and scope"), which is the system working —
but by judgement, on a prose rubric that passes output which "contains what the
task requested, even partially". A step told not to ask, answering with a
question, is decidable without judgement, and the reason can quote both halves so
the retry sees exactly which instruction it broke (§17.1196's lesson: a refusal
that names the rule is a compiler error, not a style guide).

Two-sided ON PURPOSE. The gate is silent unless the STEP ITSELF forbade asking,
so a `decision` node — whose whole job is to ask — can never trip it, and neither
can an ordinary step that reasonably surfaces a question.
"""
from __future__ import annotations

import re
from typing import Optional

#: The step saying: decide it, do not put it back to the operator.
#
# Every alternative is an INSTRUCTION, never a passing use of the word. The first
# draft matched bare `never ask`, which fired on "nothing it was never asked for"
# — the step's opening sentence, not an instruction at all — and quoted that back
# as the rule the output had broken. A gate that names the wrong rule is worse
# than no gate: the retry is told to fix something nobody said.
_FORBIDS_ASKING = re.compile(
    r"(?i)do\s*not\s+ask\b|don'?t\s+ask\b"
    r"|never\s+ask\s+(?:them|the\s+operator|again|for)\b"
    r"|do\s*not\s+re-?ask\b|do\s*not\s+put\s+(?:it|this|the\s+\w+)\s+back\b"
    r"|without\s+asking\s+(?:them|the\s+operator|first)\b"
    r"|do\s*not\s+defer\b|that\s+deferral\s+is\s+now\s+closed\b"
    r"|not\s+expected\s+to\s+know\s+the\s+technology\b"
    r"|pick\s+the\s+approach\b|choose\s+the\s+approach\b|decide\s+it\s+yourself\b"
    r"|is\s+IN\s+scope\s+for\s+this\s+step\b")

#: The output putting the work back to the operator instead of doing it.
#: Curated from what a model actually writes when it defers.
_ASKS = re.compile(
    r"(?i)why\s+I\s+am\s+asking|I\s+am\s+asking\s+now"
    r"|I\s+cannot\s+(?:build|do|complete|proceed)[^.]{0,60}\buntil\s+(?:this|it|that|you)\b"
    r"|##\s*The\s+question\b"
    r"|which\s+(?:would|do)\s+you\s+(?:prefer|want|choose)"
    r"|please\s+(?:choose|pick|select|decide|confirm\s+which)"
    r"|let\s+me\s+know\s+which"
    r"|(?:tell|reply\s+to)\s+me\s+which"
    r"|(?:comes|will\s+come|happens)\s+(?:immediately\s+)?after\s+your\s+(?:choice|answer|decision)"
    r"|(?:is|are|remains?)\s+out\s+of\s+scope\s+for\s+this\s+step"
    r"|will\s+be\s+added\s+only\s+(?:after|once)\s+(?:the\s+operator|you)\b"
    r"|once\s+you\s+(?:decide|choose|pick|confirm)\b")

#: How far into each side to read. A self-declaration and a question both belong
#: near the top; a passing mention deep in a long deliverable is not the step
#: refusing to do its job (the §17.1235 rule, same reason).
_PARAS = 8


def _hit(text_value: str, pattern: re.Pattern, paras: int = _PARAS) -> Optional[str]:
    """The sentence the pattern matched, within the opening `paras` paragraphs."""
    for para in re.split(r"\n\s*\n", str(text_value or ""))[:paras]:
        if not pattern.search(para):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n", para):
            if pattern.search(sentence):
                return " ".join(sentence.split())[:240]
        return " ".join(para.split())[:240]
    return None


def forbids_asking(step_text: str) -> Optional[str]:
    """The instruction in the step that says not to put this back to the operator."""
    return _hit(step_text, _FORBIDS_ASKING, paras=40)   # the whole step description


def asks_the_operator(output: str) -> Optional[str]:
    """The place the output asks instead of delivering."""
    return _hit(output, _ASKS)


def violation(step_text: str, output: str) -> Optional[str]:
    """The reason to fail this node, quoting BOTH halves, or None.

    Both must be present: the step forbade it and the output did it anyway.
    """
    told = forbids_asking(step_text)
    if not told:
        return None
    asked = asks_the_operator(output)
    if not asked:
        return None
    return (f'this step told the engine not to put the choice back to the operator — "{told}" — '
            f'and the output asks them anyway: "{asked}". They are not expected to know the '
            f'technology, so decide it, say in plain words what it means for them, and deliver '
            f'the step. If something genuinely cannot be decided without them, that belongs in a '
            f'decision node, not in place of this step\'s work.')
