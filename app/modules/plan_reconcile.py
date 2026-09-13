"""§17.1043 — plan reconciliation: a confirmed fix is applied to the whole plan.

The operator's proposal, verbatim: *"a system alongside assist that fixes the
broken workflow as a whole. Its sole job being to take addressed errors and
apply them or updated information or changes to the entire plan."*

Why it is needed. When the fix path corrects a wrong path, port or address on
step N and the operator's next paste shows it worked, the correction reaches
the session ledger (facts, sourced values) — and nothing else. The remaining
steps' task text still carries the old value, a cached walkthrough of a later
step may already have been drawn from it, and the digest keeps quoting the
step that carried it. The model is then told the truth in one prompt block
and shown the stale plan in another; the §17.1034 plan-only tier is how that
contradiction has been papered over.

Triggers so far — *fix confirmed* (§17.1043): a step is committed after one
or more fix replies on it; *decision committed* (§17.1044): a ``decision``
node is committed, and the chosen option is applied to every pending step
that depends on it or was written for a rejected option; *note recorded*
(§17.1045): an operator note that states a correction in its own words
("3002 instead of 3001", "10.20.0.4 → 10.20.0.40", "changed from X to Y")
is applied to every pending step that still carries the old value;
*substitution changed* (§17.1046): the operator re-pins an environment
value (``/assist env KEY=value``, the SPA editor) — the previous pin is the
old value, the new pin the new one, both the operator's own words. It derives correction records
deterministically (old value → new value, by kind), applies them to every
PENDING step's task text, invalidates any cached walkthrough that mentions the
old value so it regenerates through the verified path, records the correction
as a fact in the ledger, and keeps a durable change ledger on the job. Done
steps are never touched — they are the operator's own executed work.

Grounding rules (the same discipline as the evidence layer):
* an OLD value must appear in what the operator pasted while the step was
  failing (or in the plan text) and must NOT appear in the paste that closed
  the step;
* a NEW value must appear in a fix reply AND in the operator's closing paste
  or the session's confirmed facts — the operator's system said it;
* one old and one new per kind, or nothing: an ambiguous pair is logged and
  left alone. A model may later propose corrections; the application stays
  deterministic and the gates stay.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.modules.assist_evidence import extract_specifics
from app.providers.base import Tool

logger = logging.getLogger("scaffold")

# Absolute POSIX paths with at least two segments and no shell glob; a doubled
# separator (``/opt//opt/x``) is exactly the live class and is kept verbatim.
_PATH_RE = re.compile(r"(?<![\w.:/])(/[A-Za-z0-9_.+-]+(?:/+[A-Za-z0-9_.+-]+)+/?)")
_PATH_SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/")
KINDS = ("ip", "port", "version", "url", "path")
PROVENANCE_PREFIX = "🔁 Updated after"


def values_in(text_value: str) -> list[dict]:
    """``{kind, value}`` for every concrete value in the text: the evidence
    layer's specifics (ip / port / version / url) plus absolute paths."""
    found = list(extract_specifics(text_value or ""))
    seen = {(f["kind"], f["value"]) for f in found}
    for m in _PATH_RE.finditer(text_value or ""):
        p = m.group(1).rstrip("/")
        if len(p) < 4 or p.startswith(_PATH_SKIP_PREFIXES):
            continue
        if ("path", p) not in seen:
            seen.add(("path", p))
            found.append({"kind": "path", "value": p})
    return found


def _prune_prefix_paths(values: list[str]) -> list[str]:
    """A directory that only appears as the parent of a more specific path in
    the same set is not a separate candidate (``/etc/nginx/conf.d`` next to
    ``/etc/nginx/conf.d/site.conf``)."""
    out = []
    for v in values:
        if any(o != v and o.startswith(v.rstrip("/") + "/") for o in values):
            continue
        out.append(v)
    return out


def _mentions(value: str, hay: str) -> bool:
    return re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w])", hay or "") is not None


def derive_corrections(*, failing_pastes: list[str], fix_replies: list[str],
                       closing_paste: str, plan_text: str,
                       confirmed_facts: str = "") -> tuple[list[dict], list[dict]]:
    """Returns ``(corrections, declined)``. A correction is
    ``{kind, old, new}``; ``declined`` records candidate sets the rules could
    not resolve to exactly one pair, for the log."""
    failing = "\n".join(failing_pastes or [])
    fixes = "\n".join(fix_replies or [])
    closing = closing_paste or ""
    confirmed = (closing + "\n" + (confirmed_facts or "")).strip()
    if not fixes.strip() or not confirmed.strip():
        return [], []
    corrections: list[dict] = []
    declined: list[dict] = []
    by_kind_fix: dict[str, list[str]] = {k: [] for k in KINDS}
    for it in values_in(fixes):
        if it["value"] not in by_kind_fix[it["kind"]]:
            by_kind_fix[it["kind"]].append(it["value"])
    # OLD candidates come from what the operator pasted while failing; the
    # plan text is what they get applied TO, never a source of candidates.
    by_kind_old: dict[str, list[str]] = {k: [] for k in KINDS}
    for it in values_in(failing):
        if it["value"] not in by_kind_old[it["kind"]]:
            by_kind_old[it["kind"]].append(it["value"])
    for kind in KINDS:
        if kind == "path":
            by_kind_fix[kind] = _prune_prefix_paths(by_kind_fix[kind])
            by_kind_old[kind] = _prune_prefix_paths(by_kind_old[kind])
        # NEW: proposed by a fix AND stated by the operator's system afterwards —
        # and not part of the FAILURE (live: the missing directory itself sat in
        # the fix reply and in a ledger fact, and counted as a second "new" path).
        new = [v for v in by_kind_fix[kind] if _mentions(v, confirmed) and not _mentions(v, failing)]
        # OLD: seen while failing, absent from the CLOSING paste (the facts
        # ledger may name it — "no such directory", "Corrected at Tn: old → …"),
        # and not itself a proposed value.
        old = [v for v in by_kind_old[kind] if v not in new and not _mentions(v, closing)]
        if not new or not old:
            continue
        if len(new) == 1 and len(old) == 1 and new[0] != old[0]:
            corrections.append({"kind": kind, "old": old[0], "new": new[0]})
        else:
            declined.append({"kind": kind, "old": old[:4], "new": new[:4]})
    return corrections, declined


