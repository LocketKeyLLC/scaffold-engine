"""§17.1186 — Auto mode's hands-on steps go through the supervised write channel.

§17.1183 taught the executor which steps change a machine; §17.1184 taught it
to stop and ask; §17.1185 gave it a channel that can carry out a block the
operator approved. This module joins them: when the run reaches a hands-on
step and a runner with an open write channel is registered, the executor no
longer writes a runbook and calls the step done. It drafts the runbook, gates
its commands, and parks the job in ``awaiting_decision`` with ``kind="run"`` —
the exact commands, the verify commands, and three choices:

* **run** — the engine runs the block through the runner (the decide call is
  the operator's approval; every command is signed as in §17.1185), then the
  runbook's own verify commands read-only, and the node's output is the
  runbook plus what actually happened; a failed command fails the node with
  the reason, as any other execution failure would;
* **myself** — the runbook is the node's output, marked plan-only (today's
  behaviour), for the operator to carry out by hand;
* **skip** — the node is skipped.

The hands-on gate (§17.624/1183) stops parking a plan for /assist when the
channel is open: hands-on steps are executable — one approval each.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger("scaffold")

KIND = "run"
CHOICES = ("run", "myself", "skip")
MAX_RUN_COMMANDS = 24
MAX_VERIFY_COMMANDS = 8


def enabled() -> bool:
    return bool(settings.execution_supervised_runs_enabled)


async def channel(db: AsyncSession) -> Optional[tuple[Any, dict]]:
    """``(runner spec, write policy)`` when a runner with an open write
    channel is registered and the valve is on; else None. Uses the cached
    policy (§17.1185) — a run must not cost a runner round-trip per step."""
    if not enabled():
        return None
    try:
        from app.modules import assist_local_runner as _lr
        from app.modules import assist_supervised as _sw
        spec = await _lr.runner_spec(db)
        if spec is None or not settings.mcp_tool_enabled:
            return None
        pol = await _sw.write_policy(spec)
    except Exception as exc:
        logger.warning("supervised_runs_channel_failed err=%r", exc)
        return None
    return (spec, pol) if pol else None


async def pending_hands_on(db: AsyncSession, job_id: str) -> Optional[dict]:
    """The first dep-satisfied pending node that does host work (§17.1183)
    and has no recorded decision — the step the run would claim next."""
    from app.modules.step_classify import step_is_hands_on
    decided = _as_dict((await db.execute(
        text("SELECT metadata->'decisions' FROM jobs WHERE id = :jid"), {"jid": job_id})).scalar())
    rows = (await db.execute(
        text("""
            SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on, n.tool, n.node_type,
                   n.retry_count, n.last_verification_reason
            FROM dag_nodes n
            WHERE n.job_id = :jid AND n.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM unnest(COALESCE(n.depends_on, ARRAY[]::text[])) AS dep(k)
                  WHERE NOT EXISTS (
                      SELECT 1 FROM dag_nodes d
                      WHERE d.job_id = :jid AND d.node_key = dep.k
                        AND d.status IN ('done', 'skipped')
                  )
              )
            ORDER BY n.execution_order ASC
        """),
        {"jid": job_id},
    )).mappings().all()
    for r in rows:
        node = dict(r)
        if node["node_key"] in decided:
            continue
        if str(node.get("node_type") or "") == "decision":
            continue
        on, why = step_is_hands_on(node)
        if on and not why.startswith("tool:human"):
            node["hands_on_reason"] = why
            return node
    return None


def is_hands_on_node(node: dict) -> bool:
    from app.modules.step_classify import step_is_hands_on
    on, why = step_is_hands_on(dict(node) if hasattr(node, "keys") else {})
    return bool(on) and not why.startswith("tool:human")


# ── the runbook and its commands ────────────────────────────────────────

async def draft_runbook(node: dict, brief: dict | str, upstream: str = "") -> str:
    """The same runbook the executor would have written (its prompt and
    system), so the operator approves what Auto mode would have handed them."""
    from app import model_router
    from app.modules.prompt_assembly import EXECUTION_SYSTEM_RUNBOOK, build_base_prompt
    b = brief if isinstance(brief, dict) else {"description": str(brief or "")}
    prompt = build_base_prompt(node, b)
    if upstream:
        prompt = f"{prompt}\n\n{upstream}"
    resp = await model_router.generate(prompt, role="model_general", system=EXECUTION_SYSTEM_RUNBOOK,
                                       temperature=0.2, max_tokens=3000)
    return (getattr(resp, "text", "") or "").strip()


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.S)


def _section(text_out: str, name: str) -> str:
    """The body of the ``## <name>`` section (case-insensitive prefix match)."""
    heads = list(_SECTION_RE.finditer(text_out or ""))
    for i, h in enumerate(heads):
        if h.group(1).lower().startswith(name.lower()):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text_out)
            return text_out[h.end():end]
    return ""


