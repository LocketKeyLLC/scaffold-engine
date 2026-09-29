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
        if pol:
            # §17.1193 — the names the ENGINE holds for this machine, alongside
            # the ones in the runner's own file. Names only; the values are read
            # at run time and sent out of band. Its OWN try: this is an extra,
            # and a missing table (the migration has not run yet) must not make
            # the whole write channel disappear — that would turn a schema lag
            # into "the engine silently stopped being able to run anything".
            try:
                from app.modules import runner_secrets as _rs
                pol = {**pol, "held": await _rs.names(db)}
            except Exception as exc:
                logger.warning("runner_secret_names_unavailable err=%r", exc)
                pol = {**pol, "held": []}
    except Exception as exc:
        logger.warning("supervised_runs_channel_failed err=%r", exc)
        return None
    return (spec, pol) if pol else None


_NOT_ALLOWED = "not on the write-allow list"


_SHAPE_REFUSALS = ("substitution/heredoc", "redirect", "empty")

# §17.1198 — the same signatures the runner's own privilege note reads, so both
# ends agree on "this failed because it could not read, not because the machine
# is broken". Proxmox is the reason for the last two: an unprivileged user
# talking to /etc/pve gets `ipcc_send_rec … failed` and "Unable to load access
# control list", never the word "permission".
_NEEDS_ROOT_RE = re.compile(
    r"(?i)\b(?:permission denied|operation not permitted|are you root|must be (?:run as |the )?root|"
    r"requires? root|need(?:s|ed)? to be root|insufficient privileges|not authorized|EACCES)\b"
    r"|ipcc_send_rec\[\d+\] failed|Unable to load access control list")


async def record_needs_root(db: AsyncSession, job_id: str, commands: list[str]) -> list[str]:
    """Remember the READ commands that failed for want of root.

    The evidence-based twin of `record_wanted_prefixes` (§17.1194): rather than
    guess which of a plan's read-only checks need privilege on this particular
    host, record the ones that actually came back unable to read, and let the
    connection page offer exactly those for the runner's ``--sudo-allow`` list.
    """
    wanted: list[str] = []
    for c in commands:
        p = prefix_for(str(c or ""))
        if p and p not in wanted:
            wanted.append(p)
    if not wanted:
        return []
    await db.execute(text("""
        UPDATE jobs SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{needs_root_prefixes}',
               COALESCE(metadata->'needs_root_prefixes', '[]'::jsonb) || CAST(:add AS jsonb), true)
         WHERE id = :jid
    """), {"jid": job_id, "add": json.dumps(wanted)})
    await db.commit()
    logger.warning("needs_root_recorded job=%s prefixes=%s", job_id, ",".join(wanted))
    return wanted


def shape_retry_note(frame: dict) -> str:
    """§17.1196 — the correction to feed back when the engine's own gate refused
    the engine's own block for its SHAPE, or ``""`` when there is nothing to fix.

    A shape refusal is not a decision for the operator: `for i in $(seq 1 12)`
    is a form the channel cannot carry however much is allowed, so parking on it
    hands them a greyed-out button and no way forward. A PERMISSION refusal is
    different — that one is genuinely theirs — so a frame carrying one is left
    alone; redrafting around a permission is the engine talking itself out of
    asking. Likewise a denylist refusal, which must never be redrafted around.
    """
    from app.modules.assist_supervised import catastrophic
    shapes = [r for r in (frame or {}).get("refused") or []
              if any(s in str(r.get("why") or "") for s in _SHAPE_REFUSALS)]
    if not shapes:
        return ""
    if any(catastrophic(str(r.get("command") or "")) for r in (frame or {}).get("refused") or []):
        return ""
    if any(str(r.get("why") or "").startswith(_NOT_ALLOWED) for r in (frame or {}).get("refused") or []):
        return ""
    lines = "\n".join(f"- `{str(r.get('command') or '')[:160]}` — {r.get('why')}" for r in shapes)
    return (
        "YOUR PREVIOUS DRAFT WAS REFUSED BY THE RUNNER'S GATE — rewrite it so every command can run.\n"
        f"{lines}\n"
        "Rules that were broken, restated: each command runs alone, in its own shell. No `$(…)` or backticks "
        "ANYWHERE — including to build a list for a loop; write the list out literally (`for i in 1 2 3; do …; "
        "done`) or, better, drop the loop and state the checks as separate commands. No heredoc: write a file with "
        "`printf '%s\\n' 'line' | tee /path`. No `>`/`>>` redirects. A retry/wait loop is rarely worth it here — "
        "the operator sees the result of each command, so a single check is usually enough."
    )


