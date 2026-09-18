"""§17.1098 — deterministic single-action + coherence enforcement for a
walkthrough.

Two operator-reported defects, one root cause: a single step's walkthrough is
allowed to hold more than one action, and nothing checks that the actions make
sense together.

1. TWO STEPS AT ONCE. `apply_single_action` (§17.1011) asks the model, in the
   prompt, to hold a step to one action. A prompt rule is a request, and this
   codebase's long record (§17.882) is that they get ignored — so walkthroughs
   still arrive as "Phase 1 / Phase 2 / Phase 3", each a separate place to act.
   The real T35 walkthrough had three phases in one step.

2. SELF-CONTRADICTING INSTRUCTIONS. A walkthrough tells the operator to STOP a
   container and then, further down, to USE that same container's console/exec
   — which cannot work, the container is off. (Reported live; the coherent
   ordering — use, THEN stop — is the common, correct case and must NOT flag.)

This module is the ENFORCEMENT half the prompt directive never had:
- pure detectors (`multi_action_issue`, `self_contradictions`, `coherence_issues`)
  find the problem deterministically from the text;
- `enforce_coherence` regenerates ONCE with the problem named (the §17.893
  redraw pattern) and, if the redraw still violates, CLIPS to the first action
  and holds the rest for the next step — the operator's chosen policy.

Everything here is pure except `enforce_coherence`, which calls the model.
"""
from __future__ import annotations

import re
from typing import Optional

# ── execution contexts: "📍 On: the Proxmox host shell (root@pve)" ──────────
_CONTEXT_RE = re.compile(r"(?:^|\n)\s*(?:📍\s*)?On:\s*(.+)", re.IGNORECASE)
# ── phase/part/stage headers: "### Phase 2: Install Caddy" ──────────────────
_PHASE_RE = re.compile(r"(?im)^\s{0,3}#{0,4}\s*(?:phase|part|stage)\s+[0-9a-z]\b")

# ── resource lifecycle, as COMMANDS only ────────────────────────────────────
# Measuring on 596 real turns showed prose ("stop the service inside container
# 111", "the container stopped") and restart idioms (`qm stop N && qm start N`)
# produce false contradictions. So the detector keys off the actual CLI verbs,
# cancels on recovery, and ignores trailing/troubleshooting sections.
# (No re.VERBOSE: patterns contain literal spaces.)
_STOP_CMD_RE = re.compile(r"\b(?:pct|qm)\s+(?:stop|shutdown)\s+(\d+)", re.IGNORECASE)   # pct stop 120
_RECOVER_CMD_RE = re.compile(r"\b(?:pct|qm)\s+(?:start|reboot|restart)\s+(\d+)", re.IGNORECASE)  # qm start 120
_USE_CMD_RE = re.compile(                                              # needs it RUNNING
    r"\bpct\s+(?:exec|enter)\s+(\d+)"                                  # pct exec|enter 120
    r"|\bqm\s+(?:terminal|guest\s+exec)\s+(\d+)",                      # qm terminal 106
    re.IGNORECASE,
)
# Uses AFTER one of these headers are verifications / back-references, not new
# instructions — a `pct exec` in "## Done when" or "## If that fails" is fine.
_TRAILING_SECTION_RE = re.compile(
    r"(?im)^\s{0,3}#{1,4}\s*(?:✅\s*)?(?:done when|if that fails|if it|if `|expected|troubleshoot|verify\b)",
)


def _ids(rx: re.Pattern, text: str) -> list[tuple[int, str]]:
    """(position, resource_id) for every match — the id from whichever group hit."""
    out = []
    for m in rx.finditer(text or ""):
        rid = next((g for g in m.groups() if g), None)
        if rid:
            out.append((m.start(), rid))
    return out


def _trailing_section_at(text: str) -> int:
    """Position of the first 'Done when / If that fails / …' header (len when
    none) — uses after it are verifications/back-references, not new steps."""
    m = _TRAILING_SECTION_RE.search(text or "")
    return m.start() if m else len(text or "")


def execution_contexts(text: str) -> list[str]:
    """Distinct normalised 'On:' targets, in order of first appearance."""
    seen, out = set(), []
    for m in _CONTEXT_RE.finditer(text or ""):
        norm = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".").lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def count_phases(text: str) -> int:
    return len(_PHASE_RE.findall(text or ""))


def multi_action_issue(text: str) -> Optional[dict]:
    """A step is multi-action when it sends the operator to two+ execution
    contexts OR is split into two+ explicit phases. A single context with
    several commands (empty a file, verify, stop) is ONE action and does not
    flag — the §17.1011 max_steps prompt bound covers command count."""
    ctxs = execution_contexts(text)
    phases = count_phases(text)
    if len(ctxs) >= 2 or phases >= 2:
        return {"contexts": ctxs, "phases": phases}
    return None