def runbook_commands(text_out: str) -> list[str]:
    """The commands under ``## Run this`` (every fence there, in order);
    when the runbook has no such section, every fence in it."""
    from app.modules.assist_supervised import block_commands
    body = _section(text_out, "Run this") or (text_out or "")
    out: list[str] = []
    for fence in _FENCE_RE.findall(body):
        out.extend(block_commands(fence))
    return out[:MAX_RUN_COMMANDS]


def verify_commands(text_out: str) -> list[str]:
    """The read-only checks under ``## Verify`` — fenced lines and inline
    `…` literals that parse as commands."""
    from app.modules.assist_state_check import read_only_command
    from app.modules.assist_supervised import block_commands
    body = _section(text_out, "Verify")
    cands: list[str] = []
    for fence in _FENCE_RE.findall(body):
        cands.extend(block_commands(fence))
    prose = _FENCE_RE.sub(" ", body)
    cands.extend(m.group(1).strip() for m in re.finditer(r"`([^`\n]{2,200})`", prose))
    out: list[str] = []
    for c in cands:
        # §17.1187 — a <PLACEHOLDER> reads as a redirect to the shell gate; judge
        # the shape with a dummy value, keep the raw command for the operator.
        shape = _PLACEHOLDER_RE.sub("x", c)
        if c not in out and not c.startswith("/") and read_only_command(shape):
            out.append(c)
    return out[:MAX_VERIFY_COMMANDS]


# ── inputs the runbook needs (§17.1187) ──────────────────────────────────
# The runbook prompt's placeholder-first rule (§17.361) puts every operator-
# supplied value into the commands as <SCREAMING_SNAKE_CASE>. A command with a
# placeholder cannot run as written; the pause asks for the values inline.

_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9_]{1,40})>")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,~-]{1,200}$")
_SECRET_NAME_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL", re.I)


def placeholders(commands: list[str]) -> list[str]:
    """The distinct ``<NAME>`` tokens in the commands, in first-seen order."""
    out: list[str] = []
    for c in commands:
        for m in _PLACEHOLDER_RE.finditer(c or ""):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def input_hints(runbook: str) -> dict[str, str]:
    """``{NAME: what the runbook says about it}`` from ``## Inputs needed``."""
    body = _section(runbook, "Inputs needed")
    hints: dict[str, str] = {}
    for ln in (body or "").splitlines():
        ln = ln.strip().lstrip("-*• ").strip()
        if not ln:
            continue
        m = _PLACEHOLDER_RE.search(ln)
        if m:
            rest = (ln[:m.start()] + ln[m.end():]).strip(" :—–-`*")
            hints.setdefault(m.group(1), rest[:200])
    return hints


def inputs_for(commands: list[str], verify: list[str], runbook: str) -> list[dict]:
    """``[{name, hint, secret}]`` — the values the operator must supply."""
    hints = input_hints(runbook)
    return [{"name": n, "hint": hints.get(n, ""), "secret": bool(_SECRET_NAME_RE.search(n))}
            for n in placeholders(list(commands) + list(verify))]


def check_inputs(names: list[str], values: dict | None) -> tuple[dict[str, str], list[dict]]:
    """``(clean values, problems)`` — every name present and shell-safe (no
    whitespace, quotes, or shell metacharacters; the value is spliced into a
    command verbatim, so the gate must be able to read it as one token)."""
    values = values or {}
    clean: dict[str, str] = {}
    problems: list[dict] = []
    for n in names:
        v = str(values.get(n, "") if isinstance(values, dict) else "").strip()
        if not v:
            problems.append({"name": n, "why": "missing"})
        elif not _SAFE_VALUE_RE.match(v):
            problems.append({"name": n, "why": "letters, digits and . / : @ % + = , ~ - _ only — no spaces, quotes or shell characters"})
        else:
            clean[n] = v
    return clean, problems