def frame_is_stale(frame: dict, policy: dict) -> bool:
    """§17.1197 — was this pause drafted under a different permission set?

    A parked question is a snapshot. The operator then goes and does the very
    thing it asked for — widens the allow-list, installs the root grant — and
    comes back to the same greyed-out button, because nothing re-reads the
    policy for a question already on screen. Comparing the allow-list the frame
    was drawn with against the runner's current one says so cheaply.
    """
    if not frame or frame.get("kind") != KIND:
        return False
    was = {str(a) for a in (frame.get("allow") or [])}
    now = {str(a) for a in ((policy or {}).get("allow") or [])}
    return was != now or bool(frame.get("sudo")) != bool((policy or {}).get("sudo"))


def wanted_prefixes_in(frame: dict) -> list[str]:
    """The prefixes a pause frame was refused for, in first-seen order.

    Only PERMISSION refusals: a `substitution/heredoc` is a shape the channel
    cannot carry whatever is allowed, and a denylist refusal is never granted.
    Pure, so the same reading serves the moment of the refusal (recorded on the
    job) and the pause an operator is looking at now (§17.1195)."""
    from app.modules.assist_supervised import _SUDO_RE
    wanted: list[str] = []
    for r in (frame or {}).get("refused") or []:
        why = str((r or {}).get("why") or "")
        if not why.startswith(_NOT_ALLOWED):
            continue
        bare = _SUDO_RE.sub("", why[len(_NOT_ALLOWED):].lstrip(": ").strip(), count=1)
        for p in prefixes_for_command(bare)[0] or [prefix_for(bare)]:
            if p and p not in wanted:
                wanted.append(p)
    return wanted


async def record_wanted_prefixes(db: AsyncSession, job_id: str, frame: dict) -> list[str]:
    """§17.1194 — remember the prefixes a real step was refused for.

    The allow-list the engine recommends (§17.1189) is read off the PLAN's own
    words. What actually runs is the DRAFTED runbook, and it reaches for more:
    live, step ADD65's plan text said `qm agent 106 ping` (a check) while its
    runbook needed `qm start 106` to get there — so the run stopped on a prefix
    the recommendation could not have known about. Guessing the superset up
    front would mean recommending permissions for commands that may never run;
    asking at the moment one is refused is what a phone does with a camera.

    Stored on the job, so the connection page can offer them next to the ones
    the plan named. Names only — this is a permission request, not a command.
    """
    wanted = wanted_prefixes_in(frame)
    if not wanted:
        return []
    await db.execute(text("""
        UPDATE jobs SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{wanted_prefixes}',
               COALESCE(metadata->'wanted_prefixes', '[]'::jsonb) || CAST(:add AS jsonb), true)
         WHERE id = :jid
    """), {"jid": job_id, "add": json.dumps(wanted)})
    await db.commit()
    logger.warning("wanted_prefixes_recorded job=%s node=%s prefixes=%s",
                   job_id, frame.get("node_key"), ",".join(wanted))
    return wanted


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


# ── what the channel would need to be open for THIS plan (§17.1189) ──────
# The `runner_writes` recipe asks the operator to "list the prefixes this
# plan's steps need" and then offers a generic Proxmox example. The engine
# already knows: the plan's own steps carry the commands, `step_classify`
# decides which ones write, and the gate matches on whole-token prefixes. So
# the list is derivable — deterministically, with no model draw — and the
# operator reads their own plan back instead of guessing.

_PLACEHOLDER_TOKEN = re.compile(r"^<[A-Z][A-Z0-9_]{1,40}>$")
_FILE_WRITERS = frozenset({"tee", "install", "cp", "mv", "touch", "mkdir", "chmod", "chown", "ln", "dd"})


def prefix_for(command: str) -> str:
    """The narrowest ``--write-allow`` entry that would let this command run.

    Head plus subcommand for the two-word mutation forms the plan is full of
    (``qm set``, ``pct set``, ``pvesm free``); head plus the target DIRECTORY,
    slash-terminated, for a command whose argument is the thing it writes
    (``tee -a /etc/caddy/Caddyfile`` → ``tee -a /etc/caddy/``) — a bare ``tee``
    would allow writing anywhere. ``''`` when nothing narrower than the head
    can be read off it."""
    from app.modules.assist_supervised import _SUDO_RE
    bare = _SUDO_RE.sub("", (command or "").strip(), count=1).strip()
    toks = bare.split()
    if not toks:
        return ""
    head = toks[0]
    if head in _FILE_WRITERS:
        # The target is the first ABSOLUTE path, not the first non-flag token:
        # `chmod +x /tmp/x.run` carries a mode there, `chmod 700 /root/.ssh` a
        # number. Everything before it (flags, mode) stays in the prefix.
        target = next((t for t in toks[1:] if t.startswith("/")), "")
        if target and "/" in target.rstrip("/")[1:]:
            lead = toks[1:toks.index(target)]
            return " ".join([head, *lead, target.rsplit("/", 1)[0] + "/"])
        # No absolute target to bound it: a bare `tee -a` on the allow-list
        # would let the engine write ANY file, which is not a prefix — it is
        # the whole machine. Recommend nothing and let the step be the
        # operator's to run (they can still allow it by hand if they mean to).
        return ""
    nxt = toks[1] if len(toks) > 1 else ""
    if nxt and not nxt.startswith("-") and not _PLACEHOLDER_TOKEN.match(nxt) and "/" not in nxt and not nxt.isdigit():
        return f"{head} {nxt}"
    return head


