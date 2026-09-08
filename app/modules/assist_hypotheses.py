"""§17.973 — what the fix path has already tried, and what is left.

Operator: *"This though appears to be a larger issue with the fixing component
of the engine. Which should research troubleshooting possibilities and have its
own record knowing what was tried or not as well as what is left it could
mean."*

Measured on the live session before writing any of this:

* T31–T35 produced **54 fix turns**, and **100% of them carry a `## Diagnosis`
  section** — every hypothesis the engine tested is already written down, in a
  parseable place, in its own transcript.
* Every committed step recorded **`ruled_out=0`**. T32 took 17 fix turns and
  ruled out nothing. T34 took 11 and ruled out nothing. The `ruled_out` list in
  the playbook — the field designed for exactly this — has three entries in the
  whole session, all from a different step days earlier.

So the engine states a cause, tests it, watches it fail, and then forgets it.
Nothing carries "already eliminated" into the next fix, and nothing anywhere
represents the REMAINING space at all. Each fix is drawn as though it were the
first, which is why the same ground gets covered repeatedly and why a step can
absorb seventeen attempts without ever narrowing.

The correction is cheap because the data already exists. No new capture, no
model call: the ledger is DERIVED from the turns on read, so it cannot drift
from what actually happened.

**What makes a hypothesis eliminated.** If the engine diagnosed a cause and then
issued ANOTHER fix for the same step, the first diagnosis did not resolve it.
That is the same reasoning §17.881 uses for its failure streak, applied to the
stated cause rather than the command. The newest diagnosis is left OPEN — it is
the one currently under test.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("scaffold")

# The §17.741/852 reply contract puts the cause under `## Diagnosis`; the
# section ends at the next heading. Measured at 100% coverage over 54 live
# fix turns, so this is the engine's own format, not a hopeful guess.
_DIAGNOSIS_RE = re.compile(
    r"^#{1,4}\s*Diagnosis\s*\n(.*?)(?=\n#{1,4}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_MAX_EACH = 240
_MAX_KEPT = 12


def extract_diagnosis(reply: str) -> str:
    """The cause a fix reply says it is addressing, trimmed to one claim."""
    m = _DIAGNOSIS_RE.search(reply or "")
    if not m:
        return ""
    body = re.sub(r"\s+", " ", m.group(1)).strip()
    if not body:
        return ""
    # The first sentence carries the claim; the rest is elaboration.
    first = re.split(r"(?<=[.!?])\s+", body)[0].strip()
    return (first or body)[:_MAX_EACH]


def _norm(text: str) -> str:
    """Compare on words, so re-phrasing the same cause still matches."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


# Function words carry no diagnostic content and would drown the signal.
_STOP = frozenset(
    "the a an is are was were be been being it its this that these those and or "
    "of to in on for with by from as at not but you your i we they there here "
    "so if then than when which what who how why now still yet also very just "
    "has have had do does did can could will would should may might must".split()
)


def _keywords(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in _STOP and len(w) > 2}


# Threshold picked from the live T34 sequence rather than intuition. Measured
# keyword-Jaccard between its ten eliminated causes:
#
#   "App.jsx is corrupted" vs "App.jsx is corrupted or incomplete"      0.50
#   "append commands were truncated" vs the same, re-worded             0.38
#   "App.jsx is corrupted" vs "corrupted because pct exec truncated"    0.33
#   "corrupted or incomplete" vs "corrupted because pct exec truncated" 0.29
#   ── 0.28 ──────────────────────────────────────────────────────────────
#   "App.jsx is CORRUPTED" vs "App.jsx is actually COMPLETE and correct" 0.20
#
# That last pair is the one that matters: opposite claims about the same file,
# sharing only the identifiers `app`, `file`, `jsx`. A threshold low enough to
# catch every repeat would flag it and stop the engine concluding the file was
# fine — so the line sits above it. Conservative in the right direction: a
# missed repeat costs one turn, a false one blocks a correct new diagnosis.
_RETEST_JACCARD = 0.28


def harvest(fix_replies: list[str]) -> dict:
    """Split this step's stated causes into eliminated vs still under test.

    ``fix_replies`` is oldest-first. Everything but the newest has been tried
    and did not resolve the step, or there would not have been a later fix.
    """
    seen: list[tuple[str, str]] = []          # (normalised, display)
    for reply in fix_replies or []:
        d = extract_diagnosis(reply)
        if not d:
            continue
        n = _norm(d)
        if not n or any(n == prev for prev, _ in seen):
            continue
        seen.append((n, d))
    if not seen:
        return {"eliminated": [], "current": ""}
    return {
        "eliminated": [d for _, d in seen[:-1]][-_MAX_KEPT:],
        "current": seen[-1][1],
    }