def substitute(commands: list[str], values: dict[str, str]) -> list[str]:
    return [_PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), c or "") for c in commands]


def mask_secrets(text_out: str, values: dict[str, str], names: list[dict]) -> str:
    """A secret value never lands in the node's output or the transcript."""
    for i in names:
        if i.get("secret") and values.get(i["name"]):
            text_out = text_out.replace(values[i["name"]], "***")
    return text_out


def frame_run(node: dict, runbook: str, spec, policy: dict) -> dict:
    """The ``awaiting_decision`` frame for a hands-on step: what would run,
    what would verify, what the gate refused (then ``run`` is not offered)."""
    from app.modules.assist_supervised import gate_block
    cmds = runbook_commands(runbook)
    verify = verify_commands(runbook)
    inputs = inputs_for(cmds, verify, runbook)
    # §17.1187 — with placeholders the SHAPE is gated now (dummy values in
    # place); the real commands are gated again at resolve, once the operator
    # has supplied the values.
    shape = substitute(cmds, {i["name"]: "x" for i in inputs}) if inputs else cmds
    runnable, refused = gate_block(shape, policy.get("allow") or [])
    runner = getattr(spec, "name", "the runner") or "the runner"
    options = []
    if cmds and not refused:
        options.append({"id": "run", "label": f"Run it through {runner}",
                        "fit": f"the engine runs these {len(cmds)} command{'s' if len(cmds) != 1 else ''} on the machine, then the checks",
                        "tradeoff": "they change the machine; a failure stops the block and fails the step"})
    options.append({"id": "myself", "label": "I'll do it myself",
                    "fit": "keep the runbook as this step's output and carry it out by hand",
                    "tradeoff": "the step counts as a plan, not as executed"})
    options.append({"id": "skip", "label": "Skip this step", "fit": "not needed on this machine",
                    "tradeoff": "steps that depend on it may not make sense"})
    q = ((f"Run step {node.get('node_key')} — {node.get('title') or ''} — on {runner}?"
          + (f" It needs {len(inputs)} value{'s' if len(inputs) != 1 else ''} from you first." if inputs else "")) if cmds and not refused
         else f"Step {node.get('node_key')} — {node.get('title') or ''} — changes a machine, and the engine cannot run it as written.")
    return {
        "kind": KIND, "question": q, "detail": "", "framed": True,
        "options": options, "suggested": "run" if cmds and not refused else "myself",
        "why": (f"{runner} has these commands on its allow-list, so the engine can carry them out and check the result"
                if cmds and not refused else
                ("no runnable command was found in the runbook" if not cmds else
                 "some of its commands are not on the runner's allow-list — see the refusals")),
        "runner": runner, "commands": cmds, "verify": verify, "refused": refused,
        "inputs": inputs,
        "runbook": runbook[:12000], "allow": list(policy.get("allow") or []), "sudo": bool(policy.get("sudo")),
        "hands_on_reason": node.get("hands_on_reason") or "",
    }


# ── resolution ───────────────────────────────────────────────────────────