def prefixes_for_command(command: str) -> tuple[list[str], list[str]]:
    """``(prefixes, never_allowed_reasons)`` for one command.

    A command is judged by the gate PER SEGMENT (`write_allowed` splits on
    ``&&``, ``||``, ``;``, ``|``), and the runbook prompt asks for idempotent
    one-liners — so ``pct status 111 | grep -q running || pct start 111`` needs
    ``pct start`` allowed, not ``pct status``. Deriving one prefix from the
    whole string is the mistake that made a measurement of this read 5/21
    instead of 12/21; the segment split is the same one the gate uses."""
    from app.modules.assist_supervised import catastrophic, read_only, split_segments
    from app.modules.step_classify import command_writes
    prefixes: list[str] = []
    never: list[str] = []
    for seg in split_segments(command or ""):
        seg = seg.strip()
        if not seg or read_only(seg)[0] or not command_writes(seg):
            continue
        why = catastrophic(seg)
        if why:
            if why not in never:
                never.append(why)
            continue
        p = prefix_for(seg)
        if p and p not in prefixes:
            prefixes.append(p)
    return prefixes, never


def prefixes_for_nodes(nodes: list[dict]) -> list[dict]:
    """``[{prefix, steps, why}]`` — what these steps would need allowed, most
    widely needed first. Only commands the engine's own write test calls a
    write are counted, and a command on the catastrophic denylist is reported
    separately (no allow-list entry can ever release it)."""
    from app.modules.step_classify import step_commands
    need: dict[str, set] = {}
    refused: dict[str, set] = {}
    for node in nodes:
        key = str((node or {}).get("node_key") or "")
        text_all = "\n".join(str((node or {}).get(f) or "") for f in ("title", "description", "prompt_template"))
        for cmd, _sentence in step_commands(text_all):
            prefixes, never = prefixes_for_command(cmd)
            for p in prefixes:
                need.setdefault(p, set()).add(key)
            for why in never:
                refused.setdefault(why, set()).add(key)
    out = [{"prefix": p, "steps": sorted(ks), "why": f"{len(ks)} step(s) in this plan"}
           for p, ks in sorted(need.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    for why, ks in sorted(refused.items()):
        out.append({"prefix": "", "steps": sorted(ks), "why": f"never allowed: {why}"})
    return out


async def write_prefixes_for_job(db: AsyncSession, job_id: str) -> list[dict]:
    """``prefixes_for_nodes`` over the job's steps that are still to do and
    that change a machine."""
    from app.modules.step_classify import step_is_hands_on
    rows = (await db.execute(
        text("""SELECT node_key, title, description, prompt_template, tool, node_type
                FROM dag_nodes WHERE job_id = :jid AND status IN ('pending', 'running', 'failed')
                ORDER BY execution_order"""),
        {"jid": job_id})).mappings().all()
    todo = [dict(r) for r in rows if step_is_hands_on(dict(r))[0]]
    return prefixes_for_nodes(todo)


# ── the runbook and its commands ────────────────────────────────────────

# §17.1189 — what the supervised channel can actually carry.
#
# The runbook prompt is written for a HUMAN pasting a block into one shell, so
# it reaches for heredocs, `$(…)`, `>` and a `cd` that the next line relies on.
# The channel is not a shell session: `run_block` sends ONE command at a time,
# each to its own `create_subprocess_shell` on the runner, and the write gate
# refuses substitutions and redirects outright (`write_allowed`). Measured over
# the operator's real plan, that mismatch — not policy, not risk — was the
# single largest reason Auto mode could not run a step it had correctly
# identified: 5 of 21 pending hands-on steps carried a heredoc or a `$(…)`,
# and one sequence did `cd /tmp` and then named a file relatively.
#
# So when the engine may run the block, the drafter is told what "runnable"
# means here. The operator-facing runbook (assist guidance) keeps the old
# freedom — a human pasting a block CAN run a heredoc.
CHANNEL_RULES = """
Runnable-by-the-engine rules (this runbook may be carried out FOR the operator, one command at a time):
- Each line under "## Run this" must be ONE self-contained command. They are run in order, each in its OWN shell, so no shell state carries between them: never `cd` and then name a file relatively — write absolute paths (`wget -O /tmp/x.run …`, then `/tmp/x.run`).
- No heredocs (`<<EOF`), no command substitution (`$(…)` or backticks), and no output redirection (`>` / `>>`). Write a file by piping into `tee` with the content as arguments: `printf '%s\n' 'line one' 'line two' | tee /etc/example.conf` (append with `tee -a`). `2>/dev/null` is fine.
- No multi-line shell constructs, and no `if … then … fi` even on one line: each part of a command is judged on its own, and a `then`-prefixed part cannot be read. Write an idempotent step as a guard chain instead — `pct status 111 | grep -q running || pct start 111` — where the check and the fix are each a whole command.
- Avoid loops. A retry/wait loop is rarely worth it here: the operator sees the result of each command, so one check is usually enough. If you truly need one, write the list out literally (`for i in 1 2 3; do …; done`) — NEVER build it with `$(seq …)`, which is command substitution and is refused wherever it appears.
- Under "## Verify", each check stays a read-only command (`pct status 111`, `systemctl is-active …`, `ls -ld …`).
"""


async def draft_runbook(node: dict, brief: dict | str, upstream: str = "", *,
                        for_channel: bool = True, retry_note: str = "") -> str:
    """The same runbook the executor would have written (its prompt and
    system), so the operator approves what Auto mode would have handed them.

    §17.1196 — ``retry_note`` carries the gate's own refusal back into a second
    draft. The rules are in the system prompt; a refusal says which one this
    draft actually broke, which is the difference between a style guide and a
    compiler error."""
    from app import model_router
    from app.modules.prompt_assembly import EXECUTION_SYSTEM_RUNBOOK, build_base_prompt
    b = brief if isinstance(brief, dict) else {"description": str(brief or "")}
    prompt = build_base_prompt(node, b)
    if upstream:
        prompt = f"{prompt}\n\n{upstream}"
    if retry_note:
        prompt = f"{prompt}\n\n{retry_note}"
    # §17.1189 — the sibling this mirrors (the executor's own node generation)
    # routes through the shared empty-guard at node_generation_max_tokens
    # (8192); this call was a bare generate at 3000. On a thinking model
    # num_predict is a SHARED reasoning+content budget and the reasoning trace
    # is discarded on this path (§17.683), so 3000 is structurally starved —
    # §17.1126 measured 1.8-3.8 K reasoning tokens per draw on
    # deepseek-v4-pro. A success+empty draw yields a runbook with no commands,
    # so the frame says "no runnable command was found" and the step falls to
    # "I'll do it myself" with no stated reason. Measured on the real 131-step
    # plan: 1 of 21 pending hands-on steps lost exactly that way.
    from app.config import settings
    from app.utils.llm_retry import generate_until_nonempty
    system = EXECUTION_SYSTEM_RUNBOOK + ("\n" + CHANNEL_RULES if for_channel else "")
    # think=False from the first draw, not only as a rescue: on the generate
    # path model_router reads `response` and DISCARDS `thinking` (§17.683), so
    # the reasoning is pure cost here — §17.1126 measured the same-length
    # answer in 382 tokens / 2.5 s with it off against 14.6 s with it on.
    resp = await generate_until_nonempty(
        model_router.generate, prompt, {"role": "model_general", "think": False},
        system=system, temperature=0.2,
        max_tokens=settings.node_generation_max_tokens,
        draws=settings.node_generation_max_draws, label=f"runbook {node.get('node_key')}",
    )
    out = (getattr(resp, "text", "") or "").strip()
    if not out:
        logger.error("draft_runbook_empty node=%s — every draw came back empty", node.get("node_key"))
    return out


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


_MULTILINE_RE = re.compile(r"<<-?\s*['\"]?\w+|^\s*(?:for|while|until)\s.*\bdo\s*$|^\s*if\s.*\bthen\s*$", re.M)


def runbook_commands(text_out: str) -> list[str]:
    """The commands under ``## Run this`` (every fence there, in order);
    when the runbook has no such section, every fence in it.

    §17.1189 — a fence carrying a HEREDOC or a multi-line ``for``/``if`` block
    is one command spread over many lines, so splitting it per line invents
    commands that never existed: the operator's own plan produced a refusal
    list reading ``WorkingDirectory=x``, ``ExecStart=x``, ``reverse_proxy
    x:x`` — systemd-unit and Caddyfile *body* lines, gated as if they were
    commands. Such a fence is kept whole: the gate then refuses it once, for
    the real reason (``substitution/heredoc``), and the operator reads one
    honest refusal instead of five fictional ones."""
    from app.modules.assist_supervised import block_commands
    body = _section(text_out, "Run this") or (text_out or "")
    out: list[str] = []
    for fence in _FENCE_RE.findall(body):
        if _MULTILINE_RE.search(fence):
            whole = (fence or "").strip()
            if whole:
                out.append(whole)
            continue
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
# §17.1193 — the same shape the runner reads: `$NAME` / `${NAME}`.
_SECRET_REF_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{0,63})\}?")
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