def plan_changes(nodes: list[dict], steps: list[dict], corrections: list[dict],
                 *, source_node_key: str, source_label: Optional[str] = None) -> dict:
    """Pure: which pending nodes' task text change and how, and which cached
    walkthroughs must be regenerated. ``nodes``: ``{node_key, status,
    prompt_template}``; ``steps``: ``{node_key, status, guidance}``.
    ``source_label`` names the origin in the provenance line (default
    ``step <source_node_key>``); a note trigger passes ``your note (Tn)``."""
    label = source_label or f"step {source_node_key}"
    node_updates: list[dict] = []
    for n in nodes or []:
        nk = n.get("node_key")
        if nk == source_node_key or (n.get("status") or "") not in ("pending", "blocked"):
            continue
        txt = n.get("prompt_template") or ""
        # Earlier provenance lines quote the old value; they are never a match
        # target (idempotence) and are kept below the rewritten body.
        body_lines = [ln for ln in txt.splitlines() if not ln.lstrip().startswith(PROVENANCE_PREFIX)]
        prov_lines = [ln for ln in txt.splitlines() if ln.lstrip().startswith(PROVENANCE_PREFIX)]
        body = "\n".join(body_lines).rstrip()
        new_body = body
        applied: list[dict] = []
        for c in corrections:
            if _mentions(c["old"], new_body):
                if c["kind"] == "port":
                    # `host:container` mappings (docker -p 127.0.0.1:3001:3001): the
                    # correction names the HOST side; the container side stays.
                    new_body = re.sub(r"(?<![\w.])" + re.escape(c["old"]) + ":" + re.escape(c["old"]) + r"(?![\w])",
                                      c["new"] + ":" + c["old"], new_body)
                    new_body = re.sub(r"(?<!" + re.escape(c["new"]) + r":)(?<![\w.])" + re.escape(c["old"]) + r"(?![\w])",
                                      c["new"], new_body)
                else:
                    new_body = re.sub(r"(?<![\w.])" + re.escape(c["old"]) + r"(?![\w])", c["new"], new_body)
                applied.append(c)
        if applied:
            prov_lines.extend(
                f"{PROVENANCE_PREFIX} {label}: `{c['old']}` → `{c['new']}` ({c['kind']})."
                for c in applied)
            new_txt = new_body + "\n\n" + "\n".join(prov_lines)
            node_updates.append({"node_key": nk, "prompt_template": new_txt,
                                 "corrections": applied})
    guidance_resets: list[str] = []
    for s in steps or []:
        nk = s.get("node_key")
        if nk == source_node_key or (s.get("status") or "") in ("committed", "skipped", "handed_off", "escalated"):
            continue
        g = s.get("guidance") or ""
        if g and any(_mentions(c["old"], g) for c in corrections):
            guidance_resets.append(nk)
    return {"node_updates": node_updates, "guidance_resets": guidance_resets}


def render_note(result: dict) -> str:
    """The operator-facing line. Empty when nothing changed."""
    if result and result.get("trigger") == "decision":
        return render_decision_note(result)
    if result and not (result.get("node_updates") or result.get("guidance_resets")) and result.get("replan_proposal"):
        return (f"🔁 The fix at step {result.get('source_node_key', '?')} may change how later steps are "
                "worded — see the plan-change proposal below and confirm or dismiss it.")
    if not result or not (result.get("node_updates") or result.get("guidance_resets")):
        return ""
    src = result.get("source_node_key", "?")
    if result.get("trigger") == "note":
        head = "🔁 **Plan updated from your note** — the correction has been applied to the steps ahead:"
    elif result.get("trigger") == "substitution":
        head = "🔁 **Plan updated from your environment pin** — the new value has been applied to the steps ahead:"
    else:
        head = f"🔁 **Plan updated after step {src}** — the fix that worked has been applied to the steps ahead:"
    pairs: dict[tuple, list[str]] = {}
    for u in result.get("node_updates") or []:
        for c in u["corrections"]:
            pairs.setdefault((c["old"], c["new"], c["kind"]), []).append(u["node_key"])
    lines = [head]
    for (old, new, kind), nks in pairs.items():
        lines.append(f"- `{old}` → `{new}` ({kind}) in {', '.join(nks)}")
    resets = result.get("guidance_resets") or []
    if resets:
        lines.append(f"- the walkthrough{'s' if len(resets) > 1 else ''} for {', '.join(resets)} "
                     f"will be rewritten from the corrected plan when you reach {'them' if len(resets) > 1 else 'it'}")
    if result.get("replan_proposal"):
        lines.append("- some later steps may also need rewording — see the plan-change proposal below and confirm or dismiss it")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §17.1044 — the decision trigger.
#
# A decision node's notes carry its options ("(1) label — trade-off; (2) …",
# the §17.663 planner rule) and the committed evidence is the deliberation's
# decision record (or the operator's plain answer). The chosen option is read
# from the record deterministically — an option number, or the one option
# whose distinctive terms the record names. What changes: every PENDING step
# downstream of the decision (and any pending step that names a rejected
# option) gets a directive line naming the chosen and rejected options; cached
# walkthroughs that name a rejected option regenerate; the ledger gets a fact;
# and STRUCTURAL impact (a step written only for a rejected option) is routed
# through the existing surface-and-ask re-plan (§17.677) — a model may propose
# a drop/revise, the operator confirms, nothing structural is applied silently.
# ---------------------------------------------------------------------------

DECISION_PREFIX = "🔁 Decision at step"
_OPTION_PAREN_RE = re.compile(r"\((\d{1,2})\)\s*(.+?)(?=\s*(?:;\s*)?\(\d{1,2}\)\s|\s*$)", re.S)
_OPTION_LINE_RE = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+(.+?)\s*$")
_CHOICE_NUM_RE = re.compile(r"(?i)\b(?:option|choice|go with|pick|choose|chose|selected?)\s*\(?#?(\d{1,2})\)?|^\s*\(?(\d{1,2})\)?[.)]?\s*(?:—|-|:)")
_TERM_RE = re.compile(r"(?<![a-z0-9-])(?:--?)?[a-z0-9][a-z0-9_.+-]{2,}")  # keeps `--nginx` distinct from `nginx`
_TERM_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "than", "then",
    "your", "will", "each", "only", "also", "when", "while", "does", "not", "but",
    "use", "uses", "using", "via", "its", "can", "may", "fit", "tradeoff",
    "trade-off", "option", "options", "suggested", "default", "existing", "keep",
})


def parse_options(notes: str) -> list[dict]:
    """``[{n, label, text}]`` from a decision node's notes. ``label`` is the
    text up to the first em-dash / colon / period; ``text`` the whole option."""
    body = notes or ""
    m = re.search(r"(?i)options?\s*:\s*", body)
    if m:
        body = body[m.end():]
    # The planner's trailing "Suggested: …" / "Record the operator's choice…"
    # sentences are not part of the last option (live: they made the first
    # option's own terms look shared with the second).
    body = re.split(r"(?i)\b(?:suggested(?:\s+default)?|recommended)\s*:", body)[0]
    found = [(int(n), t.strip()) for n, t in _OPTION_PAREN_RE.findall(body)]
    if len(found) < 2:
        found = [(int(n), t.strip()) for n, t in _OPTION_LINE_RE.findall(body)]
    out: list[dict] = []
    seen: set[int] = set()
    for n, t in found:
        if n in seen or not t:
            continue
        seen.add(n)
        label = re.split(r"\s+(?:—|–|-{2,})\s+|:\s|\.\s", t, maxsplit=1)[0].strip(" .;")
        out.append({"n": n, "label": label[:120], "text": t[:400]})
    return out if len(out) >= 2 else []


