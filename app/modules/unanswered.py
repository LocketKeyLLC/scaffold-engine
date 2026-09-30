"""§17.1218 — "unsure" is not an answer, and the engine must not plan as if it were.

Refinement lists the idea's `ambiguities` and asks the operator each one. The
answers come back as a `user_feedback` blob that is handed to the planner as
context. Nothing reads it. So an ambiguity answered "unsure" is indistinguishable
from one answered with a specification, and the planner builds on a guess it
never tells anyone it made.

Live, on the operator's homelab job — three of eight ambiguities:

    Q: Preferred tech stack and hosting for the custom UI control panel
    A: unsure
    Q: Model/type of the additional graphics card already in the server
    A: I am unsure
    Q: Proxmox VE version and current storage/network configuration
    A: Unsure

The plan then emitted "Build control panel backend" and "Build control panel
frontend" — a component whose entire shape was the thing the operator had just
said they did not know — and never asked again. Their words, after living with
the result:

    "no questions or assistance in making the control panel for the whole home
     lab to work as the user wants but was never asked and never able to edit."

They were asked once, said "unsure", and the engine treated that as consent to
decide for them silently.

So: a non-answer leaves its ambiguity OPEN. It is carried into the brief as an
open question and — per §17.662/663, which already turns research options into
an explicit DAG `decision` node — it becomes a decision the operator makes from
options, instead of a guess baked into a build step.

Deterministic, and deliberately conservative: only a recognised non-answer
counts. "all", "yes we can do that", "modem to Wifi Router to server" are
answers, however short. A false positive re-asks something the operator already
settled, which is its own kind of not listening.
"""
from __future__ import annotations

import re
from typing import Optional

#: Phrases that mean "I do not know", as operators actually type them. Anchored
#: so a sentence CONTAINING the word ("I am unsure of the model, use the 1080")
#: is judged by the whole answer, not by the word alone — see `is_non_answer`.
_NON_ANSWER_RE = re.compile(
    r"^(?:"
    # "i am unsure", "i'm not sure", "im not sure" — the apostrophe form has no
    # space after the i, which the first version required and so missed.
    r"i\s*(?:'m|am|m)?\s*(?:really\s+|quite\s+|honestly\s+)?"
    r"(?:un\s?sure|not\s+sure|don'?t\s+know|do\s+not\s+know|have\s+no\s+idea|no\s+idea)"
    r"|un\s?sure|not\s+sure|no\s+idea(?:\s+yet)?|dunno|idk|don'?t\s+know|do\s+not\s+know"
    r"|n/?a|none|nothing|no\s+preference|tbd|to\s+be\s+decided"
    r"|whatever|anything|up\s+to\s+you|you\s+(?:pick|choose|decide)"
    r"|\?+"
    r")"
    r"[\s.!,]*$",
    re.I,
)


def is_non_answer(answer: str) -> bool:
    """True when the operator said they do not know.

    Judged on the WHOLE answer: "unsure" is a non-answer, "unsure of the model
    but it is a P40" is an answer that happens to start with the word.
    """
    a = (answer or "").strip()
    if not a:
        return True
    # No length heuristic. "all" is this operator's real answer to "which AI
    # workloads", and "P40" answers "which graphics card" — a short answer is
    # still an answer, and re-asking it is its own kind of not listening.
    return bool(_NON_ANSWER_RE.match(a))


def parse_feedback(blob: str) -> list[dict]:
    """The ``Q: …\\nA: …`` pairs refinement writes into ``user_feedback``.

    Returns ``[{question, answer, answered}]`` in order. A blob in any other
    shape yields nothing rather than a guess.
    """
    out: list[dict] = []
    q: Optional[str] = None
    buf: list[str] = []

    def flush() -> None:
        if q is None:
            return
        ans = " ".join(buf).strip()
        out.append({"question": q, "answer": ans, "answered": not is_non_answer(ans)})

    for raw in (blob or "").splitlines():
        ln = raw.strip()
        if ln.lower().startswith("q:"):
            flush()
            q, buf = ln[2:].strip(), []
        elif ln.lower().startswith("a:") and q is not None:
            buf = [ln[2:].strip()]
        elif q is not None and buf:
            buf.append(ln)
    flush()
    return out