def _quote_spans(cmd: str) -> list[tuple[int, int, str]]:
    """``(start, end, quote)`` for every quoted span, outermost only."""
    spans, i, n = [], 0, len(cmd or "")
    while i < n:
        c = cmd[i]
        if c in "'\"":
            j = cmd.find(c, i + 1)
            if j == -1:
                break
            spans.append((i, j, c))
            i = j + 1
        else:
            i += 1
    return spans


def secret_ref_rewrite(cmd: str, name: str) -> tuple[str, str]:
    """``(command, problem)`` — ``<NAME>`` replaced by a reference the SHELL
    will actually expand.

    A naive ``<NAME>`` → ``$NAME`` is wrong inside single quotes: the shell
    does not expand there, so the command runs with the literal text ``$NAME``
    and *succeeds* with the wrong value. The integration test caught exactly
    that — a runbook wrote ``printf '%s' '<TOKEN>' | tee …`` and the file
    ended up holding ``$TOKEN``.

    * outside quotes, or inside double quotes → ``$NAME``
    * a whole single-quoted token, ``'<NAME>'`` → ``"$NAME"`` (quoted, so a
      value with spaces still arrives as one argument)
    * inside a single-quoted span mixed with other text → refused: rewriting
      the span's quotes could change what the rest of it means.
    """
    ph = f"<{name}>"
    if ph not in (cmd or ""):
        return cmd, ""
    out, i, n = [], 0, len(cmd)
    spans = _quote_spans(cmd)
    while i < n:
        j = cmd.find(ph, i)
        if j == -1:
            out.append(cmd[i:])
            break
        span = next((sp for sp in spans if sp[0] < j and j + len(ph) <= sp[1]), None)
        if span and span[2] == "'":
            if span[0] + 1 == j and span[1] == j + len(ph):      # the whole token
                out.append(cmd[i:span[0]])
                out.append(f'"${name}"')
                i = span[1] + 1
                continue
            return cmd, (f"{ph} sits inside a single-quoted string, where the shell would not expand "
                         f"${name} — the runbook must quote it with double quotes or leave it bare")
        out.append(cmd[i:j])
        out.append(f"${name}")
        i = j + len(ph)
    return "".join(out), ""