async def resolve_run(db: AsyncSession, job_id: str, node_key: str, choice: str, waiting: dict,
                      inputs: dict | None = None) -> dict:
    """Carry out the operator's choice on a ``kind="run"`` pause. Returns
    ``{"outcome": …}`` for ``decision_pause.resolve_decision`` to finish (it
    records the decision on the job and moves it back to executing)."""
    choice = (choice or "").strip().lower()
    if choice not in CHOICES:
        return {"outcome": "bad_choice", "choices": list(CHOICES)}
    runbook = str(waiting.get("runbook") or "")
    if choice == "skip":
        upd = await db.execute(
            text("UPDATE dag_nodes SET status = 'skipped', output_text = COALESCE(output_text, :out), completed_at = NOW(), "
                 "updated_at = NOW() WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": node_key, "out": "Skipped by the operator at the supervised-run pause."})
        return {"outcome": "skipped" if upd.rowcount else "node_gone", "node_status": "skipped"}
    if choice == "myself":
        out = runbook or "(runbook)"
        upd = await db.execute(
            text("UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), "
                 "started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
                 "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": node_key, "out": out})
        return {"outcome": "runbook" if upd.rowcount else "node_gone", "node_status": "done"}
    # choice == "run"
    if waiting.get("refused") or not waiting.get("commands"):
        return {"outcome": "not_runnable", "refused": waiting.get("refused") or []}
    commands = [str(c) for c in waiting.get("commands") or []]
    verify_cmds = [str(c) for c in waiting.get("verify") or []]
    need = list(waiting.get("inputs") or [])
    values: dict[str, str] = {}
    if need:                                      # §17.1187 — the values the runbook asked for, checked, spliced in
        values, problems = check_inputs([i["name"] for i in need], inputs)
        if problems:
            return {"outcome": "inputs_missing", "problems": problems, "inputs": need}
        commands = substitute(commands, values)
        verify_cmds = substitute(verify_cmds, values)
    ch = await channel(db)
    if ch is None:
        return {"outcome": "no_channel"}
    spec, policy = ch
    from app.modules import assist_supervised as _sw
    from app.modules import assist_local_runner as _lr
    runnable, refused = _sw.gate_block(commands, policy.get("allow") or [])
    if refused:                                   # the policy changed since the frame was drawn, or a value broke the shape
        return {"outcome": "not_runnable", "refused": [{"command": mask_secrets(r["command"], values, need), "why": r["why"]} for r in refused]}
    claimed = await db.execute(
        text("UPDATE dag_nodes SET status = 'running', started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
             "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
        {"jid": job_id, "nk": node_key})
    if not claimed.rowcount:
        return {"outcome": "node_gone"}
    await db.commit()
    logger.warning("supervised_run_started job=%s node=%s runner=%s commands=%d", job_id, node_key, spec.name, len(runnable))
    executed = await _sw.run_block(spec, runnable)
    ok = bool(executed) and all(e["ok"] for e in executed) and len(executed) == len(runnable)
    verify_out = ""
    if ok and verify_cmds:
        try:
            pasted, ran = await _lr.run_probes(spec, [{"id": f"V{i}", "command": c} for i, c in enumerate(verify_cmds, 1)])
            verify_out = "\n".join(
                f"$ {e['command']}\n" + (re.search(rf"== {e['id']} ==\n(.*?)(?=\n== V\d+ ==|\Z)", pasted, re.S).group(1).rstrip()
                                         if re.search(rf"== {e['id']} ==", pasted) else "(no output)")
                for e in ran)
        except Exception as exc:
            verify_out = f"(verify could not run: {exc})"
    output = mask_secrets(_executed_report(runbook, spec.name, executed, verify_out), values, need)
    if ok:
        await db.execute(
            text("UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), updated_at = NOW(), "
                 "last_verification_reason = :why WHERE job_id = :jid AND node_key = :nk AND status = 'running'"),
            {"jid": job_id, "nk": node_key, "out": output,
             "why": f"supervised run through {spec.name}: {len(executed)} command(s) ran, all exited 0"})
        logger.warning("supervised_run_done job=%s node=%s commands=%d", job_id, node_key, len(executed))
        return {"outcome": "ran", "node_status": "done", "executed": executed, "verify": verify_out}
    last = executed[-1] if executed else None
    reason = mask_secrets("the runner ran nothing" if not last else
                          (f"the runner refused `{last['command'][:80]}`: {last['output'][:200]}" if last.get("refused")
                           else f"`{last['command'][:80]}` exited {last['exit']}: {last['output'][-300:]}"), values, need)
    await db.execute(
        text("UPDATE dag_nodes SET status = 'failed', output_text = :out, completed_at = NOW(), updated_at = NOW(), "
             "last_verification_reason = :why WHERE job_id = :jid AND node_key = :nk AND status = 'running'"),
        {"jid": job_id, "nk": node_key, "out": output, "why": f"supervised run stopped — {reason}"[:1000]})
    logger.warning("supervised_run_failed job=%s node=%s reason=%s", job_id, node_key, reason[:200])
    return {"outcome": "failed", "node_status": "failed", "executed": executed, "reason": reason}


def _executed_report(runbook: str, runner: str, executed: list[dict], verify_out: str) -> str:
    lines = [runbook.rstrip(), "", f"## Executed on {runner} (supervised, operator-approved)"]
    for e in executed:
        rc = f"\nrc={e['exit']}" if e["exit"] not in (None, 0) else ""
        lines.append(f"$ {e['command']}\n{e['output'] or '(no output)'}{rc}")
    if verify_out:
        lines += ["", "## Verify results", verify_out]
    return "\n".join(lines)


def _as_dict(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return dict(v) if isinstance(v, dict) else {}