def option_terms(opt: dict, others: list[dict]) -> set[str]:
    """The option's distinctive terms: its LABEL's tokens minus the ones any
    other option uses — what a later step names when it means THIS option. The
    trade-off text is too broad ("rewrites the server block" would flag any
    step that says "server block"); it is used only when the label has none."""
    def _toks(txt: str) -> set[str]:
        return {t for t in _TERM_RE.findall((txt or "").lower()) if t not in _TERM_STOP and not t.isdigit()}
    shared: set[str] = set()
    for o in others:
        shared |= _toks(o.get("label", "") + " " + o.get("text", ""))
    mine = {t for t in _toks(opt.get("label", "")) if t not in shared}
    if not mine:
        mine = {t for t in _toks(opt.get("text", "")) if t not in shared}
    return mine


def choose(options: list[dict], record: str) -> Optional[dict]:
    """The option the committed record names. A number wins; else the single
    option whose distinctive terms the record's opening names; else None."""
    rec = (record or "").strip()
    if not options or not rec:
        return None
    head = rec[:400]
    m = _CHOICE_NUM_RE.search(head)
    if m:
        n = int(m.group(1) or m.group(2))
        for o in options:
            if o["n"] == n:
                return o
    scores = []
    low = head.lower()
    for o in options:
        terms = option_terms(o, [x for x in options if x is not o])
        hits = sum(1 for t in terms if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low))
        scores.append((hits, o))
    scores.sort(key=lambda x: -x[0])
    if scores and scores[0][0] >= 2 and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return scores[0][1]
    return None


def _names_option(text_value: str, terms: set[str]) -> bool:
    low = (text_value or "").lower()
    hits = sum(1 for t in terms if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low))
    return hits >= 2 or any(("-" in t and len(t) >= 5) and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low) for t in terms)


def decision_directive(source_node_key: str, chosen: dict, rejected: list[dict]) -> str:
    rej = "; ".join(f"({o['n']}) {o['label']}" for o in rejected) or "none"
    return (f"{DECISION_PREFIX} {source_node_key}: chosen ({chosen['n']}) {chosen['label']}; "
            f"not {rej}. Follow the chosen option — steps written for a rejected option do not apply as written.")


def decision_changes(nodes: list[dict], steps: list[dict], *, source_node_key: str,
                     downstream: set[str], chosen: dict, rejected: list[dict]) -> dict:
    """Pure: pending steps that get the directive (downstream of the decision,
    or naming a rejected option) and cached walkthroughs to regenerate."""
    rej_terms: set[str] = set()
    for o in rejected:
        rej_terms |= option_terms(o, [chosen] + [x for x in rejected if x is not o])
    directive = decision_directive(source_node_key, chosen, rejected)
    node_updates: list[dict] = []
    for n in nodes or []:
        nk = n.get("node_key")
        if nk == source_node_key or (n.get("status") or "") not in ("pending", "blocked"):
            continue
        txt = n.get("prompt_template") or ""
        if f"{DECISION_PREFIX} {source_node_key}:" in txt:
            continue  # idempotent
        body = "\n".join(ln for ln in txt.splitlines() if not ln.lstrip().startswith(DECISION_PREFIX))
        names_rejected = bool(rej_terms) and _names_option(body, rej_terms)
        if nk in downstream or names_rejected:
            node_updates.append({"node_key": nk, "prompt_template": txt.rstrip() + "\n\n" + directive,
                                 "reason": "names a rejected option" if names_rejected else "depends on the decision"})
    guidance_resets: list[str] = []
    for s in steps or []:
        nk = s.get("node_key")
        if nk == source_node_key or (s.get("status") or "") in ("committed", "skipped", "handed_off", "escalated"):
            continue
        g = s.get("guidance") or ""
        if g and rej_terms and _names_option(g, rej_terms):
            guidance_resets.append(nk)
    return {"node_updates": node_updates, "guidance_resets": guidance_resets}


def render_decision_note(result: dict) -> str:
    if not result or not (result.get("node_updates") or result.get("guidance_resets") or result.get("replan_proposal")):
        return ""
    src = result.get("source_node_key", "?")
    chosen = result.get("chosen") or {}
    lines = [f"🔁 **Decision at step {src} applied to the plan** — chosen ({chosen.get('n', '?')}) {chosen.get('label', '')}."]
    ups = result.get("node_updates") or []
    if ups:
        lines.append(f"- the steps ahead now carry it: {', '.join(u['node_key'] for u in ups)}")
    named = [u["node_key"] for u in ups if u.get("reason") == "names a rejected option"]
    if named:
        lines.append(f"- {', '.join(named)} were written for an option you did not choose — flagged")
    resets = result.get("guidance_resets") or []
    if resets:
        lines.append(f"- the walkthrough{'s' if len(resets) > 1 else ''} for {', '.join(resets)} will be rewritten when you reach {'them' if len(resets) > 1 else 'it'}")
    if result.get("replan_proposal"):
        lines.append("- some steps may need revising or dropping for this choice — see the plan-change proposal below and confirm or dismiss it")
    return "\n".join(lines)