def apply_runner_secrets(commands: list[str], verify: list[str], inputs: list[dict],
                         policy: dict) -> tuple[list[str], list[str], list[dict], list[str], list[str]]:
    """§17.1191 — turn secret-named placeholders into runner-resolved refs.

    A `<DB_PASSWORD>` used to be typed by the operator at the pause and spliced
    into the command by the engine. Masking then kept it out of the node output
    and the transcript (§17.1187) — but the substituted command still reached
    the runner, which logs it, and `create_subprocess_shell` put it in the
    target's process table. Masking protected the record, never the run.

    So the VALUE is never carried in the command. The placeholder becomes a
    `$NAME` reference, and the runner expands it as an environment variable at
    execution from whichever store holds it.

    §17.1193 — there are now two such stores, and the operator may simply be
    asked. `policy['secrets']` are names in the runner's own file (values never
    leave that machine); `held` are names the ENGINE keeps, encrypted, from a
    value the operator typed once into a form — sent out of band at run time,
    the way Actions and Ansible do it. §17.1191 refused to hold one at all and
    made the operator edit a file on the target by hand; that was stricter than
    any tool this engine is measured against, and it bought nothing the
    out-of-band delivery does not already buy.

    Returns ``(commands, verify, inputs, resolved, missing)`` — ``inputs`` with
    every ALREADY-KNOWN secret removed (there is nothing to type), ``resolved``
    the names some store will supply, ``missing`` the ones nothing holds yet:
    those stay in ``inputs``, marked, so the pause can ask for them once.
    """
    secret_names = [str(i.get("name") or "") for i in inputs if i.get("secret")]
    if not secret_names:
        return commands, verify, inputs, [], []
    known = {str(n) for n in (policy.get("secrets") or [])} | {str(n) for n in (policy.get("held") or [])}
    resolved = [n for n in secret_names if n in known]
    missing = [n for n in secret_names if n not in known]

    unquotable: list[str] = []

    def _ref(cmds: list[str]) -> list[str]:
        out = []
        for c in cmds:
            for n in list(resolved):
                c, problem = secret_ref_rewrite(c, n)
                if problem and problem not in unquotable:
                    unquotable.append(problem)
                    if n in resolved:
                        resolved.remove(n)
                        missing.append(n)
            out.append(c)
        return out

    # The rewrite runs FIRST: it is what discovers an unquotable placeholder,
    # and `blocked` is read off that. (Computing them the other way round left
    # `unquotable` empty and every name looked askable.)
    cmds_out, verify_out = _ref(commands), _ref(verify)
    # §17.1193 — a secret nothing holds yet is ASKED FOR, once, and the answer
    # is stored encrypted rather than spliced into the command. An unquotable
    # name is the exception: asking would not help, because the runbook put the
    # placeholder where the shell will not expand a reference, so no stored
    # value could ever be delivered there.
    blocked = {p.split()[0].strip("<>") for p in unquotable}
    askable = [n for n in missing if n not in blocked]
    kept = [i for i in inputs if not i.get("secret") or i.get("name") in askable]
    for i in kept:
        if i.get("secret"):
            i["store"] = "engine"          # the form writes it to the engine's own store
            i["kept_encrypted"] = True     # said in the UI: typed once, never shown again
    return cmds_out, verify_out, kept, resolved, [n for n in missing if n in blocked]


