"""§17.1363 — a reading knows its own scope.

Eleven defects in one day (§17.1352–1362b) and six of them were the same bug:

    §17.1352  1 of 2 secret stores read        -> "nothing sets $MASS_PASSWORD"
    §17.1356  1 of 3 guests measured           -> "the services on these machines"
    §17.1357  1 guest's unit list              -> "`qbittorrent-nox` is not a unit
                                                   anything the engine holds names"
    §17.1359  the layer threw, read nothing    -> "nothing contradicts this block"
    §17.1360  the config was never read        -> "write the setting here"
    §17.1361  2 configs found, 1 named by `ls` -> "config = qBittorrent-data.conf"

Each was fixed by widening that one reading. The reading will be narrow again
tomorrow somewhere nobody has looked. What made all six possible is that **a
narrow reading was allowed to present itself as a complete one**:

* every measurement already records its own provenance — `ServiceTruth.reads`,
  `GuestTruth.reads`, written at ten sites — and **nothing consumed it**: a grep
  for `.reads` outside those writers finds one unrelated UI field;
* the facts block asserts *"SERVICES MEASURED ON THESE MACHINES (read just now;
  use these values, do not infer others)"* and never says which guests were
  looked at, which named service was not found, or that there were two config
  candidates and one was picked;
* the frame carried 24 fields and exactly one shaped like a gap
  (`secrets_missing`), so no consumer — drafter, gate, operator, post-run judge —
  could tell a complete reading from a partial one;
* and `service_truth._probe` returns THREE outcomes (`None` could not ask,
  `False` ran and failed, `True` answered) which every caller collapses to two
  with `if not ok: return []` — "I could not look" becoming "nothing is wrong".

So a `Reading` is threaded through the measurement layer: every probe that
answers is noted, every one that could not be asked or came back empty is a
**gap**, and the gaps travel with the facts (so the drafter is told not to invent
a value for them), into the frame (so the operator sees them beside `refused`),
and into the record (so the judge does not read silence as proof).
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: the sentence that turns a gap into behaviour for the drafter
UNREAD_RULE = ("Anything listed as NOT READ is not a fact: do not write a value for it. "
               "Read it in the block and act on what comes back, or say in one line what it needs.")


@dataclass
class Reading:
    """What the engine actually read for one step, and what it did not."""

    saw: list = field(default_factory=list)      # [(what, detail)]
    missed: list = field(default_factory=list)   # [(what, why)]

    # ------------------------------------------------------------------ writing

    def note(self, what: str, detail: str = "") -> None:
        """Record something the engine READ, with the evidence."""
        what = str(what or "").strip()
        if not what:
            return
        pair = (what, str(detail or "").strip())
        if pair not in self.saw:
            self.saw.append(pair)

    def gap(self, what: str, why: str) -> None:
        """Record something the engine did NOT read, and why it could not."""
        what, why = str(what or "").strip(), str(why or "").strip()
        if not what:
            return
        pair = (what, why)
        if pair not in self.missed:
            self.missed.append(pair)

    def absorb(self, reads: dict, prefix: str = "") -> None:
        """Take a measurement's own `reads` dict — the provenance that was being
        written and never read — into this scope."""
        for what, detail in (reads or {}).items():
            self.note(f"{prefix}{what}" if prefix else str(what), str(detail))

    # ------------------------------------------------------------------ reading

    def gaps(self) -> list:
        """``[{what, why}]`` — the frame's `not_measured`, shaped like `refused`."""
        return [{"what": w, "why": y} for w, y in self.missed]

    def says(self) -> str:
        """The scope paragraph that travels with the facts block.

        Empty when nothing was read AND nothing was missed, so a step the engine
        measured nothing for does not gain a paragraph saying so twice.
        """
        if not self.saw and not self.missed:
            return ""
        lines = ["WHAT WAS READ, AND WHAT WAS NOT (the scope of the facts above):"]
        for what, detail in self.saw:
            lines.append(f"- read: {what}" + (f" — {detail}" if detail else ""))
        for what, why in self.missed:
            lines.append(f"- NOT READ: {what}" + (f" — {why}" if why else ""))
        if self.missed:
            lines.append(UNREAD_RULE)
        return "\n".join(lines)

    def __bool__(self) -> bool:
        return bool(self.saw or self.missed)
