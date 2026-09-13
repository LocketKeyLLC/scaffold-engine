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
is applied to every pending step that still carries the old value. It derives correction records
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
        # NEW: proposed by a fix AND stated by the operator's system afterwards.
        new = [v for v in by_kind_fix[kind] if _mentions(v, confirmed)]
        # OLD: seen while failing, absent from the closing paste and the
        # confirmed facts, and not itself a proposed value.
        old = [v for v in by_kind_old[kind] if v not in new and not _mentions(v, confirmed)]
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
    if not result or not (result.get("node_updates") or result.get("guidance_resets")):
        return ""
    src = result.get("source_node_key", "?")
    if result.get("trigger") == "note":
        head = "🔁 **Plan updated from your note** — the correction has been applied to the steps ahead:"
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
    failing = [r["content"] or "" for r in rows
               if r["role"] == "operator" and r["kind"] in ("message", "submit") and r["created_at"] <= last_fix_at]
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
            return None
        changes = plan_changes(nodes, steps, corrections, source_node_key=node_key)
        changes["source_node_key"] = node_key
        changes["corrections"] = corrections
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
        return changes
    except Exception as exc:  # noqa: BLE001 — never break a commit
        logger.warning("plan_reconcile_failed session_id=%s node_key=%s err=%r", session_id, node_key, exc)
        return None