def frame_run(node: dict, runbook: str, spec, policy: dict, env: Optional[dict] = None) -> dict:
    """The ``awaiting_decision`` frame for a hands-on step: what would run,
    what would verify, what the gate refused (then ``run`` is not offered)."""
    from app.modules.assist_supervised import gate_block
    cmds = runbook_commands(runbook)
    verify = verify_commands(runbook)
    inputs = inputs_for(cmds, verify, runbook)
    # §17.1191 — a secret is resolved BY THE RUNNER or not at all; it is never
    # typed here and never travels through the engine.
    cmds, verify, inputs, secrets_resolved, secrets_missing = apply_runner_secrets(cmds, verify, inputs, policy)
    if inputs:                                   # §17.1188 — offer what the engine already knows
        from app.modules.runbook_inputs import suggest_inputs
        inputs = suggest_inputs(inputs, env)
    # §17.1187 — with placeholders the SHAPE is gated now (dummy values in
    # place); the real commands are gated again at resolve, once the operator
    # has supplied the values.
    # The SHAPE is what the gate judges (§17.1187): a `<PLACEHOLDER>` reads as a
    # redirect, so every one gets a dummy — including a secret the runner cannot
    # resolve, whose real refusal is stated once below rather than as a fake
    # "redirect" per command.
    _dummies = {i["name"]: "x" for i in inputs}
    _dummies.update({n: "x" for n in secrets_missing})
    shape = substitute(cmds, _dummies) if _dummies else cmds
    runnable, refused = gate_block(shape, policy.get("allow") or [])
    runner = getattr(spec, "name", "the runner") or "the runner"
    options = []
    if secrets_missing:                          # §17.1191 — nothing to type; the value belongs on the runner
        refused = list(refused) + [{
            "command": ", ".join(f"${n}" for n in secrets_missing),
            "why": (f"{runner} holds no value for {', '.join(secrets_missing)} — add it to that machine's "
                    "runner secrets file (Capabilities → \u201cLet the engine use a secret you keep on your "
                    "machine\u201d). The engine never takes a password."),
        }]
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
        "secrets_resolved": secrets_resolved, "secrets_missing": secrets_missing,
        "runbook": runbook[:12000], "allow": list(policy.get("allow") or []), "sudo": bool(policy.get("sudo")),
        "hands_on_reason": node.get("hands_on_reason") or "",
    }


# ── resolution ───────────────────────────────────────────────────────────

#: §17.1202 — what to look at when a command of this shape fails. Read-only,
#: cheap, and chosen because each one turns a GUESS in the diagnosis into a
#: fact: the live diagnosis said "the storage name may be different" when
#: `pvesm status` settles it in one line.
_STATE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("storage", ("pvesm status",)),
    ("does not exist", ("pvesm status", "qm list", "pct list")),
    ("qm ", ("qm list",)),
    ("pct ", ("pct list",)),
    ("No space left", ("df -h",)),
    ("not found", ("which qm pct pvesm",)),
)