def self_contradictions(text: str) -> list[dict]:
    """A resource is STOPPED by command and then USED (pct exec/enter, qm
    terminal) while still down — you cannot exec into a stopped container.

    Precise by construction (measured against 596 real turns):
    - keys off the actual stop/use COMMANDS, not prose ("stop the service in
      container N" is not a container stop);
    - CANCELS on recovery: a `start`/`reboot` of the same id between the stop
      and the use means it is back up (`qm stop N && qm start N` is a restart);
    - IGNORES uses inside a trailing 'Done when / If that fails' section — those
      are verifications and back-references, not new instructions;
    - down→use ORDER only (use→down, the normal edit-then-stop order, is fine).
    """
    t = text or ""
    cut = _trailing_section_at(t)
    stops = _ids(_STOP_CMD_RE, t)
    recovers = _ids(_RECOVER_CMD_RE, t)
    uses = [(p, r) for p, r in _ids(_USE_CMD_RE, t) if p < cut]
    out = []
    for use_pos, rid in uses:
        # the latest stop of this resource before the use
        prior_stops = [p for p, r in stops if r == rid and p < use_pos]
        if not prior_stops:
            continue
        d = max(prior_stops)
        # recovered if a start/reboot of the same id sits between the stop and use
        if any(d < p < use_pos for p, r in recovers if r == rid):
            continue
        out.append({"resource": rid, "down_at": d, "use_at": use_pos})
    # one per resource, earliest offending use
    best: dict[str, dict] = {}
    for c in out:
        if c["resource"] not in best or c["use_at"] < best[c["resource"]]["use_at"]:
            best[c["resource"]] = c
    return sorted(best.values(), key=lambda d: d["use_at"])


def coherence_issues(text: str) -> dict:
    """Everything wrong with this walkthrough's shape, in one call. Empty dict
    (falsy) when the walkthrough is a single coherent action."""
    out: dict = {}
    ma = multi_action_issue(text)
    if ma:
        out["multi_action"] = ma
    sc = self_contradictions(text)
    if sc:
        out["contradictions"] = sc
    return out


# ── clip: keep the first action, hold the rest for the next step ────────────

def _first_split_point(text: str) -> Optional[int]:
    """Index where the SECOND action begins — the earlier of the 2nd phase
    header and the 2nd distinct execution-context marker."""
    points = []
    phases = list(_PHASE_RE.finditer(text or ""))
    if len(phases) >= 2:
        points.append(phases[1].start())
    ctx_ms, seen = [], set()
    for m in _CONTEXT_RE.finditer(text or ""):
        norm = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".").lower()
        if norm and norm not in seen:
            seen.add(norm)
            ctx_ms.append(m.start())
    if len(ctx_ms) >= 2:
        points.append(ctx_ms[1])
    return min(points) if points else None


_CLIP_NOTE = (
    "\n\n---\n_✳️ This step had more than one action, so only the first is shown "
    "above. Once it is done, press **✓ Done → next step** and I'll walk you "
    "through the rest as the next step._"
)


def first_action_only(text: str) -> tuple[str, bool]:
    """Clip to the first action. Returns (clipped_text, did_clip)."""
    i = _first_split_point(text)
    if i is None:
        return text, False
    clipped = (text[:i]).rstrip()
    return clipped + _CLIP_NOTE, True


def coherence_directive(issues: dict) -> str:
    """The regeneration notice — names exactly what is wrong (§17.893 pattern)."""
    bits = []
    ma = issues.get("multi_action")
    if ma:
        if ma["phases"] >= 2:
            bits.append(f"it is split into {ma['phases']} phases")
        if len(ma["contexts"]) >= 2:
            bits.append(f"it sends the operator to {len(ma['contexts'])} different "
                        f"places to act ({'; '.join(ma['contexts'][:3])})")
    for c in issues.get("contradictions", []):
        bits.append(f"it stops resource {c['resource']} and then tries to use that "
                    f"same resource's console/exec afterwards, which cannot work")
    return (
        "REGENERATION NOTICE: this walkthrough is not a single coherent action — "
        + "; ".join(bits) + ".\n"
        "Rewrite it as ONE action the operator does in ONE place. Keep only the "
        "FIRST thing they must do; do not include later phases, a second "
        "execution context, or any step that uses a resource after stopping it. "
        "If more work remains, it becomes the NEXT step — end after the first "
        "action's verification. Output the corrected walkthrough in full."
    )


def contradiction_warning(issues: dict) -> str:
    """Visible flag when a stubborn contradiction survives the redraw."""
    cs = issues.get("contradictions") or []
    if not cs:
        return ""
    rs = ", ".join(sorted({c["resource"] for c in cs}))
    return (
        "\n\n---\n⚠️ **These instructions conflict:** they stop " + rs +
        " and then use that same resource's console/exec afterwards — which "
        "cannot work while it is stopped. Do the part BEFORE the stop first, "
        "then reply and I'll re-plan the rest."
    )