async def _decision_trigger(*, db, session_id: str, job_id: str, node_key: str,
                            evidence: str, nodes: list[dict], steps: list[dict]) -> Optional[dict]:
    src = next((n for n in nodes if n.get("node_key") == node_key), None)
    options = parse_options((src or {}).get("prompt_template") or "")
    if not options:
        logger.info("plan_reconcile_decision_no_options session_id=%s node_key=%s", session_id, node_key)
        return None
    chosen = choose(options, evidence)
    if chosen is None:
        logger.info("plan_reconcile_decision_unresolved session_id=%s node_key=%s options=%d",
                    session_id, node_key, len(options))
        return None
    rejected = [o for o in options if o is not chosen]
    from app.modules.assist_replan import downstream_node_keys
    try:
        downstream = set(await downstream_node_keys(db=db, job_id=job_id, root_node_key=node_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_reconcile_downstream_failed session_id=%s err=%r", session_id, exc)
        downstream = set()
    changes = decision_changes(nodes, steps, source_node_key=node_key, downstream=downstream,
                               chosen=chosen, rejected=rejected)
    changes.update({"trigger": "decision", "source_node_key": node_key, "chosen": chosen,
                    "rejected": rejected, "replan_proposal": None})
    for u in changes["node_updates"]:
        await db.execute(text("""
            UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
             WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
        """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
    for nk in changes["guidance_resets"]:
        await db.execute(text("""
            UPDATE assist_steps
               SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL, updated_at = NOW()
             WHERE session_id = :sid AND node_key = :nk
               AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
        """), {"sid": session_id, "nk": nk})
    entry = {
        "at": datetime.now(timezone.utc).isoformat(), "trigger": "decision",
        "source_node_key": node_key, "chosen": chosen, "rejected": rejected,
        "nodes": [u["node_key"] for u in changes["node_updates"]],
        "changes": node_diffs(nodes, changes["node_updates"]),  # §17.1047
        "guidance_resets": changes["guidance_resets"],
    }
    await db.execute(text("""
        UPDATE jobs
           SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                    COALESCE(metadata->'reconciliation', '[]'::jsonb) || CAST(:e AS jsonb))
         WHERE id = :jid
    """), {"e": json.dumps(entry), "jid": job_id})
    await db.commit()
    try:
        from app.modules.assist_environment import set_environment
        await set_environment(session_id=session_id, db=db, facts=[
            f"Decided at {node_key}: ({chosen['n']}) {chosen['label']}; not "
            + ("; ".join(f"({o['n']}) {o['label']}" for o in rejected) or "none")])
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_reconcile_fact_failed session_id=%s err=%r", session_id, exc)
    # Structural impact → the existing surface-and-ask re-plan (model proposes,
    # operator confirms). Only when a pending step names a rejected option.
    named = [u["node_key"] for u in changes["node_updates"] if u.get("reason") == "names a rejected option"]
    if named:
        try:
            from app.modules.assist_notes import assess_note_impact
            note = (f"Decision at {node_key} ({(src or {}).get('title') or node_key}): the operator chose "
                    f"({chosen['n']}) {chosen['label']}. Rejected: "
                    + ("; ".join(f"({o['n']}) {o['label']}" for o in rejected) or "none")
                    + f". Steps {', '.join(named)} were written for a rejected option and must be revised or dropped.")
            changes["replan_proposal"] = await assess_note_impact(
                session_id=session_id, note_kind="decision", note_text=note, db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_reconcile_decision_impact_failed session_id=%s err=%r", session_id, exc)
    logger.warning(
        "plan_reconcile_decision_applied session_id=%s node_key=%s chosen=%r rejected=%r nodes=%r "
        "guidance_resets=%r proposal=%s", session_id, node_key, chosen["label"],
        [o["label"] for o in rejected], entry["nodes"], entry["guidance_resets"],
        bool(changes.get("replan_proposal")))
    return changes


# ---------------------------------------------------------------------------
# §17.1045 — the note trigger. Only what the note SAYS is a correction: two
# values of one kind joined by the operator's own connective. No inference
# from a lone value — "the NAS is 10.20.0.40" beside a plan that says
# 10.20.0.5 for the server is not a correction, it is a second machine.
# ---------------------------------------------------------------------------

_NEW_THEN_OLD_RE = re.compile(r"(?i)^\W*(?:not|instead\s+of|rather\s+than|replaces|replacing|was|previously|no\s+longer|and\s+not)\W*$")
_OLD_THEN_NEW_RE = re.compile(r"(?i)^\W*(?:->|→|=>|is\s+now|becomes|became|changed\s+to|moved\s+to|should\s+be|must\s+be|to|now)\W*$")
_FROM_RE = re.compile(r"(?i)\bfrom\W*$")


def note_corrections(note_text: str, plan_text: str) -> tuple[list[dict], list[dict]]:
    """``(corrections, declined)`` from a note's own wording: two values of the
    same kind with a connective between them. The OLD value must appear in the
    pending plan text (else there is nothing to apply). One pair per kind."""
    txt = note_text or ""
    found = values_in(txt)
    # In a note, a bare number beside a port ("port 3002 instead of 3001") is
    # the other port — the evidence layer's extractor only takes "port N" / ":N".
    if any(it["kind"] == "port" for it in found):
        known = {it["value"] for it in found}
        for m in re.finditer(r"(?<![\w.:])(\d{4,5})(?![\w.])", txt):
            n = m.group(1)
            if n not in known and 1024 <= int(n) <= 65535:
                found.append({"kind": "port", "value": n})
                known.add(n)
    items = []
    for it in found:
        for m in re.finditer(r"(?<![\w.])" + re.escape(it["value"]) + r"(?![\w])", txt):
            items.append((m.start(), m.end(), it["kind"], it["value"]))
    items.sort()
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for i in range(len(items) - 1):
        a, b = items[i], items[i + 1]
        if a[2] != b[2] or a[3] == b[3]:
            continue
        between = txt[a[1]:b[0]]
        if len(between) > 40:
            continue
        pair = None
        if _NEW_THEN_OLD_RE.match(between):
            pair = (b[3], a[3])  # old=b, new=a
        elif _OLD_THEN_NEW_RE.match(between):
            if re.search(r"(?i)^\W*to\W*$", between) and not _FROM_RE.search(txt[max(0, a[0] - 12):a[0]]):
                continue  # "X to Y" only counts as "from X to Y"
            pair = (a[3], b[3])
        if pair:
            by_kind.setdefault(a[2], [])
            if pair not in by_kind[a[2]]:
                by_kind[a[2]].append(pair)
    corrections: list[dict] = []
    declined: list[dict] = []
    for kind, pairs in by_kind.items():
        pairs = [(o, n) for o, n in pairs if _mentions(o, plan_text)]
        if not pairs:
            continue
        if len(pairs) == 1:
            corrections.append({"kind": kind, "old": pairs[0][0], "new": pairs[0][1]})
        else:
            declined.append({"kind": kind, "pairs": pairs[:4]})
    return corrections, declined


async def reconcile_after_note(*, db, session_id: str, job_id: str, note_text: str,
                               note_kind: str, node_key: Optional[str]) -> Optional[dict]:
    """The note trigger: apply a correction the note states in its own words to
    every pending step that still carries the old value. Fail-soft; None when
    the note states no correction the plan carries."""
    if not settings.plan_reconcile_enabled:
        return None
    try:
        nodes = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, prompt_template FROM dag_nodes
             WHERE job_id = :jid ORDER BY execution_order
        """), {"jid": job_id})).mappings().all()]
        steps = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, guidance FROM assist_steps WHERE session_id = :sid
        """), {"sid": session_id})).mappings().all()]
        plan_text = "\n".join((n.get("prompt_template") or "") for n in nodes
                              if (n.get("status") or "") in ("pending", "blocked"))
        corrections, declined = note_corrections(note_text, plan_text)
        if declined:
            logger.info("plan_reconcile_note_declined session_id=%s pairs=%r", session_id, declined[:4])
        if not corrections:
            logger.info("plan_reconcile_note_no_corrections session_id=%s kind=%s", session_id, note_kind)
            return None
        src = node_key or "?"
        changes = plan_changes(nodes, steps, corrections, source_node_key=src,
                               source_label=f"your note ({src})")
        changes.update({"trigger": "note", "source_node_key": src, "corrections": corrections})
        for u in changes["node_updates"]:
            await db.execute(text("""
                UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
            """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
        for nk in changes["guidance_resets"]:
            await db.execute(text("""
                UPDATE assist_steps
                   SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL, updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
            """), {"sid": session_id, "nk": nk})
        entry = {
            "at": datetime.now(timezone.utc).isoformat(), "trigger": "note",
            "source_node_key": src, "note_kind": note_kind, "corrections": corrections,
            "nodes": [u["node_key"] for u in changes["node_updates"]],
            "changes": node_diffs(nodes, changes["node_updates"]),  # §17.1047
            "guidance_resets": changes["guidance_resets"],
        }
        await db.execute(text("""
            UPDATE jobs
               SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                        COALESCE(metadata->'reconciliation', '[]'::jsonb) || CAST(:e AS jsonb))
             WHERE id = :jid
        """), {"e": json.dumps(entry), "jid": job_id})
        await db.commit()
        logger.warning("plan_reconcile_note_applied session_id=%s node_key=%s corrections=%r nodes=%r guidance_resets=%r",
                       session_id, src, [(c["old"], c["new"]) for c in corrections], entry["nodes"],
                       entry["guidance_resets"])
        return changes
    except Exception as exc:  # noqa: BLE001 — never break note-taking
        logger.warning("plan_reconcile_note_failed session_id=%s err=%r", session_id, exc)
        return None


# ---------------------------------------------------------------------------
# §17.1046 — the substitution trigger. A re-pinned KEY is the cleanest
# correction record there is: old value = the previous pin, new value = the
# new pin, both typed by the operator. A NEW key rewrites nothing (its
# `<KEY>` placeholders resolve at render time) but a cached walkthrough that
# still shows the bare placeholder is stale and regenerates.
# ---------------------------------------------------------------------------

def substitution_corrections(old_subs: dict, new_subs: dict) -> tuple[list[dict], list[str]]:
    """``(corrections, new_keys)``: a correction per key whose value changed
    (kind inferred from the value's shape, else ``value``), and the keys that
    were pinned for the first time."""
    corrections: list[dict] = []
    new_keys: list[str] = []
    for k, new in (new_subs or {}).items():
        new_s = str(new or "").strip()
        if not new_s:
            continue
        old_s = str((old_subs or {}).get(k) or "").strip()
        if not old_s:
            new_keys.append(k)
            continue
        if old_s == new_s:
            continue
        kinds = {it["kind"] for it in values_in(new_s) if it["value"] == new_s}
        kind = next(iter(kinds)) if len(kinds) == 1 else "value"
        # A bare number in the port range, or a key that says so, is a port —
        # the `host:container` mapping rule must apply to it (live: a "value"
        # kind rewrote `-p 127.0.0.1:3001:3001` to `3002:3002`).
        if kind == "value" and (("PORT" in k.upper()) or (new_s.isdigit() and old_s.isdigit()
                                                          and 1024 <= int(new_s) <= 65535 and 1024 <= int(old_s) <= 65535)):
            kind = "port"
        corrections.append({"kind": kind, "old": old_s, "new": new_s, "key": k})
    return corrections, new_keys


def placeholder_resets(steps: list[dict], new_keys: list[str]) -> list[str]:
    """Pending steps whose cached walkthrough still shows a bare `<KEY>` that
    now has a pin."""
    out: list[str] = []
    for s in steps or []:
        if (s.get("status") or "") in ("committed", "skipped", "handed_off", "escalated"):
            continue
        g = s.get("guidance") or ""
        if g and any(f"<{k}>" in g for k in new_keys):
            out.append(s.get("node_key"))
    return out


async def reconcile_after_substitution(*, db, session_id: str, job_id: str,
                                       old_subs: dict, new_subs: dict,
                                       node_key: Optional[str] = None) -> Optional[dict]:
    """The substitution trigger. Fail-soft; None when nothing changed."""
    if not settings.plan_reconcile_enabled:
        return None
    try:
        corrections, new_keys = substitution_corrections(old_subs, new_subs)
        if not corrections and not new_keys:
            return None
        nodes = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, prompt_template FROM dag_nodes
             WHERE job_id = :jid ORDER BY execution_order
        """), {"jid": job_id})).mappings().all()]
        steps = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, guidance FROM assist_steps WHERE session_id = :sid
        """), {"sid": session_id})).mappings().all()]
        src = node_key or "env"
        changes = plan_changes(nodes, steps, corrections, source_node_key=src,
                               source_label="your environment pin") if corrections else {"node_updates": [], "guidance_resets": []}
        for nk in placeholder_resets(steps, new_keys):
            if nk not in changes["guidance_resets"]:
                changes["guidance_resets"].append(nk)
        changes.update({"trigger": "substitution", "source_node_key": src,
                        "corrections": corrections, "new_keys": new_keys})
        if not (changes["node_updates"] or changes["guidance_resets"]):
            logger.info("plan_reconcile_substitution_nothing_to_apply session_id=%s keys=%r",
                        session_id, [c["key"] for c in corrections] + new_keys)
            return None
        for u in changes["node_updates"]:
            await db.execute(text("""
                UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
            """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
        for nk in changes["guidance_resets"]:
            await db.execute(text("""
                UPDATE assist_steps
                   SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL, updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
            """), {"sid": session_id, "nk": nk})
        entry = {
            "at": datetime.now(timezone.utc).isoformat(), "trigger": "substitution",
            "source_node_key": src, "corrections": corrections, "new_keys": new_keys,
            "nodes": [u["node_key"] for u in changes["node_updates"]],
            "changes": node_diffs(nodes, changes["node_updates"]),  # §17.1047
            "guidance_resets": changes["guidance_resets"],
        }
        await db.execute(text("""
            UPDATE jobs
               SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                        COALESCE(metadata->'reconciliation', '[]'::jsonb) || CAST(:e AS jsonb))
             WHERE id = :jid
        """), {"e": json.dumps(entry), "jid": job_id})
        await db.commit()
        logger.warning("plan_reconcile_substitution_applied session_id=%s corrections=%r new_keys=%r nodes=%r guidance_resets=%r",
                       session_id, [(c["key"], c["old"], c["new"]) for c in corrections], new_keys,
                       entry["nodes"], entry["guidance_resets"])
        return changes
    except Exception as exc:  # noqa: BLE001 — never break an environment update
        logger.warning("plan_reconcile_substitution_failed session_id=%s err=%r", session_id, exc)
        return None


# ---------------------------------------------------------------------------
# §17.1047 — the change ledger as a plan-view diff, with revert.
#
# Every entry records, per rewritten node, the task text BEFORE and AFTER, so
# the operator can see exactly what a trigger changed and put it back. A
# revert restores `before` on nodes that are still pending and still carry
# exactly the `after` text (a later trigger or edit wins), resets their cached
# walkthroughs, and stamps the entry `reverted_at` — the ledger keeps both the
# change and its reversal.
# ---------------------------------------------------------------------------

def node_diffs(nodes: list[dict], node_updates: list[dict]) -> list[dict]:
    before = {n.get("node_key"): (n.get("prompt_template") or "") for n in nodes or []}
    return [{"node_key": u["node_key"], "before": before.get(u["node_key"], ""),
             "after": u["prompt_template"]} for u in node_updates or []]


def revertable(entry: dict, nodes: list[dict]) -> list[dict]:
    """The entry's node changes that can still be put back: node pending or
    blocked, and its current text still exactly the entry's `after`."""
    if not entry or entry.get("reverted_at"):
        return []
    cur = {n.get("node_key"): n for n in nodes or []}
    out: list[dict] = []
    for ch in entry.get("changes") or []:
        n = cur.get(ch.get("node_key"))
        if not n or (n.get("status") or "") not in ("pending", "blocked"):
            continue
        if (n.get("prompt_template") or "") != (ch.get("after") or ""):
            continue
        out.append(ch)
    return out


async def list_reconciliation(*, db, job_id: str) -> list[dict]:
    """The job's change ledger, oldest first, each entry with its index and
    which of its changes are still revertable."""
    raw = (await db.execute(text("SELECT metadata->'reconciliation' FROM jobs WHERE id = :jid"),
                            {"jid": job_id})).scalar()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    entries = list(raw or []) if isinstance(raw, list) else []
    nodes = [dict(r) for r in (await db.execute(text("""
        SELECT node_key, status, prompt_template FROM dag_nodes WHERE job_id = :jid
    """), {"jid": job_id})).mappings().all()]
    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        rv = revertable(e, nodes)
        out.append({**e, "index": i, "revertable": [c["node_key"] for c in rv]})
    return out


async def revert_reconciliation(*, db, job_id: str, index: int) -> dict:
    """Put back the entry's node texts where still possible; stamp the entry."""
    raw = (await db.execute(text("SELECT metadata->'reconciliation' FROM jobs WHERE id = :jid"),
                            {"jid": job_id})).scalar()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    entries = list(raw or []) if isinstance(raw, list) else []
    if index < 0 or index >= len(entries) or not isinstance(entries[index], dict):
        return {"error": "no such change", "index": index}
    entry = entries[index]
    if entry.get("reverted_at"):
        return {"index": index, "reverted": [], "already_reverted_at": entry["reverted_at"]}
    nodes = [dict(r) for r in (await db.execute(text("""
        SELECT node_key, status, prompt_template FROM dag_nodes WHERE job_id = :jid
    """), {"jid": job_id})).mappings().all()]
    rv = revertable(entry, nodes)
    reverted: list[str] = []
    for ch in rv:
        res = await db.execute(text("""
            UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
             WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
               AND prompt_template = :after
        """), {"pt": ch["before"], "jid": job_id, "nk": ch["node_key"], "after": ch["after"]})
        if res.rowcount:
            reverted.append(ch["node_key"])
    if reverted:
        await db.execute(text("""
            UPDATE assist_steps s SET guidance = NULL, guidance_status = 'none',
                   guidance_generated_at = NULL, updated_at = NOW()
              FROM assist_sessions ss
             WHERE s.session_id = ss.id AND ss.job_id = :jid AND s.node_key = ANY(CAST(:keys AS text[]))
               AND s.status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
        """), {"jid": job_id, "keys": reverted})
    stamp = {"reverted_at": datetime.now(timezone.utc).isoformat(), "reverted": reverted}
    await db.execute(text("""
        UPDATE jobs
           SET metadata = jsonb_set(metadata, CAST(:path AS text[]),
                                    (metadata->'reconciliation'->CAST(:i AS int)) || CAST(:st AS jsonb))
         WHERE id = :jid
    """), {"path": ["reconciliation", str(index)], "i": index, "st": json.dumps(stamp), "jid": job_id})
    await db.commit()
    logger.warning("plan_reconcile_reverted job=%s index=%d reverted=%r skipped=%r",
                   job_id, index, reverted, [c["node_key"] for c in (entry.get("changes") or []) if c["node_key"] not in reverted])
    return {"index": index, "reverted": reverted, "reverted_at": stamp["reverted_at"]}


# ---------------------------------------------------------------------------
# §17.1048 — model-PROPOSED corrections for what value pairing cannot see.
#
# A confirmed fix often changes a METHOD, not a value: "no symlink is needed
# here", "run it with the other interpreter", "this host has no such
# directory". The deterministic triggers cannot name those. A model reads the
# failing paste, the fix that worked and the operator's closing paste, and
# proposes exact phrase rewrites for pending steps — and every proposal must
# pass the same grounding the evidence layer applies to answers: the OLD phrase
# must be in that step's text verbatim, the NEW phrase's distinctive terms must
# come from the fix reply, the closing paste or the confirmed facts. What
# survives is STAGED as a plan-change proposal (the §17.677 surface) — the
# operator confirms, then the same deterministic rewrite applies it with a
# provenance line and a ledger entry (revertable like every other change).
# ---------------------------------------------------------------------------

PROPOSE_CORRECTIONS_TOOL = Tool(
    name="propose_plan_corrections",
    description=(
        "Report which still-pending plan steps carry an instruction the confirmed "
        "fix showed to be wrong for this operator's system, and the exact rewrite."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "corrections": {
                "type": "array",
                "description": "Empty when no pending step needs a wording change.",
                "items": {
                    "type": "object",
                    "properties": {
                        "node_key": {"type": "string", "description": "A pending step's node_key."},
                        "old_text": {"type": "string",
                                     "description": "A phrase copied VERBATIM from that step's text that is now wrong."},
                        "new_text": {"type": "string",
                                     "description": "The replacement, using only what the fix and the operator's output established."},
                        "reason": {"type": "string", "description": "One sentence: what the confirmed fix showed."},
                    },
                    "required": ["node_key", "old_text", "new_text"],
                },
            }
        },
        "required": ["corrections"],
    },
)

_PROPOSE_OPENING = (
    "A step of the operator's plan just failed, was fixed, and the operator's own output "
    "confirmed the fix. Some LATER steps may still carry the instruction that failed, or an "
    "assumption the fix disproved (a directory that does not exist on this system, a tool that is "
    "not the one in use, a sub-step that is unnecessary here). Propose the smallest exact "
    "rewrites to those pending steps. Rules: copy `old_text` VERBATIM from the step's text; write "
    "`new_text` only from what the fix reply or the operator's output established — never from "
    "general knowledge; leave steps alone when nothing in them is contradicted; never propose a "
    "change to a step that only sets a value (addresses, ports, versions are handled elsewhere)."
)


def _distinctive(text_value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9_.+/-]{3,}", (text_value or "").lower())
            if t not in _TERM_STOP}