async def _failure_state(db: AsyncSession, executed: list[dict]) -> str:
    """Read the machine on the subject of the failure. ``""`` when nothing fits.

    Only read-only commands, only through the runner's read tool, capped — this
    is context for a diagnosis, not an investigation. Fail-soft: a probe that
    cannot run leaves the diagnosis exactly as it was.
    """
    from app.modules import assist_local_runner as _lr
    bad = [e for e in executed if not e.get("ok") and not e.get("informational")]
    if not bad:
        return ""
    subject = f"{bad[-1].get('command','')}\n{bad[-1].get('output','')}"
    wanted: list[str] = []
    for needle, probes in _STATE_PROBES:
        if needle.lower() in subject.lower():
            for p in probes:
                if p not in wanted:
                    wanted.append(p)
    if not wanted:
        return ""
    try:
        spec = await _lr.runner_spec(db)
        if spec is None:
            return ""
        pasted, _ran = await _lr.run_probes(
            spec, [{"id": f"S{i}", "command": c} for i, c in enumerate(wanted[:4], 1)])
        return (pasted or "")[:3000]
    except Exception as exc:
        logger.warning("failure_state_probe_failed err=%r", exc)
        return ""


async def diagnose_failure(db: AsyncSession, job_id: str, node_key: str,
                           executed: list[dict], reason: str) -> str:
    """§17.1201 — work out WHY a supervised block failed, and what to try next.

    Auto mode's failure path ended at "node failed, here is the exit code".
    The walkthrough has diagnosed errors since §17.1027 — it reads the
    environment ledger, researches what it does not recognise, and proposes
    corrected commands — and the operator's question was whether the runner can
    do the same. It can: `assist_guide.generate_fix` takes a step context, an
    error and an environment, and needs no assist session, so the autonomous
    path can use the same head the ✦ Fix button does.

    The error handed to it is the real one: the failing command and its actual
    output, not a summary, because a summary is what a diagnosis is FOR.
    Fail-soft to ``""`` — a diagnosis that cannot be produced must never turn a
    recorded failure into a lost one.
    """
    from app.config import settings
    try:
        from app.modules.assist_agent import _assemble_ctx_for_node
        from app.modules.assist_guide import generate_fix
        from app.modules.runbook_inputs import job_environment
        bad = [e for e in executed if not e.get("ok") and not e.get("informational")]
        last = bad[-1] if bad else (executed[-1] if executed else None)
        if last is None:
            return ""
        error_text = (f"$ {last['command']}\n{str(last.get('output') or '').strip()[-4000:]}\n"
                      f"(exit {last.get('exit')})\n\nThe engine ran this itself through the local runner, "
                      f"with the operator's approval. {reason}")
        _node, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=node_key)
        env = await job_environment(db, job_id)
        # §17.1202 — ACCURACY, not provenance. A diagnosis built from the web
        # alone guesses at this machine: the live one said "the storage name may
        # be different" when one read settles it. The engine already has a
        # read-only channel to the machine that just failed, so it looks BEFORE
        # it reasons and hands the model the real state alongside the error.
        state = await _failure_state(db, executed)
        if state:
            error_text += f"\n\nWHAT THIS MACHINE ACTUALLY REPORTS RIGHT NOW (read-only, just checked):\n{state}"
        res = await generate_fix(
            ctx=ctx, error_text=error_text,
            # research is what makes this more than a re-read of the error: an
            # `ipcc_send_rec` or a `qm: command not found` is not in the plan.
            research=bool(settings.assist_guide_research),
            environment=env, node_key=node_key, domain=_node.get("domain"),
        )
        out = str((res or {}).get("fix") or "").strip()
        logger.warning("supervised_run_diagnosed job=%s node=%s chars=%d", job_id, node_key, len(out))
        return out
    except Exception as exc:
        logger.warning("supervised_run_diagnose_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return ""


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
    if waiting.get("refused") or not waiting.get("commands") or waiting.get("secrets_missing"):
        return {"outcome": "not_runnable", "refused": waiting.get("refused") or [],
                "secrets_missing": waiting.get("secrets_missing") or []}
    commands = [str(c) for c in waiting.get("commands") or []]
    verify_cmds = [str(c) for c in waiting.get("verify") or []]
    asked = list(waiting.get("inputs") or [])
    need = [i for i in asked if not i.get("secret")]
    secrets_asked = [i for i in asked if i.get("secret")]
    values: dict[str, str] = {}
    if asked:                                     # §17.1187 — the values the runbook asked for, checked
        _all, problems = check_inputs([i["name"] for i in asked], inputs)
        if problems:
            return {"outcome": "inputs_missing", "problems": problems, "inputs": asked}
        values = {k: v for k, v in _all.items() if k in {i["name"] for i in need}}
        # §17.1193 — a SECRET answer is stored encrypted and referenced, never
        # spliced: the command keeps `$NAME`, so the bytes the operator
        # approved, the runner's log line and the target's process table never
        # carry it. A plain value is substituted as before.
        for i in secrets_asked:
            name = i["name"]
            from app.modules import runner_secrets as _rs
            await _rs.set_secret(db, name, _all[name],
                                 runner=str(waiting.get("runner") or "") or None, hint=str(i.get("hint") or ""))
            commands = [secret_ref_rewrite(c, name)[0] for c in commands]
            verify_cmds = [secret_ref_rewrite(c, name)[0] for c in verify_cmds]
        if need:
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
    # §17.1193 — the values for the `$NAME` references these commands carry,
    # read from the engine's encrypted store and handed to `run_block` OUT OF
    # BAND. Names the runner's own file holds are simply absent here and it
    # resolves them itself. This dict is never logged and never written back.
    refs = sorted({m.group(1) for c in runnable + verify_cmds for m in _SECRET_REF_RE.finditer(c or "")})
    secret_env: dict[str, str] = {}
    if refs:
        from app.modules import runner_secrets as _rs
        secret_env = await _rs.values_for(db, refs)
    logger.warning("supervised_run_started job=%s node=%s runner=%s commands=%d secrets=%d",
                   job_id, node_key, spec.name, len(runnable), len(secret_env))
    executed = await _sw.run_block(spec, runnable, env=secret_env)
    # §17.1201 — a read that answered "no" is not a failed block (`grep` exits 1
    # when the thing it looked for is gone, which is often the check passing).
    ok = (bool(executed) and len(executed) == len(runnable)
          and all(e["ok"] or e.get("informational") for e in executed))
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
    # §17.1201 — the runner's connection dropped. Nobody knows whether the
    # command ran, and a write whose outcome is unknown is a different decision
    # from one that definitely failed. Say so, and do not pretend to an exit
    # code ("exited None" was what the operator saw).
    dropped = [e for e in executed if e.get("unreachable")]
    # §17.1198 — a command that failed because the runner could not read is not
    # a broken machine. The write grant covers writes; a READ-ONLY command in
    # the same block deliberately runs unprivileged, and on a Proxmox host that
    # is exactly what `qm`/`pct`/`pvesm` need root for. Say which it was, and
    # record the command so the connection page can offer the read grant.
    needs_root = [e for e in executed if not e["ok"] and _NEEDS_ROOT_RE.search(str(e.get("output") or ""))]
    if needs_root:
        try:
            await record_needs_root(db, job_id, [e["command"] for e in needs_root])
        except Exception as exc:
            logger.warning("needs_root_record_failed job=%s err=%r", job_id, exc)
    reason = mask_secrets(
        (f"the connection to {spec.name} dropped while `{dropped[-1]['command'][:80]}` was running "
         f"({dropped[-1]['output'][:120]}). Whether it ran on the machine is UNKNOWN — check before retrying, "
         f"because a repeat of a write that already happened is not the same as a retry of one that did not.")
        if dropped else
        (f"`{needs_root[-1]['command'][:80]}` could not read on that machine — the runner is unprivileged for "
         f"READ commands (the write grant covers writes only). Allow it to read as root: Settings → Machines, "
         f"or Capabilities → “Give the runner administrator rights for specific commands”.")
        if needs_root else
        ("the runner ran nothing" if not last else
         (f"the runner refused `{last['command'][:80]}`: {last['output'][:200]}" if last.get("refused")
          else f"`{last['command'][:80]}` exited {last['exit']}: {last['output'][-300:]}")), values, need)
    # §17.1201 — the runner can problem-solve, the way the walkthrough does.
    # Until now a failed block ended the step: node `failed`, a one-line reason,
    # and nothing tried. Assist has diagnosed errors since §17.1027 — reading
    # the environment ledger, researching what it does not recognise, and
    # proposing corrected commands — and `generate_fix` needs no session, so
    # Auto can use the same head. The diagnosis rides on the node's output and
    # the returned dict, where the Run stage shows it.
    diagnosis = await diagnose_failure(db, job_id, node_key, executed, reason)
    if diagnosis:
        output = f"{output}\n\n## What went wrong, and what to try\n\n{diagnosis}"
    await db.execute(
        text("UPDATE dag_nodes SET status = 'failed', output_text = :out, completed_at = NOW(), updated_at = NOW(), "
             "last_verification_reason = :why WHERE job_id = :jid AND node_key = :nk AND status = 'running'"),
        {"jid": job_id, "nk": node_key, "out": output, "why": f"supervised run stopped — {reason}"[:1000]})
    logger.warning("supervised_run_failed job=%s node=%s diagnosed=%s reason=%s",
                   job_id, node_key, bool(diagnosis), reason[:200])
    return {"outcome": "failed", "node_status": "failed", "executed": executed, "reason": reason,
            "diagnosis": diagnosis, "unknown_outcome": bool(dropped)}


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
