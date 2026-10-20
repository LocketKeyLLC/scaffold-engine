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

import functools
import json
import ast
import asyncio
import logging
import os
import shlex
import re
from datetime import datetime, timezone
from pathlib import Path
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


async def channel_state(db: AsyncSession) -> tuple[str, str]:
    """``(state, detail)`` — WHY there is no write channel, when there is none.

    §17.1369 — `channel()` returns None for three different situations and the
    callers cannot tell them apart:

      * `"off"`        the valve is off, or MCP is disabled
      * `"none"`       no runner is registered at all
      * `"unreachable"` a runner IS registered and its policy could not be read

    The third is a GAP in what the engine knows, not a fact about the work. Live,
    2026-10-05: the Proxmox host went off the LAN (`no route to host`, no ping),
    `write_policy` raised `McpError: All connection attempts failed`, `channel()`
    answered None, and the hands-on gate reclassified **151 of 169 steps** and
    parked the whole job in `awaiting_assist` — handing the operator a plan to
    carry out by hand because one probe had failed. Absence of a reading rendered
    as a conclusion, which is §17.1363's shape at the job level.

    `"open"` with the runner's name when a channel exists.
    """
    if not enabled() or not settings.mcp_tool_enabled:
        return ("off", "")
    try:
        from app.modules import assist_local_runner as _lr
        from app.modules import assist_supervised as _sw
        spec = await _lr.runner_spec(db)
        if spec is None:
            return ("none", "")
        name = str(getattr(spec, "name", "") or "the registered runner")
        try:
            pol = await _sw.write_policy(spec)
        except Exception as exc:
            return ("unreachable", f"{name}: {type(exc).__name__}")
        if not pol:
            return ("unreachable", f"{name}: it answered with no write policy")
        return ("open", name)
    except Exception as exc:
        logger.warning("channel_state_failed err=%r", exc)
        return ("unreachable", f"the registered runner: {type(exc).__name__}")


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
                from app.modules.machine_values import logins_from_held as _mv_logins
                # §17.1548 — the NAMES feed `apply_runner_secrets` (a set), unchanged.
                # Alongside them, the login METADATA (hint + for_operator) a check needs
                # to present HTTP Basic auth to a service the engine secured: a parallel
                # key, so `held` keeps its name-set shape.
                _meta = await _rs.list_secrets(db)
                pol = {**pol, "held": [m["name"] for m in _meta],
                       "held_logins": _mv_logins(_meta)}
            except Exception as exc:
                logger.warning("runner_secret_names_unavailable err=%r", exc)
                pol = {**pol, "held": [], "held_logins": []}
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
                   "takes loopback down with it",                   # §17.1570d
                   "prints a new secret once", "a file named as a secret",   # §17.1603
                   "runs only inside the container",                          # §17.1614
                   "does not parse on its own",                               # §17.1616
                   "sed rejects this program",                                # §17.1617
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
                   "this step says it is READ ONLY",                # §17.1637
                   "a field this endpoint's objects do not have",   # §17.1694
                   "inside single quotes is left alone by the shell",   # §17.1638
                   "and nothing sets it",                           # §17.1348
                   "but set in a DIFFERENT command",               # §17.1398
                   "is expanded by GUEST",                          # §17.1364
                   "never imports or assigns",                      # §17.1377
                   "different filesystems",                         # §17.1377
                   "keep a setting in the object's `fields` array",  # §17.1378
                   "VALIDATES the whole resource",                   # §17.1378b
                   "does not accept",                               # §17.1379
                   "definition in this program takes fewer",        # §17.1379
                   "cannot witness the work",                       # §17.1382
                   "the verify channel will refuse this check",      # §17.1390
                   "a credential into the block as a literal value",  # §17.1391
                   "points a PUBLIC DNS name at a private address",  # §17.1453
                   "is the only field this rewrite of net0 may change",  # §17.1457
                   "lines it holds now would be lost",              # §17.1457
                   "lives on the Proxmox HOST, not inside a guest",  # §17.1459
                   "not inside a guest, which has none of it",      # §17.1473
                   "is read inside the guest, where the runner never sends it",   # §17.1488
                   "a note the drafter left for a person",          # §17.1460
                   "is regenerated by cloud-init from the VM's Proxmox settings on every boot",   # §17.1468
                   "is run as a READ, unprivileged",                # §17.1462
                   "identifies nothing",                            # §17.1382b
                   "which does not hold one",                       # §17.1383
                   "is a value the engine READS",                   # §17.1384
                   "nothing can give it the key",                   # §17.1385
                   "type-checked this program before offering it",  # §17.1380
                   "has no check at all",                           # §17.1345
                   "reads every `$UPPER` reference as a secret",     # §17.1578
                   "writes the secret's NAME where its VALUE belongs",   # §17.1581
                   "the call can only fail",                        # §17.1346
                   "this edit changes NOTHING",                     # §17.1353
                   "no check reads that API back",                  # §17.1362
                   "pasted inside a quoted shell word",             # §17.1367
                   "reaches port",                                  # §17.1368
                   "same API the check reads",                      # §17.1360
                   "this file RUNS ITSELF",                         # §17.1355
                   "reads only the engine's OWN script",            # §17.1355
                   "rewrites the whole file when it stops",         # §17.1346
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
                   "is not a unit anything the engine holds names",  # §17.1327
                   "cannot resolve names",                         # §17.1313
                   "content cut",                                  # §17.1312 (the template draw was cut twice)
                   "ends inside a heredoc",                        # §17.1310
                   "nothing has put this host's key on guest",     # §17.1288g
                   "is a VM on this host, not a container",        # §17.1213
                   "is a container on this host, not a VM",        # §17.1213
                   "is ALREADY",                                   # §17.1240
                   "is already taken on this host",                # §17.1243
                   "there is no guest",                            # §17.1213
                   "has never been written",                       # §17.1288p
                   "is not an interface on this host",             # §17.1395
                   "is fed input here without `--pass-stdin 1`",   # §17.1397
                   "forgets a host key and pins nothing",          # §17.1399
                   "answers JSON, and this hands that JSON to a text tool",   # §17.1400
                   "the engine writes a block's files on the Proxmox",   # §17.1403
                   "the rehearsal ran this block against copies of the real files",   # §17.1409
                   "a Proxmox HOST tool",                          # §17.1403
                   "and JSON is not shell quoting",                # §17.1405
                   "is done by the calling shell, not by sudo",    # §17.1405
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


#: §17.1389 — WHOSE gap is each refusal? Four times in one day the engine
#: refused a block for something it was itself holding: §17.1332 asked the
#: operator for a key it could read, §17.1385 refused a call for a key it could
#: mint, §17.1387 refused its own escaping, §17.1388 refused a block for a check
#: it had already written into the refusal text. Each got a point fix and
#: nothing asked the question systematically.
#:
#: Three answers, and the third came out of the data rather than the design: 14
#: of these markers are raised by `runbook_preconditions` / `machine_truth`, not
#: by a shape gate at all.
#:
#:   "drafter" — the block's shape is wrong; only a redraft fixes it.
#:   "engine"  — the engine holds what is missing; it should FILL it, announced.
#:   "machine" — the machine contradicts the block; neither can fill it.
#:
#: Every marker in `_SHAPE_REFUSALS` must appear here (asserted by
#: `tests/test_whose_gap_is_it.py`), so a new gate cannot join the set without
#: someone deciding whose gap it is. That inventory, not any single filler, is
#: what stops this recurring in a fifth costume
#: ([[feedback_failsafes_are_a_registry]]).
_WHOSE_GAP: dict[str, str] = {
    # §17.1578 — the runner at hand demands a block's own `$UPPER` names: the drafter names them in lowercase
    "reads every `$UPPER` reference as a secret": "drafter",
    "writes the secret's NAME where its VALUE belongs": "drafter",     # §17.1581
    # ── the machine contradicts the block ────────────────────────────────────
    "is stopped (`": "machine",
    "fails before it starts": "machine",
    "is already taken on this host": "machine",
    "there is no guest": "machine",
    "is a VM on this host, not a container": "machine",
    "is a container on this host, not a VM": "machine",
    "is ALREADY": "machine",
    "cannot resolve names": "machine",
    "nothing has put this host's key on guest": "machine",
    "has never been written": "machine",
    "is not an interface on this host": "machine",   # §17.1395 — the NIC the step names does not exist
    "the call can only fail": "machine",
    "rewrites the whole file when it stops": "machine",
    "reaches port": "machine",
    # ── the engine holds what is missing ─────────────────────────────────────
    "has no check at all": "engine",              # §17.1388 — it composes the read-back
    "appears only in the verify": "engine",       # §17.1288d — it drops the check
    "asks the operator for a password the runner holds": "engine",   # §17.1288m
    "reads the address itself and asks the operator for it": "engine",  # §17.1288l
    "'s address: the engine measured": "engine",  # §17.1303 — measured beats written
    "nothing can give it the key": "engine",      # §17.1385 — read it, or mint it
    "is a value the engine READS": "engine",      # §17.1384
    "which does not hold one": "engine",          # §17.1383
    "the verify channel will refuse this check": "drafter",   # §17.1390 — write a read the channel takes
    "a credential into the block as a literal value": "drafter",   # §17.1391 — write the read
    "is fed input here without `--pass-stdin 1`": "drafter",       # §17.1397
    "forgets a host key and pins nothing": "drafter",              # §17.1399
    "takes loopback down with it": "drafter",                     # §17.1570d — name the interface, not `all`
    "prints a new secret once": "drafter",                        # §17.1603 — send the minted secret only to a file
    "a file named as a secret": "drafter",                        # §17.1603 — use the secret, do not print it
    "runs only inside the container": "drafter",                  # §17.1614 — docker exec it, as the job does
    "does not parse on its own": "drafter",                       # §17.1616 — one line per command, or a file
    "sed rejects this program": "drafter",                        # §17.1617 — a program sed can compile
    "this step says it is READ ONLY": "drafter",                  # §17.1637 — reads only, with what the machine has
    "a field this endpoint's objects do not have": "drafter",     # §17.1694 — the shape names the real field
    "inside single quotes is left alone by the shell": "drafter",   # §17.1638 — no `$` before an uppercase letter
    "points a PUBLIC DNS name at a private address": "drafter",    # §17.1453 — leave ip= empty (the caller's address)
    "is the only field this rewrite of net0 may change": "drafter",   # §17.1457 — keep the live net0, flip firewall
    "lines it holds now would be lost": "drafter",                 # §17.1457 — edit in place / carry every line
    "lives on the Proxmox HOST, not inside a guest": "drafter",    # §17.1459 — write /etc/pve from the host
    "not inside a guest, which has none of it": "drafter",         # §17.1473 — host reads from the host
    "is read inside the guest, where the runner never sends it": "drafter",   # §17.1488 — cross on stdin
    "a note the drafter left for a person": "drafter",             # §17.1460 — write the real content
    "is regenerated by cloud-init from the VM's Proxmox settings on every boot": "drafter",   # §17.1468 — qm set ipconfig0
    "is run as a READ, unprivileged": "drafter",                   # §17.1462 — | tee, not > FILE
    "answers JSON, and this hands that JSON to a text tool": "drafter",   # §17.1400
    "the engine writes a block's files on the Proxmox": "drafter",   # §17.1403
    "the rehearsal ran this block against copies of the real files": "drafter",   # §17.1409
    "a Proxmox HOST tool": "drafter",                                # §17.1403
    "and JSON is not shell quoting": "drafter",                      # §17.1405
    "is done by the calling shell, not by sudo": "drafter",          # §17.1405
    # ── the block's shape is the drafter's to fix ───────────────────────────
    **{m: "drafter" for m in (
        "substitution/heredoc", "redirect", "empty", "cannot report an HTTP error",
        "ON THE HOST", "not valid Python", "cannot even be split",
        "only passes a stored value", "waits on something off this machine",
        "dies at the first one that hangs", "against a",
        "whether the KEY `propertyName` appears", "by a phrase list",
        "without reading the body's `propertyName`", "has no 'needs_input' verdict",
        "a secret cannot be written into a file",
        "elevates only the first command of a line",
        "runs in the runner's own shell on the Proxmox HOST",
        "and nothing sets it", "but set in a DIFFERENT command", "is expanded by GUEST", "never imports or assigns",
        "different filesystems", "keep a setting in the object's `fields` array",
        "VALIDATES the whole resource", "does not accept",
        "definition in this program takes fewer", "cannot witness the work",
        "identifies nothing", "this edit changes NOTHING",
        "no check reads that API back", "pasted inside a quoted shell word",
        "same API the check reads", "this file RUNS ITSELF",
        "reads only the engine's OWN script", "appears nowhere the engine holds",
        "appears in nothing the engine holds",
        "is not a unit anything the engine holds names",
        "is an assumption", "inside an ssh command line",
        "reads the neighbour table cold", "is a placeholder, not a value",
        "is this host's own address", "is not waiting for the guest",
        "content cut", "and nothing runs it", "ends inside a heredoc",
        "type-checked this program before offering it",
    )},
}


#: §17.1389 — the fillers: for an "engine" gap, what the engine does about it.
#: Keyed by marker, so adding one is a line here plus the function. Each takes
#: the frame's own material and returns ``[(what to add to verify, why)]`` —
#: today only the check composer, because that is the only engine gap that was
#: being REFUSED rather than quietly handled upstream (§17.1332's read
#: substitution and §17.1288d's drop both run before the gates see anything).
_FILLERS: dict[str, str] = {
    "has no check at all": "a_check_the_engine_can_write",
}


def _channel_would_run(check: str) -> bool:
    """§17.1390 — would the read-only verify channel actually run this check?

    The engine must not fill a gap with something the channel refuses: that is
    how a frame came back `refused: 0, verify: 1` and the step was recorded done
    on exit codes with an empty verify. The channel's own predicate answers it.
    """
    if not str(check or "").strip():
        return False
    try:
        from app.modules.assist_state_check import read_only_command
    except Exception:                          # pragma: no cover - defensive
        return True                            # unknown: do not silently drop a check
    return bool(read_only_command(str(check)))


def fill_what_the_engine_holds(refused: list[dict], cmds: list[str],
                               verify: list[str], inventory: Optional[dict] = None
                               ) -> tuple[list[dict], list[str], list[dict]]:
    """§17.1389 — fill the refusals the ENGINE owns, and prove they are gone.

    Returns ``(refusals that stand, verify, corrections)``. This is a verifier
    and not another repair: a filler that produces something is only believed
    once the refusal it claimed to fix is actually absent from the re-read set.
    A filler that fires and changes nothing leaves the refusal exactly where it
    was, loudly, rather than dropping it on the strength of having tried.

    The engine never fills a "drafter" gap (the shape is the drafter's to fix)
    or a "machine" gap (nothing the engine holds can make a stopped guest run).
    """
    if not refused:
        return refused, verify, []
    out_verify, fills = list(verify), []
    kept: list[dict] = []
    for r in refused:
        why = str((r or {}).get("why") or "")
        marker = next((m for m in _SHAPE_REFUSALS if m in why), None)
        filler = _FILLERS.get(marker or "")
        if filler is None or _WHOSE_GAP.get(marker or "") != "engine":
            kept.append(r)
            continue
        from app.modules import machine_values as _mv
        produced = getattr(_mv, filler)(cmds, inventory)
        if not produced:
            kept.append(r)                     # nothing to give: the refusal stands
            continue
        usable = [(w, n) for w, n in produced if _channel_would_run(w)]
        if not usable:
            kept.append(r)                     # a filler that produced nothing usable
            continue
        for what, note in usable:
            out_verify.append(what)
            fills.append({"why": note})
        # the verifier: re-ask the rule, and keep the refusal if it still holds
        if _engine_gap_remains(marker, cmds, out_verify):
            logger.warning("refusal_filler_did_not_close_it marker=%r filler=%s", marker, filler)
            kept.append(r)
    return kept, out_verify, fills


def _engine_gap_remains(marker: str, cmds: list[str], verify: list[str]) -> bool:
    """§17.1389 — does the rule still refuse, after the fill? The rule itself is
    asked; nothing here reasons about what the fill 'should' have done."""
    if marker == "has no check at all":
        # a blank entry is not a check: the rule is asked about what would
        # actually run, which is how the first cut of this verifier read an
        # empty string as satisfaction.
        return bool(cmds) and not [v for v in verify if str(v or "").strip()]
    return False                               # an unknown marker is not claimed closed


def whose_gap(why: str) -> Optional[str]:
    """§17.1389 — ``"drafter"`` / ``"engine"`` / ``"machine"`` for one refusal."""
    text = str(why or "")
    for marker in _SHAPE_REFUSALS:
        if marker in text:
            return _WHOSE_GAP.get(marker)
    return None


def refusal_kinds(frame: dict) -> set[str]:
    """§17.1277 — which SHAPE rules a frame was refused for (the `_SHAPE_REFUSALS`
    markers its refusals carry). Two frames refused for DISJOINT kinds are a
    draft that fixed one thing and broke another, not a draft going round in
    circles -- and that one deserves one more try, told both."""
    return {s for r in (frame or {}).get("refused") or []
            for s in _SHAPE_REFUSALS if s in str(r.get("why") or "")}


def redraft_again(first: dict, second: dict) -> bool:
    """§17.1381 — is a THIRD draft worth asking for, given these two?

    The chain used to continue only when the redraft made NO progress: a draft
    with fewer refusals became the frame and the chain stopped there, so the
    drafter was cut off exactly when it was improving. MEASURED over every
    retained log (1,794 `supervised_run_*` events, 91 redrafts): 29 came back
    clean, 40 were rejected and could try again, and 22 went straight to park on
    the progress branch -- SEVEN of them ADD132, half of that step's fourteen
    drafts. Each was a draft the engine knew how to correct, parked for an
    operator cycle instead.

    True when the second draft has work to show, was refused for a SHAPE the
    drafter can act on, and either made progress, or was refused for kinds
    entirely different from the first (it fixed one thing and broke another), or
    for exactly the same kinds (§17.1288e: it followed the step text over the
    note). Partial overlap with no progress is the one shape that stops, because
    that is a draft going round in circles.
    """
    if not (second or {}).get("commands") and not (second or {}).get("files"):
        return False
    k1, k2 = refusal_kinds(first), refusal_kinds(second)
    if not k2:
        return False                           # nothing a redraft could fix
    closer = bool((second or {}).get("commands")) and \
        len(second.get("refused") or []) < len((first or {}).get("refused") or [])
    return closer or not (k1 & k2) or k2 == k1


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


def _mcp_cfg(node: Optional[dict]) -> dict:
    """§17.1551 — a node's MCP tool_config, or {} when it is not an MCP node."""
    if str((node or {}).get("tool") or "").strip().lower() != "mcp":
        return {}
    from app.modules.mcp_node import parse_tool_config
    return parse_tool_config((node or {}).get("tool_config"))


def is_mcp_write(node: Optional[dict]) -> bool:
    """§17.1551 — an MCP node whose target is the machine-runner's WRITE tool (``run_supervised``).

    Such a node CHANGES a machine, so it must not run deterministically like a read MCP node:
    ``run_supervised`` demands a single-use ``approval`` the engine mints over the exact command
    (engine↔runner auth), and minting that automatically for any planned node would remove the
    operator's oversight of machine writes. Instead the node parks for approval (``_pause_for_decision``
    frames it with ``mcp_write_runbook``), and the approved supervised path mints the approval and runs
    it secret-safely — the same gate every hands-on write already passes. Live: Jellyfin metadata-pass
    job 263e920a, T4, which hard-failed with ``approval Field required``."""
    from app.modules.assist_supervised import WRITE_TOOL, WRITE_FILE_TOOL
    cfg = _mcp_cfg(node)
    args = cfg.get("args") if isinstance(cfg.get("args"), dict) else {}
    tool = str(cfg.get("tool") or "")
    if tool == WRITE_TOOL:
        return bool(str((args or {}).get("command") or "").strip())
    # §17.1573 — the runner's FILE write is a write too. Live (job 9e1c2c68, T2/T3): the planner chose
    # `write_file` for the rule file and the unit; this predicate knew one tool name, so the node was
    # dispatched deterministically, the runner refused it (`approval Field required`) three times, and
    # the job went blocked with nothing parked for the operator. A gate that matches one word, not the
    # payload's kind (a write), is how the same defect returns under a new name.
    if tool == WRITE_FILE_TOOL:
        return bool(str((args or {}).get("path") or "").strip())
    return False


#: §17.1555 — a shell command that splices another node's output in: `${upstream.T3}`, `${brief.x}`,
#: or the bare `${T3}` the planner writes (§17.1557 renders it).
_UPSTREAM_SPLICE_RE = re.compile(r"\$\{\s*(?:(?:upstream|brief)\.[A-Za-z0-9_.\-]+|T\d+(?:v\d+)?)\s*\}")


def mcp_splices_an_upstream(node: Optional[dict]) -> bool:
    """§17.1555 — a runner-READ MCP node (``run_readonly``) whose command splices another node's
    output into shell text.

    That is not a deterministic call: the spliced text is whatever the upstream node happened to
    print (live, job 263e920a: T3's `${T2}` rendered to NOTHING, so `for lib in ; do` ran as
    `/bin/sh: 1: Syntax error`; the three verify steps spliced THAT error in as `${T3}`), it may be a
    secret (T1 read the API key, and `${T1}` would have inlined it into a command line), and the
    read-only channel cannot run the shape such a step really needs (an in-guest `python3` over the
    API). So it parks for approval like a write: ``frame_run`` sees it — the deterministic
    capabilities (§17.1554's census) replace it, the gates judge what is left — and the operator
    decides. A read with no splice (``curl -s http://…/Items``) is still a plain deterministic call."""
    cfg = _mcp_cfg(node)
    args = cfg.get("args") if isinstance(cfg.get("args"), dict) else {}
    cmd = str((args or {}).get("command") or "")
    return str(cfg.get("tool") or "") == "run_readonly" and bool(_UPSTREAM_SPLICE_RE.search(cmd))


def a_capability_claims(node: Optional[dict]) -> bool:
    """§17.1573 — True when one of the engine's deterministic host-level capabilities would replace this
    step's commands (the same rewrites ``frame_run`` applies). A runner-read MCP node such a capability
    claims must PARK and be framed by it, not run the planner's raw command: live (job 9e1c2c68), T5's
    `nft list table bridge block_ra` read a table by the plan's working name, which the engine's install
    (§17.1571, `scaffold_rafilter`) never created — a deterministic failure the capability layer could
    see and the dispatcher could not. Only capabilities that need no measured services/inventory."""
    n = dict(node) if hasattr(node, "keys") else {}
    if not n:
        return False
    for fn in (rewrite_drop_router_adverts_on_the_bridge, rewrite_verify_router_adverts_dropped):
        try:
            if fn(n, ["x"], ["y"], None, None)[2]:
                return True
        except Exception:                                      # a classifier must never break dispatch
            continue
    return False


def mcp_needs_the_pause(node: Optional[dict]) -> bool:
    """§17.1551/§17.1555/§17.1573 — an MCP node the frontier must HOLD for the operator rather than call:
    a runner write (command or file), a runner read that splices an upstream output into its command,
    or a runner read one of the engine's own capabilities claims."""
    return (is_mcp_write(node) or mcp_splices_an_upstream(node) or a_capability_claims(node)
            or bool(_mcp_cfg(node).get("needs_root")))             # §17.1574b — a read the read channel refused for privilege


def mcp_write_runbook(node: Optional[dict], upstream_outputs: Optional[dict], brief: Any) -> Optional[str]:
    """§17.1551 — the ``## Run this`` runbook for a runner-write MCP node: its command with the
    ${upstream.*}/${brief.*} placeholders rendered, wrapped in a fence so ``frame_run`` offers it like
    any hands-on block and ``resolve_run`` runs it through the approval-gated, secret-safe channel.
    §17.1555 — and for a runner-read node that splices an upstream output (``mcp_needs_the_pause``).

    A secret belongs in the command as a machine-side read (``KEY=$(…)``) or a ``$NAME`` reference, never
    rendered in from an upstream output — a rendered secret would be inlined into the runner's sudo log
    (§17.1430)."""
    if not mcp_needs_the_pause(node):
        return None
    from app.modules.mcp_node import parse_tool_config, render_args
    cfg = parse_tool_config((node or {}).get("tool_config"))
    rendered = render_args(cfg.get("args") or {}, upstream_outputs or {}, brief or {})
    cmd = str((rendered or {}).get("command") or "").strip() if isinstance(rendered, dict) else ""
    if not cmd and isinstance(rendered, dict) and str(rendered.get("path") or "").strip():
        # §17.1573 — a `write_file` node as the shell write it is, so the frame offers it, the
        # capabilities may replace it, and the gates judge it like any file a step leaves behind.
        path = str(rendered.get("path")).strip()
        body = str(rendered.get("content") or "")
        cmd = (f"mkdir -p {shlex.quote(os.path.dirname(path) or '/')} && "
               f"printf '%s\\n' {shlex.quote(body.rstrip(chr(10)))} > {shlex.quote(path)}")
    return "## Run this\n```bash\n" + cmd + "\n```" if cmd else None


async def pending_hands_on(db: AsyncSession, job_id: str) -> Optional[dict]:
    """The first dep-satisfied pending node that does host work (§17.1183)
    and has no recorded decision — the step the run would claim next."""
    from app.modules.step_classify import step_is_hands_on, deploy_target_guest
    # §17.1539 — read the decisions AND the refined brief in one query: the brief's deploy target is
    # what makes a CodeGen build node route to DEVELOP (deploy) rather than the sandbox executor.
    _jrow = (await db.execute(
        text("SELECT metadata->'decisions', refined_brief FROM jobs WHERE id = :jid"),
        {"jid": job_id})).first()
    decided = _as_dict(_jrow[0] if _jrow else None)
    _deploy_target = deploy_target_guest(str((_jrow[1] if _jrow else "") or ""))
    rows = (await db.execute(
        text("""
            SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on, n.tool, n.node_type,
                   n.retry_count, n.last_verification_reason, n.execution_order, n.output_text, n.tool_config
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
        # §17.1458 — a step whose rest is the operator's is theirs to finish, not the engine's to re-run
        from app.modules import operator_steps as _ops
        if isinstance(_entry, dict) and _entry.get("result") == _ops.ENGINE_HALF_DONE:
            continue
        if _entry is not None and not await decision_is_stale(db, job_id, node["node_key"], _entry):
            continue
        if str(node.get("node_type") or "") == "decision":
            continue
        if _ops.is_wholly_operators(node):
            continue
        # §17.1551 — a runner-write MCP node changes a machine, so it parks for approval like any
        # hands-on step rather than running deterministically with no engine↔runner approval.
        # §17.1555 — so does a runner-read whose command splices another node's output in.
        if mcp_needs_the_pause(node):
            node["hands_on_reason"] = ("mcp:runner-write" if is_mcp_write(node)
                                       else "mcp:spliced-read" if mcp_splices_an_upstream(node)
                                       else "mcp:needs-root" if _mcp_cfg(node).get("needs_root") else "mcp:capability")
            return await inherit_referenced_step_text(db, job_id, node)     # §17.1573b
        _dc = str(node.get("tool") or "").strip().lower() == "codegen" and bool(_deploy_target)
        on, why = step_is_hands_on(node, develops_codegen=_dc)
        if on and not why.startswith("tool:human"):
            node["hands_on_reason"] = why
            return await inherit_referenced_step_text(db, job_id, node)     # §17.1570c
    return None


#: §17.1570c — a step that defines itself by another step: "Same contents as T2", "as in T3", "like T5".
_REFERENCED_STEP_RE = re.compile(r"\b(?:same\s+(?:\w+\s+){0,3}as|as\s+in|like|identical\s+to|mirror(?:s|ing)?)\s+(T\d+(?:v\d+)?)\b", re.I)


#: §17.1573b — a step whose object is "the table / the unit / the rule / the file …" (a definite reference)
_ANAPHORIC_RE = re.compile(r"\bthe\s+(?:dedicated\s+|new\s+|same\s+)?(?:table|unit|rule(?:\s*file)?|file|service|config(?:uration)?|"
                           r"counter|drop\s+rule|systemd\s+unit)\b", re.I)
#: … and one that names it anyway (a path, a unit file, a table name, a step reference) needs nothing inherited
_NAMES_ITS_OBJECT_RE = re.compile(r"/(?:etc|opt|usr|var|home|root)/\S+|\b[\w.-]+\.(?:service|timer|conf|nft)\b|\btable\s+(?:bridge|inet|ip6?)\s+\w+|\[as in T\d+", re.I)


async def inherit_referenced_step_text(db: AsyncSession, job_id: str, node: dict, *, _depth: int = 0) -> dict:
    """§17.1570c — a step whose text points at another step carries that step's text.

    Live (job eff80f98): the planner wrote the work once — T2 "Writes the sysctl config file … Contents:
    net.ipv6.conf.eth0.disable_ipv6=1, …" — and then T5/T8/T11/… as "Writes the sysctl config file inside
    container 102 (prowlarr) only. Same contents as T2." Every deterministic capability reads the step's
    OWN text, so T2 was claimed by §17.1570 and T5 was left to the model, which set
    `net.ipv6.conf.all.disable_ipv6=1` and took loopback IPv6 down with it. The referenced step's title
    and text are appended to the node's description (once), so the capabilities and gates see the whole
    specification. Fail-soft: an unreadable reference leaves the node as it was."""
    own = _node_text(node)
    refs = {m.group(1) for m in _REFERENCED_STEP_RE.finditer(own)}
    # §17.1573b — a step that speaks of "the table", "the unit", "the rule file" it never names is defined by
    # the step it depends on (live, job 9e1c2c68: T3 "oneshot unit that loads the dedicated table", T4 "Loads
    # the table now", T7 "Confirms the unit is active"). Its direct dependencies' text is inherited the same
    # way; a dependency that is itself such a step brings its own dependencies (one more level).
    if _ANAPHORIC_RE.search(own) and not _NAMES_ITS_OBJECT_RE.search(own):
        refs |= {str(d) for d in (node.get("depends_on") or []) if re.fullmatch(r"T\d+(?:v\d+)?", str(d))}
    refs = sorted(refs - {str(node.get("node_key") or "")})
    if not refs:
        return node
    try:
        rows = (await db.execute(
            text("SELECT node_key, title, description, prompt_template FROM dag_nodes "
                 "WHERE job_id = :jid AND node_key = ANY(:keys)"),
            {"jid": job_id, "keys": refs})).mappings().all()
    except Exception as exc:
        logger.warning("referenced_step_lookup_failed job=%s node=%s err=%r", job_id, node.get("node_key"), exc)
        return node
    extra = []
    for r in rows:
        body = " ".join(str(r.get(k) or "").strip() for k in ("title", "description", "prompt_template") if r.get(k))
        if body and f"[as in {r['node_key']}:" not in str(node.get("description") or ""):
            extra.append(f"[as in {r['node_key']}: {body[:1200]}]")
            # §17.1573b — one more level when the dependency is itself anaphoric ("loads the table")
            if _depth == 0 and _ANAPHORIC_RE.search(body) and not _NAMES_ITS_OBJECT_RE.search(body):
                try:
                    deps = (await db.execute(
                        text("SELECT depends_on FROM dag_nodes WHERE job_id = :jid AND node_key = :k"),
                        {"jid": job_id, "k": r["node_key"]})).scalar()
                    up = (await db.execute(
                        text("SELECT node_key, title, description, prompt_template FROM dag_nodes "
                             "WHERE job_id = :jid AND node_key = ANY(:keys)"),
                        {"jid": job_id, "keys": [str(d) for d in (deps or [])]})).mappings().all()
                    for u in up:
                        ub = " ".join(str(u.get(k) or "").strip() for k in ("title", "description", "prompt_template") if u.get(k))
                        if ub and f"[as in {u['node_key']}:" not in str(node.get("description") or ""):
                            extra.append(f"[as in {u['node_key']}: {ub[:1200]}]")
                except Exception as exc:
                    logger.warning("referenced_step_lookup_failed job=%s node=%s err=%r", job_id, node.get("node_key"), exc)
    if extra:
        node = dict(node)
        node["description"] = (str(node.get("description") or "").rstrip() + "\n" + "\n".join(extra)).strip()
        logger.info("referenced_step_text_inherited job=%s node=%s from=%s", job_id, node.get("node_key"), refs)
    return node


def is_hands_on_node(node: dict, brief: str = "") -> bool:
    # §17.1539 — a CodeGen node in a job whose brief names a deploy target is a DEVELOP step
    # (it must write files to that machine), so it is hands-on; a self-contained CodeGen artifact
    # (no deploy target) is not. `brief` is threaded from the job so the peek can tell them apart.
    from app.modules.step_classify import step_is_hands_on, deploy_target_guest
    _n = dict(node) if hasattr(node, "keys") else {}
    # §17.1551 — a runner-write MCP node changes a machine, so the frontier must NOT run it
    # deterministically: it is hands-on and parks for approval (the peek/claim both read this).
    # §17.1555 — likewise a runner-read that splices an upstream output into its command.
    if mcp_needs_the_pause(_n):
        return True
    _dc = str(_n.get("tool") or "").strip().lower() == "codegen" and bool(deploy_target_guest(brief))
    on, why = step_is_hands_on(_n, develops_codegen=_dc)
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
- A SCRIPT you write does not inherit a stored value. The runner passes one only into a command whose text mentions `$NAME`, so `printf … | tee /tmp/x.py` then a bare `python3 /tmp/x.py` starts with no such variable. Either have the script read the key off the machine (above), or put the reference in the command that runs it as a trailing comment: `python3 /tmp/x.py # $NAME` (never a `NAME="$NAME"` prefix — sudo logs it). A password a CALL needs (a login) is fed to that call on stdin from the host instead: `pct exec <id> -- sh -c 'tr -d "\\n" | curl … --data-urlencode password@- …' <<< "$NAME"`.
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
engine TYPE-CHECKS every program you write here -- in a `.py` file or in a heredoc -- before the operator
is asked, so a name nothing imports, a call its own `def` rejects, and a syntax error are all caught here
and come back to you as a refusal rather than reaching the machine.

BATCHING ON THIS CHANNEL (one command is one 180-second budget): a script that works through a list a
service returned MUST take its slice bounds as arguments and be run once per batch -- write it ONCE, then
list one command per batch:

```python
import json, sys
start, end = int(sys.argv[1]), int(sys.argv[2])
items = json.load(open("/tmp/indexers.json"))     # the list the service returned
for item in items[start:end]:
    ...   # one item, with (urllib.error.URLError, TimeoutError, OSError) caught and recorded as unreachable
```

```bash
python3 /tmp/add_indexers.py 0 10
python3 /tmp/add_indexers.py 10 20
```

Ten per batch when each item waits on a remote service. A loop over batches INSIDE one script is still one
command and is refused.

A REQUEST BODY IS A FILE, NEVER A SHELL WORD. When you send JSON you did not write yourself -- an object
you fetched and changed, anything a machine gave you -- do NOT put it in the command. One apostrophe in
that value ends the quote and the shell dies on the next bracket. MEASURED on this host: Radarr's
download-client object is 6,182 bytes and holds three apostrophes, because qBittorrent's own help text
says "See Options -> Web UI -> 'Use HTTPS instead of HTTP' in qBittorrent" -- and
`-d '{...that object...}'` died with `sh: 1: Syntax error: "(" unexpected`. Three drafts in a row died
that way, each with different quoting.

Write the body to a file and point curl at the file:

## Write these files

### /tmp/body.py
```python
import json, subprocess
cur = json.loads(subprocess.run(["pct","exec","103","--","sh","-c",
      "curl -s -H 'X-Api-Key: KEY' http://127.0.0.1:7878/api/v3/downloadclient/1"],
      capture_output=True, text=True).stdout)
cur["priority"] = 1
open("/tmp/body.json","w").write(json.dumps(cur))
```

## Run this

```bash
python3 /tmp/body.py | pct exec 103 -- sh -c 'curl -s --fail-with-body -X PUT -H "X-Api-Key: $KEY" -H "Content-Type: application/json" -d @- http://127.0.0.1:7878/api/v3/downloadclient/1'
```

`-d @/path` and `-d @-` (with the body piped in) are the only two shapes that carry a value whose
characters you do not control. Better still, do the whole call in Python with `urllib.request` and
`data=json.dumps(obj).encode()`: then no shell sees the body at all.

THE FILE MUST EXIST ON THE MACHINE THAT READS IT. A guest's `/tmp` is not the host's. The file
sections above, and anything a host-side `python3` or `>` writes, land ON THE HOST -- so
`pct exec 103 -- sh -c 'curl … -d @/tmp/body.json'` hands curl inside 103 a path that is not there,
and curl sends nothing. MEASURED on this host, live: a draft wrote `/tmp/radarr_dc_update.json` with a
host-side program and read it with `-d @/tmp/radarr_dc_update.json` inside guest 103; the body was in
a file and every line ran where it claimed to, and the call still could not work.

So when the command that reads the body runs inside a guest, pick one:

- PIPE IT, as above -- `python3 /tmp/body.py | pct exec 103 -- sh -c '… -d @- …'`. Nothing is written
  in the guest and no path has to match. Prefer this.
- Or CREATE IT IN THE GUEST, in the same `pct exec` that reads it:

```bash
pct exec 103 -- sh -c 'cat > /tmp/body.json <<"JSON"
{"id": 1, "priority": 1}
JSON
curl -s --fail-with-body -X PUT -H "X-Api-Key: $KEY" -H "Content-Type: application/json" -d @/tmp/body.json http://127.0.0.1:7878/api/v3/downloadclient/1'
```

A PROGRAM IS A WHOLE PROGRAM, in a heredoc or in a `.py` file: every name it uses it must import,
and every call must match the signature it wrote. `python3 - <<'EOF'` that says `import json` and
then reaches for `os.environ[...]` raises NameError on the first line that runs; and MEASURED live,
`def pct_exec(ctid, cmd)` called as `pct_exec(ctid, cmd, input=body)` gave
`TypeError: pct_exec() got an unexpected keyword argument 'input'` at both call sites, after that
draft had everything else right. If a helper has to forward stdin, give it the parameter and pass it
on: `def pct_exec(ctid, cmd, input=None): subprocess.run([...], input=input, ...)`. Under `set -e`
either one ends the block having changed nothing.

A SETTING GOES WHERE THE API KEEPS IT, AND A READ RETURNS A SECRET MASKED. Radarr, Sonarr and
Prowlarr keep a download client's settings in the object's `fields` array, as
`{"name": "username", "value": "admin"}` -- NOT as keys of the object. MEASURED on this host, after a
block set `c["username"]` and `c["password"]` on the object and PUT it back: both came back ABSENT,
`fields.username` was untouched, and `fields.password` held the literal asterisks the GET had masked
it with, so `downloadclient/test` answered `Authentication Failure` and the stored password was left
WORSE than before the run. Set it in place, and never PUT back a secret you only read:

```python
import json, os, sys                  # a program is whole: import every name it uses

obj = json.load(sys.stdin)            # GET -- obj["fields"] password is "********"
for f in obj["fields"]:
    if f["name"] == "username":
        f["value"] = "admin"
    elif f["name"] == "password":
        f["value"] = os.environ["MASS_PASSWORD"]      # the real one, never the mask
obj["priority"] = 1                   # `priority` IS a key of the object; `fields` names are not
print(json.dumps(obj))
```

A SERVICE'S API KEY IS READ WHERE THAT SERVICE KEEPS IT, and the engine will fill it in for you:
write `<RADARR_API_KEY>`, `<SONARR_API_KEY>`, `<PROWLARR_API_KEY>` or `<JELLYFIN_API_KEY>` in a
command or a check that already runs inside that guest and the read is substituted before you are
asked for anything. In a FILE the engine leaves alone, so write the read yourself -- and the two
families are NOT the same shape:

```python
import subprocess

# radarr / sonarr / prowlarr / lidarr / readarr / whisparr -- an element in config.xml
ARR_KEY_CMD = r"sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' /var/lib/radarr/config.xml | head -1"

# jellyfin -- a ROW in its own SQLite database, and there is no <ApiKey> element
# anywhere under /etc/jellyfin (measured: database.xml encoding.xml
# logging.default.json logging.json network.xml system.xml). The guest's own
# python3 reads it with nothing installed.
JF_KEY_CMD = (
    "python3 -c 'import sqlite3;"
    'print(sqlite3.connect("file:/var/lib/jellyfin/data/jellyfin.db?mode=ro",uri=True)'
    '.execute("SELECT AccessToken FROM ApiKeys ORDER BY rowid LIMIT 1").fetchone()[0])'
    "'"
)

def guest_key(ctid, cmd):
    r = subprocess.run(["pct", "exec", str(ctid), "--", "sh", "-c", cmd],
                       capture_output=True, text=True)
    key = r.stdout.strip()
    if not key:
        raise SystemExit(f"no API key from {ctid}: {r.stderr.strip()}")
    return key
```

Never grep `<ApiKey>` out of a Jellyfin file: it returns nothing, the header goes out empty, and the
answer is a 401 that reads like a wrong key rather than one that was never there.
And never LOG IN to a service whose key is on the disk. A drafted `POST /Users/AuthenticateByName`
with a guessed username and the mass password answers **HTTP 400** on this server (that endpoint
also wants an `X-Emby-Authorization` header), and `GET /Users/Public` answers `[]` -- there is no
such user. qBittorrent is the opposite case: it has no readable key, so `POST /api/v2/auth/login`
with `admin` and `$MASS_PASSWORD` is exactly right there.

A CALL THAT NEEDS A READABLE KEY RUNS INSIDE THE GUEST. A program running on the HOST cannot get
Jellyfin's key: the engine fills `<JELLYFIN_API_KEY>` only in a command or a check that runs inside
that guest, and the runner injects only the names its own stores hold. The Verify section is the
natural home for it, because *"the library lists the title"* is a READ:

```bash
pct exec 101 -- sh -c 'curl -s -H "X-Emby-Token: $(python3 -c '"'"'import sqlite3;print(sqlite3.connect("file:/var/lib/jellyfin/data/jellyfin.db?mode=ro",uri=True).execute("SELECT AccessToken FROM ApiKeys ORDER BY rowid LIMIT 1").fetchone()[0])'"'"')" "http://127.0.0.1:8096/Items?Recursive=true&IncludeItemTypes=Movie&SearchTerm=<TITLE>"'
```

If that read says `ApiKeys is empty`, the service has no key yet and the engine makes one -- the
operator approves it like any other change, so put it ONCE under `## Run this`, never in Verify (it
restarts the service). It prints the existing key and changes nothing when there already is one.

A `/test` ENDPOINT VALIDATES THE WHOLE RESOURCE. `-d '{}'` to `…/api/v3/downloadclient/test` answers
`'Name' must not be empty`, `'Implementation' must not be empty`, `'Config Contract' must not be
empty` and `'Priority' must be between 1 and 50. You entered 0` -- measured. GET the object and POST
that same document to `/test`. And put the check where a failure means something: a malformed test
after a correct PUT reports the whole step `failed` while its work is already done.
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


def empty_frame_disposition(frame: dict, node: dict) -> Optional[tuple]:
    """§17.1548b — what to do with a hands-on frame the engine drafted into
    NOTHING: no command to run, no check to judge, and no refusal explaining why.
    Such a frame is a dead park — ``decide:run`` returns ``not_runnable`` 409 every
    time and the job hangs on a decision that can never be taken (live, job
    ae721781: the degenerate split-fragment verifies T22v2/T23v2 re-drafted empty
    every round and 409-looped the driver).

    Returns ``('fail', why)`` for a VERIFY step — it verifies nothing, so it fails
    clean and the frontier moves on (the gap is visible, the §17.1547 way: an
    absent check is a failed check, not a silent skip); ``('refuse', refusal)`` for
    any other step — park a real myself/skip/redraft decision, never a dead
    ``run``; or ``None`` when the frame has something to run, judge, or explain.
    """
    if (frame or {}).get("commands") or (frame or {}).get("verify") or (frame or {}).get("refused"):
        return None
    from app.modules.step_classify import _LEADING_OBSERVE_RE
    title = str((node or {}).get("title") or "")
    vtype = str((node or {}).get("node_type") or "").lower()
    if _LEADING_OBSERVE_RE.match(title) or vtype in ("verify", "check", "checkpoint", "validation"):
        return ("fail",
                "the engine could not draft this verify step into any runnable command or check — there is "
                "nothing to run and nothing to judge, so nothing was verified. Failed rather than parked on a "
                "decision that can never be taken (§17.1548b): the step's own text (%r) gave the drafter nothing "
                "concrete to check." % (title[:120]))
    return ("refuse", {"why": (
        "this step produced no command to run and no check to judge, and nothing refused it to explain why — "
        "there is nothing the engine can carry out here. Redraft it with a concrete command, do it yourself, or "
        "skip it (§17.1548b); `run` is withheld because it would do nothing.")})


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
    if not step_says_it_changes(node):
        return ""                                  # §17.1577 — hands-on is not "changes": a probe only reads
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


#: §17.1577 — the step's own words say it changes nothing
_NO_CHANGE_RE = re.compile(
    r"\b(?:do not|don'?t|never|without)\s+(?:modify(?:ing)?|chang(?:e|ing)|alter(?:ing)?|touch(?:ing)?|writ(?:e|ing))\b"
    r"|\bread[- ]only\b", re.I)


def step_says_it_changes(node: dict) -> bool:
    """§17.1577 — does the step's TEXT say it changes a machine?

    `step_is_hands_on` answers a different question — does this need a machine — and says yes to every
    Shell/MCP node for its TOOL alone (`tool:shell`). §17.1254 read that yes as "must change something".
    Live (c2792d4b T2 "Probe each endpoint authenticated … record status code … Do not modify anything",
    tool Shell): the engine's own read-only probe (§17.1574c) was sent back as "EVERY COMMAND ONLY LOOKS
    AT THINGS", the model was asked for a write, and the operator was shown a cookie draft in place of
    the block that would run. Here the tool is put out of the way and the classifier's own TEXT analysis
    answers (a change verb opening a title that names a service, a command that writes — not a template
    the engine owns, which fits reads too), or a change verb opening the title; an observation verb opening it, or "do not modify"
    / "read-only" in its words, says no."""
    from app.modules.step_classify import step_is_hands_on, _LEADING_CHANGE_RE, _LEADING_OBSERVE_RE
    n = dict(node) if hasattr(node, "keys") else {}
    title = str(n.get("title") or "")
    text = " ".join(str(n.get(k) or "") for k in ("title", "description", "prompt_template"))
    if _LEADING_OBSERVE_RE.match(title) or _NO_CHANGE_RE.search(text):
        return False
    _on, why = step_is_hands_on({**n, "tool": "LLM"})
    return str(why or "").startswith(("verb:", "writes:")) or bool(_LEADING_CHANGE_RE.match(title))


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


#: §17.1355 — the engine's OWN scaffolding, written by `run_in_container`/`run_on_host`:
#: the wrapper the runner executes, the payload it pushes, and the path it lands on
#: inside the guest. None of these is the step's result.
_SCAFFOLD_PATH_RE = re.compile(
    r"/tmp/in_(?:ct|vm|host)_\d{2,5}(?:_remote)?\.sh|/root/\.scaffold_step(?:\.\w+)?")
#: a path at a command position: `bash P`, `sh -x P`, `source P`, `. P`, or P itself
def _invokes(text: str, path: str) -> bool:
    esc = re.escape(path)
    return bool(re.search(rf"(?:^|[\n;&|]|&&|\|\|)\s*(?:[A-Za-z_]\w*=\S*\s+)*"
                          rf"(?:(?:ba|da|z|k)?sh|source|\.)\s+(?:-\S+\s+)*{esc}\b", text)
                or re.search(rf"(?:^|[\n;&|])\s*(?:[A-Za-z_]\w*=\S*\s+)*{esc}\b", text))


def the_block_runs_its_own_scaffolding(files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1355 — a written file whose content runs the file itself.

    Live, 2026-10-04, ADD132's frame. The template writes `/tmp/in_ct_103.sh` —
    the wrapper the runner executes — and the model's payload inside it was, in
    full:

        set -e
        export DEBIAN_FRONTEND=noninteractive
        MASS_PASSWORD=… bash /tmp/in_ct_103.sh

    The step was told to run itself. Inside container 103 that path does not
    exist, so it fails; if it did exist it would recurse. There is no work in it
    at all, and nothing said so: every other gate reads the COMMANDS, which are
    the template's own `bash /tmp/in_ct_103.sh start|wait|last` and perfectly
    correct.

    Only the file's own path counts, so the template's legitimate mentions of the
    payload it writes and pushes elsewhere are untouched.
    """
    out: list[dict] = []
    for f in files or []:
        path = str((f or {}).get("path") or "").strip()
        body = str((f or {}).get("content") or "")
        if not path.startswith("/") or not body.strip():
            continue
        if not _invokes(body, path):
            continue
        out.append({"command": path, "why": (
            f"this file RUNS ITSELF: {path} is the script the engine writes to carry this step out, and "
            f"the commands inside it invoke that same path. Inside the guest it does not exist, so the "
            f"step fails; if it did exist it would recurse. Either way there is no work in it. Live "
            f"(§17.1355), ADD132's whole payload was `MASS_PASSWORD=… bash {path}` and the step was told "
            f"to run itself. Write the commands that do the work -- the `curl` against the service's API, "
            f"the edit, the restart -- not a call to the engine's own wrapper.")})
    return out


def a_check_that_only_reads_the_scaffolding(verify: list[str],
                                            files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1355 — a check that reads only the engine's own script proves nothing.

    Live, ADD132's frame offered exactly one check:

        pct exec 103 -- bash -c "ls -la /tmp/in_ct_103.sh"

    That file is the engine's wrapper, it lives on the HOST and not in the guest,
    and its existence says nothing about Radarr, Sonarr or qBittorrent. §17.1345
    asks for a check and this satisfies it while confirming nothing — the same
    hole §17.1343 fell through, one level down.

    A check that reads a config the block WROTE is a real check (§17.1343 asks
    for exactly that), so only the engine's own scaffolding paths count here.
    """
    cmds = [str(c) for c in (verify or []) if str(c).strip()]
    if not cmds:
        return []                      # §17.1345's refusal covers an empty check
    own = {str((f or {}).get("path") or "") for f in (files or [])}
    def _only_scaffolding(cmd: str) -> bool:
        paths = re.findall(r"/[A-Za-z0-9_./-]+", cmd)
        real = [p for p in paths
                if not _SCAFFOLD_PATH_RE.fullmatch(p) and p not in own]
        return bool(paths) and not real
    if not all(_only_scaffolding(c) for c in cmds):
        return []                      # at least one check looks at something else
    return [{"command": cmds[0][:120], "why": (
        "every check in this block reads only the engine's OWN script for this step, not the result the "
        "step is supposed to leave. Live (§17.1355), ADD132's only check was "
        "`pct exec 103 -- ls -la /tmp/in_ct_103.sh` -- the wrapper the runner executes, which lives on the "
        "host and not in the guest, and whose existence says nothing about whether Radarr and Sonarr can "
        "reach the download client. Name a command that reads the RESULT: the value back out of the "
        "service's API, the key inside its section, the port answering. A step confirmed by the presence "
        "of the engine's own file is a step recorded done having changed nothing.")}]


def logical_lines(text: str) -> list:
    r"""The text's lines, with `\` continuations joined into one command.

    §17.1373 — every gate that scans line by line is blind to the way the drafter
    actually writes a long `curl`:

        curl -s --fail-with-body -X PUT \
            -H "X-Api-Key: $RADARR_KEY" \
            -d '{"id":1,…}' \
            http://192.168.1.22:7878/api/v3/downloadclient/1

    The method is on line 1 and the URL on line 4, so `api_ports_changed` saw a
    write with no URL and a URL with no write, and §17.1362 — the rule that a
    change through an API must be read back — returned nothing. Measured live,
    2026-10-05, on the ADD132 draft that was then accepted with a single
    `systemctl is-active` check. §17.1367 and §17.1368 learned the same lesson
    about whole-BLOCK judgments; this is the line-level half of it.
    """
    out: list = []
    pending = ""
    for raw in str(text or "").split("\n"):
        ln = pending + raw
        if ln.rstrip().endswith("\\"):
            pending = ln.rstrip()[:-1] + " "
            continue
        pending = ""
        out.append(ln)
    if pending:
        out.append(pending)
    return out


#: §17.1362 — an HTTP call that CHANGES something: an explicit method, or `curl -d`
#: (which is a POST unless `-X GET` says otherwise).
_HTTP_WRITE_RE = re.compile(r"-X\s*['\"]?(?P<m>POST|PUT|PATCH|DELETE)\b", re.I)
_HTTP_BODY_RE = re.compile(r"(?<![\w-])(?:-d|--data|--data-raw|--data-binary|--json)(?![\w-])")
_HTTP_GET_RE = re.compile(r"-X\s*['\"]?GET\b", re.I)
#: the port a URL in that line reaches
_URL_PORT_RE = re.compile(r"https?://[^\s'\"]*?:(?P<port>\d{2,5})(?:[/\s'\"]|$)")
#: §17.1362b — the port AND the path, so a login can be told from a change
_URL_PATH_RE = re.compile(r"https?://[^\s'\"]*?:(?P<port>\d{2,5})(?P<path>/[A-Za-z0-9_./-]*)")
#: a POST that authenticates rather than configuring: its LAST path segment is the
#: verb. `/api/v2/auth/login` is exempt; `/api/v3/authentication` is not.
_AUTH_VERBS = frozenset({"login", "logout", "signin", "signout", "token", "session",
                         "authenticate", "auth"})


#: §17.1619 — POSTs that query a model and change nothing (last two path segments)
_QUERY_POSTS = frozenset({"chat/completions", "v1/completions", "v1/embeddings", "api/chat", "api/generate",
                          "api/embed", "api/embeddings"})


def api_ports_changed(texts: list[str]) -> dict:
    r"""``{port: line}`` for every HTTP call in these texts that changes state.

    §17.1373 — over LOGICAL lines: a `curl` continued with `\` is one command.
    """
    out: dict = {}
    for t in texts:
        for ln in logical_lines(t):
            if ln.lstrip().startswith("#"):
                continue
            if _HTTP_GET_RE.search(ln):
                continue
            if not (_HTTP_WRITE_RE.search(ln) or _HTTP_BODY_RE.search(ln)):
                continue
            for m in _URL_PATH_RE.finditer(ln) or ():
                # §17.1362b — a LOGIN changes no configuration; it is how a block
                # PROVES a change it made elsewhere. Live, ADD132 sets qBittorrent's
                # password by editing its config and then posts to
                # `…:8080/api/v2/auth/login`, failing hard unless the answer is
                # `Ok.` -- and §17.1362 refused the frame for not "reading back" a
                # change the API never made. Measured: the whole recorded corpus
                # holds exactly one POST-shaped endpoint, `/api/v3/rootfolder`, a
                # real change; no login shape is lost by this.
                seg = [s for s in (m.group("path") or "").split("/") if s]
                if seg and seg[-1].lower() in _AUTH_VERBS:
                    continue
                # §17.1619 — an inference/search QUERY changes nothing: a POST to `/v1/chat/completions`,
                # `/v1/completions`, `/v1/embeddings`, Ollama's `/api/generate|chat|embed|embeddings` asks a model a
                # question. Live (job 0fb4ea7f T17, the end-to-end test of the server engine) was refused for
                # "changing" the API it only asked.
                if "/".join(seg[-2:]).lower() in _QUERY_POSTS or (seg and seg[-1].lower() in ("generate", "embed", "embeddings")
                                                                 and len(seg) >= 2 and seg[-2].lower() == "api"):
                    continue
                out.setdefault(m.group("port"), ln.strip()[:200])
            for m in _URL_PORT_RE.finditer(ln):
                if not _URL_PATH_RE.search(ln):
                    out.setdefault(m.group("port"), ln.strip()[:200])
    return out


#: §17.1367 — `VAR=$(…)`: a value the block did not write, read from a machine.
_FROM_A_MACHINE_RE = re.compile(
    # `$((` is arithmetic, not a machine: `deadline=$(( SECONDS + 165 ))` is the
    # engine's own wait loop, and the first cut of this flagged seven blocks for it.
    # the closing `)` may be many lines down: ADD132 built its body with a
    # multi-line `python3 -c '…'`, and requiring the paren on the same line missed
    # the one variable that actually broke.
    # §17.1367b — the match consumes only the NAME; the window of what produces it
    # is sliced after. A greedy `[\s\S]{0,400}` group here swallowed the following
    # assignments, so `finditer` skipped them and the rule flipped which draft it
    # caught. The producer is often inside a multi-line `python3 -c '…'`, several
    # lines below the assignment, so a single line is not enough to look at.
    r"(?:^|[\s;&|(])(?P<name>[A-Za-z_]\w*)=\$\((?!\()", re.M)
def _substitution_body(text: str, at: int, cap: int = 1200) -> str:
    """The balanced body of the `$( … )` that starts at `at`.

    §17.1367b — a fixed character window is wrong in both directions: too short
    and it misses a `json.dumps` several lines down inside a `python3 -c '…'`;
    long enough to catch that and it runs past the substitution into the NEXT
    command, where a bare `curl` makes every extracted id look like a document.
    Counting parens is the only thing that answers "what produced this value".
    """
    depth, out = 1, []
    for ch in text[at:at + cap]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    return "".join(out)
#: that name interpolated inside the quoted word of a request BODY:
#: `-d '$VAR'`, `--data \"$VAR\"`, `--json '$VAR'`. A HEADER is deliberately not
#: here -- `-H \"X-Api-Key: $KEY\"` carries a short controlled value the engine
#: reads off a config file, and flagging it would refuse the shape every correct
#: draft uses. A BODY is an arbitrary document from a machine.
#: §17.1374 — the BODY argument is whatever sits between the `-d` flag and the
#: URL. Demanding the variable be inside one quoted word misses the quote
#: juggling a draft really writes:
#:     -d '"'"''"$RADARR_UPDATED"''"'"' http://127.0.0.1:7878/api/v3/x
#: which resolves to `-d '<the whole JSON>'` for the inner shell — §17.1367's
#: failure exactly, and invisible to a single-quoted-word pattern.
_BODY_SPAN_RE = re.compile(
    r"(?<![\w-])(?:-d|--data|--data-raw|--data-binary|--json)(?![\w-])"
    r"(?P<body>.*?)(?=(?:https?://)|$)", re.S)
#: §17.1367b — the substitution produces a WHOLE DOCUMENT, not an extracted
#: scalar. That distinction is the rule's whole point: the live failure was a
#: 6,182-byte JSON object in a shell word, and an id pulled out of it with
#: `clients[0]['id']` is not that. Live, 2026-10-05, this gate refused a correct
#: draft for `{"id":$RADARR_ID,…}` -- a bare numeric JSON position holding a value
#: the block had just extracted.
_WHOLE_DOCUMENT_RE = re.compile(
    r"json\.dumps|\bjq\s+-\w*\s*\.\s*(?:\||$)|(?<![\w-])cat\s+[\"']?/(?![^\n]*\|)|"
    r"(?<![\w-])curl\b(?![^\n]*\|)|(?<![\w-])(?:pct|qm)\s+(?:exec|guest\s+exec)\b(?![^\n]*\|)")
#: the channels that have no quoting at all
_NO_QUOTING_RE = re.compile(r"@-|--data-binary\s+@|-d\s+@|<<<|\|\s*(?:curl|python3|sh|bash)\b|"
                            r"--data\s+@|-T\s|--upload-file", re.I)


def a_machine_value_in_a_shell_word(commands: list[str],
                                    files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1367 — a value read from a machine, pasted into a shell word.

    Live, 2026-10-04. ADD132's eighth draft did what §17.1366 carried to it: GET
    the download client, change two fields, PUT the whole object back. It fetched
    the object into a variable and then interpolated it:

        RADARR_UPDATED=$(echo "$RADARR_CLIENT" | python3 -c '…json.dumps(c)…')
        pct exec 103 -- sh -c "curl … -d '$RADARR_UPDATED' http://…/downloadclient/1"

    and died with

        sh: 1: Syntax error: "(" unexpected

    Reproduced with real `sh` against the real object: **6,182 bytes, 3
    apostrophes, 6 parens.** qBittorrent's own help text inside Radarr's client
    object reads *"See Options -> Web UI -> 'Use HTTPS instead of HTTP' in
    qBittorrent."* — those apostrophes close the single-quoted `-d '…'`, and a
    later `(` from `"hint": "(0)"` then parses as shell syntax.

    No amount of escaping fixes it, because the block does not know what the
    value contains: it came from the machine. A value like that travels on a
    channel with no quoting — stdin (`-d @-` and a pipe) or a file (`-d @/path`).

    §17.1255/1257 compile what a block hands to another interpreter, and this text
    compiles: the break only exists once the variable expands. So the judgment is
    about PROVENANCE, not syntax.
    """
    out: list[dict] = []
    seen: set = set()
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    # §17.1367 — the assignment and the use are often different commands of the
    # same block, so the names are collected across the WHOLE block before any
    # line is judged.
    from_machine: dict = {}
    for t in texts:
        for m in _FROM_A_MACHINE_RE.finditer(t):
            from_machine.setdefault(m.group("name"),
                                    _substitution_body(t, m.end()).strip())
    if not from_machine:
        return []
    for t in texts:
        for ln in logical_lines(t):        # §17.1373 — a continued `curl` is one command
            s = ln.strip()
            if s.startswith("#"):
                continue
            for name, how in from_machine.items():
                if name in seen:
                    continue
                if not _WHOLE_DOCUMENT_RE.search(how):
                    continue                  # an extracted scalar, not a document
                if not any(re.search(rf"\${{?{re.escape(name)}\b", m.group("body") or "")
                           for m in _BODY_SPAN_RE.finditer(ln)):
                    continue
                if _NO_QUOTING_RE.search(ln):
                    continue                      # already passed without quoting
                seen.add(name)
                out.append({"command": s[:200], "why": (
                    f"`${name}` holds whatever `{how.splitlines()[0][:70] if how else how}` returned -- a value from the machine, not one this "
                    f"block wrote -- and it is pasted inside a quoted shell word here. The block cannot "
                    f"know what characters are in it, and one wrong character ends the quote. Live "
                    f"(§17.1367), this exact shape died with `sh: 1: Syntax error: \"(\" unexpected`: "
                    f"Radarr's download-client object is 6,182 bytes holding 3 apostrophes, because "
                    f"qBittorrent's own help text says \"See Options -> Web UI -> 'Use HTTPS instead of "
                    f"HTTP' in qBittorrent\", and the first of them closed the `-d '...'` argument so a "
                    f"later `(` from `\"hint\": \"(0)\"` parsed as shell syntax. Send it on a channel with "
                    f"no quoting: `-d @-` with the value piped in, or write it to a file and use "
                    f"`-d @/path/to/file`.")})
    return out


def changes_an_api_without_reading_it(commands: list[str], verify: list[str],
                                      files: Optional[list[dict]] = None,
                                      acceptance: Optional[list[str]] = None) -> list[dict]:
    r"""§17.1362 — the block changed something through an API no check reads back.

    Measured over every recorded step of the live job: **4** make a state-changing
    HTTP call, **3** never read that API in their checks — and those three are
    exactly the steps whose record is false or that failed:

        ADD97   "point Radarr and Sonarr at the download client"  recorded done, never done
        ADD98   "prove it end to end"                             recorded done, proved nothing
        ADD134  "prove the whole chain"                           failed as a model task

    The one that does read it back is the one that worked. `curl -s` without
    `--fail` exits 0 on a 400 or a 403, so a rejected `PUT` leaves no trace: the
    only thing that can tell is a check that asks the API what it now holds.

    Live, 2026-10-04: ADD132's redraft `PUT`s the download client into Radarr and
    Sonarr and checks `systemctl is-active qbittorrent-nox.service` — the service
    runs either way. Its own previous draft had read both back, which is the shape
    this asks for.
    """
    # §17.1415 — the BLOCK's changes: its commands and the files it RUNS. A file it only writes and
    # delivers (a service's own code, whose HTTP calls a user triggers later) changes nothing when
    # the block runs. Live, every developed API feature would otherwise trip this on its own source.
    # and the step's OWN acceptance check (§17.1414: its POST, run last with `curl -f`) is the check, not
    # a change to read back -- exempt by identity, not by `--fail`: ADD132's `curl --fail-with-body -X PUT`
    # fails on a 400 yet proves nothing about the value the PUT left, which is this gate's case.
    accepted = {str(a).strip() for a in acceptance or []}
    cmd_texts = [str(c) for c in commands or [] if str(c).strip() not in accepted]
    from app.modules.runbook_preconditions import files_the_block_runs     # §17.1420 — one definition
    ran = [str((f or {}).get("content") or "") for f in files_the_block_runs(cmd_texts, files)]
    changed = api_ports_changed(cmd_texts + ran)
    if not changed:
        return []
    read = {m.group("port") for c in (verify or []) for m in _URL_PORT_RE.finditer(str(c or ""))}
    missing = sorted(p for p in changed if p not in read)
    if not missing:
        return []
    port = missing[0]
    return [{"command": changed[port], "why": (
        f"this block CHANGES something through the API on port {port} and no check reads that API back. "
        f"`curl` without `--fail` exits 0 on a 400 or a 403, so a request the service rejected leaves no "
        f"trace at all -- the step is recorded done and nothing happened. Measured over this job's own "
        f"history (§17.1362): of four steps that changed state through an API, the three that never read "
        f"it back are ADD97 (\"point Radarr and Sonarr at the download client\" -- recorded done, never "
        f"done, and the reason the download client answered 403 for weeks), ADD98 (\"prove it end to end\" "
        f"-- recorded done, proved nothing) and ADD134; the one that did read it back is the one that "
        f"worked. Add a check that GETs the same endpoint and shows the value now there -- not the "
        f"service's state, which is the same either way.")}]


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

    The remedy is a trailing reference: `python3 /tmp/x.py # $NAME`, which puts it in
    the command text where the runner looks, without the value ever appearing in the
    block. (§17.1494 — not the `NAME="$NAME"` prefix this once taught: sudo logs it.)
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
                f"starts with no such variable and fails. Put the reference in this command as a trailing "
                # §17.1494 — never the `NAME="$NAME" cmd` prefix this used to teach: elevated, sudo logs every
                # `NAME=value` it is handed (§17.1429); `# $NAME` is the reference the runner reads and sends
                f'comment: `{"bash" if path.endswith(".sh") else "python3"} {path} # ${first}`. The value still never '
                f"appears in the block; the runner expands it inside its root shell.")})
    return out


#: §17.1257 — where a command hands source to another interpreter.
#: §17.1387 — a single-quoted payload may carry `'\''` (close, a literal quote,
#: reopen), which is how a single quote gets into a single-quoted shell word and
#: how §17.1386 pastes a read that is itself a `python3 -c '…'` program. The
#: first cut stopped at the first `'` and handed `ast.parse` a lone backslash,
#: so the engine refused its own correct command as "not valid Python".
_PY_DASH_C = re.compile(r"""python[0-9.]*\s+-c\s+('[^']*'|"[^"]*")""")
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


def _dash_c_payload(cmd: str) -> Optional[str]:
    """§17.1387 — the source a `python -c` in ``cmd`` hands the interpreter.

    A regex over the RAW text cannot read this. `'\''` is not a unit inside a
    quoted word -- it is close-quote, escaped-quote, reopen -- and it belongs to
    the OUTER word, so it only resolves once that word is unquoted. A pattern
    that scans the raw command stops at the first `'` and comes back with a lone
    backslash, which is exactly what happened: §17.1386 began pasting a read
    that is itself a `python3 -c '…'` program, and §17.1257 refused the engine's
    own correct command as "not valid Python ... unexpected EOF".

    So the outer word is unquoted the way a shell unquotes it, and only then is
    the payload read -- at that level the quoting is simple, because the escape
    has already done its job. Nested shells are followed first, since the python
    nearly always sits inside a `sh -c '…'` payload bound for a guest.

    ``None`` when nothing is found or the quoting cannot be split at all; the
    caller owns that finding.
    """
    import shlex
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    for i, w in enumerate(words):
        if w != "-c" or not i or i + 1 >= len(words):
            continue
        prev = words[i - 1].rsplit("/", 1)[-1]
        if prev in ("sh", "bash", "ash", "dash"):
            inner = _dash_c_payload(words[i + 1])
            if inner is not None:
                return inner
        elif re.fullmatch(r"python[0-9.]*", prev):
            return words[i + 1]
    # not split into words at this level (a `$( … )` keeps the call inside one
    # quoted word): the quoting here is already simple, so the pattern holds.
    m = _PY_DASH_C.search(cmd)
    return m.group(1)[1:-1] if m else None


def python_payloads(cmd: str) -> list[tuple[str, Optional[str]]]:
    """``[(what it is, the Python source)]`` this command hands to an interpreter.

    A ``None`` source means the shell words could not even be split, which is a
    finding in itself.
    """
    out: list[tuple[str, Optional[str]]] = []
    if _PY_DASH_C.search(cmd):
        out.append(("the `python -c` payload", _dash_c_payload(cmd)))
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
        # §17.1398 — a brace group's `{` and `}` are the shell's grouping, not a
        # program, and `[ … ]` / `[[ … ]]` IS `test`, which already reads as
        # read-only. Live, ADD137's guard `[ -n "$KEY" ] || { echo 'FAILED: …';
        # exit 1; }` came back as two writes, and §17.1285 refused it as host
        # work that "would change the HOST instead of guest 106" -- a test and an
        # echo that change nothing anywhere.
        seg = re.sub(r"^\{\s+", "", seg)
        seg = re.sub(r"\s*\}$", "", seg).strip()
        _t = re.fullmatch(r"\[\[?\s+(.*?)\s+\]\]?", seg)
        if _t:
            seg = "test " + _t.group(1)
        if seg and not read_only_command(seg):
            out.append(seg)
    return out


_READ_HEAD_REDIRECT_RE = re.compile(r"^\s*(cat|echo|printf)\b[^\n|;&]*?(?<![<>&\d])>>?\s*['\"]?(/[^\s'\"|;&)]+)")


def read_head_redirects_into_system(commands: list[str], policy: Optional[dict]) -> list[dict]:
    """§17.1462 — `cat > /etc/… <<EOF` on a runner that elevates only WRITES. The runner classifies a line by its
    head; `cat`/`echo`/`printf` are reads, run unprivileged, so the redirect cannot write a root-owned file.
    Live (ADD151, 2026-10-11 02:03): the block stopped at `cat > /etc/pve/firewall/101.fw <<"EOF"` — "the runner
    is unprivileged for READ commands (the write grant covers writes only)" — after its `python3` edit (a write
    head, elevated) had already run."""
    if not (policy or {}).get("sudo"):
        return []
    out: list[dict] = []
    for c in commands or []:
        m = _READ_HEAD_REDIRECT_RE.match(str(c))
        if not m or m.group(2).startswith(("/tmp/", "/dev/", "/proc/")):
            continue
        out.append({"command": str(c), "why": (
            f"`{m.group(1)} > {m.group(2)}` is run as a READ, unprivileged: the runner elevates a line by its "
            f"head, and `{m.group(1)}` reads — so the redirect cannot write {m.group(2)} (Permission denied). "
            f"Write it with the content on stdin to a WRITE: `printf '%s\\n' '<line>' … | tee {m.group(2)}` "
            f"(tee runs as root).")})
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
#: §17.1628 — and a two-word subcommand (`qm disk unlink 110`, `qm disk move|import|resize`, `qm cloudinit update 106`,
#: `qm guest cmd 106`): live (job 0fb4ea7f RM1, 2026-10-17) `qm disk unlink 110 --idlist unused0 --force` was refused as
#: work that "would change the HOST instead of guest 110" while the `qm set 110 …` lines beside it passed.
_GUEST_ADDRESS_RE = re.compile(r"\b(?:pct|qm)\s+(?:guest\s+exec|[a-z-]+(?:\s+[a-z][a-z-]*)?)\s+(\d{3,5})\b")


def commands_never_reach_the_guest(commands: list[str], node: Optional[dict],
                                   files: Optional[list[dict]] = None,
                                   inventory: Optional[dict] = None) -> list[dict]:
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
    # §17.1344 — an id no machine has is not a guest. Live, the step that hands the
    # shared media tree to the services says "in-container 999:996 is host
    # 100999:100996" (their uid and gid), `container 999` read as a guest, and this
    # rule refused the step's own `chown -R 100999:100996 /oasis/media` for running
    # on the host "instead of guest 999". Run was withheld for two correct commands.
    if inventory is not None:
        from app.modules.machine_truth import known_guest
        ids = [g for g in ids if known_guest(g, inventory, text + "\n" + "\n".join(str(c) for c in commands or []))]
    if not ids or not commands:
        return []
    # §17.1637 — a step that says it changes nothing has no write to carry into a guest: §17.1637's own gate refuses
    # the write, and this one's remedy (ssh it INTO the VM) would contradict it. Live, MC1 ran on the HOST and named
    # VM 110 only in its "Why".
    if declares_read_only(node):
        return []
    # §17.1603b — a step whose own text names Proxmox's admin tools (`pveum`, `pvesh create`, `pct push`, …) is the
    # HOST's work for the guest: those tools exist only there. Live (c2792d4b T14, "create the panel's API token …
    # push it into container 111"), the right host-side draft was refused as "would change the HOST instead of guest 111".
    from app.modules.runbook_templates import _HOST_ADMIN_RE
    if _HOST_ADMIN_RE.search(text):
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
        # §17.1459 — and a file under /etc/pve/ is the HOST's own config about a guest (its firewall,
        # its .conf): writing it there IS the host-side operation on the guest. Live, ADD151's correct
        # `… | tee /etc/pve/firewall/101.fw` was refused here and redrafted INTO each guest.
        return any(_GUEST_ADDRESS_RE.search(t) or re.search(r"\bssh\b", t) or re.search(r"qemu-server/\d{3,5}\.serial", t)
                   or re.search(r"(?<![\w.-])/etc/pve/", t)
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


#: §17.1637 — the step's OWN declaration that it changes nothing: "Read only:" / "Read only, inside VM 110" opening a
#: clause, or "change nothing" (not "change nothing else", which is a write step's boundary). Measured on all 532 steps
#: of 2026-10-18: 24 hits, every one a read-only step; "read-only mount|token|checks|network view", "(read-only)" and
#: "changes nothing in Pi-hole" (a capability step that writes code) are not declarations and do not match.
_READ_ONLY_STEP_RE = re.compile(
    r"(?:^|\|\||[.;:])\s*read[- ]only(?=\s*(?:[.:;,(]|$))"
    r"|\bchange nothing\b(?![ \t]+(?:else|in|on|but|except|other|outside)\b)", re.I)


def declares_read_only(node: Optional[dict]) -> bool:
    """§17.1637 — does the step say, before its "Done when", that it changes nothing?"""
    text = " || ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    work = re.split(r"\bDone when\b", text, maxsplit=1, flags=re.I)[0]
    return bool(_READ_ONLY_STEP_RE.search(work))


#: §17.1695 — a verification step: its title leads with a reading verb and nothing in its text changes anything
_VERIFY_TITLE_RE = re.compile(r"^\s*(?:[A-Z]+\d*\s*[:—-]\s*)?(?:verify|confirm|check|list|count|recount|show|inspect|"
                              r"measure|audit)\b", re.I)
_CHANGE_VERB_RE = re.compile(r"\b(?:add|set|change|enable|re-?enable|disable|delete|remove|install|uninstall|restart|"
                             r"stop|start|fix|update|widen|map|grab|apply|create|write|edit|move|rename|replace|"
                             r"trigger|queue|search(?:es)?\s+for)\b", re.I)


def only_reads(node: Optional[dict]) -> bool:
    """§17.1695 — a step whose output is its evidence: one that declares it changes nothing (§17.1637), or a
    verification step -- its title leads with verify/confirm/check/list/recount… and its text names no change.

    Live (job e5e22b92 T11 "Verify Sonarr indexers", text "List Sonarr's indexers to confirm all TV-capable enabled
    indexers are present."): one read-only python program, refused "this block has no check at all" because the
    text never says "read-only". A check of a check is not a thing; what it read is the answer."""
    if declares_read_only(node):
        return True
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    return bool(_VERIFY_TITLE_RE.match(title)) and not _CHANGE_VERB_RE.search(text)


#: §17.1637b — the options that send a tool to a REMOTE copy of what it reads locally: ipmitool's interface, host,
#: login and password (`-I lanplus -H <BMC_IP> -U <BMC_USER> -P …`). On the machine whose BMC it is, the same read
#: needs none of them.
_REMOTE_REACH_OPTIONS = {"ipmitool": ({"-I", "-H", "-U", "-P", "-p", "-L", "-y", "-k", "-f", "-C"}, {"-E"})}


def local_form_of_a_read(cmd: str) -> str:
    """§17.1637b — `ipmitool -I lanplus -H x -U y -P "$P" sel list` → `ipmitool sel list`, when that form is a read.

    Live (MC1's redraft, 2026-10-18): told to write "a read the machine answers locally … in its plain local form",
    the model kept every BMC read over lanplus twice and the step parked. The engine knows the form; it says it.
    Returns "" when the command is not one of these tools, carries no remote option, or its local form is no read."""
    import shlex
    from app.modules.assist_state_check import read_only_command
    try:
        words = shlex.split(_PLACEHOLDER_RE.sub("x", str(cmd or "")))
    except ValueError:
        return ""
    if not words or words[0] not in _REMOTE_REACH_OPTIONS:
        return ""
    valued, flags = _REMOTE_REACH_OPTIONS[words[0]]
    out, i, dropped = [words[0]], 1, False
    while i < len(words):
        w = words[i]
        if w in valued:
            i, dropped = i + 2, True
            continue
        if w in flags:
            i, dropped = i + 1, True
            continue
        out.append(w)
        i += 1
    local = " ".join(shlex.quote(w) for w in out)
    return local if dropped and read_only_command(local) else ""


def local_reads_in_a_read_only_step(node: Optional[dict], commands: list[str], verify: list[str],
                                    runbook: str = "") -> tuple[list[str], list[str], list[dict]]:
    """§17.1637c — in a step that says it changes nothing, a read sent to a remote copy of the machine's own device
    at an address nobody knows (`ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sel list`) is
    written in its local form (`ipmitool sel list`) by the engine, said on the frame.

    Live (MC1, 2026-10-18): after §17.1637b's refusal named the local form, two more redrafts still sent every BMC
    read over lanplus, and the step parked asking the operator for the BMC's address and a login -- for a read the
    host answers itself (VS1 ran `ipmitool sel elist` there). Only a PLACEHOLDER address is rewritten: a literal
    one is a machine the drafter chose, and stays the drafter's (§17.1637b refuses it with the local form named)."""
    if not declares_read_only(node):
        return commands, verify, []
    from app.modules.runbook_templates import template_of
    if template_of(runbook or ""):
        return commands, verify, []
    from app.modules.assist_supervised import split_segments
    rewrote: list[str] = []

    def _fix(line: str) -> str:
        out = str(line)
        for seg in split_segments(out):
            seg = seg.strip()
            m = re.search(r"(?:^|\s)-H\s+(\S+)", seg)
            if not m or not _PLACEHOLDER_RE.fullmatch(m.group(1)) or seg not in out:
                continue
            local = local_form_of_a_read(seg)
            if local:
                out = out.replace(seg, local, 1)
                rewrote.append(local)
        return out

    cmds = [_fix(c) for c in commands or []]
    ver = [_fix(v) for v in verify or []]
    if not rewrote:
        return commands, verify, []
    forms = ", ".join(f"`{f}`" for f in dict.fromkeys(rewrote))
    return cmds, ver, [{"why": (
        f"this step says it changes nothing, and {len(rewrote)} read{'s' if len(rewrote) != 1 else ''} went to the "
        f"machine's device over the network at an address nobody gave (`-H <…>`, with a login): the engine wrote "
        f"{'them' if len(rewrote) != 1 else 'it'} in the local form the machine answers itself -- {forms} -- so "
        f"no address, login or password is asked")}]


def _dropped_note(runbook: str) -> str:
    """§17.1696 — the checks the draft offered and the engine could not keep, named in the "no check" refusal."""
    try:
        dropped = dropped_checks(runbook or "")
    except Exception:
        return ""
    if not dropped:
        return ""
    parts = "; ".join(f"`{c[:120]}{'…' if len(c) > 120 else ''}` was dropped -- {why}" for c, why in dropped[:2])
    return (f" The draft DID offer a check, and it could not be kept: {parts}. Write it as one short read inside the "
            "guest: `curl -s` the endpoint with the key read from the config file, piped to `grep -o` for the fields "
            "that show the change (e.g. `\"enable\": *[a-z]*`) -- no program, under 800 characters.")


def _read_only_shape_hint(node: Optional[dict]) -> str:
    """§17.1693 — for a step that only reads, the "no check" refusal names the shape that IS its own evidence.

    Live (job e5e22b92 T2 "Read full Sonarr queue", redrafted twice): a shell `while` loop of curl pages with
    `python3 -c` fragments was refused "no check at all" -- §17.1608 marks only plain read lines and §17.1680c only a
    single python program -- and the refusal told the drafter to add a check, which a read does not need. The engine
    names the shape it accepts, and the summary §17.1691 requires."""
    if not only_reads(node):                                          # §17.1695
        return ""
    return (" This step only READS, so it needs no separate check when the in-guest block is ONE `python3 - <<'PY'` "
            "program and nothing else (§17.1680c): read the key from the service's config file, call the API with "
            "urllib (GET only, every page until `totalRecords` is reached), and print a SUMMARY -- the totals, then one "
            "short line per item with only the fields the step needs -- never the raw JSON, which the record cannot "
            "hold (§17.1691). Its own output is then judged against the step's goal.")


#: §17.1680 — the line §17.1608 puts at the head of an in-guest block that only reads
READS_ONLY_MARKER = "§17.1608: this block only reads"

#: §17.1680c — what makes an in-guest program a CHANGE: an HTTP write, a file opened for writing, a process, a SQL write
_PROGRAM_CHANGE_RE = re.compile(
    r"method\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]|['\"](?:POST|PUT|PATCH|DELETE)['\"]|\b(?:Request|urlopen)\([^)]*\bdata\s*=\s*(?!None\b)"
    r"|\bopen\([^)]*,\s*['\"][wax+]|\bsubprocess\b|\bos\.(?:system|remove|unlink|rename|replace|popen|makedirs|rmdir)\b"
    r"|\bshutil\.|\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\s|\.write\(|\.write_text\(|\.unlink\(", re.I)
_REMOTE_BODY_RE = re.compile(r"<<'REMOTE'\n(?P<body>.*?)\nREMOTE\n", re.S)


#: §17.1694d — `pct exec N -- python3 - <<'TAG'` … `TAG`: the program travels on stdin into the guest
_GUEST_PY_HEREDOC_RE = re.compile(r"^\s*pct\s+exec\s+\d{3,5}\s+--\s+python3?\s+-\s*<<\s*'?(?P<tag>\w+)'?\n(?P<prog>.*?)\n(?P=tag)\s*$",
                                  re.S)


def a_read_only_python_command(cmd: str) -> bool:
    """§17.1694d — a host command that pipes ONE python program into a guest (`pct exec N -- python3 - <<'PY' … PY`)
    with no change in it. Live (job e5e22b92 T5 "Search Robot Chicken S11"): the drafter wrote §17.1680c's program in
    this form instead of inside the in-guest script; the same program, refused twice as "not a read" and "no check"."""
    m = _GUEST_PY_HEREDOC_RE.match(str(cmd or ""))
    return bool(m) and not _PROGRAM_CHANGE_RE.search(m.group("prog"))


_PCT_PUSH_RE = re.compile(r"^\s*pct\s+push\s+(?P<gid>\d{3,5})\s+(?P<src>/tmp/[\w.-]+)\s+(?P<dst>/tmp/[\w.-]+)\s*$")
_PCT_PY_FILE_RE = re.compile(r"^\s*pct\s+exec\s+(?P<gid>\d{3,5})\s+--\s+python3?\s+(?P<path>/tmp/[\w.-]+)\s*$")


def read_only_pushed_programs(commands: Optional[list[str]], files: Optional[list[dict]]) -> bool:
    """§17.1699 — every command is a read-only python program reaching a guest: piped in (§17.1694d), or a file the
    block writes under /tmp, `pct push`ed to the guest's /tmp and run there with `python3`, its content free of change.

    Live (job e5e22b92 T3 "Read second quality profile"): the drafter wrote §17.1680c's program as /tmp/sonarr_summary.py,
    `pct push 104 …` and `pct exec 104 -- python3 /tmp/sonarr_summary.py` -- the third form of one read, refused "not a
    read" and "no check" like the first two. The family is recognised, not each spelling."""
    if not commands:
        return False
    progs = {str((f or {}).get("path") or ""): str((f or {}).get("content") or "") for f in files or []}
    pushed: dict[tuple[str, str], str] = {}
    for c in commands:
        m = _PCT_PUSH_RE.match(str(c))
        if m and m.group("src") in progs:
            pushed[(m.group("gid"), m.group("dst"))] = progs[m.group("src")]
    for c in commands:
        c = str(c)
        if a_read_only_python_command(c):
            continue
        if _PCT_PUSH_RE.match(c):
            m = _PCT_PUSH_RE.match(c)
            if m.group("src") in progs:
                continue
            return False
        m = _PCT_PY_FILE_RE.match(c)
        prog = pushed.get((m.group("gid"), m.group("path"))) if m else None
        if prog is None or _PROGRAM_CHANGE_RE.search(prog):
            return False
    return True


def a_read_only_program(node: Optional[dict], files: Optional[list[dict]],
                        commands: Optional[list[str]] = None) -> bool:
    """§17.1680c — a step that DECLARES it changes nothing, whose in-guest block is a python program with no change in it
    (and no other shell work), is a read-only block like §17.1608's. Live (job c2518102 T2): an all-pages python read of
    Sonarr's missing list was refused "no check at all" -- §17.1608 classifies shell lines, and a program could do
    anything, so the engine looks inside it."""
    if not only_reads(node):                                          # §17.1695
        return False
    bodies = [m.group("body") for f in files or [] for m in _REMOTE_BODY_RE.finditer(str((f or {}).get("content") or ""))]
    if not bodies:
        # §17.1694d/1699 — or every command is a read-only python program reaching the guest
        return read_only_pushed_programs(commands, files)
    for body in bodies:
        py = re.search(r"python3?\s+-\s+<<\s*'?(?P<tag>\w+)'?\n(?P<prog>.*?)\n(?P=tag)\b", body, re.S)
        if not py or _PROGRAM_CHANGE_RE.search(py.group("prog")):
            return False
        rest = (body[:py.start()] + body[py.end():]).splitlines()
        if any(ln.strip() and not re.match(r"\s*(?:set\s+[-+]\w+|export\s+\w+=\S*|#)", ln) for ln in rest):
            return False
    return True


#: §17.1689 — a lone `curl` login, optionally wrapped in `pct exec N -- sh|bash -c '…'`
_LOGIN_WRAP_RE = re.compile(r"^\s*pct\s+exec\s+\d{3,5}\s+--\s+(?:bash|sh)\s+-c\s+(?P<arg>.+?)\s*$", re.S)
_LOGIN_FILE_RE = re.compile(r"(?:^|\s)(?:-c|--cookie-jar|-o|--output|-D|--dump-header)\s+['\"]?(?P<path>[^\s'\"]+)")


def a_login_read(seg: str) -> bool:
    """§17.1689 — a `curl` that only LOGS IN is a read in a read-only step. Live (job e5e22b92 T1 "Login qBittorrent list
    torrents", 2026-10-20): qBittorrent has no readable key (§17.1384), so listing its torrents is `POST
    /api/v2/auth/login` into a cookie jar, then a GET -- and the read-only gate refused the login as a change, though
    §17.1362b already holds that a login changes no configuration. Only a single curl whose every URL ends in an auth verb,
    writing its cookies/output under /tmp, with nothing chained to it, is a login."""
    s = str(seg or "").strip()
    m = _LOGIN_WRAP_RE.match(s)
    if m:
        try:
            parts = shlex.split(m.group("arg"))
        except ValueError:
            return False
        if len(parts) != 1:
            return False
        s = parts[0].strip()
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return False
    if not toks or toks[0] != "curl" or any(t and set(t) <= set(";&|<>()") for t in toks) or re.search(r"`|\$\(", s):
        return False
    if re.search(r"-X\s*['\"]?(?:PUT|PATCH|DELETE)\b", s, re.I):
        return False
    urls = list(_URL_PATH_RE.finditer(s))
    if not urls:
        return False
    for u in urls:
        seg_ = [x for x in (u.group("path") or "").split("/") if x]
        if not seg_ or seg_[-1].lower() not in _AUTH_VERBS:
            return False
    return all(f.group("path").startswith(("/tmp/", "/dev/null")) for f in _LOGIN_FILE_RE.finditer(s))


def writes_in_a_read_only_step(commands: list[str], node: Optional[dict], runbook: str = "",
                               files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1637 — a step that says it changes nothing, drafted with a line that changes something.

    Live (job 0fb4ea7f MC1, 2026-10-18): "On the Proxmox host: check CPU1 memory channel C … change nothing, read
    only" was drafted with `apt-get install -y ipmitool` -- on a host whose `ipmitool sel elist` VS1 had read the
    day before -- and every BMC read as `ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" …`,
    asking the operator for the BMC's address and login. Nothing refused the install for being a write; §17.1285
    refused it for missing guest 110 (named in the step's "Why"), whose remedy is to ssh the write INTO the VM.

    Only the drafter's own lines are judged: a template's frame is the engine's (its host-side `write /tmp/…sh`
    and ssh lines are how it reaches the guest) and §17.1634 already strips a read-only block's own options.
    """
    if not commands or not declares_read_only(node):
        return []
    if read_only_pushed_programs(commands, files):                   # §17.1694d/1699 — one read program, in any form
        return []
    # §17.1705 — the engine's own completeness read (§17.1504: `curl … | python3 -c "<its program>"`) is the engine's frame
    # (it reaches the guest single-quoted, so its quotes are `'\''` in the command)
    if commands and all(_COMPLETENESS_PY.replace("'", "'\\''") in str(c) for c in commands):
        return []
    from app.modules.runbook_templates import template_of
    # §17.1683 — the template's frame is trusted only while its commands ARE the template's (`bash /tmp/in_ct_N.sh
    # start|wait|last`): live, a capability replaced T2's template commands with SeriesSearch POSTs, and this gate --
    # seeing the template's runbook -- stood aside for them
    if template_of(runbook or "") and all(re.search(r"\bbash\s+/tmp/in_(?:ct|vm)_\d+(?:_agent)?\.sh\s+(?:start|wait|last)\b",
                                                    str(c)) for c in commands):
        return []
    # §17.1678 — the engine's own API/section check (§17.1641/1651/1674: GETs only, composed from the service's code) is
    # the engine's frame like a template's. Live (job 5db1b088 PW1, 2026-10-19): "Verify control panel Palworld boxes",
    # declared read only, was refused because the whole `pct exec 111 -- sh -c '…'` check is one segment no read table holds.
    from app.modules.api_route_check import run_is_the_check
    if run_is_the_check(runbook or ""):
        return []
    for c in commands:
        if a_read_only_python_command(str(c)):                       # §17.1694d
            continue
        segs = [s for s in writing_segments(str(c)) if not a_login_read(s)]
        if segs:
            local = local_form_of_a_read(segs[0])
            here = (f" Here that is `{local}`: on this machine the tool reaches its own device, so the address, login "
                    f"and password are not needed -- and the same holds for every other line written this way.") if local else ""
            # §17.1673b — a PROGRAM is not a read even when it only prints; say what is (live, NB1 was redrafted three
            # times as `curl …/api/v1/indexer | python3 -c "…"` against a refusal that never named the alternative)
            if not here and re.search(r"(?:^|[|;&\s'\"(])(?:python3?|perl|node|ruby|php)\s+-[ce]\b", str(c)):
                here = (" A program is not a read even when it only prints: filter the output with `grep -o '<pattern>'` "
                        "or `cut`, or ask an endpoint that returns only what the step needs -- for why an *arr indexer "
                        "fails, `/api/v1/indexerstatus` (each indexer's failure and disabled-until, no credentials), "
                        "and its name by id with `/api/v1/indexer | grep -o '\"name\": *\"[^\"]*\"'`.")
            return [{"command": str(c), "why": (
                f"this step says it is READ ONLY (it changes nothing), and `{segs[0].strip()[:80]}` is not a read "
                f"the engine recognises -- it would change the machine, or reach it in a form no read takes. Use "
                f"only reads, with what the machine already has: a tool that may be missing is a finding to "
                f"report (`command -v <tool> || echo '<tool>: not installed'`), never something to install; and a "
                f"read the machine answers locally is written in its plain local form, with no address, login or "
                f"password of its own." + here)}]
    return []


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


#: §17.1576 — a step whose title says it REPORTS: it quotes the commands upstream steps ran as its evidence.
#: Live, T12 "Compile verification report" (9e1c2c68) cited `systemctl is-active` from T7's output and was failed
#: three times "produced INSTRUCTIONS, not work" — the job ended blocked with every check met. Like §17.1484's
#: document, a report's commands are its content. Tight on purpose: "Report the library count" is a read, not this.
_REPORT_TITLE_RE = re.compile(
    r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*|the\s+)?(?:compile|aggregate|summari[sz]e|collate)\b"
    r"|\b(?:verification|status|final|summary|evidence|completion)\s+report\b|\breport\s*$", re.I)


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
    # §17.1484 — a DOCUMENT's commands are its content. Live, T38 "Document architecture and setup" came back whole
    # (8,203 chars, every machine from the inventory) and was failed "this step produced INSTRUCTIONS, not work"
    # for the maintenance section's `pct exec 120 -- caddy validate …`. A step that CHECKS the build still runs.
    from app.modules.inventory_report import _DOC_RE
    if _DOC_RE.search(str((node or {}).get("title") or "")):
        return None
    if _REPORT_TITLE_RE.search(str((node or {}).get("title") or "")):
        return None                      # §17.1576 — a report quotes what ran
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


# §17.1453 — a dynamic-DNS update that names a private (RFC 1918 / loopback / link-local) address.
_DDNS_UPDATE = re.compile(r"(?i)duckdns\.org/update|/nic/update|dynupdate|dynv6\.com/api/update|freedns\.afraid\.org|"
                          r"api\.cloudflare\.com/[^\s'\"]*dns_records")
_DDNS_IP = re.compile(r"(?i)(?:[?&](?:ip|myip|ipv4)=|\"content\"\s*:\s*\"|content=)"
                      r"((?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
                      r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})")


_IN_GUEST_PVE_WRITE_RE = re.compile(
    r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+(\d{3,5})\b[\s\S]*?(?:\btee\s+(?:-a\s+)?|>>?\s*)(/etc/pve/\S+)")


def pve_config_written_inside_a_guest(commands: list[str]) -> list[dict]:
    """§17.1459 — `/etc/pve` is the Proxmox host's cluster filesystem; a guest has none. Live (ADD151): every
    guest's firewall file was written INSIDE that guest (`pct exec 101 -- sh -c 'cat > /etc/pve/firewall/101.fw'`)
    — useless there, and the host's own files left as they were."""
    out: list[dict] = []
    for c in commands or []:
        m = _IN_GUEST_PVE_WRITE_RE.search(str(c))
        if m:
            out.append({"command": str(c), "why": (
                f"{m.group(2)} lives on the Proxmox HOST, not inside a guest: /etc/pve is the host's cluster "
                f"filesystem, and guest {m.group(1)} has none. Write it from the runner's own shell on the host "
                f"(`printf '%s\\n' … | tee {m.group(2)}`), with no `pct exec`/`qm guest exec` around it.")})
    return out


#: §17.1473 — what exists only on the Proxmox host: its cluster filesystem and its own tools.
_HOST_ONLY_RE = re.compile(r"(?<![\w.-])(/etc/pve/[^\s'\"|;&)]*|\b(?:pvesh|pvesm|pve-firewall|pveversion|pvecm|qm|pct)\s+[a-z])")
_IN_GUEST_SCRIPT_RE = re.compile(r"/tmp/in_(ct|vm)_(\d{3,5})(?:_agent)?\.sh$")
_REMOTE_BLOCK_RE = re.compile(r"<<'REMOTE'\n([\s\S]*?)\nREMOTE\b")


def host_only_inside_a_guest(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1473 — §17.1459 for READS too: /etc/pve and the host's tools exist only on the Proxmox host, so a check
    of them run inside a guest can only fail, however the host actually is. Live (T37 "Validate entire build",
    drafted into CT 120's helper script): `[ -d /etc/pve/firewall/groups ]` and `grep … /etc/pve/firewall/cluster.fw`
    inside the proxy container — a validation that reports the firewall missing on a host that enforces it."""
    out: list[dict] = []
    for f in files or []:
        m = _IN_GUEST_SCRIPT_RE.search(str((f or {}).get("path") or ""))
        if not m:
            continue
        body = _REMOTE_BLOCK_RE.search(str((f or {}).get("content") or ""))
        if not body:
            continue
        for ln in body.group(1).splitlines():
            if ln.lstrip().startswith("#"):
                continue
            h = _HOST_ONLY_RE.search(ln)
            if h:
                out.append({"command": f"(inside {'VM' if m.group(1) == 'vm' else 'CT'} {m.group(2)}) {ln.strip()[:160]}",
                            "why": (f"`{h.group(1).strip()}` exists only on the Proxmox HOST, not inside a guest, which has none of it: "
                                    f"inside guest {m.group(2)} it can only fail, whatever the host holds. Run it from "
                                    f"the runner's own shell on the host, and reach each guest with `pct exec N --` / "
                                    f"`qm guest exec N --` only for what is inside that guest.")})
                break
    for c in commands or []:
        mm = re.match(r"\s*(?:pct\s+exec|qm\s+guest\s+exec)\s+(\d{3,5})\s+--\s+(.*)$", str(c), re.S)
        if mm:
            h = _HOST_ONLY_RE.search(mm.group(2))
            if h:
                out.append({"command": str(c), "why": (f"`{h.group(1).strip()}` exists only on the Proxmox HOST, "
                                                        f"not inside a guest, which has none of it: run it on the host, "
                                                        f"not inside guest {mm.group(1)}.")})
    return out


HOST_ONLY_MARKER = "not inside a guest, which has none of it"


def public_dns_to_private_address(commands: list[str]) -> list[dict]:
    """§17.1453 — ``[{command, why}]`` for a dynamic-DNS update that would point a public name at an address
    only the LAN can reach.

    Live (ADD128, 2026-10-10): the drafted block for "fix Caddy's DNS and get a certificate" carried
    `curl "https://www.duckdns.org/update?domains=homelab-demo&token=<DUCKDNS_TOKEN>&ip=192.168.1.127"` —
    Caddy's LAN address — next to the correct `…&ip=` (DuckDNS fills in the caller's public address). Run, it
    would have taken the domain off the internet and made the certificate it was for impossible to issue.
    """
    out: list[dict] = []
    for c in commands or []:
        cmd = str(c)
        if not _DDNS_UPDATE.search(cmd):
            continue
        m = _DDNS_IP.search(cmd)
        if m:
            out.append({"command": cmd, "why": (
                f"this update points a PUBLIC DNS name at a private address ({m.group(1)}): only machines "
                "inside the network can reach it, so the name stops working from outside and a public "
                "certificate for it can never be issued. A public record holds the router's PUBLIC address — "
                "leave `ip=` empty and the service records the address the request came from.")})
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


#: §17.1561 — a step that asks a HOW/WHY question about a symptom: "research how Jellyfin comes to
#: resolve TV episodes as movies and how others fix it", "find out why …", "look up how people solve …".
_TROUBLESHOOTS = re.compile(
    r"(?i)\b(?:research|look\s+up|find\s+out|investigate|work\s+out)\s+(?:how|why|what)\b"
    r"|\bhow\s+(?:do\s+|did\s+)?(?:others|people|users|admins)\s+(?:fix|solve|address|handle|resolve|deal\s+with)\b"
    r"|\bwhy\s+(?:does|do|is|are|did|would|has|have)\b")


def troubleshooting_question(node: dict) -> str:
    """§17.1561 — the HOW/WHY question a step asks about a symptom, or "".

    Live (job 263e920a, FIX1): the operator's direction was "have the engine research how this current
    issue is addressed by others and apply it", and the step said so in words — "Research how Jellyfin
    comes to resolve TV episodes as Movie items in a tvshows library and how others fix it" — yet the
    drafter researched nothing: `research_for_step` fired only on a CURRENCY cue (latest / still works),
    so the draft was written from the model's memory and repeated the refresh that had already failed.
    A step that asks how or why is a step whose content depends on what others found; the question is
    the sentence that asks it, with the step's own nouns kept, and the lookup is the same
    `research_one` the diagnosis uses."""
    # each field is its own sentence, so a title never leaks into the question drawn from the body
    body = ". ".join(s.strip() for s in (str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template")) if s.strip())
    m = _TROUBLESHOOTS.search(body)
    if not m:
        return ""
    # the sentence the cue sits in, up to the next sentence end or a "then …" tail (the DO half)
    start = max(body.rfind(". ", 0, m.start()), body.rfind("; ", 0, m.start()), body.rfind(": ", 0, m.start()))
    start = start + 2 if start >= 0 else 0
    tail = re.search(r"(?:[.!?](?:\s|$)|,?\s+(?:then|and then|afterwards)\s+(?:make|run|do|fix|apply|remove|restart|re-?add|scan)\b)", body[m.start():])
    end = m.start() + tail.start() if tail else len(body)
    q = " ".join(body[start:end].split())
    q = re.sub(r"(?i)^(?:research|look\s+up|find\s+out|investigate|work\s+out)\s+", "", q)
    return q[:220]


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
    kind = "currency"
    if not question:
        # §17.1561 — a step that asks HOW others fix a symptom (or WHY it happens) is researched too:
        # the draft must apply what others found, not repeat what already failed.
        question = troubleshooting_question(node)
        kind = "how"
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
    logger.warning("step_research_attached node=%s kind=%s sources=%d q=%r",
                   key, kind, len(sources), query[:120])
    if kind == "how":
        return ("\n\n" + block
                + "\n\nUse these to decide HOW to do this step: the CAUSE others found for this symptom and the "
                  "FIX they applied. Draft the commands that apply that fix on this machine (its own paths, "
                  "guest ids and service names), and do not repeat an action the step says has already been "
                  "tried and did not help.")
    return ("\n\n" + block
            + "\n\nUse these to decide WHICH of the things this machine offers are worth using right "
              "now. What the machine lists is what EXISTS; whether each one still works is what these "
              "sources are for. Prefer the ones they confirm are alive, and do not spend the step on "
              "ones they say are dead or retired.")


async def draft_runbook(node: dict, brief: dict | str, upstream: str = "", *,
                        for_channel: bool = True, retry_note: str = "", spec=None,
                        environment: dict | None = None, truth=None, services: list | None = None) -> str:
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
            tpl = rt.select_template(node, truth, services)   # §17.1351
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
    prompt = build_base_prompt(node, b, environment, autonomous=True)   # §17.1564 — a runbook cannot ask either
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
    # §17.1696 — a check that reads its key in the guest and greps one field is ~200 characters by itself; §17.1702 —
    # 800: live (job e5e22b92 RC3), a Sonarr queue check for one series was 451, dropped twice. The runner's read
    # classification, not the length, decides what a check may be.
    cands.extend(m.group(1).strip() for m in re.finditer(r"`([^`\n]{2,800})`", prose))
    out: list[str] = []
    for c in cands:
        # §17.1187 — a <PLACEHOLDER> reads as a redirect to the shell gate; judge
        # the shape with a dummy value, keep the raw command for the operator.
        shape = _PLACEHOLDER_RE.sub("x", c)
        # §17.1666 — a check runs through the RUNNER's read channel: judged as the runner judges it
        if c not in out and not c.startswith("/") and read_only_command(shape, runner_side=True):
            out.append(c)
    return out[:MAX_VERIFY_COMMANDS]


def dropped_checks(text_out: str) -> list[tuple[str, str]]:
    """§17.1696 — the checks a runbook's ## Verify OFFERED that `verify_commands` did not keep, and why.

    Live (job e5e22b92 T13 "Re-enable healthy indexers", three drafts): each Verify held a check -- `pct exec 102 --
    bash -c "curl … /api/v1/indexer | python3 -c \"…print(i['name'], i['enable'])…\""` -- and each frame was refused
    "this block has no check at all": the inline literal was over 200 characters (never a candidate) and a program is
    not a read the runner's channel takes. The refusal named neither, so every redraft repeated it."""
    from app.modules.assist_state_check import read_only_command
    body = _section(text_out, "Verify")
    kept = set(verify_commands(text_out))
    out: list[tuple[str, str]] = []
    prose = _FENCE_RE.sub(" ", body)
    for m in re.finditer(r"`([^`\n]{2,4000})`", prose):
        c = m.group(1).strip()
        if c in kept or c.startswith("/") or not re.match(r"[a-z]", c):
            continue
        if len(c) > 800:
            why = f"it is {len(c)} characters and a check is read as one line of 800 at most"
        elif re.search(r"(?:^|[|;&\s\\\"'(])(?:python3?|perl|node|ruby|php)\s+-[ce]\b", c):
            why = "a program (`python3 -c`) is not a read the runner's read channel takes"
        elif not read_only_command(_PLACEHOLDER_RE.sub("x", c), runner_side=True):
            why = "the runner's read channel does not take it as a read"
        else:
            continue
        if not re.search(r"\b(?:pct|qm|curl|grep|cat|systemctl|ss|ip|wg)\b", c):
            continue                                                   # prose in backticks, not a command
        out.append((c, why))
    return out


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


#: §17.1603 — commands that MINT a secret and print it once
_SECRET_MINT_RE = re.compile(r"\bpveum\s+(?:user\s+)?token\s+add\b|\bpvesh\s+create\s+\S*/token/|\bopenssl\s+rand\b|\bpwgen\b")
#: a file whose name says it holds a secret
_SECRET_FILE_NAME_RE = re.compile(r"(?:token|secret|passw(?:or)?d|credential|api[-_]?key|private[-_]?key|\.key\b|\.pem\b(?<!ca\.pem))", re.I)
_CAT_FILE_RE = re.compile(r"\b(?:cat|head|tail|less|more|strings|xxd|od|base64)\s+(?:-\S+\s+(?:\d+\s+)?)*([^\s|;&><'\"]+)")


def prints_a_secret(commands: list[str], verify: Optional[list[str]] = None,
                    files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1603 — a block that puts a secret into its own output, which is the run's record, the frame and the log.

    Live (c2792d4b T14, "Create a read-only Proxmox API token for the panel"): the draft ran
    `pveum user token add … | tee /root/panel-api-token.txt` -- the token's secret, shown once, printed by `tee` --
    and its check was `cat /root/panel-api-token.txt`. Two shapes:
    * a command that MINTS a secret whose output is not sent wholly into a file (`tee` prints it; no `>` prints it);
    * a command or check that reads a file NAMED as a secret (`cat …token…`, `…secret…`, `…password…`, `….key`)."""
    texts = [("command", str(c)) for c in commands or []] + [("check", str(v)) for v in verify or []] + \
            [(str((f or {}).get("path") or "file"), str((f or {}).get("content") or "")) for f in files or []]
    out: list[dict] = []
    for where, body in texts:
        for line in body.splitlines():
            ln = line.strip()
            if not ln or ln.startswith("#"):
                continue
            m = _SECRET_MINT_RE.search(ln)
            if m and ln[:m.start()].count("$(") > ln[:m.start()].count(")"):
                m = None                          # captured into a variable (`PW=$(pwgen 20 1)`): nothing printed
            if m:
                rest = ln[m.end():]
                if re.search(r"\btee\b", rest) or not re.search(r"(?<![0-9&])>{1,2}\s*\S", rest):
                    out.append({"command": f"{where}: {ln[:160]}", "why": (
                        f"`{m.group(0)}` prints a new secret once, and this line lets it reach the output "
                        f"({'`tee` copies it to the screen' if 'tee' in rest else 'nothing sends it into a file'}): "
                        f"the run's record, the frame and the log would hold it. Send the output ONLY into a file "
                        f"(`… > /path/file` with mode 600, or through a filter that writes the file), and print "
                        f"nothing of it.")})
                    break
            for cm in _CAT_FILE_RE.finditer(ln):
                path = cm.group(1)
                if _SECRET_FILE_NAME_RE.search(path.rsplit("/", 1)[-1]):
                    out.append({"command": f"{where}: {ln[:160]}", "why": (
                        f"this reads `{path}` -- a file named as a secret -- into the output, which is the run's "
                        f"record and the log. Prove it WORKS instead: use it (call the API with it, log in with it) "
                        f"and print only the result, or check it exists and its mode (`stat -c '%a %U' {path}`).")})
                    break
    return out


#: §17.1612 — `. FILE` / `source FILE`, and what the standard release files define
_SOURCED_RE = re.compile(r"(?:^|[\s;&|(`])(?:\.|source)\s+([\"']?/[^\s;&|)`]+)")
_SOURCED_ANY = "*sourced*"
_OS_RELEASE = {"NAME", "VERSION", "ID", "ID_LIKE", "PRETTY_NAME", "VERSION_ID", "VERSION_CODENAME", "UBUNTU_CODENAME",
               "HOME_URL", "SUPPORT_URL", "BUG_REPORT_URL", "PRIVACY_POLICY_URL", "LOGO", "BUILD_ID", "VARIANT", "VARIANT_ID"}
_SOURCED_KNOWN = {"/etc/os-release": _OS_RELEASE, "/usr/lib/os-release": _OS_RELEASE,
                  "/etc/lsb-release": {"DISTRIB_ID", "DISTRIB_RELEASE", "DISTRIB_CODENAME", "DISTRIB_DESCRIPTION"}}


def a_command_that_does_not_parse(commands: list[str]) -> list[dict]:
    """§17.1616 — every command of a block runs as its OWN runner call, so each must parse alone (`bash -n`).

    Live (job 0fb4ea7f FW1, 2026-10-16): `… || sed -i '/^\\[group ai\\]/,/^$/ { /IN DROP/i IN ACCEPT …` + newline + `}' file`
    -- a quoted sed program spanning two lines was split at the line break into two commands, neither of which parses
    (unterminated quote); nothing refused it. A multi-line program belongs in a written file (or on one line)."""
    import subprocess
    out: list[dict] = []
    for c in commands or []:
        c = str(c or "")
        if not c.strip():
            continue
        probe = re.sub(r"<[A-Z][A-Z0-9_]{1,60}>", "x", c)    # a <PLACEHOLDER> is filled before anything runs
        try:
            r = subprocess.run(["bash", "-n"], input=probe, text=True, capture_output=True, timeout=5)
        except Exception:
            continue                         # no bash to ask: judge nothing
        if r.returncode != 0:
            out.append({"command": c[:200], "why": (
                f"this command does not parse on its own (`bash -n`: {r.stderr.strip().splitlines()[-1][:160] if r.stderr.strip() else 'syntax error'}). "
                "Every line of the block is its own runner call, so a quoted program that spans lines is cut at the "
                "line break and runs as broken halves. Keep each command on ONE line, or write the multi-line content to "
                "a file in `## Write these files` and run that file.")})
    return out


def a_sed_program_that_does_not_compile(commands: list[str]) -> list[dict]:
    """§17.1617 — a `sed` program that sed itself rejects. Each one is compiled against empty input (`sed -n -e PROG
    </dev/null`: no file, nothing edited) and refused when sed says it is invalid.

    Live (job 0fb4ea7f FW1, 2026-10-16): `sed -i '/^\\[group ai\\]$/,/^$/ { /^IN DROP/ i IN ACCEPT …' file` -- an `i` text
    runs to the end of the line, so the block's `}` can never come: "sed: -e expression #1, char 0: unmatched `{'".
    Run, it would have failed and opened nothing."""
    import shlex
    import subprocess
    out: list[dict] = []
    for c in commands or []:
        c = str(c or "")
        if "sed" not in c:
            continue
        probe = re.sub(r"<[A-Z][A-Z0-9_]{1,60}>", "x", c)
        for seg in re.split(r"\|\||&&|;|\|", probe):
            try:
                words = shlex.split(seg)
            except ValueError:
                continue
            if not words or words[0] != "sed":
                continue
            progs, args, i = [], words[1:], 0
            while i < len(args):
                a = args[i]
                if a in ("-e", "--expression") and i + 1 < len(args):
                    progs.append(args[i + 1]); i += 2; continue
                if a.startswith("--expression="):
                    progs.append(a.split("=", 1)[1]); i += 1; continue
                if a.startswith("-") and not progs and a not in ("-f",):
                    i += 1; continue                       # -i, -E, -n, -r, -i.bak …
                if not progs:
                    progs.append(a)                        # the first plain word is the program
                i += 1
            if not progs:
                continue
            flags = ["-E"] if any(a in ("-E", "-r", "--regexp-extended") for a in args) else []
            try:
                r = subprocess.run(["sed", "-n", *flags, *sum((["-e", p] for p in progs), [])],
                                   input="", text=True, capture_output=True, timeout=5)
            except Exception:
                continue
            if r.returncode != 0 and "sed:" in r.stderr:
                out.append({"command": c[:200], "why": (
                    f"sed rejects this program (`{r.stderr.strip().splitlines()[-1][:160]}`), so the edit would fail and "
                    "change nothing. Note `i`/`a`/`c` text runs to the end of the line -- no `}` can follow it on one "
                    "line; to insert a line before a match on one line, substitute: "
                    "`sed -i '/start/,/^MATCH$/ s|^MATCH$|NEW LINE\\nMATCH|' file`.")})
                break
    return out


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
    opened with `email aedefruscio@homelab-demo.duckdns.org` -- the operator's
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


#: §17.1327 — `systemctl <verb> <unit>`: the unit a block acts on.
_SYSTEMCTL_RE = re.compile(
    r"(?<![\w-])systemctl\s+(?:--\S+\s+|-\w\s+)*"
    r"(?:start|stop|restart|reload|reload-or-restart|try-restart|enable|disable|reenable|mask|unmask|"
    r"is-active|is-enabled|is-failed|status|cat|edit)\b[^\n|;&]*?\s"
    r"(?P<unit>[A-Za-z0-9@:._-]{2,64}?)(?P<suffix>\.(?:service|socket|timer|target))?(?=\s|$|'|\"|&)")
#: units a Linux host has without any step creating them.
_STOCK_UNITS = frozenset({
    "ssh", "sshd", "cron", "crond", "networking", "systemd-networkd", "systemd-resolved", "systemd-timesyncd",
    "systemd-journald", "dbus", "rsyslog", "ufw", "firewalld", "fail2ban", "postfix", "chrony", "chronyd",
    "qemu-guest-agent", "docker", "containerd", "snapd", "unattended-upgrades", "apparmor", "multipathd",
    "getty", "serial-getty", "systemd-udevd", "polkit", "irqbalance", "atd", "apt-daily", "man-db", "nginx",
    "apache2", "caddy", "pveproxy", "pvedaemon", "pvestatd", "pve-cluster", "pve-firewall",
})
#: a flag, a path, a variable or a number mistaken for a unit name.
_NOT_A_UNIT = re.compile(r"^(?:-|/|\$|<|\d+$|--)")
_UNIT_FILE_RE = "/(?:etc|lib|usr/lib|run)/systemd/system/[^\\s\"']*{u}\\.(?:service|socket|timer|target)"


#: §17.1357 — the guest a line addresses: `pct exec 105 -- …`, `qm guest exec 106 -- …`.
_IN_GUEST_LINE_RE = re.compile(r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+(?P<gid>\d{3,5})\b", re.I)


def unsourced_service_name(commands: list[str], files: Optional[list[dict]], env: Optional[dict],
                           node: Optional[dict], upstream: str = "", units: Optional[list[str]] = None,
                           units_by_guest: Optional[dict] = None) -> list[dict]:
    """§17.1327 — a systemd unit a block starts, enables or reads must be one that
    something the engine holds names AS A UNIT: this block writes its unit file,
    or a fact, pin, step text or earlier output mentions `<name>.service` (or a
    systemctl on it, or its unit-file path).

    Live, ADD66's model draft ran `systemctl is-enabled palworld-server` and
    `is-active palworld-server` — the VM's HOSTNAME. No such unit exists; the
    real one, `palworld.service`, sat in T23's and T24's own records, and the
    step would have reported a failure that was not about the work. The guest's
    name in the system map is not evidence that a unit of that name exists, so
    the question is asked about the name as a unit, and the nearest unit the
    engine does hold is named in the remedy.
    """
    bodies = [str((f or {}).get("content") or "") for f in files or []]
    paths = [str((f or {}).get("path") or "") for f in files or []]
    block = "\n".join([str(c) for c in commands or []] + bodies + paths)
    held = (json.dumps(env or {}, default=str) + " " + str(upstream or "") + " "
            + " ".join(str((node or {}).get(k) or "") for k in
                       ("title", "description", "prompt_template", "output_text", "last_verification_reason")))

    def names_as_unit(text: str, unit: str) -> bool:
        u = re.escape(unit)
        return bool(re.search(rf"\b{u}\.(?:service|socket|timer|target)\b", text)
                    or re.search(_UNIT_FILE_RE.format(u=u), text)
                    or re.search(rf"(?<![\w-])systemctl\s[^\n]*?\b{u}\b", text))

    out: list[dict] = []
    seen: set[str] = set()
    # §17.1327b — line by line, and never a COMMENT: the engine's own phase script explains
    # itself with "`systemctl is-active` against the JSON blob", and the first version of this
    # gate refused the engine's own template for a unit called `against`.
    for line in block.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        # §17.1357 — a unit is judged where it RUNS. `pct exec 105 -- systemctl …`
        # is a question about guest 105's units, not the subject guest's: ADD132's
        # correct `qbittorrent-nox.service` was refused because 103's list lacks it.
        # A line naming no guest keeps the subject's list, so nothing else changes
        # -- and `pct exec 103 -- systemctl restart qbittorrent-nox`, the right unit
        # in the wrong container, is still refused, which a pooled list would not be.
        _g = _IN_GUEST_LINE_RE.search(line)
        line_units = ((units_by_guest or {}).get(_g.group("gid")) if _g else None)
        if _g and line_units is None:
            line_units = units                 # unmeasured guest: judge as before
        if not _g:
            line_units = units
        for m in _SYSTEMCTL_RE.finditer(line):
            unit = (m.group("unit") or "").strip().strip("'\"")
            # §17.1357 — `unit` and `suffix` are SEPARATE groups, so `unit` never
            # holds the `.service`, and subtracting its length chopped the name:
            # `qbittorrent-nox.service` became `qbittor` (refused for a name
            # nobody has — this is what stopped ADD132's correct draft) and
            # `radarr.service` became `""`, which the next line skips, so the gate
            # was blind to every `systemctl <name>.service` with a short name.
            base = unit
            if not base or base in seen or _NOT_A_UNIT.match(base):
                continue
            if base.lower() in _STOCK_UNITS or base.split("@", 1)[0].lower() in _STOCK_UNITS:
                continue
            seen.add(base)
            # the block writing the unit FILE is a source; `.service` in its own systemctl is not
            if re.search(_UNIT_FILE_RE.format(u=re.escape(base)), block):
                continue
            if base in (line_units or []):
                continue                  # §17.1327 — the machine HAS it; that settles it
            if unit_from_installed_package(base, block):
                continue                  # §17.1488 — the block installs the package that ships it
            if re.search(rf"\bsystemd-run\b[^\n]*(?:--unit[= ]|-u\s+){re.escape(base)}\b", block):
                continue                  # §17.1521 — a TRANSIENT unit this block starts with
                                          # `systemd-run --unit <base>`: it is brought into being
                                          # here (like §17.1344 for a guest id), so polling it with
                                          # `systemctl is-active <base>` is not a reference to a unit
                                          # that does not exist.
            if names_as_unit(held, base):
                continue
            if base in engine_shipped_units():
                continue                  # §17.1632 — a unit the engine installs itself (scaffold-lan-gate, VS2)
            import difflib
            candidates = sorted(({mm.group(1) for mm in re.finditer(r"\b([A-Za-z0-9@._-]{2,40})\.service\b", held)}
                                 | {u for u in (line_units or []) if "@" not in u}) - _STOCK_UNITS)
            near = difflib.get_close_matches(base, candidates, n=1, cutoff=0.55)
            where = line.strip()
            held_by = "the guest has" if line_units else "the engine holds"
            hint = (f" The unit {held_by} is `{near[0]}.service` — use that." if near else
                    (f" Units {held_by}: {', '.join(c + '.service' for c in candidates[:4])}." if candidates else
                     " Nothing names a unit for this step: write its unit file in this block first."))
            out.append({"command": where[:200], "why": (
                f"`{unit}` is not a unit anything the engine holds names: " +
                ("the guest's own `systemctl list-unit-files` does not have it" if line_units else
                 f"no fact, pin, step text or earlier output mentions `{base}.service`") +
                ", and this block writes no unit file for it — so `systemctl` acts on a unit that does "
                "not exist and reports a failure that is not about the work." + hint)})
    return out


@functools.lru_cache(maxsize=1)
def engine_shipped_units() -> frozenset:
    """§17.1632 — the systemd units the engine ships in its own scripts/ and installs on a guest it deploys.

    Live (job 0fb4ea7f VS2, 2026-10-18): `systemctl is-active scaffold-lan-gate` was refused as "not a unit anything
    the engine holds names" on the server the engine deployed, where deploy_on_guest.sh installed that unit."""
    root = Path(__file__).resolve().parents[2] / "scripts"
    return frozenset(f.name.rsplit(".", 1)[0] for f in root.glob("*") if f.suffix in (".service", ".timer", ".socket"))


#: §17.1488 — units a package ships under another name than its own
_PACKAGE_UNITS = {"wg-quick": ("wireguard-tools", "wireguard"), "netfilter-persistent": ("netfilter-persistent",
                  "iptables-persistent"), "openvpn": ("openvpn",), "openvpn-client": ("openvpn",),
                  "tailscaled": ("tailscale",), "smbd": ("samba",), "nmbd": ("samba",), "nfs-server":
                  ("nfs-kernel-server",), "unbound": ("unbound",), "resolvconf": ("resolvconf", "openresolv"),
                  "containerd": ("containerd", "containerd.io", "docker.io", "docker-ce"),
                  "docker": ("docker.io", "docker-ce"), "fail2ban": ("fail2ban",), "rasdaemon": ("rasdaemon",)}
_APT_INSTALL_RE = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\s+([^\n;&|]+)")


def unit_from_installed_package(unit: str, block: str) -> bool:
    """§17.1488 — a unit the block's own `apt-get install` brings. Live, ADD160 (AirVPN for CT 105) was refused
    "`wg-quick@wg0` is not a unit anything the engine holds names" four lines after `apt-get install -y
    wireguard-tools`, the package that ships wg-quick@.service."""
    pkgs = {t for m in _APT_INSTALL_RE.finditer(block or "") for t in m.group(1).split() if not t.startswith("-")}
    if not pkgs:
        return False
    stem = unit.split("@", 1)[0].removesuffix(".service").lower()
    return stem in pkgs or bool(set(_PACKAGE_UNITS.get(stem, ())) & pkgs)


def secret_is_file_content(script: str, name: str) -> bool:
    """§17.1494 — is ``$NAME`` written INTO a file (a config to place), rather than used as a credential?

    Live, ADD170 (qBittorrent's port through its Web API): the login password in an in-guest script drew the
    §17.1489 advice to list it in SECRET_FILES — with `/etc/wireguard/wg0.conf` as the example."""
    ref = rf"\$\{{?{re.escape(name)}\b"
    for ln in (script or "").splitlines():
        if re.search(ref, ln) and re.search(r"(?:>>?\s*[\"']?/|\btee\b|\bcat\s*>|\bdd\s+of=)", ln):
            return True
    return False


#: §17.1488 — a stored secret named inside a script that runs INSIDE a guest
_IN_GUEST_SECRET_MARKER = "is read inside the guest, where the runner never sends it"


def secret_inside_a_guest_script(files: Optional[list[dict]], secret_names: set[str]) -> list[dict]:
    """§17.1488 — the runner hands a stored secret only to the HOST command whose text names it; a script that runs
    inside a guest (an in_ct/in_vm helper's REMOTE block) gets an empty `$NAME`. Live, ADD160 wrote
    `printf '%s\\n' "$AIRVPN_WG_CONF" > /etc/wireguard/wg0.conf` inside CT 105: the tunnel config would have been an
    empty file. The value has to cross from the host on stdin, expanded in the root shell the runner opens."""
    out: list[dict] = []
    for f in files or []:
        m = _IN_GUEST_SCRIPT_RE.search(str((f or {}).get("path") or ""))
        body = _REMOTE_BLOCK_RE.search(str((f or {}).get("content") or "")) if m else None
        if not body:
            continue
        for name in sorted(secret_names or ()):
            if re.search(rf"\$\{{?{re.escape(name)}\b|<{re.escape(name)}>", body.group(1)):   # §17.1589 — or its placeholder
                gid, vm = m.group(2), m.group(1) == "vm"
                into = (f"qm guest exec {gid} --pass-stdin 1 -- sh -c 'umask 077; cat > <PATH>'" if vm else
                        f"pct exec {gid} -- sh -c 'umask 077; cat > <PATH>'")
                if not secret_is_file_content(body.group(1), name):
                    # §17.1494 — a password for a login is not a file to write: the call itself runs from the host
                    run = (f"qm guest exec {gid} --pass-stdin 1 -- sh -c '…'" if vm else f"pct exec {gid} -- sh -c '…'")
                    out.append({"command": f"(inside {'VM' if vm else 'CT'} {gid}) ${name}", "why": (
                        f"`${name}` {_IN_GUEST_SECRET_MARKER}: it would be empty there. It is a credential, not a "
                        f"file's content, so do not use the in-guest template for this call: run it from the HOST as "
                        f"its own command, `{run} <<< \"${name}\"`, and have the program read it from stdin (curl "
                        f"`--data-urlencode password@-`, after `tr -d \"\\n\"` to drop the newline `<<<` adds) — the "
                        f"value crosses on stdin, expanded inside the runner's root shell, and never appears in the "
                        f"block or a process list.")})
                    continue
                out.append({"command": f"(inside {'VM' if vm else 'CT'} {gid}) ${name}", "why": (
                    f"`${name}` {_IN_GUEST_SECRET_MARKER}: it would be empty there. Do not drop the work: name the "
                    f"file in the template's SECRET_FILES (`<path inside the guest> {name}`, e.g. "
                    f"`/etc/wireguard/wg0.conf {name}`) and the engine writes it from the host on stdin before the "
                    f"commands run — `{into} <<< \"${name}\"`, expanded inside the runner's root shell — then keep "
                    f"every command that USES the file in the in-guest commands.")})
    return out


def unsourced_addresses_in_files(commands: list[str], files: Optional[list[dict]], env: Optional[dict],
                                 node: Optional[dict], upstream: str = "",
                                 engine_held: Optional[list] = None) -> list[dict]:
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
    # §17.1419 — the engine's OWN address is a value it holds: measured (§17.1333) or published by its
    # LAN overlay (§17.1418). Live: ADD124's config named http://192.168.1.43:8000, the URL the engine had
    # itself put in the prompt, and this gate called it a guess.
    held_ips |= {str(a) for a in engine_held or [] if a}
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
    facts named `homelab-demo.duckdns.org` and the step named five
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


#: §17.1348 — `$NAME`, `${NAME}`: a value the command expects the environment to hold
_VAR_REF_RE = re.compile(r"\$\{(?P<brace>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)")
#: `NAME=value`, `export NAME=value`, `NAME="$(…)"` — a value the block sets itself
_VAR_SET_RE = re.compile(
    # the name may open the line, follow a separator, or sit just inside a quote
    # (`bash -c 'K=$(…); …'`), but never follow a letter or a dash (`--key=value`)
    r"(?:^|[;&|(\s'\"])(?P<name>[A-Za-z_][A-Za-z0-9_]*)=", re.M)
#: §17.1348b — the other ways a shell binds a name, all of them in the engine's own
#: templates: `for h in $(seq 1 254)`, `while read -r line`, `read PORT`, a `local`.
_VAR_BIND_RE = re.compile(
    r"\bfor\s+(?P<for>[A-Za-z_][A-Za-z0-9_]*)\s+in\b"
    r"|\bselect\s+(?P<sel>[A-Za-z_][A-Za-z0-9_]*)\s+in\b"
    r"|\bread\b(?:\s+-\S+)*\s+(?P<read>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\b(?:local|declare|typeset)\s+(?P<loc>[A-Za-z_][A-Za-z0-9_]*)")
#: §17.1383 — `TITLE="$TITLE"`, `TITLE=$TITLE`, `TITLE="${TITLE}"`: an assignment
#: whose whole value is a reference to the SAME name. It sets the name to
#: whatever the name already was, which for a name nothing else provides is the
#: empty string — so the gate that asks "does anything set this?" must not count
#: it. Anchored at the name, so it is matched against the tail from `name`.
_SELF_ASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<q>[\"']?)\$\{?(?P=name)\}?(?P=q)(?:$|[\s;&|)])")

#: names the shell or the runner always provides
_SHELL_VARS = frozenset({
    "HOME", "PATH", "USER", "LOGNAME", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR",
    "SECONDS", "RANDOM", "IFS", "PS1", "PS2", "HOSTNAME", "UID", "EUID", "PPID", "BASH", "BASH_VERSION",
    "SHLVL", "LINENO", "FUNCNAME", "REPLY", "OPTARG", "OPTIND", "DEBIAN_FRONTEND", "SSHPASS", "SUDO_ASKPASS",
    # §17.1348c — AWK's own fields and variables. `awk '{print $NF}'` lives inside the
    # engine's own address sweep; `$NF` is not a shell name and nothing should set it.
    "NF", "NR", "FNR", "FS", "OFS", "RS", "ORS", "FILENAME", "RSTART", "RLENGTH", "SUBSEP",
    "CONVFMT", "OFMT", "ENVIRON", "ARGC", "ARGV",
})


#: §17.1364 — `pct exec N -- sh -c '…'` / `qm guest exec N -- bash -c "…"`: the
#: payload another machine runs, and WHO expands the variables inside it. A
#: SINGLE-quoted payload reaches the guest verbatim and the guest's shell expands
#: it; a DOUBLE-quoted one is expanded by the host before the call is made.
_GUEST_PAYLOAD_RE = re.compile(
    r"(?<![\w-])(?:pct\s+exec|qm\s+guest\s+exec)\s+(?P<gid>\d{3,5})\b"
    r"(?:(?!--\s)[^\n])*?--\s+(?:(?:ba|da|z|k)?sh)\s+-c\s+(?P<q>['\"])(?P<payload>(?:\\.|(?!(?P=q)).)*)(?P=q)",
    re.S)


def guest_payloads(text: str) -> list[tuple[str, str, str]]:
    """``[(gid, quote, payload)]`` — what each guest is told to run, and in which
    shell its variables expand.

    §17.1364 — measured on the live host: inside `pct exec 105 -- sh -c '…'` the
    environment is the GUEST's (`PATH=/sbin:/bin:/usr/sbin:/usr/bin`, where the
    host's is `/usr/local/sbin:…`) and a host variable is simply absent
    (`HASH=[]`). `pct exec` has no flag that carries one in.
    """
    return [(m.group("gid"), m.group("q"), m.group("payload"))
            for m in _GUEST_PAYLOAD_RE.finditer(str(text or ""))]


#: §17.1401b — a heredoc opener with a QUOTED delimiter, and the file it writes (if any)
_QUOTED_HEREDOC_OPEN_RE = re.compile(
    r"<<-?\s*(?:'(?P<q1>[A-Za-z_]\w*)'|\"(?P<q2>[A-Za-z_]\w*)\"|\\(?P<q3>[A-Za-z_]\w*))")
_HEREDOC_TARGET_RE = re.compile(r">\s*(?P<path>[^\s<>|;&'\"]+)|\btee\s+(?:-a\s+)?(?P<tee>[^\s<>|;&'\"]+)")
#: a file that a shell will run later: its references are expanded THEN, so they stay judged
_SHELL_FILE_RE = re.compile(r"\.(?:sh|bash)$|/bin/|/sbin/")


def without_quoted_data_heredocs(text: str) -> str:
    """The text with the bodies of quoted heredocs that write DATA files removed.

    A quoted heredoc (`<<'EOF'`) is never expanded by the shell that writes it, so
    a `${NAME}` inside it is the file's own syntax -- a JavaScript template literal,
    a systemd `${MAINPID}`, a Python f-string -- not a shell reference. Live,
    2026-10-06, ADD122 wrote `routes/palworld-settings.js` with
    `` `${PALWORLD_USER}@${PALWORLD_IP}` `` inside `<<'EOF'`, and §17.1348 refused
    `$PALWORLD_USER` as "read here and nothing sets it". A quoted heredoc into a
    SHELL script (the guest template's `<<'REMOTE'`) keeps its body: that script
    runs later and expands its references then.
    """
    lines = str(text or "").split("\n")
    out: list[str] = []
    stack: list[tuple[str, bool]] = []          # (delimiter, keep its body?)
    for ln in lines:
        if stack:
            delim, keep = stack[-1]
            if ln.strip() == delim:
                stack.pop()
                out.append(ln)
                continue
            if not all(k for _, k in stack):
                continue                         # inside a data heredoc: not the shell's text
        out.append(ln)
        m = _QUOTED_HEREDOC_OPEN_RE.search(ln)
        if m:
            tgt = _HEREDOC_TARGET_RE.search(ln[:m.start()]) or _HEREDOC_TARGET_RE.search(ln[m.end():])
            path = (tgt.group("path") or tgt.group("tee")) if tgt else ""
            stack.append((m.group("q1") or m.group("q2") or m.group("q3"), bool(_SHELL_FILE_RE.search(path or ""))))
    return "\n".join(out)


def a_shell_runs_it(cmd: str, path: str, body: str) -> bool:
    """§17.1404 — is the written file at `path` run by a SHELL in `cmd`?

    Live, 2026-10-06, ADD122's draft wrote `/tmp/add_palworld_route.py` and ran
    `python3 /tmp/add_palworld_route.py`; its route code, a JavaScript template
    literal inside a Python string, carries `${k}=${v}`, and §17.1348 refused `$k`
    as "read here and nothing sets it". A Python program is not a shell script:
    `$k` there is nobody's variable. Only a file a shell interprets has shell
    references -- run by `bash`/`sh`/`dash`/`zsh`, `source`d or `.`'d, or executed
    directly with a shell shebang or a `.sh` name.
    """
    p = re.escape(path)
    if re.search(rf"\b(?:ba|da|z|k)?sh\s+(?:-\S+\s+)*{p}\b|(?:^|[;&|\s])(?:source|\.)\s+{p}\b", cmd):
        return True
    interp = re.search(rf"\b(python3?|node|perl|ruby|php|awk|jq)\b[^\n;&|]*?{p}\b", cmd)
    if interp:
        return False
    first = (body or "").lstrip().split("\n", 1)[0]
    if first.startswith("#!"):
        return bool(re.search(r"\b(?:ba|da|z|k)?sh\b", first))
    # run directly with no shebang: the kernel refuses it and the shell runs it as sh
    if re.search(rf"(?:^|[;&|]\s*){p}\b", cmd.strip()):
        return True
    return path.endswith((".sh", ".bash"))


#: §17.1578 — the first runner whose `secret_refs` leaves the names a block binds itself alone
_RUNNER_LEAVES_BOUND_NAMES = 23


def _single_quoted_spans(text: str) -> list[tuple[int, int]]:
    """§17.1638 — the (start, end) of each single-quoted span, as bash reads them (a `'` inside "…" or after a
    backslash outside quotes is a plain character). The end is the closing quote; an unclosed span runs to the end."""
    spans: list[tuple[int, int]] = []
    i, n, dq = 0, len(text), False
    while i < n:
        ch = text[i]
        if ch == "\\" and not dq:
            i += 2
            continue
        if ch == "\\" and dq:
            i += 2
            continue
        if ch == '"':
            dq = not dq
        elif ch == "'" and not dq:
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            spans.append((i + 1, j))
            i = j + 1
            continue
        i += 1
    return spans


#: §17.1638 — awk's own `$`-variables: inside a single-quoted program `$NF` is the last field, and `$(NF)` is the same
_AWK_FIELD_REF_RE = re.compile(r"\$(NF|NR|FNR)\b")


def awk_fields_the_runner_reads_as_secrets(commands: list[str], verify: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """§17.1638 — `awk '{print $NF}'` written `awk '{print $(NF)}'`, said on the frame.

    Live (job 0fb4ea7f MC1, 2026-10-18, helper 23): the approved read-only block stopped at its fifth line,
    `ipmitool sel list | grep -E "Correctable ECC|…" | … | awk '{print $NF}' | sort | uniq -c`: "this command needs
    $NF, which neither the engine sent nor this runner's secrets file holds". The runner's `secret_refs` reads every
    `$UPPER` as a stored secret, inside single quotes too, where the shell never expands it. To awk the two spellings
    are one expression, and the runner's pattern does not match `$(`."""
    notes: list[str] = []

    def _fix(line: str) -> str:
        text = str(line)
        for a, b in reversed(_single_quoted_spans(text)):
            span = text[a:b]
            new = _AWK_FIELD_REF_RE.sub(lambda m: f"$({m.group(1)})", span)
            if new != span:
                notes.extend(f"${m.group(1)}" for m in _AWK_FIELD_REF_RE.finditer(span))
                text = text[:a] + new + text[b:]
        return text

    cmds = [_fix(c) for c in commands or []]
    ver = [_fix(v) for v in verify or []]
    if not notes:
        return commands, verify, []
    names = ", ".join(f"`{x}`" for x in dict.fromkeys(notes))
    return cmds, ver, [{"why": (
        f"awk's {names} inside a single-quoted program is written `$(…)`: the runner reads every `$UPPER` as a stored "
        f"secret, quotes or not, and would refuse the line; to awk the two are the same field")}]


def single_quoted_names_the_runner_reads_as_secrets(commands: list[str], policy: dict) -> list[dict]:
    """§17.1638 — any OTHER `$UPPER` inside single quotes that no store holds: the shell leaves it alone, the runner
    demands it as a secret and refuses the approved block. Said before Run, so it redrafts instead."""
    held = ({str(x) for x in ((policy or {}).get("secrets") or [])}
            | {str(x) for x in ((policy or {}).get("held") or [])})
    out: list[dict] = []
    for cmd in commands or []:
        text = str(cmd or "")
        # a name the block binds itself is §17.1578's (an old runner) or nobody's (helper 23 leaves it alone)
        bound = {m.group("name") for m in _VAR_SET_RE.finditer(text)
                 if not _SELF_ASSIGN_RE.match(text[m.start("name"):])}
        bound |= {g for m in _VAR_BIND_RE.finditer(text)
                  for g in (m.group("for"), m.group("sel"), m.group("read"), m.group("loc")) if g}
        hits = sorted({m.group(1) for a, b in _single_quoted_spans(text)
                       for m in _SECRET_REF_RE.finditer(text[a:b])} - held - bound)
        if hits:
            out.append({"command": text[:200], "why": (
                f"{', '.join(f'`${h}`' for h in hits)} inside single quotes is left alone by the shell, but the runner "
                f"reads every `$UPPER` reference as a secret it must hold and refuses the block (\"this command needs "
                f"${hits[0]}, which neither the engine sent nor this runner's secrets file holds\"). Spell it so no "
                f"`$` is followed by an uppercase letter: lowercase the name, or in awk write `$(NF)`.")})
    return out


def locals_the_runner_reads_as_secrets(commands: list[str], policy: dict) -> list[dict]:
    """§17.1578 — an UPPERCASE name the block sets and then reads, on a runner that demands a value for
    EVERY `$UPPER` reference.

    Live (c2792d4b T2, helper 22): the engine's own probe opened `CPW=$(cat); … PORT=$(grep …)` and used
    `$CPW`/`$PORT`; the runner's `secret_refs` reads every `$[A-Z]…` as a secret it must resolve, found
    neither in its file nor sent, and refused the approved block — "this command needs $PORT, $CPW, which
    neither the engine sent nor this runner's secrets file holds". The engine's own gate (§17.1348) knew
    the names were bound and said nothing, so Run was offered for a block that could not run. Helper 23's
    `secret_refs` subtracts the names a block binds; until the machine runs it, the frame says so and
    Run is off. Names the runner or the engine HOLDS are not this: those it resolves whatever the block
    does with them."""
    helper = str((policy or {}).get("helper") or "").strip()
    try:
        if helper and int(helper) >= _RUNNER_LEAVES_BOUND_NAMES:
            return []
    except ValueError:
        pass
    held = ({str(x) for x in ((policy or {}).get("secrets") or [])}
            | {str(x) for x in ((policy or {}).get("held") or [])} | set(_SHELL_VARS))
    out: list[dict] = []
    for cmd in commands or []:
        text_value = str(cmd or "")
        bound = {m.group("name") for m in _VAR_SET_RE.finditer(text_value)
                 if not _SELF_ASSIGN_RE.match(text_value[m.start("name"):])}
        bound |= {g for m in _VAR_BIND_RE.finditer(text_value)
                  for g in (m.group("for"), m.group("sel"), m.group("read"), m.group("loc")) if g}
        hits = sorted({m.group(1) for m in _SECRET_REF_RE.finditer(text_value)} & bound - held)
        if hits:
            names = ", ".join(f"`${n}`" for n in hits)
            out.append({"command": text_value[:200], "why": (
                f"{names} {'is a name' if len(hits) == 1 else 'are names'} this block sets itself, but the runner at hand "
                f"(helper {helper or '?'}) reads every `$UPPER` reference as a secret it must hold and refuses the block: "
                f"\"this command needs {', '.join('$' + n for n in hits)}, which neither the engine sent nor this runner's "
                f"secrets file holds\". Name the block's own variables in lowercase (`{hits[0].lower()}`), or update the "
                f"runner — helper {_RUNNER_LEAVES_BOUND_NAMES} leaves the names a block binds alone.")})
    return out


def a_secret_name_written_as_its_value(commands: list[str], files: Optional[list[dict]], policy: dict,
                                       verify: Optional[list[str]] = None) -> list[dict]:
    """§17.1581 — a held secret's NAME typed where its VALUE belongs.

    Live (c2792d4b T4): the draft's in-guest script ran `sed -i 's|<ApiKey>.*</ApiKey>|<ApiKey>RADARR_API_KEY
    </ApiKey>|' /var/lib/radarr/config.xml` — the WORD `RADARR_API_KEY` as Radarr's new key, after stopping
    Radarr. §17.1391 catches a VALUE typed in; this catches the name: `>NAME<`, `=NAME`, `"NAME"`, `: NAME`.
    `$NAME`/`${NAME}` (the runner resolves it) and `<NAME>` (the engine's placeholder) are the right forms and
    pass; so does `NAME=…` (an assignment to it)."""
    held = sorted({str(x) for x in ((policy or {}).get("secrets") or [])} | {str(x) for x in ((policy or {}).get("held") or [])})
    if not held:
        return []
    pat = re.compile(r"(?<![$<{\w])(?:(?<=[>=:'\"`])|(?<=[>=:'\"`]\s))(" + "|".join(re.escape(h) for h in held) + r")(?=['\"`<]|[ \t]*(?:$|[;&|)}\],]))(?!\s*=[^=])", re.M)   # §17.1588 — the name ENDS the value: `'NAME not configured'` is prose
    texts = ([str(c) for c in commands or []] + [str(v) for v in verify or []]
             + [str((f or {}).get("content") or "") for f in files or []])
    out: list[dict] = []
    seen: set[str] = set()
    for t in texts:
        for m in pat.finditer(t):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = next((ln for ln in t.splitlines() if name in ln), t)
            out.append({"command": line.strip()[:200], "why": (
                f"this writes the secret's NAME where its VALUE belongs: `{name}` is the word itself, not the value "
                f"the engine holds under that name. Written into a config or a header it replaces a working credential "
                f"with the string `{name}`. Reference it so the runner resolves it — `$"
                f"{name}` in a command the runner runs, or the engine's `<{name}>` placeholder — or read it off the "
                f"machine with `$( … )`; never the bare word.")})
    return out


def variables_nothing_sets(commands: list[str], files: Optional[list[dict]], policy: dict) -> list[dict]:
    """§17.1348 — a command that reads a variable nothing provides.

    Live, 2026-10-04: ADD131's draft carried
    `curl -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/api/v3/rootfolder`
    inside `pct exec 103`. Nothing sets that name -- the runner resolves only the
    secrets it holds, and a container's environment does not carry it -- so the
    header would have gone out EMPTY and the app would have answered 401, which
    reads like a wrong key rather than a value that was never there. The engine had
    just been taught to read that key off the machine (§17.1332/1342); the draft
    replaced the read with a name, and nothing noticed because the gates looked for
    `<PLACEHOLDER>`, not `$NAME`.

    A reference is satisfied when the block ASSIGNS it, the runner holds it as a
    secret, or the shell always provides it. Anything else is empty at run time.
    """
    # §17.1352 — the names the ENGINE holds count as much as the ones in the
    # runner's own file. Live, this gate refused `$MASS_PASSWORD` with "the runner
    # holds no secrets" while `runner_secrets.names()` answered AIRVPN_WG_CONF,
    # MASS_PASSWORD, PROWLARR_API_KEY, RADARR_API_KEY, SONARR_API_KEY -- five values
    # the engine has had all along, two of them the very API keys §17.1332 taught it
    # to read off the machines. The operator said so twice before the measurement
    # proved it: "it should already have the password".
    have = (set(_SHELL_VARS)
            | {str(x) for x in ((policy or {}).get("secrets") or [])}
            | {str(x) for x in ((policy or {}).get("held") or [])})
    texts = [str(c) for c in commands or []]
    file_by_path = {str((f or {}).get("path") or ""): str((f or {}).get("content") or "") for f in files or []}
    out: list[dict] = []
    def _assigned_in(blobs: list) -> set:
        out_set = {m.group("name") for b in blobs for m in _VAR_SET_RE.finditer(b)
                   # §17.1383 — `TITLE="$TITLE"` assigns the name FROM ITSELF, so
                   # it satisfies this gate while passing nothing in. Live,
                   # 2026-10-06: ADD134's command opened
                   # `TITLE="$TITLE" RADARR_API_KEY="$RADARR_API_KEY" … python3
                   # /tmp/chain_proof.py` with `inputs: []`, and the program's
                   # first line is `os.environ["TITLE"]` -- so it would have
                   # searched Radarr for the empty string. A secret the RUNNER
                   # resolves is the §17.1191 contract and is covered by `have`
                   # below, which is why only the self-reference is discounted
                   # here and not the shape.
                   if not _SELF_ASSIGN_RE.match(b[m.start("name"):])}
        out_set |= {g for b in blobs for m in _VAR_BIND_RE.finditer(b)
                    for g in (m.group("for"), m.group("sel"), m.group("read"), m.group("loc")) if g}
        # §17.1612 — a SOURCED file assigns too: `$(. /etc/os-release && echo "$VERSION_CODENAME")` (Docker's own
        # install line) was refused "nothing sets it" (live, job 0fb4ea7f T7). The two release files' names are known;
        # any other sourced file may set anything, and then nothing in that command is judged.
        for b in blobs:
            for sm in _SOURCED_RE.finditer(b):
                path = sm.group(1).strip("\"'")
                out_set |= _SOURCED_KNOWN.get(path, {_SOURCED_ANY})
        return out_set

    for cmd in texts:
        # a command that RUNS a written file carries that file's references too
        bodies = [cmd] + [body for path, body in file_by_path.items()
                          if path and path in cmd and a_shell_runs_it(cmd, path, body)]
        # §17.1401b — a quoted heredoc's body is the written file's syntax, not the shell's
        bodies = [without_quoted_data_heredocs(b) for b in bodies]
        # §17.1364 — a variable is judged in the shell that EXPANDS it. A
        # single-quoted payload handed to `pct exec N -- sh -c '…'` reaches the
        # guest verbatim, and the guest expands it against the GUEST's environment:
        # measured on the live host, `PATH=/sbin:/bin:/usr/sbin:/usr/bin` there
        # against `/usr/local/sbin:…` on the host, and a host variable reads back
        # empty (`HASH=[]`). `pct exec` has no flag that carries one in. So the
        # host's assignments -- and the runner's secrets, which it injects into the
        # process IT starts -- do not reach inside; only what the payload itself
        # sets does. Live, ADD132's draft computed `HASH=$(python3 …)` on the host
        # and wrote `WebUI\\Password_PBKDF2=$HASH` inside a single-quoted guest
        # payload: the password would have been written EMPTY, and §17.1348 passed
        # it because something in the block did assign that name.
        guest_bits: list = []
        for body in bodies:
            for gid, quote, payload in guest_payloads(body):
                if quote == "'":
                    guest_bits.append((gid, payload))
        host_bodies = list(bodies)
        for _gid, payload in guest_bits:
            host_bodies = [b.replace(payload, " ") for b in host_bodies]
        assigned = _assigned_in(host_bodies)
        for gid, payload in guest_bits:
            inside = _assigned_in([payload]) | set(_SHELL_VARS)
            if _SOURCED_ANY in inside:
                continue                    # §17.1612
            for m in _VAR_REF_RE.finditer(payload):
                name = m.group("brace") or m.group("bare")
                if name in inside or name.isdigit():
                    continue
                held_here = sorted({str(x) for x in ((policy or {}).get("secrets") or [])}
                                   | {str(x) for x in ((policy or {}).get("held") or [])})
                out.append({"command": cmd.strip()[:200], "why": (
                    f"`${name}` is expanded by GUEST {gid}'s own shell, not by the host: it sits inside the "
                    f"single-quoted payload of `pct exec {gid} -- sh -c '…'`, which reaches the guest "
                    f"verbatim. Nothing inside that payload assigns it, a guest's environment does not "
                    f"carry the host's, and `pct exec` has no flag that passes one in -- measured on this "
                    f"host, `PATH` inside the guest is `/sbin:/bin:/usr/sbin:/usr/bin` against "
                    f"`/usr/local/sbin:…` outside it, and a host variable reads back empty. So it expands "
                    f"to NOTHING there"
                    + (f" (the runner's own values -- {', '.join(held_here)} -- are injected into the "
                       f"process it starts on the HOST, which is not this shell either)" if held_here else "")
                    + ". Let the HOST expand it before the call -- close the quote around it, "
                      "`sh -c '… '\"$NAME\"' …'`, or double-quote the whole payload -- or assign it "
                      "inside the payload from something the guest can read.")})
                break
        for body in host_bodies:
            if _SOURCED_ANY in assigned:
                break                       # §17.1612 — a sourced file may set any name: judge nothing here
            for m in _VAR_REF_RE.finditer(body):
                name = m.group("brace") or m.group("bare")
                if name in have or name in assigned or name.isdigit():
                    continue
                # §17.1398 — set in ANOTHER command of the block. Each command is its own
                # runner call (`run_block` sends them one by one), so its own shell: the
                # assignment is gone before this line runs. Live, ADD137 put
                # `KEY="$(pct exec 111 -- cat …pub)"` and `[ -n "$KEY" ]` on separate lines,
                # was told "the block never assigns it" -- false -- and the remedy this
                # refusal gave ("assign it in the block before use") was the shape it had.
                elsewhere = [c for c in texts if c is not cmd and name in _assigned_in([c])]
                if elsewhere:
                    out.append({"command": cmd.strip()[:200], "why": (
                        f"`${name}` is read here but set in a DIFFERENT command "
                        f"(`{elsewhere[0].strip()[:70]}`). Each command in a block runs as its own runner "
                        f"call, in its own shell, so that assignment is gone before this line runs and "
                        f"`${name}` expands to NOTHING. Keep the assignment and every use of it in ONE "
                        f"command (join them with `&&`), or write a script file that does both and run it.")})
                    break
                out.append({"command": cmd.strip()[:200], "why": (
                    f"`${name}` is read here and nothing sets it: the block never assigns it, and the "
                    f"values the engine and the runner have between them are "
                    + (", ".join(sorted({str(x) for x in ((policy or {}).get("secrets") or [])}
                                        | {str(x) for x in ((policy or {}).get("held") or [])})) or "none")
                    + ". A guest's environment does not carry it either, so it expands to NOTHING and the "
                      "command runs with an empty value -- live (§17.1348), an empty `X-Api-Key` header made "
                      "an app answer 401, which reads like a wrong key rather than a value that was never "
                      "there. Read the value in the same command (a command substitution) -- each command runs "
                      "in its own shell, so an assignment on another line does not reach this one.")})
                break
        if out and out[-1]["command"] == cmd.strip()[:200]:
            continue
    return out


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


def is_develop_delivery(files: Optional[list[dict]]) -> bool:
    """§17.1589 — the block delivers files the develop flow staged (`/tmp/scaffold-dev-<step>--…`)."""
    return any(str((f or {}).get("path") or "").startswith("/tmp/scaffold-dev") for f in files or [])


def secrets_in_files(files: Optional[list[dict]], inputs: list[dict], resolved: Optional[list[str]] = None) -> list[dict]:
    """§17.1280 — a SECRET placeholder inside a file cannot be filled: the runner
    expands `$NAME` in a COMMAND's environment, never inside a file's bytes, and
    writing the value into a file on disk is the leak §17.1191 exists to stop.

    §17.1589 — `resolved`: the names a store ALREADY holds. `apply_runner_secrets` removes them from `inputs`
    (nothing to type), so a `<RADARR_API_KEY>` in a file the block writes passed this gate, Run was offered,
    and the run refused "missing (used in the file …)" — live (c2792d4b T8), a driver looped on it."""
    secret = {i["name"] for i in inputs or [] if i.get("secret")} | {str(n) for n in resolved or []}
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
    known = {str(n) for n in (policy.get("secrets") or [])} | {str(n) for n in (policy.get("held") or [])}
    # §17.1667 — a name a store already HOLDS is a secret, whatever it is called: the name test (§17.1275) reads
    # `AIRVPN_BITMAGNET_CONF` as a plain value, and the frame asked the operator to type the WireGuard config in.
    inputs = [{**i, "secret": True} if str(i.get("name") or "") in known and not i.get("secret") else i
              for i in inputs]
    secret_names = [str(i.get("name") or "") for i in inputs if i.get("secret")]
    if not secret_names:
        return commands, verify, inputs, [], []
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
    # §17.1649 — a name the operator would be ASKED for gets the same test: its answer is stored and referenced, so a
    # placeholder the reference cannot reach (inside a single-quoted guest payload) could never carry it. Live (job
    # 5db1b088 PK1): `pct exec 111 -- sh -c 'echo "… <KEY_FROM_STEP_1>" >> known_hosts'` was asked for, answered, and ran
    # with the literal `<KEY_FROM_STEP_1>` written into CT 111's known_hosts.
    for n in [m for m in missing if m not in resolved]:
        for c in list(commands or []) + list(verify or []):
            _c, problem = secret_ref_rewrite(c, n)
            if problem and problem not in unquotable:
                unquotable.append(problem)
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


def _rewritten_for_display(runbook: str, cmds: list[str], verify: list[str], files: list[dict], repairs: list) -> str:
    """§17.1577 — a capability replaced the model's draft: the operator approves what they READ, so the
    frame's prose opens with the block that will run (§17.1569's record head: commands, checks, files,
    the engine's own drafting notes) and carries the model's draft after it, marked as replaced.

    Live (c2792d4b T2): the frame's `commands` was the engine's probe with the held login, its `runbook`
    the model's `-b "connect.sid=<PANEL_COOKIE>"` prose asking for a cookie nobody holds — two stories, and
    the one on top was the one that would not run. The draft is kept whole underneath, so the rehearsal
    path can still find the text a frame was rendered from (`_full_runbook` matches the tail)."""
    head = _frame_runbook({"files": [{"path": (f or {}).get("path")} for f in files or []], "engine_fixed": list(repairs or [])},
                          cmds, verify)
    return head + "\n\n---\n\n## The model's draft, replaced by the block above\n\n" + _runbook_for_display(runbook, cmds)


def frame_run(node: dict, runbook: str, spec, policy: dict, env: Optional[dict] = None,
              preconditions: Optional[list[dict]] = None, upstream: str = "",
              units: Optional[list[str]] = None, inventory: Optional[dict] = None,
              engine_address: Optional[str] = None, services: Optional[list] = None,
              units_by_guest: Optional[dict] = None, reading=None) -> dict:
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
    # §17.1657 — an *arr API call inside its own guest with no key gets one, FIRST, so every later step (the
    # wrapper repairs, §17.1332's in-guest read of the placeholder) sees it as if the draft had written it
    from app.modules.arr_api_key import present_the_arr_key as _present_arr_key
    cmds, verify, files, _ak_notes = _present_arr_key(cmds, verify, files, services)
    from app.modules import lan_checks as _lan                     # §17.1679 — "from the LAN" runs on the host
    cmds, verify, _lan_notes = _lan.from_the_host(node, cmds, verify)
    _ak_notes = list(_ak_notes) + _lan_notes
    from app.modules import json_grep as _jg                     # §17.1658 — JSON greps tolerate the spacing
    cmds, verify, files, _jg_notes = _jg.repair(cmds, verify, files)
    _ak_notes = list(_ak_notes) + _jg_notes
    # §17.1288d — a check that needs a value the run never uses is dropped, not
    # refused: live, the third draft of ADD82 had fixed everything it was told
    # and still carried `ssh … <PALWORLD_IP>` in its Verify, so the frame parked
    # on the host-side first draft with the operator's Run greyed out. The run
    # is the operator's decision; a check the engine cannot fill is the
    # engine's to remove, said on the frame (`engine_fixed`). When NO check
    # would be left, the refusal stands and the redraft must supply one.
    # §17.1332 — a value a MACHINE holds is read on the machine, not asked of the
    # operator. This runs BEFORE the drop-gate below: a check that only needed a
    # key off the disk is now a check the engine can fill, so it is kept and
    # filled rather than dropped.
    from app.modules import machine_values as _mv
    # §17.1499 — fold a redundant `$(pct exec GID … )` the drafter nested inside a
    # command already running in guest GID back into the guest's own shell, BEFORE
    # the key substitution and the host-tool gate see it (else it is refused as a
    # `pct` run inside a guest, and the read would run on the host unprivileged).
    cmds, verify, _fold_notes = _mv.fold_redundant_nested_guest_exec(cmds, verify)
    cmds, verify, _local_notes = local_reads_in_a_read_only_step(node, cmds, verify, runbook)   # §17.1637c
    # §17.1647 — an *arr test call that sent only an id GETs the object and posts it back whole
    from app.modules.arr_test_calls import send_the_whole_object as _whole_object
    cmds, verify, _tc_notes = _whole_object(cmds, verify)
    _local_notes = list(_local_notes) + _tc_notes
    cmds, verify, _awk_notes = awk_fields_the_runner_reads_as_secrets(cmds, verify)   # §17.1638
    _local_notes = list(_local_notes) + _awk_notes
    # §17.1504 — a "check <show> completeness" step reads the *arr library and reports
    # the gaps deterministically, instead of the model's lookup/add/true draft. Runs
    # BEFORE the key reader so `<APP_API_KEY>` is filled in the guest it now runs in.
    _orig_cmds, _orig_verify = list(cmds), list(verify)          # §17.1589 — kept, for a delivery no capability may rewrite
    _comp_cmds, _comp_verify, _comp_notes = rewrite_completeness_check(node, cmds, verify, services, inventory)
    if _comp_notes:
        cmds, verify = _comp_cmds, _comp_verify
    # §17.1508 — a "queue/search the missing episodes" step triggers Sonarr's per-series
    # search for each show's gap, holding a whole-series backfill above a threshold.
    _q_cmds, _q_verify, _q_notes = rewrite_queue_missing(node, cmds, verify, services, inventory)
    if _q_notes:
        cmds, verify = _q_cmds, _q_verify
        _comp_notes = list(_comp_notes) + _q_notes
    # §17.1515 — "remove the stalled downloads and re-search": bulk-clear the dead queue items
    # (blocklist) and trigger MissingEpisodeSearch, deterministically.
    _cs_cmds, _cs_verify, _cs_notes = rewrite_clear_stalled(node, cmds, verify, services, inventory)
    if _cs_notes:
        cmds, verify = _cs_cmds, _cs_verify
        _comp_notes = list(_comp_notes) + _cs_notes
    # §17.1650 — "dismiss the import-blocked queue items": out of the queue only, then the app's missing search.
    _db_cmds, _db_verify, _db_notes = rewrite_dismiss_import_blocked(node, cmds, verify, services, inventory)
    if _db_notes:
        cmds, verify = _db_cmds, _db_verify
        _comp_notes = list(_comp_notes) + _db_notes
    # §17.1523 — "inventory the Prowlarr indexers": read /api/v1/indexer + /indexerstatus in-guest
    # (no service stop, no jq) and print the table, deterministically.
    _pi_cmds, _pi_verify, _pi_notes = rewrite_inventory_prowlarr_indexers(node, cmds, verify, services, inventory)
    if _pi_notes:
        cmds, verify = _pi_cmds, _pi_verify
        _comp_notes = list(_comp_notes) + _pi_notes
    # §17.1524 — "disable the failing indexers": PUT each failing indexer's own full object back
    # with enable=false and forceSave=true, deterministically (the draft was an inert template).
    _pd_cmds, _pd_verify, _pd_notes = rewrite_disable_failing_indexers(node, cmds, verify, services, inventory)
    if _pd_notes:
        cmds, verify = _pd_cmds, _pd_verify
        _comp_notes = list(_comp_notes) + _pd_notes
    # §17.1526 — "add general-purpose public TV indexers": read the definition catalog and POST a
    # curated set of public no-login trackers not already present, deterministically.
    _pa_cmds, _pa_verify, _pa_notes = rewrite_add_general_tv_indexers(node, cmds, verify, services, inventory)
    if _pa_notes:
        cmds, verify = _pa_cmds, _pa_verify
        _comp_notes = list(_comp_notes) + _pa_notes
    # §17.1516 — "deploy FlareSolverr": create its own LXC (host) and run the official Docker
    # container inside it, deterministically (the plan had only the decision, not the deploy step).
    _fs_cmds, _fs_verify, _fs_notes = rewrite_deploy_flaresolverr(node, cmds, verify, services, inventory)
    if _fs_notes:
        cmds, verify = _fs_cmds, _fs_verify
        _comp_notes = list(_comp_notes) + _fs_notes
    # §17.1535 — "deploy Byparr" (FlareSolverr's Camoufox drop-in): same deploy shape, its own LXC.
    _bp_cmds, _bp_verify, _bp_notes = rewrite_deploy_byparr(node, cmds, verify, services, inventory)
    if _bp_notes:
        cmds, verify = _bp_cmds, _bp_verify
        _comp_notes = list(_comp_notes) + _bp_notes
    # §17.1519 — "give container N a working nameserver": set the resolver host-side and reboot,
    # instead of the drafter's in-guest apt (which cannot resolve because DNS is what is broken).
    _ns_cmds, _ns_verify, _ns_notes = rewrite_set_container_nameserver(node, cmds, verify, services, inventory)
    if _ns_notes:
        cmds, verify = _ns_cmds, _ns_verify
        _comp_notes = list(_comp_notes) + _ns_notes
    # §17.1510 — a "configure/organize the Jellyfin library" step gets the streaming-service
    # layout (typed Movies/TV libraries, no mixed pile, a scan) deterministically.
    _jf_cmds, _jf_verify, _jf_notes = rewrite_organize_jellyfin(node, cmds, verify, services, inventory)
    if _jf_notes:
        cmds, verify = _jf_cmds, _jf_verify
        _comp_notes = list(_comp_notes) + _jf_notes
    # §17.1552 — "rename/organize the episode files into season folders": Sonarr renames and moves them
    # itself (renameEpisodes + seasonFolder + one RenameSeries), so Jellyfin can identify every file.
    _sr_cmds, _sr_verify, _sr_notes = rewrite_sonarr_rename_files(node, cmds, verify, services, inventory)
    if _sr_notes:
        cmds, verify = _sr_cmds, _sr_verify
        _comp_notes = list(_comp_notes) + _sr_notes
    # §17.1553 — "refresh the Jellyfin metadata/images": a recursive ReplaceAll refresh per typed library.
    _jr_cmds, _jr_verify, _jr_notes = rewrite_refresh_jellyfin_metadata(node, cmds, verify, services, inventory)
    if _jr_notes:
        cmds, verify = _jr_cmds, _jr_verify
        _comp_notes = list(_comp_notes) + _jr_notes
    # §17.1554 — "verify / enumerate / wait for the Jellyfin metadata": the per-item census, read in-guest.
    _jc_cmds, _jc_verify, _jc_notes = rewrite_jellyfin_metadata_report(node, cmds, verify, services, inventory)
    if _jc_notes:
        cmds, verify = _jc_cmds, _jc_verify
        _comp_notes = list(_comp_notes) + _jc_notes
    # §17.1562 — "make the library re-resolve its files / rebuild the mistyped library": remove, scan, re-add.
    _jb_cmds, _jb_verify, _jb_notes = rewrite_rebuild_jellyfin_library(node, cmds, verify, services, inventory)
    if _jb_notes:
        cmds, verify = _jb_cmds, _jb_verify
        _comp_notes = list(_comp_notes) + _jb_notes
    # §17.1563 — "make <service> reach <public API> / it times out on IPv6": prefer IPv4 in its guest, restart the unit.
    _p4_cmds, _p4_verify, _p4_notes = rewrite_prefer_ipv4_in_guest(node, cmds, verify, services, inventory)
    if _p4_notes:
        cmds, verify = _p4_cmds, _p4_verify
        _comp_notes = list(_comp_notes) + _p4_notes
    # §17.1570 — "disable IPv6 on container N": a persisted sysctl.d drop-in applied live, loopback untouched.
    _d6_cmds, _d6_verify, _d6_notes = rewrite_disable_ipv6_in_guest(node, cmds, verify, services, inventory)
    if _d6_notes:
        cmds, verify = _d6_cmds, _d6_verify
        _comp_notes = list(_comp_notes) + _d6_notes
    # §17.1570h — "Verify IPv6 disabled on <guest>": the engine's own state read, settled by STATE-OK.
    _v6_cmds, _v6_verify, _v6_notes = rewrite_verify_ipv6_disabled(node, cmds, verify, services, inventory)
    if _v6_notes:
        cmds, verify = _v6_cmds, _v6_verify
        _comp_notes = list(_comp_notes) + _v6_notes
    # §17.1571 — "drop the gateway's IPv6 router advertisements at the bridge": an nft bridge table + oneshot unit.
    _ra_cmds, _ra_verify, _ra_notes = rewrite_drop_router_adverts_on_the_bridge(node, cmds, verify, services, inventory)
    if _ra_notes:
        cmds, verify = _ra_cmds, _ra_verify
        _comp_notes = list(_comp_notes) + _ra_notes
    # §17.1573 — a verify step about the RA filter reads the filter the engine installed, by its name.
    _rv_cmds, _rv_verify, _rv_notes = rewrite_verify_router_adverts_dropped(node, cmds, verify, services, inventory)
    if _rv_notes:
        cmds, verify = _rv_cmds, _rv_verify
        _comp_notes = list(_comp_notes) + _rv_notes
    # §17.1572 — "inventory the endpoints/routes of <service>": the engine reads the measured service's own code.
    _ri_cmds, _ri_verify, _ri_notes = rewrite_inventory_service_routes(node, cmds, verify, services, inventory)
    if _ri_notes:
        cmds, verify = _ri_cmds, _ri_verify
        _comp_notes = list(_comp_notes) + _ri_notes
    # §17.1574c — "probe each endpoint authenticated": the engine presents the login it holds to every GET route.
    # §17.1662 — the engine's own API/section check (§17.1641, composed from the service's code before drafting) is
    # final; the older probe (which only reads routes at run time and passes any answer) must not overwrite it
    from app.modules.api_route_check import run_is_the_check as _engine_api_check
    _pe_cmds, _pe_verify, _pe_notes = (rewrite_probe_service_endpoints(
        node, cmds, verify, services, inventory, logins=(policy or {}).get("held_logins") or [])
        if not _engine_api_check(runbook) else (cmds, verify, []))
    if _pe_notes:
        cmds, verify = _pe_cmds, _pe_verify
        _comp_notes = list(_comp_notes) + _pe_notes
    # §17.1667 — "copy <host file> into container N": the engine's own pct push from the host, never an in-guest draft.
    from app.modules import host_file_push as _hfp
    _hp = _hfp.block(node) or _hfp.secret_block(
        node, list((policy or {}).get("held") or []) + list((policy or {}).get("secrets") or []))
    if _hp:
        cmds, verify = _hp[0], _hp[1]
        _comp_notes = list(_comp_notes) + [{"why": _hp[2]}]
    # §17.1669 — "set up WireGuard wgN from <config>" in a container: the engine's own block, judged by a handshake.
    from app.modules import wireguard_up as _wgu
    _wu = _wgu.block(node, cmds)
    if _wu:
        cmds, verify = _wu[0], _wu[1]
        _comp_notes = list(_comp_notes) + [{"why": _wu[2]}]
    # §17.1670 — "configure the VPN kill-switch" for a container's tunnel: CT 105's measured shape, plus Docker's FORWARD.
    from app.modules import kill_switch as _ks
    _kb = _ks.block(node, cmds) or _ks.proof_block(node, cmds)
    if _kb:
        cmds, verify = _kb[0], _kb[1]
        _comp_notes = list(_comp_notes) + [{"why": _kb[2], **(_kb[3] if len(_kb) > 3 else {})}]
    # §17.1671 — "write the official bitmagnet compose": bitmagnet's published file, never one from memory.
    from app.modules import bitmagnet_compose as _bmc
    _bc = _bmc.block(node, cmds, files) or _bmc.start_block(node, cmds, files)
    if _bc:
        cmds, verify = _bc[0], _bc[1]
        _comp_notes = list(_comp_notes) + [{"why": _bc[2], **(_bc[3] if len(_bc) > 3 else {})}]
    # §17.1672 — "add <name> to Prowlarr as Generic Torznab at <url>": Prowlarr's own template, tested before it saves.
    from app.modules import prowlarr_torznab as _ptz
    _pt = (_ptz.block(node, lambda: _arr_guest("prowlarr", services, inventory))
           or _ptz.test_block(node, lambda: _arr_guest("prowlarr", services, inventory), cmds, files))
    if _pt:
        cmds, verify = _pt[0], _pt[1]
        _comp_notes = list(_comp_notes) + [{"why": _pt[2], **(_pt[3] if len(_pt) > 3 else {})}]
    # §17.1656 — "update <arr app> to <version>": from the measured tarball install, never apt.
    from app.modules import arr_update as _au
    _up = _au.block(node, services)
    if _up:
        cmds, verify = _up[0], _up[1]
        _comp_notes = list(_comp_notes) + [{"why": _up[2]}]
    # §17.1645 — "rotate <arr app>'s API key and update every holder": generated in its guest, moved only through
    # root-only files, the app answering with it before any holder changes, each holder updated where it is a VALUE.
    from app.modules import arr_key_rotation as _akr
    _rk = _akr.block(node, lambda app: _arr_guest(app, services, inventory))
    if _rk:
        cmds, verify = _rk[0], _rk[1]
        _comp_notes = list(_comp_notes) + [{"why": _rk[2]}]
    # §17.1570e — a deterministic rewrite REPLACES the draft: the files the drafted runbook asked to write
    # belong to the discarded draft (the `run_in_container` template's /tmp/in_ct_N.sh carrying the model's
    # in-guest script). Live (job eff80f98, T6): the rewrite's commands were right, but the draft's file —
    # with `net.ipv6.conf.all.disable_ipv6 = 1` inside — stayed on the frame, the §17.1570d gate refused it,
    # and the run would have written it to the host anyway. The rewrites never declare files.
    # §17.1589 — a DEVELOPED delivery (its files staged under /tmp/scaffold-dev-…) is the develop flow's: read the live
    # files, proposed whole, gated, rehearsed, judged by its journal. No capability's shape replaces it. Live, T7's
    # delivery was swapped for the FlareSolverr deploy by the chain above.
    if _comp_notes and is_develop_delivery(files):
        logger.warning("capability_rewrite_skipped_on_delivery node=%s notes=%d", (node or {}).get("node_key"), len(_comp_notes))
        cmds, verify, _comp_notes = _orig_cmds, _orig_verify, []
    if _comp_notes and files:
        logger.info("rewrite_dropped_drafted_files node=%s files=%d", (node or {}).get("node_key"), len(files))
        files = []
    # §17.1570g — a rewrite may say what its check SHOWS when the goal holds (`{"expect": {check: text}}`);
    # the frame carries it to `already_met`, whose judge then has a deterministic verdict instead of
    # reading the step's title. Live, T6 was recorded "already met" twice off a check that printed
    # `lo.disable_ipv6=1` — the judge confirmed "Apply sysctl live prowlarr" because the title says
    # nothing about loopback; the capability knows the invariant and now states it.
    _expects: dict[str, str] = {}
    for _n in _comp_notes or []:
        if isinstance(_n, dict) and isinstance(_n.get("expect"), dict):
            _expects.update({str(k): str(v) for k, v in _n["expect"].items()})
    # §17.1671d — a capability whose block PROVES something no check can show beforehand is never "already met"
    _always_run = next((str(_n["always_run"]) for _n in _comp_notes or []
                        if isinstance(_n, dict) and _n.get("always_run")), "")
    # §17.1502 — a host-side call to a guest's port on loopback is wrapped to run
    # inside that guest (measured port→guest map), BEFORE the key reader and the
    # §17.1368 refusal gate, so the call reaches the service instead of parking.
    _loop_notes: list[dict] = []
    if services:
        try:
            from app.modules import service_truth as _st_fix
            cmds, verify, _loop_notes = _st_fix.send_loopback_into_its_guest(cmds, verify, services)
        except Exception as exc:
            logger.warning("loopback_repair_failed err=%r", exc)
    # §17.1500 — a readable (non-stored) service key the drafter wrote as `$NAME`
    # becomes the `<NAME>` placeholder in the guest's own shell, so the reader below
    # fills it. A stored secret's `$NAME` is left for the runner to inject.
    _stored_names = {str(x) for x in ((policy or {}).get("secrets") or [])} \
        | {str(x) for x in ((policy or {}).get("held") or [])}
    cmds, verify, _var_notes = _mv.read_a_readable_key_written_as_a_var(cmds, verify, _stored_names)
    cmds, verify, _mv_notes = _mv.read_on_the_machine(cmds, verify, inventory)
    _mv_notes = list(_mv_notes) + _ak_notes
    _mv_notes = list(_fold_notes) + list(_var_notes) + list(_mv_notes)
    # §17.1333 — and a value the ENGINE holds about ITSELF. A literal address is
    # safe in a written file too, so files are filled here.
    cmds, verify, files, _ea_notes = _mv.read_from_the_engine(cmds, verify, files, engine_address)
    _mv_notes = list(_mv_notes) + _ea_notes
    # §17.1501 — an empty-body POST to an *arr `/{resource}/test` validates the whole
    # resource and fails on its required fields; the standard "test everything
    # configured" is `/{resource}/testall`, which needs no body. Repair it here, before
    # the §17.1378b refusal gate, so a connectivity-verify step runs instead of parking.
    cmds, verify, _test_notes = repair_empty_body_test_call(cmds, verify)
    _mv_notes = list(_comp_notes) + list(_loop_notes) + list(_mv_notes) + _test_notes
    _vhits = verify_needs_a_value_the_run_never_used(cmds, verify, files)
    _vdrops: list[dict] = []
    if _vhits and any(v not in {h["command"] for h in _vhits} for v in verify):
        _bad = {h["command"] for h in _vhits}
        verify = [v for v in verify if v not in _bad]
        _vdrops = [{"why": f"dropped the check `{h['command'][:120]}` -- {h['why'].split(':', 1)[0]}: the run "
                           f"never uses that value, so nobody has it to type; the remaining checks stand"}
                   for h in _vhits]
        _vhits = []
    # §17.1548 — a check that curls a service the engine holds a login for presents it
    # as HTTP Basic auth, the password fed to the guest on stdin as the runner-resolved
    # placeholder. BEFORE inputs_for, so the `<SECRET>` it inserts is picked up as a
    # secret input and resolved by apply_runner_secrets below (never travelling here).
    cmds, verify, _ba_notes = _mv.present_a_held_login(cmds, verify, services, (policy or {}).get("held_logins") or [])
    _mv_notes = list(_mv_notes) + _ba_notes
    inputs = inputs_for(cmds, verify, runbook, files)      # §17.1280 — a file's placeholders are inputs too
    # §17.1191 — a secret is resolved BY THE RUNNER or not at all; it is never
    # typed here and never travels through the engine.
    cmds, verify, inputs, secrets_resolved, secrets_missing = apply_runner_secrets(cmds, verify, inputs, policy)
    if inputs:                                   # §17.1188 — offer what the engine already knows
        from app.modules.runbook_inputs import suggest_inputs
        inputs = suggest_inputs(inputs, env, text=step_text(node))   # §17.1275 — and what the step says
    _mv_asked = (_mv.still_asked(inputs, inventory)          # §17.1332 — the other end of the fix
                 + _mv.engine_still_asked(inputs, engine_address))   # §17.1333
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
    _secret_files = secrets_in_files(files, inputs, secrets_resolved)   # §17.1589 — a resolved secret in a file is still in a file
    runnable, refused = gate_block(shape, policy.get("allow") or [])
    # §17.1346 — the services the engine MEASURED on these machines judge the block's
    # values: a port or a data dir that belongs to another guest, and an edit to a
    # config the service rewrites itself.
    _svc_refused: list[dict] = []
    if services:
        try:
            from app.modules import service_truth as _st
            _svc_refused = (_st.values_from_another_guest(cmds, files, services)
                            + _st.edits_a_config_the_service_rewrites(cmds, files, services)
                            # §17.1368 — and the same mistake from the host side
                            + _st.loopback_on_the_host(cmds, files, services)
                            # §17.1405 — and a write to a measured config that sudo does not cover
                            + _st.a_redirect_sudo_does_not_cover(cmds, files, services))
        except TypeError:
            # §17.1359 — a swallowed TypeError here once disabled ~15 gates for a
            # day. A signature that no longer matches is a defect in this file,
            # not a condition to log past.
            raise
        except Exception as exc:
            logger.warning("service_truth_gates_failed err=%r", exc)
    # §17.1348 — a variable nothing sets expands to nothing, and the command runs
    # with an empty value rather than failing loudly.
    _empty_vars = variables_nothing_sets(cmds, files, policy)
    # §17.1519 — a precondition is computed from the ORIGINAL drafted block (the in-guest apt), before
    # the rewrites above replace it. When this step was rewritten to SET the nameserver host-side, a
    # stale "cannot resolve names" precondition is self-contradictory — this step IS the remedy — so it
    # is dropped rather than refusing the very step that fixes DNS.
    _pre = list(preconditions or [])
    if _ns_notes:
        _pre = [p for p in _pre if "cannot resolve names" not in str((p or {}).get("why") or "")]
    refused = list(refused) + _pre + _mv_asked + _svc_refused + _empty_vars   # §17.1213, §17.1332, §17.1346
    # §17.1312 — the drafter said the template's content was cut twice; the frame says it
    # too, as a refusal, so Run is withheld and the redraft chain carries the reason.
    if str(runbook or "").lstrip().startswith("<!-- runbook-cut -->"):
        _why = " ".join(_section(runbook, "Why this cannot run yet").split())[:400]
        refused = refused + [{"command": (cmds[0] if cmds else "(content cut)"), "why": f"content cut: {_why}"}]
    # §17.1234 — a write that cannot report an HTTP error is a SHAPE problem the
    # engine made, so it joins the gate's refusals and the §17.1196 redraft gets
    # a chance to fix it before the operator ever sees the block.
    refused = refused + curl_writes_without_fail(cmds)
    refused = refused + public_dns_to_private_address(cmds)   # §17.1453
    refused = refused + pve_config_written_inside_a_guest(cmds)   # §17.1459
    refused = refused + host_only_inside_a_guest(cmds, files)       # §17.1473
    # §17.1488 — the names a store holds (the runner's own file, the engine's encrypted store): only those are secrets
    _stored = ({str(n) for n in ((policy or {}).get("secrets") or [])}
               | {str(n) for n in ((policy or {}).get("held") or [])})
    refused = refused + secret_inside_a_guest_script(files, _stored)
    # §17.1248 — a pipe out of `pct exec` executes on the HOST.
    refused = refused + pipe_escapes_the_guest(cmds)
    # §17.1283 — a runner older than helper 19 elevates only the head of a line.
    refused = refused + compound_write_on_a_head_only_runner(cmds, policy)
    refused = refused + locals_the_runner_reads_as_secrets(list(cmds) + list(verify), policy)   # §17.1578
    refused = refused + single_quoted_names_the_runner_reads_as_secrets(list(cmds), policy)   # §17.1638 — the supervised path only
    refused = refused + a_secret_name_written_as_its_value(cmds, files, policy, verify)         # §17.1581
    refused = refused + read_head_redirects_into_system(cmds, policy)   # §17.1462
    # §17.1285 — a step about a guest whose commands never leave the host.
    refused = refused + commands_never_reach_the_guest(cmds, node, shape_files, inventory)   # §17.1344
    refused = refused + writes_in_a_read_only_step(cmds, node, runbook, shape_files)   # §17.1637, §17.1699
    # §17.1270 — repair the one quoting mistake that is provably a mistake, and
    # prove the repair by compiling it, before anything is refused for it.
    cmds, _repairs = repair_shell_quoted_payloads(cmds)
    _repairs = list(_repairs) + _vdrops + _lookup_repairs + _mv_notes + _local_notes  # §17.1288d/o, §17.1332 — a dropped check, a repaired lookup: corrections too
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
    # §17.1355 — the block mistook the engine's own scaffolding for the work:
    # a written file that runs itself, and a check that reads only that file.
    refused = refused + the_block_runs_its_own_scaffolding(shape_files)
    from app.modules.prints_a_secret import prints_a_credential as _prints_a_credential
    refused = refused + _prints_a_credential(cmds, verify)              # §17.1652 — a key is used, never printed
    from app.modules.prints_a_secret import prints_arr_settings as _prints_arr_settings
    refused = refused + _prints_arr_settings(cmds, verify)              # §17.1673 — an *arr settings list holds credentials
    from app.modules.check_writes import a_check_that_writes as _a_check_that_writes
    refused = refused + _a_check_that_writes(cmds, node)                 # §17.1659 — a check does not change what it checks
    refused = refused + a_check_that_only_reads_the_scaffolding(verify, shape_files)
    # §17.1288 — a check must use what the run used; a secret rides no ssh
    # command line; the neighbour table is read warm. Three readings of one frame.
    refused = refused + _vhits                            # §17.1288 — only when no check would be left
    # §17.1345 — a block with NO check proves nothing. Every piece of machinery that
    # confirms a step reads its checks: §17.1302 records an already-met step from
    # them, the post-run judge reads them, §17.1343's inert key passed because a
    # check looked at the file and not at the section. A block with none is recorded
    # done on exit codes alone, which is how a step "succeeds" having changed
    # nothing that matters. Live, ADD131 was drafted with `verify: []`.
    # §17.1388/§17.1389 — a block with no check is an ENGINE gap when the engine
    # can compose the read-back itself, so the refusal is raised and then handed
    # to the filler, which only closes it once the rule agrees it is closed. One
    # mechanism, not a special case: `_WHOSE_GAP` says whose gap each refusal is
    # and `_FILLERS` says what the engine does about the ones it owns.
    # §17.1477 — a block that only READS changes nothing: its reads are its evidence, so they are its checks.
    # Live, T37 "Validate entire build" — 25 reads across the host and every guest — and ADD158 (one
    # `ipmitool mc watchdog get`) were refused "this block has no check at all".
    if cmds and not verify and all(_reads_only(c) for c in cmds):
        verify = list(cmds)
    # §17.1488 — and the step's OWN checks count here, as they do at the run (§17.1470): ADD160 named three
    # (`wg show`, the public-address read, the IPv6 read) and its frame was refused "this block has no check at all"
    if cmds and not verify:
        verify = with_own_checks(node, verify, cmds)
    # §17.1680 — and a template block the engine itself marked read-only (§17.1608's `set +e` line) is the same case:
    # its commands are the template's wrapper (`bash /tmp/in_ct_N.sh start`), its reads are inside the script, and the
    # check the drafter repeated (`curl … | jq …`) is one the read channel drops. Live (job c2518102 T2 "Fetch Sonarr
    # wanted missing"): one read of Sonarr's missing list, refused "this block has no check at all".
    _reads_only_block = (any(READS_ONLY_MARKER in str((f or {}).get("content") or "") for f in shape_files or [])
                         or a_read_only_program(node, shape_files, cmds))                        # §17.1680c/1694d
    if cmds and not verify and not _reads_only_block:
        _no_check = [{"command": cmds[0][:120], "why": (
            "this block has no check at all: nothing would confirm it, and a step recorded done on exit "
            "codes alone is how a change that did nothing passes (§17.1343: the key was written, the file "
            "had it, and the application never read it). Name at least one command that READS the result "
            "the step is supposed to leave -- the value back out of the API, the key inside its section, "
            f"the service active -- not the command that made it.{_read_only_shape_hint(node)}{_dropped_note(runbook)}")}]
        _no_check, verify, _engine_fills = fill_what_the_engine_holds(
            _no_check, cmds, verify, inventory)
        refused = refused + _no_check
        _repairs = list(_repairs) + _engine_fills
    # §17.1362 — a change made through an API that no check reads back.
    from app.modules import develop as _develop          # §17.1415 — the step's own acceptance check
    refused = refused + changes_an_api_without_reading_it(cmds, verify, shape_files,
                                                          _develop.acceptance_checks(node))
    # §17.1367 — a value read from a machine, pasted into a shell word.
    refused = refused + a_machine_value_in_a_shell_word(cmds, shape_files)
    refused = refused + secret_in_an_ssh_command_line(cmds, shape_files)
    refused = refused + prints_a_secret(cmds, verify, shape_files)        # §17.1603
    refused = refused + a_command_that_does_not_parse(cmds)               # §17.1616
    from app.modules.arr_api_shapes import wrong_fields as _wrong_fields
    refused = refused + _wrong_fields(cmds, shape_files)                  # §17.1694
    refused = refused + a_sed_program_that_does_not_compile(cmds)         # §17.1617
    from app.modules.guest_evidence import tools_only_in_containers as _toic   # §17.1614
    refused = refused + _toic(upstream, list(cmds) + [str((f or {}).get("content") or "") for f in shape_files or []])
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
    refused = refused + unsourced_addresses_in_files(cmds, files, env, node, upstream,   # §17.1312
                                                     [engine_address, os.environ.get("SCAFFOLD_LAN_ADDRESS")])
    # §17.1357 — judged against the units of the guest each line addresses
    refused = refused + unsourced_service_name(cmds, files, env, node, upstream, units,
                                              units_by_guest)  # §17.1327, §17.1357
    # §17.1288m — the held password is referenced, not asked for again; a
    # wait pings the guest, not the router. (A secret as an ARGUMENT by name,
    # `--key $TOKEN`, is the §17.1191/1193 contract and is not refused.)
    refused = refused + asks_for_a_secret_the_store_holds(
        [str(i.get("name") or "") for i in inputs if i.get("secret")], policy, node)   # the ones still ASKED
    refused = refused + waits_on_a_fixed_address(cmds, node, files)
    # §17.1377 — a body in a file is only a body if the reader's machine can see
    # the file; and an inline program that never imports what it uses cannot run.
    refused = refused + a_program_that_uses_a_name_it_never_defines(cmds, shape_files)
    # §17.1378 — a secret set on the object instead of in the `fields` array the API
    # keeps it in, and a `/test` endpoint handed a body it validates and rejects.
    # §17.1379 — and a call its own function rejects at call time.
    refused = refused + a_call_its_own_function_cannot_accept(cmds, shape_files)
    refused = refused + a_credential_set_where_the_api_does_not_keep_it(cmds, shape_files)
    refused = refused + a_validating_endpoint_sent_an_empty_body(cmds, shape_files)
    # §17.1380 — and the GENERAL check behind those two specific ones: the type
    # checker the engine already runs on its own source, run on the program the
    # engine writes for the operator's machines. Fed from the one extractor
    # (§17.1379) so it cannot see a shape the others miss.
    from app.modules.program_check import a_program_the_type_checker_says_will_raise
    refused = refused + a_program_the_type_checker_says_will_raise(
        _inline_programs(cmds, shape_files))
    # §17.1382 — a check that answers the same thing on every machine, and an
    # identifying field sent as 0 by a draft that had nowhere to get it.
    refused = refused + a_check_that_proves_nothing(verify)
    # §17.1390 — and a check the read-only channel will refuse to run.
    refused = refused + a_check_the_channel_will_refuse(verify)
    # §17.1391 — and a credential typed in as a literal where the engine reads it.
    refused = refused + a_credential_written_as_a_literal(cmds, shape_files, verify)
    refused = refused + an_id_sent_as_zero(cmds, shape_files)
    # §17.1397 — input piped to the guest agent that it never reads.
    refused = refused + a_pipe_the_guest_agent_never_reads(cmds, shape_files)
    # §17.1399 — a host key forgotten and nothing pinned in its place.
    refused = refused + a_host_key_forgotten_not_pinned(cmds, shape_files)
    # §17.1400 — the agent's JSON answer read by a text tool.
    refused = refused + the_agents_json_read_as_text(cmds, shape_files)
    # §17.1403 — a host-written file, or a host tool, run inside a guest.
    refused = refused + a_host_file_run_inside_a_guest(cmds, shape_files)
    # §17.1405 — JSON used as shell quoting.
    refused = refused + json_used_as_shell_quoting(cmds, shape_files)
    # §17.1383 — and the block must read a service's key where that service
    # keeps it: the substitution leaves a written file alone, so a draft that
    # rolls its own read can roll the wrong one.
    refused = refused + _mv.reads_a_key_where_the_service_does_not_keep_it(cmds, shape_files)
    # §17.1384 — and a block that stops reading altogether and invents a login
    # for a service whose key the engine holds.
    refused = refused + _mv.authenticates_instead_of_reading_the_key(cmds, shape_files)
    # §17.1385 — and a host-side program calling a service whose key nothing
    # can give it there: the one position the earlier refusals left with no answer.
    refused = refused + _mv.a_host_program_needs_a_key_only_the_guest_can_read(
        cmds, shape_files, policy)
    try:
        from app.modules import service_truth as _st2
        refused = refused + _st2.a_file_read_on_another_machine(cmds, shape_files)
    except TypeError:
        raise
    except Exception as exc:                   # pragma: no cover - defensive
        logger.warning("file_machine_gate_failed err=%r", exc)
    # §17.1319 — a template's run is a LIST of phases; a refusal earned by the file they
    # all run repeats once per phase (live: nine identical lines for one finding). The
    # first command keeps it; the same words are not said again.
    _seen_why: set[str] = set()
    _dedup: list[dict] = []
    for _r in refused:
        _w = str((_r or {}).get("why") or "")
        if _w in _seen_why:
            continue
        _seen_why.add(_w)
        _dedup.append(_r)
    refused = _dedup
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
    # §17.1570d — a sysctl that disables IPv6 on ALL interfaces takes loopback with it (::1 gone, services
    # bound to it dark). Live, the model's draft for "Write sysctl config prowlarr" did exactly that.
    refused = refused + disables_loopback_ipv6(cmds, shape_files)
    # §17.1278 — and it must tell a dead tracker from a bad request the way the
    # service does: by whether a field is NAMED, not whether the key is present.
    refused = refused + classifies_by_key_presence(cmds, shape_files)
    # §17.1630 — the frame shows every check the run will make: the step's own "Done when" checks were added only at
    # the run (§17.1470/§17.1625), so OL2's frame listed `qm status 110` while two in-guest checks ran after it.
    if cmds:
        verify = with_own_checks(node, verify, cmds)
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
    # §17.1472 — the operator can send the block back with what is wrong with it
    options.append({"id": "redraft", "label": "Redraft it with my correction",
                    "fit": "the block is wrong; say what in the note and the engine drafts the step again"})
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
        # §17.1363 — what the engine READ for this step, and what it could not.
        # The frame carried 24 fields and exactly one shaped like a gap
        # (`secrets_missing`), so a draft built on a narrow reading looked exactly
        # like one built on a complete one -- to the drafter, to the gates, to the
        # operator and to the post-run judge.
        "not_measured": (reading.gaps() if reading is not None else []),
        # §17.1577 — when a capability REPLACED the draft, the operator reads the block that will run first
        "runbook": (_rewritten_for_display(runbook, cmds, verify, files, _repairs) if _comp_notes
                    else _runbook_for_display(runbook, cmds)),
        "allow": list(policy.get("allow") or []),
        "sudo": bool(policy.get("sudo")),
        "hands_on_reason": node.get("hands_on_reason") or "",
        "expects": _expects,                      # §17.1570g — what a rewrite's check shows when the goal holds
        "always_run": _always_run,                # §17.1671d — the block proves what no pre-read can
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


#: §17.1372b — a diagnosis asserting the runner refused, or that a command
#: changed nothing despite exiting 0.
_CLAIMS_A_REFUSAL_RE = re.compile(
    r"refuses\s+mutation|refuse[sd]?\s+(?:the\s+)?(?:write|mutation|command)|"
    r"success-looking\s+exit|without\s+actually\s+(?:chang|do)ing|"
    r"did\s+not\s+actually\s+(?:run|change|apply)|silently\s+(?:did\s+nothing|ignored)", re.I)
#: what the runner's own refusal looks like in a record
_RUNNER_REFUSAL_MARK = "(refused by the local runner"


def diagnosis_contradicts_the_record(executed: list[dict], diagnosis: str) -> str:
    """Why this diagnosis cannot be true given the record, or ``""``.

    §17.1372b — live, 2026-10-05. ADD135's three commands each exited 0 and the
    first printed the tool's own confirmation:

        $ qm set 106 --onboot 1 --startup order=10
        update VM 106: -onboot 1 -startup order=10

    The diagnosis said: *"The most likely cause is that the command was run
    through the local runner (pve-runner), which refuses mutation verbs — it
    returned a success-looking exit code without actually changing anything."*

    Measured immediately afterwards: `onboot: 1` and `startup: order=10/20/30` on
    106, 111 and 120. The work had landed. Nothing in the record carries a runner
    refusal — `refused` is false on every entry and no output holds
    `(refused by the local runner` — so the claim is contradicted by the evidence
    the diagnosis was handed.

    This matters more than a wrong sentence: §17.1366 feeds diagnoses into the
    NEXT draft, so a false cause actively steers the retry wrong.
    """
    text = str(diagnosis or "")
    if not text.strip() or not _CLAIMS_A_REFUSAL_RE.search(text):
        return ""
    refused = [e for e in executed or []
               if e.get("refused") or _RUNNER_REFUSAL_MARK in str(e.get("output") or "")]
    if refused:
        return ""                     # the record does carry a refusal
    ran = [e for e in executed or [] if not e.get("informational")]
    zero = [e for e in ran if e.get("exit") == 0]
    if not ran or len(zero) != len(ran):
        return ""                     # something really did fail; not this check's business
    spoke = [e for e in zero if str(e.get("output") or "").strip()]
    return ("it says the runner refused the work or changed nothing, and the record says otherwise: "
            f"{len(ran)} command(s) ran, every one exited 0, none is marked refused and none printed "
            f"the runner's own refusal"
            + (f", and {len(spoke)} printed the tool's own output (e.g. "
               f"`{str(spoke[0].get('output') or '').strip().splitlines()[0][:80]}`)" if spoke else ""))


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
        # §17.1372b — a diagnosis that contradicts the record is worse than none,
        # because §17.1366 hands it to the next draft. Replaced with the facts.
        _wrong = diagnosis_contradicts_the_record(executed, out)
        if _wrong:
            logger.warning("supervised_run_diagnosis_refused job=%s node=%s why=%s",
                           job_id, node_key, _wrong[:200])
            lines = "\n".join(f"$ {e.get('command')}\n{str(e.get('output') or '').strip() or '(no output)'}"
                               for e in (executed or [])[:6])
            return ("## What the record actually says\n\n"
                    "The engine drafted a diagnosis and then refused it: " + _wrong + ".\n\n"
                    "So the commands did run and the machine accepted them. What follows is the record "
                    "itself, with nothing inferred — read it before retrying, because repeating a write "
                    "that already happened is not a retry:\n\n```\n" + lines + "\n```\n\n"
                    "If this step was recorded failed, look first at whether a CHECK is wrong rather "
                    "than the work: a check that prints nothing says nothing (§17.1372).")
        logger.warning("supervised_run_diagnosed job=%s node=%s chars=%d", job_id, node_key, len(out))
        return out
    except Exception as exc:
        logger.warning("supervised_run_diagnose_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return ""


async def resolve_run(db: AsyncSession, job_id: str, node_key: str, choice: str, waiting: dict,
                      inputs: dict | None = None, note: str = "", verdict: str | None = None) -> dict:
    """Carry out the operator's choice on a ``kind="run"`` pause. Returns
    ``{"outcome": …}`` for ``decision_pause.resolve_decision`` to finish (it
    records the decision on the job and moves it back to executing)."""
    choice = (choice or "").strip().lower()
    if choice not in CHOICES:
        return {"outcome": "bad_choice", "choices": list(CHOICES)}
    runbook = str(waiting.get("runbook") or "")
    if choice == "skip":
        # §17.1661 — a stated verdict is recorded where §17.1644 reads it (last_verification_reason)
        _verdict = verdict if verdict in ("passed", "failed") else None
        upd = await db.execute(
            text("UPDATE dag_nodes SET status = 'skipped', output_text = COALESCE(output_text, :out), completed_at = NOW(), "
                 "updated_at = NOW(), last_verification_reason = COALESCE(:why, last_verification_reason) "
                 "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": node_key, "why": (f"skipped by the operator -- verdict {_verdict}" if _verdict else None),
             "out": "Skipped by the operator at the supervised-run pause."
                    + (f"\n\nOperator's verdict: {_verdict}" if _verdict else "")
                    + (f"\n\nOperator's note: {note.strip()}" if (note or "").strip() else "")})
        return {"outcome": "skipped" if upd.rowcount else "node_gone", "node_status": "skipped"}
    if choice == "myself":
        # §17.1614c — the operator's note IS the record of what they did: live (job 0fb4ea7f T9), the note "Ollama runs in
        # Docker … `docker exec ollama ollama pull`" was dropped and the node kept only the unrun draft
        out = ((f"## Done by the operator\n{note.strip()}\n\n" if (note or "").strip() else "") + (runbook or "(runbook)"))
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
    # §17.1625 — the step's whole text: the frame's `description` is `description or prompt_template`, so a short
    # description hid the prompt's "Done when"; the frame now carries the prompt too (a frame parked before it: as before)
    verify_cmds = with_own_checks({"title": waiting.get("title"), "description": waiting.get("description"),
                                   "prompt_template": waiting.get("prompt_template")},
                                  waiting.get("verify") or [], commands)         # §17.1470, §17.1625
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
            _rw = [secret_ref_rewrite(c, name) for c in commands]
            _rwv = [secret_ref_rewrite(c, name) for c in verify_cmds]
            _stuck = [p for _c, p in _rw + _rwv if p]
            if _stuck:
                # §17.1649 — never run a command the stored answer cannot reach: it would run with the bare placeholder
                return {"outcome": "not_runnable", "refused": [{"command": "", "why": (
                    f"`<{name}>` sits where a stored value cannot reach -- {_stuck[0]} -- so the command would run with "
                    f"the literal text `<{name}>` (live, PK1 wrote it into CT 111's known_hosts). Redraft it so the value "
                    f"is read in the same command that uses it.")}]}
            commands, verify_cmds = [c for c, _p in _rw], [c for c, _p in _rwv]
        if need:
            commands = substitute(commands, values)
            verify_cmds = substitute(verify_cmds, values)
        # §17.1649 — backstop: no asked placeholder may survive into what runs
        _left_ph = sorted({i["name"] for i in asked if any(f"<{i['name']}>" in c for c in commands + verify_cmds)})
        if _left_ph:
            return {"outcome": "not_runnable", "refused": [{"command": "", "why": (
                f"`<{_left_ph[0]}>` is still in the command after the answers were applied, so it would run with the "
                f"placeholder itself (§17.1649).")}]}
    ch = await channel(db)
    if ch is None:
        return {"outcome": "no_channel"}
    spec, policy = ch
    from app.modules import assist_supervised as _sw
    from app.modules import assist_local_runner as _lr
    runnable, refused = _sw.gate_block(commands, policy.get("allow") or [])
    if refused:                                   # the policy changed since the frame was drawn, or a value broke the shape
        return {"outcome": "not_runnable", "refused": [{"command": mask_secrets(r["command"], values, need), "why": r["why"]} for r in refused]}
    # §17.1457 — the last look before anything runs: the machine may have moved since the frame was drawn
    from app.modules.rewrite_guard import destructive_rewrites
    _lost = await destructive_rewrites(runnable, spec, [f for f in (waiting.get("files") or []) if (f or {}).get("path")])
    if _lost:
        return {"outcome": "not_runnable", "refused": [{"command": mask_secrets(r["command"], values, need), "why": r["why"]} for r in _lost]}
    # §17.1466 — the addresses of the guests this block touches, read before it runs
    from app.modules import address_watch as _aw
    _aw_guests, _aw_before = await _aw.around_run(spec, runnable, waiting.get("files") or [])
    # §17.1271 — the files this block needs, written first and each one with its
    # own signed approval. They come BEFORE the commands because the commands are
    # what run them, and a failure to write stops the block exactly as a failed
    # command does: the records share one shape and one sequence.
    _files = [{"path": f["path"], "content": f["content"]}
              for f in (waiting.get("files") or []) if (f or {}).get("path")]
    # §17.1280 — the values the operator supplied (or the machine answered) go
    # into the files as well as the commands; a placeholder left over is a
    # problem to report, never bytes on disk.
    # §17.1496 — and it is decided BEFORE the node is claimed: this return used to
    # come after the claim's commit, so the caller's rollback undid nothing, the
    # node sat at `running` under a job still awaiting the decision, and every
    # later "run" answered `node_gone` until the orphan reaper reset it -- and
    # then the same loop again. A refusal must leave the node where it found it.
    _files, _left = fill_files(_files, values)
    if _left:
        return {"outcome": "inputs_missing", "problems": _left, "inputs": asked}
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
    _unverified = ""                           # §17.1390
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
                        goal_claim(str(waiting.get("title") or node_key), str(waiting.get("description") or "")), verify_cmds, pasted)
            elif ok:
                # §17.1390 — before judging the ANSWERS, ask whether the checks
                # ran at all. A refused check is not an ambiguous check: §17.1233
                # deliberately lets an unclear answer leave the outcome alone,
                # and a check the runner never executed reads identically to one
                # that answered unclearly. Live, 2026-10-06: every check was
                # refused (`python3` on a read-only channel), the verify came
                # back empty, and the step was recorded done on one exit code --
                # §17.1343's hollow success, through the one door still open.
                # `is False` on purpose: a missing key is UNKNOWN, not a
                # claim that the check never ran. The looser form flipped a
                # unit test whose probe dicts carry no `ran` at all.
                _never_ran = [x for x in (ran or []) if x.get("ran") is False]
                if verify_cmds and len(_never_ran) == len(verify_cmds) and the_run_is_the_read(waiting, executed):
                    # §17.1574e — a READ the read-only channel refused for privilege (§17.1574b) just ran, approved, on
                    # the root channel: its own output IS the reading. Re-running the same command as a "check" on the
                    # unprivileged channel is refused again by construction — that refusal says nothing about the work.
                    # Live (job 9e1c2c68, T8/T10): `pve-firewall status; pve-firewall compile` printed the whole
                    # ruleset as root and the step was then recorded failed for "nothing confirmed the work".
                    verify_out = ("(the run is the read: it ran as root with the operator's approval and its output above "
                                  "is the reading; the read-only channel cannot repeat it — §17.1574e)")
                    logger.info("supervised_run_read_as_root job=%s node=%s", job_id, node_key)
                elif verify_cmds and len(_never_ran) == len(verify_cmds):
                    _unverified = (
                        f"the {len(verify_cmds)} check(s) this step offered were all refused by "
                        f"{spec.name} and never ran, so nothing confirmed the work: "
                        f"{str((_never_ran[0].get('why') or '')).strip()[:160]}. The commands exited 0, "
                        f"which is exactly the evidence §17.1345 says is not enough. Write a check the "
                        f"read-only channel takes -- a plain `curl`, `cat`, `find` or `systemctl "
                        f"is-active` inside the guest.")
                    logger.warning("supervised_run_no_check_ran job=%s node=%s checks=%d",
                                   job_id, node_key, len(verify_cmds))
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
                # §17.1570g — the post-run judge gets the rewrite's stated expectation too (STATE-OK), so a
                # capability's check is settled deterministically, not read against the step's title.
                _against = contradicted(await _verify_verdicts(
                    goal_claim(str(waiting.get("title") or node_key), str(waiting.get("description") or "")), verify_cmds, pasted,
                    expects={str(k): str(v) for k, v in (waiting.get("expects") or {}).items()}))
                if _against:
                    ok = False
                    refuted = _against
                    logger.warning("supervised_run_verify_contradicted job=%s node=%s checks=%d first=%r",
                                   job_id, node_key, len(_against),
                                   str(_against[0].get("reason"))[:140])
        except Exception as exc:
            verify_out = f"(verify could not run: {exc})"
    # §17.1630 — a "Done when it prints `X`" is a check too, and the cheapest one: X is in what ran, or it is not.
    if ok:
        from app.modules.operator_steps import expected_prints, missing_prints
        _expected = expected_prints({"description": waiting.get("description"),
                                     "prompt_template": waiting.get("prompt_template")})
        _missing = missing_prints(_expected, "\n".join([str(e.get("output") or "") for e in executed] + [verify_out]))
        if _missing:
            ok = False
            refuted = list(refuted) + [{"verdict": "contradicted", "reason": f"nothing printed `{t}`, which its Done when names"}
                                       for t in _missing]
            logger.warning("supervised_run_expected_print_missing job=%s node=%s missing=%r",
                           job_id, node_key, [t[:60] for t in _missing])
    # §17.1680b — a read-only block with no separate check (§17.1680) is judged by its OWN output: the same judge, the
    # step's goal, the block's reads as the one piece of evidence. Live (job c2518102 T2): with no jq in CT 104 the read
    # printed only "Sonarr API request failed", §17.1608's `trap 'exit 0'` made it exit 0, and it was recorded done.
    if ok and not verify_cmds:
        _against = await judge_a_read_only_block(waiting, node_key, executed)
        if _against:
            ok = False
            refuted = list(refuted) + _against
            logger.warning("supervised_run_read_block_contradicted job=%s node=%s first=%r",
                           job_id, node_key, str(_against[0].get("reason"))[:140])
    # §17.1281 — nothing a run printed reaches the record, the response or the
    # log with an engine-held value or a secret-shaped token in it.
    for e in executed:
        e["output"] = scrub_run_output(e.get("output"), secret_env)
    verify_out = scrub_run_output(verify_out, secret_env)
    # §17.1569 — the record's head is the FRAME that was approved and ran, not the pre-rewrite draft
    _head = _frame_runbook(waiting, runnable, verify_cmds) if runnable else runbook
    output = mask_secrets(_executed_report(_head, spec.name, executed, verify_out), values, need)
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
    # §17.1691 — a read-only step whose own output was cut (the runner's 20,000 characters, the template's 300 lines)
    _read_cut = a_read_was_cut(waiting, executed)
    if _read_cut and ok:
        ok = False
        repeated_reason = _read_cut
        logger.warning("supervised_run_read_cut job=%s node=%s", job_id, node_key)
    # §17.1390 — applied after the other downgrades so it cannot be clobbered by
    # the `repeated_reason` assignment above: a step nothing checked is not done.
    if _unverified and ok:
        ok = False
        repeated_reason = _unverified
        logger.warning("supervised_run_unverified job=%s node=%s", job_id, node_key)
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
        # §17.1466 — a guest that moved: a fact, the steps ahead corrected, a step for what done steps set up
        await _aw.after_run(db, spec, job_id, node_key, _aw_guests, _aw_before)
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


def goal_claim(title: str, description: str = "") -> str:
    """§17.1396 — the claim the step's checks are judged against: its title AND its
    description, where the done-condition lives.

    Live, 2026-10-06, ADD135 — "Make the machines that serve the operator's goals
    start on boot", whose description carries the operator's decision: "onboot on
    VM 110 ai-vm ONLY ... Done when 110.conf carries onboot: 1 and 100.conf still
    has no onboot line." `qm set 110 --onboot 1` ran, and the check on VM 100
    answered "no onboot line (correct)". The judge saw only the title, read that
    as a machine that does not start on boot, and CONTRADICTED it: the step that
    did exactly what the operator chose was recorded failed.

    The already-met path had built `title — description` since §17.1302; the
    post-run contradiction (§17.1233) and confirm-after-drop (§17.1225) passed
    the title alone. Three judges of the same checks, two of them blind to the
    condition that settles them.
    """
    # The cap that stood here (400) cut ADD135 off at "OPERATOR DECISION 2026-10-06:
    # s" -- the decision and the done-condition were both past it, so even the
    # already-met path never saw them. Wider, and the done-condition is carried
    # whole when the middle has to go: it is the sentence that settles a check.
    # §17.1570h — text a step INHERITED from another step (§17.1570c's `[as in T2: …]`) is specification
    # for the drafter and the capabilities, not part of this step's claim: live, T17 "Write sysctl config
    # caddy-proxy" was judged "the claim says container 101 (jellyfin) … but the command ran pct exec 120"
    # — T2's text, read as T17's goal — and a step that had landed was recorded failed.
    desc = " ".join(re.sub(r"\[as in T\d+(?:v\d+)?: .*?\]", " ", str(description or ""), flags=re.S).split())
    if not desc:
        return str(title or "")
    if len(desc) > _CLAIM_CAP:
        done = re.search(r"(?:^|(?<=[.!?]\s))(?:Done|Complete|Finished) when\b[^\n]*", desc, re.I)
        tail = done.group(0)[:_CLAIM_CAP // 2] if done else ""
        desc = desc[:_CLAIM_CAP - len(tail)].rstrip() + (" … " + tail if tail else "")
    return f"{title} — {desc}"


#: how much of a step's description the judge reads (§17.1396)
_CLAIM_CAP = 1200


def may_be_already_met(node: Optional[dict], frame: Optional[dict], developed: bool = False,
                       engine_check: bool = False) -> bool:
    """§17.1302 asks the machine whether a step is ALREADY done before running it; this says when that question means
    anything.

    §17.1426 — not for DEVELOPED code judged by `systemctl is-active|is-enabled` alone (live, ADD127 recorded met while
    the panel answered 200 with no credential). §17.1641 — not when the run IS the check. §17.1692 — never for a step
    that declares it only READS: its output is its answer, and there is nothing for it to have already done. Live (job
    e5e22b92 T1 "Login qBittorrent list torrents", reset after §17.1691): the check `test -s /tmp/qb_cookies.txt` still
    held from the first run, the step was recorded already met, and the torrent list was never read."""
    verify = (frame or {}).get("verify") or [""]
    if developed and all(re.search(r"\bsystemctl\s+is-(?:active|enabled|failed)\b", str(c)) for c in verify):
        return False
    if engine_check:
        return False
    return not declares_read_only(node)


#: §17.1691 — the line the in-guest templates print when the step's log is longer than the tail they keep
TEMPLATE_CUT_MARK = "[CUT: the step printed"


def a_read_was_cut(waiting: Optional[dict], executed: list[dict]) -> str:
    """§17.1691 — why a READ-ONLY step's own output cannot be evidence of the whole read ("" when it can).

    A read-only step's record is its answer (§17.1680b), and two cuts left it holding a fraction while it was recorded
    done. Live (job e5e22b92, 2026-10-20): T1 "Login qBittorrent list torrents" printed the torrent list as one JSON line
    and the runner returned its first 20,000 characters (local_runner_mcp's `[:20000]`, silent) -- 10 torrents, the
    tenth cut mid-field; T2 "Read full Sonarr queue" printed Sonarr's pretty JSON and the in-guest template kept the
    last 300 lines -- 7 of 91 records. Only a step that declares it changes nothing is judged: a change's output is not
    its evidence, its checks are."""
    if not only_reads(waiting):                                       # §17.1695
        return ""
    from app.modules.rehearsal import RUNNER_OUTPUT_CAP
    for e in executed or []:
        out = str(e.get("output") or "")
        cmd = str(e.get("command") or "")[:80]
        if TEMPLATE_CUT_MARK in out:
            m = re.search(r"\[CUT: the step printed (\d+) lines", out)
            return (f"this read printed {m.group(1) if m else 'more than 300'} lines inside the guest and the record keeps "
                    "the last 300, so what it saw is mostly not in it -- the step is not done. Print what the step needs, "
                    "not the raw response: counts, and one short line per item with only the fields that matter "
                    "(a `python3 - <<'PY'` program that makes the call and prints a summary).")
        if len(out) >= RUNNER_OUTPUT_CAP - 64:
            return (f"`{cmd}` printed at least {RUNNER_OUTPUT_CAP:,} characters and the runner returns no more than that, "
                    "silently, so the read was cut -- the step is not done. Print what the step needs, not the raw "
                    "response: counts, and one short line per item with only the fields that matter (in the guest, a "
                    "`python3` program that parses the response and prints a summary).")
    return ""


async def judge_a_read_only_block(waiting: dict, node_key: str, executed: list[dict]) -> list[dict]:
    """§17.1680b — the contradicted verdicts for a read-only template block's OWN output ([] when it is not one).

    A block §17.1608 marked read-only needs no separate check (§17.1680); what it printed is the evidence, judged by
    the same judge against the step's goal. Live (job c2518102 T2): with no jq in CT 104 the read printed only "Sonarr
    API request failed", exited 0 under `trap 'exit 0'`, and was recorded done."""
    if not (any(READS_ONLY_MARKER in str((f or {}).get("content") or "") for f in (waiting.get("files") or []))
            or a_read_only_program(waiting, waiting.get("files") or [], waiting.get("commands") or [])):   # §17.1680c/1694d
        return []
    own = "\n".join(str(e.get("output") or "") for e in executed or [])[-6000:]
    return contradicted(await _verify_verdicts(
        goal_claim(str(waiting.get("title") or node_key), str((waiting.get("description") or ""))),
        ["(this block's own reads)"], "== V1 ==\n" + (own.strip() or "(it printed nothing)")))


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
        # §17.1372 — a check that printed NOTHING cannot be evidence against the
        # step unless the runbook said what it should print. An absence check
        # ("confirm nothing was set on 100 and 110") answers correctly with
        # silence, and the judge was reading that silence as "the work did not
        # land". Where an expectation WAS stated, an empty answer still
        # contradicts it, so that path keeps biting.
        _blank = empty_checks(pasted)
        if _blank:
            _expects = expects or {}
            for v in out or []:
                _vid = str(v.get("id") or "").replace("V:", "V")
                if _vid not in _blank or str(v.get("verdict") or "") != "contradicted":
                    continue
                _cmd = str(v.get("command") or "")
                if str(_expects.get(_cmd) or "").strip():
                    continue                      # the runbook said what it shows
                v["verdict"] = "unknown"
                v["reason"] = ("this check printed nothing and the step never said what it should "
                               "print, so it says neither that the work landed nor that it did not: "
                               + str(v.get("reason") or "")[:160])
                logger.warning("verify_empty_not_contradicted id=%s cmd=%r", _vid, _cmd[:90])
        back = {v: k for k, v in ids.items()}
        for v in out:
            v["id"] = back.get(str(v.get("id") or ""), v.get("id"))
        return out
    except Exception as exc:
        logger.warning("verify_judge_failed err=%r", exc)
        return []


#: §17.1372 — a `== V<n> ==` / `== V:<n> ==` marker line in a verify paste.
_MARKER_LINE_RE = re.compile(r"\s*==\s*V:?(\d+)\s*==\s*$")


#: §17.1377 — an inline Python program: `python3 - <<'EOF' … EOF`, `python3 -c "…"`.
_HEREDOC_PY_RE = re.compile(
    r"python3?\s+(?:-\s+)?<<\s*[\"']?(?P<tag>[A-Za-z_]\w*)[\"']?\s*\n(?P<body>.*?)\n(?P=tag)",
    re.S)


def _inline_programs(commands: list[str],
                     files: Optional[list[dict]] = None) -> list[tuple]:
    r"""§17.1379 — every Python program this block runs, as (label, source).

    §17.1377 judged only `python3 - <<'EOF'` heredocs, so the same defect in a
    `.py` file went unjudged -- and a file is the shape FILE_RULES RECOMMENDS.
    Live, 2026-10-05: ADD132's next draft was `/tmp/fix_download_clients.py`,
    4,169 bytes, and the undefined-name gate looked straight past it. One
    extractor now, so a program-level judgment cannot see one shape and miss the
    other ([[feedback_sibling_call_sites_drift]]).
    """
    import ast
    out: list[tuple] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        for m in _HEREDOC_PY_RE.finditer(t):
            out.append((f"python3 <<{m.group('tag')}", m.group("body")))
    run_text = "\n".join(texts)
    for f in files or []:
        path = str((f or {}).get("path") or "")
        body = str((f or {}).get("content") or "")
        if not body.strip():
            continue
        runs = path.endswith(".py") or (
            path and re.search(rf"python3?\s+{re.escape(path)}(?![\w/])", run_text))
        if not runs:
            continue
        try:
            ast.parse(body)
        except SyntaxError:
            continue                           # not ours to judge
        out.append((path or "the program", body))
    return out


#: §17.1382 — commands that answer the same thing on every machine, before and
#: after any step. `id` reached a live frame as a whole verify entry.
_PROVES_NOTHING = {"id", "true", ":", "pwd", "whoami", "hostname", "date", "uptime",
                   "env", "printenv", "uname", "echo"}


def a_credential_written_as_a_literal(commands: list[str], files: Optional[list[dict]] = None,
                                      verify: Optional[list[str]] = None) -> list[dict]:
    r"""§17.1391 — a credential typed into the block instead of read.

    Live, 2026-10-06, and this is the capability from §17.1382 turning on its
    author. The read it added PRINTS the key it finds; §17.1366 carries a
    previous attempt's output into the next draft's context; so the key reached
    the model, and the model wrote it into its own check:

        pct exec 101 -- sh -c 'curl -s -H "X-Emby-Token: c9044034…" …'

    Measured afterwards: **4 model requests and 2 model responses** carried that
    value, and one `dag_nodes.output_text` held it in the clear. §17.1191 says a
    secret is resolved by the runner or not at all and never travels through the
    engine; the engine's own key-reading broke it, because `scrub_run_output`
    masks what the engine SENT and this was read off a machine.

    The masker (§17.1391) keeps it out of the record. This keeps it out of the
    BLOCK, which is the thing that would send it: a literal where the engine has
    a read is refused, with the read as the remedy. A `$( … )` is exactly right
    and says nothing.
    """
    out: list[dict] = []
    texts = ([str(c) for c in commands or []]
             + [str(v) for v in verify or []]
             + [str((f or {}).get("content") or "") for f in files or []])
    seen: set[str] = set()
    for t in texts:
        for m in _CREDENTIAL_LITERAL_RE.finditer(t):
            token = m.group(2)
            if token in seen:
                continue
            seen.add(token)
            out.append({"command": (m.group(1) + token[:4] + "…")[:120], "why": (
                "this writes a credential into the block as a literal value. The engine READS these: "
                "write `<RADARR_API_KEY>`, `<SONARR_API_KEY>`, `<PROWLARR_API_KEY>` or "
                "`<JELLYFIN_API_KEY>` in a command or check that runs inside that guest and the read "
                "is substituted before anyone is asked for anything (§17.1332/1382), or read it "
                "inline with `$( … )`. MEASURED live (§17.1391): a key the engine had printed came "
                "back through a previous attempt's report into 4 model requests and 2 model "
                "responses, and into the step's record in the clear -- §17.1191's contract is that a "
                "secret never travels through the engine at all.")})
    return out


def a_check_the_channel_will_refuse(verify: list[str]) -> list[dict]:
    r"""§17.1390 — a check the verify channel will not run is not a check.

    Live, 2026-10-06, and this one was mine the whole way down. §17.1382 gave the
    engine a Jellyfin key read that uses `python3`; §17.1332 fills that read into
    checks; the verify channel is READ-ONLY and its judge refuses `python3`. So
    the frame was perfect -- `refused: 0`, `verify: 1` -- the check was refused
    at run time, dropped, and the step was recorded **done on one command's exit
    code**:

        supervised_run_done ... commands=1 confirmed_after_drop=False
        verify: (empty)
        last_verification_reason: "1 command(s) ran, all exited 0"

    which is §17.1343's hollow success with a check in front of it. §17.1265 asks
    whether a check SURVIVED parsing; nothing asked whether the channel would
    agree to run it.

    The same predicate the channel itself uses answers it, so this cannot drift
    from the real decision. Measured over every check in the real fixtures: 15 of
    15 read-only-runnable, so the gate costs nothing where checks already work.
    """
    out: list[dict] = []
    try:
        from app.modules.assist_state_check import read_only_command
    except Exception as exc:                   # pragma: no cover - defensive
        logger.warning("read_only_predicate_unavailable err=%r", exc)
        return []
    for v in verify or []:
        if not str(v or "").strip():
            continue
        # §17.1187 — judge the SHAPE: an unfilled `<PLACEHOLDER>` is not a reason
        # the channel would refuse this, and the first cut of this gate flagged
        # 12 of 38 checks across the fixtures for exactly that. The predicate
        # accepts the same checks once the values are in place (measured: an
        # `ssh … "systemctl is-active …"` is fine, the same line with
        # `<PALWORLD_USER>@<GUEST_IP>` is not).
        shape = re.sub(r"<[A-Z][A-Z0-9_]{1,40}>", "x", str(v))   # `<IP>` is two chars
        if read_only_command(shape):
            continue
        out.append({"command": str(v)[:120], "why": (
            "the verify channel will refuse this check, so it would prove nothing and the step "
            "would be recorded done on exit codes alone (§17.1343). That channel is READ-ONLY and "
            "its judge is the same one used here -- MEASURED live (§17.1390): a check carrying "
            "`python3 -c` to read a key was refused at run time, dropped, and the step went `done` "
            "with an empty verify. Write a check the read channel takes: `pct exec <gid> -- curl -s "
            "<url>`, `pct exec <gid> -- find <path> -type f`, `pct exec <gid> -- cat <file>` -- a "
            "plain read, with no interpreter and no command substitution in it.")})
    return out


def a_check_that_proves_nothing(verify: list[str]) -> list[dict]:
    r"""§17.1382 — a check whose answer cannot depend on the step.

    Live, 2026-10-06: ADD134's frame carried four checks and the second was the
    bare word

        id

    -- a real command, so every shape gate passed it and it exits 0 on any
    machine at any time. §17.1345 requires a check to EXIST and §17.1362 that an
    API change be read back; nothing asked whether a check could answer
    differently before and after the work. A step recorded done on checks like
    this is §17.1343's defect with a check in front of it.

    Only the commands whose whole output is a property of the machine are named,
    and only when the check is JUST that command: `id -u jellyfin` reads
    something about the step's subject, and `echo "$(curl …)"` is a read.
    """
    out: list[dict] = []
    for v in verify or []:
        words = str(v or "").strip().split()
        if not words or len(words) > 1:
            continue
        if words[0] not in _PROVES_NOTHING:
            continue
        out.append({"command": str(v), "why": (
            f"`{words[0]}` answers the same thing on every machine, before this step and after it, so "
            f"it cannot witness the work -- and a step is recorded done from its checks. MEASURED live "
            f"(§17.1382): this arrived as a whole verify entry, and because it is a real command that "
            f"exits 0, every other gate passed it. Name a command that READS what the step is supposed "
            f"to leave: the value back out of the API, the file at its path, the title the library now "
            f"lists.")})
    return out


#: §17.1382 — `"tmdbId": 0` in a body being POSTed. The quotes may be ESCAPED:
#: live, the body reached the gate as `-d "{\"tmdbId\":0}"`, and the first cut of
#: this pattern required a bare quote and saw nothing (§17.1048 — a gate matches
#: modulo formatting, or it does not match the thing that happens).
_ZERO_ID_RE = re.compile(r"""\\?['"]((?:[a-z][a-z0-9]*)?[Ii]d)\\?['"]\s*:\s*0(?![0-9.])""")


#: §17.1405 — a JSON-encoded value interpolated into a shell command
_JSON_NAME_RE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*JSON\.stringify\(")
_SHELLISH_RE = re.compile(r"\b(?:printf|echo|ssh|sh\s+-c|bash\s+-c|tee)\b|>\s*\S")
#: §17.1425 — the shell words alone, and an HTML tag: on a line of markup, a bare `>` closes a tag. Live,
#: ADD126's page showed an answer as `innerHTML = `<div class="result">${JSON.stringify(data)}</div>`` -- no
#: shell anywhere -- and §17.1405 refused all eight rounds for the tag's `>`.
_SHELL_WORD_RE = re.compile(r"\b(?:printf|echo|ssh|sh\s+-c|bash\s+-c|tee|execSync|spawnSync)\b")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?>")


def _shellish(line: str) -> bool:
    if _SHELL_WORD_RE.search(line):
        return True
    return bool(_SHELLISH_RE.search(line)) and not _HTML_TAG_RE.search(line)


def json_used_as_shell_quoting(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1405 — JSON.stringify is not shell quoting.

    Live, 2026-10-06, ADD122's draft sent the Palworld settings file to VM 106 as
    `printf '%s' ${JSON.stringify(content)} > …` inside an `execSync` ssh command.
    JSON writes a newline as the two characters `\n`, which `printf '%s'` prints
    literally: the `[/Script/Pal.PalGameWorldSettings]` header and the
    `OptionSettings=…` line become one corrupt line. And JSON's double quotes let
    the remote shell expand `$` and backticks inside a value -- a password, say.
    Content belongs on stdin, where nothing re-parses it.
    """
    texts = [str((f or {}).get("content") or "") for f in files or []] + [str(c) for c in commands or []]
    out: list[dict] = []
    for t in texts:
        if "JSON.stringify" not in t:
            continue
        names = set(_JSON_NAME_RE.findall(t))
        for line in t.split("\n"):
            if line.lstrip().startswith("//") or not _shellish(line):
                continue
            if "${JSON.stringify(" in line or any("${" + n + "}" in line for n in names):
                out.append({"command": line.strip()[:200], "why": (
                    "a JSON-encoded value is spliced into a shell command here, and JSON is not shell quoting: "
                    "it writes a newline as the two characters `\\n`, which `printf '%s'`/`echo` write literally "
                    "(a multi-line file becomes one line), and its double quotes let the shell expand `$` and "
                    "backticks inside the value. Pass the content on STDIN, where nothing re-parses it -- "
                    "`execFile('ssh', [target, 'sudo tee <path> >/dev/null'], { input: content })` in Node, "
                    "`subprocess.run([...], input=content)` in Python.")})
                break
    return out


#: §17.1403 — a guest exec and what runs after its `--`
_GUEST_RUN_RE = re.compile(r"\b(?P<tool>pct\s+exec|qm\s+guest\s+exec)\s+(?P<gid>\d{3,5})\b[^\n]*?\s--\s(?P<rest>[^\n]*)")
#: the Proxmox host's own tools: they do not exist inside a guest
_HOST_TOOL_RE = re.compile(r"(?:^|[;&|(\s'\"])(?P<tool>qm|pct|pvesm|pvesh|pveam|vzdump)\s+[a-z]")


def _guest_part(rest: str) -> str:
    """§17.1669c — the part of `pct exec N -- …` that runs IN the guest: up to the first `;`, `&`, `|` outside quotes.
    Live (job 3cc24343 T7): `if pct exec 109 -- test -L /etc/resolv.conf; then ns=$(pct config 109 …)` was refused
    because the host's own `pct config`, after the `;`, was read as running inside guest 109."""
    q, k = "", 0
    while k < len(rest):
        c = rest[k]
        if c == "\\" and q != "'":
            k += 2
            continue
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c in ";&|":
            return rest[:k]
        k += 1
    return rest


def a_host_file_run_inside_a_guest(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1403 — the engine writes a block's files on the HOST; inside a guest they are not there.

    Live, 2026-10-06, ADD122's draft (after §17.1402 told it where Palworld lives):
    the block wrote `/tmp/add_palworld_settings.sh` and ran it as
    `pct exec 111 -- bash /tmp/add_palworld_settings.sh`. The runner writes files
    on the Proxmox host, so inside container 111 that path does not exist; and the
    script itself called `qm guest exec 106 …`, a tool only the host has. The
    engine's own templates run their script ON THE HOST and reach into the guest
    from there (`run_in_container`, `run_in_vm_via_agent`).
    """
    paths = [str((f or {}).get("path") or "") for f in files or [] if (f or {}).get("path")]
    content = {str((f or {}).get("path") or ""): str((f or {}).get("content") or "") for f in files or []}
    lines = [ln for c in commands or [] for ln in str(c).split("\n") if not ln.lstrip().startswith("#")]
    out: list[dict] = []
    for i, ln in enumerate(lines):
        for m in _GUEST_RUN_RE.finditer(ln):
            gid, rest, tool = m.group("gid"), _guest_part(m.group("rest")), m.group("tool")
            for p in paths:
                if not re.search(rf"(?<![\w./-]){re.escape(p)}(?![\w./-])", rest):
                    continue
                pushed = any(re.search(rf"\bpct\s+push\s+{gid}\s+\S*{re.escape(p)}", x) for x in lines[:i] + [ln])
                if pushed:
                    continue
                out.append({"command": ln.strip()[:200], "why": (
                    f"`{p}` is a file this block writes, and the engine writes a block's files on the Proxmox "
                    f"HOST -- inside guest {gid} that path does not exist, so `{tool.split()[0]} … -- {rest.strip()[:60]}` "
                    f"runs nothing. Run the script on the host and reach into the guest from there, line by line "
                    f"(`pct exec {gid} -- …` / `qm guest exec {gid} -- …`), the way the engine's own templates do"
                    + (f" -- or `pct push {gid} {p} {p}` first" if tool.startswith("pct") else "")
                    + ".")})
                body = content.get(p, "")
                hit = next((h for h in (_HOST_TOOL_RE.search(bl) for bl in body.split("\n")
                                         if not bl.lstrip().startswith("#")) if h), None)
                if hit:
                    out.append({"command": ln.strip()[:200], "why": (
                        f"and `{p}` calls `{hit.group('tool')}`, a Proxmox HOST tool, while running inside guest "
                        f"{gid}: there is no `{hit.group('tool')}` in a guest. A step that needs a second machine "
                        f"is run from the host, which can reach both.")})
                break
            # a host tool inside an inline guest payload
            hit = _HOST_TOOL_RE.search(" " + rest)
            if hit and not any(p in rest for p in paths):
                out.append({"command": ln.strip()[:200], "why": (
                    f"`{hit.group('tool')}` runs inside guest {gid} here, and it is a Proxmox HOST tool: there is "
                    f"no `{hit.group('tool')}` in a guest. Run it on the host, outside the `{tool.split()[0]} … --`.")})
    return out


#: §17.1400 — text tools that cannot read the agent's JSON answer
_TEXT_TOOL_RE = re.compile(r"^\s*(?:sed|grep|egrep|awk|cut|tr|head|tail|wc)\b")


def the_agents_json_read_as_text(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1400 — `qm guest exec`'s answer is JSON; a text tool reading it reads the wrapper.

    Live, 2026-10-06, ADD137 (the draft that finally pinned VM 106's real host
    key, as §17.1399 asked) read it with

        qm guest exec 106 -- cat /etc/ssh/ssh_host_ed25519_key.pub |
            sed -n 's/.*"out-data":"\([^"]*\)".*/\1/p'

    The agent answers PRETTY-PRINTED JSON -- `"out-data" : "ssh-ed25519 …\n"`,
    spaces around the colon, the newline escaped -- as the same step's previous
    run printed (`"exitcode" : 0`). The pattern matches nothing, the key comes back
    empty, and the step stops. The run before it did the same with `| grep -o
    '[0-9]*'` and reported "0 1 4 lines". §17.1319 is this defect in the engine's
    own template; the template has parsed with `json` ever since.
    """
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    out: list[dict] = []
    for t in texts:
        for line in t.split("\n"):
            if line.lstrip().startswith("#") or not _QGE_RE.search(line):
                continue
            # blank quoted DATA (same length, so positions hold) -- but a double-quoted
            # `"$( … )"` is code: live, the key read sat inside `HOSTKEY="$(qm guest exec … | sed …)"`
            bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"",
                          lambda m: m.group(0) if "$(" in m.group(0)
                          else m.group(0)[0] + "x" * (len(m.group(0)) - 2) + m.group(0)[-1], line)
            m = _QGE_RE.search(bare)
            if not m:
                continue
            # the stages AFTER this invocation, split on top-level pipes only
            stages = re.split(r"(?<!\|)\|(?!\|)", bare[m.end():])[1:]
            if not stages:
                continue
            if _TEXT_TOOL_RE.match(stages[0]) and not re.search(r"\b(?:python3?|jq)\b", stages[0]):
                out.append({"command": line.strip()[:200], "why": (
                    "`qm guest exec` answers JSON, and this hands that JSON to a text tool: the agent "
                    "pretty-prints it -- `\"out-data\" : \"…\\n\"`, spaces around the colon, newlines "
                    "escaped -- so a pattern written for the raw text reads the wrapper and finds nothing "
                    "(or the digits of `exitcode`). Parse it: `| python3 -c 'import json,sys; "
                    "sys.stdout.write(json.load(sys.stdin).get(\"out-data\") or \"\")'` -- the engine's own "
                    "guest-agent template does -- then filter the text that comes out.")})
                break
    return out


#: §17.1399 — a known_hosts entry removed: `ssh-keygen -R`, a sed/grep -v rewrite, an rm or truncate
_FORGETS_HOST_KEY_RE = re.compile(
    r"ssh-keygen\b[^\n|;&]*\s-R\b|\bsed\b[^\n]*-i[^\n]*known_hosts|\brm\b[^\n]*known_hosts|"
    r"(?<![<>])>(?!>)\s*\S*known_hosts\b|\bgrep\s+-v\b[^\n]*known_hosts", re.I)
#: the target's own host key, read through a channel that is not the network being doubted
_READS_HOST_KEY_RE = re.compile(r"\b(?:qm\s+guest\s+exec|pct\s+exec)\b[^\n]*ssh_host_\w*key\.pub")


def a_host_key_forgotten_not_pinned(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    """§17.1399 — a block that removes a known_hosts entry must pin the real key, not trust the next answer.

    Live, 2026-10-06, ADD137: the panel's ssh to VM 106 failed with `REMOTE HOST
    IDENTIFICATION HAS CHANGED` (§17.1393 judged it). The redraft that learned
    from that failure ran `ssh-keygen -R 192.168.1.106`, then
    `ssh -o StrictHostKeyChecking=accept-new` -- deleting the warning and
    trusting whatever answers at that address next. That is the reflex a
    man-in-the-middle relies on, and the engine had no need of it: VM 106 is a
    guest on this host, and its guest agent is a channel that is not the network
    being doubted. Reading `/etc/ssh/ssh_host_ed25519_key.pub` through it and
    writing exactly that key is deterministic AND safe.
    """
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    lines = [ln for t in texts for ln in t.split("\n") if not ln.lstrip().startswith("#")]
    forgets = next((ln for ln in lines if _FORGETS_HOST_KEY_RE.search(ln)), None)
    if not forgets or any(_READS_HOST_KEY_RE.search(ln) for ln in lines):
        return []
    return [{"command": forgets.strip()[:200], "why": (
        "this forgets a host key and pins nothing in its place, so the next ssh trusts whatever answers at "
        "that address -- `StrictHostKeyChecking=accept-new` records a stranger as gladly as the real "
        "machine. A changed key is exactly what a man-in-the-middle looks like, and deleting the warning is "
        "the reflex it relies on. The engine does not need to guess: when the target is a guest on this "
        "host, read its key through the guest's own channel, which is not the network being doubted -- "
        "`qm guest exec <id> -- cat /etc/ssh/ssh_host_ed25519_key.pub | python3 -c 'import json,sys; "
        "sys.stdout.write(json.load(sys.stdin).get(\"out-data\") or \"\")'` (the agent answers "
        "pretty-printed JSON, so parse it -- never sed it) or `pct exec <id> -- cat /etc/ssh/ssh_host_ed25519_key.pub` -- then replace the old "
        "line with `<address> <that key>` in the client's known_hosts, and ssh with "
        "`StrictHostKeyChecking=yes`.")}]


#: §17.1397 — `qm guest exec` given input: piped into, redirected from, or a heredoc
_QGE_RE = re.compile(r"\bqm\s+guest\s+exec\b")
_PASS_STDIN_RE = re.compile(r"--pass-stdin(?:\s+|=)(?:1|true|yes|on)\b")


def a_pipe_the_guest_agent_never_reads(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1397 — input fed to `qm guest exec` without `--pass-stdin 1` never arrives.

    Live, 2026-10-06, ADD137's redraft installed the panel's key on VM 106 with

        pct exec 111 -- sh -c 'cat /root/.ssh/id_ed25519.pub' |
            qm guest exec 106 -- sh -c 'cat >> /home/aedefruscio/.ssh/authorized_keys'

    `qm guest exec` reads STDIN only with `--pass-stdin 1` (default 0): the guest's
    `cat` gets EOF, appends nothing, and the line exits 0. The engine's own
    `run_in_vm_via_agent` template has always written `--pass-stdin 1`; nothing
    held a drafted block to the same rule.
    """
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    out: list[dict] = []
    for t in texts:
        for line in t.split("\n"):
            if line.lstrip().startswith("#"):
                continue
            for m in _QGE_RE.finditer(line):
                # where this `qm guest exec` invocation ends: the next pipe or list operator after it
                rest = line[m.start():]
                end = re.search(r"\s(?:\|\|?|&&|;)\s", rest)
                own = rest[:end.start()] if end else rest
                before = line[:m.start()]
                piped = bool(re.search(r"(?<!\|)\|\s*$", before))
                # a `<` / `<<` that belongs to THIS invocation, not one quoted inside its guest command
                bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", own)
                redirected = bool(re.search(r"(?<![<\d])<(?!\()", bare))
                if not (piped or redirected) or _PASS_STDIN_RE.search(own):
                    continue
                out.append({"command": line.strip()[:200], "why": (
                    "`qm guest exec` is fed input here without `--pass-stdin 1`, so the input never arrives: "
                    "the agent forwards STDIN only when told to (the default is 0), the command inside the "
                    "guest reads EOF, and the line exits 0 having done nothing. Write `qm guest exec <id> "
                    "--pass-stdin 1 -- …` -- the engine's own guest-agent template does -- or put the value in "
                    "the command itself.")})
                break
    return out


def an_id_sent_as_zero(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1382 — an identifying field sent as 0, which identifies nothing.

    Live, 2026-10-06: ADD134's draft added a film to Radarr with

        -d '{"title":"<TITLE>", …, "tmdbId":0, "year":0, "images":[], "tags":[]}'

    and asked the OPERATOR to type `TITLE_SLUG`. Both values -- the TMDB id and
    the slug -- are what `GET /api/v3/movie/lookup?term=…` returns; Radarr
    rejects a create with `tmdbId: 0`, so the step could not have worked. This is
    the §17.1332 lesson one layer further out: a value another CALL holds is no
    more an operator's to type than one a file holds, and a literal zero in its
    place is the shape that says the drafter had nowhere to get it.

    Only a body being SENT is judged (a `-d` payload or a file the block writes),
    never a response being read, and only a field whose name ends in `id`.
    """
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        if not re.search(r"-d\s|--data|json\.dump|json\.dumps", t):
            continue
        for m in _ZERO_ID_RE.finditer(t):
            field = m.group(1)
            out.append({"command": t[:120], "why": (
                f"`{field}` is sent as 0, and 0 identifies nothing -- the service rejects a create "
                f"whose id is zero. MEASURED live (§17.1382): a draft added a film to Radarr with "
                f"`\"tmdbId\":0` while asking the operator to type the title slug, and both of those "
                f"are fields `GET /api/v3/movie/lookup?term=<TITLE>` returns. Call the lookup first, "
                f"take `{field}` (and the slug, the year, the images) from its answer, and send that "
                f"object. A value another call holds is not a value to invent or to ask for.")})
    return out


def a_call_its_own_function_cannot_accept(
        commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1379 — a call the program's OWN function rejects at call time.

    Live, 2026-10-05. §17.1378's rules worked: ADD132's next draft set the
    password inside the `fields` entry and POSTed the whole object to `/test`.
    Then it did this:

        def pct_exec(ctid, cmd):
            return run(["pct", "exec", str(ctid), "--", "sh", "-c", cmd])
        …
        def api_put(ctid, port, key, path, body):
            r = pct_exec(ctid, "curl … -d @- …", input=body)

    `pct_exec` takes two arguments. Measured by running it:

        TypeError: pct_exec() got an unexpected keyword argument 'input'

    at BOTH call sites, so the block dies at the first Radarr PUT having changed
    nothing. Only the program's own functions are judged -- an imported or builtin
    callable has a signature this gate does not know -- and a `**kwargs` or a
    `*args` in the definition, or a `*`/`**` at the call, makes it say nothing.
    """
    out: list[dict] = []
    for label, src in _inline_programs(commands, files):
        for name, detail, line, kind in _calls_the_signature_rejects(src):
            out.append({"command": f"{label}: line {line}, {name}(…)", "why": (
                (f"`{name}()` is called with a keyword argument `{detail}` that its own "
                 f"definition in this program does not accept"
                 if kind == "keyword" else
                 f"`{name}()` is called with {detail} positional arguments and its own "
                 f"definition in this program takes fewer")
                + ", so Python raises TypeError the moment that line runs and the block "
                  "stops there having changed nothing. MEASURED live (§17.1379) on exactly "
                  "this shape: `def pct_exec(ctid, cmd)` called as "
                  "`pct_exec(ctid, cmd, input=body)` gave `TypeError: pct_exec() got an "
                  "unexpected keyword argument 'input'` at both call sites, after the draft "
                  "had everything else right. Either give the function the parameter and "
                  "pass it on to `subprocess.run`, or stop passing it.")})
    return out


def _calls_the_signature_rejects(src: str) -> list[tuple]:
    """§17.1379 — (name, detail, line, kind) for each call a local def rejects."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    sigs: dict = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = n.args
        if n.name in sigs:
            sigs[n.name] = None                # redefined: do not judge
            continue
        sigs[n.name] = {
            "pos": [x.arg for x in list(getattr(a, "posonlyargs", [])) + list(a.args)],
            "kwonly": [x.arg for x in a.kwonlyargs],
            "vararg": a.vararg is not None,
            "kwarg": a.kwarg is not None,
        }
    out: list[tuple] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
            continue
        sig = sigs.get(n.func.id)
        if not sig:
            continue
        if any(isinstance(x, ast.Starred) for x in n.args) or \
                any(k.arg is None for k in n.keywords):
            continue                           # splatted: the shape is unknowable
        for kw in n.keywords:
            if not sig["kwarg"] and kw.arg not in sig["pos"] + sig["kwonly"]:
                out.append((n.func.id, kw.arg, n.lineno, "keyword"))
        if not sig["vararg"] and len(n.args) > len(sig["pos"]):
            out.append((n.func.id, str(len(n.args)), n.lineno, "positional"))
    return out


def a_program_that_uses_a_name_it_never_defines(commands: list[str],
                                                files: list[dict] | None) -> list[dict]:
    r"""§17.1377 — an inline Python program referencing a name it never imports.

    Live, 2026-10-05: ADD132's accepted draft wrote the Radarr body with

        python3 - <<'EOF'
        import json
        …
            {"name": "password", "value": os.environ['MASS_PASSWORD']},
        EOF

    -- `os` imported nowhere. The Sonarr block three lines later says
    `import json, os`, so the draft knew the import and dropped it once. Under
    `set -euo pipefail` the NameError ends the run at that step, having changed
    nothing, and every other gate passed the block.

    Only names that are decidably absent are named: the program is parsed, and
    builtins, imports, assignments, defs, comprehension and `with`/`for`/`except`
    targets, and arguments all count as defined.
    """
    out: list[dict] = []
    # §17.1379 — a program is a program wherever it lives: a heredoc, or a `.py`
    # file in the channel, which is the shape FILE_RULES recommends.
    for label, body in _inline_programs(commands, files):
            missing = _undefined_names(body)
            if not missing:
                continue
            first = body.strip().split("\n")[0][:60]
            out.append({"command": f"{label} … ({first} …)",
                        "why": (
                f"this inline Python program uses {', '.join('`' + n + '`' for n in missing)} "
                f"and never imports or assigns {'them' if len(missing) > 1 else 'it'}, so it "
                f"raises NameError the moment it runs -- and under `set -e` that ends the "
                f"block having changed nothing. Live (§17.1377) this was `os.environ[...]` "
                f"in a program whose only import was `json`, while the next program in the "
                f"same block had `import json, os`. Add the import.")})
    return out


def _undefined_names(src: str) -> list[str]:
    """§17.1377 — module-level Load names with nothing binding them. [] if unparseable."""
    import ast
    import builtins
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []                              # not our judgment to make
    bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in n.names}
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            for a in list(getattr(n, "args", None) and n.args.args or []) + \
                    list(getattr(n, "args", None) and n.args.kwonlyargs or []):
                bound.add(a.arg)
            for a in (getattr(getattr(n, "args", None), "vararg", None),
                      getattr(getattr(n, "args", None), "kwarg", None)):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound |= set(n.names)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - bound)


def empty_checks(pasted: str) -> set:
    """The check ids (`V1`, `V2`…) whose section in the paste has NO output.

    §17.1372 — a check that printed nothing is AMBIGUOUS, and the judge was
    treating it as evidence against the step. Live, 2026-10-05, ADD135 set the
    boot flags on all three guests and was recorded FAILED:

        every command exited 0, but this step's own verify checks say the work did
        not land: no output — neither onboot nor startup found in 100.conf or 110.conf

    That fourth check exists to confirm VMs 100 and 110 were LEFT ALONE — the
    step's own text says *"Leave VM 100 gpu-vm and VM 110 ai-vm alone"* — so
    finding nothing is the correct answer. Measured on the machine straight after:
    `onboot: 1` and `startup: order=10/20/30` on 106, 111 and 120, and nothing on
    100 or 110. The work had landed exactly as drafted.
    """
    out: set = set()
    cur, buf = "", []
    for ln in str(pasted or "").split("\n"):
        m = _MARKER_LINE_RE.fullmatch(ln)
        if m:
            if cur and not "".join(buf).strip():
                out.add(f"V{cur}")
            cur, buf = m.group(1), []
            continue
        buf.append(ln)
    if cur and not "".join(buf).strip():
        out.add(f"V{cur}")
    return out


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


#: §17.1391 — a credential carried in the clear by the thing that asks for it:
#: `X-Api-Key: <token>`, `X-Emby-Token: <token>`, `?api_key=<token>`,
#: `Authorization: Bearer <token>`. The header NAME is kept so a reader can see
#: what was sent; the value never survives.
_CREDENTIAL_LITERAL_RE = re.compile(
    r"((?:X-(?:Api-Key|Emby-Token|MediaBrowser-Token|Plex-Token)|Authorization)\s*:\s*"
    r"(?:Bearer\s+)?|(?:api_?key|access_?token)\s*=\s*"
    r"|[\"']?\w*api_?key[\"']?\s*:\s*[\"'])"              # §17.1583 — `"sonarrApiKey": "<hex>"` in a JSON/JS file
    r"(?!\$)([A-Za-z0-9_-]{16,128})", re.I)


def mask_credential_literals(text_out: str) -> str:
    """§17.1391 — mask a credential that a command carried in the clear.

    Live, 2026-10-06. §17.1385's read PRINTS the key it finds, §17.1366 carries a
    previous attempt's output into the next draft's context, and the drafter then
    wrote the literal into its own check. Measured afterwards: the Jellyfin key
    reached **4 model requests and 2 model responses**, and one `dag_nodes`
    record held it in the clear. §17.1191's contract -- a secret is resolved by
    the runner or not at all, and never travels through the engine -- was broken
    by the engine's own key-reading capability, which `scrub_run_output` had no
    way to know about: it masks values the engine SENT, and this one was read off
    the machine.

    Shape, not a list of known values, because the engine cannot enumerate the
    credentials its machines hold. A `$(…)` read is left alone: that is the form
    the engine WANTS in a record.
    """
    return _CREDENTIAL_LITERAL_RE.sub(lambda m: m.group(1) + "***", str(text_out or ""))


def scrub_run_output(text_out: str, held: Optional[dict[str, str]]) -> str:
    """§17.1281 — what a run printed, with every engine-held value and every
    secret-shaped token masked before it is stored, returned or logged. The
    runner redacts values from ITS store and the values the engine SENT; a value
    the engine holds but did not send on this call (a read-only probe that `cat`s
    a config file) came back in clear."""
    out = str(text_out or "")
    for v in sorted((v for v in (held or {}).values() if v and len(v) >= 6), key=len, reverse=True):
        out = out.replace(v, "***")
    # §17.1391 — and a credential the engine never SENT but a command printed or
    # carried: the engine reads service keys off the machine now, so the value
    # can be one no store here has ever seen.
    out = mask_credential_literals(out)
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


#: §17.1365 — a config KEY the block writes: `Key=value` appended or substituted,
#: in a file or through a tool. The name is what a check would have to read back.
_WRITES_KEY_RE = re.compile(
    r"(?:^|[\s'\"(;&|>])(?P<key>[A-Za-z][\w.\\-]{2,60}?)\s*=\s*(?=[^\s=])"
    r"|s[|/#,@:!~](?:\^)?(?P<skey>[A-Za-z][\w.\\-]{2,60}?)\s*=")
#: §17.1365 — a line that MODIFIES AN EXISTING file: an append (`>>`, `tee -a`),
#: an in-place `sed`, or a sed append/insert/change. A WHOLE-file write is
#: deliberately not here: a block that creates a unit file and checks
#: `systemctl is-active <unit>` has a perfectly good witness -- the service cannot
#: run unless the file was written. Measured, that distinction is the difference
#: between flagging ADD132 alone and flagging T23, T24 and ADD114 as well, each of
#: which writes `Description=`/`After=`/`Type=` into a NEW unit and proves it with
#: `is-active`. The keys that need reading back are the ones appended or
#: substituted into a config a running service already had. `/dev/null` is never a
#: file anyone reads back.
_WRITES_TO_A_FILE_RE = re.compile(
    r">>\s*[\"']?(?!/dev/)(?:/|\$)|(?<![\w-])tee\s+-a\s+(?!/dev/)|"
    r"sed\s+(?:-\S+\s+)*-i|[|/#](?:\^)?\[?[A-Za-z][^|/#\n]{0,60}\]?[|/#]\s*[aic]\s", re.I)
#: names that are shell plumbing, not a setting a check could read
_NOT_A_SETTING = frozenset({
    "if", "then", "else", "fi", "for", "do", "done", "while", "case", "esac",
    "set", "export", "local", "readonly", "echo", "printf", "true", "false",
    "DEBIAN_FRONTEND", "PATH", "HOME", "LANG", "LC_ALL", "IFS", "PS1", "TERM",
    "GID", "UNIT", "CONF", "HASH", "RESULT", "LOGIN_RESULT", "deadline", "state",
    "result", "code", "i", "f", "name", "value", "id", "key", "salt",
    # §17.1365 — systemd-run's own options and ssh's, which the engine's templates
    # write on every detached step
    "StandardOutput", "StandardError", "SSHPASS", "StrictHostKeyChecking",
    "BatchMode", "ConnectTimeout", "UserKnownHostsFile", "LogLevel", "START",
    "mac", "order"})


#: §17.1370 — a field the block SENDS to an API: `"username": "admin"`,
#: `\"password\":\"$MASS_PASSWORD\"`, escaped through any number of quoting
#: layers. A value changed through an API is as written as one put in a file, and
#: §17.1365's question — can a check witness it? — is the same question.
_SENT_FIELD_RE = re.compile(r"\\{0,2}[\"'](?P<field>[A-Za-z][A-Za-z0-9_]{2,40})\\{0,2}[\"']\s*:\s*"
                            r"(?=\\{0,2}[\"']|\$|\d|true|false)")
#: §17.1373 — `{"name":"username","value":"admin"}`: the *arr `fields` array,
#: where the setting's name is a VALUE. Escaped through any quoting depth.
_NAMED_FIELD_RE = re.compile(
    r'\\{0,2}["\']name\\{0,2}["\']\s*:\s*\\{0,2}["\'](?P<field>[A-Za-z][A-Za-z0-9_]{2,40})\\{0,2}["\']'
    r'\s*,\s*\\{0,2}["\']value\\{0,2}["\']\s*:')
#: the fields every *arr object carries as structure, which name no setting
_NOT_A_SENT_SETTING = frozenset({
    "name", "implementation", "implementationName", "configContract", "protocol",
    "fields", "value", "label", "type", "order", "advanced", "privacy", "hint",
    "helpText", "id", "tags", "presets", "selectOptions", "isFloat", "unit",
    "errorMessage", "propertyName", "severity", "errorCode", "message"})


#: §17.1378 — the *arr family keeps a download client's settings in a `fields`
#: array, not as keys of the object, and masks secrets on read.
_VERSIONED_API_RE = re.compile(r"/api/v\d+/")
_CREDENTIAL_KEYS = ("password", "username", "apikey", "api_key", "token", "secret")
#: `c["password"] = …`, `obj['username'] = …`, `client.get("password")` is not one.
_TOP_LEVEL_SET_RE = re.compile(
    r"(?P<var>[A-Za-z_]\w*)\s*\[\s*[\"'](?P<key>\w+)[\"']\s*\]\s*=(?!=)")
#: the correct place: `{"name": "password", "value": …}` or `f["name"] == "password"`.
_FIELD_ENTRY_RE = re.compile(
    r"[\"']name[\"']\s*[:=]=?\s*[\"'](?P<field>\w+)[\"']")


def a_credential_set_where_the_api_does_not_keep_it(
        commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1378 — a secret assigned to a key the API drops, and a mask written back.

    Live, 2026-10-05. ADD132 finally RAN: the body reached the guest, every heredoc
    imported what it used, and the block exited 22 only on its own malformed test
    call. Then the machines were read:

        fields.username : 'admin'
        fields.password : MASKED - the literal asterisks
        top-level username/password the draft set : ABSENT

    The draft did this:

        c = json.load(sys.stdin)[0]        # GET -- password comes back "********"
        c["username"] = "admin"            # DROPPED: the API keeps settings in `fields`
        c["password"] = os.environ[...]     # DROPPED
        … PUT c back                        # STORES THE MASK as the password

    So `downloadclient/test` answered `Authentication Failure` on Username, and the
    stored password was left WORSE than before the run -- the asterisks, not the old
    value. §17.1370 already measured that these settings live in the `fields` array
    as `{"name":"username","value":"admin"}`; this is a block round-tripping an
    object from such an API and setting the credential on the object itself.
    """
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        if not _VERSIONED_API_RE.search(t):
            continue                       # not one of these APIs
        if not re.search(r"(?:-X\s*(?:PUT|POST)|method\s*=\s*[\"'](?:PUT|POST))", t):
            continue                       # nothing is sent back
        in_fields = {m.group("field").lower() for m in _FIELD_ENTRY_RE.finditer(t)}
        seen: set = set()
        for ln in logical_lines(t):
            if ln.strip().startswith("#"):
                continue
            for m in _TOP_LEVEL_SET_RE.finditer(ln):
                key = m.group("key")
                low = key.lower()
                if low not in _CREDENTIAL_KEYS or low in in_fields or low in seen:
                    continue
                seen.add(low)
                out.append({"command": ln.strip()[:200], "why": (
                    f"`{m.group('var')}[\"{key}\"] = …` sets `{key}` as a key of the object, "
                    f"and these APIs keep a setting in the object's `fields` array -- so the "
                    f"API DROPS it and the value that gets stored is whatever the GET "
                    f"returned. MEASURED on this host (§17.1378) after exactly this block "
                    f"ran: `fields.username` was still 'admin', the top-level `username` and "
                    f"`password` the draft set came back ABSENT, and `fields.password` held "
                    f"the literal asterisks the GET had masked it with -- so "
                    f"`downloadclient/test` answered `Authentication Failure` and the stored "
                    f"password was left worse than before. Set it where the API keeps it: "
                    f"`for f in obj[\"fields\"]:` / `if f[\"name\"] == \"{key}\": "
                    f"f[\"value\"] = …`, adding the entry when it is absent. Never PUT back a "
                    f"secret field you only read -- a read returns it masked.")})
    return out


#: §17.1378b — `…/api/v3/downloadclient/test`, `…/notification/test`: endpoints that
#: VALIDATE the whole resource rather than accepting a bare trigger.
_VALIDATING_TEST_RE = re.compile(r"/api/v\d+/\w+/test\b")
#: §17.1378b — only a curl-shaped call can be judged: a body passed as a function
#: ARGUMENT (`api(ct, key, port, 'POST', path, client)`) is invisible here, and
#: `-d @-` / `-d @/path` take the body from stdin or a file. Measured: treating
#: "no -d flag" as "no body" produced 158 flags, nearly all of them false.
_DATA_FLAG_RE = re.compile(
    r"(?<![\w-])(?:-d|--data|--data-raw|--data-binary|--json)(?![\w-])")


def _body_carries_nothing(line: str) -> bool:
    """§17.1378b — the inline body is `{}`, empty, or only an id."""
    if not _DATA_FLAG_RE.search(line):
        return False                       # not a shape this gate can read
    m = _BODY_SPAN_RE.search(line)
    if not m:
        return False
    body = (m.group("body") or "").strip().strip("'\"").strip()
    if body.startswith("@"):
        return False                       # stdin or a file -- §17.1377's shape
    if body in ("", "{}", "''", '""'):
        return True
    try:
        obj = json.loads(body)
    except Exception:
        return False                       # unparseable: not our judgment
    return isinstance(obj, dict) and set(k.lower() for k in obj) <= {"id"}


#: §17.1501 — *arr resources that expose a `/{resource}/testall` beside `/{resource}/test`
#: (the UI's "Test All"): it tests every CONFIGURED one and needs no request body. The
#: resources a media-stack connectivity step actually tests. A resource NOT here is left
#: for the §17.1378b refusal, because inventing a `/testall` it may not have is worse.
_TESTALL_RESOURCES = frozenset({
    "downloadclient", "indexer", "notification", "importlist", "application", "applications",
    "indexerproxy",
})
_TEST_ENDPOINT_RE = re.compile(r"(/api/v\d+/(\w+))/test\b")
#: the `-d/--data…` flag together with its one quoted-or-bare argument, to delete.
_DATA_FLAG_WITH_ARG_RE = re.compile(
    r"\s*(?:-d|--data|--data-raw|--data-binary|--json)(?![\w-])\s*(?:'[^']*'|\"[^\"]*\"|\S+)")


def repair_empty_body_test_call(commands: list[str], verify: list[str]) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1501 — rewrite an empty-body POST to an *arr `/{resource}/test` into
    `/{resource}/testall` with the body removed.

    Live (job c75d77fa T3v2, 2026-10-12): the "Verify testable" step drafted
    `curl … -X POST … -d '{}' …/api/v3/downloadclient/test` four times. A `/test`
    endpoint validates the whole object, so an empty body fails on the resource's
    required fields (`'Name' must not be empty`, …) — §17.1378b refuses it and
    tells the drafter to GET the object and POST it. For a connectivity CHECK the
    simpler, exactly-right form is `/{resource}/testall`: it tests every configured
    client/indexer and takes no body, which is what the arr UI's "Test All" calls.
    So the engine repairs the drafter's own mistake instead of parking the step.

    Only resources known to carry `/testall` are repaired; anything else is left to
    the §17.1378b refusal, because inventing an endpoint is worse than refusing."""
    repaired: set[str] = set()

    def fix(seq: list[str]) -> tuple[list[str], int]:
        out, n = [], 0
        for c in seq or []:
            line = str(c)
            m = _TEST_ENDPOINT_RE.search(line)
            if not m or m.group(2).lower() not in _TESTALL_RESOURCES or not _body_carries_nothing(line):
                out.append(c)
                continue
            fixed = line[:m.start()] + m.group(1) + "/testall" + line[m.end():]
            fixed = _DATA_FLAG_WITH_ARG_RE.sub("", fixed)
            out.append(fixed)
            n += 1
            repaired.add(m.group(2).lower())
        return out, n

    out_cmds, nc = fix(commands)
    out_verify, nv = fix(verify)
    notes: list[dict] = []
    if nc or nv:
        notes.append({"why": (
            "a connectivity check POSTed an empty body to an *arr `/" + "`, `/".join(sorted(repaired))
            + "/test` endpoint, which validates the whole object and fails on its required fields; "
            "rewrote it to `/testall` (the UI's \"Test All\": every configured one, no body needed) so "
            "the check tests the connection instead of the empty object (§17.1501).")})
    return out_cmds, out_verify, notes


#: §17.1504 — the deterministic completeness report, run inside the *arr guest so only
#: the short summary crosses the runner channel (the full /series JSON is >20 KB — the
#: runner's silent 20 000-char cap would truncate it; the pipe keeps it in the guest).
#: Single-quoted Python strings only, so it sits inside `python3 -c "…"` unescaped.
_COMPLETENESS_PY = (
    "import sys,json,re\n"
    "d=json.load(sys.stdin)\n"
    "show=sys.argv[1].lower().strip()\n"
    "ini=lambda t: ''.join(w[0] for w in re.findall(r'[A-Za-z0-9]+', t or '')).lower()\n"
    # §17.1506 — exact title, then substring, then INITIALISM: a step titled 'Check HIMYM
    # completeness' must find 'How I Met Your Mother' in the library (its initials are himym).
    "m=([s for s in d if (s.get('title') or '').lower()==show]\n"
    "   or [s for s in d if show and show in (s.get('title') or '').lower()]\n"
    "   or [s for s in d if show and ini(s.get('title') or '')==show.replace(' ','')])\n"
    "if not m:\n"
    "    print('RESULT: NOT IN LIBRARY ('+sys.argv[1]+')'); sys.exit(0)\n"
    "s=m[0]\n"
    "print('SERIES: '+str(s.get('title') or ''))\n"
    "miss=0\n"
    "for se in sorted(s.get('seasons') or [], key=lambda x: x.get('seasonNumber') or 0):\n"
    "    if (se.get('seasonNumber') or 0)==0: continue\n"
    "    st=se.get('statistics') or {}\n"
    "    have=int(st.get('episodeFileCount') or 0); avail=int(st.get('episodeCount') or 0); mi=max(0,avail-have); miss+=mi\n"
    "    print('Season %d: have %d of %d%s' % (se.get('seasonNumber') or 0, have, avail, (' MISSING %d' % mi) if mi else ''))\n"
    "print('RESULT: COMPLETE' if miss==0 else 'RESULT: MISSING %d episode(s)' % miss)\n"
)
#: the step whose work is "is this show complete in Sonarr/Radarr, and what is missing".
#: §17.1507 — "completeness"/"complete", NOT a bare "missing episode": the latter also
#: appears in the ACTION steps ("Queue missing episodes", "Consolidate missing episodes
#: list") that this read must NOT hijack. "completion" (Monitor downloads completion) does
#: not match `complet(?:e|eness)`.
_COMPLETENESS_RE = re.compile(r"\bcomplet(?:e|eness)\b", re.I)
#: §17.1507 — an action the step DOES, not a state it READS: these steps change the
#: library (queue/add/search/…) and are the model's to draft, never a completeness read.
_COMPLETENESS_ACTION_RE = re.compile(
    r"\b(?:queue|add|download|grab|import|search|trigger|fetch|request|monitor|consolidate|set)\b", re.I)
_SEASONS_RE = re.compile(r"\b(?:season|episode)s?\b", re.I)
_SHOW_FROM_TITLE_RE = re.compile(r"\b(?:check|verify|confirm)\s+(.+?)\s+complet", re.I)
_SHOW_FROM_PROMPT_RE = re.compile(r"\b(?:query\s+\w+\s+for|for)\s+(.+?)\s*(?:[.\n]|$)", re.I)
_ARR_PORTS = {"sonarr": "8989", "radarr": "7878"}


def _sq_payload(s: str) -> str:
    """`s` as one single-quoted shell word."""
    return "'" + s.replace("'", "'\\''") + "'"


def _arr_guest(app: str, services: Optional[list], inventory: Optional[dict]) -> Optional[str]:
    """The container id running ``app`` (sonarr/radarr), from the measured services
    first, then the inventory names. None when neither names it."""
    for s in services or []:
        if app in str(getattr(s, "name", "") or "").lower() and getattr(s, "guest", ""):
            return str(getattr(s, "guest"))
    names = (inventory or {}).get("names") or {}
    hits = [str(g) for g, nm in names.items() if app in re.sub(r"[^a-z0-9]", "", str(nm or "").lower())]
    return hits[0] if len(hits) == 1 else None


def rewrite_completeness_check(node: Optional[dict], commands: list[str], verify: list[str],
                               services: Optional[list] = None,
                               inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1504 — a "check <show> completeness" step reads the *arr library and reports
    the gaps, deterministically, instead of whatever the model drafts for it.

    Live (job c75d77fa, the five show steps): "Check Big Bang Theory completeness"
    ("Query Sonarr for <show>. Compare existing episodes against all available
    seasons/episodes. List any missing episodes.") drafted anything from a catalog
    `series/lookup` (which proves nothing about the library) to an add-the-series POST
    needing an external id to a `true` check — and a weak self-check let a hollow one
    pass. The engine can do this exactly: GET the library's `/series` (with per-season
    statistics), find the show, and print `have X of Y` per season and a COMPLETE /
    MISSING N verdict. The read runs INSIDE the guest, so the >20 KB JSON never hits the
    runner's 20 000-char cap; only the short report returns. The key is read on the
    machine (§17.1503) by `<APP_API_KEY>`.

    Replaces the step's commands AND checks when the step is a completeness check and the
    *arr guest is known; otherwise leaves the draft untouched."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    title = str((node or {}).get("title") or "")
    # §17.1507 — only a CHECK, never an action step. "Queue missing episodes" matched the
    # old detector on "missing episode" and was hijacked into a read that queued nothing.
    # §17.1705 — "complete" must be what the step IS (its title), not a word it quotes: live (job e5e22b92 SC1 "Read
    # Scrubs S1 episodes, releases and packs"), the prompt listed pack titles to look for -- "'Season 1', 'Complete'" --
    # and the whole read was replaced with a library-completeness report.
    if not (_COMPLETENESS_RE.search(title) and _SEASONS_RE.search(text)) or _COMPLETENESS_ACTION_RE.search(title):
        return list(commands or []), list(verify or []), []
    app = "radarr" if re.search(r"\bradarr\b|\bmovies?\b", text, re.I) else "sonarr"
    gid = _arr_guest(app, services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    m = _SHOW_FROM_TITLE_RE.search(text) or _SHOW_FROM_PROMPT_RE.search(str((node or {}).get("prompt_template") or ""))
    show = re.sub(r'["\\]', "", (m.group(1).strip() if m else "")).strip()
    if not show:
        return list(commands or []), list(verify or []), []
    port, keyname = _ARR_PORTS[app], f"{app.upper()}_API_KEY"
    payload = (f'curl -s --fail-with-body -H "X-Api-Key: <{keyname}>" '
               f'"http://127.0.0.1:{port}/api/v3/series" | python3 -c "{_COMPLETENESS_PY}" "{show}"')
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1505 — the verify runs through the READ-ONLY channel, which refuses `python3`
    # and command substitution (§17.1390), so it cannot be the parsed report. It is a
    # plain authenticated read of the same library: `http_200` witnesses that the read
    # the RUN depends on actually answers (not a `true` — it needs this machine's {app}
    # up and a valid key), and the RUN's own output carries the per-season verdict. The
    # stored key `${keyname}` is host-expanded (run_verify injects it); no `$( … )`.
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "{app} library HTTP %{{http_code}}" -H "X-Api-Key: ${keyname}" '
             f'"http://127.0.0.1:{port}/api/v3/series"')
    note = [{"why": (
        f"drafted the completeness check from the engine's own shape (§17.1504): it reads {app}'s library "
        f"`/series` inside guest {gid} and prints each season's `have X of Y` with a COMPLETE / MISSING verdict "
        f"for \"{show}\" — the read stays in the guest so the full catalogue never hits the runner's 20 000-char "
        f"cap, and the check witnesses real episode counts, not a lookup that proves nothing.")}]
    return [run], [check], note


#: §17.1508 — queue each show's MISSING monitored episodes for download, holding any show
#: whose gap exceeds a threshold (a whole-series backfill the operator must ask for by name).
#: Single-quoted Python only; runs inside the guest; argv: KEY, THRESHOLD, [DRY].
_QUEUE_MISSING_PY = (
    "import sys,json,urllib.request\n"
    "BASE='http://127.0.0.1:8989'\n"
    "KEY=sys.argv[1]; TH=int(sys.argv[2]); DRY=len(sys.argv)>3 and sys.argv[3]=='dry'\n"
    # §17.1267 — a short timeout so the whole batch fits the 180 s command budget even if a
    # request hangs: at most 10 searches × 15 s = 150 s, well inside it.
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body else None)\n"
    "    return json.load(urllib.request.urlopen(r,timeout=15))\n"
    "def miss_of(s):\n"
    "    st=s.get('statistics') or {}\n"
    "    return (st.get('episodeCount') or 0)-(st.get('episodeFileCount') or 0)\n"
    "series=api('/api/v3/series')\n"
    # the no-network pass: say where every show stands.
    "for s in series:\n"
    "    t=s.get('title') or ''; m=miss_of(s)\n"
    "    if m<=0: print('COMPLETE: '+t)\n"
    "    elif m>TH: print('HELD: %s (%d missing, exceeds %d -- search it by name to backfill the whole show)' % (t,m,TH))\n"
    # §17.1267 — the network loop is a BOUNDED slice (at most 10 shows per run); §17.1274 — each guarded.
    "targets=[s for s in series if 0 < miss_of(s) <= TH][:10]\n"
    "for s in targets:\n"
    "    t=s.get('title') or ''; m=miss_of(s)\n"
    "    if DRY:\n"
    "        print('WOULD SEARCH: %s (%d episodes)' % (t,m)); continue\n"
    "    try:\n"
    "        api('/api/v3/command','POST',{'name':'SeriesSearch','seriesId':s.get('id')})\n"
    "        print('SEARCHING: %s (%d episodes)' % (t,m))\n"
    "    except Exception as e:\n"
    "        print('FAILED to queue %s: %r' % (t,e))\n"
)
#: the step that triggers downloads of the missing episodes.
_QUEUE_RE = re.compile(r"\b(?:queue|search|download|grab|fetch|backfill)\b", re.I)
_MISSING_RE = re.compile(r"\bmissing\b", re.I)
#: §17.1508 — a show whose gap is at least this many episodes is HELD: a whole-series
#: backfill (e.g. Power Rangers' ~972) is the operator's explicit call, not an auto-trigger.
_QUEUE_HOLD_THRESHOLD = 100


def rewrite_queue_missing(node: Optional[dict], commands: list[str], verify: list[str],
                          services: Optional[list] = None,
                          inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1508 — a "queue/search the missing episodes" step triggers Sonarr's per-series
    search for each show's monitored-missing episodes, deterministically, holding any show
    whose gap exceeds `_QUEUE_HOLD_THRESHOLD` (a whole-series backfill the operator asks for
    by name).

    Live (job c75d77fa): the LLM "Consolidate missing episodes list" step returned a refusal,
    so the queue behind it never ran; the completeness checks already know exactly what is
    missing. This skips the flaky consolidation: Sonarr itself knows the gaps, so the engine
    reads `/series`, and for each show with missing monitored episodes below the threshold
    POSTs `SeriesSearch` (which feeds Prowlarr → qBittorrent); a larger gap is reported HELD.
    Runs inside the guest; the key is read on the machine (§17.1503) via `<APP_API_KEY>`."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    title = str((node or {}).get("title") or "")
    if not (_QUEUE_RE.search(title) and _MISSING_RE.search(text) and _SEASONS_RE.search(text)):
        return list(commands or []), list(verify or []), []
    # §17.1683 — a step that says it changes nothing is never a search. Live (job c2518102 T2 "Fetch Sonarr wanted
    # missing", text "Read only, change nothing"): "Fetch" matched the download verbs and the read became SeriesSearch
    # POSTs for ten shows -- in an audit whose whole point was to present a plan before changing anything.
    if declares_read_only(node):
        return list(commands or []), list(verify or []), []
    if _QUEUE_REMOVAL_RE.search(title):
        return list(commands or []), list(verify or []), []     # §17.1650 — taking items OUT of the queue is not a search
    app = "radarr" if re.search(r"\bradarr\b|\bmovies?\b", title, re.I) else "sonarr"
    gid = _arr_guest(app, services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    port, keyname = _ARR_PORTS[app], f"{app.upper()}_API_KEY"
    payload = (f'python3 -c "{_QUEUE_MISSING_PY}" "<{keyname}>" "{_QUEUE_HOLD_THRESHOLD}"')
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # read-only-safe verify (§17.1505): the command endpoint answers for an authenticated read.
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "{app} command API HTTP %{{http_code}}" -H "X-Api-Key: ${keyname}" '
             f'"http://127.0.0.1:{port}/api/v3/command"')
    note = [{"why": (
        f"drafted the queue from the engine's own shape (§17.1508): it reads {app}'s library inside guest {gid} "
        f"and, for each show with missing monitored episodes, triggers a per-series search (SeriesSearch → "
        f"Prowlarr → qBittorrent) — except a show whose gap exceeds {_QUEUE_HOLD_THRESHOLD} episodes, which is "
        f"HELD and reported, since a whole-series backfill is the operator's explicit call.")}]
    return [run], [check], note


#: §17.1515 — clear the stalled (no-seeder / errored) downloads and re-search. ONE GET of the
#: Sonarr queue, ONE bulk DELETE (removeFromClient + blocklist, so the dead release is not grabbed
#: again), then MissingEpisodeSearch so Sonarr re-grabs better-seeded releases. Two API calls, no
#: loop (the bulk endpoint clears all at once — within the 180 s budget). Single-quoted Python only.
_CLEAR_STALLED_PY = (
    "import sys,json,urllib.request\n"
    "BASE='http://127.0.0.1:8989'; KEY=sys.argv[1]; DRY=len(sys.argv)>2 and sys.argv[2]=='dry'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=30); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "q=api('/api/v3/queue?pageSize=2000&includeUnknownSeriesItems=true') or {}\n"
    "recs=q.get('records') or []\n"
    "def stalled(r):\n"
    # §17.1515 — target a DEAD download (no seeders), not an import problem. An importBlocked/
    # importPending item downloaded fine and is stuck on naming — blocklisting it would discard a
    # good file and re-grab. Match only stall/no-connection/no-files signals.
    "    state=str(r.get('trackedDownloadState') or '').lower()\n"
    "    if state in ('importblocked','importpending','importing'): return False\n"
    "    blob=(json.dumps(r.get('statusMessages') or [])+' '+str(r.get('errorMessage') or '')).lower()\n"
    "    return ('stall' in blob) or ('no connection' in blob) or ('no files found' in blob) or ('no peers' in blob)\n"
    "ids=[r.get('id') for r in recs if stalled(r) and r.get('id') is not None]\n"
    "print('STALLED: %d of %d queue items' % (len(ids), len(recs)))\n"
    "if DRY:\n"
    "    print('WOULD REMOVE %d (blocklist) then re-search' % len(ids)); sys.exit(0)\n"
    "if ids:\n"
    "    try:\n"
    "        api('/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false','DELETE',{'ids':ids})\n"
    "        print('REMOVED %d stalled (blocklisted)' % len(ids))\n"
    "    except Exception as e:\n"
    "        print('FAILED bulk remove: %r' % e)\n"
    "try:\n"
    "    api('/api/v3/command','POST',{'name':'MissingEpisodeSearch'}); print('RE-SEARCH: MissingEpisodeSearch triggered')\n"
    "except Exception as e:\n"
    "    print('RE-SEARCH failed: %r' % e)\n"
    "print('RESULT: cleared %d stalled, re-search running' % len(ids))\n"
)
_CLEAR_STALLED_RE = re.compile(r"\b(?:clear|remove|purge|cancel|clean)\b.*\b(?:stalled|stuck|dead|failed)\b|\bstalled\b.*\b(?:download|torrent|queue)\b", re.I)


def rewrite_clear_stalled(node: Optional[dict], commands: list[str], verify: list[str],
                          services: Optional[list] = None,
                          inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1515 — "remove the stalled downloads and re-search" done deterministically.

    Live (job aefaef87): 120 of 238 Sonarr queue items were "stalled with no connections"
    (dead releases, no seeders). The engine clears them through Sonarr's own queue API — a
    single bulk DELETE with `blocklist=true` so the same dead release is not re-grabbed, and
    `removeFromClient=true` so the partial files are dropped from qBittorrent — then triggers
    `MissingEpisodeSearch` so Sonarr pulls better-seeded releases. Two API calls, no loop (the
    bulk endpoint clears all at once, inside the 180 s budget). Runs inside the Sonarr guest;
    key read on the machine (§17.1503) via `<SONARR_API_KEY>`."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    title = str((node or {}).get("title") or "")
    if not _CLEAR_STALLED_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("sonarr", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_CLEAR_STALLED_PY}" "<SONARR_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "sonarr queue API HTTP %{{http_code}}" -H "X-Api-Key: $SONARR_API_KEY" '
             f'"http://127.0.0.1:8989/api/v3/queue?pageSize=1"')
    note = [{"why": (
        f"drafted the clear-stalled from the engine's own shape (§17.1515): inside guest {gid} it reads the "
        f"Sonarr queue, bulk-removes every stalled/errored item with blocklist=true (so the dead release is not "
        f"grabbed again) and removeFromClient=true (drops the partials from qBittorrent), then triggers "
        f"MissingEpisodeSearch to pull better-seeded releases — two API calls, no per-item loop.")}]
    return [run], [check], note


#: §17.1650 — a title that takes items OUT of an *arr queue (dismiss / remove / clear … queue)
_QUEUE_REMOVAL_RE = re.compile(r"\b(?:dismiss|remove|clear|purge|drop|delete)\b[^.;\n]{0,60}\bqueue\b", re.I)
_IMPORT_BLOCKED_RE = re.compile(r"\bimport[- ]?blocked\b|\bblocked\b[^.;\n]{0,20}\bimports?\b", re.I)
#: in the app's guest: the importBlocked items only, out of the QUEUE only (the client keeps seeding, no blocklist, no
#: file touched), then the app's own search for what is still missing
_DISMISS_BLOCKED_PY = (
    "import sys,json,urllib.request\n"
    "PORT,CMD=sys.argv[1],sys.argv[2]; KEY=sys.argv[3]; BASE='http://127.0.0.1:'+PORT\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=60); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "def blocked():\n"
    "    q=api('/api/v3/queue?pageSize=2000') or {}; recs=q.get('records') or []\n"
    "    return recs,[r['id'] for r in recs if str(r.get('trackedDownloadState') or '').lower()=='importblocked' and r.get('id') is not None]\n"
    "recs,ids=blocked()\n"
    "print('IMPORT-BLOCKED: %d of %d queue items' % (len(ids),len(recs)))\n"
    "if ids:\n"
    "    api('/api/v3/queue/bulk?removeFromClient=false&blocklist=false&skipRedownload=true','DELETE',{'ids':ids})\n"
    "    print('DISMISSED %d from the queue (left in the download client, not blocklisted, no file touched)' % len(ids))\n"
    "api('/api/v3/command','POST',{'name':CMD}); print('RE-SEARCH: %s queued' % CMD)\n"
    "left=blocked()[1]\n"
    "print('STILL IMPORT-BLOCKED: %d' % len(left)); print('STATE-OK' if not left else 'STATE-WRONG'); sys.exit(1 if left else 0)\n")


def rewrite_dismiss_import_blocked(node: Optional[dict], commands: list[str], verify: list[str],
                                   services: Optional[list] = None,
                                   inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1650 — "dismiss the import-blocked queue items" done deterministically.

    Live (job 5db1b088 IB1, 2026-10-18): 82 of Sonarr's 107 queue items were finished season packs held importBlocked
    ("One or more episodes expected in this release were not imported or missing from the release"; every file in them
    already imported). The operator chose to dismiss them from Sonarr's queue only. §17.1508's per-series search claimed
    the step (its title says "queue", its text "missing episodes") and would have searched instead of dismissing;
    §17.1515's clear-stalled deliberately spares importBlocked and drops from the client with a blocklist. Inside the
    app's guest: one bulk DELETE of the importBlocked ids with removeFromClient=false and blocklist=false, the app's
    missing search, a re-read settled by STATE-OK."""
    title = str((node or {}).get("title") or "")
    text = _node_text(node)
    if not (_QUEUE_REMOVAL_RE.search(title) and _IMPORT_BLOCKED_RE.search(text)):
        return list(commands or []), list(verify or []), []
    app = "radarr" if re.search(r"\bradarr\b|\bmovies?\b", title, re.I) else "sonarr"
    gid = _arr_guest(app, services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    port, search = ("7878", "MissingMoviesSearch") if app == "radarr" else ("8989", "MissingEpisodeSearch")
    key = app.upper() + "_API_KEY"
    payload = f'python3 -c "{_DISMISS_BLOCKED_PY}" "{port}" "{search}" "<{key}>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # the check is a READ: how many queue items are still importBlocked (the run itself fails when any is left)
    check = (f'pct exec {gid} -- sh -c {_sq_payload("curl -s -H " + chr(34) + "X-Api-Key: <" + key + ">" + chr(34) + " " + chr(34) + f"http://127.0.0.1:{port}/api/v3/queue?pageSize=2000" + chr(34) + " | grep -o importBlocked | wc -l")}')
    note = [{"why": (
        f"drafted the dismissal from the engine's own shape (§17.1650): inside guest {gid} it takes {app}'s importBlocked "
        f"items out of the QUEUE only -- removeFromClient=false and blocklist=false, so the download client keeps them, "
        f"nothing is blocklisted and no file is touched -- queues {search}, and re-reads the queue (STATE-OK when none "
        f"is left; the run fails when any is).")}]
    return [run], [check], note


#: §17.1523 — "inventory the Prowlarr indexers" done deterministically.
#:
#: Live (job aefaef87 T2): the drafter wrapped the read in the §17.1317 detached template but the
#: in-guest script (1) `systemctl stop prowlarr.service` before curling its own API — it copied the
#: machine-truth note "the service rewrites config.xml, stop it before editing" onto a READ-only
#: inventory, so the API it then curls is dead and the inventory comes back empty; (2) parsed with
#: `jq`, a dependency the guest need not carry; (3) the verify ran on the HOST against loopback 9696
#: (the host does not listen there — §17.1368 refused it). The engine does this exactly: INSIDE the
#: Prowlarr guest, without touching the service, GET `/api/v1/indexer` and `/api/v1/indexerstatus`
#: and print a table (id, name, enabled, protocol, privacy, and FAILING from the status list) plus a
#: one-line summary. python3 parses in the write channel (no jq); the key is read on the machine
#: (§17.1503) by `<PROWLARR_API_KEY>`. The same read answers T3 "identify failing" — the FAILING
#: column and the summary name them — so that step reuses this capability.
_PROWLARR_PORT = "9696"
_PROWLARR_INVENTORY_PY = (
    "import sys,json,urllib.request\n"
    "BASE='http://127.0.0.1:" + _PROWLARR_PORT + "'; KEY=sys.argv[1]\n"
    "def api(p):\n"
    "    r=urllib.request.Request(BASE+p,headers={'X-Api-Key':KEY})\n"
    "    d=urllib.request.urlopen(r,timeout=30).read()\n"
    "    return json.loads(d) if d else []\n"
    "idx=api('/api/v1/indexer') or []\n"
    # §17.1523 — indexerstatus lists only the indexers Prowlarr has auto-disabled after repeated
    # failures (a disabledTill in the future). Its presence is the "failing" signal; the static
    # indexer object has no such flag.
    "try: st=api('/api/v1/indexerstatus') or []\n"
    "except Exception: st=[]\n"
    "failing={s.get('indexerId') for s in st}\n"
    "print('INDEXERS: %d configured' % len(idx))\n"
    "print('id\\tname\\tenabled\\tprotocol\\tprivacy\\tstate')\n"
    "for x in sorted(idx,key=lambda a: a.get('id') or 0):\n"
    "    state='FAILING' if x.get('id') in failing else 'ok'\n"
    "    print('%s\\t%s\\t%s\\t%s\\t%s\\t%s' % (x.get('id'),x.get('name'),x.get('enable'),x.get('protocol'),x.get('privacy'),state))\n"
    "nen=sum(1 for x in idx if x.get('enable'))\n"
    "nfail=sum(1 for x in idx if x.get('id') in failing)\n"
    "fn=[x.get('name') for x in idx if x.get('id') in failing]\n"
    "print('SUMMARY: %d indexers, %d enabled, %d failing%s' % (len(idx),nen,nfail,(': '+', '.join(fn)) if fn else ''))\n"
)
#: inventory/enumerate verbs on indexers — a READ. "identify failing" is a read too (the FAILING
#: column answers it). Guarded off the write verbs (disable/remove/add/sync/test) so T5/T6/T7/T9
#: keep their own capabilities or the model draft.
_PROWLARR_INVENTORY_RE = re.compile(r"\b(?:inventor|enumerat|list|catalog|audit|identif)\w*\b.{0,40}\b(?:failing\s+)?indexers?\b", re.I)
_PROWLARR_INVENTORY_NOT_RE = re.compile(r"\b(?:disable|remove|delete|\badd\b|sync|test|cloudflare|protected)\b", re.I)


def rewrite_inventory_prowlarr_indexers(node: Optional[dict], commands: list[str], verify: list[str],
                                        services: Optional[list] = None,
                                        inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1523 — inventory (and identify-failing) of the Prowlarr indexers, deterministically.

    Replaces the step's commands AND verify when the step is a Prowlarr indexer inventory/identify
    read and the Prowlarr guest is known; otherwise leaves the draft untouched."""
    title = str((node or {}).get("title") or "")
    if not _PROWLARR_INVENTORY_RE.search(title) or _PROWLARR_INVENTORY_NOT_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("prowlarr", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_PROWLARR_INVENTORY_PY}" "<PROWLARR_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1505 — the verify rides the READ-ONLY channel (no python3, no `$(`): a plain
    # authenticated read of the same endpoint, witnessing that Prowlarr is up and the key
    # is valid. The RUN's own output carries the table; this only proves the read answers.
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "prowlarr indexer API HTTP %{{http_code}}" -H "X-Api-Key: $PROWLARR_API_KEY" '
             f'"http://127.0.0.1:{_PROWLARR_PORT}/api/v1/indexer"')
    note = [{"why": (
        f"drafted the Prowlarr indexer inventory from the engine's own shape (§17.1523): inside guest {gid}, "
        f"WITHOUT stopping prowlarr.service (it is a read), it GETs /api/v1/indexer and /api/v1/indexerstatus and "
        f"prints a table — id, name, enabled, protocol, privacy, and FAILING for any Prowlarr auto-disabled after "
        f"repeated failures — plus a summary; python3 parses in-guest (no jq), the key read on the machine.")}]
    return [run], [check], note


#: §17.1524 — "disable the failing Prowlarr indexers" done deterministically.
#:
#: Live (job aefaef87 T5): the drafter's frame carried an unfilled template — a PUT to
#: `/api/v1/indexer/<INDEXER_ID>` with a body of `{"enable": false, "id": <INDEXER_ID>, "name":
#: "<INDEXER_NAME>"}`. The id/name are never filled (so the call is inert), and even filled it is
#: wrong twice over: an *arr PUT validates the WHOLE object (a three-field body is rejected on its
#: missing required fields), and saving an indexer runs its connection test — which a FAILING
#: indexer cannot pass, so the save is refused unless `?forceSave=true`. The engine does it exactly:
#: inside the guest, GET the indexers + indexerstatus, and for each one Prowlarr has auto-disabled
#: after repeated failures, PUT the indexer's OWN full object back with enable=false and
#: forceSave=true (skip the doomed test). The key is read on the machine by `<PROWLARR_API_KEY>`.
_PROWLARR_DISABLE_FAILING_PY = (
    "import sys,json,urllib.request\n"
    "BASE='http://127.0.0.1:" + _PROWLARR_PORT + "'; KEY=sys.argv[1]\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,"
    "headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    # §17.1525 — a short per-request timeout so the bounded batch below cannot outlive the 180 s
    # command budget even if every save hangs (15 saves x 10 s = 150 s < 180 s).
    "    d=urllib.request.urlopen(r,timeout=10).read()\n"
    "    return json.loads(d) if d else None\n"
    "idx=api('/api/v1/indexer') or []\n"
    "try: st=api('/api/v1/indexerstatus') or []\n"
    "except Exception: st=[]\n"
    "failing={s.get('indexerId') for s in st}\n"
    # only the ones still ENABLED need disabling; Prowlarr may already have disabled some itself.
    "targets=[x for x in idx if x.get('id') in failing and x.get('enable')]\n"
    "print('FAILING+ENABLED: %d of %d indexers' % (len(targets),len(idx)))\n"
    "done=[]\n"
    # §17.1525 — the loop reaches the service once per item, so it is BOUND to a slice that fits the
    # budget (the network-loop gate refuses an open-ended loop over whatever the API returns). The
    # batch is resumable: a disabled indexer drops out of `targets`, so a re-run handles any overflow.
    "for x in targets[:15]:\n"
    # PUT the indexer's OWN full object (every required field already present), only flip enable.
    "    x['enable']=False\n"
    "    try:\n"
    # §17.1524 — forceSave=true: saving runs the connection test, which a failing indexer cannot
    # pass; without it the disable itself is refused.
    "        api('/api/v1/indexer/%s?forceSave=true' % x['id'],'PUT',x)\n"
    "        done.append(x.get('name')); print('DISABLED: %s (id %s)' % (x.get('name'),x.get('id')))\n"
    "    except Exception as e:\n"
    "        print('FAILED to disable %s (id %s): %r' % (x.get('name'),x.get('id'),e))\n"
    "print('RESULT: disabled %d failing indexer(s)%s' % (len(done),(': '+', '.join(done)) if done else ''))\n"
)
#: disable + failing/dead on indexers — a WRITE. Guarded off add/remove so T6/T7 keep their path.
_PROWLARR_DISABLE_RE = re.compile(r"\b(?:disable|turn\s*off|deactivat)\w*\b.{0,40}\b(?:failing|dead|broken|erroring)?\s*indexers?\b", re.I)
_PROWLARR_DISABLE_NOT_RE = re.compile(r"\b(?:\badd\b|remove|delete|cloudflare|sync)\b", re.I)


def rewrite_disable_failing_indexers(node: Optional[dict], commands: list[str], verify: list[str],
                                     services: Optional[list] = None,
                                     inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1524 — disable the Prowlarr indexers that Prowlarr auto-disabled after repeated
    failures, deterministically. Replaces the step's commands AND verify when the step is a
    disable-failing-indexers write and the Prowlarr guest is known; otherwise untouched."""
    title = str((node or {}).get("title") or "")
    if not _PROWLARR_DISABLE_RE.search(title) or _PROWLARR_DISABLE_NOT_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("prowlarr", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_PROWLARR_DISABLE_FAILING_PY}" "<PROWLARR_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "prowlarr indexer API HTTP %{{http_code}}" -H "X-Api-Key: $PROWLARR_API_KEY" '
             f'"http://127.0.0.1:{_PROWLARR_PORT}/api/v1/indexer"')
    note = [{"why": (
        f"drafted the disable-failing from the engine's own shape (§17.1524): inside guest {gid} it finds the "
        f"indexers Prowlarr auto-disabled after repeated failures and PUTs each one's OWN full object back with "
        f"enable=false and forceSave=true (an *arr PUT validates the whole object, and saving runs the connection "
        f"test a failing indexer cannot pass) — replacing the drafter's inert `<INDEXER_ID>` template.")}]
    return [run], [check], note


#: §17.1526/1528 — "add general-purpose public TV indexers" done deterministically, routing the
#: Cloudflare-gated ones through FlareSolverr (operator decisions ASK7 = PUBLIC/no-login only, and
#: T1 = KEEP the Cloudflare indexers by routing them through FlareSolverr).
#:
#: Live (job aefaef87 T7): the model drafted a 24-command kitchen sink re-doing every other step of
#: the job (the whole job's RAG context bled into the node), with one line actually about T7, and
#: the frame refused. The first deterministic draft (§1526) then taught the engine two live facts:
#:   (a) its curated names mostly did not exist in the catalog — of 659 definitions, this Prowlarr
#:       carries 87 public-torrent ones, and the real general/TV names are the set below (measured);
#:   (b) forceSave does NOT skip the connection test on an ADD, so a Cloudflare-gated tracker
#:       (1337x, EZTV) cannot be added until its requests route through FlareSolverr — which is
#:       exactly what T1 decided and the plan never had a step for.
#: So the engine does the whole thing (§1528): inside the Prowlarr guest, ensure a `flaresolverr`
#: tag and a FlareSolverr indexer-proxy (host = the deployed FlareSolverr) exist, then for each
#: wanted public tracker not already configured, POST the catalog's own template; if the add is
#: refused with a Cloudflare error, retry the POST WITH the flaresolverr tag so the connection test
#: runs THROUGH FlareSolverr. Idempotent, bounded, hang-safe, errors reported per name. The key is
#: read on the machine (§17.1503) by `<PROWLARR_API_KEY>`.
#:
#: The set: the general/English-TV public (no-login) definitions this Prowlarr actually carries and
#: that the measured inventory of 49 (heavy on anime/foreign/adult) did not already have.
_PROWLARR_TV_INDEXERS = (
    "1337x", "EZTV", "LimeTorrents", "TorrentProject2", "kickasstorrents.to",
)
_PROWLARR_ADD_INDEXERS_PY = (
    "import sys,json,urllib.request,urllib.error\n"
    "BASE='http://127.0.0.1:" + _PROWLARR_PORT + "'; KEY=sys.argv[1]\n"
    # §17.1535 — the indexer proxy points at BYPARR (FlareSolverr's Camoufox drop-in), not
    # FlareSolverr, because FlareSolverr can no longer clear these trackers (cookie-replay detection).
    "FS_URL='http://192.168.1.27:8191/'\n"
    "WANT=" + repr(list(_PROWLARR_TV_INDEXERS)) + "\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,"
    "headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    try:\n"
    "        d=urllib.request.urlopen(r,timeout=12).read()\n"
    "        return (json.loads(d) if d else None, None)\n"
    "    except urllib.error.HTTPError as e:\n"
    "        return (None, e.read().decode('utf-8','replace'))\n"
    # §17.1527 — a tracker that does not answer raises URLError/TimeoutError/OSError, NOT an
    # HTTPError; catch those too so a hang on one POST is recorded and the batch keeps going.
    "    except (urllib.error.URLError, TimeoutError, OSError) as e:\n"
    "        return (None, 'unreachable: %r' % e)\n"
    # §17.1534 — make Prowlarr prefer IPv4 FIRST (Prowlarr #2810): FlareSolverr clears Cloudflare and
    # grabs the clearance over the site's IPv4 address, but Prowlarr otherwise prefers the tracker's
    # IPv6 and connects there with a cookie that does not apply ("Challenge solved" yet "blocked").
    # glibc caches /etc/gai.conf per process, so Prowlarr must RE-READ it: restart it through its OWN
    # API (no systemctl — that unit is not in this frame's measured-unit list, §17.1327; the API
    # restart needs only the key this script already holds), then wait for it back. Idempotent.
    "import time\n"
    "try: _gai=open('/etc/gai.conf').read()\n"
    "except Exception: _gai=''\n"
    "if 'precedence ::ffff:0:0/96  100' not in _gai:\n"
    "    try: open('/etc/gai.conf','a').write('precedence ::ffff:0:0/96  100\\n')\n"
    "    except Exception as e: print('gai.conf write failed: %r' % e)\n"
    "    api('/api/v1/system/restart','POST',{})\n"
    "    n=0\n"
    "    while n < 30:\n"
    "        try: urllib.request.urlopen(urllib.request.Request(BASE+'/ping'),timeout=5); break\n"
    "        except Exception: n+=1; time.sleep(3)\n"
    "    print('IPv4 preference set; prowlarr restarted (waited %d)' % n)\n"
    "else: print('IPv4 preference already set')\n"
    # §17.1529 — classify a failed save by the error's SHAPE, never a phrase list (a phrase list is
    # always one phrase short). An *arr 400 is a list of failures: a failure whose propertyName is
    # EMPTY is an availability error (the tracker is unreachable — try it THROUGH FlareSolverr); a
    # failure that NAMES a field is a bad request (report it, do not retry). A non-JSON body (a raw
    # timeout) is treated as availability.
    "def is_availability(err):\n"
    "    try: items=json.loads(err)\n"
    "    except Exception: return True\n"
    "    if isinstance(items,dict): items=[items]\n"
    "    if not isinstance(items,list) or not items: return True\n"
    "    return all(not (i.get('propertyName') or '') for i in items if isinstance(i,dict))\n"
    # §17.1528 — ensure the FlareSolverr tag + indexer-proxy exist (T1: keep Cloudflare indexers by
    # routing them through FlareSolverr). Idempotent: created once, reused after.
    "tags=(api('/api/v1/tag')[0]) or []\n"
    "tag=next((t for t in tags if (t.get('label') or '')=='flaresolverr'), None)\n"
    "if not tag: tag=api('/api/v1/tag','POST',{'label':'flaresolverr'})[0]\n"
    "tagid=(tag or {}).get('id')\n"
    "prox=(api('/api/v1/indexerproxy')[0]) or []\n"
    "fsp=next((p for p in prox if p.get('implementation')=='FlareSolverr'), None)\n"
    "if fsp is None:\n"
    "    psch=(api('/api/v1/indexerproxy/schema')[0]) or []\n"
    "    fs=next((d for d in psch if d.get('implementation')=='FlareSolverr'), None)\n"
    "    if fs:\n"
    "        pb=dict(fs); pb['name']='Byparr'; pb['tags']=[tagid] if tagid else []\n"
    "        for f in pb.get('fields',[]):\n"
    "            if f.get('name')=='host': f['value']=FS_URL\n"
    # §17.1536 — Byparr's CPU-only Camoufox solve takes 60-70 s; the proxy's default 60 s request
    # timeout cuts it off, so Prowlarr sees "unreachable" though Byparr returns 200. Give it 180 s.
    "            if f.get('name')=='requestTimeout': f['value']=180\n"
    "        _,perr=api('/api/v1/indexerproxy?forceSave=true','POST',pb)\n"
    "        print('PROXY: '+('added Byparr -> '+FS_URL if perr is None else 'FAILED '+str(perr)[:120].replace(chr(10),' ')))\n"
    "    else: print('PROXY: no FlareSolverr-compatible proxy schema')\n"
    "else:\n"
    # §17.1535 — a proxy already exists (it pointed at FlareSolverr); REPOINT its host at Byparr and
    # make sure it carries the flaresolverr tag, so the Cloudflare indexers route through Byparr.
    "    cur=next((f.get('value') for f in fsp.get('fields',[]) if f.get('name')=='host'),'')\n"
    "    curto=next((f.get('value') for f in fsp.get('fields',[]) if f.get('name')=='requestTimeout'),None)\n"
    "    if cur!=FS_URL or curto!=180 or (tagid and tagid not in (fsp.get('tags') or [])):\n"
    "        for f in fsp.get('fields',[]):\n"
    "            if f.get('name')=='host': f['value']=FS_URL\n"
    "            if f.get('name')=='requestTimeout': f['value']=180\n"
    "        if tagid and tagid not in (fsp.get('tags') or []): fsp['tags']=(fsp.get('tags') or [])+[tagid]\n"
    "        fsp['name']='Byparr'\n"
    "        _,uerr=api('/api/v1/indexerproxy/%s?forceSave=true' % fsp.get('id'),'PUT',fsp)\n"
    "        print('PROXY: repointed to Byparr -> '+FS_URL+' (timeout 180s)'+(' FAILED '+str(uerr)[:80] if uerr else ''))\n"
    "    else: print('PROXY: Byparr already configured')\n"
    "schema=(api('/api/v1/indexer/schema')[0]) or []\n"
    "existing=(api('/api/v1/indexer')[0]) or []\n"
    "have={(x.get('name') or '').lower() for x in existing}\n"
    "bydef={(s.get('name') or '').lower(): s for s in schema}\n"
    "added=[]; viafs=[]; skipped=[]; failed=[]\n"
    # §17.1536 — split present/absent/to-do up front. Byparr's CPU-only solve is 60-70 s each, so
    # only a FEW fit one command's 180 s budget; add a small resumable batch and let a re-run take
    # the next — an already-added tracker drops out of `todo`, so re-running advances, never repeats.
    "todo=[]\n"
    "for w in WANT:\n"
    "    wl=w.lower()\n"
    "    if wl in have: skipped.append(w+' (already present)')\n"
    "    elif wl not in bydef: skipped.append(w+' (not in schema)')\n"
    "    else: todo.append(w)\n"
    "for w in todo[:2]:\n"
    "    s=bydef[w.lower()]\n"
    "    body=dict(s); body['enable']=True\n"
    # Prowlarr requires an app-sync profile id > 0; 1 is the default "Standard" profile.
    "    if not body.get('appProfileId'): body['appProfileId']=1\n"
    "    res,err=api('/api/v1/indexer?forceSave=true','POST',body)\n"
    "    if err is None: added.append(w); continue\n"
    # §17.1528/1529 — an AVAILABILITY failure (empty propertyName: the tracker is unreachable,
    # e.g. behind Cloudflare) is retried WITH the flaresolverr tag so the test runs through the
    # proxy; a NAMED-field failure is a bad request, reported per name and never retried.
    "    if is_availability(err) and tagid:\n"
    "        b2=dict(body); b2['tags']=[tagid]\n"
    "        _,err2=api('/api/v1/indexer?forceSave=true','POST',b2)\n"
    "        if err2 is None: viafs.append(w); continue\n"
    "        failed.append('%s: %s' % (w, ('unreachable even via the proxy' if is_availability(err2) else err2[:110].replace(chr(10),' '))))\n"
    "    else: failed.append('%s: %s' % (w, err[:120].replace(chr(10),' ')))\n"
    "remaining=max(0, len(todo)-2)\n"
    "print('SCHEMA: %d definitions; EXISTING: %d indexers' % (len(schema),len(existing)))\n"
    "print('ADDED (direct): %d - %s' % (len(added), ', '.join(added) if added else 'none'))\n"
    "print('ADDED (via Byparr): %d - %s' % (len(viafs), ', '.join(viafs) if viafs else 'none'))\n"
    "if skipped: print('SKIPPED: %s' % '; '.join(skipped))\n"
    "if failed: print('FAILED: %s' % '; '.join(failed))\n"
    "if remaining: print('REMAINING to add: %d (re-run this step to continue — Byparr solves are slow)' % remaining)\n"
    "print('RESULT: added %d public indexer(s) (%d direct, %d via Byparr)' % (len(added)+len(viafs),len(added),len(viafs)))\n"
    # §17.1533 — if NOTHING the step was asked to add landed and there were real failures (not just
    # ones already present), EXIT NON-ZERO. A capability that catches its own errors and exits 0 is
    # recorded `done`, so the engine's failure-research (§17.1201 diagnose_failure → generate_fix,
    # which reads the live machine AND researches the web) never engages — the operator finds the
    # failure by hand instead of the engine finding the fix. An honest exit routes an unknown failure
    # back into the engine's own research loop. (Partial success, or all-already-present, stays 0.)
    "sys.exit(1 if (failed and not added and not viafs) else 0)\n"
)
#: add + (general/public/tv) indexers — a WRITE. Guarded off disable/remove/sync/inventory.
_PROWLARR_ADD_RE = re.compile(r"\badd\b.{0,40}\b(?:general|public|tv|more|new)?\s*indexers?\b", re.I)
_PROWLARR_ADD_NOT_RE = re.compile(r"\b(?:disable|remove|delete|sync|inventor|list|failing)\b", re.I)


def rewrite_add_general_tv_indexers(node: Optional[dict], commands: list[str], verify: list[str],
                                    services: Optional[list] = None,
                                    inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1526 — add general-purpose public (no-login) TV indexers, deterministically. Replaces
    the step's commands AND verify when the step is an add-indexers write and the Prowlarr guest
    is known; otherwise leaves the draft untouched."""
    title = str((node or {}).get("title") or "")
    if not _PROWLARR_ADD_RE.search(title) or _PROWLARR_ADD_NOT_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("prowlarr", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_PROWLARR_ADD_INDEXERS_PY}" "<PROWLARR_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    check = (f'pct exec {gid} -- curl -s --fail-with-body -o /dev/null '
             f'-w "prowlarr indexer API HTTP %{{http_code}}" -H "X-Api-Key: $PROWLARR_API_KEY" '
             f'"http://127.0.0.1:{_PROWLARR_PORT}/api/v1/indexer"')
    note = [{"why": (
        f"drafted the add-indexers from the engine's own shape (§17.1526/1528/1534): inside guest {gid} it makes "
        f"Prowlarr prefer IPv4 and restarts it via its OWN API so FlareSolverr's IPv4 clearance applies (Prowlarr "
        f"#2810), ensures a FlareSolverr-type indexer-proxy pointed at BYPARR (host {_BYPARR_IP}:8191 — "
        f"FlareSolverr's Camoufox drop-in, since FlareSolverr can no longer clear these trackers; operator "
        f"decision T1: keep the Cloudflare indexers working), then for each general/TV PUBLIC no-login tracker "
        f"this Prowlarr's catalog carries and not already present ({', '.join(_PROWLARR_TV_INDEXERS)}) POSTs the "
        f"catalog's own template; a Cloudflare refusal is retried WITH the flaresolverr tag so the test runs through "
        f"the proxy. Idempotent, bounded, hang-safe; exits non-zero if nothing lands (§17.1533) so the engine "
        f"researches it — replacing the model's 24-command kitchen sink that re-did the whole job.")}]
    return [run], [check], note


#: §17.1516 — deploy FlareSolverr as its OWN new LXC (operator decision ASK1), so Prowlarr can
#: route its Cloudflare-gated indexers through it. Measured slot on this homelab (2026-10-12):
#: containers 101-105/111/120/130 exist → next free id 107; LAN .20-.25 taken → .26 free; bridge
#: vmbr0, gw .1, template local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst, rootfs local-lvm,
#: all media CTs carry nesting=1. FlareSolverr's official image bundles Chromium, so the reliable
#: path is a Debian LXC running the official container (nesting=1,keyctl=1 for Docker). Idempotent:
#: the create is skipped if 107 already exists; the container is replaced, not duplicated.
_FS_VMID = "107"
_FS_IP = "192.168.1.26"
_FS_TEMPLATE = "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
#: the host prologue: create (if absent) + start + wait-running + let the network settle. One host
#: command, well inside the 180 s budget (create is quick, boot is the only wait).
_FS_CREATE = (
    "sh -c '"
    f"if pct status {_FS_VMID} >/dev/null 2>&1; then echo \"CT {_FS_VMID} already exists: $(pct status {_FS_VMID})\"; "
    # §17.1530 — a container that already exists is RIGHT-SIZED and recovered: FlareSolverr bundles
    # Chromium and 1 GB OOM-wedged the guest (pct exec timed out on everything), so raise it to 2 GB
    # and force a clean stop+start — that applies the new memory AND clears a hung guest. stop is
    # forceful after its timeout, which `pct reboot` is not when the guest cannot shut itself down.
    f"pct set {_FS_VMID} --memory 2048 --swap 1024; "
    f"pct stop {_FS_VMID} >/dev/null 2>&1 || true; "
    f"n=0; while [ $n -lt 12 ]; do pct status {_FS_VMID} | grep -q stopped && break; n=$((n+1)); sleep 3; done; "
    f"pct start {_FS_VMID}; "
    f"else pct create {_FS_VMID} {_FS_TEMPLATE} "
    # §17.1530 — 2 GB/1 GB swap from the start: 1 GB could not hold FlareSolverr's Chromium.
    "--hostname flaresolverr --cores 2 --memory 2048 --swap 1024 "
    "--rootfs local-lvm:8 "
    f"--net0 name=eth0,bridge=vmbr0,firewall=1,gw=192.168.1.1,ip={_FS_IP}/24,type=veth "
    # §17.1519 — a nameserver, or the container inherits the host's Tailscale resolver
    # (100.100.100.100) and cannot resolve public names, so apt/docker pulls hang. The working
    # media CTs use the gateway; 1.1.1.1 is the fallback.
    "--nameserver \"192.168.1.1 1.1.1.1\" "
    "--features nesting=1,keyctl=1 --unprivileged 1 --onboot 1 --start 1 && echo \"created CT " + _FS_VMID + "\"; fi; "
    f"pct status {_FS_VMID} | grep -q running || pct start {_FS_VMID}; "
    f"n=0; while [ $n -lt 24 ]; do pct status {_FS_VMID} | grep -q running && break; n=$((n+1)); sleep 5; done; "
    # §17.1517 — NO `pct exec` inside this host script: a nested guest-exec here made the host-tool
    # gate read everything after it (the final `pct status`) as running in the guest and refuse the
    # frame. A plain sleep lets the guest's network settle; the install command does its own waiting.
    "sleep 10; "
    f"echo \"CT {_FS_VMID} $(pct status {_FS_VMID})\"'"
)
#: §17.1518 — the in-guest install script (apt + get.docker.com + the ~GB image pull). It is pushed
#: into the guest as a FILE and run as a DETACHED systemd unit, because a raw `pct exec … <install>`
#: runs synchronously and a long install is cut by the executor's 180 s command budget (live, the
#: first attempt timed out mid-apt). Pushing a file keeps the script out of the shell-quoting layers.
#: NO single quotes (it rides inside a single-quoted host `sh -c '…'` heredoc). Replaces any old
#: container, then waits for :8191 inside the unit so the unit only finishes once FlareSolverr is up.
_FS_INSTALL_BODY = (
    "export DEBIAN_FRONTEND=noninteractive\n"
    "apt-get update\n"
    "apt-get install -y curl ca-certificates\n"
    "command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh\n"
    "systemctl enable --now docker\n"
    "docker rm -f flaresolverr 2>/dev/null || true\n"
    "docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191 "
    "-e LOG_LEVEL=info ghcr.io/flaresolverr/flaresolverr:latest\n"
    "n=0; while [ $n -lt 45 ]; do curl -sf http://127.0.0.1:8191/ >/dev/null 2>&1 && break; n=$((n+1)); sleep 4; done\n"
    "if curl -sf http://127.0.0.1:8191/ >/dev/null 2>&1; then echo FlareSolverr-ready-on-8191; "
    "else echo FAILED-flaresolverr-not-up; docker logs flaresolverr 2>&1 | tail -30; exit 1; fi\n"
)
#: start: write the script, push it into the guest, launch it as a transient systemd unit (NOT
#: --collect, so the unit's ExecMainStatus is still readable after it finishes), then return at once.
_FS_START = (
    "sh -c 'cat > /tmp/fs_install.sh <<\"FSEOS\"\n"
    + _FS_INSTALL_BODY
    + "FSEOS\n"
    f"pct push {_FS_VMID} /tmp/fs_install.sh /root/.fs_install.sh\n"
    f"pct exec {_FS_VMID} -- bash -c \"systemctl stop scaffold-fs 2>/dev/null; "
    "systemctl reset-failed scaffold-fs 2>/dev/null; rm -f /root/.fs.log; "
    "systemd-run --unit scaffold-fs -p StandardOutput=append:/root/.fs.log "
    "-p StandardError=append:/root/.fs.log bash /root/.fs_install.sh\"\n"
    "echo started-scaffold-fs'"
)
#: wait: poll the unit ~30×5 s = 150 s (inside the 180 s budget), then report its state. Repeated
#: across several commands so the whole install + image pull is covered. §17.1522 — a COUNTER loop,
#: not `$SECONDS`: the runner demands a value for every `$NAME`-shaped variable it does not itself set,
#: and `$SECONDS` (a bash built-in) is refused "neither the engine sent nor the secrets file holds";
#: a locally-assigned `n` is not demanded (the create's own loop proved it).
_FS_WAIT = (
    f"pct exec {_FS_VMID} -- bash -c 'n=0; while [ $n -lt 30 ]; do "
    "st=$(systemctl is-active scaffold-fs 2>/dev/null); "
    "case $st in active|activating|reloading|deactivating) n=$((n+1)); sleep 5;; *) break;; esac; done; "
    "echo wait-state:$(systemctl is-active scaffold-fs 2>/dev/null)'"
)
#: last: finish waiting, print the tail of the log, and fail the step if the unit failed.
_FS_LAST = (
    f"pct exec {_FS_VMID} -- bash -c 'n=0; while [ $n -lt 30 ]; do "
    "st=$(systemctl is-active scaffold-fs 2>/dev/null); "
    "case $st in active|activating|reloading|deactivating) n=$((n+1)); sleep 5;; *) break;; esac; done; "
    "tail -n 30 /root/.fs.log 2>/dev/null; "
    "final=$(systemctl is-active scaffold-fs 2>/dev/null); "
    "ec=$(systemctl show scaffold-fs -p ExecMainStatus --value 2>/dev/null); "
    "if [ \"$final\" = failed ] || { [ -n \"$ec\" ] && [ \"$ec\" != 0 ]; }; "
    "then echo FAILED-scaffold-fs-exit=$ec; exit 1; fi; echo scaffold-fs-finished-ok-exit=$ec'"
)
# §17.1542 — fire ONLY on a genuine FlareSolverr CONTAINER deploy, never on a step that merely names
# it as a service to add/surface. Dropped the "add" lead verb and the "service"/"deploy"/"install"
# tail keywords: "Add FlareSolverr and Byparr services" (a control-panel FEATURE step, developed on
# CT 111) matched both and was hijacked into a `pct create 107` deploy. Real deploy titles still
# match via a deploy verb ("Deploy/Install/Create/Set up FlareSolverr") or a container/lxc tail.
_FLARESOLVERR_RE = re.compile(r"\b(?:deploy|install|create|set\s*up|provision|stand\s*up)\b.*flare\s*solverr|flare\s*solverr(?:\s+\w+){0,2}\s+(?:container|lxc)\b", re.I)   # §17.1589 — adjacent, not anywhere


#: §17.1535 — deploy Byparr, the Camoufox-based FlareSolverr drop-in (ghcr.io/thephaseless/byparr),
#: as its OWN new LXC. The engine's own failure-research (§17.1201) on the T7 add diagnosed that
#: FlareSolverr can no longer clear 1337x/EZTV/… — Cloudflare detects the solved-cookie replay — and
#: recommended byparr-proxy, which both the engine and a web search landed on as the current fix.
#: Byparr is a true drop-in: same port 8191, same `/v1` API, so Prowlarr's FlareSolverr indexer-proxy
#: just points at it. Measured slot: next-free id 108, next-free LAN .27. Same shape as the
#: FlareSolverr deploy (§17.1516/1530): 2 GB (Camoufox is a full Firefox), Docker via get.docker.com,
#: the official image, detached install across wait phases, idempotent create + recover.
_BYPARR_VMID = "108"
_BYPARR_IP = "192.168.1.27"


def engine_deployed_services(names: list[str], measured: Optional[list] = None) -> list:
    """§17.1591 — the services the ENGINE'S OWN DEPLOYS put on this host, as measured services, for a step that names
    them and whose unit-based measurement cannot see them (they run as Docker containers inside guests the read
    channel cannot exec into). Live (c2792d4b T7): FlareSolverr and Byparr were named, nothing was measured, the
    facts carried no address, and the model wrote `192.168.1.107` and kept `192.168.1.25:8192` — the engine had put
    them on CT 107 (192.168.1.26:8191, §17.1516) and CT 108 (192.168.1.27:8191, §17.1535) itself."""
    from app.modules.service_truth import ServiceTruth as Service
    have = {str(getattr(s, "name", "") or "").lower() for s in measured or []}
    known = {"flaresolverr": (_FS_VMID, _FS_IP), "byparr": (_BYPARR_VMID, _BYPARR_IP)}
    out = []
    for n in names or []:
        key = str(n or "").lower()
        if key in known and key not in have:
            gid, ip = known[key]
            out.append(Service(guest=gid, unit="", name=key, state="running", address=ip, ports=("8191",)))   # both on :8191 (docker run -p 8191:8191)
    return out
_BYPARR_CREATE = (
    "sh -c '"
    f"if pct status {_BYPARR_VMID} >/dev/null 2>&1; then echo \"CT {_BYPARR_VMID} already exists: $(pct status {_BYPARR_VMID})\"; "
    f"pct set {_BYPARR_VMID} --memory 2048 --swap 1024; "
    f"pct stop {_BYPARR_VMID} >/dev/null 2>&1 || true; "
    f"n=0; while [ $n -lt 12 ]; do pct status {_BYPARR_VMID} | grep -q stopped && break; n=$((n+1)); sleep 3; done; "
    f"pct start {_BYPARR_VMID}; "
    f"else pct create {_BYPARR_VMID} {_FS_TEMPLATE} "
    "--hostname byparr --cores 2 --memory 2048 --swap 1024 "
    "--rootfs local-lvm:8 "
    f"--net0 name=eth0,bridge=vmbr0,firewall=1,gw=192.168.1.1,ip={_BYPARR_IP}/24,type=veth "
    "--nameserver \"192.168.1.1 1.1.1.1\" "
    "--features nesting=1,keyctl=1 --unprivileged 1 --onboot 1 --start 1 && echo \"created CT " + _BYPARR_VMID + "\"; fi; "
    f"pct status {_BYPARR_VMID} | grep -q running || pct start {_BYPARR_VMID}; "
    f"n=0; while [ $n -lt 24 ]; do pct status {_BYPARR_VMID} | grep -q running && break; n=$((n+1)); sleep 5; done; "
    "sleep 10; "
    f"echo \"CT {_BYPARR_VMID} $(pct status {_BYPARR_VMID})\"'"
)
_BYPARR_INSTALL_BODY = (
    "export DEBIAN_FRONTEND=noninteractive\n"
    "apt-get update\n"
    "apt-get install -y curl ca-certificates\n"
    "command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh\n"
    "systemctl enable --now docker\n"
    "docker rm -f byparr 2>/dev/null || true\n"
    "docker run -d --name byparr --restart unless-stopped -p 8191:8191 "
    "ghcr.io/thephaseless/byparr:latest\n"
    # Byparr pulls a full Camoufox browser on first run, so allow a longer readiness wait.
    "n=0; while [ $n -lt 60 ]; do curl -sf http://127.0.0.1:8191/ >/dev/null 2>&1 && break; n=$((n+1)); sleep 5; done\n"
    "if curl -sf http://127.0.0.1:8191/ >/dev/null 2>&1; then echo Byparr-ready-on-8191; "
    "else echo FAILED-byparr-not-up; docker logs byparr 2>&1 | tail -30; exit 1; fi\n"
)
_BYPARR_START = (
    "sh -c 'cat > /tmp/byparr_install.sh <<\"BPEOS\"\n"
    + _BYPARR_INSTALL_BODY
    + "BPEOS\n"
    f"pct push {_BYPARR_VMID} /tmp/byparr_install.sh /root/.byparr_install.sh\n"
    f"pct exec {_BYPARR_VMID} -- bash -c \"systemctl stop scaffold-byparr 2>/dev/null; "
    "systemctl reset-failed scaffold-byparr 2>/dev/null; rm -f /root/.byparr.log; "
    "systemd-run --unit scaffold-byparr -p StandardOutput=append:/root/.byparr.log "
    "-p StandardError=append:/root/.byparr.log bash /root/.byparr_install.sh\"\n"
    "echo started-scaffold-byparr'"
)
_BYPARR_WAIT = (
    f"pct exec {_BYPARR_VMID} -- bash -c 'n=0; while [ $n -lt 30 ]; do "
    "st=$(systemctl is-active scaffold-byparr 2>/dev/null); "
    "case $st in active|activating|reloading|deactivating) n=$((n+1)); sleep 5;; *) break;; esac; done; "
    "echo wait-state:$(systemctl is-active scaffold-byparr 2>/dev/null)'"
)
_BYPARR_LAST = (
    f"pct exec {_BYPARR_VMID} -- bash -c 'n=0; while [ $n -lt 30 ]; do "
    "st=$(systemctl is-active scaffold-byparr 2>/dev/null); "
    "case $st in active|activating|reloading|deactivating) n=$((n+1)); sleep 5;; *) break;; esac; done; "
    "tail -n 30 /root/.byparr.log 2>/dev/null; "
    "final=$(systemctl is-active scaffold-byparr 2>/dev/null); "
    "ec=$(systemctl show scaffold-byparr -p ExecMainStatus --value 2>/dev/null); "
    "if [ \"$final\" = failed ] || { [ -n \"$ec\" ] && [ \"$ec\" != 0 ]; }; "
    "then echo FAILED-scaffold-byparr-exit=$ec; exit 1; fi; echo scaffold-byparr-finished-ok-exit=$ec'"
)
# §17.1542 — same tightening for Byparr: a genuine container deploy only, not "add … Byparr
# services". Dropped "add"/"wire" lead verbs and the "service"/"deploy"/"install"/"proxy" tail.
# "Deploy byparr-proxy as a new LXC" still matches (deploy verb + "new LXC" tail).
_BYPARR_RE = re.compile(r"\b(?:deploy|install|create|set\s*up|provision|stand\s*up)\b.*\bbyparr\b|\bbyparr\b(?:\s+\w+){0,2}\s+(?:container|lxc)\b", re.I)   # §17.1589


def rewrite_deploy_byparr(node: Optional[dict], commands: list[str], verify: list[str],
                          services: Optional[list] = None,
                          inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1535 — "deploy Byparr" (FlareSolverr's Camoufox-based drop-in) as its own new LXC,
    deterministically, the same way the FlareSolverr deploy works. Replaces the draft when the step
    is a Byparr deploy; otherwise untouched."""
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not (_BYPARR_RE.search(title) or _BYPARR_RE.search(text)):
        return list(commands or []), list(verify or []), []
    from app.modules.machine_truth import place_guest as _pg
    if _pg(node) and str(_pg(node)) != str(_BYPARR_VMID):          # §17.1589 — placed elsewhere: not this deploy
        return list(commands or []), list(verify or []), []
    run = [_BYPARR_CREATE, _BYPARR_START, _BYPARR_WAIT, _BYPARR_WAIT, _BYPARR_WAIT,
           _BYPARR_WAIT, _BYPARR_WAIT, _BYPARR_WAIT, _BYPARR_LAST]
    check = [(f'pct exec {_BYPARR_VMID} -- curl -s -o /dev/null -w "byparr HTTP %{{http_code}}" '
              f"http://127.0.0.1:8191/")]
    note = [{"why": (
        f"drafted the Byparr deploy from the engine's own shape (§17.1535): the engine's failure-research on the "
        f"indexer-add found FlareSolverr can no longer clear these trackers (Cloudflare cookie-replay detection) "
        f"and recommended Byparr, its Camoufox-based drop-in. On the host it creates container {_BYPARR_VMID} "
        f"({_BYPARR_IP} on vmbr0, 2 GB, nesting+keyctl for Docker) if absent, then runs "
        f"ghcr.io/thephaseless/byparr on :8191 inside it (same port/API as FlareSolverr, so Prowlarr's proxy just "
        f"points at it). The install runs DETACHED across the wait phases; idempotent create + recover.")}]
    return run, check, note


_NAMESERVER_RE = re.compile(r"\b(?:name\s*server|nameserver|resolver|resolv\.conf|\bdns\b)\b", re.I)
_NS_VERB_RE = re.compile(r"\b(?:give|set|fix|configure|repair|add|working|point|assign)\b", re.I)
_NS_CT_RE = re.compile(r"\b(?:container|ct|lxc|guest)\s*#?\s*(\d{2,4})\b", re.I)


#: §17.1664 — a title that CREATES a guest (its nameserver is a `pct create` option, not a repair)
_CREATES_A_GUEST_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:create|provision|make|build|deploy|set\s+up)\b[^.;\n]{0,40}\b(?:CT|LXC|container|guest|VM)\b", re.I)


def rewrite_set_container_nameserver(node: Optional[dict], commands: list[str], verify: list[str],
                                     services: Optional[list] = None,
                                     inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1519 — "give container N a working nameserver", done host-side.

    When a new LXC inherits the host's Tailscale resolver (100.100.100.100) it cannot resolve public
    names, so apt/docker pulls hang (live: CT 107's install timed out twice). The engine's own
    precondition machinery detects this and inserts a "give container N a working nameserver" step,
    but drafts it to run INSIDE the guest — where DNS is exactly what is broken. The fix is host-side:
    `pct set N --nameserver "192.168.1.1 1.1.1.1"` (the gateway the working media CTs use, with a
    Cloudflare fallback) then a reboot so the running container regenerates resolv.conf. No `pct exec`
    in the command (the host-tool gate, §17.1517); the verify resolves a public name inside the guest."""
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    # §17.1675 — the TITLE asks for the repair (the engine's own is "Give container N a working nameserver"); the body
    # text no longer does. Live (job 5db1b088 SIP2 "VM 106: make 192.168.1.44 its static address", 2026-10-19): its text
    # said "nameserver 192.168.1.1" and "from CT 111 `ssh …`", and this rewrite replaced the whole step with
    # `pct set 111 --nameserver …; pct reboot 111` -- the control panel's container, rebooted, the static address dropped.
    if not (_NAMESERVER_RE.search(title) and _NS_VERB_RE.search(title)):
        return list(commands or []), list(verify or []), []
    if re.search(r"\bVM\s*#?\s*\d{2,4}\b", title):
        return list(commands or []), list(verify or []), []            # a VM's resolver is not `pct set`
    m = _NS_CT_RE.search(title) or _NS_CT_RE.search(text)
    gid = m.group(1) if m else None
    if not gid and inventory:
        # fall back to the step's subject guest when the title names no id
        subj = (node or {}).get("subject") or (node or {}).get("guest")
        gid = str(subj) if subj else None
    if not gid:
        return list(commands or []), list(verify or []), []
    # §17.1664 — a repair of an EXISTING container never claims the step that creates it. Live (job 3cc24343 T2
    # "Create CT 109 container", 2026-10-19): the step's text said to pass `--nameserver` to `pct create`, this rewrite
    # matched the text and replaced the whole create with `pct set 109 --nameserver …` -- on a container not yet there.
    if _CREATES_A_GUEST_RE.search(title):
        return list(commands or []), list(verify or []), []
    _known = {str(k) for k in ((inventory or {}).get("cts") or {})} | {str(k) for k in ((inventory or {}).get("vms") or {})}
    if inventory and _known and str(gid) not in _known:
        return list(commands or []), list(verify or []), []     # measured: no such guest to repair
    run = [
        f"sh -c 'pct set {gid} --nameserver \"192.168.1.1 1.1.1.1\"; "
        f"pct status {gid} | grep -q running && pct reboot {gid}; "
        f"n=0; while [ $n -lt 24 ]; do pct status {gid} | grep -q running && break; n=$((n+1)); sleep 5; done; "
        f"sleep 8; echo \"CT {gid} nameserver set to 192.168.1.1 1.1.1.1 and rebooted\"'"
    ]
    check = [f"pct exec {gid} -- getent hosts deb.debian.org"]
    note = [{"why": (
        f"drafted the nameserver fix from the engine's own shape (§17.1519): the drafter put the fix inside "
        f"guest {gid}, where DNS is exactly what is broken. On the HOST it sets the container's resolver to "
        f"192.168.1.1 1.1.1.1 (the gateway the working media containers use, plus a Cloudflare fallback) and "
        f"reboots it so resolv.conf regenerates. Verify resolves a public name inside the guest.")}]
    return run, check, note


def rewrite_deploy_flaresolverr(node: Optional[dict], commands: list[str], verify: list[str],
                                services: Optional[list] = None,
                                inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1516 — "deploy FlareSolverr" done deterministically, as its own new LXC.

    The plan carried only the ASK1 decision ("how should FlareSolverr run") and no step that
    actually stands it up. This capability fires on a "deploy/install/create FlareSolverr" step and
    produces the real deploy: a host-side `pct create` of container 107 (the measured next-free id,
    .26 on vmbr0) then the official FlareSolverr Docker container inside it. The install is a single
    `pct exec … bash -c '…'`, which the executor re-wraps into the §17.1317 detached systemd unit, so
    the long apt + Docker install + image pull is polled across the wait phases, not cut by one
    command's budget. Idempotent: the create is skipped when 107 exists, and the container is replaced
    rather than duplicated. Verify reads :8191 through the read-only channel (no python3/`$()`)."""
    title = str((node or {}).get("title") or "")
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not (_FLARESOLVERR_RE.search(title) or _FLARESOLVERR_RE.search(text)):
        return list(commands or []), list(verify or []), []
    # §17.1589 — a step PLACED on another guest ("… fix its handler on container 111 … FlareSolverr") is that
    # guest's work, not this deploy: live, c2792d4b T7 "Fix remaining endpoint backends" was hijacked into a
    # re-deploy of CT 107 (stop/start, docker run) and recorded done while the panel's handlers were never touched.
    from app.modules.machine_truth import place_guest as _pg
    if _pg(node) and str(_pg(node)) != str(_FS_VMID):
        return list(commands or []), list(verify or []), []
    # §17.1518 — create (host), then the detached install polled across six wait phases + a last
    # phase that fails the step if the unit failed. Six waits (~15 min) cover apt + the image pull.
    run = [_FS_CREATE, _FS_START, _FS_WAIT, _FS_WAIT, _FS_WAIT, _FS_WAIT, _FS_WAIT, _FS_WAIT, _FS_LAST]
    check = [(f'pct exec {_FS_VMID} -- curl -s -o /dev/null -w "flaresolverr HTTP %{{http_code}}" '
              f"http://127.0.0.1:8191/")]
    note = [{"why": (
        f"drafted the FlareSolverr deploy from the engine's own shape (§17.1516/1518): the plan had only the "
        f"decision on how it should run, not a step that stands it up. On the host it creates container "
        f"{_FS_VMID} (the measured next-free id, {_FS_IP} on vmbr0, Debian 12, nesting+keyctl for Docker) if "
        f"it does not already exist, then runs the official FlareSolverr Docker container on :8191 inside it. "
        f"The long install (apt + image pull) runs as a DETACHED systemd unit polled across the wait phases, so "
        f"it is not cut by one command's 180 s budget. Re-runnable: the create is skipped when {_FS_VMID} exists "
        f"and the container is replaced, not duplicated.")}]
    return run, check, note


#: §17.1510 — give Jellyfin the Netflix-style layout: a typed Movies library at /media/movies,
#: a typed TV Shows library at /media/tv, and no leftover "mixed" library over the /media root,
#: then a scan so posters/metadata populate. Idempotent and reversible (library config only — it
#: never touches media files). Single-quoted Python only; runs inside the guest; argv: KEY, [DRY].
_ORGANIZE_JELLYFIN_PY = (
    "import sys,json,urllib.request,urllib.parse\n"
    "BASE='http://127.0.0.1:8096'\n"
    "KEY=sys.argv[1]; DRY=len(sys.argv)>2 and sys.argv[2]=='dry'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=10); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "def q(s): return urllib.parse.quote(str(s))\n"
    "libs=api('/Library/VirtualFolders') or []\n"
    "byname={l.get('Name'):l for l in libs}\n"
    "desired=[('Movies','movies','/media/movies'),('Shows','tvshows','/media/tv')]\n"
    "for name,ctype,path in desired:\n"
    "    cur=byname.get(name)\n"
    "    if cur and cur.get('CollectionType')==ctype and path in (cur.get('Locations') or []):\n"
    "        print('OK: %s already %s -> %s' % (name,ctype,path)); continue\n"
    "    try:\n"
    "        if cur: api('/Library/VirtualFolders?name=%s&refreshLibrary=false' % q(name),'DELETE')\n"
    "        if DRY:\n"
    "            print('WOULD SET: %s -> %s (%s)' % (name,ctype,path)); continue\n"
    "        api('/Library/VirtualFolders?name=%s&collectionType=%s&paths=%s&refreshLibrary=false' % (q(name),ctype,q(path)),'POST',{'LibraryOptions':{}})\n"
    "        print('SET: %s -> %s (%s)' % (name,ctype,path))\n"
    "    except Exception as e:\n"
    "        print('FAILED %s: %r' % (name,e))\n"
    "for l in libs[:8]:\n"
    "    nm=l.get('Name')\n"
    "    if nm not in ('Movies','Shows') and '/media' in (l.get('Locations') or []):\n"
    "        if DRY:\n"
    "            print('WOULD REMOVE mixed library: %s' % nm); continue\n"
    "        try:\n"
    "            api('/Library/VirtualFolders?name=%s&refreshLibrary=false' % q(nm),'DELETE'); print('REMOVED mixed library: %s' % nm)\n"
    "        except Exception as e:\n"
    "            print('FAILED remove %s: %r' % (nm,e))\n"
    "if not DRY:\n"
    "    try: api('/Library/Refresh','POST'); print('RESULT: libraries typed (Movies/TV Shows); scan triggered')\n"
    "    except Exception as e: print('RESULT: libraries set, scan trigger failed: %r' % e)\n"
    "else: print('RESULT: dry run, nothing changed')\n"
)
#: the step that lays Jellyfin out like a streaming service.
_ORGANIZE_RE = re.compile(r"\b(?:configure|organi[sz]e|arrange|set\s*up|lay\s*out)\b", re.I)
_JELLYFIN_RE = re.compile(r"\bjellyfin\b|\blibrar(?:y|ies)\b", re.I)


def rewrite_organize_jellyfin(node: Optional[dict], commands: list[str], verify: list[str],
                              services: Optional[list] = None,
                              inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1510 — a "configure/organize the Jellyfin library" step gives Jellyfin a
    streaming-service layout deterministically: a typed Movies library at /media/movies, a
    typed TV Shows library at /media/tv, no leftover mixed library over /media, and a scan.

    Live (job c75d77fa): the operator found Jellyfin "not organized" — measured, its three
    libraries (Media, Movies, Shows) had NO ContentType (all mixed) and a redundant Media
    library over the /media root, so there was no clean Movies/TV split or poster wall. The
    engine fixes it through Jellyfin's own VirtualFolders API (remove+re-add each library with
    the right collectionType, drop the mixed one, refresh). Idempotent and reversible: library
    config only, never media files. Runs inside the Jellyfin guest; key read on the machine
    (§17.1382) via `<JELLYFIN_API_KEY>`."""
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    title = str((node or {}).get("title") or "")
    if not (_ORGANIZE_RE.search(title) and _JELLYFIN_RE.search(text) and re.search(r"\bjellyfin\b", text, re.I)):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("jellyfin", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_ORGANIZE_JELLYFIN_PY}" "<JELLYFIN_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1512 — the verify reads the TYPE off the FILESYSTEM, not the API: Jellyfin's key is
    # readable-not-stored, so `$JELLYFIN_API_KEY` in a check becomes the in-guest `$(python3 …)`
    # read (§17.1500) the read-only verify channel refuses (§17.1390). Jellyfin records a library's
    # type as a `<type>.collection` MARKER FILE in its folder (measured live: `movies.collection`,
    # `tvshows.collection`) — NOT in options.xml (§17.1511 grepped there and witnessed nothing, so
    # the step passed on exit code alone). Listing `*.collection` per library witnesses the real
    # typing, and listing the folders shows the mixed library is gone — no key, no interpreter, no `$( )`.
    check = (f"pct exec {gid} -- sh -c {_sq_payload('ls -1 /var/lib/jellyfin/root/default/*/*.collection 2>/dev/null; echo ---libraries---; ls -1 /var/lib/jellyfin/root/default/')}")
    note = [{"why": (
        f"drafted the Jellyfin layout from the engine's own shape (§17.1510): inside guest {gid} it types the "
        f"Movies library (movies → /media/movies) and the TV Shows library (tvshows → /media/tv), removes any "
        f"leftover mixed library over /media, and triggers a scan so posters and metadata populate — through "
        f"Jellyfin's own VirtualFolders API, library config only, no media files touched.")}]
    return [run], [check], note


def _node_text(node: Optional[dict]) -> str:
    """§17.1552 — the step's whole text: title, description, prompt_template AND an MCP node's own
    command (``tool_config.args.command``). A planner-written MCP verify step ("Verify: Name. (split
    from T7)") names its service only in the command (``X-Emby-Token`` / ``:8096``), so a detector that
    read the prose alone could not tell which app it checks."""
    n = node or {}
    parts = [str(n.get(k) or "") for k in ("title", "description", "prompt_template")]
    cfg = _mcp_cfg(n)
    args = cfg.get("args") if isinstance(cfg.get("args"), dict) else {}
    parts.append(str((args or {}).get("command") or ""))
    # §17.1573 — a `write_file` node's path and content are its text too (the unit it writes names the table it loads)
    parts.append(str((args or {}).get("path") or ""))
    parts.append(str((args or {}).get("content") or "")[:2000])
    return " ".join(parts)


#: §17.1552 — Sonarr renames every episode file into the library's canonical shape and moves it into
#: its season folder, through Sonarr's own API: turn on `renameEpisodes` (keeping the configured
#: formats), set `seasonFolder` on each series, then ONE `RenameSeries` command, polled to completion.
#: Measured live (CT 104, 2026-10-14): `renameEpisodes: false`, so every import kept its scene name in
#: a FLAT series folder — `Power.Rangers.Super.Megaforce.S01E19…mkv` beside `…Beast.Morphers.S26E01…`
#: under /media/tv/Power Rangers — and Jellyfin read S01E19 as Mighty Morphin season 1. Idempotent
#: (a renaming library is left alone); media files are MOVED/renamed, never deleted. Single-quoted
#: Python only; runs inside the Sonarr guest; argv: KEY, [dry].
_SONARR_RENAME_PY = (
    "import sys,json,time,urllib.request\n"
    "BASE='http://127.0.0.1:8989'\n"
    "KEY=sys.argv[1]; DRY=len(sys.argv)>2 and sys.argv[2]=='dry'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Api-Key':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=15); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "nm=api('/api/v3/config/naming') or {}\n"
    "if nm.get('renameEpisodes'):\n"
    "    print('OK: naming already renames episodes (%s)' % nm.get('standardEpisodeFormat'))\n"
    "elif DRY:\n"
    "    print('WOULD SET: renameEpisodes=true (format %s)' % nm.get('standardEpisodeFormat'))\n"
    "else:\n"
    "    nm['renameEpisodes']=True\n"
    "    api('/api/v3/config/naming/%s' % nm.get('id',1),'PUT',nm)\n"
    "    print('SET: renameEpisodes=true (episode format %s; season folder %s)' % (nm.get('standardEpisodeFormat'),nm.get('seasonFolderFormat')))\n"
    "series=api('/api/v3/series') or []\n"
    "ids=[]\n"
    # §17.1267 — a bounded slice at timeout=15: 10 x 15 s = 150 s inside the 180 s budget; a library
    # with more series takes a second (idempotent) run, said in the output.
    "for s in series[:10]:\n"
    "    t=s.get('title') or ''\n"
    "    ids.append(s.get('id'))\n"
    "    if s.get('seasonFolder'):\n"
    "        print('OK: %s already uses season folders' % t); continue\n"
    "    if DRY:\n"
    "        print('WOULD SET: %s seasonFolder=true' % t); continue\n"
    "    try:\n"
    "        s['seasonFolder']=True\n"
    "        api('/api/v3/series/%s' % s.get('id'),'PUT',s)\n"
    "        print('SET: %s seasonFolder=true' % t)\n"
    "    except Exception as e:\n"
    "        print('FAILED %s: %r' % (t,e))\n"
    "if len(series)>10:\n"
    "    print('HELD: %d more series beyond the 10 this run takes -- run the step again for the rest' % (len(series)-10))\n"
    "if DRY:\n"
    "    print('RESULT: dry run, nothing renamed'); sys.exit(0)\n"
    "cmd=api('/api/v3/command','POST',{'name':'RenameSeries','seriesIds':ids}) or {}\n"
    "cid=cmd.get('id'); st=cmd.get('status') or 'queued'\n"
    # the rename is Sonarr's own background command: poll it to a terminal state, bounded (8 x 15 s).
    "for i in range(8):\n"
    "    if st in ('completed','failed','aborted'): break\n"
    "    time.sleep(15)\n"
    "    try:\n"
    "        st=(api('/api/v3/command/%s' % cid) or {}).get('status') or st\n"
    "    except Exception as e:\n"
    "        st='unknown (%r)' % e\n"
    "print('RENAME: Sonarr command %s is %s for %d series' % (cid,st,len(ids)))\n"
    "if st=='failed': sys.exit('RESULT: rename FAILED -- see Sonarr System > Events')\n"
    "print('RESULT: rename %s -- episode files now live at Series/Season N/Series - SxxEyy - Episode Title' % st)\n"
)
#: the step whose work is "rename/organize the episode files" (Sonarr's job, not Jellyfin's).
_RENAME_TITLE_RE = re.compile(r"\b(?:rename|re-?organi[sz]e|organi[sz]e|restructure|normali[sz]e|standardi[sz]e|tidy)\b", re.I)
_RENAME_TEXT_RE = re.compile(r"\bsonarr\b|\bseason\s*folders?\b|\bepisode\s*files?\b|\bfile\s*nam(?:es?|ing)\b|\brenam(?:e|ing)\b", re.I)


def rewrite_sonarr_rename_files(node: Optional[dict], commands: list[str], verify: list[str],
                                services: Optional[list] = None,
                                inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1552 — a "rename/organize the episode files into season folders" step makes Sonarr do
    it, deterministically, through its own API: `renameEpisodes=true`, `seasonFolder=true` on every
    series, one `RenameSeries` command polled to completion.

    Live (Jellyfin metadata job 263e920a, 2026-10-14): the operator saw the TV library "not broken
    down into seasons / episode names" and wrong posters. Measured on CT 104, Sonarr's naming config
    had `renameEpisodes: false` and the series folders were FLAT scene names — Power Rangers held
    `Super.Megaforce.S01E19` beside `Beast.Morphers.S26E01` — so Jellyfin parsed the scene numbers
    and identified the wrong episodes. No Jellyfin refresh can fix a file that names the wrong
    episode; the rename is the first step, and it is Sonarr's to do (it knows each file's real
    season/episode). Key read on the machine via `<SONARR_API_KEY>` (§17.1382). Not fired on a
    Jellyfin-only organize step (§17.1510's) — that one names no Sonarr/season-folder/file work."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if not (_RENAME_TITLE_RE.search(title) and _RENAME_TEXT_RE.search(text)):
        return list(commands or []), list(verify or []), []
    if re.search(r"\bjellyfin\b", title, re.I) and not re.search(r"\bsonarr\b", text, re.I):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("sonarr", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_SONARR_RENAME_PY}" "<SONARR_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1505 — the verify is a plain authenticated READ of the setting the run changes (the stored
    # key `$SONARR_API_KEY` is host-expanded by run_verify; no `$( )`, no interpreter): it prints
    # Sonarr's naming config, so `renameEpisodes` is witnessed as a value, not inferred from exit 0.
    check = (f'pct exec {gid} -- curl -s --fail-with-body -H "X-Api-Key: $SONARR_API_KEY" '
             f'"http://127.0.0.1:8989/api/v3/config/naming"')
    note = [{"why": (
        f"drafted the rename from the engine's own shape (§17.1552): inside guest {gid} it turns on Sonarr's "
        f"episode renaming (keeping the configured formats), sets season folders on every series, and issues ONE "
        f"RenameSeries command, polled to completion — Sonarr knows each file's real season and episode, so the "
        f"files land as Series/Season N/Series - SxxEyy - Title and Jellyfin can identify them. Measured: "
        f"renameEpisodes was off and the series folders held flat scene names (Power Rangers Super Megaforce "
        f"S01E19 read as season 1). Files are moved and renamed, never deleted.")}]
    return [run], [check], note


#: §17.1553 — Jellyfin re-scans and re-identifies every typed library (Movies/TV) with a recursive
#: refresh whose mode the step names: ReplaceAllMetadata, ReplaceAllImages, or both. Prints a BEFORE
#: census per library (items lacking a name, overview or poster) so the run records what it started
#: from. Single-quoted Python only; runs inside the Jellyfin guest; argv: KEY, MODE, [dry].
_JELLYFIN_REFRESH_PY = (
    "import sys,json,urllib.request\n"
    "BASE='http://127.0.0.1:8096'\n"
    "KEY=sys.argv[1]; MODE=sys.argv[2] if len(sys.argv)>2 else 'both'; DRY=len(sys.argv)>3 and sys.argv[3]=='dry'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=20); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "libs=[l for l in (api('/Library/VirtualFolders') or []) if l.get('CollectionType') in ('movies','tvshows')]\n"
    "meta='FullRefresh' if MODE in ('metadata','both') else 'None'\n"
    "img='FullRefresh' if MODE in ('images','both') else 'None'\n"
    "q='Recursive=true&MetadataRefreshMode=%s&ImageRefreshMode=%s&ReplaceAllMetadata=%s&ReplaceAllImages=%s' % "
    "(meta,img,str(meta=='FullRefresh').lower(),str(img=='FullRefresh').lower())\n"
    # §17.1267 — 8 libraries x timeout=20 = 160 s inside the budget; each call guarded (§17.1274).
    "for l in libs[:8]:\n"
    "    name=l.get('Name'); lid=l.get('ItemId')\n"
    "    try:\n"
    "        d=api('/Items?ParentId=%s&Recursive=true&IncludeItemTypes=Series,Season,Episode,Movie&Fields=Overview&EnableImageTypes=Primary&ImageTypeLimit=1' % lid) or {}\n"
    "        items=d.get('Items') or []\n"
    "        lack=[i for i in items if not (i.get('Name') and i.get('Overview') and (i.get('ImageTags') or {}).get('Primary'))]\n"
    "        print('BEFORE %s: %d items, %d lacking a name, overview or poster' % (name,len(items),len(lack)))\n"
    "    except Exception as e:\n"
    "        print('BEFORE %s: census failed %r' % (name,e))\n"
    "    if DRY:\n"
    "        print('WOULD REFRESH: %s (%s)' % (name,q)); continue\n"
    "    try:\n"
    "        api('/Items/%s/Refresh?%s' % (lid,q),'POST')\n"
    "        print('REFRESH QUEUED: %s (metadata=%s images=%s)' % (name,meta,img))\n"
    "    except Exception as e:\n"
    "        print('FAILED %s: %r' % (name,e))\n"
    "if not libs:\n"
    "    print('RESULT: no typed Movies/TV library -- organize the libraries first'); sys.exit(1)\n"
    "print('RESULT: %s refresh queued for %d librar%s -- Jellyfin re-scans the folders and re-identifies every item; "
    "the wait/verify step reports completion' % (MODE,len(libs),'y' if len(libs)==1 else 'ies'))\n"
)
_JF_REFRESH_TITLE_RE = re.compile(r"\b(?:refresh|re-?fetch|re-?scan|scan|update|pull|download|populate|fix|repair|replace|trigger)\b", re.I)
_JF_METADATA_RE = re.compile(r"\bmetadata\b|\bposters?\b|\bartwork\b|\bimages?\b|\bcover\s*art\b|\boverviews?\b", re.I)
_JF_SERVICE_RE = re.compile(r"\bjellyfin\b|\bemby\b|x-emby-token|:8096\b", re.I)
#: a step that READS or WAITS is never the refresh: those go to §17.1554.
_JF_OBSERVE_TITLE_RE = re.compile(r"\b(?:verify|check|confirm|validate|wait|poll|monitor|report|enumerate|list|audit|inventory|until)\b", re.I)


def _jellyfin_refresh_mode(text: str) -> str:
    """Which halves the step asks for: the explicit ReplaceAll flags first, then the words."""
    meta = bool(re.search(r"ReplaceAllMetadata\s*=\s*true", text, re.I))
    img = bool(re.search(r"ReplaceAllImages\s*=\s*true", text, re.I))
    if meta and not img:
        return "metadata"
    if img and not meta:
        return "images"
    if meta and img:
        return "both"
    words_img = bool(re.search(r"\bposters?\b|\bartwork\b|\bimages?\b|\bcover\s*art\b", text, re.I))
    words_meta = bool(re.search(r"\bmetadata\b|\boverviews?\b|\bepisode\s*names?\b|\btitles?\b", text, re.I))
    if words_img and not words_meta:
        return "images"
    if words_meta and not words_img:
        return "metadata"
    return "both"


def rewrite_refresh_jellyfin_metadata(node: Optional[dict], commands: list[str], verify: list[str],
                                      services: Optional[list] = None,
                                      inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1553 — a "refresh the Jellyfin metadata/images" step queues Jellyfin's own recursive
    refresh on every typed library, deterministically, with the ReplaceAll flags the step names.

    Live (job 263e920a, T4/T5): the planner wrote `for lib in ${T2}; do curl -s -X POST -H
    'X-Emby-Token: ${T1}' …/Items/$lib/Refresh…` — bare placeholders nothing renders, a key spliced
    in from another node's output (which would have inlined it into the sudo log), `curl -s` that
    cannot report a 401, no check. The engine knows the whole shape: read the key on the machine
    (`<JELLYFIN_API_KEY>`), list the typed libraries through VirtualFolders, and POST
    `/Items/{ItemId}/Refresh?Recursive=true&…ReplaceAll…=true` per library — the same call the
    Jellyfin UI's "Scan library"/"Refresh metadata" makes, so it also picks up files Sonarr renamed
    (§17.1552). Mode from the step text: ReplaceAllMetadata → metadata, ReplaceAllImages → images,
    both or unspecific → both. A verify/wait step is §17.1554's, not this."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if not (_JF_REFRESH_TITLE_RE.search(title) and _JF_METADATA_RE.search(text) and _JF_SERVICE_RE.search(text)):
        return list(commands or []), list(verify or []), []
    if _JF_OBSERVE_TITLE_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("jellyfin", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    mode = _jellyfin_refresh_mode(text)
    payload = f'python3 -c "{_JELLYFIN_REFRESH_PY}" "<JELLYFIN_API_KEY>" "{mode}"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1512 — Jellyfin's key is readable-not-stored, so the check carries no key, no interpreter
    # and no `$( )`: it witnesses the server answering (an unauthenticated public read, HTTP 200 only
    # when Jellyfin is up) and the refresh landing in Jellyfin's own log ("… will be refreshed" lines
    # per day-file) — what the API call left behind on the machine, not the call's exit code.
    check_payload = ('curl -s -o /dev/null -w "jellyfin HTTP %{http_code}" http://127.0.0.1:8096/System/Info/Public; '
                     'echo; grep -c "will be refreshed" /var/log/jellyfin/*.log')
    check = f"pct exec {gid} -- sh -c {_sq_payload(check_payload)}"
    note = [{"why": (
        f"drafted the refresh from the engine's own shape (§17.1553): inside guest {gid} it reads the key on the "
        f"machine, lists Jellyfin's typed Movies/TV libraries and queues a recursive {mode} refresh on each with "
        f"the ReplaceAll flag(s) set — the call the Jellyfin UI makes for 'Refresh metadata' — after printing how "
        f"many items in each library lacked a name, overview or poster before it ran. The draft spliced another "
        f"step's output in as `${{T1}}`/`${{T2}}`, which nothing renders and which would put the key in the sudo log.")}]
    return [run], [check], note


#: §17.1554 — the per-item census of Jellyfin's typed libraries: for every Series, Season, Episode and
#: Movie, does it have a Name, an Overview and a Primary image? Prints a per-library/per-type table, the
#: first 40 lacking items by name, and a RESULT line. In `wait`/`last` mode it first decides whether the
#: refresh is still running (an item was refreshed in the last 3 minutes → poll twice more, 40 s apart;
#: still moving → `wait` prints and exits 0 so the next phase continues, `last` exits 1 so the step
#: FAILS honestly). Single-quoted Python only; runs inside the Jellyfin guest; argv: KEY, [wait|last].
_JELLYFIN_CENSUS_PY = (
    "import sys,json,time,urllib.request\n"
    "from datetime import datetime,timezone\n"
    "BASE='http://127.0.0.1:8096'\n"
    "KEY=sys.argv[1]; PHASE=sys.argv[2] if len(sys.argv)>2 else 'report'\n"
    "def api(p):\n"
    "    r=urllib.request.Request(BASE+p,headers={'X-Emby-Token':KEY})\n"
    "    u=urllib.request.urlopen(r,timeout=20); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "def label(i):\n"
    "    t=i.get('Type') or '?'\n"
    "    if t=='Episode': return 'Episode %s S%02dE%02d' % (i.get('SeriesName') or '?', i.get('ParentIndexNumber') or 0, i.get('IndexNumber') or 0)\n"
    "    if t=='Season': return 'Season %s / %s' % (i.get('SeriesName') or '?', i.get('Name') or '?')\n"
    "    return '%s %s' % (t, i.get('Name') or '?')\n"
    "def census():\n"
    "    libs=[l for l in (api('/Library/VirtualFolders') or []) if l.get('CollectionType') in ('movies','tvshows')]\n"
    "    rows=[]; lacking=[]; last=''\n"
    "    for l in libs[:8]:\n"
    "        lid=l.get('ItemId'); nm=l.get('Name')\n"
    "        rows.append('LIBRARY %s type=%s paths=%s' % (nm,l.get('CollectionType'),','.join(l.get('Locations') or [])))\n"
    # what the library's top level resolved to (Series folders in a TV library, Movies in a movie one):
    # the shape the operator sees first, and the first thing wrong when a TV library shows a flat list.
    "        try:\n"
    "            top=(api('/Items?ParentId=%s&Fields=Path' % lid) or {}).get('Items') or []\n"
    "            th={}\n"
    "            for i in top: th[i.get('Type') or '?']=th.get(i.get('Type') or '?',0)+1\n"
    "            rows.append('  top level: %s' % (', '.join('%s x%d' % (k,th[k]) for k in sorted(th)) or 'empty'))\n"
    "            for i in top[:3]: rows.append('  top: %s %s (%s)' % (i.get('Type'),i.get('Name'),i.get('Path')))\n"
    "        except Exception as e:\n"
    "            rows.append('  top level: read failed %r' % e)\n"
    "        try:\n"
    "            d=api('/Items?ParentId=%s&Recursive=true&IncludeItemTypes=Series,Season,Episode,Movie&Fields=Overview,Path,DateLastRefreshed&EnableImageTypes=Primary&ImageTypeLimit=1' % lid) or {}\n"
    "        except Exception as e:\n"
    "            rows.append('%s: read failed %r' % (nm,e)); continue\n"
    "        by={}; shown=0\n"
    "        for i in d.get('Items') or []:\n"
    "            t=i.get('Type') or '?'; c=by.setdefault(t,[0,0,0,0]); c[0]+=1; miss=[]\n"
    "            if not i.get('Name'): c[1]+=1; miss.append('name')\n"
    "            if not i.get('Overview'): c[2]+=1; miss.append('overview')\n"
    "            if not (i.get('ImageTags') or {}).get('Primary'): c[3]+=1; miss.append('poster')\n"
    "            if miss: lacking.append('%s -- no %s' % (label(i),', '.join(miss)))\n"
    "            if miss and shown<2: shown+=1; rows.append('  sample lacking: %s %s (%s)' % (t,i.get('Name'),i.get('Path')))\n"
    "            last=max(last,str(i.get('DateLastRefreshed') or ''))\n"
    "        for t in sorted(by):\n"
    "            c=by[t]; rows.append('%s/%s: %d, missing name %d, overview %d, poster %d' % (nm,t,c[0],c[1],c[2],c[3]))\n"
    # a TV library whose items Jellyfin resolved as Movies (or a movie library full of Episodes) is
    # not a metadata gap -- no refresh can identify a show that was never resolved as one. Said
    # plainly, and the step FAILS on it, so the engine's own failure research takes it from here.
    "        if l.get('CollectionType')=='tvshows' and 'Movie' in by and not ('Series' in by or 'Episode' in by):\n"
    "            wrong.append('%s is a TV library (tvshows) but Jellyfin resolved its %d items as Movie and none as Series/Episode -- the files are not being identified as shows, so no metadata refresh can name their episodes' % (nm,by['Movie'][0]))\n"
    "        if l.get('CollectionType')=='movies' and 'Episode' in by and 'Movie' not in by:\n"
    "            wrong.append('%s is a movie library but Jellyfin resolved its items as Episodes' % nm)\n"
    "    return rows,lacking,last\n"
    "wrong=[]\n"
    "def age(last):\n"
    "    try: return (datetime.now(timezone.utc).replace(tzinfo=None)-datetime.strptime(last[:19],'%Y-%m-%dT%H:%M:%S')).total_seconds()\n"
    "    except Exception: return 1e9\n"
    "rows,lacking,last=census()\n"
    "if PHASE in ('wait','last'):\n"
    # the server's own timestamps when it gives them (this Jellyfin returned none for DateLastRefreshed);
    # else the counts: a library still being refreshed changes between two reads 40 s apart.
    "    sig=(tuple(rows),len(lacking)); moving=(age(last)<180) if last else True\n"
    "    for i in range(2):\n"
    "        if not moving: break\n"
    "        time.sleep(40)\n"
    "        try:\n"
    "            rows,lacking,last2=census()\n"
    "        except Exception as e:\n"
    "            print('poll failed %r' % e); continue\n"
    "        sig2=(tuple(rows),len(lacking))\n"
    "        moving=(last2!=last) if (last or last2) else (sig2!=sig); last=last2; sig=sig2\n"
    "    if moving:\n"
    "        print('STILL REFRESHING: last item refreshed %s; %d item(s) still lack something' % (last,len(lacking)))\n"
    "        sys.exit(1 if PHASE=='last' else 0)\n"
    "    print('REFRESH SETTLED: last item refreshed %s' % last)\n"
    "for r in rows: print(r)\n"
    "for x in lacking[:40]: print('LACKING: '+x)\n"
    "if len(lacking)>40: print('... and %d more' % (len(lacking)-40))\n"
    "for w in wrong[-2:]: print('WRONG TYPES: '+w)\n"
    "print('RESULT: COMPLETE -- every series, season, episode and movie has a name, overview and poster' if not lacking "
    "else 'RESULT: %d item(s) still lack a name, overview or poster' % len(lacking))\n"
    "if wrong: sys.exit(2)\n"
)
_JF_WAIT_TITLE_RE = re.compile(r"\b(?:wait|poll|until|monitor)\b", re.I)
#: §17.1643 — the other apps of the media stack; a check titled with one of them is a comparison, not a census
_JF_OTHER_APP_RE = re.compile(r"\b(?:radarr|sonarr|lidarr|readarr|prowlarr|qbittorrent|transmission|deluge|sabnzbd|nzbget)\b", re.I)
_JF_CENSUS_TEXT_RE = re.compile(r"\bmetadata\b|\boverviews?\b|\bprimary\s*image\b|\bposters?\b|\bimages?\b|\bnames?\b|\bitems?\b"
                                r"|\bepisodes?\b|\bseasons?\b|\bseries\b|\brefresh(?:ed|ing|library)?\b|\bscheduledtasks\b|\blibrar(?:y|ies)\b", re.I)


def rewrite_jellyfin_metadata_report(node: Optional[dict], commands: list[str], verify: list[str],
                                     services: Optional[list] = None,
                                     inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1554 — a "verify / enumerate / wait for the Jellyfin metadata" step reads the library
    itself and reports, deterministically: per library and type, how many items lack a Name, an
    Overview or a Primary image, the first 40 by name, and a COMPLETE / N-lacking verdict.

    Live (job 263e920a, T3/T6/T7*): "Enumerate all items" ran a `for lib in ${T2}` loop that the
    runner answered with `/bin/sh: 1: Syntax error: "do" unexpected` — and was marked done; the three
    "Verify: Name / Overview / Primary image presence" checks spliced that output in as `${T3}`. The
    read runs INSIDE the guest so the multi-megabyte /Items answer never crosses the runner's 20 000-
    char cap; only the table and the verdict return. A WAIT step ("Wait for refresh completion",
    "Poll until…") becomes six `wait` phases and a `last`: each first asks whether any item was
    refreshed in the last three minutes, polls twice more when it was, and the `last` phase FAILS the
    step if the refresh is still moving — never a `while true` on the runner's 180 s budget."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if not (_JF_OBSERVE_TITLE_RE.search(title) and _JF_SERVICE_RE.search(text) and _JF_CENSUS_TEXT_RE.search(text)):
        return list(commands or []), list(verify or []), []
    # §17.1643 — a check whose title names ANOTHER app compares Jellyfin with it, which a Jellyfin-only census cannot
    # answer. Live (job 5db1b088 T2 "Verify Movies points to the folder Radarr imports into"): the census claimed it off
    # the splitter's "(split from T2: Verify Jellyfin library paths)" and would have recorded it done without reading Radarr.
    if _JF_OTHER_APP_RE.search(title):
        return list(commands or []), list(verify or []), []
    # §17.1660 — and what the check is ABOUT is in its title: live (job 5db1b088 T24 "Verify shared storage paths"),
    # the census claimed a storage check because its text mentioned Jellyfin's library folders.
    if not _JF_CENSUS_TEXT_RE.search(title):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("jellyfin", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []

    def _phase(p: str) -> str:
        payload = f'python3 -c "{_JELLYFIN_CENSUS_PY}" "<JELLYFIN_API_KEY>" "{p}"'
        return f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    waiting = bool(_JF_WAIT_TITLE_RE.search(title))
    run = [_phase("wait")] * 6 + [_phase("last")] if waiting else [_phase("report")]
    # §17.1512 — no key in the check (readable-not-stored): the public system read witnesses that the
    # server the run read is up and answering, with no interpreter and no `$( )`.
    check = [(f'pct exec {gid} -- curl -s -o /dev/null -w "jellyfin HTTP %{{http_code}}" '
              f'http://127.0.0.1:8096/System/Info/Public')]
    what = ("waits for the refresh to settle (six polls, then a last phase that fails the step if items are still "
            "being refreshed) and then prints" if waiting else "prints")
    note = [{"why": (
        f"drafted the metadata check from the engine's own shape (§17.1554): inside guest {gid} it reads every typed "
        f"library's items through Jellyfin's API and {what} a per-type table of items lacking a name, overview or "
        f"poster, the first 40 by name, and a COMPLETE / N-lacking verdict — the read stays in the guest so the full "
        f"item list never hits the runner's output cap. The draft spliced another step's output in as a shell "
        f"variable nothing sets.")}]
    return run, check, note


#: §17.1562 — rebuild a typed Jellyfin library whose items resolved as the WRONG kind: remove the library
#: (database rows only — removing a library never touches media), wait for the scan that drops its
#: orphaned items, re-add it with the same name/type/paths, and let the fresh scan resolve the files
#: with the right resolvers. Measured live (2026-10-14): Shows was typed tvshows, yet its top level was
#: `Folder x6` and 902 `Movie` items — rows left over from the mixed-content library that had covered
#: /media; Jellyfin reuses an existing item at a path and never re-types it, and §17.1510's re-add did
#: not drop them because the mixed library still covered the paths at that moment.
#: NEVER `DELETE /Items/{id}`: in Jellyfin that endpoint deletes the MEDIA FILES from disk.
#: Single-quoted Python only; runs inside the Jellyfin guest; argv: KEY, [dry].
_REBUILD_JELLYFIN_PY = (
    "import sys,json,time,urllib.request,urllib.parse\n"
    "BASE='http://127.0.0.1:8096'\n"
    "KEY=sys.argv[1]; DRY=len(sys.argv)>2 and sys.argv[2]=='dry'\n"
    "def api(p,method='GET',body=None):\n"
    "    r=urllib.request.Request(BASE+p,method=method,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},"
    "data=json.dumps(body).encode() if body is not None else None)\n"
    "    u=urllib.request.urlopen(r,timeout=15); d=u.read()\n"
    "    return json.loads(d) if d else None\n"
    "def q(s): return urllib.parse.quote(str(s))\n"
    "def scan_idle():\n"
    "    tasks=api('/ScheduledTasks') or []\n"
    "    for t in tasks[:60]:\n"
    "        if t.get('Key')=='RefreshLibrary': return t.get('State')=='Idle'\n"
    "    return True\n"
    "def wait_idle():\n"
    "    for i in range(6):\n"
    "        try:\n"
    "            if scan_idle(): return True\n"
    "        except Exception as e:\n"
    "            print('poll failed %r' % e)\n"
    "        time.sleep(10)\n"
    "    return False\n"
    "EXPECT={'tvshows':'Series','movies':'Movie'}\n"
    "libs=[l for l in (api('/Library/VirtualFolders') or []) if l.get('CollectionType') in EXPECT]\n"
    "wrong=[]\n"
    "for l in libs[:8]:\n"
    "    nm=l.get('Name'); ct=l.get('CollectionType')\n"
    "    try:\n"
    "        top=(api('/Items?ParentId=%s' % l.get('ItemId')) or {}).get('Items') or []\n"
    "    except Exception as e:\n"
    "        print('SKIP %s: top level unreadable %r' % (nm,e)); continue\n"
    "    types=sorted(set(i.get('Type') or '?' for i in top))\n"
    "    bad=[i for i in top if (i.get('Type') or '?')!=EXPECT[ct]]\n"
    "    if top and bad:\n"
    "        print('WRONG TYPES: %s (%s) top level is %s, expected %s' % (nm,ct,', '.join(types),EXPECT[ct])); wrong.append(l)\n"
    "    else:\n"
    "        print('OK: %s (%s) top level is %s' % (nm,ct,', '.join(types) or 'empty'))\n"
    "if not wrong:\n"
    "    print('RESULT: every typed library resolves as its own kind -- nothing to rebuild'); sys.exit(0)\n"
    "if DRY:\n"
    "    for l in wrong[:4]: print('WOULD REBUILD: %s -> remove, scan, re-add as %s over %s' % (l.get('Name'),l.get('CollectionType'),','.join(l.get('Locations') or [])))\n"
    "    print('RESULT: dry run, nothing changed'); sys.exit(0)\n"
    # §17.1267 — at most 4 libraries; every call timeout=15; the two waits are bounded (6 x 10 s each).
    "for l in wrong[:4]:\n"
    "    nm=l.get('Name'); ct=l.get('CollectionType'); paths=l.get('Locations') or []\n"
    "    try:\n"
    "        api('/Library/VirtualFolders?name=%s&refreshLibrary=true' % q(nm),'DELETE')\n"
    "        print('REMOVED library %s (rows only; media untouched) -- scan drops its stale items' % nm)\n"
    "    except Exception as e:\n"
    "        print('FAILED to remove %s: %r' % (nm,e)); continue\n"
    "    idle=wait_idle()\n"
    "    print('scan after removal: %s' % ('idle' if idle else 'still running after 60 s'))\n"
    "    try:\n"
    "        api('/Library/VirtualFolders?name=%s&collectionType=%s&paths=%s&refreshLibrary=true' % (q(nm),ct,'&paths='.join(q(p) for p in paths)),'POST',{'LibraryOptions':{}})\n"
    "        print('RE-ADDED library %s as %s over %s -- fresh scan resolves the files with the %s resolvers' % (nm,ct,','.join(paths),ct))\n"
    "    except Exception as e:\n"
    "        print('FAILED to re-add %s: %r -- re-add it by hand: name %s, type %s, paths %s' % (nm,e,nm,ct,','.join(paths))); sys.exit(1)\n"
    "idle=wait_idle()\n"
    "print('RESULT: %d librar%s rebuilt; scan %s -- the verify step reads the new item types' % (len(wrong[:4]),'y' if len(wrong[:4])==1 else 'ies','finished' if idle else 'still running'))\n"
)
#: the step that asks for a library's items to be re-resolved / the library rebuilt.
_JF_REBUILD_RE = re.compile(
    r"\bre-?resolve\b|\brebuild\b|\bre-?create\b|\bre-?add\b|\bwrong\s+types?\b|\bstale\s+(?:items?|rows?|entries)\b"
    r"|\bresolved?\s+(?:its\s+|the\s+|their\s+)?(?:content|items|files|episodes|shows)?\s*as\s+(?:plain\s+)?(?:folders?|movies?)\b"
    r"|\bresolve\s+as\s+series\b", re.I)


def rewrite_rebuild_jellyfin_library(node: Optional[dict], commands: list[str], verify: list[str],
                                     services: Optional[list] = None,
                                     inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1562 — a "make the Jellyfin library re-resolve its files / rebuild the library whose items
    came out as the wrong type" step does it deterministically: remove the mistyped library, wait for
    the scan that drops its orphaned rows, re-add it typed over the same paths, scan.

    Live (job 263e920a, FIX1): the census (§17.1554b) failed honestly — Shows typed tvshows, top level
    `Folder x6`, 902 `Movie` items — and the fix step was left to the model with the engine's research
    attached (§17.1561). Three drafts: `systemctl stop jellyfin` + `sqlite3` SELECTs (sqlite3 is not
    installed there; `set -e` would have left Jellyfin stopped), then exploratory `find`/`jq` with the
    key read from a file that has none. The research itself found no source for this exact shape. The
    fix others use for a mis-resolved library is remove-and-re-add; the engine owns that shape already
    (§17.1510) and knows the condition exactly (the census). Media files are never touched — and
    `DELETE /Items/{id}` is never used, because in Jellyfin that endpoint deletes the files."""
    text = _node_text(node)
    if not (_JF_REBUILD_RE.search(text) and _JF_SERVICE_RE.search(text) and re.search(r"\blibrar(?:y|ies)\b", text, re.I)):
        return list(commands or []), list(verify or []), []
    gid = _arr_guest("jellyfin", services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    payload = f'python3 -c "{_REBUILD_JELLYFIN_PY}" "<JELLYFIN_API_KEY>"'
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    # §17.1512 — the type MARKER files witness the re-added libraries exist and are typed (no key, no
    # interpreter, no `$( )`); the new item types are the census step's to read.
    check = (f"pct exec {gid} -- sh -c {_sq_payload('ls -1 /var/lib/jellyfin/root/default/*/*.collection 2>/dev/null; echo ---libraries---; ls -1 /var/lib/jellyfin/root/default/')}")
    note = [{"why": (
        f"drafted the rebuild from the engine's own shape (§17.1562): inside guest {gid} it reads each typed library's "
        f"top level through Jellyfin's API; a TV library whose top level is not Series items (or a movie library whose "
        f"is not Movies) is REMOVED (database rows only — media files are never touched), the scan that drops its "
        f"orphaned rows is waited for, and it is RE-ADDED with the same name, type and paths so the fresh scan resolves "
        f"the files with the right resolvers. A library that already resolves as its own kind is left alone.")}]
    return [run], [check], note


#: §17.1563 — a guest whose service cannot reach a public API because the guest HOLDS a global IPv6
#: address that routes nowhere: glibc orders AAAA first, .NET follows it, and every call waits out the
#: full timeout. Measured live (CT 101, 2026-10-14): `getent ahosts api.themoviedb.org` listed eight
#: 2600:… addresses before 3.170.…; `curl -6` → no answer, `curl -4` → 401 (reachable); Jellyfin's log
#: carried 67 `TmdbClientManager.EnsureClientConfigAsync … HttpClient.Timeout of 100 seconds` traces that
#: day — so no overview or poster could ever land, whatever the library did. §17.1532/1534 fixed the same
#: class for Prowlarr INLINE in the indexer template; this is the capability. The precedence line makes
#: IPv4 win in getaddrinfo (RFC 6724 override), the unit is restarted because glibc reads gai.conf once
#: per process. Reversible: one line in /etc/gai.conf.
_GAI_PRECEDENCE = "precedence ::ffff:0:0/96  100"
_PREFER_IPV4_RE = re.compile(
    r"\bipv6\b|\bipv4\b|\bgai\.conf\b|\bprefer\s+ipv4\b|\btimes?\s*out\b|\btime-?outs?\b|\bcannot\s+reach\b|\bcan'?t\s+reach\b"
    r"|\bunreachable\b|\bno\s+route\b|\bhttpclient\.timeout\b", re.I)
_PUBLIC_API_RE = re.compile(r"\b(?:tmdb|themoviedb|tvdb|omdb|fanart|musicbrainz|github|public\s+api|external\s+api|metadata\s+provider)\b", re.I)
_PREFER_IPV4_APPS = ("jellyfin", "prowlarr", "sonarr", "radarr", "lidarr", "readarr", "bazarr", "qbittorrent")
_PROBE_HOSTS = {"jellyfin": "api.themoviedb.org", "sonarr": "skyhook.sonarr.tv", "radarr": "api.radarr.video",
                "prowlarr": "prowlarr.servarr.com", "lidarr": "api.lidarr.audio", "readarr": "api.bookinfo.club"}


def rewrite_prefer_ipv4_in_guest(node: Optional[dict], commands: list[str], verify: list[str],
                                 services: Optional[list] = None,
                                 inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1563 — a "make <service> reach <public API> / it times out / prefers IPv6" step prefers IPv4
    in that service's guest, deterministically: append the RFC 6724 precedence line to /etc/gai.conf
    (once), restart the unit so glibc re-reads it, and show the unit active.

    Live (job 263e920a): after the Sonarr rename (§17.1552) and the library rebuild (§17.1562) the TV
    library finally resolved as 6 Series / 46 Seasons / 902 Episodes — with 0 overviews and 0 posters,
    because Jellyfin's TMDb client had been timing out on IPv6 since midnight. The verify reads what
    changed back off the machine: the precedence line, which family `getent` now orders first, and the
    provider host answering over the default stack (read-only: grep, getent, curl GET to /dev/null)."""
    text = _node_text(node)
    if not (_PREFER_IPV4_RE.search(text) and (_PUBLIC_API_RE.search(text) or re.search(r"\bipv6\b|\bgai\.conf\b", text, re.I))):
        return list(commands or []), list(verify or []), []
    # §17.1563c — a VERIFY/CHECK step is a read ("Verify prowlarr IPv4 first"): never the fix, which would
    # restart the unit once more per check. Live plan 977a8a38: 6 appends, 6 restarts, 6 verifies.
    if _JF_OBSERVE_TITLE_RE.search(str((node or {}).get("title") or "")):
        return list(commands or []), list(verify or []), []
    # §17.1570 — a step that DISABLES IPv6 is that capability's, not a preference tweak
    if _DISABLE_IPV6_RE.search(text):
        return list(commands or []), list(verify or []), []
    # §17.1563b — any guest the step names: a known app, a MEASURED service (name + unit), an inventory name,
    # or "container N" with the unit measured on that guest (111 control-panel, 120 caddy, 130 pihole are not
    # *arr apps and the same dead IPv6 route hit them). §17.1570c — read off the step's OWN words.
    app, gid, unit = _named_guest(_own_step_text(node), services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    if not unit:
        return list(commands or []), list(verify or []), []           # no unit to restart → not this rule's call
    m = re.search(r"\b((?:[a-z0-9-]+\.)+(?:org|com|net|io|tv|club|video|audio))\b", text, re.I)
    host = m.group(1) if m and not m.group(1).startswith("127.") else _PROBE_HOSTS.get(app, "api.themoviedb.org")
    run_payload = (f"grep -qxF '{_GAI_PRECEDENCE}' /etc/gai.conf 2>/dev/null || echo '{_GAI_PRECEDENCE}' >> /etc/gai.conf; "
                   f"echo 'gai.conf:'; grep -n 'precedence ::ffff:0:0/96' /etc/gai.conf; "
                   f"systemctl restart {unit} && sleep 5 && systemctl is-active {unit} && "
                   f"echo 'RESULT: {unit} restarted with IPv4 preferred for getaddrinfo'")
    run = f"pct exec {gid} -- sh -c {_sq_payload(run_payload)}"
    check_payload = (f'grep -c "{_GAI_PRECEDENCE}" /etc/gai.conf; getent ahosts {host} | head -n 1; '
                     f'curl -s -m 8 -o /dev/null -w "{host} HTTP %{{http_code}}" https://{host}/; systemctl is-active {unit}')
    check = f"pct exec {gid} -- sh -c {_sq_payload(check_payload)}"
    note = [{"why": (
        f"drafted the fix from the engine's own shape (§17.1563): inside guest {gid} it makes IPv4 win in "
        f"getaddrinfo (one `{_GAI_PRECEDENCE}` line in /etc/gai.conf, written once) and restarts {unit} so it "
        f"re-reads the policy. Measured: the guest holds a global IPv6 address with no working route, glibc listed "
        f"{host}'s AAAA records first, and {app}'s HTTP client waited out its timeout on every call — curl -6 got no "
        f"answer while curl -4 did. The check reads the line back, shows which family `getent` orders first, and "
        f"asks {host} over the default stack.")}]
    return [run], [check], note


def _own_step_text(node: Optional[dict]) -> str:
    """§17.1570c — the step's OWN words, without the `[as in T<n>: …]` text it inherited: the guest a
    step names is read here (T5 is about container 102 even though it says "same as T2", whose text
    names 101), while detection reads the whole text."""
    return re.sub(r"\[as in T\d+(?:v\d+)?: .*?\]", " ", _node_text(node), flags=re.S)


def _named_guest(text: str, services: Optional[list], inventory: Optional[dict]) -> tuple[str, str, str]:
    """§17.1570 — the guest a step names, as ``(name, gid, unit)``: a known app, a MEASURED service
    (name + unit), an inventory name, or "container N". ``unit`` is "" when none was measured."""
    for a in _PREFER_IPV4_APPS:
        if re.search(rf"\b{a}\b", text, re.I):
            gid = _arr_guest(a, services, inventory)
            if gid:
                return a, gid, f"{a}.service"
    for s in services or []:
        nm = str(getattr(s, "name", "") or "").lower()
        if nm and re.search(rf"\b{re.escape(nm)}\b", text, re.I) and getattr(s, "guest", ""):
            return nm, str(getattr(s, "guest")), str(getattr(s, "unit", "") or "")
    names = (inventory or {}).get("names") or {}
    m_ct = re.search(r"\b(?:container|ct|lxc)\s*(\d{3})\b", text, re.I)
    for g, nm in names.items():
        nm_s = str(nm or "")
        if (nm_s and re.search(rf"\b{re.escape(nm_s)}\b", text, re.I)) or (m_ct and str(g) == m_ct.group(1)):
            unit = next((str(getattr(s, "unit", "") or "") for s in services or []
                         if str(getattr(s, "guest", "")) == str(g) and getattr(s, "unit", "")), "")
            return nm_s.lower() or f"guest {g}", str(g), unit
    if m_ct:
        return f"guest {m_ct.group(1)}", m_ct.group(1), ""
    return "", "", ""


#: §17.1570 — switch IPv6 OFF in a guest whose IPv6 route is dead: a persisted sysctl.d drop-in that
#: disables IPv6 on the LAN interface and on interfaces created later, and stops accepting router
#: advertisements, applied live. Loopback is left alone, so a service bound to ::1 keeps working.
#: Measured (CT 101, 2026-10-15): net0 carries no ip6 setting, `eth0` has accept_ra=1 autoconf=1 and
#: two SLAAC addresses from the gateway's RAs — the cause behind §17.1563's symptom.
_DISABLE_IPV6_FILE = "/etc/sysctl.d/99-scaffold-disable-ipv6.conf"
#: §17.1570b — and the shapes a PLANNER writes for the same work: the sysctl key itself (`disable_ipv6`,
#: `net.ipv6.conf.…`), the drop-in's name, "sysctl … ipv6". Live (job eff80f98, T2 "Write sysctl config
#: jellyfin": "Contents: net.ipv6.conf.eth0.disable_ipv6=1, …"): the phrase regex saw no word boundary inside
#: `disable_ipv6`, so §17.1563's prefer-IPv4 block claimed the step instead.
_DISABLE_IPV6_RE = re.compile(r"\b(?:disabl(?:e|es|ed|ing)|turn(?:s|ed|ing)?\s+off|switch(?:es|ed|ing)?\s+off|stop\s+(?:accepting|using))\b[^.;\n]{0,40}\bipv6\b"
                              r"|\bipv6\b[^.;\n]{0,30}\b(?:off|disabled)\b|\bno\s+ipv6\b"
                              r"|\bdisable_ipv6\b|\bnet\.ipv6\.conf\.|disable-ipv6\.conf\b|\bsysctl\b[^.;\n]{0,60}\bipv6\b", re.I)


def _ipv6_state_check(gid: str, iface: str) -> str:
    """§17.1570g — the read that settles the IPv6-off claim with one verdict word: the LAN interface off,
    router advertisements refused, loopback still on, the drop-in present."""
    check_payload = (f"echo {iface}.disable_ipv6=$(cat /proc/sys/net/ipv6/conf/{iface}/disable_ipv6) "
                     f"{iface}.accept_ra=$(cat /proc/sys/net/ipv6/conf/{iface}/accept_ra) "
                     f"lo.disable_ipv6=$(cat /proc/sys/net/ipv6/conf/lo/disable_ipv6) "
                     f"all.disable_ipv6=$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6); "
                     f"echo global_ipv6_on_{iface}=$(ip -6 addr show dev {iface} scope global 2>/dev/null | grep -c inet6); "
                     f"echo loopback_::1=$(ip -6 addr show dev lo 2>/dev/null | grep -c ::1); cat {_DISABLE_IPV6_FILE}; "
                     f"test \"$(cat /proc/sys/net/ipv6/conf/{iface}/disable_ipv6)\" = 1 && "
                     f"test \"$(cat /proc/sys/net/ipv6/conf/lo/disable_ipv6)\" = 0 && echo STATE-OK || echo STATE-WRONG")
    return f"pct exec {gid} -- sh -c {_sq_payload(check_payload)}"


def rewrite_verify_ipv6_disabled(node: Optional[dict], commands: list[str], verify: list[str],
                                 services: Optional[list] = None,
                                 inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1570h — a "Verify IPv6 disabled on <guest>" checkpoint is the engine's own read, settled by a
    verdict word, not a model draft judged by a model.

    Live (job eff80f98): T4 (model-drafted) died on `grep -c inet6` exiting 1 for the DESIRED count of
    zero under `set -e`; T10 and T16 printed the right state and were judged "IPv6 not disabled on
    all/lo" — the judge read "Verify IPv6 disabled" as every interface while the step only ever
    claimed eth0. Both the run and the check are the same read-only state probe (§17.1570g), the
    expectation is STATE-OK, so the pre-pass, the post-run judge and the operator all read one word."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if not (_JF_OBSERVE_TITLE_RE.search(title) and _DISABLE_IPV6_RE.search(text) or
            (_JF_OBSERVE_TITLE_RE.search(title) and re.search(r"\bipv6\b", text, re.I) and re.search(r"\bdisabled?\b|\boff\b", text, re.I))):
        return list(commands or []), list(verify or []), []
    if re.search(r"\bloopback\b|\blo\b", title, re.I) and not re.search(r"\beth\d|\bens\d", text):
        return list(commands or []), list(verify or []), []          # the loopback-intact check is its own step
    own = _own_step_text(node)
    name, gid, _unit = _named_guest(own, services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    m_if = re.search(r"\b(eth\d+|ens\d+|enp\w+|net\d+)\b", own) or re.search(r"\b(eth\d+|ens\d+|enp\w+|net\d+)\b", text)
    iface = m_if.group(1) if m_if else "eth0"
    check = _ipv6_state_check(gid, iface)
    note = [{"expect": {check: "STATE-OK"}, "why": (
        f"drafted the check from the engine's own shape (§17.1570h): inside guest {gid} ({name}) it prints {iface}'s "
        f"disable_ipv6 and accept_ra, loopback's disable_ipv6, the count of global IPv6 addresses left on {iface}, "
        f"whether ::1 is present, the drop-in, and one verdict word — STATE-OK only when {iface} is off AND loopback "
        f"is still on. A read, settled by that word; a count of zero is the goal, not a failure.")}]
    return [check], [check], note


def rewrite_disable_ipv6_in_guest(node: Optional[dict], commands: list[str], verify: list[str],
                                  services: Optional[list] = None,
                                  inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1570 — a "disable IPv6 on container N / in <service>'s guest" step does it deterministically:
    writes ``/etc/sysctl.d/99-scaffold-disable-ipv6.conf`` (eth0 + default disable_ipv6, accept_ra and
    autoconf off), applies it with ``sysctl -p``, and prints the live state. Reversible: delete the file
    and reboot (or set the sysctls back). No service restart, no interface reset.

    Operator (2026-10-15), after §17.1563 had treated the symptom on seven guests: "disable IPv6 on the
    LXCs through the engine" — remove the cause (a dead IPv6 route from the gateway's advertisements)
    rather than keep preferring around it. The verify reads the change back: the sysctl value, the count
    of global IPv6 addresses left, and the drop-in's contents."""
    text = _node_text(node)
    if not _DISABLE_IPV6_RE.search(text):
        return list(commands or []), list(verify or []), []
    if _JF_OBSERVE_TITLE_RE.search(str((node or {}).get("title") or "")):
        return list(commands or []), list(verify or []), []
    own = _own_step_text(node)                       # §17.1570c — the guest is the step's own, never an inherited one
    name, gid, _unit = _named_guest(own, services, inventory)
    if not gid:
        return list(commands or []), list(verify or []), []
    m_if = re.search(r"\b(eth\d+|ens\d+|enp\w+|net\d+)\b", own) or re.search(r"\b(eth\d+|ens\d+|enp\w+|net\d+)\b", text)
    iface = m_if.group(1) if m_if else "eth0"
    # §17.1570c — loopback is stated, not assumed: `all` first (resets every interface's flag, undoing a
    # prior blanket disable), then lo explicitly on, then the LAN interface off. Applying this file heals a
    # guest where an earlier draft had set all.disable_ipv6=1 (live: CT 102, ::1 gone).
    body = (f"net.ipv6.conf.all.disable_ipv6 = 0\\nnet.ipv6.conf.lo.disable_ipv6 = 0\\n"
            f"net.ipv6.conf.{iface}.disable_ipv6 = 1\\nnet.ipv6.conf.default.disable_ipv6 = 1\\n"
            f"net.ipv6.conf.{iface}.accept_ra = 0\\nnet.ipv6.conf.default.accept_ra = 0\\nnet.ipv6.conf.all.accept_ra = 0\\n"
            f"net.ipv6.conf.{iface}.autoconf = 0\\nnet.ipv6.conf.default.autoconf = 0\\nnet.ipv6.conf.all.autoconf = 0\\n")
    run_payload = (f"printf '{body}' > {_DISABLE_IPV6_FILE} && sysctl -q -p {_DISABLE_IPV6_FILE} && "
                   f"echo \"disable_ipv6({iface})=$(cat /proc/sys/net/ipv6/conf/{iface}/disable_ipv6) "
                   f"accept_ra({iface})=$(cat /proc/sys/net/ipv6/conf/{iface}/accept_ra)\" && "
                   f"echo \"global IPv6 addresses left: $(ip -6 addr show scope global | grep -c inet6 || true)\" && "
                   f"echo 'RESULT: IPv6 disabled on {iface} in guest {gid} ({name}); persisted in {_DISABLE_IPV6_FILE}; loopback untouched'")
    run = f"pct exec {gid} -- sh -c {_sq_payload(run_payload)}"
    # §17.1570f/g — the check reads BOTH halves of the claim (the LAN interface off AND loopback still on)
    # and ends with one verdict word. Live, T6 was recorded "already met" off a check that read only the
    # eth0 half while 102's loopback IPv6 was down from a prior blanket disable.
    check = _ipv6_state_check(gid, iface)
    note = [{"expect": {check: "STATE-OK"}, "why": (
        f"drafted from the engine's own shape (§17.1570): inside guest {gid} ({name}) it writes {_DISABLE_IPV6_FILE} "
        f"— IPv6 off on {iface} and on interfaces created later, router advertisements and autoconf off — applies it "
        f"live with sysctl -p, and prints the resulting state. Loopback keeps IPv6 so a service bound to ::1 is "
        f"unaffected; nothing is restarted. Measured: the guest took SLAAC addresses from the gateway's advertisements "
        f"over a route that answers nothing. Reversible: remove the file and reboot the guest.")}]
    return [run], [check], note


#: §17.1570d — `net.ipv6.conf.all.disable_ipv6 = 1` written or set (sysctl -w, a file line, /proc write)
_ALL_DISABLE_IPV6_RE = re.compile(
    r"net\.ipv6\.conf\.all\.disable_ipv6\s*=\s*1\b|/proc/sys/net/ipv6/conf/all/disable_ipv6\b[^\n|;]*\b1\b"
    r"|\becho\s+1\s*>\s*/proc/sys/net/ipv6/conf/all/disable_ipv6\b", re.I)


def disables_loopback_ipv6(commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1570d — the block switches IPv6 off on ALL interfaces, loopback included.

    Live (job eff80f98, T5 "Write sysctl config prowlarr", model-drafted): the script wrote
    `net.ipv6.conf.all.disable_ipv6 = 1` and applied it; CT 102 lost `::1`, and the step's own brief
    had said "leave loopback IPv6 enabled so services bound to ::1 keep working". The `all` knob is a
    blanket; the interface that routes nowhere is `eth0`, and `default` covers interfaces created later.
    Judged on code, never comments; a line that sets `all` back to 0 is not this."""
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        for ln in t.splitlines():
            code = _strip_comment_shell(ln)
            if _ALL_DISABLE_IPV6_RE.search(code):
                out.append({"command": ln.strip()[:160], "why": (
                    "this disables IPv6 on ALL interfaces (`net.ipv6.conf.all.disable_ipv6 = 1`), which takes "
                    "loopback down with it: `::1` disappears and anything bound to it goes dark. MEASURED live "
                    "(§17.1570d): CT 102 lost `::1` to exactly this line. Disable the LAN interface by name "
                    "(`net.ipv6.conf.eth0.disable_ipv6 = 1`) and `default` for interfaces created later; leave "
                    "`all` and `lo` at 0.")})
                break
    return out


#: §17.1571 — stop the gateway's IPv6 router advertisements where the engine can reach them: on the
#: Proxmox bridge every guest hangs off, with an nftables BRIDGE-family table of its own (coexists with
#: pve-firewall, iptables or nft mode) that drops `nd-router-advert` frames being forwarded to the guests
#: and delivered to the host. Persisted as a scoped systemd oneshot (`ExecStartPre=-nft delete table …`,
#: `ExecStart=nft -f …`), so it survives reboot and never touches another ruleset. Reversible: disable the
#: unit, delete the table. Measured (pve, 2026-10-15): RAs from fe80::4eab:f8ff:fed1:5491 (the Spectrum
#: gateway, no LAN admin surface) reach every veth on vmbr0; the host itself keeps no RA route.
_RAFILTER_FILE = "/etc/scaffold/rafilter.nft"
_RAFILTER_UNIT = "scaffold-rafilter.service"
_RAFILTER_TABLE = "bridge scaffold_rafilter"
_RAFILTER_NFT = (
    "table bridge scaffold_rafilter {\\n"
    "  chain forward {\\n"
    "    type filter hook forward priority -200; policy accept;\\n"
    "    icmpv6 type nd-router-advert counter drop comment \\\"scaffold: the gateway IPv6 RA routes nowhere (sec 17.1571)\\\"\\n"
    "  }\\n"
    "  chain input {\\n"
    "    type filter hook input priority -200; policy accept;\\n"
    "    icmpv6 type nd-router-advert counter drop\\n"
    "  }\\n"
    "}\\n")
_RAFILTER_UNIT_TEXT = (
    "[Unit]\\nDescription=scaffold: drop IPv6 router advertisements on the bridge (sec 17.1571)\\n"
    "After=network-online.target\\nWants=network-online.target\\n\\n"
    "[Service]\\nType=oneshot\\nRemainAfterExit=yes\\n"
    "ExecStartPre=-/usr/sbin/nft delete table bridge scaffold_rafilter\\n"
    "ExecStart=/usr/sbin/nft -f /etc/scaffold/rafilter.nft\\n"
    "ExecStop=-/usr/sbin/nft delete table bridge scaffold_rafilter\\n\\n"
    "[Install]\\nWantedBy=multi-user.target\\n")
_RA_DROP_RE = re.compile(r"\b(?:drop|block|filter|stop|refuse|discard|suppress)\w*\b[^.;\n]{0,60}\b(?:router\s+advertisements?|\bRAs?\b|nd-router-advert)"
                         r"|\b(?:router\s+advertisements?|nd-router-advert)\b[^.;\n]{0,60}\b(?:drop|block|filter|stop|refuse|discard|suppress)", re.I)
_RA_WHERE_RE = re.compile(r"\bbridge\b|\bvmbr\d*\b|\bproxmox\s+host\b|\bthe\s+host\b|\bhypervisor\b|\bgateway\b|\bnftables?\b", re.I)
#: §17.1573 — the filter's own names, the engine's or the planner's: a step that writes/loads/enables/verifies
#: "block-ra" / "rafilter" / the RA table or unit is a piece of this one install (live, job 9e1c2c68: T2 wrote
#: the rule file, T3 the unit, T4 enabled it, T5–T7 verified each — fragments of one capability).
_RA_NAMED_RE = re.compile(r"\bblock[-_ ]?ra\b|\brafilter\b|\bnd-router-advert\b|\brouter[- ]advert", re.I)
_RA_PIECE_RE = re.compile(r"\b(?:nft|nftables|table|unit|oneshot|service|systemd|rule|counter|enable|start|load)\w*\b", re.I)
#: … but IN-GUEST IPv6 work ("stop accepting router advertisements in container 102", sysctl, eth0) is §17.1570's, not this
_IN_GUEST_IPV6_RE = re.compile(r"\bcontainer\s+\d+|\bCT\s*\d+\b|\bpct\s+exec\b|disable[_-]ipv6|\bsysctl\b|\baccept_ra\b|\bautoconf\b|"
                               r"\bin\s+(?:the\s+|each\s+|every\s+)?(?:guest|container|LXC)s?\b|\beth0\b|\bgai\.conf\b", re.I)


def _rafilter_check() -> str:
    """§17.1574 — the check runs on the READ-ONLY channel, which is unprivileged: `nft list` there answers
    `netlink: Error: cache initialization failed: Operation not permitted` (live, T2–T7 of 9e1c2c68). The
    oneshot is `RemainAfterExit`, so `active` means `nft -f` loaded the table; the files are read back; the
    counter is read in the approved RUN, whose channel is root (T6 saw `counter packets 12`)."""
    return (f"systemctl is-active {_RAFILTER_UNIT}; systemctl is-enabled {_RAFILTER_UNIT}; "
            f"cat {_RAFILTER_FILE}; cat /etc/systemd/system/{_RAFILTER_UNIT}; "
            f"systemctl is-active {_RAFILTER_UNIT} | grep -qx active && grep -q nd-router-advert {_RAFILTER_FILE} "
            f"&& echo STATE-OK || echo STATE-WRONG")


def rewrite_verify_router_adverts_dropped(node: Optional[dict], commands: list[str], verify: list[str],
                                          services: Optional[list] = None,
                                          inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    """§17.1573 — a verify/check step about the RA filter (its table, rule, counter or unit, by the engine's
    name or the planner's "block-ra") reads the filter as installed: run == check == `_rafilter_check()`,
    expecting STATE-OK. The planner's own read named ITS table (`nft list table bridge block_ra`), which
    does not exist once §17.1571 installed `scaffold_rafilter`; the engine reads what it built."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if not (_JF_OBSERVE_TITLE_RE.search(title) and _RA_NAMED_RE.search(text) and _RA_PIECE_RE.search(text)):
        return list(commands or []), list(verify or []), []
    own = _own_step_text(node)
    if _IN_GUEST_IPV6_RE.search(own):
        return list(commands or []), list(verify or []), []          # a guest's IPv6 state: §17.1570's verify
    if re.search(r"\bpve-firewall\b|nftables\.conf|\bguests?\s+(?:untouched|unchanged|were\s+not)", own, re.I) \
            and not _RA_NAMED_RE.search(own):
        return list(commands or []), list(verify or []), []          # a different thing is being checked
    check = _rafilter_check()
    run = f"nft list table {_RAFILTER_TABLE}; {check}"          # the root channel lists the table and its counter
    note = [{"expect": {check: "STATE-OK"}, "why": (
        f"drafted the check from the engine's own shape (§17.1573): it lists the installed table {_RAFILTER_TABLE} — the "
        f"rule's counter grows with every advertisement dropped — the unit's active/enabled state, and both files, ending "
        f"STATE-OK. The draft read a table by the plan's working name, which is not the one the engine installed.")}]
    return [run], [check], note


def rewrite_drop_router_adverts_on_the_bridge(node: Optional[dict], commands: list[str], verify: list[str],
                                              services: Optional[list] = None,
                                              inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1571 — a "drop/block the IPv6 router advertisements at the bridge/host/gateway" step installs
    the bridge filter on the Proxmox host, deterministically.

    Operator (2026-10-15): "fix the IPv6 router advertisements at the gateway through the engine." The
    gateway is a Spectrum router with no LAN admin page or API (the operator's standing note), so the
    nearest point the engine controls is the bridge: an nftables bridge table dropping nd-router-advert
    in the forward hook (to the guests) and the input hook (to the host), persisted as its own oneshot
    unit. The counter on the rule is the witness — it grows with every advertisement dropped — and the
    check prints it with one verdict word. Runs on the HOST (no pct exec), as the runner itself does."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if _JF_OBSERVE_TITLE_RE.search(title):
        return list(commands or []), list(verify or []), []
    if _IN_GUEST_IPV6_RE.search(_own_step_text(node)):
        return list(commands or []), list(verify or []), []          # in-guest IPv6 work is §17.1570's
    # the whole ("drop the RAs at the bridge") or a piece of it ("write the block-ra unit", "enable block-ra.service")
    whole = bool(_RA_DROP_RE.search(text) and _RA_WHERE_RE.search(text))
    piece = bool(_RA_NAMED_RE.search(text) and _RA_PIECE_RE.search(text))
    if not (whole or piece):
        return list(commands or []), list(verify or []), []
    run = (f"test -x /usr/sbin/nft || {{ echo 'FAILED: /usr/sbin/nft is not installed on this host'; exit 1; }}; "
           f"mkdir -p /etc/scaffold && printf '{_RAFILTER_NFT}' > {_RAFILTER_FILE} && "
           f"printf '{_RAFILTER_UNIT_TEXT}' > /etc/systemd/system/{_RAFILTER_UNIT} && "
           f"systemctl daemon-reload && systemctl enable {_RAFILTER_UNIT} && systemctl restart {_RAFILTER_UNIT} && "
           f"nft list table {_RAFILTER_TABLE} && "
           f"echo 'RESULT: IPv6 router advertisements are dropped on the bridge (forward to guests, input to host); "
           f"persisted as {_RAFILTER_UNIT}; existing SLAAC addresses in guests age out by their own lifetimes'")
    # The CHECK runs on the read-only channel (`nft list` is a read form there — measured: the runner ran
    # `nft list ruleset` for T1 of job 9e1c2c68): the table as loaded, with its counter; the oneshot's state
    # (RemainAfterExit: `active` only when `nft -f` succeeded); both files read back; one verdict word.
    check = _rafilter_check()
    note = [{"expect": {check: "STATE-OK"}, "why": (
        f"drafted from the engine's own shape (§17.1571): on the Proxmox host it writes {_RAFILTER_FILE} — an nftables "
        f"BRIDGE table that drops nd-router-advert frames forwarded to the guests and delivered to the host — and "
        f"{_RAFILTER_UNIT}, a oneshot that loads it at boot (delete-then-load, so it is idempotent and touches no "
        f"other ruleset, pve-firewall included). The gateway (fe80::…:5491, a Spectrum router) has no LAN admin "
        f"surface, so the bridge is the nearest point the engine controls. The check lists the table — its counter "
        f"grows with every advertisement dropped — and ends with STATE-OK. Reversible: disable the unit, delete the table.")}]
    return [run], [check], note


def _guest_by_name_fragment(text: str, services: Optional[list], inventory: Optional[dict]) -> tuple[str, str, str]:
    """§17.1572 — the guest whose measured-service or inventory NAME shares a word (4+ letters) with the step:
    "the panel backend" → control-panel (111). Fallback after `_named_guest`; "" when nothing matches."""
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", text or "")}
    for s in services or []:
        nm = str(getattr(s, "name", "") or "").lower()
        if nm and getattr(s, "guest", "") and any(t in words for t in re.split(r"[^a-z0-9]+", nm) if len(t) >= 4):
            return nm, str(getattr(s, "guest")), str(getattr(s, "unit", "") or "")
    for g, nm in ((inventory or {}).get("names") or {}).items():
        nm_s = str(nm or "").lower()
        if nm_s and any(t in words for t in re.split(r"[^a-z0-9]+", nm_s) if len(t) >= 4):
            unit = next((str(getattr(s, "unit", "") or "") for s in services or []
                         if str(getattr(s, "guest", "")) == str(g) and getattr(s, "unit", "")), "")
            return nm_s, str(g), unit
    return "", "", ""


#: §17.1572 — "inventory / list / map the endpoints|routes|API of <service>" is a READ the engine owns:
#: grep the service's route registrations and its config keys under its /opt/<name>* directories.
#: §17.1602 — `read` the verb, never the adjective: "Create a read-only Proxmox API token" matched "read … API"
#: §17.1682 — `api` as a URL path (`/api/v3/wanted/missing`) is a call TO an API, not the service's code: live (job c2518102
#: T2 "Fetch Sonarr wanted missing", text "read Sonarr's /api/v3/wanted/missing …"), the read became a route inventory
_ROUTE_INVENTORY_RE = re.compile(r"\b(?:inventory|inventor(?:y|ies)|list|enumerate|map|read(?![-‐ ]only|[-‐ ]write)|catalog(?:ue)?|survey)\b[^.;\n]{0,50}"
                                 r"(?<![/\w])(?:endpoints?|routes?|api(?:s)?|handlers?|code\s*base|codebase|source)\b", re.I)
_ROUTE_SERVICE_RE = re.compile(r"\b(control-?panel|panel|backend|express|node|flask|fastapi)\b", re.I)


#: §17.1602 — a step whose title opens with a change is never rewritten into a read-only template: live (c2792d4b T14
#: "Create a read-only Proxmox API token for the panel"), the inventory read replaced the draft and would have recorded
#: the step done with no token created.
_CHANGE_TITLE_RE = re.compile(r"^\s*(?:create|add|set|install|configure|fix|change|write|deploy|enable|disable|remove|"
                              r"delete|grant|give|update|replace|rotate|build|implement|make|move|rename|restart|issue|"
                              r"generate|provision|register|store|put|push)\b", re.I)


def title_changes_something(node: Optional[dict]) -> bool:
    return bool(_CHANGE_TITLE_RE.match(str((node or {}).get("title") or "")))


def rewrite_inventory_service_routes(node: Optional[dict], commands: list[str], verify: list[str],
                                     services: Optional[list] = None,
                                     inventory: Optional[dict] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1572 — an "inventory the panel's endpoints" step reads the measured service's own code,
    deterministically: every route registration (`app|router.get|post|put|delete|use(`) under the
    service's /opt/<name>* directories, the config directory's files and the key NAMES they hold
    (never values), and the served page's fetch targets.

    Live (job c2792d4b, T1): the model drafted 24 exploratory `find / -maxdepth 5 … | xargs grep` commands
    with no check — refused, `suggested: myself`. The engine already knows the service, its guest and
    its directories (service_truth / develop.read_workspace); a model cannot read remote files and need
    not guess where they are. A read, settled by STATE-OK when at least one route was found."""
    text = _node_text(node)
    title = str((node or {}).get("title") or "")
    if title_changes_something(node) or not (_ROUTE_INVENTORY_RE.search(title) or _ROUTE_INVENTORY_RE.search(text)):
        return list(commands or []), list(verify or []), []
    # §17.1682b — "read" alone (not inventory/enumerate/list/map/catalog/survey) claims only a service built from code the
    # engine can read (_ROUTE_SERVICE_RE): live (job c2518102 T6), "read qBittorrent's torrent list from its local Web API"
    # became an inventory of /opt/qbittorrent* -- a packaged app, no routes to read
    _m = _ROUTE_INVENTORY_RE.search(title) or _ROUTE_INVENTORY_RE.search(text)
    if _m and re.match(r"read\b", _m.group(0), re.I) and not _ROUTE_SERVICE_RE.search(text):
        return list(commands or []), list(verify or []), []
    own = _own_step_text(node)
    name, gid, _unit = _named_guest(own, services, inventory)
    if not gid:
        name, gid, _unit = _guest_by_name_fragment(own, services, inventory)    # "the panel backend" → control-panel
    if not gid:
        return list(commands or []), list(verify or []), []
    stem = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-").split("-")[0] or "app"
    routes = "(app|router)\\.(get|post|put|patch|delete|use)\\("
    inc = "--include='*.js' --include='*.ts' --include='*.py' --exclude-dir=node_modules --exclude-dir='*.bak*'"
    # plain reads over the service's /opt/<stem>* glob: no loops, no substitutions, so the read-only channel takes it;
    # the develop flow's `.bak.<stamp>` copies are left out (live, T1 of c2792d4b listed three of them)
    payload = (
        f"echo '--- service dirs ---'; ls -d /opt/{stem}* | grep -v '\\.bak'; "
        f"echo '--- routes ---'; grep -rnE '{routes}' /opt/{stem}* {inc} 2>/dev/null | grep -v node_modules | head -n 150; "
        f"echo '--- served page fetch targets ---'; grep -rhoE 'fetch\\([^)]{{0,120}}\\)' /opt/{stem}* --include='*.html' --include='*.js' --exclude-dir='*.bak*' 2>/dev/null | grep -v node_modules | sort | uniq -c | sort -rn | head -n 40; "
        f"echo '--- config files and key names (values never printed) ---'; ls /opt/{stem}*/config 2>/dev/null; "
        f"grep -rhoE '\"[A-Za-z_]*([Kk]ey|[Tt]oken|[Pp]assword|[Uu]rl|[Aa]ddress)\"' /opt/{stem}*/config 2>/dev/null | sort | uniq -c; "
        f"echo '--- route files ---'; grep -rlE '{routes}' /opt/{stem}* {inc} 2>/dev/null | grep -v node_modules | grep -c . && echo STATE-OK || echo STATE-WRONG")
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)}"
    note = [{"expect": {run: "STATE-OK"}, "why": (
        f"drafted the inventory from the engine's own shape (§17.1572): inside guest {gid} ({name}) it lists the "
        f"service's /opt/{stem}* directories, every route registration in them, the served page's fetch targets, "
        f"and the config files with the NAMES of the keys they hold (values are never printed) — a read, settled by "
        f"STATE-OK when at least one route file was found. The draft was 24 `find / … | xargs grep` guesses with no check.")}]
    return [run], [run], note


#: §17.1574c — "probe / call / test each endpoint, authenticated as <the login user>": the engine presents the
#: login it holds and reads every GET route the service's own code registers.
_PROBE_ENDPOINTS_RE = re.compile(r"\b(?:probe|call|test|exercise|query|hit|fetch|request|curl)\w*\b[^.;\n]{0,40}\b(?:endpoints?|routes?|api)\b", re.I)
_AUTHENTICATED_RE = re.compile(r"\bauthenticated\b|\blogged[- ]in\b|\bbasic[- ]auth\b|\bas\s+the\s+\w+\s+user\b|\bwith\s+(?:the\s+)?(?:login|credentials?)\b", re.I)


def rewrite_probe_service_endpoints(node: Optional[dict], commands: list[str], verify: list[str],
                                    services: Optional[list] = None, inventory: Optional[dict] = None,
                                    logins: Optional[list] = None) -> tuple[list[str], list[str], list[dict]]:
    r"""§17.1574c — a "probe each endpoint authenticated" step for a service the engine holds a login for.

    Live (job c2792d4b, T2): the model drafted a loop over endpoints it guessed, with
    `-H "Cookie: <PANEL_SESSION_COOKIE>"` (a value nobody holds) and no check — refused. The engine holds
    the login (PANEL_PASSWORD, user `panel`, §17.1548's `logins_from_held`) and can read the routes off
    the service's own code, so inside the guest it: lists every GET route registered under /opt/<stem>*,
    curls each on the measured loopback port with HTTP Basic auth — the password fed on stdin as the
    runner-resolved `<NAME>` placeholder, never in the command — and prints `HTTP <code> in <s>` with the
    tail of the body; STATE-OK when every route answered (any code is an answer; 000 is not). The same
    line is run and check: it changes nothing."""
    from app.modules import machine_values as _mv
    text = _node_text(node)
    if title_changes_something(node) or not (_PROBE_ENDPOINTS_RE.search(text) and _AUTHENTICATED_RE.search(text)):
        return list(commands or []), list(verify or []), []   # §17.1602 — a change is not a probe
    own = _own_step_text(node)
    name, gid, _unit = _named_guest(own, services, inventory)
    if not gid:
        name, gid, _unit = _guest_by_name_fragment(own, services, inventory)
    if not gid or not logins:
        return list(commands or []), list(verify or []), []
    guard = _mv._guarded_ports(services, list(logins))
    mine = [(port, secret, user) for port, (secret, user, g, vm) in guard.items() if str(g) == str(gid) and not vm]
    stem = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-").split("-")[0] or "app"
    if mine:
        port, secret, user = sorted(mine)[0]
        port_expr = f"port={port}; "
    else:
        # §17.1574d — no MEASURED service for this guest (live, c2792d4b: the job named no service, so nothing was
        # measured): the login whose stem names the guest (`PANEL_PASSWORD` → `panel` ∈ `control-panel`) is still the
        # engine's, and the service's own code says which port it listens on — read at run time, in the guest.
        _gn = _mv._norm(name)
        fits = [L for L in logins if _mv._login_stem(L.get("name", "")) and _gn
                and (_mv._login_stem(L.get("name", "")) in _gn or _gn in _mv._norm(L.get("hint", "")))]
        if not fits or not str(fits[0].get("user") or "").strip():
            return list(commands or []), list(verify or []), []
        secret, user = str(fits[0]["name"]), str(fits[0]["user"])
        # §17.1579 — the port is what LISTENS in the guest, read off the machine first (live: the panel's server.js
        # says `app.listen(PORT…)`, no literal; `ss` in CT 111 says node listens on 3001), then the code's literal.
        port_expr = ('port=$(ss -ltnpH 2>/dev/null | grep -E "users:...(node|python|gunicorn|uvicorn|deno|bun|java|ruby|php)" '
                     '| grep -oE ":[0-9]+ " | head -n 1 | tr -d ": "); '
                     f'test -n "$port" || port=$(grep -rhoE "listen\\(\\s*[0-9]+" /opt/{stem}* --include="*.js" --include="*.ts" --include="*.py" '
                     f'--exclude-dir=node_modules --exclude-dir="*.bak*" 2>/dev/null | grep -oE "[0-9]+" | head -n 1); '
                     'test -n "$port" || echo "FAILED: no app listener in the guest and no listen(<port>) in the service code"; ')   # an empty port → every curl 000 → STATE-WRONG
    # double quotes only inside the payload: `_sq_payload` wraps it once, so no `'\''` escapes appear
    payload = (
        'cpw=$(cat); fails=""; n=0; ' + port_expr +     # the password arrives on stdin (`cat` is a reader at both ends; `read` is not on the runner's table)
        # §17.1578 — the block's own names are LOWERCASE: the runner reads every `$UPPER` reference as a secret it must
        # hold and refused `$CPW`/`$PORT` live ("neither the engine sent nor this runner's secrets file holds")
        # the route's quote is matched with `.` and stripped with tr's octal `\047`: no quote character of either kind
        # appears in the payload, so `_sq_payload` wraps it once with no `'\''` escapes
        f'for ep in $(grep -rhoE "(app|router)\\.get\\(.(/[^,)]*)" /opt/{stem}* --include="*.js" --include="*.ts" '
        f'--exclude-dir=node_modules --exclude-dir="*.bak*" 2>/dev/null | grep -oE "/[^,)]*" | tr -d "\\047\\042" | sort -u); do '
        f'echo "=== $ep ==="; out=$(curl -s -m 20 -u "{user}:$cpw" -w "\\nHTTP %{{http_code}} in %{{time_total}}s" "http://127.0.0.1:$port$ep" 2>&1); '
        'n=$((n+1)); case "$out" in *"HTTP 000"*) fails="$fails $ep";; esac; printf "%s\\n" "$out" | tail -c 400; echo; done; '
        # §17.1580 — the probe's verdict is that it PROBED (a port, at least one route): a route that hangs is the
        # finding the next step needs, not this step's failure (live, c2792d4b T2: three routes hang; that is the bug)
        'echo "unanswered:$fails"; echo "probed:$n"; test -n "$port" && test "$n" -gt 0 && echo STATE-OK || echo STATE-WRONG')
    run = f"pct exec {gid} -- sh -c {_sq_payload(payload)} <<< \"<{secret}>\""
    note = [{"expect": {run: "STATE-OK"}, "why": (
        f"drafted the probe from the engine's own shape (§17.1574c): inside guest {gid} ({name}) it reads every GET route "
        f"the service's code registers under /opt/{stem}*, calls each on the service's loopback port as user `{user}` with the login the "
        f"engine holds ({secret}, fed on stdin as a runner-resolved placeholder — never in the command or a log), and prints "
        f"`HTTP <code> in <s>` with the tail of each body and lists the routes that did not answer (`unanswered:`); STATE-OK "
        f"when the probe ran — a hanging route is this step's finding, not its failure. The draft guessed the routes "
        f"and sent a session cookie nobody holds, with no check.")}]
    return [run], [run], note


def a_validating_endpoint_sent_an_empty_body(
        commands: list[str], files: Optional[list[dict]] = None) -> list[dict]:
    r"""§17.1378b — a `/test` endpoint called with `{}` when it validates the object.

    Live, 2026-10-05: the block that finally ran exited 22 on its own last step --

        pct exec 103 -- sh -c "curl … -d '{}' http://127.0.0.1:7878/api/v3/downloadclient/test"

    and Radarr answered with four validation errors at once:

        'Name' must not be empty.          'Implementation' must not be empty.
        'Config Contract' must not be empty.
        'Priority' must be between 1 and 50. You entered 0.

    The PUTs before it had already landed, so the run reported `failed` for a
    malformed CHECK while its work was done -- the worst kind of outcome to read.
    These endpoints validate the resource: send the object, not a trigger.
    """
    out: list[dict] = []
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        for ln in logical_lines(t):
            s_line = ln.strip()
            if s_line.startswith("#") or not _VALIDATING_TEST_RE.search(s_line):
                continue
            if not _body_carries_nothing(s_line):
                continue
            out.append({"command": s_line[:200], "why": (
                "this posts an empty or near-empty body to a `/test` endpoint that "
                "VALIDATES the whole resource, so it fails on the resource's own required "
                "fields rather than testing anything. MEASURED live (§17.1378b): `-d '{}'` "
                "to `…/api/v3/downloadclient/test` answered `'Name' must not be empty`, "
                "`'Implementation' must not be empty`, `'Config Contract' must not be "
                "empty` and `'Priority' must be between 1 and 50. You entered 0` -- four "
                "errors about the empty body, and because the PUTs before it had already "
                "landed the run reported `failed` for a malformed CHECK while its work was "
                "done. Send the object: GET it, then POST that same document to `/test`.")})
    return out


def fields_sent_to_an_api(commands: list[str], files: Optional[list[dict]] = None) -> list[str]:
    """Every settings FIELD this block sends in a request body, by name.

    §17.1370 — live, 2026-10-05: ADD132 was recorded

        already met: 2 verify check(s) read off pve-runner before the run confirm the goal

    for the SECOND time, by a route §17.1365 does not cover. That draft writes no
    config key — it changes Radarr's and Sonarr's download client through their
    APIs — so `keys_the_block_writes` was empty, the guard did not engage, and the
    checks (`GET …/api/v3/downloadclient`, a list that has held a `qBittorrent`
    entry all along) "confirmed" the goal.

    qBittorrent's own log says what was true:

        11:42:34  WebAPI login failure. Reason: IP has been banned, IP: 192.168.1.22, username: admin
        12:42:30  WebAPI login failure. Reason: invalid credentials, attempt count: 1, IP: 192.168.1.23
    """
    out: list[str] = []
    for t in [str(c) for c in commands or []] + \
             [str((f or {}).get("content") or "") for f in files or []]:
        for ln in logical_lines(t):          # §17.1373
            s = ln.strip()
            if s.startswith("#") or not _HTTP_BODY_RE.search(s):
                continue
            if _HTTP_GET_RE.search(s):
                continue
            for m in _SENT_FIELD_RE.finditer(s):
                field = m.group("field")
                if field in _NOT_A_SENT_SETTING or field in out:
                    continue
                out.append(field)
            # §17.1373 — the shape the *arr APIs really take: the settings live in
            # a `fields` array as `{"name":"username","value":"admin"}`, so the
            # field NAME is a value, not a key, and `_SENT_FIELD_RE` saw only the
            # object's own `enable` and `priority`. Live, that left `username`,
            # `password`, `host` and `port` invisible to §17.1370.
            for m in _NAMED_FIELD_RE.finditer(s):
                field = m.group("field")
                if field in _NOT_A_SENT_SETTING or field in out:
                    continue
                out.append(field)
    return out


def keys_the_block_writes(commands: list[str], files: Optional[list[dict]] = None) -> list[str]:
    """Every settings KEY this block would write, by name.

    §17.1365 — what a check has to read back for a pre-pass to mean anything.
    §17.1370 — a field sent to an API counts: the surface differs, the question
    does not.
    """
    out: list[str] = list(fields_sent_to_an_api(commands, files))
    for t in [str(c) for c in commands or []] + \
             [str((f or {}).get("content") or "") for f in files or []]:
        # §17.1365 — a heredoc BODY is part of the write that opened it:
        # `tee /etc/x.conf <<EOF` / `Key=1` / `EOF` writes `Key`, and the key is
        # not on the line that names the file.
        in_heredoc: Optional[str] = None
        for ln in t.split("\n"):
            s = ln.strip()
            if in_heredoc is not None:
                if s == in_heredoc:
                    in_heredoc = None
                    continue
            elif s.startswith("#"):
                continue
            if in_heredoc is None:
                _hd = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", ln)
                if _hd and _WRITES_TO_A_FILE_RE.search(ln):
                    in_heredoc = _hd.group(1)
            # §17.1365 — the key must be written INTO A FILE. Measured over the
            # job's own history, the first cut of this flagged six blocks for
            # command OPTIONS and throwaway redirects: `systemd-run -p
            # StandardOutput=append:/root/.scaffold_step.log`, `ssh -o
            # BatchMode=yes`, `SSHPASS=… sshpass -e`, and any line ending
            # `>/dev/null`. None of those is a setting a check could read back.
            if in_heredoc is None and not _WRITES_TO_A_FILE_RE.search(s):
                continue
            for m in _WRITES_KEY_RE.finditer(s):
                key = (m.group("key") or m.group("skey") or "").strip()
                key = key.lstrip("$").strip("\"'")
                if not key or key in _NOT_A_SETTING or (key.isupper() and len(key) <= 3):
                    continue
                # an option's argument (`-p K=v`, `-o K=v`, `--opt K=v`) is not a
                # setting, and nor is an environment prefix before a command
                before = s[:m.start()].rstrip()
                if re.search(r"(?:^|\s)(?:-[A-Za-z]|--[\w-]+)\s*$", before):
                    continue
                if key not in out:
                    out.append(key)
    return out


#: §17.1655 — a step whose job is to TAKE a backup / snapshot / copy
_TAKES_A_BACKUP_RE = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:back\s*up|backup|snapshot|archive|save\s+a\s+copy|copy)\b", re.I)


def a_backup_is_taken_now(node: Optional[dict]) -> str:
    """§17.1655 — ``""``, or why a backup step is never "already met".

    Live (job d89078ed T7 "Backup Prowlarr config database", 2026-10-18): the pre-read listed Prowlarr's own
    scheduled backups -- the newest from Oct 12 -- and the judge confirmed the goal, so T7 was recorded done and no
    backup was taken before T8 replaced Prowlarr's binaries. A backup exists to hold the state of NOW; one already
    on disk is a copy of the past, and no pre-read can show the new one."""
    title = str((node or {}).get("title") or "")
    if _TAKES_A_BACKUP_RE.match(title):
        return "a backup step takes a copy of the state now; an existing backup on disk is an older one"
    return ""


def checks_can_witness_the_change(commands: list[str], verify: list[str],
                                  files: Optional[list[dict]] = None) -> str:
    """``""`` when some check reads back a key the block writes, else why not.

    §17.1365 — `already_met` reads the step's own checks off the machine BEFORE
    the block runs and records the step `done` when every one is `confirmed`.
    That is sound only if a check could have FAILED. Live, 2026-10-04: ADD132
    ("Make Radarr and Sonarr actually drive qBittorrent, and prove the
    connection") was recorded

        already met: 3 verify check(s) read off pve-runner before the run confirm the goal

    while its checks only `GET …/api/v3/downloadclient` — a list that held a
    `qBittorrent` entry before the step and after it. Measured afterwards:
    `qBittorrent.conf` holds `WebUI\\Port` and nothing else, and the WebUI answers
    **403** from off the box. The step's work -- writing `WebUI\\Username` and
    `WebUI\\Password_PBKDF2` -- never happened, no check read either key, and the
    goal was recorded met.

    So: when a block writes named settings and no check names one of them, the
    checks cannot witness the change, and a pre-pass is not evidence. A block that
    writes no settings (ADD84's `growpart` + `resize2fs`, confirmed by `df -h /`)
    is untouched -- that case is what §17.1302 is for.
    """
    keys = keys_the_block_writes(commands, files)
    if keys:
        # §17.1365 — the same key is spelled three ways across the layers: the block
        # writes `WebUI\\\\Username` through two quoting levels, the file holds
        # `WebUI\\Username`, and a check greps `^WebUI.Username=` with a regex dot. The
        # comparison is on the letters and digits alone.
        def _alnum(x: str) -> str:
            return re.sub(r"[^A-Za-z0-9]", "", str(x or ""))

        blob = _alnum("\n".join(str(c or "") for c in verify or []))
        for key in keys:
            if _alnum(key) and _alnum(key) in blob:
                return ""
        return (f"the block writes {', '.join('`' + k + '`' for k in keys[:4])}"
                + (f" and {len(keys) - 4} more" if len(keys) > 4 else "")
                + ", and no check reads any of them back")
    # §17.1559 — nothing NAMED is written, so §17.1365 did not engage; two more shapes a pre-read
    # cannot settle. Live (job 263e920a, 2026-10-14): five steps in a row — "Trigger metadata
    # refresh", "Trigger image refresh", "Wait for refresh completion", "Verify Name", "Verify
    # Overview" — were recorded `already met` off `jellyfin HTTP 200` (+ a log grep count), and the
    # model judge said "confirmed" to each. Nothing had run; Jellyfin had not been refreshed.
    probes = [c for c in verify or [] if _status_only_probe(str(c or ""))]
    if verify and len(probes) == len(verify):
        return ("every check is a liveness probe (an HTTP status or exit code only), which says the "
                "service is up, not that this step's goal holds")
    sent = sends_a_change(commands, files)
    if sent:
        return (f"the block sends a change ({sent}) that no check reads back -- a trigger's goal is the "
                f"work it queues, which no pre-read can show already done")
    return ""                         # a state the checks read (ADD84's `df -h /`): §17.1302's own case


#: §17.1559 — a mutating HTTP call: curl's method/body flags, a python `method='POST'`, a wrapper
#: called with a method literal (`api('/Items/x/Refresh','POST')`), or `requests.post(…)`.
#: §17.1626 — a body flag is curl's only when curl is the command it belongs to: `-d` alone is also `tr -d`, `cut -d`,
#: `[ -d dir ]`, `docker run -d`, `date -d`. Live (RD2, CHK1, 2026-10-16/17): the ssh template's own `tr -d '[:space:]'`
#: made EVERY block through it "blind" ("sends a change (`-d` a request body)"), so no ssh-guest step could ever be
#: recorded already met -- CHK1 was a pure `head` read.
_SENDS_A_CHANGE_RE = re.compile(
    r"(?:-X|--request)\s*[\"']?(?P<m1>POST|PUT|PATCH|DELETE)\b"
    r"|\bcurl\b[^|;&\n]*?\s(?:-d|--data(?:-raw|-binary|-urlencode)?|--json)(?:\s|=)"
    r"|\bwget\b[^|;&\n]*?\s--post-(?:data|file)(?:\s|=)"
    r"|\bmethod\s*=\s*[\"'](?P<m2>POST|PUT|PATCH|DELETE)[\"']"
    r"|\b\w+\((?:[^()\n]|\([^()\n]*\))*?[\"'](?P<m3>POST|PUT|PATCH|DELETE)[\"']"
    r"|\brequests\.(?P<m4>post|put|patch|delete)\(", re.I)


def sends_a_change(commands: list[str], files: Optional[list[dict]] = None) -> str:
    """§17.1559 — the first mutating request the block sends (``"POST"``), or ``""``.

    Code only, never comments (§17.1040): a comment saying "do not POST" names no request."""
    texts = [str(c) for c in commands or []] + \
            [str((f or {}).get("content") or "") for f in files or []]
    for t in texts:
        # §17.1626 — one logical line: a `curl … \` continued onto `  -d '{…}'` keeps its body flag with its curl
        for ln in re.sub(r"\\\r?\n[ \t]*", " ", t).splitlines():
            line = _strip_comment_shell(ln)
            m = _SENDS_A_CHANGE_RE.search(line)
            if m:
                verb = next((g for g in (m.group("m1"), m.group("m2"), m.group("m3"), m.group("m4")) if g), "a body")
                return f"`{verb.upper()}`" if verb != "a body" else "`-d` a request body"
    return ""


def _strip_comment_shell(line: str) -> str:
    """The code before an unquoted ``#``."""
    q = ""
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
        elif ch == "#":
            return line[:i]
    return line


#: §17.1559 — a check that reports a STATUS and nothing else: curl with its body thrown away
#: (`-o /dev/null`) and `-w` / `--fail`, or a bare `echo`. `systemctl is-active`, `df -h /`,
#: a `grep` over a file or an API body are READS and are not this.
_PROBE_PIECE_RE = re.compile(r"^\s*(?:echo\b.*|(?:\S+\s+)*?curl\b(?=.*\s-o\s*/dev/null)(?=.*(?:-w\b|--write-out\b|--fail\b|\s-f\b|-sf\b|-fs\b)).*)$", re.S)


def _status_only_probe(check: str) -> bool:
    """§17.1559 — True when every command in the check only reports a status code."""
    c = str(check or "").strip()
    if not c:
        return False
    # unwrap `pct exec N -- sh -c '…'` / `bash -c "…"` to what runs inside
    for _ in range(3):
        m = re.match(r"^(?:pct\s+exec\s+\d+\s+--\s+|sudo\s+)?(?:ba)?sh\s+-c\s+(['\"])(.*)\1\s*$", c, re.S)
        if not m:
            m2 = re.match(r"^pct\s+exec\s+\d+\s+--\s+(.*)$", c, re.S)
            if not m2:
                break
            c = m2.group(1).strip()
            continue
        c = m.group(2).replace("'\\''", "'").strip()
    pieces = [p for p in re.split(r"\s*(?:;|&&|\|\||\|)\s*", c) if p.strip()]
    return bool(pieces) and all(_PROBE_PIECE_RE.match(p) for p in pieces)


def _reads_only(cmd: str) -> bool:
    from app.modules.assist_state_check import read_only_command
    c = re.sub(r"<[A-Z][A-Z0-9_]{1,40}>", "x", str(cmd or ""))     # a placeholder is a value, not a verb
    return read_only_command(c)


#: §17.1625 — the engine's in-guest templates: their block runs INSIDE guest N through the host's script
#: (`bash /tmp/in_vm_N.sh start` over ssh, `…/in_vm_N_agent.sh` through the guest agent, `…/in_ct_N.sh` in an LXC).
_IN_GUEST_RUN_RE = re.compile(
    r"^(?P<prefix>.*?)bash\s+/tmp/in_(?P<kind>vm|ct)_(?P<gid>\d+)(?P<agent>_agent)?\.sh\s+(?:start|wait|last)\b")
#: a check that names the hypervisor itself is a host check whatever the step's subject
_HOST_CHECK_RE = re.compile(r"^\s*(?:qm|pct|pvesh|pvesm|pvecm|pveversion|ha-manager|lxc-\w+)\b")


def _inside_the_guest(commands: Optional[list]):
    """§17.1625 — when the block runs inside a guest through an engine template, the wrapper that runs ONE check in
    that same guest (as root); else None. Live (job 0fb4ea7f T18, 2026-10-16): the ssh template's checks are all
    host-side (`qm status 110`), so the step's own `head -n 40 /opt/scaffold-engine/PLACEMENT.md` was added bare
    and read the HOST, where the file does not exist; on the agent and LXC templates the same check was dropped."""
    for c in commands or []:
        m = _IN_GUEST_RUN_RE.match(str(c).strip())
        if not m:
            continue
        gid, q = m["gid"], shlex.quote
        if m["kind"] == "ct":
            return lambda chk: f"pct exec {gid} -- bash -c {q(chk)}"
        if m["agent"]:
            return lambda chk: f"qm guest exec {gid} -- bash -c {q(chk)}"
        prefix = m["prefix"]          # the run line's own MASS_PASSWORD / GUEST_USER assignments
        return lambda chk: f"{prefix}bash /tmp/in_vm_{gid}.sh check {q(chk)}"
    return None


def with_own_checks(node: Optional[dict], verify: list, commands: Optional[list] = None) -> list[str]:
    """§17.1470 — the checks a step names after "Done when" always run, whatever the drafted block chose.

    Live (2026-10-11): ADR1 ("Point what earlier steps set up at the new address of VM 106") named three greps that
    must print nothing — inside VM 106, inside the panel's LXC 111, and on the host. The drafted verify list kept
    only the host one and added three `systemctl is-active` reads; all four confirmed, the step was recorded
    "already met", and the panel's palworld_settings_route.js still held `address: '192.168.1.106'`."""
    from app.modules.operator_steps import checks
    out = [str(c) for c in verify or []]
    into = re.compile(r"^\s*(?:pct\s+exec|qm\s+guest\s+exec)\s+\d+")
    # A bare check written for inside a guest (ADD84's `df -h /`) would read the HOST if run as written: only a
    # check that says where it runs is added, or any check when the block has checks and all run on the host.
    host_level = bool(out) and not any(into.match(c) for c in out)
    inside = _inside_the_guest(commands)                                     # §17.1625
    for c in checks(node):
        if inside and not into.match(c) and not _HOST_CHECK_RE.match(c):
            c = inside(c)              # the step's work is in the guest: so is its bare check
        elif not (into.match(c) or host_level):
            continue
        if c not in out:
            out.append(c)
    return out


def the_run_is_the_read(waiting: Optional[dict], executed: Optional[list]) -> bool:
    """§17.1574e — True when the approved RUN of this frame was itself the reading: a root-needing MCP read
    (`hands_on_reason` mcp:needs-root, §17.1574b) whose commands all ran and printed something. Its check is the
    same command on the unprivileged channel — refused by construction — so that refusal is not "unverified"."""
    w = waiting or {}
    if str(w.get("hands_on_reason") or "") != "mcp:needs-root":
        return False
    ex = [e for e in (executed or []) if isinstance(e, dict)]
    return bool(ex) and all(e.get("ok") is not False and str(e.get("output") or "").strip() for e in ex)


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
    verify_cmds = with_own_checks(node, (frame or {}).get("verify") or [],
                                  (frame or {}).get("commands") or [])          # §17.1470, §17.1625
    if not verify_cmds or not (frame or {}).get("commands"):
        return None
    # §17.1365 — a pre-pass means nothing when the checks could not have failed.
    _blind = checks_can_witness_the_change(
        [str(c) for c in (frame or {}).get("commands") or []], verify_cmds,
        (frame or {}).get("files") or [])
    # §17.1655 — a BACKUP is taken now, before the change it guards; a backup that already exists is an older one
    _blind = _blind or a_backup_is_taken_now(node) or str((frame or {}).get("always_run") or "")
    if _blind:
        logger.warning("supervised_run_already_met_blind node=%s why=%s",
                       (node or {}).get("node_key"), _blind)
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
    claim = goal_claim(title, str((node or {}).get("description") or ""))
    # §17.1570g — the runbook's own "`cmd` shows `text`" bullets, plus what a rewrite declared on the frame
    verdicts = await _verify_verdicts(claim, verify_cmds, pasted,
                                      expects={**verify_expectations(str((frame or {}).get("runbook") or "")),
                                               **{str(k): str(v) for k, v in ((frame or {}).get("expects") or {}).items()}})
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


_REMOTE_HEREDOC_RE = re.compile(r"<<'REMOTE'\n(.*?)\nREMOTE\n", re.S)


def _frame_runbook(waiting: dict, commands: list[str], verify_cmds: list[str]) -> str:
    """§17.1569 — the record's head is WHAT WAS APPROVED AND RAN: the frame's commands, its checks, the
    files it wrote and the engine's own drafting notes — not the pre-rewrite draft.

    Live (jobs 263e920a/977a8a38): a supervised node's `output_text` opened with `## Run this` + the
    planner's original block (`for item in ${T3}; do curl … | jq …`, or the model's `systemctl stop
    jellyfin` + sqlite3 draft) while `## Executed on` held the capability's rewrite that actually ran.
    Every reader of the record saw a command that never ran presented as the step's plan — the report
    step mirrored it (§17.1567), an operator reading the node would have believed it. The frame
    (`awaiting_decision`) is the approved truth: `commands`, `verify`, `files`, `engine_fixed`."""
    w = waiting or {}
    lines = ["## Run this", "```bash", *[str(c) for c in commands or []], "```"]
    files = [str((f or {}).get("path") or "") for f in (w.get("files") or []) if (f or {}).get("path")]
    if files:
        lines += ["", "## Files written", *[f"- `{p}`" for p in files]]
    # §17.1614b — the commands that ran INSIDE the guest: the engine's guest templates carry them in a written
    # script's `REMOTE` heredoc, and the record kept only the wrapper (`bash /tmp/in_vm_110.sh start`). Live (job
    # 0fb4ea7f), T9's record never said Ollama went into Docker, so every later reader -- the drafter of T11, the
    # operator -- could not tell how to run it.
    inside = []
    for f in (w.get("files") or []):
        m = _REMOTE_HEREDOC_RE.search(str((f or {}).get("content") or ""))
        if m:
            inside += [ln for ln in m.group(1).splitlines()
                       if ln.strip() and not ln.startswith(("set ", "export DEBIAN_FRONTEND"))]
    if inside:
        lines += ["", "## Ran inside the guest", "```bash", *inside, "```"]
    if verify_cmds:
        lines += ["", "## Verify", "```bash", *[str(c) for c in verify_cmds], "```"]
    # the frame stores its notes as plain strings (live) or as {"why": …} dicts (the rewrites' own shape)
    notes = [str(n.get("why") if isinstance(n, dict) else n or "").strip() for n in (w.get("engine_fixed") or [])]
    notes = [n for n in notes if n]
    if notes:
        lines += ["", "## The engine drafted this", *[f"- {n}" for n in notes]]
    return "\n".join(lines)


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


async def settle_interrupted(job_id: str, node_key: str, exc: BaseException) -> bool:
    """§17.1496 — a run that died AFTER the node was claimed is recorded, not stranded.

    `resolve_run` commits the claim (`pending -> running`) before anything reaches the
    machine, so that a second "run" cannot double-write. Everything after that commit
    used to be unguarded: a secret-store error, a runner exception, the client
    disconnecting from `POST /decide` (Starlette cancels the handler) or a restart
    mid-run left the node at `running` under a job still `awaiting_decision` -- the
    frame kept offering "Run", "run" answered `node_gone`, and the orphan reaper's
    reset re-offered a block that may already have run on the machine.

    The honest record is `failed` with the reason: the operator reads what happened,
    checks the machine, and `reset`s the node when it is safe to offer the block
    again. Uses its OWN session: the caller's may be the thing that broke. Never
    raises; returns whether the node moved."""
    from app.database import async_session
    reason = (f"interrupted after the node was claimed ({type(exc).__name__}: "
              f"{' '.join(str(exc).split())[:200] or 'no detail'}) -- part of the block may have run; "
              f"check the machine, then reset the node to offer the block again")
    try:
        async with async_session() as db:
            res = await db.execute(
                text("UPDATE dag_nodes SET status = 'failed', last_verification_reason = :why, updated_at = NOW() "
                     "WHERE job_id = :jid AND node_key = :nk AND status = 'running'"),
                {"jid": job_id, "nk": node_key, "why": reason})
            await db.commit()
        moved = bool(res.rowcount)
    except Exception as settle_exc:              # the DB itself is down: the log line is all there is
        logger.error("supervised_run_interrupted_unsettled job=%s node=%s err=%r", job_id, node_key, settle_exc)
        return False
    logger.error("supervised_run_interrupted job=%s node=%s moved=%s why=%s", job_id, node_key, moved, reason)
    return moved