def locate_phrase(phrase: str, text_value: str) -> Optional[str]:
    """The step's own text for a phrase the model quoted — verbatim, or
    verbatim modulo whitespace, surrounding quotes/backticks and case (models
    normalise those); None when the phrase is not in the step at all."""
    if not phrase or not text_value:
        return None
    if phrase in text_value:
        return phrase
    toks = [t.strip("`'\"") for t in re.split(r"\s+", phrase.strip()) if t.strip("`'\"")]
    if not toks:
        return None
    pat = r"[`'\"]?" + r"[`'\"]?\s+[`'\"]?".join(re.escape(t) for t in toks) + r"[`'\"]?"
    m = re.search(pat, text_value, flags=re.IGNORECASE)
    return m.group(0) if m else None


def grounded_proposals(raw: list, nodes: list[dict], *, evidence_text: str) -> tuple[list[dict], list[dict]]:
    """Keep a proposal only when: the node is pending; `old_text` is in that
    node's text verbatim (and is not a provenance line); `new_text` differs and
    at least one of its distinctive terms appears in the evidence (fix reply,
    closing paste, confirmed facts). Returns ``(kept, dropped)``."""
    by_key = {n.get("node_key"): n for n in nodes or []}
    ev = (evidence_text or "").lower()
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        nk = str(item.get("node_key") or "").strip()
        old = str(item.get("old_text") or "").strip()
        new = str(item.get("new_text") or "").strip()
        reason = str(item.get("reason") or "").strip()[:300]
        why = None
        n = by_key.get(nk)
        if not n or (n.get("status") or "") not in ("pending", "blocked"):
            why = "node not pending"
        elif len(old) < 4 or locate_phrase(old, n.get("prompt_template") or "") is None:
            why = "old_text not verbatim in the step"
        elif locate_phrase(old, n.get("prompt_template") or "").lstrip().startswith(("🔁", PROVENANCE_PREFIX, DECISION_PREFIX)):
            why = "old_text is a provenance line"
        elif not new or new == old:
            why = "no replacement"
        else:
            terms = _distinctive(new) - _distinctive(old)
            if terms and not any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", ev) for t in terms):
                why = "new_text not grounded in the fix or the operator's output"
        if why:
            dropped.append({"node_key": nk, "old_text": old[:120], "new_text": new[:120], "why": why})
            continue
        kept.append({"node_key": nk, "action": "rewrite",
                     "current_assumption": locate_phrase(old, n.get("prompt_template") or "")[:300],
                     "proposed_change": new[:400], "reason": reason})
        if len(kept) >= 8:
            break
    return kept, dropped


