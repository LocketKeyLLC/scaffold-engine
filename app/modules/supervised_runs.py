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
import ast
import asyncio
import logging
import shlex
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


#: §17.1234 adds "cannot report an HTTP error" — a shape the engine can fix
#: itself, so the redraft must recognise it as one.
#: §17.1269 — every refusal the ENGINE makes about its own block's shape must be
#: listed here, or `shape_retry_note` does not recognise it, no redraft happens,
#: and the operator is handed a greyed-out Run with nothing to do about it. That
#: is exactly what §17.1268 did on its first live outing: the budget refusal was
#: correct, unregistered, and therefore a dead end. A test below walks every
#: refusal-producing function and fails when one's text matches nothing here.
_SHAPE_REFUSALS = ("substitution/heredoc", "redirect", "empty", "cannot report an HTTP error",
                   "ON THE HOST", "not valid Python", "cannot even be split",
                   "only passes a stored value",
                   "waits on something off this machine",         # §17.1268
                   "dies at the first one that hangs",            # §17.1274
                   "against a",                                    # §17.1274 slice × timeout
                   "whether the KEY `propertyName` appears",       # §17.1278
                   "by a phrase list",                             # §17.1278b
                   "without reading the body's `propertyName`",    # §17.1278c
                   "has no 'needs_input' verdict",                 # §17.1279
                   "a secret cannot be written into a file",       # §17.1280
                   "elevates only the first command of a line",    # §17.1283
                   "runs in the runner's own shell on the Proxmox HOST",   # §17.1285
                   "appears only in the verify",                   # §17.1288
                   "inside an ssh command line",                   # §17.1288b
                   "reads the neighbour table cold",               # §17.1288c
                   "is an assumption",                             # §17.1288h
                   # §17.1288j — the machine's contradictions (runbook_preconditions)
                   # are the drafter's to fix too: live, draft 3 was told nothing
                   # about the stopped VM and draft 4 nothing about the key, so
                   # each "fixed" the shape note and lost what the other had.
                   "is stopped (`",                                # §17.1288f / §17.1213
                   "fails before it starts",                       # §17.1213 needs-running / §17.1301
                   "'s address: the engine measured",             # §17.1303 measured address beats a written one
                   "is a placeholder, not a value",                # §17.1306
                   "appears nowhere the engine holds",             # §17.1307
                   "appears in nothing the engine holds",          # §17.1312
                   "content cut",                                  # §17.1312 (the template draw was cut twice)
                   "ends inside a heredoc",                        # §17.1310
                   "nothing has put this host's key on guest",     # §17.1288g
                   "is a VM on this host, not a container",        # §17.1213
                   "is a container on this host, not a VM",        # §17.1213
                   "is ALREADY",                                   # §17.1240
                   "is already taken on this host",                # §17.1243
                   "there is no guest",                            # §17.1213
                   "has never been written",                       # §17.1288p
                   "and nothing runs it",                          # §17.1288k
                   "reads the address itself and asks the operator for it",   # §17.1288l
                   "is this host's own address",                   # §17.1288l
                   "asks the operator for a password the runner holds",   # §17.1288m
                   "is not waiting for the guest")                 # §17.1288m

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


def refusal_kinds(frame: dict) -> set[str]:
    """§17.1277 — which SHAPE rules a frame was refused for (the `_SHAPE_REFUSALS`
    markers its refusals carry). Two frames refused for DISJOINT kinds are a
    draft that fixed one thing and broke another, not a draft going round in
    circles -- and that one deserves one more try, told both."""
    return {s for r in (frame or {}).get("refused") or []
            for s in _SHAPE_REFUSALS if s in str(r.get("why") or "")}


