#!/usr/bin/env python3
"""§17.1174 — replay the OPERATOR'S own assist turns through every routing gate.

`tests/fixtures/assist_policy_corpus.json` pins the live turns the §-entries
already quote in public. This is the other half: the gates run over the real
`assist_turns` on THIS box, which cannot be committed (the operator's words,
their hostnames, their topology) and which is the only thing that can answer
"what does this detector do on messages nobody wrote a §-entry about".

Read-only. It opens the database, selects operator messages, and calls pure
functions. Nothing is written, no model is called, no session is touched.

    python3 scripts/replay_assist_policy.py               # distribution + invariants
    python3 scripts/replay_assist_policy.py --gate pivot  # every message one gate fires on
    python3 scripts/replay_assist_policy.py --limit 2000

The invariant check is the part worth running after ANY change to
assist_policy: `claim ⊆ evidence ⊆ advance`, `claim ∩ hedged = ∅`,
`claim ∩ denial = ∅`. A violation means two gates that are meant to be nested
have drifted apart, which no example-based test would notice.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import re
import sys

GATE_NAMES = ("pivot", "howto", "help", "blocked", "claim", "hedged", "denial",
              "advance", "evidence", "whats_next", "add_step", "confirm",
              "decline", "uncertain", "recommend", "gui")


def _gates():
    from app.modules import assist_policy as P
    return {
        "pivot": P.looks_like_pivot, "howto": P.looks_like_howto_question,
        "help": P.looks_like_help_request, "blocked": P.looks_like_blocked,
        "claim": P.looks_like_completion_claim, "hedged": P.hedged_completion_report,
        "denial": P.looks_like_completion_denial, "advance": P.has_advancement_signal,
        "evidence": P.is_completion_evidence, "whats_next": P.looks_like_whats_next,
        "add_step": P.looks_like_add_step_request, "confirm": P.looks_like_confirmation,
        "decline": P.looks_like_decline, "uncertain": P.expresses_uncertainty,
        "recommend": P.wants_a_recommendation, "gui": P.looks_like_gui_question,
    }


def _redact(msg: str, width: int = 110) -> str:
    """The operator's topology never reaches stdout — this prints in a terminal
    that may be shared, pasted, or screen-shotted into an issue."""
    msg = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", msg)
    msg = re.sub(r"\b[\w.-]+@[\w.-]+\b", "<user@host>", msg)
    return " ".join(msg.split())[:width]


async def _load(limit: int) -> list[str]:
    from sqlalchemy import text
    from app.database import async_session
    async with async_session() as db:
        rows = await db.execute(text("""
            SELECT content FROM assist_turns
             WHERE role = 'operator' AND kind IN ('message', 'submit')
               AND content IS NOT NULL AND length(content) BETWEEN 3 AND 2000
             ORDER BY id DESC LIMIT :n
        """), {"n": limit})
        return [r[0] for r in rows.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--gate", choices=GATE_NAMES, help="print every message this gate fires on")
    args = ap.parse_args()

    turns = asyncio.run(_load(args.limit))
    if not turns:
        print("no operator turns on this database — nothing to replay.")
        return 0
    gates = _gates()
    fired = {t: sorted(g for g, fn in gates.items() if fn(t)) for t in turns}

    if args.gate:
        hits = [t for t, gs in fired.items() if args.gate in gs]
        print(f"{args.gate}: {len(hits)} of {len(turns)} turns\n")
        for t in hits:
            print("  •", _redact(t))
        return 0

    counts = collections.Counter(g for gs in fired.values() for g in gs)
    print(f"turns replayed: {len(turns)}\n")
    for g in GATE_NAMES:
        n = counts[g]
        print(f"  {n:5}  {n / len(turns):5.1%}  {g}")

    print("\ninvariants (a violation means two nested gates have drifted apart):")
    bad = 0
    for t, gs in fired.items():
        s = set(gs)
        for why, broken in (
            ("claim without evidence", "claim" in s and "evidence" not in s),
            ("evidence without advance", "evidence" in s and "advance" not in s),
            ("claim AND hedged", {"claim", "hedged"} <= s),
            ("claim AND denial", {"claim", "denial"} <= s),
        ):
            if broken:
                bad += 1
                print(f"  ✗ {why}: {_redact(t)}")
    print("  ✓ all hold" if not bad else f"  {bad} violation(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