def render_tested_hypotheses(ledger: dict | None) -> str:
    """The prompt block: what is closed, and the demand to name what is not."""
    elim = list((ledger or {}).get("eliminated") or [])
    if not elim:
        return ""
    lines = [
        "### CAUSES YOU HAVE ALREADY TESTED ON THIS STEP AND ELIMINATED "
        f"({len(elim)}). Each of these was your own stated diagnosis, and the "
        "step did not resolve after it:",
    ]
    lines += [f"- {d}" for d in elim]
    lines.append(
        "Do NOT propose any of these again, in different words or with a "
        "different command. They are closed. Before writing your next "
        "Diagnosis, say in one line what is LEFT that could explain this — "
        "including causes UPSTREAM of this step, and anything in the files or "
        "services this session itself created — and pick from that."
    )
    return "\n".join(lines)


def find_retested_hypothesis(draft: str, ledger: dict | None) -> list[dict]:
    """A draft re-diagnosing a cause already eliminated on this step.

    Deterministic backstop in the §17.882 tradition: the prompt block above is
    guidance, and guidance gets ignored precisely when a step has gone long
    enough for the list to matter. Matched on a strong word overlap so a
    re-phrasing is still caught, and it stays quiet on short diagnoses where
    overlap means little.
    """
    elim = list((ledger or {}).get("eliminated") or [])
    d = extract_diagnosis(draft)
    if not d or not elim:
        return []
    new = _keywords(d)
    if len(new) < 4:
        return []
    hits: list[dict] = []
    for prev in elim:
        old = _keywords(prev)
        if len(old) < 4:
            continue
        overlap = len(new & old) / max(1, len(new | old))
        if overlap >= _RETEST_JACCARD:
            hits.append({"previous": prev, "overlap": round(overlap, 2),
                         "shared": sorted(new & old)[:6]})
    return sorted(hits, key=lambda h: -h["overlap"])[:2]


# ── §17.977 — the project, not just the node ─────────────────────────────
#
# Operator: *"is there a way to fix the older logs and implement research
# information into it? Perhaps creating an update path within the system
# correcting the OVERALL project instead of just the singular node."*
#
# Measured: harvesting every node's fix turns in the live session recovers
# **124 eliminated causes across 19 nodes**. The project-level playbook holds
# **3**. Everything the engine has disproved in three days of work exists in its
# own transcript and is visible to nothing beyond the step that produced it.
#
# The update path is that there is no path: these are DERIVED on read, exactly
# like the per-step ledger, so every existing session recovers its full history
# the moment the code ships. Nothing to migrate, nothing to backfill, and no
# stored copy that can disagree with what actually happened.
#
# **What is deliberately NOT done.** These are not merged into the playbook's
# `ruled_out`. That list is a BINDING prohibition on METHODS that failed on this
# system ("apt.servarr.com fails DNS inside container 102") and it is enforced;
# an eliminated hypothesis is a CAUSE that was considered and disproved, which
# is a different claim and often a narrower one. "App.jsx is corrupted" was
# disproved on T34 and would be actively wrong as a standing prohibition — the
# file WAS corrupted twice before it wasn't. Promoting step-scoped reasoning
# into a project-wide ban is how a knowledge store starts lying.
#
# So the cross-step view is INFORMATIONAL and says so: what was disproved
# elsewhere, on which step, offered as orientation. Only the same-step ledger
# (§17.973) gates anything.

_MAX_CROSS_STEP = 12


def harvest_session(rows: list[tuple[str, str]]) -> dict[str, dict]:
    """Per-node ledgers from every fix turn in a session.

    ``rows`` is ``[(node_key, reply)]`` oldest-first.
    """
    by_node: dict[str, list[str]] = {}
    for nk, reply in rows or []:
        if nk:
            by_node.setdefault(nk, []).append(reply)
    return {nk: harvest(replies) for nk, replies in by_node.items()}


def cross_step_eliminated(session_ledgers: dict[str, dict],
                          current_node: str | None) -> list[tuple[str, str]]:
    """(node, cause) pairs disproved on OTHER steps, newest nodes last."""
    out: list[tuple[str, str]] = []
    for nk, led in (session_ledgers or {}).items():
        if nk == current_node:
            continue
        for cause in (led or {}).get("eliminated") or []:
            out.append((nk, cause))
    return out[-_MAX_CROSS_STEP:]


def render_cross_step_eliminated(pairs: list[tuple[str, str]] | None) -> str:
    """Orientation, not prohibition — the distinction is the whole design."""
    if not pairs:
        return ""
    lines = [
        "### DISPROVED ELSEWHERE IN THIS PROJECT — context, not a rule. Each of "
        "these was tested on the step named and did not explain the problem "
        "there. That does NOT mean it cannot be the cause here; it means the "
        "engine has already paid to learn about it, so do not re-derive it from "
        "scratch, and say so explicitly if you are proposing one again:",
    ]
    lines += [f"- [{nk}] {cause}" for nk, cause in pairs]
    return "\n".join(lines)