async def propose_corrections(*, nodes: list[dict], failing_pastes: list[str], fix_replies: list[str],
                              closing_paste: str, confirmed_facts: str = "",
                              model_overrides: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """One model call → grounded proposals. Fail-soft → ``([], [])``."""
    pending = [n for n in nodes or [] if (n.get("status") or "") in ("pending", "blocked")]
    if not pending or not fix_replies:
        return [], []
    steps_block = "\n".join(
        f"- {n.get('node_key')}: {(n.get('title') or '')[:80]} — {(n.get('prompt_template') or '')[:400]}"
        for n in pending)
    msg = (
        _PROPOSE_OPENING + "\n\nPENDING STEPS:\n" + steps_block
        + "\n\nWHAT FAILED (the operator's paste):\n" + ("\n".join(failing_pastes)[-1500:] or "(none)")
        + "\n\nTHE FIX THAT WORKED (the engine's reply):\n" + ("\n".join(fix_replies)[-2500:])
        + "\n\nTHE OPERATOR'S CLOSING OUTPUT (confirms the fix):\n" + (closing_paste or "")[-800:]
        + ("\n\nCONFIRMED FACTS ABOUT THIS SYSTEM:\n" + confirmed_facts[-1200:] if confirmed_facts else "")
    )
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    try:
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": msg}],
            tools=[PROPOSE_CORRECTIONS_TOOL], role="model_general", overrides=model_overrides,
            temperature=0.0, tool_choice="auto", max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001 — a proposal must never block a commit
        logger.warning("plan_reconcile_propose_failed: %r", exc)
        return [], []
    args = read_tool_args(resp)
    raw = (args or {}).get("corrections")
    if not isinstance(raw, list):
        logger.info("plan_reconcile_propose_unparsed text_head=%r", (getattr(resp, "text", "") or "")[:160])
        return [], []
    evidence = "\n".join(fix_replies) + "\n" + (closing_paste or "") + "\n" + (confirmed_facts or "")
    kept, dropped = grounded_proposals(raw, nodes, evidence_text=evidence)
    logger.info("plan_reconcile_model_proposals raw=%d kept=%d dropped=%r",
                len(raw), len(kept), [(d["node_key"], d["why"], d["old_text"][:60]) for d in dropped][:6])
    return kept, dropped


async def apply_rewrite_proposals(*, db, session_id: str, job_id: str, proposals: list[dict],
                                  source_node_key: str) -> list[str]:
    """Apply CONFIRMED rewrite proposals with the same deterministic machinery
    as every other trigger (provenance line, walkthrough reset, ledger entry
    with before/after so it is revertable). Returns the node keys rewritten."""
    rewrites = [p for p in proposals or [] if p.get("action") == "rewrite"
                and (p.get("current_assumption") or "").strip() and (p.get("proposed_change") or "").strip()]
    if not rewrites:
        return []
    nodes = [dict(r) for r in (await db.execute(text("""
        SELECT node_key, status, prompt_template FROM dag_nodes WHERE job_id = :jid ORDER BY execution_order
    """), {"jid": job_id})).mappings().all()]
    steps = [dict(r) for r in (await db.execute(text("""
        SELECT node_key, status, guidance FROM assist_steps WHERE session_id = :sid
    """), {"sid": session_id})).mappings().all()]
    original = {n.get("node_key"): (n.get("prompt_template") or "") for n in nodes}
    all_updates: list[dict] = []
    resets: list[str] = []
    corrections_done: list[dict] = []
    for p in rewrites:
        corr = [{"kind": "text", "old": p["current_assumption"].strip(), "new": p["proposed_change"].strip()}]
        target = [n for n in nodes if n.get("node_key") == p.get("node_key")]
        ch = plan_changes(target, steps, corr, source_node_key=source_node_key,
                          source_label=f"the confirmed fix at {source_node_key} (change you approved)")
        for u in ch["node_updates"]:
            await db.execute(text("""
                UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
            """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
            all_updates.append(u)
            corrections_done.append({**corr[0], "node_key": u["node_key"], "reason": p.get("reason", "")})
            for n in nodes:  # keep the in-memory copy current for the diff and later proposals
                if n.get("node_key") == u["node_key"]:
                    n["prompt_template"] = u["prompt_template"]
        for nk in ch["guidance_resets"]:
            if nk not in resets:
                resets.append(nk)
    if not all_updates:
        return []
    for nk in resets:
        await db.execute(text("""
            UPDATE assist_steps SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL, updated_at = NOW()
             WHERE session_id = :sid AND node_key = :nk AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
        """), {"sid": session_id, "nk": nk})
    entry = {
        "at": datetime.now(timezone.utc).isoformat(), "trigger": "model_proposed",
        "source_node_key": source_node_key, "corrections": corrections_done,
        "nodes": sorted({u["node_key"] for u in all_updates}),
        "changes": [{"node_key": nk, "before": original.get(nk, ""),
                     "after": next(n for n in nodes if n.get("node_key") == nk)["prompt_template"]}
                    for nk in sorted({u["node_key"] for u in all_updates})],
        "guidance_resets": resets,
    }
    await db.execute(text("""
        UPDATE jobs
           SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                    COALESCE(metadata->'reconciliation', '[]'::jsonb) || CAST(:e AS jsonb))
         WHERE id = :jid
    """), {"e": json.dumps(entry), "jid": job_id})
    await db.commit()
    logger.warning("plan_reconcile_model_proposed_applied session_id=%s source=%s nodes=%r",
                   session_id, source_node_key, entry["nodes"])
    return entry["nodes"]


async def _stage_model_proposals(*, db, session_id: str, node_key: str, nodes: list[dict],
                                 ctx: dict, closing: str, confirmed_facts: str) -> Optional[dict]:
    """§17.1048 — model proposes, grounding filters, the §17.677 surface stages;
    nothing is applied here. Fail-soft → None."""
    if not settings.plan_reconcile_model_proposals_enabled:
        return None
    try:
        kept, _dropped = await propose_corrections(
            nodes=nodes, failing_pastes=ctx["failing"], fix_replies=ctx["fixes"],
            closing_paste=closing, confirmed_facts=confirmed_facts)
        if not kept:
            return None
        for k in kept:
            k["source_node_key"] = node_key
        from app.modules.assist_notes import _stage_replan_proposal
        note_text = (f"The fix at {node_key} worked: "
                     + " ".join((closing or "").split())[:160])
        return await _stage_replan_proposal(
            session_id=session_id, note_text=note_text, note_kind="fix", affected=kept, db=db)
    except Exception as exc:  # noqa: BLE001 — a proposal must never break a commit
        logger.warning("plan_reconcile_stage_proposals_failed session_id=%s err=%r", session_id, exc)
        return None


async def _fix_context(*, db, session_id: str, node_key: str) -> Optional[dict]:
    step = (await db.execute(text("""
        SELECT presented_at, evidence FROM assist_steps
         WHERE session_id = :sid AND node_key = :nk
    """), {"sid": session_id, "nk": node_key})).mappings().first()
    if not step:
        return None
    rows = (await db.execute(text("""
        SELECT role, kind, content, created_at FROM assist_turns
         WHERE session_id = :sid AND node_key = :nk
           AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz))
         ORDER BY created_at, id
    """), {"sid": session_id, "nk": node_key, "since": step["presented_at"]})).mappings().all()
    fixes = [r["content"] or "" for r in rows if r["role"] == "assistant" and r["kind"] == "fix"]
    if not fixes:
        return None
    last_fix_at = max(r["created_at"] for r in rows if r["role"] == "assistant" and r["kind"] == "fix")
    before_fix = [r["content"] or "" for r in rows
                  if r["role"] == "operator" and r["kind"] in ("message", "submit") and r["created_at"] <= last_fix_at]
    # The FAILING pastes are the ones that read as a failure (§17.1027's
    # symptom detector). A success-shaped paste the verifier refused before
    # the fix (live: a closing paste judged incomplete, then a fix, then the
    # real close) must not make the fix's new value look "part of the failure".
    from app.modules.assist_evidence import derive_need
    failing = [t for t in before_fix if derive_need(t).kind == "error"] or before_fix
    return {"fixes": fixes, "failing": failing, "closing": step["evidence"] or ""}


async def reconcile_after_commit(*, db, session_id: str, job_id: str, node_key: str,
                                 evidence: str) -> Optional[dict]:
    """The fix-confirmed trigger. Called after a step is committed; returns the
    change record (or None when the step had no fix, or nothing applied).
    Fail-soft: a reconciliation failure never breaks a commit."""
    if not settings.plan_reconcile_enabled:
        return None
    try:
        nodes = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, node_type, title, prompt_template FROM dag_nodes
             WHERE job_id = :jid ORDER BY execution_order
        """), {"jid": job_id})).mappings().all()]
        steps = [dict(r) for r in (await db.execute(text("""
            SELECT node_key, status, guidance FROM assist_steps WHERE session_id = :sid
        """), {"sid": session_id})).mappings().all()]
        src = next((n for n in nodes if n.get("node_key") == node_key), None)
        if src and (src.get("node_type") or "").strip().lower() == "decision":
            return await _decision_trigger(db=db, session_id=session_id, job_id=job_id,
                                           node_key=node_key, evidence=evidence, nodes=nodes, steps=steps)
        ctx = await _fix_context(db=db, session_id=session_id, node_key=node_key)
        if not ctx:
            return None
        closing = (evidence or "") + "\n" + (ctx["closing"] or "")
        plan_text = "\n".join((n.get("prompt_template") or "") for n in nodes
                              if (n.get("status") or "") in ("pending", "blocked"))
        from app.modules.assist_environment import _environment_from_metadata
        from app.modules.assist_evidence import ledger_text
        meta = (await db.execute(text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
                                 {"sid": session_id})).scalar()
        env = _environment_from_metadata(meta)
        corrections, declined = derive_corrections(
            failing_pastes=ctx["failing"], fix_replies=ctx["fixes"], closing_paste=closing,
            plan_text=plan_text, confirmed_facts=ledger_text(env))
        if declined:
            logger.info("plan_reconcile_declined session_id=%s node_key=%s candidates=%r",
                        session_id, node_key, declined[:4])
        if not corrections:
            logger.info("plan_reconcile_no_corrections session_id=%s node_key=%s fixes=%d",
                        session_id, node_key, len(ctx["fixes"]))
            # §17.1048 — no value pair, but the fix may still change how later
            # steps are worded: ask the model, stage what survives grounding.
            proposal = await _stage_model_proposals(
                db=db, session_id=session_id, node_key=node_key, nodes=nodes, ctx=ctx,
                closing=closing, confirmed_facts=ledger_text(env))
            if proposal:
                return {"trigger": "fix_confirmed", "source_node_key": node_key, "node_updates": [],
                        "guidance_resets": [], "corrections": [], "replan_proposal": proposal}
            return None
        changes = plan_changes(nodes, steps, corrections, source_node_key=node_key)
        changes["source_node_key"] = node_key
        changes["corrections"] = corrections
        changes["trigger"] = "fix_confirmed"
        for u in changes["node_updates"]:
            await db.execute(text("""
                UPDATE dag_nodes SET prompt_template = :pt, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk AND status IN ('pending', 'blocked')
            """), {"pt": u["prompt_template"], "jid": job_id, "nk": u["node_key"]})
        for nk in changes["guidance_resets"]:
            await db.execute(text("""
                UPDATE assist_steps
                   SET guidance = NULL, guidance_status = 'none', guidance_generated_at = NULL,
                       updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
            """), {"sid": session_id, "nk": nk})
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "trigger": "fix_confirmed",
            "source_node_key": node_key,
            "corrections": corrections,
            "nodes": [u["node_key"] for u in changes["node_updates"]],
            "changes": node_diffs(nodes, changes["node_updates"]),  # §17.1047
            "guidance_resets": changes["guidance_resets"],
        }
        await db.execute(text("""
            UPDATE jobs
               SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{reconciliation}',
                                        COALESCE(metadata->'reconciliation', '[]'::jsonb)
                                        || CAST(:e AS jsonb))
             WHERE id = :jid
        """), {"e": json.dumps(entry), "jid": job_id})
        await db.commit()
        try:
            from app.modules.assist_environment import set_environment
            await set_environment(
                session_id=session_id, db=db,
                facts=[f"Corrected at {node_key}: {c['old']} → {c['new']} ({c['kind']}), confirmed by the operator"
                       for c in corrections])
        except Exception as exc:  # noqa: BLE001 — the ledger fact is a courtesy
            logger.warning("plan_reconcile_fact_failed session_id=%s err=%r", session_id, exc)
        logger.warning(
            "plan_reconcile_applied session_id=%s node_key=%s corrections=%r nodes=%r guidance_resets=%r",
            session_id, node_key, [(c["old"], c["new"]) for c in corrections],
            entry["nodes"], entry["guidance_resets"])
        # §17.1048 — and the wording changes value pairing cannot see, on the
        # rewritten texts, staged for the operator to confirm.
        for u in changes["node_updates"]:
            for n in nodes:
                if n.get("node_key") == u["node_key"]:
                    n["prompt_template"] = u["prompt_template"]
        changes["replan_proposal"] = await _stage_model_proposals(
            db=db, session_id=session_id, node_key=node_key, nodes=nodes, ctx=ctx,
            closing=closing, confirmed_facts=ledger_text(env))
        return changes
    except Exception as exc:  # noqa: BLE001 — never break a commit
        logger.warning("plan_reconcile_failed session_id=%s node_key=%s err=%r", session_id, node_key, exc)
        return None