def shape_retry_note(frame: dict, previous: Optional[dict] = None, repeated: bool = False) -> str:
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
    # §17.1277 — live, draft 1 was hang-safe and unbatched, draft 2 batched by
    # argv and dropped the hang handling: each fixed the refusal it was shown
    # and lost what the other had right. When the previous attempt was refused
    # for something ELSE, say so, so the next draft keeps both.
    # §17.1288e — the step's own text can be the thing the refusal contradicts.
    # Live, ADD82's specification (written by an earlier repair, before the
    # engine knew how to reach a VM without an agent) said "the host has NO way
    # in … done at the console, by hand" and quoted the four host-side lines;
    # the draft prompt says the specification wins over the task line, and the
    # redraft obeyed the specification over the refusal -- twice in a row at
    # 18:03, after reaching the VM at 17:33. The refusal is what the engine
    # measured AFTER that text was written, so it outranks the text, and the
    # note has to say so where the specification said the opposite.
    lines += ("\n\nTHIS NOTE OUTRANKS THE SPECIFICATION ABOVE wherever they disagree: the specification was "
              "written before these refusals were measured. A text that says a machine has \"no way in\" or that "
              "the step is \"done at the console\" is superseded by the way in the refusal names; the commands such "
              "a text quotes for a console are the GUEST's commands and go inside the script's ssh payload. Never "
              "answer a refusal with console steps or prose -- a draft with nothing the runner can run is the "
              "worst outcome, and the same block again is the second worst.")
    if repeated and previous and (previous or {}).get("refused"):
        lines = ("YOUR REDRAFT REPEATED THE SAME REFUSED SHAPE -- it was refused for exactly what the draft before "
                 "it was refused for. Do not restate the specification; follow the refusal.\n" + lines)
    elif previous and (previous or {}).get("refused"):
        prev = "\n".join(f"- `{str(r.get('command') or '')[:160]}` — {r.get('why')}"
                          for r in previous["refused"])
        lines += ("\n\nTHE DRAFT BEFORE THAT was refused for something different:\n" + prev +
                  "\nThis draft fixed that and was refused for the points above instead. The next draft "
                  "must satisfy BOTH: keep every correction the earlier draft already had right (its "
                  "exception handling, its batching, its checks) and add what is still missing. Do not "
                  "trade one refusal for another.")
    # §17.1276b — the restated rules must fit the channel the frame was drawn
    # for. Told in the refusal to batch by argv and in THIS summary to "write a
    # file with printf | tee" and "cut into batches and send several commands",
    # the live redraft followed the summary and batched inside one script.
    file_channel = bool((frame or {}).get("file_channel"))
    batching = (
        f"A command gets {_RUN_BUDGET_S} seconds, so work that waits on something off this machine many "
        "times over must be cut into batches: write the script ONCE under ## Write these files, taking the "
        "slice bounds as arguments (`start, end = int(sys.argv[1]), int(sys.argv[2])`; `for item in "
        "items[start:end]:`), and list one command per batch under ## Run this (`python3 /tmp/x.py 0 10`, "
        "`python3 /tmp/x.py 10 20`, …) — ten per batch when each item waits on a remote service. Never a loop "
        "over batches inside one script: one command is one budget. Each batch treats a thing that is already "
        "there as success. A loop over third parties catches `(urllib.error.URLError, TimeoutError, OSError)` "
        "around the call and records the item as unreachable. "
        if file_channel else
        f"A command gets {_RUN_BUDGET_S} seconds, so work that waits on something off this machine many "
        "times over must be cut into batches (`items[:20]`, or ten if each one waits on a remote service) "
        "and sent as several commands, each one treating a thing that is already there as success. ")
    files = ("No heredoc and no `printf | tee`: a file goes under ## Write these files, written exactly as the "
             "interpreter will see it. "
             if file_channel else
             "No heredoc: write a file with `printf '%s\\n' 'line' | tee /path`. ")
    return (
        "YOUR PREVIOUS DRAFT WAS REFUSED BY THE RUNNER'S GATE — rewrite it so every command can run.\n"
        f"{lines}\n"
        "Rules that were broken, restated: each command runs alone, in its own shell. "
        + batching +
        "No `$(…)` or backticks "
        "ANYWHERE — including to build a list for a loop; write the list out literally (`for i in 1 2 3; do …; "
        "done`) or, better, drop the loop and state the checks as separate commands. "
        + files +
        "No `>`/`>>` redirects. A retry/wait loop is rarely worth it here — "
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


async def decision_is_stale(db: AsyncSession, job_id: str, node_key: str, entry: dict) -> bool:
    """§17.1200/§17.1245 — has this decision already been spent on an attempt?

    The record carries the moment the operator answered. If the node has been
    written since — it ran, it failed, it was reopened — the answer belongs to
    that past attempt and the step must be asked about again. Anything
    unparseable counts as stale: asking once more costs a click, the other way
    round marks a machine-changing step done without running it.

    §17.1245 — this lived in `execution_agent` and only `_hand_back_for_approval`
    used it. `pending_hands_on`, three functions below here, skipped ANY node with
    a recorded decision and never asked whether that decision was spent. Live,
    ADD111: the operator approved it, the run died on §17.1244's five-second
    clock, the step was reset — and it could never be offered for approval again,
    because their approval from the failed attempt was still on the job. One
    implementation now, called from both.
    """
    at = str((entry or {}).get("at") or "")
    if not at:
        return True
    row = (await db.execute(
        text("SELECT updated_at FROM dag_nodes WHERE job_id = :jid AND node_key = :nk"),
        {"jid": job_id, "nk": node_key})).mappings().first()
    if not row:
        return True
    try:
        from datetime import datetime
        answered = datetime.fromisoformat(at)
        touched = row["updated_at"]
        if answered.tzinfo is None or touched is None:
            return True
        return touched > answered
    except Exception as exc:
        logger.warning("decision_staleness_unreadable job=%s node=%s err=%r", job_id, node_key, exc)
        return True


async def pending_hands_on(db: AsyncSession, job_id: str) -> Optional[dict]:
    """The first dep-satisfied pending node that does host work (§17.1183)
    and has no recorded decision — the step the run would claim next."""
    from app.modules.step_classify import step_is_hands_on
    decided = _as_dict((await db.execute(
        text("SELECT metadata->'decisions' FROM jobs WHERE id = :jid"), {"jid": job_id})).scalar())
    rows = (await db.execute(
        text("""
            SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on, n.tool, n.node_type,
                   n.retry_count, n.last_verification_reason, n.execution_order, n.output_text
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
        # §17.1260 — `reset` nulls output_text and last_verification_reason, the
        # two fields §17.1247 reads. Put them back from the pre-image.
        node = await recover_prior_attempt(db, job_id, node)
        # §17.1245 — a decision is consumed by the attempt it authorised. Skip the
        # node only while that answer still belongs to THIS attempt.
        _entry = decided.get(node["node_key"])
        if _entry is not None and not await decision_is_stale(db, job_id, node["node_key"], _entry):
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
#: §17.1278c — the classifier, given rather than described. Three drafts in one
#: night each misread the sentence a different way (key present; phrase list;
#: never looked), so the drafter gets the code and the gate checks it was used.
WHOSE_FAULT_HELPER = """
def whose_fault(errors):
    # a *arr 400 body is a list of {propertyName, errorMessage, attemptedValue}
    errors = [e for e in errors if isinstance(e, dict)]
    msgs = " ".join(str(e.get("errorMessage", "")) for e in errors).lower()
    if "unique" in msgs or "already exists" in msgs:
        return "duplicate"
    named = [e for e in errors if str(e.get("propertyName") or "").strip()]
    if named and all(e.get("attemptedValue") == "" for e in named):
        return "needs_input"          # a field is NAMED and EMPTY: a template that wants a value only the operator has -- report it by name, go on
    if named:
        return "bad_request"          # a field is NAMED with a value: our body is wrong -- stop and print it
    return "unreachable"              # no field named: that tracker is down -- record it and go on
"""


CHANNEL_RULES = """
Runnable-by-the-engine rules (this runbook may be carried out FOR the operator, one command at a time):
- Each line under "## Run this" must be ONE self-contained command. They are run in order, each in its OWN shell, so no shell state carries between them: never `cd` and then name a file relatively — write absolute paths (`wget -O /tmp/x.run …`, then `/tmp/x.run`).
- No heredocs (`<<EOF`), no command substitution (`$(…)` or backticks), and no output redirection (`>` / `>>`). Write a file by piping into `tee` with the content as arguments: `printf '%s\n' 'line one' 'line two' | tee /etc/example.conf` (append with `tee -a`). `2>/dev/null` is fine.
- No multi-line shell constructs, and no `if … then … fi` even on one line: each part of a command is judged on its own, and a `then`-prefixed part cannot be read. Write an idempotent step as a guard chain instead — `pct status 111 | grep -q running || pct start 111` — where the check and the fix are each a whole command.
- Avoid loops. A retry/wait loop is rarely worth it here: the operator sees the result of each command, so one check is usually enough. If you truly need one, write the list out literally (`for i in 1 2 3; do …; done`) — NEVER build it with `$(seq …)`, which is command substitution and is refused wherever it appears.
- Under "## Verify", each check stays a read-only command (`pct status 111`, `systemctl is-active …`, `ls -ld …`).
- DO the work with an API or a CLI, never by describing the web UI. "Open the Prowlarr web UI and go to Settings → Apps → Add Application", "click Test, then Save" is not something this channel can carry out — it produces a runbook with no commands at all, and the step falls back to the operator doing it by hand. Almost every service here has an HTTP API: drive it with `curl` (`curl -s -X POST http://HOST:9696/api/v1/indexer -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' -d '{…}'`), or its own CLI where it has one.
- If you can CHECK something with a command under "## Verify", you can DO it with a command under "## Run this". A Verify section full of `curl …/api/v1/…` calls beside a Run section of UI clicks is the specific contradiction to avoid: the same API that answers the check also makes the change.
- A `curl` that CHANGES something must fail loudly: add `--fail-with-body` (so an HTTP 400/401/404/500 exits non-zero and still shows the server's message). Without it `curl -s` exits 0 having fetched an error page, and a step that changed nothing is reported as done — live, nine `POST`s to an API answered "must be greater than 0" and every one "succeeded". A read under "## Verify" may stay a plain `curl -s`.
- A service's own API key usually lives in that service's own config file on the machine, and reading it there beats depending on a stored copy: it cannot be stale, nothing secret has to cross into your commands, and the value never appears in the block or any log. An *arr app keeps it in the `<ApiKey>` element of the `config.xml` under its `-data=` directory (find that with `systemctl show <svc> -p ExecStart --value` rather than guessing the path). Prefer that to `$NAME` whenever the key is on a machine you can read.
- WRITING A SCRIPT, exactly. Wrap each `printf` argument in DOUBLE quotes and use SINGLE quotes inside the code; then nothing needs escaping and the file parses. Never put a backslash before a quote in a `printf` argument -- the shell keeps the backslash and the interpreter chokes on it. Like this:
    printf '%s\\n' "import json, urllib.request" "cfg = open('/tmp/config.xml').read()" "key = cfg.split('<ApiKey>')[1].split('<')[0]" "print(key[:4])" | tee /tmp/x.py
  and then run it as its own command: `python3 /tmp/x.py`. Keep each line short; a long line is where the quoting goes wrong.
- AN API TELLS YOU ITS OWN RULES -- ask it once before doing it 88 times. Do not write a request body from memory: a schema or template an API hands you is what it ACCEPTS as a description, not necessarily a valid body to post back. Do the operation ONCE, and if it is rejected print the full response body and stop; a service says in that body exactly which field it refused (Prowlarr: "'App Profile Id' must be greater than '0'"). Fix the body from what it said, then do the rest. A loop that swallows each error into a one-line summary turns one useful diagnosis into dozens of useless lines and changes nothing.
- A SCRIPT you write does not inherit a stored value. The runner passes one only into a command whose text mentions `$NAME`, so `printf … | tee /tmp/x.py` then a bare `python3 /tmp/x.py` starts with no such variable. Either have the script read the key off the machine (above), or put the reference in the command that runs it: `NAME="$NAME" python3 /tmp/x.py`.
- ONE COMMAND GETS 180 SECONDS, and when it runs out the command is killed and the step fails with nothing to show for the work it did. So count what you are asking for: a loop over 89 things, each a call to a service OUTSIDE this machine that waits on a connection test, does not fit -- and a run that dies at 180s leaves no record of the 40 it managed. Split work like that into batches that each fit comfortably -- twenty per command, or ten when each one waits on a remote service that may be slow or dead, across several commands, and make every batch RESUMABLE -- treat "already present" as success, not as an error -- so re-running one costs nothing and a later batch never redoes an earlier one. A read that only looks at this machine is not the problem; waiting on something across the network, many times over, is. And give every request its OWN short timeout -- about 15 seconds -- so a batch cannot outlive the budget even if every item hangs: ten items at `timeout=60` is 600 seconds, not 180.
- WHEN A SERVICE REFUSES SOMETHING YOU ARE ADDING, ITS OWN BODY SAYS WHOSE FAULT IT IS -- read that, do not guess from a list of phrases. A VALIDATION error names the field it refused (`"propertyName": "Name"`, `"'App Profile Id' must be greater than '0'"`): your body is wrong, so stop at the first one and print it. An AVAILABILITY error names no field and talks about reaching the thing (`"propertyName": ""` with `"Unable to access 16mag.net, blocked by CloudFlare Protection"`, "Unable to connect", "timed out", a captcha, a certificate): that one thing is unusable right now, so record its name, skip it, and keep going. Branch on THAT distinction -- whether a field is named -- and not on a hand-written list of error strings: live, a block matched four connection phrases, met "blocked by CloudFlare Protection" on its second indexer of 89, called it a validation failure and stopped. Do not write that branch yourself -- paste this helper into the script and call it on the parsed 400 body:
""" + WHOSE_FAULT_HELPER + """
  'unreachable' -> record the name, continue; 'needs_input' -> record "needs configuration: <name> (<field>)", continue; 'bad_request' -> print the body, stop; 'duplicate' -> already present. Live, the 88th definition was "Torrent RSS Feed" -- a generic template whose BaseUrl must be typed -- and a script that stopped there called a template a bad request.
- REACHING A MACHINE OVER SSH FOR THE FIRST TIME: nothing can type a password here, and the host key is unknown. A password the store holds goes to ssh through sshpass's environment, never argv: `apt-get install -y sshpass` if it is missing, then `SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new -i /root/.ssh/id_rsa.pub user@host` (the same prefix works for `ssh` and `scp`). After that, key auth works and `ssh -o BatchMode=yes user@host true` is the check. The ACCOUNT inside a guest is a placeholder named after the guest (`<PALWORLD_USER>@$IP`) unless a pin or the step's own text names it -- `root` is a guess (Ubuntu Server refuses root over ssh), and `root@pve` is the host's shell, not a guest's. The PASSWORD is `$MASS_PASSWORD`, by name, through the environment (`MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/x.sh`) -- never a new `<X_PASSWORD>` placeholder. A wait pings the guest's FOUND address (`$IP`), never a fixed one (the router or this host answer whether or not the guest is up). A secret never rides an ssh COMMAND LINE: `ssh host 'echo "$MASS_PASSWORD" | sudo -S …'` expands on the remote, where it is unset, and the double-quoted form puts the value in both machines' process lists. Feed it on stdin: `ssh -o BatchMode=yes user@host "sudo -S -p '' bash -c 'apt-get update && apt-get install -y x'" <<< "$MASS_PASSWORD"`. A VM you just STARTED is not up yet: wait for it with a loop of reads (`for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ping -c 1 -W 2 host >/dev/null 2>&1 && break; sleep 5; done`) before the first ssh -- a loop of reads is one line, elevated or not.
- A VM WITH NO GUEST AGENT IS STILL REACHABLE, and reaching it is your job, not the operator's. Its NIC's MAC is in `qm config N` (`net0: virtio=BC:24:…`); once the VM is up, its address comes from a sweep: `nmap -sn <the bridge's /24>` prints `Nmap scan report for <ip>` then `MAC Address: <mac>` -- read the ip beside the guest's MAC from nmap's OWN output (nmap's ARP scan never fills `ip neigh`; a guest answered it for an hour while the kernel table stayed empty). Without nmap: `for h in $(seq 1 254); do ping -c 1 -W 1 <net>.$h >/dev/null 2>&1 & done; wait` DOES fill the kernel table, then `ip neigh show | grep -i <mac>`. Either way, loop until it appears. Then `SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new <user>@<address>`, and do the step's work over `ssh <user>@<address> '…'` -- for the agent itself: `apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent`, checked with `qm agent N ping` on the host. Values must pass between those steps (the address found feeds the ssh), so write the WHOLE sequence as one bash script under ## Write these files and run it with `MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/<name>.sh`; inside a file `$(…)`, loops and variables are all fine -- but under `set -e` a lookup that may find nothing (`IP=$(ip neigh show | grep … )`) ends the script on the first empty pass: write `IP=$(… || true)` for every lookup a wait loop expects to be empty. A step is the operator's ONLY when ssh itself is refused -- say which command refused and why, with the output.
- YOUR VERIFY CHECKS GO THROUGH THE SAME CHANNEL as the run commands, so they obey the same rules: one simple read-only command each, no `$(...)` substitution, no pipe into `python3 -c`. A clever one-liner that reads a key and counts the results in one go is refused and the step is left with nothing checking it. Read the value in one check, use it in the next. A check uses what the run used: a placeholder that appears only in a verify is a value the operator would type for nothing -- check from the host (`qm agent N ping`, `qm agent N exec -- systemctl is-active <unit>`) or in the script's last lines.
- A LIST THE MACHINE HANDS YOU IS WHAT EXISTS, NOT WHAT WORKS. A schema, catalogue or definition list shipped with a service tells you what it can be CONFIGURED with; it says nothing about whether each of those things is still alive this week. Only the second question goes stale, and it is the one the web sources above answer. So: a rejection of your REQUEST (400, 422, "must be greater than") is your mistake — stop at the first one, print the body, fix it. A failure to REACH the thing (502, 503, timeout, refused) is that thing's problem — record it by name, skip it, and keep going through the rest of the list. Finish with a count of what landed and a line per one you skipped and why; a step that adds 35 of 89 and names the 54 corpses has done its job, and one that stops at the first corpse has not. A thing that HANGS is not an HTTP status: `urlopen` raises TimeoutError or urllib.error.URLError, so `except HTTPError` alone lets one slow tracker kill the whole run with no summary -- catch `(urllib.error.URLError, TimeoutError, OSError)` around the call, INSIDE the loop, and record that item as unreachable exactly like a 502.
- A service that runs INSIDE a guest is reached at THAT guest's address, not the host's. Name the placeholder after the guest it belongs to — `<PROWLARR_IP>`, `<RADARR_IP>` — never `<PROXMOX_HOST_IP>` for something listening inside a container. The guest list below says which guest each service is in; the engine can read that guest's address off the host and fill it in, but only if you name it after the guest.
"""


#: §17.1271 — only shown when the runner on the other end actually has the
#: `write_file` tool. An older helper does not, and telling the drafter to use a
#: notation that machine would refuse is how a capability becomes a trap.
FILE_RULES = """
WRITING A FILE: THIS MACHINE TAKES FILES DIRECTLY, so never build one with `printf … | tee`.
Put it in its own section and the engine hands the content to the machine as-is -- no shell touches it,
so NOTHING in it needs escaping. Write the code exactly as the interpreter will see it:

## Write these files

### /tmp/add_indexers.py
```python
import json, urllib.request
entry = {"name": "Anidex"}
print(f"added: {entry["name"]}")
```

## Run this

```bash
python3 /tmp/add_indexers.py
```

Quotes inside quotes are fine there, because there is no shell to confuse. That is the whole reason this
exists: a script built with `printf '%s\n' '…'` has to escape its own quotes, and an escaped quote inside a
single-quoted shell word is a literal backslash the interpreter then refuses. Use the section, and the
problem cannot happen. Files are written before the commands run, in the order you list them, and the
engine compiles a `.py` file before offering it -- so a syntax error is caught before the operator is asked.

BATCHING ON THIS CHANNEL (one command is one 180-second budget): a script that works through a list a
service returned MUST take its slice bounds as arguments and be run once per batch -- write it ONCE, then
list one command per batch:

```python
import sys
start, end = int(sys.argv[1]), int(sys.argv[2])
for item in items[start:end]:
    ...   # one item, with (urllib.error.URLError, TimeoutError, OSError) caught and recorded as unreachable
```

```bash
python3 /tmp/add_indexers.py 0 10
python3 /tmp/add_indexers.py 10 20
```

Ten per batch when each item waits on a remote service. A loop over batches INSIDE one script is still one
command and is refused.
"""


def no_commands_retry_note(step_text: str, runbook: str) -> str:
    """§17.1227 — the draft described a web UI, so there is nothing to run.

    Live, ADD96 ("Add the search sources to Prowlarr and connect it to Radarr
    and Sonarr"): a 3,961-character runbook whose "## Run this" was nine steps
    of *"Open the Prowlarr web UI"*, *"go to Indexers → Add Indexer"*, *"Click
    Test to verify the connection, then Save"* — and whose "## Verify" section
    called `curl -s http://…:9696/api/v1/indexer -H "X-Api-Key: …"` four times.
    The drafter knew the API existed and used it only to CHECK. `commands` came
    back empty, so Run was never offered, the frame suggested "I'll do it
    myself", and the operator was handed nine screens of clicking on a host the
    engine could reach.

    Neither existing redraft trigger fires on this: `shape_retry_note` needs a
    REFUSAL (there were none — there was nothing to refuse) and the coverage
    pass only replaces a draft with one that is both non-empty and complete,
    which a UI-clicking redraft never is. So the specific remedy has to be
    named, the way §17.1196 names a gate refusal.
    """
    if not (runbook or "").strip():
        return ""
    verify = [c for c in _verify_commands_in(runbook) if c]
    api = sorted({m.group(0) for c in verify for m in _API_PATH_RE.finditer(c)})
    lines = [
        "YOUR DRAFT HAS NOTHING TO RUN. The \"## Run this\" section came back with no commands — "
        "it describes navigating a web UI, which this channel cannot carry out. A runbook with no "
        "commands means the engine offers the operator nothing and they do the whole step by hand.",
        "",
        "Rewrite \"## Run this\" as shell commands, one self-contained command per line.",
    ]
    if api:
        lines += [
            "",
            "Your OWN \"## Verify\" section already calls this service's HTTP API:",
            *[f"  {a}" for a in api[:6]],
            "",
            "That is the same API that makes the change. Use it — `curl -s -X POST …` with "
            "`-H \"X-Api-Key: $NAME\"` and a JSON body — instead of telling the operator where to "
            "click. If a call needs a field you do not know, GET the relevant endpoint first and "
            "say so in the step.",
        ]
    else:
        lines += [
            "",
            "If the thing you are configuring has an HTTP API or a CLI, drive that. Do not write "
            "\"open the UI and click\" as a step the engine is meant to run.",
        ]
    lines += [
        "",
        "If some part genuinely CANNOT be done without a browser, put only that part in a final "
        "\"## By hand\" section and make every other part a command — a step that is 80% runnable "
        "is worth far more than one that is 0% runnable.",
    ]
    return "\n".join(lines)


#: `/api/v1/indexer`, `/api/v3/rootfolder` — an API path in a drafted command.
_API_PATH_RE = re.compile(r"/api/v\d+/[A-Za-z0-9_/-]+")


def _verify_commands_in(runbook: str) -> list[str]:
    """The fenced commands under a "## Verify" heading only."""
    m = re.search(r"^##+\s*Verify.*?$(.*)", runbook or "", re.M | re.S)
    if not m:
        return []
    return [c for c in runbook_commands("## Run this\n" + m.group(1))]


#: a `curl` that changes something: an explicit write method, or a body.
_CURL_WRITE = re.compile(r"(?:^|\s)curl\b(?=.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)\b|\s(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form|-T|--upload-file)\b))", re.I)
#: the flags that make its exit code mean something.
_CURL_FAILS = re.compile(r"(?:--fail-with-body|--fail-early|\s--fail\b|\s-f\b|\s-[a-eg-zA-Z]*f[a-eg-zA-Z]*\s)")


#: a runbook saying, in its own words, that it cannot be carried out yet.
_SELF_BLOCKED = re.compile(
    r"(?i)\b(?:is|are|remains?|stays?)\s+blocked\s+until\b"
    r"|\bblocked\s+(?:until|on|by)\b"
    r"|\bcannot\s+(?:proceed|continue|be\s+(?:done|completed|carried\s+out))\s+until\b"
    r"|\bcan(?:not|'t)\s+be\s+(?:verified|proven|tested)\s+until\b"
    r"|\bmust\s+wait\s+(?:for|until)\b"
    r"|\bnot\s+possible\s+until\b")


def declares_itself_blocked(text_value: str) -> Optional[str]:
    """§17.1235 — the sentence in which a step says it is not done.

    Live, ADD98 ("Prove it end to end: ask for one film and watch it"), recorded
    `done`, whose FIRST line was

        This proof is blocked until the download client decision from ADD102 is
        resolved and ADD97 steps 1-2 are complete.

    It produced prose, no command ran, it said in its own words that it could not
    be carried out — and it counted toward the job's finished total. A step that
    describes its own blockage is the clearest possible signal that the work did
    not happen, and it was the one signal nothing read.

    Deliberately narrow: only a self-declaration about THIS step. A runbook that
    lists prerequisites ("Radarr container 103 is running") is describing a state
    it expects, not announcing a failure, and must still pass.
    """
    for para in re.split(r"\n\s*\n", str(text_value or ""))[:6]:
        m = _SELF_BLOCKED.search(para)
        if m:
            sentence = next((sn.strip() for sn in re.split(r"(?<=[.!?])\s+", para)
                             if _SELF_BLOCKED.search(sn)), para.strip())
            return " ".join(sentence.split())[:300]
    return None


#: an interpreter that EXECUTES whatever it is handed on stdin.
_INTERPRETER = re.compile(r"^(?:ba|da|z|k)?sh\b|^python[0-9.]*\b|^perl\b|^ruby\b|^node\b")
#: entering a guest — everything after `--` runs INSIDE it, and nothing after a
#: pipe does.
_GUEST_ENTRY = re.compile(r"^(?:pct\s+(?:exec|enter)|qm\s+guest\s+exec)\b")


def all_reads_for_a_changing_step(commands: list[str], node: dict) -> str:
    """§17.1254 — a block that only LOOKS, for a step that must CHANGE something.

    Live, ADD96 ("Add the search sources to Prowlarr and connect it to Radarr and
    Sonarr"). One draft produced 24 commands: read the indexer schema, then a
    `curl -X POST …/api/v1/indexer` per public tracker. The next draft of the SAME
    step produced two — both `GET …/api/v1/indexer/schema`. Nothing added
    anything. Run was offered, the gate was clean, and approving it would have
    marked the step done having changed nothing: the false-`done` family §17.1233
    and §17.1235 exist to close.

    Nothing else catches this. The shape gate judges each command and two reads
    are individually fine; §17.1227 needs ZERO commands; §17.1215's coverage pass
    compares the block against commands quoted in the step TEXT, and this step
    quotes none — it just says what to achieve.

    So: when `step_classify` says the step changes a machine and every drafted
    command is read-only, the block cannot be what the step is for. Returned as a
    retry note, because the answer is a better draft and not a refusal the
    operator has to interpret.
    """
    from app.modules.assist_supervised import read_only, split_segments
    from app.modules.step_classify import step_is_hands_on
    cmds = [str(c) for c in (commands or []) if str(c).strip()]
    if not cmds:
        return ""                                  # §17.1227 owns the empty case
    on, _why = step_is_hands_on(dict(node) if hasattr(node, "keys") else {})
    if not on:
        return ""                                  # a reading step may legitimately only read
    for c in cmds:
        for seg in split_segments(c):
            if seg.strip() and not read_only(seg)[0]:
                return ""                          # something changes; fine
    lines = [
        "EVERY COMMAND IN YOUR DRAFT ONLY LOOKS AT THINGS. This step has to CHANGE something -- "
        "that is why it is being run through the channel -- and nothing in the block does.",
        "",
        "What you wrote:",
    ]
    lines += [f"  - {c[:150]}" for c in cmds[:8]]
    lines += [
        "",
        "Reading is how you find out WHAT to change; it is not the change. Keep the reads if they "
        "tell you something you need, then add the commands that actually do the work -- and make "
        "them fail loudly (`--fail-with-body` on a `curl` that writes) so a rejection is not "
        "mistaken for success. If the step genuinely cannot be done through this channel, say so in "
        "one line instead of drafting a block that looks busy and changes nothing.",
    ]
    return "\n".join(lines)


#: §17.1255 — an inline interpreter script whose quoting cannot work.
_INLINE_SCRIPT = re.compile(
    r"(?:python[0-9.]*|perl|ruby|node)\s+-(?:c|e)\s+'", re.I)


#: §17.1255b — a file being WRITTEN that is itself code.
_CODE_TARGET = re.compile(r"\btee\s+(?:-a\s+)?(\S+\.(?:py|sh|pl|rb|js|bash))\b", re.I)

#: a single-quoted shell argument. Inside one, `\"` is never necessary — the
#: quotes already protect a double quote — so it reaches the file or the
#: interpreter as a literal backslash.
_SQ_ARG = re.compile(r"'((?:[^']){0,4000}?)'")



#: §17.1256 — a secret name a written script expects from its environment.
_SCRIPT_ENV_READ = re.compile(
    r"""os\.environ(?:\.get)?\s*[\[(]\s*["']([A-Z][A-Z0-9_]{2,60})["']"""
    r"""|ENV\s*\[\s*["']([A-Z][A-Z0-9_]{2,60})["']"""
    r"""|getenv\s*\(\s*["']([A-Z][A-Z0-9_]{2,60})["']""")

#: the file a `tee` is writing, when that file is a script.
_TEE_SCRIPT = re.compile(r"\btee\s+(?:-a\s+)?(\S+\.(?:py|sh|pl|rb|js|bash))\b", re.I)


def script_secret_not_passed(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1256 — a written script reads a secret the command running it never gets.

    The runner injects a secret only into commands whose TEXT references it:

        needed = {n: v for n, v in env.items() if f"${n}" in cmd …}

    So `printf … | tee /tmp/add_indexers.py` followed by a bare
    `python3 /tmp/add_indexers.py` cannot work: the script asks for
    `os.environ["PROWLARR_API_KEY"]` and the process is started with no such
    variable. Live, ADD115 died on exactly that —

        KeyError: 'PROWLARR_API_KEY'

    and the redraft, told the reason by §17.1247, "fixed" it by switching to
    `os.environ.get(...)`: no exception, `KEY = None`, and every POST would have
    failed 401 instead. The model cannot reason its way here because the rule is
    internal to the runner, so the engine has to say it.

    The remedy is one prefix: `NAME="$NAME" python3 /tmp/x.py`, which puts the
    reference in the command text where the runner looks, without the value ever
    appearing in the block.
    """
    cmds = [str(c) for c in (commands or [])]
    wanted: dict[str, set] = {}          # script path -> secret names it reads
    for c in cmds:
        tee = _TEE_SCRIPT.search(c)
        if not tee:
            continue
        names = {g for m in _SCRIPT_ENV_READ.finditer(c) for g in m.groups() if g}
        if names:
            wanted.setdefault(tee.group(1), set()).update(names)
    for f in files or []:                 # §17.1274 — a file written through the channel is a script too
        path = str((f or {}).get("path") or "")
        content = str((f or {}).get("content") or "")
        names = {g for m in _SCRIPT_ENV_READ.finditer(content) for g in m.groups() if g}
        if path.endswith(".sh"):          # §17.1286 — a bash script reads a secret as `$NAME`
            from app.modules.runbook_inputs import secret_name
            names |= {m.group(1) for m in _SECRET_REF_RE.finditer(content) if secret_name(m.group(1))}
            # §17.1288j — a name the script ASSIGNS (`PASS="${MASS_PASSWORD:?…}"`)
            # is its own variable, not one read from the environment; live, the
            # one draft that had everything right was refused for `$PASS`.
            names = {n for n in names if not re.search(rf"^\s*(?:export\s+)?{re.escape(n)}=", content, re.M)}
        if path and names:
            wanted.setdefault(path, set()).update(names)
    if not wanted:
        return []
    out: list[dict] = []
    for c in cmds:
        if _TEE_SCRIPT.search(c):
            continue                      # the command that WRITES it is fine
        for path, names in wanted.items():
            if path not in c:
                continue
            missing = sorted(n for n in names if f"${n}" not in c and "${" + n + "}" not in c)
            if not missing:
                continue
            first = missing[0]
            out.append({"command": c, "why": (
                f"this runs {path}, which reads {', '.join(missing)} from its environment — and the "
                f"runner only passes a stored value into a command that MENTIONS it, so the script "
                f"starts with no such variable and fails. Put the reference in this command: "
                f'`{first}="${first}" {"bash" if path.endswith(".sh") else "python3"} {path}`. The value still never appears in the block; '
                f"the runner expands it on the machine.")})
    return out


#: §17.1257 — where a command hands source to another interpreter.
_PY_DASH_C = re.compile(r"python[0-9.]*\s+-c\s+('[^']*'|\"[^\"]*\")")
_PY_FILE_WRITE = re.compile(r"\btee\s+(?:-a\s+)?(\S+\.py)\b", re.I)


def _printf_lines(cmd: str) -> Optional[list[str]]:
    """The lines a `printf '%s\\n' "a" "b" …` writes, or None if unsplittable."""
    if "printf" not in cmd:
        return None
    body = cmd.split("printf", 1)[1].split("|", 1)[0]
    try:
        args = shlex.split(body)
    except ValueError:
        return None
    return args[1:] if len(args) > 1 else []


def python_payloads(cmd: str) -> list[tuple[str, Optional[str]]]:
    """``[(what it is, the Python source)]`` this command hands to an interpreter.

    A ``None`` source means the shell words could not even be split, which is a
    finding in itself.
    """
    out: list[tuple[str, Optional[str]]] = []
    m = _PY_DASH_C.search(cmd)
    if m:
        out.append(("the `python -c` payload", m.group(1)[1:-1]))
    target = _PY_FILE_WRITE.search(cmd)
    if target:
        lines = _printf_lines(cmd)
        if lines is None:
            out.append((f"the script written to {target.group(1)}", None))
        elif lines:
            out.append((f"the script written to {target.group(1)}", "\n".join(lines)))
    return out


def _py_sources(commands: list[str], files: Optional[list[dict]] = None) -> list[tuple[str, Optional[str], str]]:
    """§17.1274 — every piece of Python this block hands to an interpreter, from
    BOTH places it can come from: a ``-c`` / ``printf | tee`` payload inside a
    command (§17.1257) and a file written through the §17.1271 channel. Yields
    ``(what it is, the source, the label a refusal names)``.

    The gates below read only the commands until the live ADD115 frame came back
    ``refused: []`` with a 103-line script looping over every public indexer in
    ``files`` — the day the file channel opened, every payload gate went blind
    to the thing it was written for (feedback: a gate goes blind when code moves).
    """
    out: list[tuple[str, Optional[str], str]] = []
    for cmd in commands or []:
        for what, source in python_payloads(str(cmd)):
            out.append((what, source, str(cmd)))
    for f in files or []:
        path = str((f or {}).get("path") or "")
        if path.endswith(".py"):
            label = f"the file {path}"
            out.append((label, str((f or {}).get("content") or ""), label))
    return out


def _callee(call) -> Optional[str]:
    return getattr(call.func, "attr", None) or getattr(call.func, "id", None)


#: §17.1276 — modules whose calls wait on something off this machine, and the
#: constructors whose instances do (`s = requests.Session(); s.get(…)`).
_NETWORK_MODULES = frozenset({"requests", "httpx", "urllib", "urllib2", "urllib3", "http", "socket",
                              "subprocess", "aiohttp", "paramiko", "ftplib", "smtplib", "telnetlib"})
_NETWORK_CTORS = frozenset({"Session", "Client", "AsyncClient", "HTTPConnection", "HTTPSConnection",
                            "PoolManager", "ClientSession", "socket", "create_connection", "SSHClient"})
#: attribute names that are the network whoever the receiver is.
_NETWORK_UNAMBIGUOUS = frozenset({"urlopen", "getresponse", "sendall", "recv", "recv_into", "check_output",
                                  "check_call", "Popen", "exec_command", "urlretrieve"})


def _net_context(tree) -> tuple[set[str], set[str]]:
    """``(roots, bare)``: the names whose attribute calls reach the network (imported
    network modules and their aliases; receivers built from a network constructor),
    and the bare names imported FROM a network module (``from requests import get``).

    §17.1276 — the first live outing of the hang gate refused a draft that caught
    every hang correctly, because `entry.get("name")` matched the bare name
    list. `get` on a dict is not the network; `get` on `requests` is. Both gates
    now ask WHO is being called, not only what the method is named."""
    import ast
    roots: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in _NETWORK_MODULES:
                    roots.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in _NETWORK_MODULES:
                for a in node.names:
                    bare.add(a.asname or a.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            v = node.value
            if isinstance(v, ast.Call) and _callee(v) in _NETWORK_CTORS:
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(t, ast.Name):
                        roots.add(t.id)
    return roots, bare


def _root_name(expr) -> Optional[str]:
    import ast
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _is_network_call(call, ctx: tuple[set[str], set[str]], wrappers: set[str] = frozenset()) -> bool:
    """Does this call wait on something off the machine? A wrapper the source
    defines counts (§17.1274). An ambiguous method (`get`, `post`, `run`, `call`,
    `request`) counts only on a network receiver; an unambiguous one (`urlopen`,
    `check_output`, `Popen`) counts anywhere; a bare name counts when it was
    imported from a network module or is itself a wrapper."""
    import ast
    roots, bare = ctx
    f = call.func
    if isinstance(f, ast.Attribute):
        if f.attr in _NETWORK_UNAMBIGUOUS:
            return True
        return f.attr in _NETWORK_CALL and _root_name(f.value) in roots
    if isinstance(f, ast.Name):
        return f.id in wrappers or f.id in bare and f.id in _NETWORK_CALL | _NETWORK_UNAMBIGUOUS
    return False


#: §17.1268 — the calls that wait on something outside this machine. A loop
#: around one of these is where a command's time budget goes.
_NETWORK_CALL = frozenset({
    "urlopen", "request", "get", "post", "put", "delete", "patch",
    "head", "getresponse", "connect", "sendall", "recv", "check_output", "run",
    "call", "check_call", "Popen",
})
# §17.1276b — `urllib.request.Request(...)` BUILDS a request and sends nothing;
# the live draft built it outside the wrapper's `try` and the hang gate read
# that as an unguarded network call. Constructors are never the network.


def _literal_names(tree) -> set[str]:
    """Names this source assigns a literal collection to, and never anything else.

    `apps = [{…Radarr…}, {…Sonarr…}]` then `for app in apps:` is two items and
    always will be -- the apps half of ADD115, which must keep working. A name
    that is ALSO assigned something else (a response, a filter over one) is not
    counted, because then its size is whatever that was.
    """
    import ast
    literal: set[str] = set()
    other: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            (literal if isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
             else other).add(t.id)
    return literal - other


def _bounded(it, literal_names: set[str] | None = None) -> bool:
    """Is this iterable something whose size the draft itself fixed?

    A literal list or tuple is bounded by construction, whether it is written in
    the loop or assigned to a name just above it. A slice or `islice` is the
    author saying how many. A bare name holding whatever a service returned is
    not.
    """
    import ast
    names = literal_names or set()
    if isinstance(it, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return True
    if isinstance(it, ast.Subscript):                   # public[:20]
        return True
    if isinstance(it, ast.Name):
        return it.id in names
    if isinstance(it, ast.Call):
        name = getattr(it.func, "attr", None) or getattr(it.func, "id", None)
        if name == "islice":
            return True
        if name == "range":
            # §17.1277b — `range(0, len(public), 10)` is however long `public` is;
            # `range(10)` is ten. The live in-script batching hid behind the former.
            return bool(it.args) and all(_counts_as_bounded(a, names) for a in it.args)
        if name in ("enumerate", "list", "sorted", "reversed", "tuple"):
            return bool(it.args) and _bounded(it.args[0], names)
    return False


def _counts_as_bounded(expr, names: set[str]) -> bool:
    """A range argument the author fixed: a literal, a bounded name, or the
    length of something bounded."""
    import ast
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, (int, float))
    if isinstance(expr, ast.Name):
        return expr.id in names
    if isinstance(expr, ast.Call) and _callee(expr) == "len" and expr.args:
        return _bounded(expr.args[0], names)
    if isinstance(expr, ast.BinOp):
        return _counts_as_bounded(expr.left, names) and _counts_as_bounded(expr.right, names)
    return False


def _bounded_names(tree) -> set[str]:
    """§17.1277b — names whose EVERY assignment is itself bounded, to a fixed
    point. `batch = public[start:end]` then `for entry in batch:` is a slice the
    author chose, exactly as `for entry in public[start:end]:` is; the live
    redraft wrote the first form and the budget gate, which knew only literal
    collections by name, refused the correct answer. `public = [d for d in
    schema …]` stays unbounded: a comprehension over a response is the response."""
    import ast
    assigns: dict[str, list] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                assigns.setdefault(t.id, []).append(node.value)
    bounded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, vals in assigns.items():
            if name not in bounded and vals and all(v is not None and _bounded(v, bounded) for v in vals):
                bounded.add(name)
                changed = True
    return bounded


def _single_assignment(tree, name: str):
    """The value a name is assigned exactly once, else None."""
    import ast
    vals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    return vals[0] if len(vals) == 1 else None


def _literal_iter(it, literal_names: set[str]) -> bool:
    """Is this iterable a collection the draft WROTE OUT -- `apps = [radarr,
    sonarr]` -- as opposed to a slice of something a service returned? The
    former is this machine's own things; the latter is third parties, which is
    where §17.1263's survive-a-corpse rule applies."""
    import ast
    if isinstance(it, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return True
    if isinstance(it, ast.Name):
        return it.id in literal_names
    if isinstance(it, ast.Call):
        name = _callee(it)
        if name == "range":
            return True
        if name in ("enumerate", "list", "sorted", "reversed", "tuple"):
            return bool(it.args) and _literal_iter(it.args[0], literal_names)
    return False


def _network_wrappers(tree) -> set[str]:
    """§17.1274 — functions defined in this source whose body reaches a network
    call, directly or through another such function. ``def api_post(…):
    urlopen(…)`` then ``for entry in public: api_post(…)`` waits on the network
    exactly as much as the bare call does; a gate matching call NAMES passed the
    live script because the loop body said `api_post`, not `urlopen`."""
    import ast
    defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ctx = _net_context(tree)
    wrappers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, fn in defs.items():
            if name in wrappers:
                continue
            if any(isinstance(c, ast.Call) and _is_network_call(c, ctx, wrappers) for c in ast.walk(fn)):
                wrappers.add(name)
                changed = True
    return wrappers


def _slice_bound(it) -> Optional[int]:
    """How many items ``public[:20]`` is, when the author wrote the number."""
    import ast
    if isinstance(it, ast.Call) and _callee(it) in ("enumerate", "list", "sorted", "reversed", "tuple") and it.args:
        return _slice_bound(it.args[0])
    if isinstance(it, ast.Subscript) and isinstance(it.slice, ast.Slice):
        up, lo = it.slice.upper, it.slice.lower
        if isinstance(up, ast.Constant) and isinstance(up.value, int):
            start = lo.value if isinstance(lo, ast.Constant) and isinstance(lo.value, int) else 0
            return max(0, up.value - start)
    return None


def _request_timeouts(tree) -> list[float]:
    """Literal ``timeout=`` seconds on the network calls in this source."""
    import ast
    out: list[float] = []
    ctx = _net_context(tree)
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and _is_network_call(c, ctx):
            for kw in c.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
                    out.append(float(kw.value.value))
    return out


#: §17.1274 — handler types that survive a HANG. `HTTPError` is deliberately
#: absent: it is what the live script caught, and a socket timeout is not one.
_SURVIVES_A_HANG = frozenset({"Exception", "BaseException", "OSError", "IOError", "URLError", "TimeoutError",
                              "timeout", "error", "ConnectionError", "RequestException", "RemoteDisconnected"})


def _handlers_survive(try_node) -> bool:
    import ast
    for h in try_node.handlers:
        if h.type is None:
            return True                                 # bare except
        types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        if any((getattr(t, "attr", None) or getattr(t, "id", None)) in _SURVIVES_A_HANG for t in types):
            return True
    return False


def _call_guarded(call, scope) -> bool:
    """Is `call` inside a try, within `scope`, whose handlers would survive a hang?"""
    import ast
    for t in ast.walk(scope):
        if not isinstance(t, ast.Try):
            continue
        if any(c is call for stmt in t.body for c in ast.walk(stmt)) and _handlers_survive(t):
            return True
    return False


def _wrapper_survives(name: str, defs: dict, wrappers: set[str], ctx: tuple[set[str], set[str]],
                      seen: Optional[set] = None) -> bool:
    """Does this wrapper guard its OWN network calls (transitively)?"""
    import ast
    seen = seen or set()
    fn = defs.get(name)
    if fn is None or name in seen:
        return False
    seen.add(name)
    inner = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _is_network_call(c, ctx, wrappers)]
    if not inner:
        return True
    for c in inner:
        if _call_guarded(c, fn):
            continue
        if _callee(c) in wrappers and _wrapper_survives(_callee(c), defs, wrappers, ctx, seen):
            continue
        return False
    return True


def loop_dies_on_one_dead_party(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1274 — a loop over third parties that one of them can kill.

    Live, ADD115's script walked 89 public indexers, classified 20 corpses
    correctly off their HTTP bodies (§17.1263/1266) and then met a tracker that
    HUNG: ``urlopen`` raised ``TimeoutError``, which is not an ``HTTPError``, so
    the only ``except`` in the loop never fired — traceback, exit 1, step
    failed, 0 added, no summary. The rule it broke had been in CHANNEL_RULES
    since §17.1263 as prose; a rule the draft ignores is not a fail-safe
    (§17.1268), so this is the gate.

    Decidable from the source: a loop over something that is NOT a literal the
    draft wrote out (`apps = [radarr, sonarr]` is this machine's own things and
    is left alone) whose body reaches a network call — directly or through a
    wrapper (§17.1274's `_network_wrappers`) — with no enclosing ``try`` that
    would survive a hang, at the call site or inside the wrapper.
    """
    import ast
    out: list[dict] = []
    for what, source, label in _py_sources(commands, files):
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue                                    # §17.1257 reports that, not this
        names = _literal_names(tree)
        defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        ctx = _net_context(tree)
        wrappers = _network_wrappers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor)) or _literal_iter(node.iter, names):
                continue
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call) and _is_network_call(c, ctx, wrappers)]
            if not calls:
                continue
            exposed = []
            for c in calls:
                name = _callee(c)
                if _call_guarded(c, node):
                    continue
                if name in wrappers and _wrapper_survives(name, defs, wrappers, ctx):
                    continue
                exposed.append(name)
            if not exposed:
                continue
            where = ast.unparse(node.iter)[:60]
            out.append({"command": label, "why": (
                f"{what} loops over `{where}` -- things outside this machine -- and reaches the network "
                f"through `{exposed[0]}` with nothing catching a HANG. `urlopen` raises TimeoutError or "
                f"urllib.error.URLError when a tracker does not answer, which is not an HTTPError, so this run "
                f"dies at the first one that hangs, with no summary and no record of the rest -- live, that was "
                f"indexer 40 of 89. Catch `(urllib.error.URLError, TimeoutError, OSError)` around that call, INSIDE "
                f"the loop, and record the item as unreachable exactly like a 502 (the rule this block already "
                f"applies to HTTP errors).")})
            break                                       # one finding per payload is enough
    if out:
        logger.warning("network_loop_dies_on_hang count=%d first=%r", len(out), out[0]["why"][:120])
    return out


#: §17.1283 — the first helper that elevates a compound line as a whole.
WHOLE_LINE_SUDO_HELPER = 19

_SHELL_WORDS = frozenset({"do", "done", "then", "else", "elif", "fi", "break", "continue", "true", "false",
                          "esac", "exit", ":", "wait"})


def _shell_keyword_only(segment: str) -> bool:
    """A segment that is a shell keyword (or `exit N` / `true`) rather than a program."""
    words = (segment or "").strip().split()
    return bool(words) and words[0] in _SHELL_WORDS and all(w in _SHELL_WORDS or w.isdigit() for w in words)


_LOOP_HEAD_RE = re.compile(r"^(?:for|while|until|if|elif|select)\b")


def writing_segments(cmd: str) -> list[str]:
    """§17.1287c — the segments of a (compound) command that WRITE. The shell's
    own words (`do`, `done`, `break`), loop/branch heads (`for i in 1 2 3`,
    `if …`) and read-only programs are not writes. Placeholders are judged
    as a dummy value (§17.1187): `<PALWORLD_IP>` is a redirect to the parser.
    Live, the ADD82 redraft's wait -- `for i in 1 … 12; do ping -c 1 -W 2
    <PALWORLD_IP> >/dev/null 2>&1 && break; sleep 5; done` -- was refused by the
    guest gate as a write that reaches no guest: the whole loop is unjudgeable
    as one command, and the placeholder broke the ping's own judgment."""
    from app.modules.assist_state_check import read_only_command
    from app.modules.assist_supervised import split_segments
    shape = _PLACEHOLDER_RE.sub("x", str(cmd or ""))
    if read_only_command(shape):
        return []
    out: list[str] = []
    for seg in split_segments(shape):
        seg = seg.strip()
        if not seg or _shell_keyword_only(seg) or _LOOP_HEAD_RE.match(seg):
            continue
        if seg.startswith("do ") or seg.startswith("then "):
            seg = seg.split(" ", 1)[1].strip()
        if seg and not read_only_command(seg):
            out.append(seg)
    return out


def compound_write_on_a_head_only_runner(commands: list[str], policy: Optional[dict]) -> list[dict]:
    """§17.1283 — a runner older than helper 19 prefixes `sudo -n` to the
    command STRING, so only the first simple command of a line is elevated.
    Live: `qm status 110 | grep -q running || qm start 110` -- the guard form
    §17.1240 recommends -- ran its check as root and its `qm start` unprivileged
    (`ipcc_send_rec … Unable to load access control list`), and ADD26 failed on
    its first command. Until the runner is updated, a write after `|`, `||`,
    `&&` or `;` cannot run as root there; the remedy that needs no update is one
    command per line -- and the host inventory in the prompt already says whether
    the guest is running, so the guard is usually unnecessary.
    """
    pol = policy or {}
    try:
        helper = int(str(pol.get("helper") or "0"))
    except ValueError:
        helper = 0
    if not pol.get("sudo") or helper >= WHOLE_LINE_SUDO_HELPER:
        return []
    from app.modules.assist_supervised import split_segments
    out: list[dict] = []
    for cmd in commands or []:
        segs = split_segments(str(cmd))
        if len(segs) < 2:
            continue
        # §17.1284b — `do`, `done`, `break`, `then`, `fi` are the shell's own
        # words, not commands: the first live outing refused the drafter's wait
        # loop (`for …; do ping … && break; sleep 5; done`) for them, the
        # redraft dropped the wait, and ssh ran into a VM one second into its boot.
        # §17.1287c — one classifier with the guest gate (placeholders judged as a value).
        first = _PLACEHOLDER_RE.sub("x", segs[0]).strip()
        writes = [sg for sg in writing_segments(str(cmd)) if sg != first]
        if not writes:
            continue
        out.append({"command": str(cmd), "why": (
            f"this runner (helper {helper}) elevates only the first command of a line, so `{writes[0].strip()[:60]}` "
            f"after the operator would run unprivileged and fail on pmxcfs -- live, `qm start 110` did exactly that "
            f"behind `qm status 110 | grep -q running ||`. Write ONE command per line: the host inventory above says "
            f"whether the guest is running, so when it is stopped write the start alone (`qm start 110`), and when it "
            f"is running leave the start out. (Updating the runner to helper {WHOLE_LINE_SUDO_HELPER} -- Settings → "
            f"Machines, the install line -- elevates whole lines; until then, one command per line.)")})
    if out:
        logger.warning("compound_write_on_head_only_runner count=%d helper=%s", len(out), helper)
    return out


_GUEST_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
#: any `pct <verb> N` / `qm <verb> N` either reaches the guest (exec, enter,
#: guest exec) or is the host's own work ON it (start, set, config, …) — both
#: are the step addressing its subject.
_REACH_TOOL_INSTALL_RE = re.compile(r"\bapt(?:-get)?\s+install\b[^\n|;&]*\b(?:sshpass|nmap|openssh-client|arp-scan|fping)\b")
_GUEST_ADDRESS_RE = re.compile(r"\b(?:pct|qm)\s+(?:guest\s+exec|[a-z-]+)\s+(\d{3,5})\b")


def commands_never_reach_the_guest(commands: list[str], node: Optional[dict],
                                   files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1285 — a step ABOUT a guest whose commands all run on the host.

    Live, ADD82 "Install and enable QEMU Guest Agent in VM 106" drafted
    `sudo apt-get update` / `apt-get install -y qemu-guest-agent` /
    `systemctl enable --now qemu-guest-agent` -- four commands that would run in
    the runner's own shell on the Proxmox HOST, installing the agent on the
    wrong machine, with `qm agent 106 ping` as the check that would then
    contradict it. The subject was in the title, the inventory in the prompt,
    and §17.1213 (a block the host contradicts) has no view of this: nothing
    addressed 106 at all, so there was nothing to contradict.

    Narrow: the step's text names a guest id, at least one command changes a
    machine, and NO command addresses any guest (`pct exec N`, `qm guest exec N`,
    an `ssh` into a machine, or a host-side `qm`/`pct` operation ON the guest,
    which is legitimately the host's work about it).
    """
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    ids = sorted({m.group(1) for m in _GUEST_SUBJECT_RE.finditer(text)})
    if not ids or not commands:
        return []
    cmds = [str(c) for c in commands]
    writes = [c for c in cmds if writing_segments(c) and not _REACH_TOOL_INSTALL_RE.search(c)]
    # §17.1287c — a loop of reads is not a write. §17.1288i — installing the
    # MEANS of reaching the guest on the host (`apt-get install -y sshpass`,
    # nmap) is the rule's own remedy, not a write that misses the guest.
    if not writes:
        return []                                           # reads only: nothing is installed anywhere
    # §17.1287b — it is the WRITES that must reach the guest. The next live draft
    # was the same host-side install with `qm agent 106 ping` tacked on the end,
    # and that one read satisfied a gate that asked whether ANY command named
    # the guest. A write reaches a guest when it addresses one (`pct exec N`,
    # `qm guest exec N`, an ssh), IS the host's own operation on one (`qm set
    # N`, `pct start N`), or runs a written file that does.
    def _reaches(cmd: str) -> bool:
        from app.modules.runbook_preconditions import _resolve_ids   # §17.1290 — `GID=106` … `pct exec "$GID"` reaches 106
        texts = [_resolve_ids(cmd)] + [_resolve_ids(str((f or {}).get("content") or "")) for f in files or []
                                       if (f or {}).get("path") and str(f["path"]) in cmd]
        # §17.1291 — the guest's serial socket (`/var/run/qemu-server/106.serial0`) reaches the guest too
        return any(_GUEST_ADDRESS_RE.search(t) or re.search(r"\bssh\b", t) or re.search(r"qemu-server/\d{3,5}\.serial", t)
                   for t in texts)
    unreached = [c for c in writes if not _reaches(c)]
    if not unreached:
        return []
    gid = ids[0]
    return [{"command": unreached[0], "why": (
        f"`{unreached[0][:70]}` runs in the runner's own shell on the Proxmox HOST, and the step is about "
        f"VM/CT {gid} -- it would change the HOST instead of guest {gid} (live, ADD82's agent install would have "
        f"landed on the hypervisor) (a "
        f"`qm agent {gid} ping` beside it is a read and reaches nothing). Every command that CHANGES something must reach "
        f"the guest: a container with `pct exec {gid} -- <command>`; a VM with `qm guest exec {gid} -- <command>` "
        f"(needs the agent) or over ssh -- and a VM WITHOUT the agent is reached by finding its address from its MAC "
        f"(`qm config {gid}` → net0 MAC → `ip neigh show`, after `qm start {gid}` and a wait) and "
        f"`SSHPASS=\"$MASS_PASSWORD\" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new <user>@<address>`, "
        f"as ONE bash script under ## Write these files run with `MASS_PASSWORD=\"$MASS_PASSWORD\" bash /tmp/<name>.sh`. "
        f"A host-side operation ON the guest is `qm …/pct … {gid}`. The step is the operator's only when ssh itself "
        f"is refused -- say which command refused and why.")}]


def classifies_by_key_presence(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1278 — a block that tells a malformed request from a dead third party
    by whether the KEY `propertyName` appears in the body.

    Live: `elif status == 400 and "propertyName" in str(resp):` → "VALIDATION
    FAILURE" → `sys.exit(1)` on the FIRST indexer, whose body was
    `"propertyName": ""` + "Unable to connect to indexer … 502". The key is always
    there; the §17.1266 rule — and CHANNEL_RULES, in words — is whether a field is
    NAMED. The model implemented the opposite of the sentence it was given, so the
    sentence became a gate. Decidable from the source: a membership test of the
    literal string "propertyName" against anything.
    """
    import ast
    out: list[dict] = []
    for what, source, label in _py_sources(commands, files):
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        phrases: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not node.ops or not isinstance(node.ops[0], (ast.In, ast.NotIn)):
                continue
            left = node.left
            if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                continue
            if left.value.strip().lower() == "propertyname":
                out.append({"command": label, "why": (
                    f"{what} decides whose fault a 400 is by whether the KEY `propertyName` appears in the "
                    f"body (`{ast.unparse(node)[:80]}`) -- it always does: Prowlarr sends `\"propertyName\": \"\"` "
                    f"for a tracker it cannot reach, which is how the live run called `Unable to connect to "
                    f"indexer … 502` a validation failure and exited on the first of 89. Test whether a field "
                    f"is NAMED, not whether the key exists: `named = any((e.get('propertyName') or '').strip() "
                    f"for e in errors)` -- stop and print the body only when it is; otherwise record the item as "
                    f"unreachable and continue.")})
                break
            # §17.1278b — the same rule, broken the other way round: the field
            # test is right and then a PHRASE LIST decides availability, with
            # "unknown 400 → stop" as the default. Live, `blocked by CloudFlare
            # Protection` was not on the list and batch 1 stopped at indexer 2.
            if _UNREACHABLE.search(left.value) and not re.search(r"(?i)unique|already", left.value):
                phrases.append(left.value)
        # §17.1278c — the third way round: a 400 branch that never reads the field
        # at all. Prowlarr answers 400 for a dead tracker too, so "400 → validation
        # failure → stop" turns every corpse into a bad request.
        if not out and "propertyname" not in source.lower() and any(
                isinstance(n, ast.Compare) and any(isinstance(c, ast.Constant) and c.value == 400
                                                  for c in ast.walk(n))
                for n in ast.walk(tree)):
            out.append({"command": label, "why": (
                f"{what} decides what a 400 means without reading the body's `propertyName` at all -- and "
                f"Prowlarr answers 400 for a tracker it cannot REACH as well as for a bad request, so this block "
                f"turns every dead tracker into a 'validation failure' and stops at the first (live: 0Magnet, "
                f"`blocked by CloudFlare Protection`, `\"propertyName\": \"\"`). The verdict is in the body: "
                f"paste and call `{WHOSE_FAULT_HELPER.strip()}` -- 'unreachable' is recorded by name and the loop "
                f"continues; 'bad_request' stops and prints the body; 'duplicate' counts as already present.")})
        # §17.1279 — a pasted `whose_fault` that predates `needs_input` stops on a
        # template (live: "Torrent RSS Feed", `propertyName: BaseUrl`,
        # `attemptedValue: ""`, batch 9 of 9, connect_apps never ran).
        if not out and "def whose_fault" in source and "needs_input" not in source:
            out.append({"command": label, "why": (
                f"{what} carries an older `whose_fault` that has no 'needs_input' verdict, so a definition "
                f"that is a TEMPLATE -- a field NAMED with an EMPTY attemptedValue, like \"Torrent RSS Feed\" "
                f"wanting a BaseUrl only the operator can type -- is treated as a bad request and the whole run "
                f"stops on it. Replace it with the current helper and call it the same way: "
                f"`{WHOSE_FAULT_HELPER.strip()}` -- 'needs_input' is recorded as \"needs configuration: <name> "
                f"(<field>)\" and the loop continues.")})
        if phrases and not out:
            out.append({"command": label, "why": (
                f"{what} decides whether a tracker is unreachable by a phrase list ({', '.join(repr(p) for p in phrases[:4])}"
                f"{', …' if len(phrases) > 4 else ''}) -- and a phrase list is always one phrase short: live, "
                f"`Unable to access 16mag.net, blocked by CloudFlare Protection.` matched none of them and the "
                f"block stopped on the second indexer of ten, calling it a validation failure. The body's SHAPE "
                f"decides, not its words: a 400 whose `propertyName` is EMPTY is an availability error, whatever "
                f"the message says -- record the item as unreachable and continue; a 400 that NAMES a field is "
                f"a bad request -- stop and print it. No phrase list, and no 'unknown → stop' default.")})
    if out:
        logger.warning("classifies_by_key_presence count=%d", len(out))
    return out


def loops_the_network_without_a_budget(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1268 — a block that cannot finish in the time it is given.

    §17.1267 told the drafter a command gets 180 seconds and asked for batches.
    The very next draft looped over all 89 indexers again -- each POST making the
    service connection-test a remote tracker -- and put a `time.sleep(1)` INSIDE
    the loop, which is strictly worse than the attempt that had just been killed.
    A rule the prompt states and the draft ignores is not a fail-safe, so this is
    the gate (feedback: fail-safes are a registry; gates must bite).

    The question is the one that can be answered from the source: does a loop
    whose body waits on something off this machine run over a collection the
    draft itself did not bound? A literal list is bounded -- `for app in [radarr,
    sonarr]` is two items, which is why the apps half of this very step passes.
    A name holding whatever an API returned is not, and `public[:20]` is the
    remedy, stated by the author.

    Reuses §17.1257's payload extraction: the source is already in hand, and the
    AST it compiles is already proof the code is real.

    §17.1274 — reads written FILES as well as command payloads, follows the
    loop's calls through local wrappers (`api_post`), and does the arithmetic on
    a bounded slice: `public[:10]` at `timeout=60` is 600 seconds, not a batch.
    """
    import ast
    out: list[dict] = []
    for what, source, label in _py_sources(commands, files):
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue                      # §17.1257 reports that, not this
        names = _bounded_names(tree)            # §17.1277b — bounded through assignment, not merely literal
        ctx = _net_context(tree)
        wrappers = _network_wrappers(tree)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call) and _is_network_call(c, ctx, wrappers)]
            if not calls:
                continue
            # §17.1277b — the budget is spent by the OUTERMOST loop: `for i in
            # range(0, len(public), 10): for e in public[i:i+10]: …` runs the
            # whole list in one command however bounded the inner slice looks.
            chain, up = [node], parents.get(node)
            while up is not None:
                if isinstance(up, (ast.For, ast.AsyncFor)):
                    chain.append(up)
                up = parents.get(up)
            unbounded = [f for f in chain if not _bounded(f.iter, names)]
            if unbounded:
                node = unbounded[-1]                  # name the outermost one
            where = ast.unparse(node.iter)[:60]
            # the thing to slice is the collection, not `enumerate(collection)`
            coll = (ast.unparse(node.iter.args[0])[:60]
                    if isinstance(node.iter, ast.Call) and _callee(node.iter) in ("enumerate", "list", "sorted", "reversed", "tuple")
                    and node.iter.args else where)
            if not unbounded:
                # the arithmetic sees through `batch = public[:10]`
                it = node.iter
                if isinstance(it, ast.Name) and _single_assignment(tree, it.id) is not None:
                    it = _single_assignment(tree, it.id)
                n, tos = _slice_bound(it), _request_timeouts(tree)
                if n and tos and n * max(tos) > _RUN_BUDGET_S:
                    out.append({"command": label, "why": (
                        f"{what} loops over `{where}` -- {n} items -- and each request may wait "
                        f"`timeout={max(tos):g}` seconds: {n * max(tos):g} seconds against a {_RUN_BUDGET_S}-second "
                        f"budget if every item hangs, and one command is KILLED at {_RUN_BUDGET_S}. Give each request "
                        f"a short timeout (`timeout=15`) or take fewer items, so the batch finishes well inside the "
                        f"budget even when every item waits on something off this machine.")})
                    break
                continue
            sleeps = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                      and getattr(c.func, "attr", None) == "sleep"]
            via = sorted({_callee(c) for c in calls if _callee(c) in wrappers})
            is_file = label.startswith("the file ")
            path = label[len("the file "):] if is_file else "/tmp/x.py"
            # §17.1276 — the remedy has to fit the channel. Told "send several
            # commands", the live redraft batched INSIDE one script (`for i in
            # range(0, len(public), 10)`) -- still one command, still one budget.
            batching = (
                f" On the file channel that means: write the script ONCE taking the slice bounds as "
                f"arguments (`start, end = int(sys.argv[1]), int(sys.argv[2])`; `for item in {coll}[start:end]:`), "
                f"and list one command per batch under ## Run this -- `python3 {path} 0 10`, `python3 {path} 10 20`, "
                f"… -- never a loop over batches inside one script, because one command is one budget."
                if is_file else "")
            out.append({"command": label, "why": (
                f"{what} loops over `{where}` -- however many that service returns -- and every pass "
                f"waits on something off this machine"
                + (f" (through `{via[0]}`)" if via else "")
                + (", with a sleep inside the loop as well" if sleeps else "")
                + f". One command gets {_RUN_BUDGET_S} seconds and is KILLED at that point, with no "
                f"record of how much of the work landed, so a run over 89 of anything cannot be "
                f"offered. Bound it: take a slice of a size YOU choose (`{coll}[:20]`, or fewer) and send "
                f"several commands, each one resumable -- treat a thing that is already there as "
                f"success and move on. Pick the size so a batch finishes well inside the budget: if "
                f"every item waits on a remote service that may answer slowly or not at all, ten is "
                f"safer than twenty. Then a batch that runs out of time loses only itself." + batching)})
            break                             # one finding per payload is enough
    if out:
        logger.warning("network_loop_without_budget count=%d first=%r", len(out), out[0]["why"][:120])
    return out


def _unescape_in_single_quotes(cmd: str) -> str:
    r"""``\"`` inside a ``'…'`` shell word becomes ``"``; everything else is left
    exactly as it was.

    POSIX single quotes have no escapes at all, so a backslash between them is a
    literal backslash and reaches the interpreter. Removing it is the whole
    repair, and it is only ever applied where the quoting says the backslash
    cannot have been meant.
    """
    out: list[str] = []
    i, in_single = 0, False
    while i < len(cmd):
        c = cmd[i]
        if c == "'":
            in_single = not in_single
            out.append(c)
            i += 1
            continue
        if in_single and c == "\\" and i + 1 < len(cmd) and cmd[i + 1] == '"':
            out.append('"')                 # drop the backslash, keep the quote
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def repair_shell_quoted_payloads(commands: list[str]) -> tuple[list[str], list[dict]]:
    """§17.1270 — fix the engine's own recurring quoting mistake instead of
    refusing it for the fourth time.

    THREE of four drafts for ADD115 died on the same thing: a script written with
    ``printf '%s\n' 'line' 'line' …`` containing ``print(f"added: {entry[\"name\"]}")``.
    Inside a single-quoted shell word that backslash is literal, so the
    interpreter is handed ``{entry[\"name\"]}`` and refuses it. §17.1257 catches it
    every time, §17.1255's rule and a worked example are both in the prompt, and
    the redraft makes the same mistake again -- so the prompt is not where this
    gets fixed.

    It does not need judgment. POSIX single quotes have no escapes, the backslash
    cannot have been intended, and removing it is a transformation whose result
    is PROVEN before use: the repaired command's payload must compile when the
    original's did not. If it does not compile, nothing is changed and §17.1257
    refuses as before. That is the deterministic half of the split this project
    already relies on -- propagate what is provable, ask about what is not.

    Returns the commands to use and a record of every repair, for the log and for
    the operator-facing note (a changed command is never a silent change).
    """
    out: list[str] = []
    repairs: list[dict] = []
    for cmd in commands or []:
        text_value = str(cmd)
        if not payload_will_not_compile([text_value]):
            out.append(text_value)
            continue
        candidate = _unescape_in_single_quotes(text_value)
        if candidate != text_value and not payload_will_not_compile([candidate]):
            out.append(candidate)
            repairs.append({"command": text_value, "repaired": candidate, "why": (
                "a quote was escaped inside a single-quoted shell word, where a backslash is "
                "literal and reaches the interpreter. The backslashes were removed and the "
                "payload compiles; nothing else was changed.")})
        else:
            out.append(text_value)          # §17.1257 reports it, unrepaired
    if repairs:
        logger.warning("payload_quotes_repaired count=%d first=%r",
                       len(repairs), repairs[0]["command"][:120])
    return out, repairs


def payload_will_not_compile(commands: list[str]) -> list[dict]:
    """§17.1257 — compile the code the block hands to another interpreter.

    THE UNDERLYING ISSUE behind a run of near-identical defects. The engine
    generates source for a second interpreter — `python3 -c '…'`, or a script
    written out with `printf … | tee x.py` — and validated only the SHELL. Its own
    shell parser says every one of these commands is fine, because they are:
    `parse_error=False`. The defect is in the Python being handed over, and
    nothing ever looked at it.

    So each new way of malforming that payload needed its own pattern. §17.1255
    added one for `-c` payloads with escaped quotes; §17.1255b added another when
    the identical mistake appeared in a written script; three drafts in a row
    produced it anyway, and so did three of my own attempts at the rule. That is
    not a sequence of unrelated bugs, it is one missing check.

    `ast.parse` answers it directly and generically, with the real compiler
    message and a line number, and it catches shapes nobody has seen yet. It
    supersedes §17.1255 and §17.1255b, which are deleted.

    Note what this does NOT do: a payload can compile perfectly and still be
    wrong (§17.1248's pipe crossing a guest boundary, §17.1256's missing secret,
    §17.1234's silent HTTP failure). Those are semantics, not syntax, and they
    keep their own rules. This closes the syntax family only — but it closes it
    properly rather than one shape at a time.
    """
    out: list[dict] = []
    for c in commands or []:
        cmd = str(c)
        for what, src in python_payloads(cmd):
            if src is None:
                out.append({"command": cmd, "why": (
                    f"{what} cannot even be split into shell words — the quoting is unbalanced, so "
                    f"nothing can tell what the file would contain. Rewrite it with one short "
                    f"argument per line and no escaped quotes.")})
                continue
            try:
                ast.parse(src)
            except SyntaxError as exc:
                line = (src.splitlines()[exc.lineno - 1].strip()
                        if exc.lineno and exc.lineno <= len(src.splitlines()) else "")
                out.append({"command": cmd, "why": (
                    f"{what} is not valid Python and would fail the moment it ran: "
                    f"{exc.msg} (line {exc.lineno})"
                    + (f" -- {line[:120]!r}" if line else "")
                    + ". This was compiled before offering it, so the error is certain, not a "
                      "guess. Fix the source: inside a single-quoted shell argument a backslash is "
                      "literal, so never escape a quote there.")})
            except (ValueError, RecursionError) as exc:
                out.append({"command": cmd, "why": (
                    f"{what} could not be compiled ({type(exc).__name__}), so it cannot be offered "
                    f"as runnable.")})
    return out


#: §17.1258 — the same failure, over and over, instead of one diagnosis.
_FAIL_LINE = re.compile(
    r"(?im)^\s*(?:failed|error|skip(?:ped)?)\b[:\s].*?"
    r"((?:HTTP\s*(?:Error\s*)?\d{3})|(?:\b[45]\d\d\b)|(?:Bad Request)|(?:Unauthorized)|(?:Forbidden))")


#: §17.1263 — a status the REMOTE end owns. 400/401/403/422 say the request was
#: wrong: ours to fix, and identical every time it is sent. 502/503/504, a
#: refused connection or a timeout say the thing being configured is DOWN, which
#: is that thing's problem. Live, ADD115: the first attempt sent 88 malformed
#: bodies (one diagnosis, discarded 88 times — §17.1258 is right about that), and
#: the attempt after it hit genuinely dead trackers. Treating the second like the
#: first stops the step on the first corpse and adds none of the ones that work.
_UNREACHABLE = re.compile(
    r"(?i)\b(?:50[234]|bad gateway|service unavailable|gateway time-?out|timed?\s*out|"
    r"timeout|connection refused|connection reset|no route to host|unable to connect|"
    r"could not resolve|name or service not known|temporary failure in name resolution|"
    # §17.1266 — measured on the real run. ADD115's second indexer came back
    # "Unable to access 16mag.net, blocked by CloudFlare Protection", which none
    # of the above matches, so a tracker that is simply unusable was classified
    # as a malformed request and the step stopped at 2 of 89. A phrase list is
    # always one phrase short; these are the classes a THIRD PARTY owns.
    r"unable to (?:access|reach|retrieve|fetch)|blocked by|cloudflare|captcha|"
    r"forbidden|unauthorized by|no such host|certificate|ssl error|handshake|"
    r"site is down|offline|not responding|dns)\b")

#: §17.1263 — a line reporting that one thing actually landed. If any did, the
#: block was not spinning on one mistake; it was working through a list.
_OK_LINE = re.compile(
    r"(?im)^\s*(?:added|created|ok|success(?:fully)?|configured|installed|enabled|done)\b[:\s]")


_EMPTY_PROP = re.compile(r'"propertyName"\s*:\s*""')
_ERR_MSG = re.compile(r'"errorMessage"\s*:\s*"((?:[^"\\]|\\.)*)"')
_STOPPED_ON = re.compile(r"(?im)^\s*(?:VALIDATION FAILURE|VALIDATION ERROR|BAD REQUEST|FAILED|ERROR|STOPPED)[^\n:]*? on ([^:\n]+):")


_NAMED_EMPTY = re.compile(r'"propertyName"\s*:\s*"([A-Za-z][\w ]*)"[^}]*?"attemptedValue"\s*:\s*""', re.S)


def stopped_on_a_template(output: str) -> Optional[str]:
    """§17.1279 — the block exited on a definition that wants a value only the
    operator has. Live: batches 1–8 clean (49 already present, 38 dead and
    skipped by name), batch 9 stopped on "Torrent RSS Feed" -- `propertyName:
    BaseUrl`, `attemptedValue: ""` -- a generic template, not a bad request,
    and the tenth command (connect the apps) never ran."""
    o = output or ""
    if _EMPTY_PROP.search(o) and not _NAMED_EMPTY.search(o):
        return None
    m = _NAMED_EMPTY.search(o)
    if not m:
        return None
    field = m.group(1)
    who = _STOPPED_ON.search(o)
    name = who.group(1).strip() if who else "that definition"
    return (f"the block stopped on {name}, a definition whose `{field}` must be TYPED (`attemptedValue: \"\"`) -- "
            f"a generic template, not a bad request: nothing in the body was wrong, the definition simply wants a "
            f"value only the operator has. Record it as \"needs configuration: {name} ({field})\" and keep going; "
            f"the commands after this one never ran.")


def stopped_on_a_dead_party(output: str) -> Optional[str]:
    """§17.1278 — the block exited on an AVAILABILITY error it called a bad request.

    Live: batch 1 of the first clean argv block stopped on its FIRST indexer —
    `"propertyName": ""` with "Unable to connect to indexer … 502" — printed
    "VALIDATION FAILURE" and exited 1. Nothing in the body was wrong; the tracker
    was dead. The one-line reason the operator and the next draft would have seen
    was the raw JSON. This names what actually happened, so §17.1247 carries the
    right correction into the next draft: no field was named, so skip and go on.
    """
    o = output or ""
    if not _EMPTY_PROP.search(o):
        return None
    dead = [m.group(1) for m in _ERR_MSG.finditer(o) if _UNREACHABLE.search(m.group(1))]
    if not dead:
        return None
    m = _STOPPED_ON.search(o)
    who = m.group(1).strip() if m else "that item"
    return (f"the block stopped on {who}, which answered an AVAILABILITY error -- "
            f"{dead[-1][:120]} -- with no field named (`\"propertyName\": \"\"`). That is the tracker's "
            f"problem, not the request's: nothing in the body was wrong, and a run that stops here adds "
            f"none of the trackers that work. Record it as unreachable by name and keep going through the "
            f"rest of the list; stop and print the body only when the service NAMES a field.")


_HEREDOC_EOF_RE = re.compile(r"here-document at line \d+ delimited by end-of-file \(wanted [`'\"]?(\w+)", re.I)


def script_was_cut(executed: list[dict]) -> str:
    """§17.1310 — the reason, or ``""``: a command whose output carries bash's
    unterminated-heredoc warning did not run what came after the opener, whatever
    its exit code says."""
    for e in executed or []:
        m = _HEREDOC_EOF_RE.search(str(e.get("output") or ""))
        if m:
            return (f"`{str(e.get('command') or '')[:80]}` was cut: bash reported a here-document (`{m.group(1)}`) "
                    f"delimited by end-of-file, so everything after the opener ran as text and nothing it was "
                    f"meant to do happened; the exit code 0 is the warning's, not the work's.")
    return ""


def repeated_identical_failures(output: str, *, threshold: int = 3) -> Optional[str]:
    """§17.1258 — a block that repeated a failing call instead of stopping at it.

    ADD115's script read Prowlarr's schema correctly and then POSTed all 88 public
    definitions. Every one came back `HTTP Error 400: Bad Request`, and the output
    was 88 lines of

        failed: Anidex - HTTP Error 400: Bad Request
        failed: NewStudio - HTTP Error 400: Bad Request
        …

    Prowlarr says in the response BODY exactly which property it rejected -- the
    first attempt at this step had already been told `'App Profile Id' must be
    greater than '0'` -- and the script discarded that 88 times over. The
    operator got no diagnosis and the engine learned nothing it could act on.

    One failure is information. The same failure 88 times is the same information,
    with the useful part thrown away. So when a run's output shows one error
    repeated, the step fails with THAT as the reason, which §17.1247 then carries
    into the next draft.
    """
    counts: dict[str, int] = {}
    for m in _FAIL_LINE.finditer(output or ""):
        counts[m.group(1).strip().lower()] = counts.get(m.group(1).strip().lower(), 0) + 1
    if not counts:
        return None
    # §17.1263 — a dead third party is not the engine repeating its own mistake.
    # Once something in the run has landed, an unreachable remote is a thing to
    # skip and report, not a reason to abandon the rest of the list.
    if _OK_LINE.search(output or ""):
        counts = {e: c for e, c in counts.items() if not _UNREACHABLE.search(e)}
        if not counts:
            return None
    err, n = max(counts.items(), key=lambda kv: kv[1])
    if n < threshold:
        return None
    return (
        f"the block hit the SAME failure {n} times -- {err!r} -- and carried on instead of stopping "
        f"at the first one. One failure is information; the same failure {n} times is the same "
        f"information with the useful part discarded. The service says in its RESPONSE BODY which "
        f"field it rejected, and that body was never shown.\n\n"
        f"Next attempt: do the operation ONCE, and if it fails print the full response body and "
        f"stop. Fix the body from what the service says, then do the rest. Do not write a loop that "
        f"swallows an error into a one-line summary.")


async def wrote_instructions_instead_of_doing_it(output: str, node: dict, db) -> Optional[str]:
    """§17.1259 — a step that wrote a runbook for a machine the engine can reach.

    THE honest answer to "a step can be marked finished having done nothing". A
    step with no shell backend writes instructions and the node still goes to
    `done`. The `runbook_only` flag exists in the SSE payload; the STATUS does
    not know, so the job's counts and every downstream dependency treat
    instructions as work.

    Live, and not cosmetic. ADD116 ("give the media-stack containers working
    DNS") was marked done having produced 1,582 characters of prose. DNS stayed
    broken on all five containers -- and because it was `done`, ADD115 unblocked
    and ran straight into the wall ADD116 was created to remove. ADD97 and ADD98
    did the same earlier; ADD98 is "prove it end to end: ask for one film and
    watch it arrive", recorded finished having proven nothing.

    The discriminator is WHOSE machine. If the prose carries commands for a host
    the runner can reach, the engine could have run them and chose to describe
    them instead -- that is not done. If the work is elsewhere (an app on the
    operator's phone, a router with no API), prose is the correct and only
    output, so the check stays silent.

    Fail-soft: no channel, no commands in the prose, or an unreadable channel all
    leave the step exactly as it was.
    """
    text_value = str(output or "")
    if not text_value.strip() or "## Executed on" in text_value:
        return None                      # it really ran; nothing to judge here
    try:
        from app.modules.step_classify import step_is_hands_on
        cmds = runbook_commands(text_value)
        if not cmds:
            return None                  # pure guidance, nothing it could have run
        ch = await channel(db)
        if ch is None:
            return None                  # no write channel: prose is all it could do
        spec, _policy = ch
        pre = await _unmet_for(cmds, spec)
    except Exception as exc:
        logger.warning("instructions_check_failed err=%r", exc)
        return None
    on, _why = step_is_hands_on(dict(node) if hasattr(node, "keys") else {})
    reachable = [c for c in cmds if _targets_this_host(c)]
    if not reachable:
        return None                      # the work is on something else entirely
    return (
        f"this step produced INSTRUCTIONS, not work. It wrote {len(cmds)} command"
        f"{'' if len(cmds) == 1 else 's'} for a machine the engine can reach"
        + (f" (for example `{reachable[0][:70]}`)" if reachable else "")
        + ", the write channel to that machine is open, and none of them ran. A step that describes "
          "what should happen has not made it happen, so it is not done -- and anything depending on "
          "it would start from a false premise.\n\n"
        "Either run it through the channel, or if it genuinely cannot be run there say so in one "
        "line and name what blocks it."
        + (f"\n\nThe host also already contradicts part of it: {pre[0]['why'][:160]}" if pre else ""))


def _targets_this_host(cmd: str) -> bool:
    """Is this a command for the Proxmox host or a guest on it?"""
    head = (str(cmd).strip().split() or [""])[0]
    return head in {"pct", "qm", "pvesm", "pveam", "pvesh", "pvenode", "systemctl",
                    "ip", "sed", "tee", "printf", "apt-get", "apt", "curl", "dig", "getent"}


async def _unmet_for(cmds: list[str], spec) -> list[dict]:
    try:
        from app.modules.runbook_preconditions import unmet
        return await unmet(cmds, spec)
    except Exception:
        return []


def pipe_escapes_the_guest(commands: list[str]) -> list[dict]:
    """§17.1248 — a pipe after `pct exec` runs the right-hand side on the HOST.

    Live, ADD111's resuming draft:

        pct exec 130 -- curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended

    The shell splits that into `pct exec 130 -- curl …` and `bash /dev/stdin
    --unattended`. So `curl` fetches the Pi-hole installer inside container 130
    and hands it to a shell on the PROXMOX HOST — which would have installed
    Pi-hole on the host itself, taking port 53 and the host's resolver with it.
    The attempt before this one had it right, inside `bash -c "…"`; the redraft
    moved the pipe out and nothing noticed, because every existing check judges
    the segments separately and each of these segments is individually fine.

    That is the whole point: the danger is not in either half, it is in the
    boundary between them. Refused as a SHAPE problem so §17.1196's redraft
    fixes it, with the correction named — put the pipeline inside the guest.
    """
    from app.modules.assist_supervised import split_segments
    out: list[dict] = []
    for c in commands or []:
        segs = [x.strip() for x in split_segments(str(c)) if x.strip()]
        if len(segs) < 2 or not _GUEST_ENTRY.search(segs[0]):
            continue
        # only a PIPE carries data across; `&&` / `;` just sequence host commands,
        # which is a different (and legitimate) thing.
        if "|" not in re.sub(r"\|\|", "", str(c)):
            continue
        after = [x for x in segs[1:] if _INTERPRETER.search(x)]
        if not after:
            continue
        guest = re.search(r"\b(\d{3,5})\b", segs[0])
        gid = guest.group(1) if guest else "the guest"
        out.append({"command": str(c), "why": (
            f"this pipes out of {gid} and into `{after[0].split()[0]}` ON THE HOST. Everything after "
            f"`--` runs inside the guest; everything after the `|` does not — so the script fetched "
            f"in {gid} would be executed by the Proxmox host itself. Put the whole pipeline inside "
            f"the guest instead: `pct exec {gid} -- bash -c \"… | {after[0]}\"`, one self-contained "
            f"command, and nothing crosses the boundary.")})
    return out


def curl_writes_without_fail(commands: list[str]) -> list[dict]:
    """§17.1234 — ``[{command, why}]`` for every write-shaped `curl` whose exit
    code cannot report an HTTP error.

    The root cause under §17.1233: `curl -s` exits 0 when the server answers
    400, so the runner reports success for a request the service rejected. Nine
    of them in one block on ADD96, all rejected, the step marked done. Reported
    as a SHAPE refusal, which the existing §17.1196 redraft already feeds back
    to the drafter — so the engine fixes its own block instead of the operator
    discovering it later.
    """
    out: list[dict] = []
    for c in commands or []:
        cmd = str(c)
        if _CURL_WRITE.search(cmd) and not _CURL_FAILS.search(cmd):
            out.append({"command": cmd, "why": (
                "this `curl` sends a change but cannot report an HTTP error: `curl -s` exits 0 "
                "even when the server answers 400 or 401, so a request the service REJECTED "
                "would be recorded as done. Add `--fail-with-body` so the failure is a non-zero "
                "exit and the server's message is still shown.")})
    return out


async def host_inventory(spec) -> str:
    """§17.1232 — the guests that exist, for the drafter that keeps guessing.

    Live, ADD96's third draw: `curl -s -X POST http://<PROXMOX_HOST_IP>:9696/api/v1/indexer`.
    Prowlarr is container 102 on that host, Radarr 103, Sonarr 104 — one `pct
    list` says so, through the channel the engine had open. The drafter did not
    know any guest existed, so it addressed a service listening inside a
    container at the host's own address, and named the placeholder to match.
    Nothing downstream can repair that: discovery resolves a name to a machine,
    and the name pointed at the wrong machine.

    Read-only, both listings, fail-soft to "" — a drafter without the inventory
    writes what it wrote before.
    """
    if spec is None:
        return ""
    try:
        from app.modules.runbook_discovery import _read, guests_by_name
        cts = await _read(spec, "pct list")
        vms = await _read(spec, "qm list")
    except Exception as exc:
        logger.warning("host_inventory_failed err=%r", exc)
        return ""
    lines: list[str] = []
    for name, cid in sorted(guests_by_name(cts).items(), key=lambda kv: kv[1]):
        lines.append(f"  container {cid} — {name}")
    for m in re.finditer(r"^\s*(\d{3,5})\s+(\S+)\s+(\S+)", vms or "", re.M):
        if m.group(2).lower() != "name":
            lines.append(f"  VM {m.group(1)} — {m.group(2)} ({m.group(3)})")
    if not lines:
        return ""
    return ("\n\nGUESTS ON THIS HOST (read just now, and this is the whole list):\n"
            + "\n".join(lines)
            + "\n\nAddress a service at the guest it runs in. If a name here matches the "
              "service your step is about, the placeholder for its address must be named after "
              "that guest — `<NAME_IP>` — so the engine can read the address off the host and "
              "fill it in. Use `pct exec <id> -- …` to act inside a container and `qm` for a VM; "
              "`pct` cannot address a VM and `qm` cannot address a container.")


async def known_secret_names() -> list[dict]:
    """§17.1222 — ``[{name, hint}]`` for every value some store already holds.

    NAMES ONLY. The value never leaves its store: the engine writes `$NAME` and
    the runner expands it on the machine (§17.1191). Fail-soft — a drafter that
    cannot read the store simply asks, which is the old behaviour.
    """
    try:
        from app.database import async_session
        from app.modules import runner_secrets as _rs
        async with async_session() as db:
            rows = await _rs.list_secrets(db)
        return [{"name": r.get("name"), "hint": (r.get("hint") or "")[:120]}
                for r in rows if r.get("name")]
    except Exception:
        return []


async def stored_values_block(*, for_commands: bool = True) -> str:
    """§17.1224 — the ONE rendering of "the operator already gave us this".

    §17.1222 taught the runbook drafter the names of the stored values. Its own
    docstring names the sibling it mirrors — "the executor's own node
    generation" — and that sibling was never taught them, so the awareness
    existed on exactly one of the three prompt paths. Live, on the home-lab
    job: ADD102 ("Put the download client behind AirVPN") was written up as
    prose by the executor, and its first two instructions were

        Go to Config Generator … generate a configuration. Download the
        resulting `.conf` file.
        Go to Ports and request a new forwarded port.

    while `AIRVPN_WG_CONF` sat in the store, labelled, from the operator's own
    upload minutes earlier. Asking someone to go and fetch what they already
    handed you is the precise complaint §17.1222 was written to answer, and it
    survived because the fix was applied at one call site instead of the shared
    layer (feedback: sibling call sites drift).

    Names only — a value never enters a prompt. Fail-soft: a store that cannot
    be read yields "", which is the old behaviour of asking.
    """
    try:
        names = await known_secret_names()
    except Exception as exc:
        logger.warning("known_secret_names_failed err=%r", exc)
        return ""
    if not names:
        return ""
    listed = "\n".join(f"  ${n['name']}" + (f"  ({n['hint']})" if n.get("hint") else "")
                       for n in names)
    how = (
        "Write `$NAME` directly in the command; it is expanded on the machine at run "
        "time and never appears in the block, the transcript or any log. Do not invent "
        "a placeholder for something already on this list, and do not write the value "
        "itself even if you think you know it."
        if for_commands else
        "Refer to it as `$NAME`. The operator already supplied it and it is held for "
        "them — do NOT write a step that tells them to create it, generate it, download "
        "it, look it up or type it again, and do not write the value itself."
    )
    return ("\n\nVALUES ALREADY STORED — reference these by name and NEVER ask the "
            "operator for them again:\n" + listed + "\n\n" + how)


def executed_commands(report: str) -> list[str]:
    """The commands an earlier attempt actually sent, in order, from its own
    ``## Executed on`` report. `run_block` stops at the first failure, so the
    LAST one is where it stopped and everything before it happened."""
    i = (report or "").find("## Executed on")
    if i < 0:
        return []
    body = report[i:]
    end = body.find("\n## ", 1)
    if end > 0:
        body = body[:end]
    return [m.group(1).strip() for m in re.finditer(r"^\$ (.+)$", body, re.M)]


async def recover_prior_attempt(db: AsyncSession, job_id: str, node: dict) -> dict:
    """§17.1260 — put back what `reset` deleted, so the next draft can read it.

    The operator: "With the fails, shouldn't the engine also be using the
    research component to assist it?" It does. `diagnose_failure` researched
    ADD115's failure and produced a 6,548-character diagnosis. And then the retry
    path deleted it: `_reset_keys` sets `output_text = NULL` and
    `last_verification_reason = NULL`, which are precisely the two fields
    §17.1247 reads to tell the next draft what happened.

    So §17.1247 worked after a `reask` -- the node keeps its record -- and was
    INERT after a `reset`, which is the ordinary way to retry a step. ADD115 has
    seven reset pre-images, the largest holding 23,201 bytes of executed report
    and researched diagnosis, every one of them written to `dag_node_edits` by
    §17.1211 and never read back.

    Nothing is lost, it was simply in the wrong place. This reads the most recent
    `reset` pre-image and fills the blanks, so the engine stops paying for
    research it then throws away. Fail-soft: anything unreadable leaves the node
    as it was.
    """
    out = dict(node)
    if str(out.get("output_text") or "").strip() and str(out.get("last_verification_reason") or "").strip():
        return out                       # the live row still has it
    try:
        row = (await db.execute(
            text("SELECT before FROM dag_node_edits "
                 " WHERE job_id = :j AND node_key = :nk AND op = 'reset' "
                 "   AND before ? 'output_text' "
                 " ORDER BY created_at DESC LIMIT 1"),
            {"j": job_id, "nk": out.get("node_key")})).scalar()
    except Exception as exc:
        logger.warning("prior_attempt_unreadable job=%s node=%s err=%r",
                       job_id, out.get("node_key"), exc)
        return out
    before = _as_dict(row)
    if not before:
        return out
    for field in ("output_text", "last_verification_reason"):
        if not str(out.get(field) or "").strip() and str(before.get(field) or "").strip():
            out[field] = before[field]
    if out is not node:
        logger.warning("prior_attempt_recovered job=%s node=%s chars=%d",
                       job_id, out.get("node_key"), len(str(out.get("output_text") or "")))
    return out


def diagnosis_of(report: str) -> str:
    """§17.1260 — the researched diagnosis an earlier attempt produced.

    `diagnose_failure` writes it under "## What went wrong, and what to try" --
    that is where the RESEARCH lands. `attempt_feedback` carried the one-line
    reason and the command list and skipped this entirely, so the engine
    researched a failure and then told the next draft only the headline.
    """
    i = (report or "").find("## What went wrong")
    if i < 0:
        return ""
    body = report[i:]
    nxt = body.find("\n## ", 4)
    return (body[:nxt] if nxt > 0 else body).strip()


#: §17.1268 — the one place the per-command budget is named for prose and gate
#: alike; `assist_supervised.RUN_COMMAND_TIMEOUT_S` is what actually enforces it
#: and a test ties the two together.
_RUN_BUDGET_S = 180

#: §17.1267 — the runner's own words when a command runs out of time.
_TIMED_OUT = re.compile(r"(?i)timed?\s*out after (\d+)\s*s")


def attempt_feedback(node: dict) -> str:
    """§17.1247 — what the LAST attempt at this step did, and why it stopped.

    The gap this closes is the one that made a human the feedback loop all
    evening. A supervised run that fails on the machine records its reason on the
    node and its diagnosis in the output; `pending_hands_on` even SELECTs
    `retry_count` and `last_verification_reason` onto the dict it hands the
    drafter — and `draft_runbook` never read either. It calls `build_base_prompt`
    directly, so it never sees `_format_reviewer_feedback`, which only the
    ordinary LLM node path uses. Every redraft of a failed hands-on step was a
    FIRST attempt from the model's point of view.

    ADD111 needed five runs. Four of them re-made a mistake the engine had
    already seen: an invented template version, a dropped `pveam download`, a
    privileged read that had already been refused. The engine held all of it. The
    only path from the failure to the next draft was a person pasting it into the
    step's description by hand.

    Gated on the REASON, not on `retry_count`: `reset_node` deliberately does not
    bump the counter, and a reset is how a supervised step gets another attempt,
    so `_format_reviewer_feedback`'s `retry_count > 0` would have stayed silent
    here even if it had been wired in.
    """
    reason = str(node.get("last_verification_reason") or "").strip()
    if not reason:
        return ""
    ran = executed_commands(str(node.get("output_text") or ""))
    # §17.1267 — a timeout is not a mistake in the commands, it is work that did
    # not fit the budget, and the remedy is specific. Live, ADD115: the script
    # classified every refusal correctly and was killed at 180s partway through
    # 89 indexers, each POST of which makes the service connection-test a remote
    # tracker. Without this the next draft reads "exited None: (timed out)" and
    # has no reason to write anything different.
    budget = _TIMED_OUT.search(reason)
    lines = [
        "\n\nTHE PREVIOUS ATTEMPT AT THIS STEP FAILED. Do not repeat it.",
        "",
        "Why it stopped:",
        f"  {reason[:700]}",
    ]
    if ran:
        lines += ["", "What it actually sent, in order — the run stops at the first failure, so "
                      "everything above the last line DID happen on the machine:"]
        for i, c in enumerate(ran, 1):
            mark = "FAILED HERE" if i == len(ran) else "ok"
            lines.append(f"  {i}. [{mark}] {c[:150]}")
        lines += [
            "",
            "So write this draft to FINISH FROM THERE, not to start over. Guard anything "
            "already done instead of repeating it — `pct status N >/dev/null 2>&1 || pct create N …`, "
            "`pct status N | grep -q running || pct start N` — because a create or a start that "
            "has already happened fails the second time and would stop this attempt at the very "
            "first command.",
        ]
    if budget:
        secs = budget.group(1)
        lines += [
            "",
            f"THAT WAS A TIME BUDGET, NOT A MISTAKE. The command was killed at {secs}s with the work "
            "part-finished, and because it was killed there is no record of how much of it landed. "
            "The commands themselves may have been right.",
            "",
            "So do not send the same single command again — it will be killed at the same place. "
            "Split the work into batches that each finish well inside the budget (around 20 items per "
            "command), and make each batch RESUMABLE so re-running one costs nothing: treat a thing "
            "that is already there as success and move on, never as an error. Then the batches can be "
            "sent as separate commands, each one reporting what it did, and a batch that runs out of "
            "time loses only itself.",
        ]
    lines += ["", "Address the reason above specifically. If it was a value you did not read off the "
                  "machine, read it. If it was a command the runner may not run, do not send it again."]
    # §17.1260 — and the diagnosis the engine already researched for this failure.
    diag = diagnosis_of(str(node.get("output_text") or ""))
    if diag:
        lines += ["", "THE ENGINE ALREADY RESEARCHED THIS FAILURE. Its findings, which cost a web "
                      "search and a model call and must not be ignored:", "", diag[:3000]]
    return "\n".join(lines)


#: §17.1262 — step wording that asks for the state of the world right now. Only
#: these pull a web query: research costs a search and a model call, and most
#: steps are about this machine, which the engine reads directly instead.
#: Bare `working`, `available`, `reachable` and `alive` are deliberately ABSENT.
#: "Give the containers working DNS" is about this machine and is answered by
#: reading it (§17.1212/1229/1232); a web search there would be slower and worse.
#: Only currency or liveness of something OUTSIDE this host earns a lookup.
_NEEDS_CURRENCY = re.compile(
    r"(?i)\b(?:currently|up[- ]to[- ]date|latest|newest"
    r"|still\s+(?:works?|working|up|alive|available|maintained)"
    r"|maintained|deprecated|retired|shut\s*down|defunct"
    r"|as\s+many\s+as\s+possible|which\s+ones?)\b"
    r"|\bcurrent(?:ly)?\s+(?:working|available|live|up|active|maintained)\b")


#: §17.1262c — words that name the WORK rather than the THING. A currency
#: question is about the thing: a search for "add every public indexer Prowlarr
#: listed and connect it to Radarr" returns tutorials on how to add indexers,
#: which is not what was asked. Measured live on ADD115: the query that went out
#: was the step's title and the five sources were how-to pages.
_TASK_WORD = frozenset("""
add adds adding added install installs installing installed configure configures configured
connect connects connecting connected set sets setting give gives given write writes writing
create creates creating enable enables enabling disable remove removes delete update updates
run runs running make makes making use uses using put puts point points pointing verify check
every all each both listed list the a an and or but it its their them this that these those
to for from of on in into with at by as step steps so then also again more most
""".split())

#: §17.1262c — the two kinds of currency question, by the cue the step used.
#: "install the LATEST driver" wants a version; "which public indexers STILL
#: WORK" wants liveness. Asking the second shape for the first would be wrong.
_WANTS_VERSION = re.compile(r"(?i)\b(?:latest|newest|up[- ]to[- ]date|current(?:ly)?\s+(?:version|release))\b")


def _subject_of(title: str) -> str:
    """The THING a step is about — its title with the task words taken out.

    Only the first clause: "Add every public indexer Prowlarr listed, and
    connect it to Radarr and Sonarr" is two jobs, and the currency question
    belongs to the first one.
    """
    head = re.split(r"[,:;]|\s+and\s+(?:connect|configure|set|install|point|enable|add|give)\b",
                    str(title or ""), maxsplit=1)[0]
    words = [w.strip(".,;:()'\"`") for w in head.split()]
    keep = [w for w in words if w and w.lower() not in _TASK_WORD]
    return " ".join(keep[:8])


def currency_question(node: dict) -> str:
    """The question to research for this step, or "" when it needs no web lookup.

    §17.1262 — the operator, after 89 indexer adds found most trackers dead:
    "this is the exact reason for the researcher component to be wired up to
    fixing issues. It should find the most current up to date and available
    indexers."

    They are right, and the gap is structural in the same way as the others
    today. Research IS wired into `diagnose_failure`, so the engine researches a
    step AFTER it fails. It was never wired into DRAFTING one, so a step whose
    content depends on the state of the world is written from the model's memory
    and then discovers reality one 502 at a time.

    Prowlarr's schema is the authority on what definitions EXIST -- shipped with
    the version, static. It says nothing about which trackers are ALIVE this
    week, which changes constantly. That second question is exactly what a web
    search answers and what no amount of reading the machine can.

    §17.1262c — and it has to be ASKED as a currency question. The first cut
    searched the step's title, so the live query read "Prowlarr add all public
    indexers connect Radarr Sonarr": grounded correctly, aimed at the wrong
    thing, and five how-to pages came back. The subject is the step's thing with
    the task words removed, and the framing comes from the cue the step used --
    a version question for "latest", a liveness question for "still works".

    Narrow on purpose: only step wording that actually asks about currency or
    availability triggers a lookup. A step about this host's own disks or
    containers is answered by reading the host (§17.1212/1229/1232), and
    researching it would be slower and worse.
    """
    body = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _NEEDS_CURRENCY.search(body):
        return ""
    subject = _subject_of(str((node or {}).get("title") or ""))
    if not subject:
        return ""
    year = datetime.now(timezone.utc).year
    if _WANTS_VERSION.search(body):
        return f"{subject} latest version {year}"[:200]
    return f"{subject} still working {year}"[:200]


async def research_for_step(node: dict, environment: dict | None = None) -> str:
    """§17.1262 — current external facts for a step whose content depends on them.

    §17.1262b — the query carries the operator's SYSTEM, not just the step's own
    words, because that is the difference between a generic list and a usable
    one. "public indexers that still work" retrieves somebody else's setup; the
    same question with this machine's nouns in it retrieves the ones that work
    with what the operator actually runs. ``derive_need`` reads the step's goal
    and the environment profile for named hardware, and ``finalize_query`` makes
    those terms a rule rather than a hint — §17.1021 measured a generator
    dropping the model number three times when merely asked.

    Fail-soft in every direction: no currency wording, no sources, or any error
    leaves the prompt exactly as it was. A draft without research is the old
    behaviour, which is survivable; a draft blocked on a failed search is not.
    """
    question = currency_question(node)
    if not question:
        return ""
    key = str((node or {}).get("node_key") or "?")
    profile = str((environment or {}).get("profile") or "")
    try:
        from app.modules.assist_evidence import derive_need, finalize_query
        from app.modules.assist_research_lib import _render_research_block, research_one
        need = derive_need(
            question,
            title=str((node or {}).get("title") or ""),
            goal_terms=question,
            operator_notes=[ln.strip() for ln in profile.splitlines() if ln.strip()],
        )
        query = finalize_query(need, question, node_key=key) or question
        res = await research_one(question=query,
                                 node_key=key,
                                 synthesize=False,
                                 goal_terms=question,
                                 context_hint=profile[:600] or None,
                                 prerequisite_env=environment or None)
        sources = (res or {}).get("sources") or []
        block = _render_research_block(sources)
    except Exception as exc:
        logger.warning("step_research_failed node=%s err=%r",
                       (node or {}).get("node_key"), exc)
        return ""
    if not block:
        return ""
    logger.warning("step_research_attached node=%s sources=%d q=%r",
                   key, len(sources), query[:120])
    return ("\n\n" + block
            + "\n\nUse these to decide WHICH of the things this machine offers are worth using right "
              "now. What the machine lists is what EXISTS; whether each one still works is what these "
              "sources are for. Prefer the ones they confirm are alive, and do not spend the step on "
              "ones they say are dead or retired.")


async def draft_runbook(node: dict, brief: dict | str, upstream: str = "", *,
                        for_channel: bool = True, retry_note: str = "", spec=None,
                        environment: dict | None = None, truth=None) -> str:
    """The same runbook the executor would have written (its prompt and
    system), so the operator approves what Auto mode would have handed them.

    §17.1196 — ``retry_note`` carries the gate's own refusal back into a second
    draft. The rules are in the system prompt; a refusal says which one this
    draft actually broke, which is the difference between a style guide and a
    compiler error."""
    from app import model_router
    from app.modules.prompt_assembly import EXECUTION_SYSTEM_RUNBOOK, build_base_prompt
    b = brief if isinstance(brief, dict) else {"description": str(brief or "")}
    # §17.1290 — a shape the engine OWNS is rendered, not drafted: the model
    # fills the one free parameter (the commands inside the guest), the engine
    # supplies everything the gates would otherwise have to chase. Only on the
    # first draft: a template that is refused (it must not be -- every template
    # passes every gate in CI) hands the redraft to the model path below.
    # §17.1308 — and on a REDRAFT too: the shape is the engine's and pre-gated, so a
    # refusal of a template frame is about the model's CONTENT (an invented e-mail,
    # a placeholder). Handing the redraft to the model path threw the shape away:
    # live, ADD88's second draft lost the guarded start and was refused for THAT,
    # the third re-invented the e-mail, and the frame parked on a refusal. The
    # refusal goes to the free-parameter draw instead, the way a compiler error
    # feeds the next attempt.
    if truth is not None and for_channel:
        try:
            from app.modules import runbook_templates as rt
            tpl = rt.select_template(node, truth)
            if tpl is not None:
                model_vals = await rt.fill_free_params(tpl, node, str(b if isinstance(b, str) else json.dumps(b, default=str)),
                                                       upstream=upstream, environment=environment,   # §17.1306
                                                       retry_note=retry_note)                        # §17.1308
                rendered = rt.render(tpl, rt.values_for(tpl, node, truth, environment, model_vals))
                logger.warning("runbook_from_template node=%s template=%s chars=%d redraft=%s", node.get("node_key"), tpl.name,
                               len(rendered), bool(retry_note))
                return rendered
        except Exception as exc:
            logger.warning("runbook_template_failed node=%s err=%r", node.get("node_key"), exc)
            # §17.1312 — content cut twice is not a reason to hand the step to the
            # model path (which would write the same half-file without a template
            # around it): say so in a runbook the frame refuses and the operator reads.
            if type(exc).__name__ == "ContentCut":
                return ("<!-- runbook-cut -->\n## Why this cannot run yet\n\n"
                        f"{exc}\n\n## Run this\n\n```bash\necho \"content cut\"\n```\n")
    prompt = build_base_prompt(node, b, environment)
    # §17.1222 — a value the operator already gave once must never be asked for
    # again. The store, the `$NAME` reference and the out-of-band delivery all
    # existed (§17.1191/1193); what did not was the drafter KNOWING the names,
    # so it wrote "enter the password" into step after step for a password that
    # had been typed once and labelled. The operator: "Entering secrets should
    # be similar, like labeling it 'mass password' then applying it across the
    # project." Names only — a value never enters a prompt.
    # §17.1247 — what the last attempt at this step did, and why it stopped.
    prompt += attempt_feedback(node)
    # §17.1262 — what the machine lists is what EXISTS; whether it still works
    # today is a web question, and research was only ever wired into diagnosing
    # a failure rather than drafting the step.
    if for_channel:
        prompt += await research_for_step(node, environment)
    prompt += await stored_values_block()
    # §17.1232 — what machines exist, so the draft addresses the right one.
    # §17.1242 — and what hardware is in it, so it stops asking about the GPUs.
    if for_channel and spec is not None:
        prompt += await host_inventory(spec)
        try:
            from app.modules.runbook_discovery import hardware_facts
            prompt += await hardware_facts(spec)
        except Exception as exc:
            logger.warning("hardware_facts_failed err=%r", exc)
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
    # §17.1271 — the file notation is offered only when the runner can take a
    # file. Read from the cached policy here, in ONE place, rather than threaded
    # through every draft call site: six sites that each had to remember would
    # drift, which is how §17.1237 hid behind four green tests.
    if for_channel and spec is not None:
        try:
            from app.modules.assist_supervised import write_policy
            if ((await write_policy(spec)) or {}).get("can_write_files"):
                system += "\n" + FILE_RULES
        except Exception as exc:
            logger.warning("file_rules_capability_unreadable err=%r", exc)
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


def _without_sections(text_out: str, names: tuple) -> str:
    """The runbook with the named ``## <name>`` sections cut out."""
    text_value = text_out or ""
    heads = list(_SECTION_RE.finditer(text_value))
    keep, cursor = [], 0
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text_value)
        if any(h.group(1).lower().startswith(n.lower()) for n in names):
            keep.append(text_value[cursor:h.start()])
            cursor = end
    keep.append(text_value[cursor:])
    return "".join(keep)


_MULTILINE_RE = re.compile(r"<<-?\s*['\"]?\w+|^\s*(?:for|while|until)\s.*\bdo\s*$|^\s*if\s.*\bthen\s*$", re.M)


#: §17.1271 — one written file inside a ``## Write these files`` section:
#:
#:     ### /tmp/add_indexers.py
#:     ```python
#:     <the script, exactly as the interpreter will see it>
#:     ```
#:
#: The path is a sub-heading and the content is one fence. No shell is involved
#: anywhere in that, which is the entire point.
_FILE_HEAD_RE = re.compile(r"^\s{0,3}###\s+(/\S+)\s*$", re.M)
_ANY_HEAD3_RE = re.compile(r"^\s{0,3}###\s+(.+?)\s*$", re.M)


def _run_fences_after_the_files(text_out: str) -> list[str]:
    """§17.1288k — fences under a NON-path `###` heading inside ## Write these
    files are run commands. Live (trace 1148) the drafter wrote `## Run this` →
    `### 1. Write the script` → `## Write these files` → the file → `### 2. Run
    the script` → the command: a natural order, and a `##` inside the Run
    section ended it before any fence, so the frame had a file and no command,
    and the chain stopped because "nothing to redraft from"."""
    body = _section(text_out, "Write these files") or _section(text_out, "Write this file")
    if not body.strip():
        return []
    heads = list(_ANY_HEAD3_RE.finditer(body))
    out: list[str] = []
    for i, h in enumerate(heads):
        if h.group(1).strip().startswith("/"):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        out.extend(_FENCE_RE.findall(body[h.end():end]))
    return out
#: A written file is a script, not a payload: past this something is wrong.
FILE_MAX_BYTES = 256 * 1024
#: Which contents the engine can CHECK before sending. Anything else is written
#: as given -- the engine does not invent judgments it cannot make.
_COMPILED_SUFFIXES = (".py",)


def file_writes(text_out: str) -> list[dict]:
    """``[{path, content}]`` the runbook asks to be written, in order.

    Deliberately strict about the shape: a path heading must be followed by a
    fence, because a path with prose under it is not a file and guessing would
    write whatever the drafter was thinking out loud.
    """
    body = _section(text_out, "Write these files") or _section(text_out, "Write this file")
    if not body.strip():
        return []
    out: list[dict] = []
    heads = list(_FILE_HEAD_RE.finditer(body))
    bounds = [m.start() for m in _ANY_HEAD3_RE.finditer(body)]      # §17.1288k — any `###` ends a file
    for i, h in enumerate(heads):
        end = next((b for b in bounds if b > h.start()), len(body))
        fences = _FENCE_RE.findall(body[h.end():end])
        if not fences:
            logger.warning("file_write_without_fence path=%r", h.group(1))
            continue
        out.append({"path": h.group(1).strip(), "content": fences[0]})
    return out


_HEREDOC_OPEN_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?(?=\s|$|\))", re.M)


def unterminated_heredoc(body: str) -> Optional[str]:
    """§17.1310 — the delimiter of a heredoc this script opens and never closes,
    or None. A file that ends inside a heredoc was CUT (live: a fence marker in
    the model's content closed the runbook's file block early); bash only warns
    about it and runs the rest as heredoc text, so nothing after the opener
    happens and the exit code is 0."""
    lines = (body or "").split("\n")
    i = 0
    while i < len(lines):
        m = _HEREDOC_OPEN_RE.search(lines[i])
        if m and not lines[i].lstrip().startswith("#"):
            tag = m.group(1)
            j = i + 1
            while j < len(lines) and lines[j].strip() != tag:
                j += 1
            if j >= len(lines):
                return tag
            i = j + 1
            continue
        i += 1
    return None


def file_writes_will_not_work(files: list[dict]) -> list[dict]:
    """§17.1271 — refuse a written file the machine would reject or the
    interpreter could not run, before the operator is asked to approve it.

    The same bargain as §17.1257: the engine can compile what it is about to
    hand to another interpreter, so it does, and a refusal here is certain
    rather than a guess. What it cannot judge -- the content of a config file,
    say -- it does not pretend to.
    """
    out: list[dict] = []
    for f in files or []:
        path, body = str((f or {}).get("path") or ""), str((f or {}).get("content") or "")
        what = f"the file {path}"
        if not path.startswith("/"):
            out.append({"command": what, "why": (
                f"{path!r} is not an absolute path, and the runner refuses a relative one because "
                f"what it resolves to depends on where the runner happens to be running.")})
        tag = unterminated_heredoc(body)
        if tag:
            out.append({"command": what, "why": (
                f"the file {path} ends inside a heredoc (`<<'{tag}'` is opened and never closed), so it was cut: "
                f"bash would warn `here-document delimited by end-of-file`, treat everything after the opener as "
                f"text, exit 0 and run NOTHING -- a step recorded done for a script that did not happen. "
                f"Close the heredoc, and keep fence markers (```) out of file content: a fence inside a file "
                f"block ends the block.")})
            continue
        if ".." in path.split("/"):
            out.append({"command": what, "why": f"{path!r} contains '..', which the runner refuses."})
            continue
        size = len(body.encode())
        if not body.strip():
            out.append({"command": what, "why": "the file is empty — nothing was written in its fence."})
            continue
        if size > FILE_MAX_BYTES:
            out.append({"command": what, "why": (
                f"{size} bytes is past the {FILE_MAX_BYTES}-byte limit for a written file.")})
            continue
        if path.endswith(".sh"):                      # §17.1286 — a bash script is checked the way python is
            try:
                import subprocess
                r = subprocess.run(["bash", "-n"], input=body.encode(), capture_output=True, timeout=5)
                if r.returncode != 0:
                    out.append({"command": what, "why": (
                        f"{path} is not valid bash and would fail the moment it ran: "
                        f"{r.stderr.decode(errors='replace').strip()[:200]}")})
                    continue
            except Exception:                         # no bash here: nothing to say
                pass
        if path.endswith(_COMPILED_SUFFIXES):
            try:
                compile(body, path, "exec")
            except (SyntaxError, ValueError) as exc:
                out.append({"command": what, "why": (
                    f"{path} is not valid Python and would fail the moment it ran: {exc}. This was "
                    f"compiled before offering it, so the error is certain, not a guess. Nothing here "
                    f"goes through a shell, so write the code plainly -- quotes need no escaping.")})
                continue
            except RecursionError:
                out.append({"command": what, "why": f"{path} could not be compiled, so it cannot be offered."})
                continue
    if out:
        logger.warning("file_writes_refused count=%d first=%r", len(out), out[0]["why"][:120])
    return out


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
    run_section = _section(text_out, "Run this")
    body = run_section or (text_out or "")
    fences = _FENCE_RE.findall(body)
    if not run_section:
        # §17.1288k — with no ## Run this at all, every fence is a command EXCEPT
        # a written file's content: that is the file, not something to run.
        file_bodies = {f["content"] for f in file_writes(text_out)}
        fences = [f for f in fences if f not in file_bodies]
    out: list[str] = []
    for fence in fences + ([] if not run_section else _run_fences_after_the_files(text_out)):   # §17.1288k
        if _MULTILINE_RE.search(fence):
            whole = (fence or "").strip()
            if whole:
                out.append(whole)
            continue
        out.extend(block_commands(fence))
    if not out:
        # §17.1238 — no fence at all: the drafter wrote the command inline in
        # backticks. `verify_commands` two functions below has always accepted
        # that form; this one never did, so the two siblings disagreed about what
        # a command looks like.
        #
        # Live, ADD110 ("Start container 120 (caddy-proxy)") — a one-command step,
        # drafted correctly:
        #
        #     ## Run this
        #     `pct start 120`
        #     ## Verify
        #     `pct status 120` - expect `status: running`
        #
        # The Verify check was extracted and the Run command was not, so the
        # frame carried ZERO commands, Run was not offered, and the step the
        # operator had just asked for could not be carried out. §17.1227's
        # no-commands redraft fired and could not help: the second draft was
        # just as correct and just as invisible.
        #
        # Reuses `step_classify.step_commands`, which already reads fences AND
        # inline literals with the guards that matter (a path or an assignment is
        # not a command). Only when the fences yielded nothing, so a good fenced
        # runbook can never have prose-derived commands mixed into it.
        try:
            from app.modules.step_classify import step_commands
            seen: set[str] = set()
            # §17.1288k — with no ## Run this, the inline scan must not read a
            # written file's content or the Verify/Rollback checks as commands.
            scan = body if run_section else _without_sections(
                text_out, ("Write these files", "Write this file", "Verify", "Rollback"))
            for cmd, _sentence in step_commands(scan):
                c = (cmd or "").strip()
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
            if out:
                logger.warning("runbook_commands_from_inline count=%d first=%r", len(out), out[0][:80])
        except Exception as exc:
            logger.warning("inline_command_extract_failed err=%r", exc)
    return out[:MAX_RUN_COMMANDS]


#: §17.1265 — a backticked line under ## Verify, at any length. The extractor
#: below caps an inline literal at 200 characters, which is right for a command
#: and wrong for COUNTING what the drafter wrote: ADD115's three checks were 227
#: characters each, so nothing even reached the read-only judge.
_VERIFY_CANDIDATE = re.compile(r"`([^`\n]{2,600})`")
#: The shape that actually disqualified them — a command substitution. Measured:
#: `read_only_command` returns False for the whole line, True for each half.
_VERIFY_SUBST = re.compile(r"\$\(")


def verify_not_runnable(runbook: str) -> str:
    r"""§17.1265 — the Verify section wrote checks this channel cannot run.

    Measured on the real draft, not inferred. ADD115 parked with checks written
    under ``## Verify`` and ``verify: []`` in the frame -- twice, for two
    different reasons:

        curl … -H "X-Api-Key: $(pct exec 102 -- cat …)" | python3 -c "…"
        curl … -H "X-Api-Key: $PROWLARR_API_KEY"        | python3 -c "…"

    §17.1265's first cut asked whether a candidate used ``$(…)`` or ran past the
    length a command takes, which was the first draft's reason and not the
    second's: the redraft that followed piped into an interpreter instead, the
    detector stayed silent, and the step parked unverifiable again. A detector
    that names the shapes it has seen is one shape short, every time (the same
    lesson as §17.1266's phrase list).

    So the question is the one that actually matters: did the drafter write
    something command-shaped under ``## Verify``, and did NONE of it survive? The
    candidates come from ``step_commands``, which already tells a command from a
    path or a value, and the survivors from ``verify_commands``. Candidates and
    no survivors is the defect, whatever the reason -- and the step would
    otherwise be judged only on what its own script chose to print.

    The channel can verify this perfectly well: ``pct exec 102 -- cat <config>``
    is a read and ``curl -s <url>`` is a read. What it cannot take is the two
    welded into one clever line.
    """
    body = _section(runbook, "Verify")
    if not body.strip() or verify_commands(runbook):
        return ""
    try:
        from app.modules.step_classify import step_commands
        cands = [c for c, _s in step_commands(body)]
    except Exception as exc:                      # a judge that cannot run refuses nothing
        logger.warning("verify_candidates_failed err=%r", exc)
        return ""
    if not cands:
        return ""
    def _why(c: str) -> str:
        return ("it uses `$(...)` command substitution" if "$(" in c
                else "it pipes into an interpreter" if "|" in c
                else f"it is {len(c)} characters long" if len(c) > 200
                else "running a script this step just wrote is not a check of anything"
                if re.match(r"^(?:python3?|bash|sh)\s+/tmp/", c)
                else "")
    # Quote the candidate whose problem can be NAMED. Live, ADD115's first
    # candidate was `python3 /tmp/add_indexers.py` while the informative ones --
    # a pipe into an interpreter -- were four lines below it.
    first = next((c for c in cands if _why(c)), cands[0])
    why = _why(first) or "the read-only channel does not accept it as written"
    logger.warning("verify_not_runnable candidates=%d why=%s first=%r", len(cands), why, first[:120])
    return (
        "YOUR VERIFY SECTION CANNOT BE RUN, so this step would be approved with nothing to check it "
        f"against. This one could not be used because {why}:\n\n"
        f"    {first[:300]}\n\n"
        "A check goes through the read-only channel as ONE SIMPLE COMMAND: no `$(...)`, no pipe into "
        "`python3 -c`, and not a script you wrote in this step (running it again is not a check). "
        "Split it. Each half is allowed on its own -- read the value in one check and use it in the "
        "next, and let the operator compare the two:\n\n"
        "    ## Verify\n"
        "    - the API key this service is using: `pct exec 102 -- cat /var/lib/prowlarr/config.xml`\n"
        "    - how many indexers it now has (paste the key from the previous check): "
        "`curl -s -H \"X-Api-Key: <PROWLARR_API_KEY>\" http://<PROWLARR_IP>:9696/api/v1/indexer`\n\n"
        "Rewrite ## Verify as checks of that shape. Keep ## Run this exactly as it is -- it was "
        "accepted, and only the checks are being redrawn.")


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


def step_text(node: dict) -> str:
    """§17.1275 — the step's own words: title, description, template. NOT the
    runbook: that is the drafter's text, and its `root@pve` execution-context
    line would read as an account on the target."""
    seen: list[str] = []
    for k in ("title", "description", "prompt_template"):
        v = str((node or {}).get(k) or "").strip()
        if v and v not in seen:
            seen.append(v)
    return "\n".join(seen)


def verify_needs_a_value_the_run_never_used(commands: list[str], verify: list[str],
                                            files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288 — a placeholder that appears ONLY in a verify command is a value
    the operator is asked to type that the run itself never uses. Live, ADD82's
    script found VM 106's address from its MAC and the verify still said
    `ssh <PALWORLD_USER>@<PALWORLD_IP> 'systemctl is-active …'` -- the frame
    asked for `PALWORLD_IP` with the hint "if the operator already knows it
    (otherwise the script discovers it)", and prefilled the host's own address.
    An input is never optional here; a check uses what the run used."""
    used = set(placeholders([str(c) for c in commands or []] +
                            [str((f or {}).get("content") or "") for f in files or []]))
    out: list[dict] = []
    for v in verify or []:
        extra = [n for n in placeholders([str(v)]) if n not in used]
        if not extra:
            continue
        out.append({"command": str(v), "why": (
            f"`<{extra[0]}>` appears only in the verify: the run never uses it, so the operator would be "
            f"typing a value for the check alone -- and when the run itself finds that value (an address "
            f"read from a MAC), nobody has it to type. A check uses what the run used: from the host "
            f"(`qm agent N ping`, `qm agent N exec -- systemctl is-active <unit>`, `pct exec N -- …`), or as "
            f"the last lines of the script, over the same ssh the script opened. Drop the placeholder "
            f"from the verify or use it in the run too.")})
    return out


_SSH_WORD_RE = re.compile(r"(?<![\w./-])ssh(?![\w-])")
_QUOTED_ARG_RE = re.compile(r"""\s(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")
_ENV_REF_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,60})\}?")


def secret_in_an_ssh_command_line(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288b — a secret referenced inside an ssh REMOTE COMMAND cannot work
    and must not be made to. Live, ADD82's script ran

        ssh … "$USERNAME@$IP" 'echo "$MASS_PASSWORD" | sudo -S apt-get update && …'

    Single-quoted, `$MASS_PASSWORD` expands on the REMOTE, where it is unset:
    sudo reads an empty password and the script dies at the first install (after
    the key was copied, so a rerun looks different). Double-quoted it expands
    here, and the value rides ssh's argv on the host and the remote's command
    line -- both process lists. The secret goes on ssh's STDIN, which the remote
    sudo reads: `ssh -o BatchMode=yes user@host "sudo -S -p '' bash -c '…'" <<< "$NAME"`."""
    from app.modules.runbook_inputs import secret_name
    texts = [("command", str(c)) for c in commands or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    out: list[dict] = []
    for where, body in texts:
        for sm in _SSH_WORD_RE.finditer(body):
            rest = body[sm.end():].split("\n", 1)[0]     # every quoted argument on the ssh's own line
            hit = next((m for m in _QUOTED_ARG_RE.finditer(rest)
                        if not rest[:m.start()].rstrip().endswith("<<<")     # the here-string IS the remedy
                        and any(secret_name(n) for n in _ENV_REF_RE.findall(
                            m.group(1) if m.group(1) is not None else m.group(2)))), None)
            if hit is None:
                continue
            names = [n for n in _ENV_REF_RE.findall(hit.group(1) if hit.group(1) is not None else hit.group(2))
                     if secret_name(n)]
            line = body[body.rfind("\n", 0, sm.start()) + 1:].split("\n", 1)[0].strip()
            single = hit.group(2) is not None
            out.append({"command": line[:200] if where == "command" else f"{where}: {line[:160]}", "why": (
                f"`${names[0]}` is inside an ssh command line"
                + (f": single-quoted, it expands on the REMOTE, where `{names[0]}` is not set, so sudo reads an "
                   f"empty password and the step fails there"
                   if single else
                   ": double-quoted, the value is spliced into ssh's arguments on this host and into the remote's "
                   "command line, where any process list shows it")
                + f". A secret goes to the remote on ssh's STDIN, which `sudo -S` reads: "
                  f"`ssh -o BatchMode=yes user@host \"sudo -S -p '' bash -c '<the commands, && between them>'\" "
                  f"<<< \"${names[0]}\"` -- one ssh, one sudo, the value in no command line.")})
            break
    return out


_NEIGH_READ_RE = re.compile(r"\bip\s+(?:-4\s+|-6\s+)?(?:neigh|neighbour|neighbor|n)\b|\barp\s+-[an]\b")
_SWEEP_RE = re.compile(r"\bnmap\s+-sn\b|\barp-scan\b|\bfping\b|\bping\b[^\n]*\$\(seq\b|\bseq\b[^\n]*\n?[^\n]*\bping\b|\{1\.\.25[0-4]\}[^\n]*\bping\b|\bping\b[^\n]*\{1\.\.25[0-4]\}")


def reads_the_neighbour_table_cold(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288c — `ip neigh` lists the machines THIS host has exchanged packets
    with; a VM that just booted and talked only to the router is not in it,
    and waiting does not put it there. Live, ADD82's script waited 60 s for
    VM 106's MAC to appear and would have exited "did not appear in ip neigh;
    is its network up?" -- the host had `nmap` the whole time, and the rule
    (§17.1286) said to sweep first. A rule the drafter skips becomes a gate."""
    texts = [("command", str(c)) for c in commands or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    out: list[dict] = []
    # §17.1288n — a sweep in an EARLIER command warms the table for a later one:
    # live (trace 1160) `nmap -sn …` was command 3 and `ip neigh show | grep <mac>`
    # command 4, and the read was refused as cold. The block runs in order.
    earlier = ""
    for where, body in texts:
        # §17.1295 — a comment is not a read: the gate fired on a template's own `# … ip neigh …` note
        body = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith("#"))
        m = _NEIGH_READ_RE.search(body)
        if not m or _SWEEP_RE.search(earlier + "\n" + body[:m.start()]):
            earlier += "\n" + body
            continue
        line = body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
        out.append({"command": line[:200] if where == "command" else f"{where}: {line[:160]}", "why": (
            "reads the neighbour table cold: `ip neigh` lists only the machines this host's KERNEL has exchanged "
            "packets with, so a guest that just booted and spoke to the router alone never appears, however long "
            "the loop waits. Find the address from a sweep's OWN report: `nmap -sn <the bridge's /24>` prints "
            "`Nmap scan report for <ip>` followed by `MAC Address: <mac>` -- read the ip beside the MAC (nmap's "
            "ARP scan uses raw sockets and never fills `ip neigh`; live, a guest answered nmap for an hour while "
            "`ip neigh` stayed blank). Without nmap, a kernel ping sweep does fill the table: `NET=$(ip -4 route "
            "get 1 | sed -n 's/.* src \\([0-9.]*\\)\\.[0-9]*.*/\\1/p'); for h in $(seq 1 254); do ping -c 1 -W 1 "
            "\"$NET.$h\" >/dev/null 2>&1 & done; wait` and THEN `ip neigh show | grep -i <mac>`.")})
    return out


_SSH_LITERAL_USER_RE = re.compile(
    r"(?<![\w./-])(?:ssh|ssh-copy-id|scp|sftp)(?![\w-])[^\n]*?\s(?:-o\s+\S+\s+|-\w\s+\S+\s+)*(?:-i\s+\S+\s+)?"
    r"([a-z_][a-z0-9_-]{0,31})@(\S+)")


def ssh_assumes_the_guests_account(commands: list[str], node: Optional[dict], env: Optional[dict],
                                   files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288h — a literal account on an ssh into the step's guest is a GUESS
    unless something the engine holds names it. Live, ADD82's fourth clean draft
    wrote `ssh-copy-id … root@"$IP"` and `ssh … root@"$IP"` into VM 106 -- an
    Ubuntu Server 22.04 install (ADD5), which refuses root over ssh by default
    and has no root password -- when every earlier frame had asked for
    `<PALWORLD_USER>`. A pin names an account; the step's own text names one
    (`aedefruscio@192.168.1.129`, ADD26); nothing names `root` inside a guest --
    the shell's `root@pve` is the HOST's. The placeholder is the honest form:
    the operator fills it once and it travels by name."""
    from app.modules.runbook_inputs import _USER_AT_HOST_RE
    subject = step_text(node) if node else ""
    if not _GUEST_SUBJECT_RE.search(subject):
        return []                                            # not a step about a guest
    named = {u for u, _h in _USER_AT_HOST_RE.findall(subject)}
    subs = (env or {}).get("substitutions") or {}
    named |= {str(v) for k, v in (subs.items() if isinstance(subs, dict) else []) if "USER" in str(k).upper()}
    texts = [("command", str(c)) for c in commands or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    out: list[dict] = []
    gid = _GUEST_SUBJECT_RE.search(subject).group(1)
    for where, body in texts:
        for m in _SSH_LITERAL_USER_RE.finditer(body):
            user = m.group(1)
            if user in named or m.group(0).rstrip().endswith("@pve"):
                continue
            line = body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
            out.append({"command": line[:200] if where == "command" else f"{where}: {line[:160]}", "why": (
                f"`{user}@{m.group(2)[:40]}` -- the account inside guest {gid} is an assumption: no pin names "
                f"it, the step's text names none, and no finished step logged in there as `{user}`. An "
                f"Ubuntu Server guest refuses root over ssh by default and has no root password, so a guessed "
                f"account fails at the first ssh-copy-id. Write a placeholder named after the guest, "
                f"`<{_guest_word(subject, gid, env)}_USER>@$IP`, in the ssh-copy-id AND the ssh; the operator fills "
                f"it once (they log in at the console with it) and the value travels by name.")})
            break
        if out:
            break
    return out


_GUEST_NAME_RE = re.compile(r"\b([a-z][a-z0-9]+)(?:-[a-z0-9]+)+\b")   # `palworld-server`, `ai-vm`, `caddy-proxy`


def _guest_word(subject: str, gid: str, env: Optional[dict] = None) -> str:
    """The word a placeholder for guest `gid` is named after: the system map's
    name for it when the map has one, else the hyphenated name nearest the
    guest's mention in the step text (`palworld-server` → `PALWORLD`), else
    `VM<id>`."""
    state = (env or {}).get("system_state") or {}
    ent = state.get(gid) if isinstance(state, dict) else None
    attrs = (ent or {}).get("attrs") if isinstance(ent, dict) else None
    label = str((attrs or {}).get("name") or (attrs or {}).get("hostname") or "")
    if label:
        return re.split(r"[^a-z0-9]", label.lower(), 1)[0].upper() or f"VM{gid}"
    anchor = re.search(rf"\b{gid}\b", subject)
    names = [(abs(m.start() - (anchor.start() if anchor else 0)), m.group(1)) for m in _GUEST_NAME_RE.finditer(subject)
             if m.group(1).lower() not in ("qemu", "guest", "apt", "ssh", "ip", "ssh-copy")]
    if names:
        return min(names)[1].upper()
    return f"VM{gid}"


_ADDRESS_READ_RE = re.compile(r"\bip\s+(?:-4\s+)?(?:neigh|neighbour|neighbor|n)\b|\barp\s+-[an]\b|network-get-interfaces|\bqm\s+agent\s+\d+\s+ping\b")
_IP_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9_]*_(?:IP|ADDR|ADDRESS))>")


def reads_the_address_and_asks_for_it(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288l — a block that READS a guest's address (`ip neigh show | grep
    <mac>`) and then writes `<PALWORLD_IP>` for the operator to type is asking
    for a value it has already found -- and nobody has it to type (the VM
    was stopped a minute ago). Live, trace 1151: four top-level commands, the
    neigh read in the second, the placeholder in the third and fourth. Values
    pass between commands only inside ONE bash script (`IP=$(…)`), which is
    the rule the draft skipped."""
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    joined = "\n".join(texts)
    if not _ADDRESS_READ_RE.search(joined):
        return []
    out: list[dict] = []
    for c in commands or []:
        m = _IP_PLACEHOLDER_RE.search(str(c))
        if m:
            out.append({"command": str(c)[:200], "why": (
                f"this block reads the address itself and asks the operator for it: `<{m.group(1)}>` here, while "
                f"another command reads `ip neigh` for the guest's MAC. The operator cannot type what the block "
                f"finds at run time. Carry it: ONE bash script under ## Write these files -- "
                f"`IP=$(ip neigh show | grep -i \"$MAC\" | awk '{{print $1}}' | head -n1)` -- and use `$IP` in the "
                f"ssh-copy-id and the ssh; run it as `MASS_PASSWORD=\"$MASS_PASSWORD\" bash /tmp/<name>.sh`.")})
            break
    return out


_TARGETED_RE = re.compile(r"\b(?:ping|ssh|ssh-copy-id|scp|sftp|nc|curl|wget)\b[^\n|;&]*?(?:[\w.-]+@)?((?:\d{1,3}\.){3}\d{1,3})\b")


#: §17.1306 — sample values a model writes when it has no real one.
_PLACEHOLDER_VALUE_RE = re.compile(
    r"(?<![\w.-])(?:[\w.+-]+@)?(?:example|yourdomain|your-domain|mydomain|my-domain|changeme|change-me|placeholder)"
    r"\.(?:com|org|net|local|test)\b|\b(?:CHANGE_?ME|changeme|YOUR_(?:DOMAIN|EMAIL|PASSWORD|TOKEN|API_KEY|HOST))\b"
    r"|\byour-?(?:domain|email|password)\.?(?:here)?\b|\bsome\.?domain\.(?:com|org)\b", re.I)
_DOMAIN_IN_FACT_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:duckdns\.org|com|org|net|io|dev|xyz|me|us|cc|tv))\b", re.I)


_EMAIL_LITERAL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][\w.+-]*@(?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.I)


def invented_email_in_files(commands: list[str], files: Optional[list[dict]], env: Optional[dict],
                            node: Optional[dict]) -> list[dict]:
    """§17.1307 — an e-mail address in written content that appears in no fact,
    no pin and not in the step's own text was made up. Live, ADD88's Caddyfile
    opened with `email aedefruscio@defrusciohomelab.duckdns.org` -- the operator's
    account name glued to their DuckDNS domain, a mailbox that does not exist,
    handed to the ACME CA as the contact. A contact address is the operator's to
    give: `<ACME_EMAIL>` on the frame, or leave the option out."""
    held = " ".join([str(f.get("text") if isinstance(f, dict) else f) for f in ((env or {}).get("facts") or [])]
                    + [f"{k}={v}" for k, v in ((env or {}).get("substitutions") or {}).items()]
                    + [str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template")]).lower()
    texts = [(str(c), "command") for c in commands or []] + \
            [(str((f or {}).get("content") or ""), str((f or {}).get("path") or "file")) for f in files or []]
    out: list[dict] = []
    for body, where in texts:
        for m in _EMAIL_LITERAL_RE.finditer(body):
            addr = m.group(1)
            if addr.lower() in held or "$" in addr or "<" in addr:
                continue
            line = body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
            out.append({"command": (line[:200] if where == "command" else f"{where}: {line[:160]}"), "why": (
                f"`{addr}` appears nowhere the engine holds -- not in the facts, the pins or the step -- so it was "
                f"made up, and a contact address is the operator's to give. Write `<ACME_EMAIL>` (filled once on the "
                f"frame) or leave the option out.")})
            break
    return out


_IPV4_LITERAL_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_UNSOURCED_SKIP = {"0.0.0.0", "127.0.0.1", "255.255.255.0", "255.255.0.0", "255.0.0.0", "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"}


def unsourced_addresses_in_files(commands: list[str], files: Optional[list[dict]], env: Optional[dict],
                                 node: Optional[dict], upstream: str = "") -> list[dict]:
    """§17.1312 — an IPv4 literal written into a file or a command must appear in
    something the engine holds: a fact, a pin, the system map, the step's own
    text, or an upstream output. Live, ADD100's server.js said Pi-hole is
    `192.168.1.130` (the container's ID, not an address) and the engine is at
    `192.168.1.110:8080`; ADD88's Caddyfile sent Jellyfin to `192.168.1.101`.
    Every gate was green. A value with no source is the model's guess, and a
    guess written into a config is wrong on purpose."""
    e = env or {}
    if not (e.get("facts") or e.get("substitutions") or e.get("system_state")):
        return []                       # no ledger to compare against: blindness invents nothing
    held = (json.dumps(e, default=str) + " " + str(upstream or "") + " "
            + " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template")))
    held_ips = set(_IPV4_LITERAL_RE.findall(held))
    texts = [(str(c), "command") for c in commands or []] + \
            [(str((f or {}).get("content") or ""), str((f or {}).get("path") or "file")) for f in files or []]
    out: list[dict] = []
    for body, where in texts:
        missing = []
        for ip in dict.fromkeys(_IPV4_LITERAL_RE.findall(body)):
            # a `.1` is the subnet's router by convention (the engine's own templates default the
            # gateway to it); a `.0`/`.255` is a network or broadcast; the rest need a source
            if ip in held_ips or ip in _UNSOURCED_SKIP or ip.rsplit(".", 1)[1] in ("0", "1", "255"):
                continue
            missing.append(ip)
        if not missing:
            continue
        first = missing[0]
        line = body[body.rfind("\n", 0, body.find(first)) + 1:].split("\n", 1)[0].strip()
        out.append({"command": (line[:200] if where == "command" else f"{where}: {line[:160]}"), "why": (
            f"`{'`, `'.join(missing[:4])}` appears in nothing the engine holds -- no fact, pin, system map entry, "
            f"step text or earlier output names {'it' if len(missing) == 1 else 'them'} -- so {'it is' if len(missing) == 1 else 'they are'} "
            f"a guess written into {where if where != 'command' else 'the command'}. Use an address the facts or pins hold, "
            f"read it off the machine first (`pct exec N -- hostname -I`, `ip neigh`), or write a `<NAME_IP>` the operator fills.")})
    return out


def placeholder_values_in_files(commands: list[str], files: Optional[list[dict]], env: Optional[dict]) -> list[dict]:
    """§17.1306 — `admin@example.com` in a file the block writes is not a value,
    it is the model saying it had none. Live, ADD88's Caddyfile came back as
    `{ email admin@example.com } :80 { respond "Caddy is running" }` while the
    facts named `defrusciohomelab.duckdns.org` and the step named five
    reverse-proxy blocks. A placeholder written to disk is a config that is
    wrong on purpose; the honest forms are the real value or a `<NAME>` the
    operator fills on the frame."""
    texts = [(str(c), "command") for c in commands or []] + \
            [(str((f or {}).get("content") or ""), str((f or {}).get("path") or "file")) for f in files or []]
    facts = [str(f.get("text") if isinstance(f, dict) else f) for f in ((env or {}).get("facts") or [])]
    known = []
    for f in facts:
        for m in _DOMAIN_IN_FACT_RE.finditer(f):
            d = m.group(1).lower()
            if "example." not in d and d not in known:
                known.append(d)
    known.sort(key=lambda d: (0 if d.endswith("duckdns.org") else 1, d))
    out: list[dict] = []
    for body, where in texts:
        m = _PLACEHOLDER_VALUE_RE.search(body)
        if not m:
            continue
        line = body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
        hint = (f" The facts name `{known[0]}`; use it." if known else
                " Nothing the engine holds names one: write `<DOMAIN>` (or the value's own <NAME>) and the operator fills it on the frame.")
        out.append({"command": (line[:200] if where == "command" else f"{where}: {line[:160]}"), "why": (
            f"`{m.group(0)}` is a placeholder, not a value: written to {where if where != 'command' else 'the command'} it "
            f"becomes a config that is wrong on purpose and a step recorded done for it." + hint)})
    return out


def targets_the_host_as_the_guest(commands: list[str], node: Optional[dict], env: Optional[dict],
                                  files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288l — on a step about a guest, a ping/ssh/ssh-copy-id aimed at the
    HOST's own address (the system map's `host` entry) is aimed at the wrong
    machine. Live, trace 1151 waited for VM 106 by pinging 192.168.1.156 --
    the Proxmox host, the only address the step's text names (its web
    console) -- which answers at once whether or not the VM is up."""
    subject = step_text(node) if node else ""
    if not _GUEST_SUBJECT_RE.search(subject):
        return []
    state = (env or {}).get("system_state") or {}
    host_addrs = {str((e.get("attrs") or {}).get("ip") or "").split("/")[0]
                  for e in (state.values() if isinstance(state, dict) else [])
                  if isinstance(e, dict) and str(e.get("kind") or "") in ("host", "node")}
    host_addrs.discard("")
    if not host_addrs:
        return []
    gid = _GUEST_SUBJECT_RE.search(subject).group(1)
    texts = [("command", str(c)) for c in commands or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    for where, body in texts:
        for m in _TARGETED_RE.finditer(body):
            if m.group(1) in host_addrs:
                line = body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
                return [{"command": line[:200] if where == "command" else f"{where}: {line[:160]}", "why": (
                    f"`{m.group(1)}` is this host's own address (the system map's host entry), not guest {gid}'s -- "
                    f"a ping or ssh at it reaches the Proxmox host, which answers whether or not the VM is up. "
                    f"The guest's address is read from its MAC (`qm config {gid}` → net0 → sweep → `ip neigh show`) "
                    f"inside the script; the only address the step's text names is the host's web console.")}]
    return []


def asks_for_a_secret_the_store_holds(missing: list[str], policy: Optional[dict], node: Optional[dict]) -> list[dict]:
    """§17.1288m — on a step about a guest, a NEW password placeholder when the
    runner already holds the mass password is a question the operator has
    answered: every guest in this job was built with `MASS_PASSWORD` (§17.1286
    says so to the drafter; ADD26 proved it on VM 110). Live, trace 1155 asked
    for `<PALWORLD_PASSWORD>` -- "the same `$MASS_PASSWORD` if the guest was
    built with the mass password, but the runbook must not assume that" -- and
    the frame would have had the operator type a password into the engine's
    store. If the guest's password really differs, the run says `Permission
    denied` and the engine asks then, with evidence."""
    if not missing or not node or not _GUEST_SUBJECT_RE.search(step_text(node)):
        return []
    known = {str(n) for n in ((policy or {}).get("secrets") or [])} | {str(n) for n in ((policy or {}).get("held") or [])}
    held_pw = sorted(n for n in known if n.upper().endswith(("PASSWORD", "PASS", "PASSWD")))
    if not held_pw:
        return []
    out: list[dict] = []
    for n in missing:
        if not str(n).upper().endswith(("PASSWORD", "PASS", "PASSWD")):
            continue
        h = "MASS_PASSWORD" if "MASS_PASSWORD" in held_pw else held_pw[0]
        out.append({"command": f"<{n}>", "why": (
            f"`<{n}>` asks the operator for a password the runner holds: `{h}` is the password the guests of "
            f"this job were built with, and it is referenced by NAME -- `{h}=\"${h}\" bash /tmp/<name>.sh`, "
            f"`$\u007b{h}\u007d` inside the script, `SSHPASS=\"${h}\" sshpass -e …`, `<<< \"${h}\"` for sudo. "
            f"Do not ask for a second one; if the guest's password really differs, the run will say "
            f"`Permission denied` and the engine asks then, with that evidence.").replace("\\u007b", "{").replace("\\u007d", "}")})
    return out


_PING_LITERAL_RE = re.compile(r"\bping6?\b[^\n|;&]*?\s((?:\d{1,3}\.){3}\d{1,3})\b")


def waits_on_a_fixed_address(commands: list[str], node: Optional[dict], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1288m — on a step about a guest, a wait loop that pings a FIXED
    address is not waiting for the guest: the host's own address (§17.1288l)
    or the router (trace 1155: `ping -c 1 -W 2 192.168.1.1 && break`) answers at
    once, the loop ends, the sweep runs against a VM still booting, and the
    script fails "could not find guest address". The guest's readiness is the
    neigh entry for its MAC, or a ping at `$IP` once found."""
    if not node or not _GUEST_SUBJECT_RE.search(step_text(node)):
        return []
    gid = _GUEST_SUBJECT_RE.search(step_text(node)).group(1)
    texts = [("command", str(c)) for c in commands or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    for where, body in texts:
        depth = 0
        for ln in body.split("\n"):
            stripped = ln.strip()
            opens = len(re.findall(r"(?:^|;\s*|&&\s*)(?:for|while|until)\b", stripped)) if where != "command" else 0
            closes = len(re.findall(r"(?:^|;\s*)done\b", stripped)) if where != "command" else 0
            in_loop = depth > 0 or bool(_LOOP_HEAD_RE.match(stripped))
            m = _PING_LITERAL_RE.search(stripped)
            if m and in_loop:
                return [{"command": stripped[:200] if where == "command" else f"{where}: {stripped[:160]}", "why": (
                    f"a wait that pings `{m.group(1)}` is not waiting for the guest: that address answers whether "
                    f"or not VM {gid} is up, so the loop ends at once and what follows runs while the guest is still "
                    f"booting. Wait on the guest itself: loop (12 × 5 s) over the sweep and the `ip neigh show` read "
                    f"for its MAC until the entry appears, then `ping -c 1 -W 2 \"$IP\"` and the first ssh.")}]
            depth = max(0, depth + opens - closes)
    return []


_STALE_CLAIM_RE = re.compile(
    r"no way in(?:to)?[^.\n]*|done at the console[^.\n]*|by hand[^.\n]*|at the (?:VM's |guest's )?console[^.\n]*"
    r"|no network presence[^.\n]*|chicken and egg[^.\n]*", re.I)
_CORRECTION_MARK = "ENGINE CORRECTION"
#: the refusals that contradict a step's own text about how its guest is reached
_REACH_REFUSAL_MARKS = ("runs in the runner's own shell on the Proxmox HOST",
                        "nothing has put this host's key on guest", "is stopped (`")


def step_text_correction(node: Optional[dict], frame: dict, env: Optional[dict] = None) -> str:
    """§17.1288n — the paragraph to APPEND to a step's description when the
    engine's own gate has just contradicted what that description says about
    reaching the guest. Live, ADD82's description — written by an earlier
    engine repair — said *"The host therefore has NO way in to this guest …
    So this one is done at the console, by hand"*, and the draft prompt
    presents the description as the specification that wins. Twelve reasks:
    every first draft was host-side because the spec said so, and the chain
    spent its rungs climbing out. §17.1288e told the redraft the note outranks
    the text; this corrects the TEXT, so the next first draft starts right.
    Returns "" when nothing in the description makes the stale claim, when
    no refusal of that kind is in the frame, or when a correction is already
    there (idempotent; the marker is checked)."""
    desc = str((node or {}).get("description") or "")
    if not desc or _CORRECTION_MARK in desc:
        return ""
    claims = [m.group(0).strip() for m in _STALE_CLAIM_RE.finditer(desc)]
    if not claims:
        return ""
    whys = [str(r.get("why") or "") for r in (frame or {}).get("refused") or []]
    hit = next((w for w in whys if any(mk in w for mk in _REACH_REFUSAL_MARKS)), "")
    if not hit:
        return ""
    gm = _GUEST_SUBJECT_RE.search(step_text(node))
    gid = gm.group(1) if gm else ""
    account = f"<{_guest_word(step_text(node), gid, env)}_USER>" if gid else "<GUEST_USER>"
    seen: set[str] = set()
    distinct = [c for c in claims if not (c.lower()[:14] in seen or seen.add(c.lower()[:14]))]
    quoted = "; ".join(f"\"{c[:60]}\"" for c in distinct[:5])
    return (f"{_CORRECTION_MARK} ({datetime.now(timezone.utc).date().isoformat()}): the lines above that say {quoted} are SUPERSEDED -- "
            f"the engine measured otherwise after they were written: {hit.rstrip()} "
            f"The commands quoted above are the GUEST's and run inside it through that ssh. The account inside "
            f"guest {gid or 'N'} is {account} (the operator fills it; nothing the engine holds names it); the "
            f"password is `$MASS_PASSWORD`, by name. Nothing here is done at a console by hand.")


_SET_E_RE = re.compile(r"^\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b", re.M)
_LOOKUP_ASSIGN_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)=\$\((?P<pipe>[^\n]*\b(?:grep|pgrep)\b[^\n]*)\)\s*$", re.M)


def repair_lookups_under_set_e(files: Optional[list[dict]]) -> tuple[list[dict], list[dict]]:
    """§17.1288o — ``(files, repairs)``: under ``set -e`` a lookup that finds
    nothing ends the script. Live (ADD82, 23:12 UTC): the script started VM 106
    and on the first pass of its wait loop ran ``IP=$(ip neigh show | grep -i
    "$MAC" | … | head -1)`` -- grep found nothing yet, exited 1, ``pipefail``
    made that the pipeline's status, ``set -e`` made it the script's: exit 1
    after ``MAC: …``, no message, the VM left booting. The script's own next
    line was ``if [ -n "$IP" ]`` -- it EXPECTED the empty result. Proved in a
    shell: ``set -euo pipefail; X=$(printf "" | grep x | head -1)`` exits 1;
    with ``|| true`` inside the substitution it continues. The repair is that
    ``|| true``, applied to every ``NAME=$(… grep …)`` assignment in a
    ``set -e`` script whose value the script then tests, and said on the frame
    (§17.1270: never a silent repair)."""
    out: list[dict] = []
    repairs: list[dict] = []
    for f in files or []:
        path, body = str((f or {}).get("path") or ""), str((f or {}).get("content") or "")
        if not path.endswith(".sh") or not _SET_E_RE.search(body):
            out.append(f)
            continue
        fixed = body
        for m in _LOOKUP_ASSIGN_RE.finditer(body):
            name, pipe = m.group("name"), m.group("pipe")
            if re.search(r"\|\|\s*(?:true|:)\s*$", pipe):
                continue                                     # already survives an empty result
            if not re.search(rf"\[\s+-[nz]\s+\"?\$\{{?{re.escape(name)}\}}?\"?\s+\]|-z\s+\"\${re.escape(name)}\"", body):
                continue                                     # the script never tests it: not a lookup that may be empty
            fixed = fixed.replace(m.group(0), f"{m.group('indent')}{name}=$({pipe} || true)", 1)
            repairs.append({"why": (
                f"added `|| true` to `{name}=$(…)` in {path}: under `set -e` a `grep` that finds nothing ends "
                f"the script, and this lookup is one the script itself expects to be empty (it tests "
                f"`[ -n \"${name}\" ]`) -- live, the wait loop's first pass killed the run before the guest "
                f"had booted.")})
        out.append({**f, "content": fixed} if fixed != body else f)
    return out, repairs


def inputs_for(commands: list[str], verify: list[str], runbook: str,
               files: Optional[list[dict]] = None) -> list[dict]:
    """``[{name, hint, secret}]`` — the values the operator must supply.

    §17.1280 — a placeholder inside a WRITTEN FILE is an input too. Live, both
    scripts carried `PROWLARR_URL = "http://<PROWLARR_IP>:9696"`; this read only
    the commands, so the frame said `inputs: []`, nothing was asked or
    discovered, and the file reached the disk with the literal `<PROWLARR_IP>`,
    which `urlopen` then tried to resolve as a hostname.
    """
    hints = input_hints(runbook)
    from app.modules.runbook_inputs import secret_name       # §17.1275 — one definition
    texts = list(commands) + list(verify) + [str((f or {}).get("content") or "") for f in files or []]
    return [{"name": n, "hint": hints.get(n, ""), "secret": secret_name(n)} for n in placeholders(texts)]


def secrets_in_files(files: Optional[list[dict]], inputs: list[dict]) -> list[dict]:
    """§17.1280 — a SECRET placeholder inside a file cannot be filled: the runner
    expands `$NAME` in a COMMAND's environment, never inside a file's bytes, and
    writing the value into a file on disk is the leak §17.1191 exists to stop."""
    secret = {i["name"] for i in inputs or [] if i.get("secret")}
    out: list[dict] = []
    for f in files or []:
        found = [n for n in placeholders([str((f or {}).get("content") or "")]) if n in secret]
        if found:
            out.append({"command": f"the file {(f or {}).get('path')}", "why": (
                f"the file {(f or {}).get('path')} contains <{found[0]}>, a secret, and a secret cannot be "
                f"written into a file: the runner expands `$NAME` only in a command's environment, and a "
                f"value on disk is the leak the store exists to prevent. Read it on the machine instead "
                f"(a *arr key is the `<ApiKey>` in its config.xml, via `pct exec`), or pass it as an "
                f"environment variable -- `{found[0]}=\"${found[0]}\" python3 {(f or {}).get('path')}` -- "
                f"and read `os.environ[\"{found[0]}\"]` in the script.")})
    return out


def fill_files(files: list[dict], values: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """§17.1280 — ``(files with placeholders substituted, problems)``. A placeholder
    left over after substitution is a problem, never a file on disk: `<PROWLARR_IP>`
    written verbatim became `Name or service not known` live."""
    out: list[dict] = []
    problems: list[dict] = []
    for f in files or []:
        content = substitute([str((f or {}).get("content") or "")], values or {})[0]
        left = placeholders([content])
        for n in left:
            problems.append({"name": n, "why": f"missing (used in the file {(f or {}).get('path')})"})
        out.append({**f, "content": content})
    return out, problems


def placeholder_contexts(name: str, texts: list[str]) -> set[str]:
    """§17.1282 — where ``<NAME>`` sits across a block's commands, checks and
    files: ``bare`` (a shell word of its own), ``dq`` (inside ``"…"``) or ``sq``
    (inside ``'…'``). The value's safety rule depends on which."""
    out: set[str] = set()
    pat = re.compile(rf"<{re.escape(name)}>")
    for t in texts or []:
        t = str(t or "")
        spans = _quote_spans(t)
        for m in pat.finditer(t):
            ctx = "bare"
            for start, end, q in spans:
                if start < m.start() and m.end() <= end:
                    ctx = "dq" if q == '"' else "sq"
                    break
            out.add(ctx)
    return out


_DQ_UNSAFE = re.compile(r'["$`\\\n\r]')
_SQ_UNSAFE = re.compile(r"['\n\r]")
_QUOTED_MAX = 4096


def check_inputs(names: list[str], values: dict | None,
                 texts: Optional[list[str]] = None) -> tuple[dict[str, str], list[dict]]:
    """``(clean values, problems)`` — every name present and safe WHERE IT IS
    SPLICED. A bare placeholder must be one shell token (no whitespace, quotes
    or metacharacters; the gate has to read it as one word). §17.1282 — one
    inside ``"…"`` may hold spaces and up to 4 KB but no ``"``, ``$``, backtick,
    backslash or newline; inside ``'…'`` no ``'`` or newline. Live, the engine
    read the host's own `ssh-rsa AAAA… root@pve` off the machine, prefilled it
    into ``echo "<OPERATOR_SSH_PUBKEY>" >> ~/.ssh/authorized_keys``, and then
    refused the value at approval for containing spaces. Without ``texts`` the
    bare rule applies everywhere (the historical behaviour)."""
    values = values or {}
    clean: dict[str, str] = {}
    problems: list[dict] = []
    for n in names:
        v = str(values.get(n, "") if isinstance(values, dict) else "").strip()
        if not v:
            problems.append({"name": n, "why": "missing"})
            continue
        ctx = placeholder_contexts(n, texts or []) if texts else {"bare"}
        if not ctx or "bare" in ctx:
            if not _SAFE_VALUE_RE.match(v):
                problems.append({"name": n, "why": "letters, digits and . / : @ % + = , ~ - _ only — no spaces, quotes or shell characters"})
                continue
        else:
            if len(v) > _QUOTED_MAX:
                problems.append({"name": n, "why": f"longer than {_QUOTED_MAX} characters"})
                continue
            if "dq" in ctx and _DQ_UNSAFE.search(v):
                problems.append({"name": n, "why": 'spliced inside double quotes: no " $ ` \\ or line breaks'})
                continue
            if "sq" in ctx and _SQ_UNSAFE.search(v):
                problems.append({"name": n, "why": "spliced inside single quotes: no ' or line breaks"})
                continue
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


#: how much of the runbook the frame carries. The frame rides in job metadata,
#: so it cannot be unbounded.
RUNBOOK_DISPLAY_CHARS = 12000


def _runbook_for_display(runbook: str, cmds: list[str]) -> str:
    """§17.1252 — the operator approves what they READ, so a cut must say so.

    The frame carried `runbook[:12000]` while `commands` was parsed from the FULL
    text. Live, ADD96 ("add the search sources to Prowlarr"): its runbook is over
    12,000 characters because every indexer is a `curl` with a JSON body, so the
    stored prose stopped mid-command and parsing it back yields 19 commands —
    while the block that would actually run has 24. Five commands were going to
    execute that were not in what the operator was shown, with nothing saying the
    text had been cut.

    The command list in the frame is authoritative and complete; the prose is for
    reading. So when the prose is cut, say it plainly, say how much is missing,
    and point at the list that is not.
    """
    text_value = str(runbook or "")
    if len(text_value) <= RUNBOOK_DISPLAY_CHARS:
        return text_value
    return (text_value[:RUNBOOK_DISPLAY_CHARS].rstrip()
            + f"\n\n---\n\n**This write-up is cut off here.** It is "
              f"{len(text_value):,} characters long and only the first "
              f"{RUNBOOK_DISPLAY_CHARS:,} are shown. The full block is "
              f"{len(cmds)} command{'' if len(cmds) == 1 else 's'} — every one of them is listed "
              f"above the write-up, and that list is what runs. Read it rather than this text if "
              f"the two seem to disagree.")


def frame_run(node: dict, runbook: str, spec, policy: dict, env: Optional[dict] = None,
              preconditions: Optional[list[dict]] = None, upstream: str = "") -> dict:
    """The ``awaiting_decision`` frame for a hands-on step: what would run,
    what would verify, what the gate refused (then ``run`` is not offered).

    §17.1213 — `preconditions` are refusals the MACHINE raised, not the gate:
    the block names a guest that is stopped, or does not exist, or is addressed
    with the wrong tool. They join `refused` so Run is off and the suggestion
    flips to "I'll do it myself", because a block the host already contradicts
    is not a decision for anyone.
    """
    from app.modules.assist_supervised import gate_block
    cmds = runbook_commands(runbook)
    verify = verify_commands(runbook)
    # §17.1271 — files the runbook asks to be written. They go across as MCP
    # parameters with no shell involved, so their content needs no quoting; the
    # engine compiles what it can before offering them.
    files = file_writes(runbook) if (policy or {}).get("can_write_files") else []
    files, _lookup_repairs = repair_lookups_under_set_e(files)   # §17.1288o — before anything reads them
    # §17.1288d — a check that needs a value the run never uses is dropped, not
    # refused: live, the third draft of ADD82 had fixed everything it was told
    # and still carried `ssh … <PALWORLD_IP>` in its Verify, so the frame parked
    # on the host-side first draft with the operator's Run greyed out. The run
    # is the operator's decision; a check the engine cannot fill is the
    # engine's to remove, said on the frame (`engine_fixed`). When NO check
    # would be left, the refusal stands and the redraft must supply one.
    _vhits = verify_needs_a_value_the_run_never_used(cmds, verify, files)
    _vdrops: list[dict] = []
    if _vhits and any(v not in {h["command"] for h in _vhits} for v in verify):
        _bad = {h["command"] for h in _vhits}
        verify = [v for v in verify if v not in _bad]
        _vdrops = [{"why": f"dropped the check `{h['command'][:120]}` -- {h['why'].split(':', 1)[0]}: the run "
                           f"never uses that value, so nobody has it to type; the remaining checks stand"}
                   for h in _vhits]
        _vhits = []
    inputs = inputs_for(cmds, verify, runbook, files)      # §17.1280 — a file's placeholders are inputs too
    # §17.1191 — a secret is resolved BY THE RUNNER or not at all; it is never
    # typed here and never travels through the engine.
    cmds, verify, inputs, secrets_resolved, secrets_missing = apply_runner_secrets(cmds, verify, inputs, policy)
    if inputs:                                   # §17.1188 — offer what the engine already knows
        from app.modules.runbook_inputs import suggest_inputs
        inputs = suggest_inputs(inputs, env, text=step_text(node))   # §17.1275 — and what the step says
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
    # §17.1280 — the gates judge the files as they will be WRITTEN (placeholders
    # filled with a dummy); the frame keeps them as drafted so resolve fills the
    # real values. A secret placeholder in a file has no fill and is refused.
    shape_files = ([{**f, "content": substitute([str(f.get("content") or "")], _dummies)[0]} for f in files]
                   if _dummies else files)
    _secret_files = secrets_in_files(files, inputs)
    runnable, refused = gate_block(shape, policy.get("allow") or [])
    refused = list(refused) + list(preconditions or [])      # §17.1213
    # §17.1312 — the drafter said the template's content was cut twice; the frame says it
    # too, as a refusal, so Run is withheld and the redraft chain carries the reason.
    if str(runbook or "").lstrip().startswith("<!-- runbook-cut -->"):
        _why = " ".join(_section(runbook, "Why this cannot run yet").split())[:400]
        refused = refused + [{"command": (cmds[0] if cmds else "(content cut)"), "why": f"content cut: {_why}"}]
    # §17.1234 — a write that cannot report an HTTP error is a SHAPE problem the
    # engine made, so it joins the gate's refusals and the §17.1196 redraft gets
    # a chance to fix it before the operator ever sees the block.
    refused = refused + curl_writes_without_fail(cmds)
    # §17.1248 — a pipe out of `pct exec` executes on the HOST.
    refused = refused + pipe_escapes_the_guest(cmds)
    # §17.1283 — a runner older than helper 19 elevates only the head of a line.
    refused = refused + compound_write_on_a_head_only_runner(cmds, policy)
    # §17.1285 — a step about a guest whose commands never leave the host.
    refused = refused + commands_never_reach_the_guest(cmds, node, shape_files)
    # §17.1270 — repair the one quoting mistake that is provably a mistake, and
    # prove the repair by compiling it, before anything is refused for it.
    cmds, _repairs = repair_shell_quoted_payloads(cmds)
    _repairs = list(_repairs) + _vdrops + _lookup_repairs  # §17.1288d/o — a dropped check, a repaired lookup: corrections too
    from app.modules.runbook_templates import template_of
    _tpl = template_of(runbook)
    if _tpl:                                               # §17.1290 — said on the frame: the shape is the engine's
        _repairs = _repairs + [{"why": f"drafted from the engine's template `{_tpl}`: the script's shape is the engine's "
                                       f"own, pre-gated; the model wrote only the commands that run inside the guest"}]
    # §17.1271 — and judge the written files the same way the commands are judged.
    refused = refused + file_writes_will_not_work(shape_files) + _secret_files
    # §17.1255 — an inline `-c '…'` payload with escaped quotes cannot parse.
    # §17.1257 — compile what the block hands to another interpreter. Supersedes
    # §17.1255 and §17.1255b, which pattern-matched two shapes of the same bug.
    refused = refused + payload_will_not_compile(cmds)
    # §17.1256 — a written script that reads a secret the runner never passes.
    refused = refused + script_secret_not_passed(cmds, shape_files)
    # §17.1288 — a check must use what the run used; a secret rides no ssh
    # command line; the neighbour table is read warm. Three readings of one frame.
    refused = refused + _vhits                            # §17.1288 — only when no check would be left
    refused = refused + secret_in_an_ssh_command_line(cmds, shape_files)
    refused = refused + reads_the_neighbour_table_cold(cmds, shape_files)
    # §17.1288h — a literal account into the step's guest is a guess.
    refused = refused + ssh_assumes_the_guests_account(cmds, node, env, files)
    # §17.1288l — an address the block reads is not an input; the host's own
    # address is not the guest's.
    refused = refused + reads_the_address_and_asks_for_it(cmds, files)
    refused = refused + targets_the_host_as_the_guest(cmds, node, env, files)
    # §17.1306 — a sample value written to disk is a config that is wrong on purpose.
    refused = refused + placeholder_values_in_files(cmds, files, env)
    refused = refused + invented_email_in_files(cmds, files, env, node)       # §17.1307
    refused = refused + unsourced_addresses_in_files(cmds, files, env, node, upstream)   # §17.1312
    # §17.1288m — the held password is referenced, not asked for again; a
    # wait pings the guest, not the router. (A secret as an ARGUMENT by name,
    # `--key $TOKEN`, is the §17.1191/1193 contract and is not refused.)
    refused = refused + asks_for_a_secret_the_store_holds(
        [str(i.get("name") or "") for i in inputs if i.get("secret")], policy, node)   # the ones still ASKED
    refused = refused + waits_on_a_fixed_address(cmds, node, files)
    # §17.1288k — a file written and never run is a draft with nothing to run,
    # said as a refusal so the redraft is told and the chain goes on.
    if files and not cmds:
        refused = refused + [{"command": f"the file {files[0].get('path')}", "why": (
            f"writes {files[0].get('path')} and nothing runs it: no command under ## Run this names the file. "
            f"Add the one that runs it there -- `MASS_PASSWORD=\"$MASS_PASSWORD\" bash {files[0].get('path')}` "
            f"for a bash script that reads that secret -- after the ## Write these files section if you like, "
            f"under its own `### Run the script` heading.")}]
    # §17.1268 — work that cannot finish in the time one command is given. A
    # refusal rather than a rule, because §17.1267 put the budget in the prompt
    # and the next draft looped over all 89 again with a sleep added.
    refused = refused + loops_the_network_without_a_budget(cmds, shape_files)
    # §17.1274 — a loop over third parties must survive one of them hanging; the
    # live script died at indexer 40 of 89 on a TimeoutError its `except HTTPError`
    # never saw. Prose since §17.1263; a gate now.
    refused = refused + loop_dies_on_one_dead_party(cmds, shape_files)
    # §17.1278 — and it must tell a dead tracker from a bad request the way the
    # service does: by whether a field is NAMED, not whether the key is present.
    refused = refused + classifies_by_key_presence(cmds, shape_files)
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
    # §17.1271 — writing a file IS work the block does, so a step whose commands
    # are a single `python3 /tmp/x.py` plus the file it needs is a normal block.
    _has_work = bool(cmds or files)
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
        # §17.1271 — the operator approves the FILES as well as the commands, so
        # the frame carries each path, its size and its content.
        "file_channel": bool((policy or {}).get("can_write_files")),   # §17.1276b
        "files": [{"path": f["path"], "bytes": len(str(f["content"]).encode()),
                   "lines": str(f["content"]).count("\n") + (0 if str(f["content"]).endswith("\n") else 1),
                   "content": f["content"]} for f in files],
        # §17.1270 — a command the engine CORRECTED is a changed command, and the
        # operator approves what they are shown. Never a silent repair.
        "engine_fixed": [r["why"] for r in _repairs],
        "secrets_resolved": secrets_resolved, "secrets_missing": secrets_missing,
        "runbook": _runbook_for_display(runbook, cmds), "allow": list(policy.get("allow") or []),
        "sudo": bool(policy.get("sudo")),
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
        _all, problems = check_inputs([i["name"] for i in asked], inputs,
                                      texts=commands + verify_cmds + [str((f or {}).get("content") or "")
                                                                       for f in waiting.get("files") or []])
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
    # §17.1271 — the files this block needs, written first and each one with its
    # own signed approval. They come BEFORE the commands because the commands are
    # what run them, and a failure to write stops the block exactly as a failed
    # command does: the records share one shape and one sequence.
    _files = [{"path": f["path"], "content": f["content"]}
              for f in (waiting.get("files") or []) if (f or {}).get("path")]
    # §17.1280 — the values the operator supplied (or the machine answered) go
    # into the files as well as the commands; a placeholder left over is a
    # problem to report, never bytes on disk.
    _files, _left = fill_files(_files, values)
    if _left:
        return {"outcome": "inputs_missing", "problems": _left, "inputs": asked}
    logger.warning("supervised_run_started job=%s node=%s runner=%s files=%d commands=%d secrets=%d",
                   job_id, node_key, spec.name, len(_files), len(runnable), len(secret_env))
    executed = await _sw.write_files_on(spec, _files) if _files else []
    _wrote_all = len(executed) == len(_files) and all(e["ok"] for e in executed)
    if _wrote_all:
        executed = executed + await _sw.run_block(spec, runnable, env=secret_env)
    # §17.1201 — a read that answered "no" is not a failed block (`grep` exits 1
    # when the thing it looked for is gone, which is often the check passing).
    # §17.1271 — every file and every command, all of them accounted for.
    ok = (bool(executed) and len(executed) == len(_files) + len(runnable)
          and all(e["ok"] or e.get("informational") for e in executed))
    # §17.1225 — a lost RESPONSE is not a failed COMMAND. `dropped` is computed
    # here, before the verify decision, because the verify probes are exactly
    # what settles an unknown outcome — see `_goal_confirmed`.
    dropped = [e for e in executed if e.get("unreachable")]
    hard_fail = [e for e in executed
                 if not e["ok"] and not e.get("informational") and not e.get("unreachable")]
    indeterminate = bool(dropped) and not hard_fail
    verify_out = ""
    confirmed_after_drop = False
    unreadable = False
    refuted: list[dict] = []
    if verify_cmds and (ok or indeterminate):
        try:
            pasted, ran = await run_verify(spec, verify_cmds, secret_env)      # §17.1281
            verify_out = _probe_report(pasted, ran)
            if indeterminate:
                # §17.1239 — the runner was still recovering from the dropped
                # stream, so the check that would settle it came back EMPTY.
                # Live, ADD110: `pct start 120` lost its response, `pct status
                # 120` then printed nothing, the step was recorded failed — and
                # the container was running. An unreadable check is not evidence
                # against the work (§17.1204's rule, applied to our own probe);
                # give the channel one more moment and ask again.
                if not _has_evidence(pasted, ran):
                    await asyncio.sleep(_RECHECK_DELAY_S)
                    pasted2, ran2 = await run_verify(spec, verify_cmds, secret_env)   # §17.1281
                    if _has_evidence(pasted2, ran2):
                        pasted, ran = pasted2, ran2
                        verify_out = _probe_report(pasted, ran)
                        logger.warning("verify_recheck_got_evidence job=%s node=%s", job_id, node_key)
                    else:
                        unreadable = True
                        logger.warning("verify_recheck_still_blank job=%s node=%s", job_id, node_key)
                if not unreadable:
                    confirmed_after_drop = await _goal_confirmed(
                        str(waiting.get("title") or node_key), verify_cmds, pasted)
            elif ok:
                # §17.1233 — and judge them when the commands "succeeded" too.
                #
                # Live, ADD96: nine `curl -s -X POST` calls to Prowlarr's API,
                # every one answered with a validation error array ("'App Profile
                # Id' must be greater than '0'"), nothing added — and every one
                # exited 0, because `curl -s` succeeds at fetching a 400. The
                # step was recorded `done`. All four verify checks came back
                # EMPTY, sitting in the same record, and nothing read them:
                # §17.1225 judged the checks only when a response was lost.
                # Judging the transport and not the answer is the whole bug.
                #
                # Asymmetric on purpose: only a `contradicted` verdict downgrades
                # a step. An ambiguous check must never fail work that really
                # happened, so `unknown` leaves the outcome alone.
                _against = contradicted(await _verify_verdicts(
                    str(waiting.get("title") or node_key), verify_cmds, pasted))
                if _against:
                    ok = False
                    refuted = _against
                    logger.warning("supervised_run_verify_contradicted job=%s node=%s checks=%d first=%r",
                                   job_id, node_key, len(_against),
                                   str(_against[0].get("reason"))[:140])
        except Exception as exc:
            verify_out = f"(verify could not run: {exc})"
    # §17.1281 — nothing a run printed reaches the record, the response or the
    # log with an engine-held value or a secret-shaped token in it.
    for e in executed:
        e["output"] = scrub_run_output(e.get("output"), secret_env)
    verify_out = scrub_run_output(verify_out, secret_env)
    output = mask_secrets(_executed_report(runbook, spec.name, executed, verify_out), values, need)
    # §17.1258 — a block that repeated one failure is not a success, whatever the
    # exit codes said. The commands "worked"; the work did not.
    _repeated = repeated_identical_failures(
        "\n".join(str(e.get("output") or "") for e in executed))
    if _repeated and ok:
        ok = False
        repeated_reason = _repeated
        logger.warning("supervised_run_repeated_failure job=%s node=%s", job_id, node_key)
    else:
        repeated_reason = ""
    # §17.1310 — bash exits 0 after "here-document at line N delimited by end-of-file":
    # the script was cut, everything after the opener ran as TEXT. Live, ADD100's
    # 345-byte script did exactly that and was recorded done.
    _cut = script_was_cut(executed)
    if _cut and ok:
        ok = False
        repeated_reason = _cut
        logger.warning("supervised_run_script_cut job=%s node=%s", job_id, node_key)
    if confirmed_after_drop:
        output += ("\n\n## The response was lost, the work was not\n\nThe connection to "
                   f"{spec.name} dropped before `{dropped[-1]['command'][:80]}` answered, so the engine "
                   "did not know whether it had run. The checks above were then read back off the "
                   "machine and they show the step's goal already met, so it is recorded as done "
                   "rather than retried — repeating a write that already happened is not a retry.")
    if ok or confirmed_after_drop:
        await db.execute(
            text("UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), updated_at = NOW(), "
                 "last_verification_reason = :why WHERE job_id = :jid AND node_key = :nk AND status = 'running'"),
            {"jid": job_id, "nk": node_key, "out": output,
             "why": (f"supervised run through {spec.name}: {len(executed)} command(s) ran, all exited 0"
                     if ok else
                     f"supervised run through {spec.name}: the response to "
                     f"`{dropped[-1]['command'][:60]}` was lost, and the verify checks read back off "
                     f"the machine confirm the step's goal is met")})
        logger.warning("supervised_run_done job=%s node=%s commands=%d confirmed_after_drop=%s",
                       job_id, node_key, len(executed), confirmed_after_drop)
        try:                                                     # §17.1315 — a finished OS install voids the in-guest work before it
            from app.modules import machine_truth as _mt
            await _mt.after_step_done(job_id, {"node_key": node_key, "title": str(waiting.get("title") or "")})
        except Exception as exc:
            logger.warning("after_step_done_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return {"outcome": "ran", "node_status": "done", "executed": executed, "verify": verify_out,
                "confirmed_after_drop": confirmed_after_drop}
    last = executed[-1] if executed else None
    # §17.1201 — the runner's connection dropped. Nobody knows whether the
    # command ran, and a write whose outcome is unknown is a different decision
    # from one that definitely failed. Say so, and do not pretend to an exit
    # code ("exited None" was what the operator saw).
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
    # §17.1278 — a block that exited on a dead third party did not fail for the
    # reason its own output claims.
    dead_reason = ((stopped_on_a_dead_party(str(last.get("output") or ""))
                    or stopped_on_a_template(str(last.get("output") or "")))
                   if last and not last.get("ok") and not last.get("refused") and not dropped else None) or ""
    if dead_reason:
        dead_reason = f"`{last['command'][:80]}` exited {last['exit']}: " + dead_reason
        logger.warning("supervised_run_stopped_on_dead_party job=%s node=%s", job_id, node_key)
    reason = mask_secrets(
        (f"the connection to {spec.name} dropped while `{dropped[-1]['command'][:80]}` was running "
         f"({dropped[-1]['output'][:120]}). "
         + ("The step's own verify checks could not be read either — they came back empty twice — so "
            "whether it ran on the machine is UNKNOWN. Read the state yourself before retrying: a "
            "repeat of a write that already happened is not a retry."
            if unreadable else
            "The step's own verify checks were then read back off the machine and did NOT show its goal "
            "met, so it is recorded as stopped." if verify_cmds else
            "Whether it ran on the machine is UNKNOWN and this step carries no verify check to settle it — "
            "check before retrying, because a repeat of a write that already happened is not the same as a "
            "retry of one that did not."))
        if dropped else
        (f"`{needs_root[-1]['command'][:80]}` could not read on that machine — the runner is unprivileged for "
         f"READ commands (the write grant covers writes only). Allow it to read as root: Settings → Machines, "
         f"or Capabilities → “Give the runner administrator rights for specific commands”.")
        if needs_root else
        ("every command exited 0, but this step's own verify checks say the work did not land: "
         + "; ".join(str(v.get("reason") or v.get("claim") or "")[:120] for v in refuted[:3])
         + ". A command that fetched an error page still exits 0 — the check is what settles it.")
        if refuted else
        dead_reason if dead_reason else
        repeated_reason if repeated_reason else
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


async def _verify_verdicts(title: str, verify_cmds: list[str], pasted: str,
                           expects: Optional[dict[str, str]] = None) -> list[dict]:
    """§17.1233 — the state-check judge's verdicts for this step's own checks.

    One place builds the probes so the drop path (§17.1225) and the success path
    read the SAME evidence the same way.
    """
    if not verify_cmds or not (pasted or "").strip():
        return []
    try:
        from app.modules.assist_state_check import judge_outputs
        # §17.1302b — the judge attributes output by splitting the paste on
        # `== <L>:<id> ==` markers (MARKER_RE: `S:ADD5`, `F:1`, `K:HOST`). The run
        # path labels its checks `V1` and `run_probes` writes `== V1 ==`, which that
        # regex does not match: every section came back missing, every verdict
        # "unknown". So §17.1225's confirm-after-drop never confirmed and §17.1233's
        # contradiction never contradicted — from the day they were written. The
        # ids are translated here, in the ONE place both paths build their probes,
        # and translated back so callers keep seeing `V1`.
        ids = {f"V{i}": f"V:{i}" for i in range(1, len(verify_cmds) + 1)}
        marked = re.sub(r"^(\s*==\s*)V(\d+)(\s*==\s*)$", r"\1V:\2\3", pasted or "", flags=re.M)
        # §17.1302 — `expect` feeds the judge's deterministic pre-pass: a runbook that
        # says "`cmd` shows `ostype: l26`" is confirmed with no model draw when it does.
        probes = [{"id": ids[f"V{i}"], "kind": "state", "claim": title, "command": c,
                   "expect": (expects or {}).get(c, "")}
                  for i, c in enumerate(verify_cmds, 1)]
        out = await judge_outputs(probes, marked)
        back = {v: k for k, v in ids.items()}
        for v in out:
            v["id"] = back.get(str(v.get("id") or ""), v.get("id"))
        return out
    except Exception as exc:
        logger.warning("verify_judge_failed err=%r", exc)
        return []


def contradicted(verdicts: list[dict]) -> list[dict]:
    """The checks that positively say the goal is NOT met.

    Only `contradicted` counts — never `unknown`. A step that really worked must
    not be marked failed because a check was ambiguous, so the asymmetry is
    deliberate: evidence AGAINST downgrades, absence of evidence does not.
    """
    return [v for v in verdicts or [] if str(v.get("verdict") or "") == "contradicted"]


#: §17.1239 — how long to let a dropped channel settle before re-reading.
_RECHECK_DELAY_S = 4


async def run_verify(spec, verify_cmds: list[str], env: Optional[dict[str, str]]) -> tuple[str, list[dict]]:
    """§17.1281 — run a step's verify checks and return ``(pasted, ran)`` in the
    ``== V1 ==`` marker form `_probe_report`, `_has_evidence` and the judge read.

    Live, ADD115's four `curl -s -H "X-Api-Key: $PROWLARR_API_KEY" …` checks came
    back BLANK: `run_probes` sends through `run_readonly`, which expands nothing,
    so the request carried an empty key, got a 401, and `curl -s` printed nothing
    -- the judge said "unknown" and `done` stood on checks that answered nothing.
    A check that references a stored value goes through the supervised path with
    the same `env` the block had (the runner expands `$NAME` there, and redacts
    it); a plain check keeps the read-only path.
    """
    from app.modules import assist_local_runner as _lr
    from app.modules import assist_supervised as _sw
    probes = [{"id": f"V{i}", "command": c} for i, c in enumerate(verify_cmds, 1)]
    with_ref = [p for p in probes if _SECRET_REF_RE.search(p["command"])]
    plain = [p for p in probes if p not in with_ref]
    pasted, ran = "", []
    if plain:
        pasted, ran = await _lr.run_probes(spec, plain)
    if with_ref:
        done = await _sw.run_block(spec, [p["command"] for p in with_ref], env=env or {})
        chunks = []
        for p, rec in zip(with_ref, done, strict=False):     # run_block stops at the first failure
            out = _lr.unwrap_guest_exec(p["command"], str(rec.get("output") or ""))   # §17.1304
            if rec.get("refused") or rec.get("unreachable"):
                ran.append({"id": p["id"], "command": p["command"], "ok": False, "ran": False,
                            "why": out[:200] or "did not run"})
                continue                                   # no marker: not evidence (§17.1204)
            chunks.append(f'== {p["id"]} ==\n{out.rstrip()}\n')
            ran.append({"id": p["id"], "command": p["command"], "ok": True, "ran": True})
        pasted += "".join(chunks)
    ran.sort(key=lambda r: int(str(r["id"])[1:]))
    return pasted, ran


def scrub_run_output(text_out: str, held: Optional[dict[str, str]]) -> str:
    """§17.1281 — what a run printed, with every engine-held value and every
    secret-shaped token masked before it is stored, returned or logged. The
    runner redacts values from ITS store and the values the engine SENT; a value
    the engine holds but did not send on this call (a read-only probe that `cat`s
    a config file) came back in clear."""
    out = str(text_out or "")
    for v in sorted((v for v in (held or {}).values() if v and len(v) >= 6), key=len, reverse=True):
        out = out.replace(v, "***")
    try:
        from app.modules.redaction import redact_secrets
        out, _kinds = redact_secrets(out)
    except Exception as exc:                        # pragma: no cover - defensive
        logger.warning("scrub_run_output_failed err=%r", exc)
    return out


def _probe_report(pasted: str, ran: list[dict]) -> str:
    """The `$ cmd` / output block the operator reads. One renderer, so a
    re-check renders identically to the first read."""
    out = []
    for e in ran or []:
        m = re.search(rf"== {e['id']} ==\n(.*?)(?=\n== V\d+ ==|\Z)", pasted or "", re.S)
        out.append(f"$ {e['command']}\n" + (m.group(1).rstrip() if m else "(no output)"))
    return "\n".join(out)


def _has_evidence(pasted: str, ran: list[dict]) -> bool:
    """Did the probes actually say anything?

    A marker present with nothing under it is evidence ("the list is empty");
    a marker that never arrived is not. All-missing means the channel did not
    answer, which must not be read as the work having failed (§17.1204).
    """
    for e in ran or []:
        if re.search(rf"== {e['id']} ==", pasted or ""):
            return True
    return False


# ── §17.1302 — a step whose own checks already pass is already done ──────────
#: a `## Verify` bullet of the form "`cmd` shows `text`" — the text is what the
#: output contains when the goal holds.
_EXPECT_RE = re.compile(r"`([^`\n]{2,200})`\s+(?:shows|prints|reports|returns|contains|lists|includes)\s+`([^`\n]{1,200})`", re.I)


def verify_expectations(runbook: str) -> dict[str, str]:
    """``{command: expected text}`` from the ``## Verify`` bullets that spell out
    what the check shows when the step's goal holds. Prose only — the fenced
    checks carry no expectation."""
    body = _FENCE_RE.sub(" ", _section(runbook or "", "Verify"))
    return {m.group(1).strip(): m.group(2).strip() for m in _EXPECT_RE.finditer(body)}


async def already_met(spec, node: dict, frame: dict, env: Optional[dict[str, str]]) -> Optional[dict]:
    """§17.1302 — read the step's OWN verify checks off the machine before the
    block is parked. When every check confirms the step's goal, the step is
    already done and nothing should run: return the evidence; else None.

    Live, ADD84 "Grow the VM 106 filesystem to fill the disk" was framed as
    `growpart /dev/sda 1` + `resize2fs` on a guest whose cloud-init had grown
    the root partition to 99.9 G on first boot (`df -h /` → 97 G of a 100 G
    disk). `growpart` exits 1 for NOCHANGE, so approving the block would have
    failed a step whose goal the machine already showed met — and the checks
    that show it were sitting in the frame's own `verify` list, on a runner
    with an open read-only channel. §17.1225 built this exact read for the
    case where a RESPONSE was lost; the case where the WORK was already done
    is the same read, a step earlier.

    Asymmetric like §17.1225: every check must be `confirmed` (the deterministic
    pre-pass on the runbook's own "`cmd` shows `text`" bullets, then the judge
    against the step's title AND its "done when" text). `unknown` or
    `contradicted` anywhere → None, and the block is parked as before.
    """
    verify_cmds = [str(c) for c in (frame or {}).get("verify") or []]
    if not verify_cmds or not (frame or {}).get("commands"):
        return None
    pasted, ran = await run_verify(spec, verify_cmds, env)
    answered = [r for r in ran or [] if r.get("ran")]
    if not _has_evidence(pasted, ran) or len(answered) != len(verify_cmds):
        # a check that did not answer is not evidence -- say which, so a verify the
        # read-only channel refuses (an ssh) is visible as the reason nothing was read
        logger.info("supervised_run_already_met_unreadable node=%s unanswered=%s",
                    (node or {}).get("node_key"),
                    [f"{r.get('id')}: {str(r.get('why') or 'no marker')[:60]}" for r in ran or [] if not r.get("ran")] or "all")
        return None
    title = str((node or {}).get("title") or (frame or {}).get("node_key") or "")
    desc = " ".join(str((node or {}).get("description") or "").split())[:400]
    claim = f"{title} — {desc}" if desc else title
    verdicts = await _verify_verdicts(claim, verify_cmds, pasted,
                                      expects=verify_expectations(str((frame or {}).get("runbook") or "")))
    if not verdicts or len(verdicts) != len(verify_cmds):
        return None
    kinds = [str(v.get("verdict") or "") for v in verdicts]
    if not all(k == "confirmed" for k in kinds):
        logger.info("supervised_run_not_yet_met title=%r verdicts=%s reasons=%s", title[:60], kinds,
                    [f"{v.get('id')}: {str(v.get('reason') or '')[:90]}" for v in verdicts if v.get("verdict") != "confirmed"])
        return None
    return {"report": _probe_report(pasted, ran), "verdicts": verdicts, "checks": len(verify_cmds)}


def already_met_record(node: dict, runner: str, met: dict) -> str:
    """The node's output when §17.1302 found the goal already met: the reads,
    verbatim, and why nothing ran."""
    title = str((node or {}).get("title") or "")
    lines = ["## Already met — nothing was run\n",
             f"Before running anything, the engine read this step's own checks off {runner}:\n",
             "```", str(met.get("report") or "").rstrip(), "```", "",
             f"Every check confirms the goal (\"{title}\"), so the block was not run and the step is recorded as done. "
             "Repeating a change the machine already shows is not a step forward.", ""]
    for v in met.get("verdicts") or []:
        why = str(v.get("reason") or "").strip()
        lines.append(f"- `{v.get('id')}` {str(v.get('claim') or '')[:90]}" + (f" — {why[:160]}" if why else ""))
    return "\n".join(lines).rstrip() + "\n"


async def _goal_confirmed(title: str, verify_cmds: list[str], pasted: str) -> bool:
    """§17.1225 — did the step's own checks, read back off the machine, show its
    goal already met?

    Used only when a command's RESPONSE was lost (`unreachable`) and nothing
    actually failed. The engine already had the means to answer this and did not
    use them: live, ADD50 ran `pct status 111` (stopped) then `pct start 111`,
    whose SSE stream ended before it answered. The step was recorded FAILED and
    the operator was told to "run `pct status 111` and tell me what it shows" —
    the very command sitting in the step's own verify list, on a host the engine
    had an open read-only channel to. The container was running. The start had
    worked; only the answer was lost.

    Reuses the state-check judge (§17.1050) — its deterministic pre-pass settles
    an obvious case with no model draw, and the model reads the rest against the
    step's goal. Requires EVERY check to be confirmed: a step recorded done on
    partial evidence is the failure mode this whole seam exists to prevent, and
    the honest fallback (`failed`, outcome unknown) is what happens today.
    """
    verdicts = await _verify_verdicts(title, verify_cmds, pasted)
    if not verdicts or len(verdicts) != len(verify_cmds):
        return False
    ok = all(str(v.get("verdict") or "") == "confirmed" for v in verdicts)
    logger.warning("goal_confirmed_after_drop title=%r checks=%d confirmed=%s verdicts=%s",
                   title[:60], len(verdicts), ok,
                   [str(v.get("verdict")) for v in verdicts])
    return ok


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
