"""§17.1038 — plan-level value provenance.

The evidence layer (§17.1027–1037, ``assist_evidence``) checks every value an
ASSIST answer states against what the operator gave the engine. Its plan-only
tier (§17.1034) exists because the PLAN itself was never checked: the planner
is handed the brief and writes tasks that carry addresses, ports and versions
of its own — ``192.168.1.1``, ``8211`` — which then sit in the task text that
every downstream prompt (executor, walkthrough, fix, research) treats as
project context. A value the model invented at planning time was laundered
into provenance one step later.

This pass runs after the DAG is generated and before it is persisted. Every
concrete value in a task's name, notes, inputs and outputs is traced to the
plan's own provenance — the refined brief, the operator's original request and
the confirm-phase research record. A value none of them states is ASSUMED: it
is kept (a plan may legitimately propose a default), but the task's notes say
so in the operator's face and in every downstream prompt, and the task carries
``assumed_values`` so consumers can treat them as the plan-only tier they are.

No model call: the check is the same deterministic extraction the assist paths
use, so the two ends of the pipeline cannot disagree about what a value is.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.config import settings
from app.modules.assist_evidence import owned_hosts, unsupported_specifics

logger = logging.getLogger("scaffold")

ASSUMED_NOTE_PREFIX = "Assumed (not in the brief or research)"
_ASSUMED_LINE_RE = re.compile(r"^\s*" + re.escape(ASSUMED_NOTE_PREFIX) + r".*$", re.MULTILINE)


def _flat(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def plan_corpus(brief_data: Any, input_text: Optional[str] = None,
                research_data: Any = None) -> str:
    """What the plan may legitimately state a value from: the refined brief
    (the operator's approved statement of the project, with their confirm-phase
    answers folded in), their original request, and the research record the
    confirm phase stored. NOT the few-shot exemplars — those are other
    projects' solutions, and a value copied from one is exactly the class to mark."""
    return "\n".join(p for p in (_flat(brief_data), _flat(input_text), _flat(research_data)) if p)


def task_text(task: dict) -> str:
    """The task's own words, minus any note this pass wrote earlier."""
    parts = [str(task.get("name") or ""), _strip_assumed_note(str(task.get("notes") or ""))]
    for key in ("inputs", "outputs"):
        v = task.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(p for p in parts if p)


def _strip_assumed_note(notes: str) -> str:
    return _ASSUMED_LINE_RE.sub("", notes or "").rstrip()


def assumed_note(values: list[dict]) -> str:
    return (
        f"{ASSUMED_NOTE_PREFIX}: "
        + ", ".join(f"`{u['value']}` ({u['kind']})" for u in values[:8])
        + " — confirm on the operator's system before relying on it; a value the "
          "operator reports replaces it."
    )


def mark_assumed_values(tasks: list[dict], corpus: str, *, job_id: str = "?") -> list[dict]:
    """Mutate ``tasks`` in place: each task whose text states a value the
    ``corpus`` never mentions gets ``assumed_values`` and an explicit note in
    its ``notes`` (the text that becomes the node's prompt template). Returns
    ``[{id, values}]`` for the tasks marked. Idempotent — an earlier note is
    replaced, never stacked, and the note's own values are not re-counted."""
    if not settings.dag_value_provenance_enabled:
        return []
    owned = owned_hosts({"_brief_text": corpus})
    marked: list[dict] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        clean_notes = _strip_assumed_note(str(task.get("notes") or ""))
        uns = unsupported_specifics(task_text(task), corpus, owned=owned)
        if not uns:
            task.pop("assumed_values", None)
            if task.get("notes") is not None:
                task["notes"] = clean_notes
            continue
        task["assumed_values"] = uns
        task["notes"] = (clean_notes + "\n\n" if clean_notes else "") + assumed_note(uns)
        marked.append({"id": task.get("id"), "values": uns})
    if marked:
        logger.warning(
            "dag_values_assumed job=%s tasks=%d values=%r", job_id, len(marked),
            [(m["id"], [u["value"] for u in m["values"]][:4]) for m in marked][:8],
        )
    else:
        logger.info("dag_values_provenance_clean job=%s tasks=%d", job_id, len(tasks))
    return marked