def unresolved(brief: dict) -> list[str]:
    """The ambiguities still open after the operator answered.

    An ambiguity with no Q/A pair at all is open too — it was never put to them.
    """
    pairs = parse_feedback(str((brief or {}).get("user_feedback") or ""))
    by_q = {p["question"].strip().lower(): p for p in pairs}
    out: list[str] = []
    for amb in (brief or {}).get("ambiguities") or []:
        hit = by_q.get(str(amb).strip().lower())
        if hit is None or not hit["answered"]:
            out.append(str(amb))
    return out


def decision_brief(open_items: list[str]) -> str:
    """What the planner is told about the things nobody has decided.

    Named as decisions to PUT TO the operator, not as freedom to choose: the
    failure this fixes is a planner reading silence as consent.
    """
    if not open_items:
        return ""
    lines = "\n".join(f"  - {q}" for q in open_items)
    return (
        "\n\nSTILL UNDECIDED — the operator was asked these and could not answer "
        "yet:\n" + lines + "\n\nDo NOT choose for them and do NOT build as though "
        "a choice had been made. For each one emit a `decision` node that puts "
        "the real options to the operator, with the trade-off of each, BEFORE any "
        "step that depends on the answer. A step that builds on an undecided "
        "question produces something they did not ask for and cannot correct."
    )


# ---------------------------------------------------------------------------
# §17.1220 — enforcement, not advice.
#
# §17.1218 told the planner to emit a decision node for anything still
# undecided. That is a prompt instruction, and prompt instructions get ignored:
# §17.686 has told planners to emit a configure step for every install since
# long before this operator's media stack was installed and never configured.
#
# So the engine does it itself. For every ambiguity the operator could not
# answer, a `decision` node is INSERTED if the planner did not write one, and
# every step whose subject is that same undecided thing is made to DEPEND on it.
# A build step for the control panel then cannot run until the control panel has
# actually been decided — which is the whole of "unsure must not become a build".
# ---------------------------------------------------------------------------

_KEY_STOP = frozenset({
    "the", "and", "for", "with", "what", "which", "whether", "should", "would",
    "look", "like", "type", "model", "preferred", "existing", "current", "your",
    "its", "how", "much", "many", "specific", "already", "additional", "custom",
    "etc", "and/or", "from", "that", "this", "any", "all", "are", "was", "has",
    "run", "runs", "where", "when", "who", "why", "into", "onto", "per", "via",
})


def _keywords(text: str) -> set[str]:
    """The words that make an ambiguity about a THING, so the steps about that
    same thing can be found."""
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text or "")
            if w.lower() not in _KEY_STOP}


def _covers(node: dict, keys: set[str]) -> bool:
    """Does this node already put that question to the operator?"""
    if (node.get("node_type") or "") != "decision":
        return False
    return len(_keywords(node.get("title", "")) & keys) >= 2


def dependent_steps(nodes: list[dict], keys: set[str]) -> list[str]:
    """Steps whose subject IS the undecided thing — the ones that must wait.

    Two shared keywords, so "Build control panel backend" waits on the control
    panel decision while "Configure Proxmox firewall rules" does not.
    """
    out = []
    for n in nodes:
        if (n.get("node_type") or "") == "decision":
            continue
        if len(_keywords(n.get("title", "")) & keys) >= 2:
            out.append(n.get("node_key"))
    return out


