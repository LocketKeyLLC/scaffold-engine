"""§17.1611 — a step is drafted with what earlier steps of its job READ on the same guest.

Live (job 0fb4ea7f, 2026-10-15): T2 "Verify ai-vm resources" measured the root filesystem on `/dev/sda2`; T5 "Resize
ai-vm filesystem" depends only on T3 (the host-side disk grow), so its drafter saw T3's output and nothing of T2's, and
wrote `growpart /dev/sda 1; resize2fs /dev/sda1` -- a guess, on the wrong partition. The measurement was in the job's
own record. A plan's edges say what must run FIRST; they do not say what the drafter may know.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")

#: per earlier step, the tail of its record (the reads are at the end) and how many steps
PER_STEP_CHARS = 2500
MAX_STEPS = 4


def guest_of(node: dict, inventory: Optional[dict]) -> str:
    from app.modules.machine_truth import subject_guest
    try:
        return str(subject_guest(node, inventory) or "")
    except Exception:
        return ""


def block(rows: list[dict], gid: str, skip: set[str]) -> str:
    """The evidence block from done rows of the same job whose subject is `gid` (newest last), minus `skip`."""
    picked = [r for r in rows if str(r.get("node_key")) not in skip and r.get("output_text")][-MAX_STEPS:]
    if not gid or not picked:
        return ""
    parts = [f"## What earlier steps of this job measured on guest {gid}",
             "Read off the machine by finished steps (not this step's upstream). Use these values; do not guess them."]
    # §17.1611c — newest first: the drafter's context is capped (`plan_context`), and the most recent step on the guest
    # is the most likely to say how things now stand there (live, T9's record -- Ollama in Docker -- was the one cut)
    for r in reversed(picked):
        out = str(r["output_text"])
        # §17.1611b — what the step RAN is at the head of its record, what it SAW at the tail; both are evidence.
        # Live (T11 "Pull selected models"): T9's tail said "Testing ollama list…" while its head held
        # `docker exec ollama …` -- the drafter, given only the tail, ran `ollama pull` on the VM, where no ollama is.
        if len(out) <= PER_STEP_CHARS:
            tail = out
        else:
            head_n = PER_STEP_CHARS * 2 // 5
            tail = out[:head_n] + "\n[… middle of the record left out …]\n" + out[-(PER_STEP_CHARS - head_n):]
        # the records hold their own ``` fences (runbooks, outputs): a tilde fence cannot be closed by them
        parts.append(f"### {r['node_key']} — {r.get('title') or ''}\n~~~~\n{tail.replace('~~~~', '~ ~ ~ ~')}\n~~~~")
    return "\n\n".join(parts)


async def earlier_reads(db, job_id: str, run_node: dict, inventory: Optional[dict]) -> str:
    gid = guest_of(run_node, inventory)
    if not gid:
        return ""
    res = await db.execute(text(
        "SELECT node_key, title, description, prompt_template, output_text FROM dag_nodes "
        "WHERE job_id = :j AND status = 'done' AND output_text IS NOT NULL ORDER BY completed_at NULLS LAST"),
        {"j": job_id})
    rows = [dict(r) for r in res.mappings().all()]
    same = [r for r in rows if guest_of(r, inventory) == gid]
    skip = {str(k) for k in (run_node.get("depends_on") or [])} | {str(run_node.get("node_key"))}
    return block(same, gid, skip)


#: §17.1614 — `docker exec [-it] <container> <tool>`: a tool the job runs only inside a container
_DOCKER_EXEC_RE = re.compile(r"\bdocker\s+exec\s+(?:-[a-zA-Z]+\s+)*([\w.-]+)\s+([a-z][\w.-]*)")


def tools_only_in_containers(upstream: str, bodies: list[str]) -> list[dict]:
    """§17.1614 — a block calls `<tool>` bare while the job's own records run it only as `docker exec <ctr> <tool>`.

    Live (job 0fb4ea7f T11 "Pull selected models", three drafts): T9 had put Ollama in Docker (`docker exec ollama
    ollama list`); the step text said "ollama pull", and the draft ran `ollama pull …` on the VM -- no such command
    there. The evidence was in the prompt (§17.1611); the step's wording won. A tool the block itself installs, or
    one the records also run bare, is not judged."""
    seen: dict[str, str] = {}
    for m in _DOCKER_EXEC_RE.finditer(upstream or ""):
        seen.setdefault(m.group(2), m.group(1))
    if not seen:
        return []
    bare_elsewhere = set()
    for ln in (upstream or "").splitlines():
        w = ln.strip().split()
        if w and w[0] in seen and "docker" not in ln:
            bare_elsewhere.add(w[0])
    out: list[dict] = []
    for body in bodies:
        text_all = str(body or "")
        for ln in text_all.splitlines():
            w = ln.strip().split()
            if not w or w[0] not in seen or w[0] in bare_elsewhere:
                continue
            tool = w[0]
            if any(re.search(r"\binstall\b|\|\s*(?:sudo\s+)?(?:ba)?sh\b", x) and re.search(rf"\b{re.escape(tool)}\b", x)
                   for x in text_all.splitlines()):
                continue                       # the block installs it on the guest itself
            ctr = seen[tool]
            out.append({"command": ln.strip()[:200], "why": (
                f"`{tool}` runs only inside the container `{ctr}` on this guest -- this job's own record ran it as "
                f"`docker exec {ctr} {tool} …` -- so a bare `{tool}` on the guest is a command that is not there. "
                f"Run it the way the job does: `docker exec {ctr} {' '.join(w)[:120]}`.")})
            break
    return out
