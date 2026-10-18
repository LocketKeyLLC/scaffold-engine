"""§17.1644 — a fix step that runs "only if" named checks found failures is skipped when every one of them passed.

Live (job 5db1b088, 2026-10-18): the planner wrote nine fix steps of one shape -- "Only if T1/T2/T3 found failures:
fix library types, paths, or series resolution via Jellyfin API" -- each depending on the checks it names. Nothing in
the engine read the condition. T1–T3 had all passed (Jellyfin typed correctly, at the folders Radarr and Sonarr import
into), and T4 was drafted anyway: a `POST /Library/VirtualFolders` re-adding the Movies library that was already there.
The gates refused it for other reasons; had they not, the step would have changed a working library to "fix" nothing.

The engine settles the condition from the plan's own record: when every named check -- and every split child of one
(`T2` → `T2v2`) -- is settled and none FAILED, the fix has nothing to fix and is recorded skipped with that reason, before
anything is drafted. A failed check, a check not yet settled, or a key the plan does not hold leaves the step to its draft.
"""
from __future__ import annotations

import re
from typing import Optional

_KEY = r"[A-Z]{1,4}\d+(?:v\d+)?"
#: "Only if T1/T2/T3 found failures:", "Only if T6 found failures", "only if T8, T10 or T12 report problems"
_ONLY_IF_RE = re.compile(
    rf"^\s*only\s+if\s+(?P<keys>{_KEY}(?:\s*(?:/|,|&|\bor\b|\band\b)\s*{_KEY})*)\s+"
    r"(?:found|finds|find|reported|reports|report|showed|shows|show|detected|detects)\s+"
    r"(?:any\s+|a\s+|an\s+)?(?:failures?|failed|problems?|issues?|errors?|faults?|broken)\b", re.I)


def _step_text(node: Optional[dict]) -> str:
    return str((node or {}).get("prompt_template") or (node or {}).get("description") or "")


def named_checks(node: Optional[dict]) -> list[str]:
    """The step keys the step's "Only if …" condition names, or [] when it states none."""
    m = _ONLY_IF_RE.match(_step_text(node))
    return list(dict.fromkeys(re.findall(_KEY, m.group("keys")))) if m else []


def unmet_condition(node: Optional[dict], plan_rows: Optional[list[dict]]) -> Optional[str]:
    """Why the step has nothing to do -- every check it is conditional on passed -- or None to draft it as usual."""
    keys = named_checks(node)
    if not keys:
        return None
    by = {str(r.get("node_key") or ""): r for r in plan_rows or []}
    if any(k not in by for k in keys):
        return None                          # a key the plan does not hold: not the engine's to settle
    # a named check that was split carries its children: "T2" is also T2v2
    family = [k2 for k in keys for k2 in by if k2 == k or re.fullmatch(rf"{re.escape(k)}v\d+", k2)]
    status = {k: str(by[k].get("status") or "") for k in dict.fromkeys(family)}
    if any(s not in ("done", "skipped") for s in status.values()):
        return None                          # one failed, or one is not settled yet
    if not any(s == "done" for s in status.values()):
        return None                          # nothing actually checked: every one was skipped
    listed = ", ".join(f"{k} {s}" for k, s in status.items())
    return (f"nothing to fix: this step runs only if {'/'.join(keys)} found failures, and none did ({listed}). "
            f"Recorded skipped by the engine before anything was drafted (§17.1644).")