def ensure_decisions(nodes: list[dict], brief: dict, *, key_prefix: str = "ASK") -> list[dict]:
    """A decision node for each unresolved ambiguity NO step covers.

    §17.1221 — when a step does touch the question, that step asks it on arrival
    and nothing is inserted; this is only the backstop for something the
    operator could not answer that no step would ever raise, which would
    otherwise be decided by silence.

    Never raises and never reorders: it only adds nodes and widens `depends_on`.
    """
    open_items = unresolved(brief)
    if not open_items:
        return []
    added: list[dict] = []
    used = {str(n.get("node_key")) for n in nodes}
    for i, q in enumerate(open_items, 1):
        keys = _keywords(q)
        if not keys or any(_covers(n, keys) for n in nodes):
            continue                                  # the planner did write one
        waiters = dependent_steps(nodes, keys)
        if waiters:
            # §17.1221 — a step already touches this, so the step asks it when
            # the operator arrives there. Adding a node as well would put the
            # question in the plan twice and make the plan about interrogation
            # rather than work, which is exactly what the operator objected to.
            continue
        key = f"{key_prefix}{i}"
        while key in used:
            i += 1
            key = f"{key_prefix}{i}"
        used.add(key)
        added.append({
            "node_key": key,
            "node_type": "decision",
            "title": f"Decide with the operator: {q[:90]}",
            "description": (
                f"DECISION — the operator was asked this and could not answer:\n\n  {q}\n\n"
                "They are not expected to know the technology (§17.1219). Do NOT ask them to "
                "supply a specification and do NOT pick for them.\n\n"
                "Explain in plain words what is actually being chosen and why it matters to "
                "them, offer 2-4 concrete options with the trade-off of each in everyday "
                "terms, and record what they pick. If they still cannot choose, narrow it: "
                "ask what they want to be able to DO, and turn that into the choice.\n\n"
                + (f"These steps are waiting on this answer: {', '.join(waiters[:12])}.\n"
                   if waiters else "")
                + "Done when the operator has made a choice in their own words."),
            "tool": "LLM",
            "depends_on": [],
            "_waiters": waiters,
        })
    return added


# ---------------------------------------------------------------------------
# §17.1221 — ask at the NODE, not in the plan.
#
# The operator's design, and it is better than §17.1220's:
#
#     "Can't the engine present the questions when the user arrives to the node.
#      This way the large plan itself does not need to focus on it but the
#      smaller nodes will."
#
# It is the pattern the engine already uses for VALUES: a run pause asks for the
# `<PLACEHOLDER>`s it needs at the moment it needs them (§17.1187), with the
# commands filling in as they are typed. Nobody is asked during planning what
# `<DISK_NAME>` will be.
#
# Design decisions should work the same way. "What should the control panel do?"
# is nearly unanswerable in the abstract during refinement and obvious when you
# are standing in front of the step that builds it. So the plan stays about the
# work, and the step carries its own unanswered question — asked, in plain
# words, with options, at the moment it actually blocks something.
# ---------------------------------------------------------------------------


def questions_for_step(title: str, brief: dict, *, description: str = "") -> list[str]:
    """The unresolved ambiguities this particular step cannot be done without.

    Matched on shared subject words, the same rule `dependent_steps` uses — so
    the control-panel question reaches the control-panel step and nothing else.
    """
    open_items = unresolved(brief or {})
    if not open_items:
        return []
    mine = _keywords(f"{title} {description}")
    return [q for q in open_items if len(_keywords(q) & mine) >= 2]


def ask_first_block(questions: list[str]) -> str:
    """What the step's own prompt carries so the walkthrough opens by asking.

    Phrased for a guide that is about to talk to someone who does not know the
    technology (§17.1219) — the engine's standing instruction.
    """
    if not questions:
        return ""
    lines = "\n".join(f"  - {q}" for q in questions)
    return (
        "\n\nASK BEFORE YOU BUILD — this step depends on something the operator has "
        "not decided:\n" + lines + "\n\nOpen with the question, not the work. They are "
        "NOT expected to know the technology: do not ask for a specification, do not "
        "use a term of art without explaining it, and do not pick for them. Say in "
        "plain words what is being chosen and why it matters to them, offer 2-4 "
        "concrete options with the everyday trade-off of each, and wait for their "
        "answer. If they cannot choose, narrow it — ask what they want to be able to "
        "DO, and turn that into the options. Build only what they then chose; a step "
        "that guesses produces something they did not ask for and cannot correct."
    )
