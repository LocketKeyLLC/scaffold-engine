"""§17.478 (Phase 4) — interactive node control (CRUD) for dag_nodes.

Operations: edit / insert / delete / reorder / reset. Every mutation:
  * validates the post-edit graph is acyclic with valid depends_on refs,
  * renumbers execution_order contiguously,
  * honors an optimistic-lock ``edit_version`` (stale expected → 409),
  * writes an append-only ``dag_node_edits`` audit row (before/after JSONB),
  * cascade-resets any DONE/terminal node whose inputs were invalidated.

``reset_node`` generalizes ``execution_retry.retry_failed_node`` beyond
FAILED: it resets ANY status to pending (a deliberate re-run, so it does NOT
bump retry_count) and cascade-resets transitive downstream.

Each op returns a dict. On failure: ``{"error": msg, "http_status": N}`` —
the router maps it to that HTTP status. On success: ``{"status": "ok", ...}``.
"""
from __future__ import annotations

import json
import logging
from collections import deque

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold.node_editor")

# Fields a PATCH /nodes edit may change. title/description/is_deliverable are
# metadata (no output invalidation); the INVALIDATING set changes what the
# node produces, so editing them on an already-run node resets it + downstream.
# §17.614 (audit #11) — prompt_template (not optimized_prompt) is editable: it is
# the field _build_prompt consumes on re-execution. Editing optimized_prompt was a
# no-op the executor overwrote, silently discarding the operator's prompt fix.
EDITABLE_FIELDS = {
    "title", "description", "prompt_template", "tool",
    "depends_on", "assigned_model", "is_deliverable",
    "tool_config",  # §17.772 — an MCP node's {server, tool, args}
}
INVALIDATING_FIELDS = {"prompt_template", "tool", "depends_on", "tool_config"}
_TERMINAL = ("done", "failed", "skipped")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _load_nodes(db: AsyncSession, job_id: str) -> list[dict]:
    # §17.611 (audit #37) — a non-UUID job_id would raise asyncpg DataError
    # against the UUID `job_id` column, propagating as an uncaught HTTP 500 (the
    # /web node-action POST routes are auth-exempt, so a crafted garbage id hit
    # a raw 500 + error_logs row). Return empty so every op emits its normal
    # "not found" error dict and the web layer renders the graceful banner.
    from uuid import UUID
    try:
        UUID(str(job_id))
    except (ValueError, TypeError):
        return []
    rows = (await db.execute(
        text(
            "SELECT node_key, status, depends_on, execution_order, "
            "       edit_version, is_deliverable "
            "FROM dag_nodes WHERE job_id = :j ORDER BY execution_order, node_key"
        ),
        {"j": job_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def list_nodes(job_id: str, db: AsyncSession) -> dict:
    """Full editable node list for the /ui plan editor.

    Returns every column ``PATCH /nodes/{job_id}/{node_key}`` accepts plus
    ``edit_version`` (the optimistic-lock token the editor round-trips) and
    ``status``/``execution_order``. Distinct from ``execution_handler.node_outputs``
    (output bodies, no versions/prompt) and ``GET /dag/{id}`` (topology only) —
    those omit ``edit_version`` and the editable payload, so the editor could
    neither pre-fill the form nor PATCH with a correct ``expected_version``.

    On a malformed/missing job returns the standard ``{"error", "http_status"}``
    dict the ``nodes`` router maps to an HTTPException. ``tool_config`` (JSONB)
    is normalized to a dict/None since a raw ``text()`` read can surface it as a
    JSON string (see ``mcp_node.parse_tool_config``)."""
    from uuid import UUID
    try:
        UUID(str(job_id))
    except (ValueError, TypeError):
        return {"error": "Invalid job_id format", "http_status": 400}
    job = (await db.execute(
        text("SELECT status FROM jobs WHERE id = :j"), {"j": job_id},
    )).mappings().first()
    if not job:
        return {"error": f"Job {job_id} not found", "http_status": 404}
    rows = (await db.execute(
        text(
            "SELECT node_key, title, description, status, depends_on, "
            "       execution_order, edit_version, prompt_template, "
            "       assigned_model, tool, "
            "       COALESCE(is_deliverable, FALSE) AS is_deliverable, tool_config "
            "FROM dag_nodes WHERE job_id = :j "
            "ORDER BY execution_order, node_key"
        ),
        {"j": job_id},
    )).mappings().all()
    nodes = []
    for r in rows:
        n = dict(r)
        tc = n.get("tool_config")
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except (ValueError, TypeError):
                tc = None
        n["tool_config"] = tc
        n["depends_on"] = list(n.get("depends_on") or [])
        nodes.append(n)
    return {
        "job_id": str(job_id),
        "job_status": job["status"],
        "nodes": nodes,
    }


def _transitive_downstream(nodes: list[dict], node_key: str) -> set[str]:
    """Keys that transitively depend on ``node_key`` (excludes it)."""
    rev: dict[str, set[str]] = {}
    for n in nodes:
        for parent in (n["depends_on"] or []):
            rev.setdefault(parent, set()).add(n["node_key"])
    seen: set[str] = set()
    queue = deque(rev.get(node_key, set()))
    while queue:
        k = queue.popleft()
        if k in seen:
            continue
        seen.add(k)
        queue.extend(rev.get(k, set()))
    return seen


def _cycle_nodes(deps_by_key: dict[str, list[str]]) -> set[str]:
    """The nodes Kahn's cannot order — those in a cycle, or behind one."""
    keys = set(deps_by_key)
    indeg = {k: len(deps_by_key[k]) for k in keys}
    adj: dict[str, list[str]] = {k: [] for k in keys}
    for k, deps in deps_by_key.items():
        for d in deps:
            if d in adj:
                adj[d].append(k)
    queue = deque([k for k in keys if indeg[k] == 0])
    ordered: set[str] = set()
    while queue:
        k = queue.popleft()
        ordered.add(k)
        for nxt in adj[k]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return keys - ordered


def _find_cycle(deps_by_key: dict[str, list[str]]) -> list[str]:
    """One concrete cycle as a path, so the message can NAME it. An operator
    told "there is a cycle" and nothing else cannot act on it."""
    color: dict[str, int] = {}
    stack: list[str] = []

    def walk(u: str) -> list[str] | None:
        color[u] = 1
        stack.append(u)
        for v in deps_by_key.get(u, []):
            if v not in deps_by_key:
                continue
            if color.get(v) == 1:
                return stack[stack.index(v):] + [v]
            if color.get(v, 0) == 0:
                hit = walk(v)
                if hit:
                    return hit
        stack.pop()
        color[u] = 2
        return None

    for k in deps_by_key:
        if color.get(k, 0) == 0:
            hit = walk(k)
            if hit:
                return hit
    return []


def _validate_graph(
    deps_by_key: dict[str, list[str]], *,
    baseline: dict[str, list[str]] | None = None,
) -> str | None:
    """Return an error message if any dep ref is unknown or the graph has a
    cycle, else None.

    §17.1210 — `baseline` is the graph WITHOUT the caller's edit, and it exists
    because this function used to answer "edit would create a dependency cycle"
    for a plan that was already cyclic before anyone touched it. Two harms, and
    the second is the bad one:

    1. It blamed the edit. Live, on a 131-node plan carrying EIGHT cycles from
       earlier re-plans, every insert was refused with a message pointing at the
       wrong thing — and naming no node, so there was nothing to go and look at.
    2. It made the cycles PERMANENT. Every call site validates the whole graph,
       so with a cycle present no edit passed — including the edits that would
       remove it. The only tool for fixing a cycle was locked behind the cycle.

    So: an edit is judged on whether it makes things WORSE. When the baseline is
    already cyclic, an edit that adds no node to the tangle is allowed through
    (that is how a plan gets repaired), and one that widens it is refused naming
    what it added. With an acyclic baseline the old behaviour stands exactly.
    """
    keys = set(deps_by_key)
    for k, deps in deps_by_key.items():
        for d in deps:
            if d not in keys:
                return f"node {k} depends on unknown node {d}"
            if d == k:
                return f"node {k} depends on itself"
    stuck = _cycle_nodes(deps_by_key)
    if not stuck:
        return None
    cyc = _find_cycle(deps_by_key)
    shown = " → ".join(cyc) if cyc else ", ".join(sorted(stuck)[:8])
    if baseline is None:
        return f"the plan's dependencies contain a cycle: {shown}"
    was = _cycle_nodes(baseline)
    if stuck <= was:
        logger.warning(
            "node_edit_on_cyclic_plan: allowed (no worse) cycle=%s stuck=%d", shown, len(stuck),
        )
        return None
    added = sorted(stuck - was)
    return (f"this edit would pull {', '.join(added[:6])} into a dependency cycle: {shown}"
            if was else f"this edit would create a dependency cycle: {shown}")


async def _renumber(db: AsyncSession, job_id: str, ordered_keys: list[str]) -> None:
    for i, nk in enumerate(ordered_keys):
        await db.execute(
            text(
                "UPDATE dag_nodes SET execution_order = :i, updated_at = now() "
                "WHERE job_id = :j AND node_key = :nk"
            ),
            {"i": i, "j": job_id, "nk": nk},
        )


async def _audit(
    db: AsyncSession, job_id: str, node_key: str, op: str,
    before, after, edited_by: str | None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO dag_node_edits (job_id, node_key, op, before, after, edited_by) "
            "VALUES (:j, :nk, :op, CAST(:b AS JSONB), CAST(:a AS JSONB), :by)"
        ),
        {
            "j": job_id, "nk": node_key, "op": op,
            "b": json.dumps(before) if before is not None else None,
            "a": json.dumps(after) if after is not None else None,
            "by": edited_by,
        },
    )


async def _reset_keys(db: AsyncSession, job_id: str, keys: list[str]) -> None:
    """Reset nodes to pending — and write down what they were first.

    §17.1211 — this destroyed status, output, both timestamps and the failure
    reason for every key, and recorded NOTHING. When it fired wrongly on a live
    job the only reason the work was recoverable is that `assist_steps` happened
    to carry the same record; an autonomous job has no such second copy and the
    loss would have been permanent. The pre-image goes to `dag_node_edits` as a
    `reset` row, which is the table a revert would read.
    """
    if not keys:
        return
    rows = (await db.execute(
        text("SELECT node_key, status, output_text, started_at, completed_at, "
             "       last_verification_reason "
             "  FROM dag_nodes WHERE job_id = :j AND node_key = ANY(:keys) "
             "   AND status <> 'pending'"),
        {"j": job_id, "keys": keys},
    )).mappings().all()
    for r in rows:
        await _audit(
            db, job_id, r["node_key"], "reset",
            {"status": r["status"], "output_text": r["output_text"],
             "started_at": r["started_at"].isoformat() if r["started_at"] else None,
             "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
             "last_verification_reason": r["last_verification_reason"]},
            {"status": "pending"}, "node_editor:reset",
        )
    await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'pending', output_text = NULL, "
            "started_at = NULL, completed_at = NULL, "
            "last_verification_reason = NULL, updated_at = now() "
            "WHERE job_id = :j AND node_key = ANY(:keys)"
        ),
        {"j": job_id, "keys": keys},
    )
    if rows:
        logger.warning("node_reset_preimage job=%s nodes=%d keys=%s",
                       job_id, len(rows), sorted(r["node_key"] for r in rows))


async def _reopen_job(db: AsyncSession, job_id: str) -> None:
    """A reset/edit that invalidates output re-opens a TERMINAL job so the
    executor will pick the reset nodes back up.

    §17.1211 — `blocked` is not terminal and must not be swept up here. It is
    re-enterable already, and rewriting it to `executing` claims a run is in
    flight when none is: live, a `blocked` job became `executing` with zero
    running nodes because an edit touched it, and every surface then said the
    engine was working on something it was not. The three genuinely terminal
    statuses still reopen, because there the status would otherwise outrank the
    work that has just been invalidated.
    """
    res = await db.execute(
        text(
            "UPDATE jobs SET status = 'executing', compiled_output = NULL, "
            "updated_at = now() "
            "WHERE id = :j AND status IN ('completed', 'failed', 'cancelled') "
            "RETURNING id"
        ),
        {"j": job_id},
    )
    if res.fetchone() is not None:
        logger.warning("node_edit_reopened_terminal_job job=%s -> executing", job_id)


def _version_conflict(node: dict, expected_version: int | None) -> dict | None:
    """Optimistic-lock check. expected_version=None → lenient (last-write-wins,
    logged). Mismatch → 409 dict."""
    if expected_version is None:
        return None
    if int(node["edit_version"]) != int(expected_version):
        return {
            "error": (
                f"stale edit_version: expected {expected_version}, "
                f"current {node['edit_version']}"
            ),
            "http_status": 409,
        }
    return None


async def _bump_version(db: AsyncSession, job_id: str, node_key: str) -> None:
    await db.execute(
        text(
            "UPDATE dag_nodes SET edit_version = edit_version + 1, updated_at = now() "
            "WHERE job_id = :j AND node_key = :nk"
        ),
        {"j": job_id, "nk": node_key},
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

async def edit_node(
    job_id: str, node_key: str, fields: dict, *,
    expected_version: int | None = None, edited_by: str | None = None,
    db: AsyncSession, cascade: bool = False,
) -> dict:
    """§17.1288r — an INVALIDATING edit (depends_on, prompt, …) of a node that
    is not pending resets that node; the cascade over its transitive
    downstream is OPT-IN, as §17.1284 made it for `reset_node`. Live: making
    ADD82 and ADD65 depend on a new install step reset twenty-one nodes behind
    them, un-doing sixteen `skipped` decisions settled weeks earlier (restored
    from the pre-images). What was NOT touched is returned as ``downstream_kept``."""
    nodes = await _load_nodes(db, job_id)
    by_key = {n["node_key"]: n for n in nodes}
    node = by_key.get(node_key)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}

    conflict = _version_conflict(node, expected_version)
    if conflict:
        return conflict

    updates = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not updates:
        return {"error": "no editable fields provided", "http_status": 400}

    # If depends_on changes, validate the post-edit graph.
    if "depends_on" in updates:
        new_deps = list(updates["depends_on"] or [])
        base = {n["node_key"]: list(n["depends_on"] or []) for n in nodes}
        deps_by_key = {**base, node_key: new_deps}
        err = _validate_graph(deps_by_key, baseline=base)
        if err:
            return {"error": err, "http_status": 400}

    before = {k: node.get(k) for k in updates}

    # Build the UPDATE. depends_on is a text[]; the rest are scalar columns.
    set_parts = []
    params: dict = {"j": job_id, "nk": node_key}
    col_map = {
        "title": "title", "description": "description",
        "prompt_template": "prompt_template", "tool": "tool",
        "assigned_model": "assigned_model", "is_deliverable": "is_deliverable",
        "depends_on": "depends_on", "tool_config": "tool_config",
    }
    for k, v in updates.items():
        col = col_map[k]
        if k == "tool_config":
            # JSONB column — bind the JSON text and cast (§17.772).
            set_parts.append(f"{col} = CAST(:{k} AS jsonb)")
            params[k] = json.dumps(v) if v is not None else None
        else:
            set_parts.append(f"{col} = :{k}")
            params[k] = v
    await db.execute(
        text(
            f"UPDATE dag_nodes SET {', '.join(set_parts)}, updated_at = now() "
            f"WHERE job_id = :j AND node_key = :nk"
        ),
        params,
    )
    await _bump_version(db, job_id, node_key)

    # Output invalidation: an invalidating edit to an already-run node resets
    # it + transitive downstream (their inputs / this output are now stale).
    reset_keys: list[str] = []
    downstream_kept: list[str] = []
    # §17.1211 — an edit that only REMOVES dependencies invalidates nothing.
    # The node ran; dropping a prerequisite it never actually needed does not
    # make what it produced stale. This reset fired on ANY `depends_on` change,
    # so cutting eight spurious edges out of a tangled plan reset 31 finished
    # nodes to pending and wiped their output — on a live job, and with no
    # pre-image (see `_reset_keys`). The recovery only existed because
    # `assist_steps` happened to hold the same record.
    touched = set(updates)
    if touched == {"depends_on"}:
        was = set(node["depends_on"] or [])
        now = set(updates["depends_on"] or [])
        if now <= was:                      # purely a removal (or a no-op)
            touched = set()
            logger.info(
                "node_edit_deps_narrowed job=%s node=%s dropped=%s (no reset)",
                job_id, node_key, sorted(was - now),
            )
    if INVALIDATING_FIELDS & touched and node["status"] != "pending":
        # Compute downstream over the POST-edit graph (the node's new deps
        # don't affect who depends on IT, but keep the snapshot consistent).
        post_nodes = [dict(n) for n in nodes]
        if "depends_on" in updates:
            for n in post_nodes:
                if n["node_key"] == node_key:
                    n["depends_on"] = list(updates["depends_on"] or [])
        all_downstream = _transitive_downstream(post_nodes, node_key)
        downstream = all_downstream if cascade else set()          # §17.1288r — opt-in
        downstream_kept = sorted(all_downstream - downstream)
        reset_keys = sorted({node_key} | downstream)
        await _reset_keys(db, job_id, reset_keys)
        await _reopen_job(db, job_id)

    after = {k: updates[k] for k in updates}
    await _audit(db, job_id, node_key, "edit", before, after, edited_by)
    await db.commit()
    logger.info(
        "node_edit job=%s node=%s fields=%s reset=%s",
        job_id, node_key, list(updates), reset_keys,
    )
    return {"status": "ok", "node_key": node_key, "updated": list(updates),
            "reset": reset_keys, "downstream_kept": downstream_kept}


async def insert_node(
    job_id: str, spec: dict, *, edited_by: str | None = None, db: AsyncSession,
) -> dict:
    node_key = spec.get("node_key")
    title = spec.get("title")
    if not node_key or not title:
        return {"error": "node_key and title are required", "http_status": 400}
    nodes = await _load_nodes(db, job_id)
    if not nodes:
        return {"error": f"job {job_id} has no DAG", "http_status": 404}
    by_key = {n["node_key"]: n for n in nodes}
    if node_key in by_key:
        return {"error": f"node {node_key} already exists", "http_status": 409}

    new_deps = list(spec.get("depends_on") or [])
    base = {n["node_key"]: list(n["depends_on"] or []) for n in nodes}
    deps_by_key = {**base, node_key: new_deps}
    err = _validate_graph(deps_by_key, baseline=base)
    if err:
        return {"error": err, "http_status": 400}

    # Append at the end of execution order (renumber keeps it contiguous).
    new_order = len(nodes)
    await db.execute(
        text(
            "INSERT INTO dag_nodes "
            "(job_id, node_key, title, description, node_type, status, "
            " depends_on, tool, prompt_template, assigned_model, "
            " execution_order, is_deliverable, tool_config) "
            "VALUES (:j, :nk, :title, :descr, :ntype, 'pending', :deps, :tool, "
            "        :prompt, :model, :order, :deliv, CAST(:tcfg AS jsonb))"
        ),
        {
            "j": job_id, "nk": node_key, "title": title,
            "descr": spec.get("description"),
            "ntype": spec.get("node_type", "task"),
            "deps": new_deps, "tool": spec.get("tool", "LLM"),
            "prompt": spec.get("prompt_template"),
            "model": spec.get("assigned_model"),
            "order": new_order,
            "deliv": bool(spec.get("is_deliverable", False)),
            "tcfg": json.dumps(spec["tool_config"])
            if spec.get("tool_config") is not None else None,
        },
    )
    # Re-number so order is contiguous (defensive if prior gaps existed).
    ordered = [n["node_key"] for n in nodes] + [node_key]
    await _renumber(db, job_id, ordered)
    await _audit(db, job_id, node_key, "insert", None, spec, edited_by)
    # §17.600 — re-open a terminal job so the newly-inserted 'pending' node is
    # actually scheduled. edit/delete/reset_node all do this; insert_node
    # didn't, so inserting into a completed/failed/blocked/cancelled job left
    # it terminal and the new node never ran (with a misleading 200 'ok').
    await _reopen_job(db, job_id)
    await db.commit()
    logger.info("node_insert job=%s node=%s deps=%s", job_id, node_key, new_deps)
    return {"status": "ok", "node_key": node_key}


async def delete_node(
    job_id: str, node_key: str, *, edited_by: str | None = None, db: AsyncSession,
) -> dict:
    nodes = await _load_nodes(db, job_id)
    by_key = {n["node_key"]: n for n in nodes}
    node = by_key.get(node_key)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}
    if len(nodes) <= 1:
        return {"error": "cannot delete the last node", "http_status": 400}

    # Dependents must be rewired (drop the deleted key from their depends_on).
    dependents = [n["node_key"] for n in nodes if node_key in (n["depends_on"] or [])]
    deps_by_key = {
        n["node_key"]: [d for d in (n["depends_on"] or []) if d != node_key]
        for n in nodes if n["node_key"] != node_key
    }
    err = _validate_graph(
        deps_by_key,
        baseline={n["node_key"]: list(n["depends_on"] or []) for n in nodes},
    )
    if err:
        return {"error": err, "http_status": 400}

    # Apply the rewire.
    for dep_key in dependents:
        await db.execute(
            text(
                "UPDATE dag_nodes SET depends_on = :deps, updated_at = now() "
                "WHERE job_id = :j AND node_key = :nk"
            ),
            {"deps": deps_by_key[dep_key], "j": job_id, "nk": dep_key},
        )
    await db.execute(
        text("DELETE FROM dag_nodes WHERE job_id = :j AND node_key = :nk"),
        {"j": job_id, "nk": node_key},
    )
    # Renumber remaining nodes and cascade-reset rewired dependents (+ their
    # downstream) since their input set changed.
    remaining = [n["node_key"] for n in nodes if n["node_key"] != node_key]
    await _renumber(db, job_id, remaining)
    reset_keys: list[str] = []
    if dependents:
        post = await _load_nodes(db, job_id)
        ds: set[str] = set(dependents)
        for d in dependents:
            ds |= _transitive_downstream(post, d)
        reset_keys = sorted(ds)
        await _reset_keys(db, job_id, reset_keys)
        await _reopen_job(db, job_id)
    await _audit(db, job_id, node_key, "delete", node, None, edited_by)
    await db.commit()
    logger.info(
        "node_delete job=%s node=%s rewired=%s reset=%s",
        job_id, node_key, dependents, reset_keys,
    )
    return {"status": "ok", "node_key": node_key, "rewired": dependents,
            "reset": reset_keys}


async def reorder_nodes(
    job_id: str, ordered_keys: list[str], *, edited_by: str | None = None,
    db: AsyncSession,
) -> dict:
    nodes = await _load_nodes(db, job_id)
    existing = {n["node_key"] for n in nodes}
    if set(ordered_keys) != existing:
        return {
            "error": "ordered_keys must be a permutation of the job's node_keys",
            "http_status": 400,
        }
    before = [n["node_key"] for n in nodes]
    await _renumber(db, job_id, ordered_keys)
    await _audit(db, job_id, "*", "reorder", {"order": before},
                 {"order": ordered_keys}, edited_by)
    await db.commit()
    logger.info("node_reorder job=%s order=%s", job_id, ordered_keys)
    return {"status": "ok", "order": ordered_keys}


async def mark_satisfied(
    job_id: str, node_key: str, *, evidence: str, edited_by: str | None = None, db: AsyncSession,
) -> dict:
    """§17.1226 — record that ONE step's goal is already met, with the evidence,
    and cascade NOTHING.

    The gap this closes, live: ADD50 ran `pct start 111` through the runner, the
    SSE response was lost, and the step was recorded `failed`. Two read-only
    checks then showed container 111 running with `node` listening on `*:3001`
    — the work HAD happened. There was no way to say so. `reset_node` is the
    only status write the API offers and it resets the transitive downstream,
    which here would have re-opened a `skipped` sibling, "Set VM 106's scsi0
    disk to 40G", on a disk that had just been grown to 100G. The only safe
    option was to leave a true thing recorded as false.

    So: this marks exactly the named node `done`, appends the evidence to its
    output, records the pre-image (§17.1211) and touches nothing else. It is
    NOT a way to skip work — the caller supplies the evidence and it is written
    into the node where the next step reads it.

    Refuses a node already `done` (nothing to correct) and a `running` one (a
    worker owns it).
    """
    if not (evidence or "").strip():
        return {"error": "evidence is required — a step is not done because someone said so",
                "http_status": 422}
    nodes = await _load_nodes(db, job_id)
    node = next((n for n in nodes if n["node_key"] == node_key), None)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}
    if node["status"] == "done":
        return {"error": f"{node_key} is already done", "http_status": 409}
    if node["status"] == "running":
        return {"error": f"{node_key} is running — a worker owns it; wait for it to land",
                "http_status": 409}
    prior = (await db.execute(
        text("SELECT output_text FROM dag_nodes WHERE job_id = :j AND node_key = :nk"),
        {"j": job_id, "nk": node_key})).scalar() or ""
    block = ("## Recorded as already done\n\n" + evidence.strip())
    await db.execute(
        text("UPDATE dag_nodes SET status = 'done', completed_at = NOW(), updated_at = NOW(), "
             "started_at = COALESCE(started_at, NOW()), "
             "output_text = CASE WHEN COALESCE(output_text, '') = '' THEN :block "
             "ELSE output_text || :sep || :block END, "
             "last_verification_reason = :why "
             "WHERE job_id = :j AND node_key = :nk AND status <> 'running'"),
        {"j": job_id, "nk": node_key, "block": block, "sep": "\n\n",
         "why": "recorded as already done, with evidence (§17.1226)"})
    await _audit(db, job_id, node_key, "satisfied",
                 {"status": node["status"], "output_text": prior[:8000]},
                 {"status": "done", "evidence": evidence[:4000], "cascade": []},
                 edited_by)
    await db.commit()
    logger.info("node_marked_satisfied job=%s node=%s was=%s evidence_chars=%d",
                job_id, node_key, node["status"], len(evidence))
    return {"status": "ok", "node_key": node_key, "was": node["status"],
            "node_status": "done", "cascade": []}


async def reset_node(
    job_id: str, node_key: str, *, edited_by: str | None = None, db: AsyncSession,
    cascade: bool = False,
) -> dict:
    """Reset ANY-status node to pending; with ``cascade=True`` also its
    transitive downstream. Generalizes retry_failed_node beyond FAILED; does
    NOT bump retry_count.

    §17.1284 — the cascade is OPT-IN. Resetting ADD26 (failed, to retry it)
    reset sixteen nodes behind it, un-doing one `done` and twelve `skipped`
    steps the operator had settled weeks earlier -- recoverable only because
    §17.1211's pre-image existed, the shape §17.1216 recorded as still open. A
    retry of a failed step is the common case and touches nothing else; work
    built on an answer that CHANGED is `revise_decision`'s job, and it cascades
    on purpose. What was NOT touched is returned as ``downstream_kept``."""
    nodes = await _load_nodes(db, job_id)
    node = next((n for n in nodes if n["node_key"] == node_key), None)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}

    all_downstream = _transitive_downstream(nodes, node_key)
    downstream = all_downstream if cascade else set()
    reset_keys = sorted({node_key} | downstream)
    await _reset_keys(db, job_id, reset_keys)
    await _reopen_job(db, job_id)
    await _audit(
        db, job_id, node_key, "reset",
        {"status": node["status"]}, {"status": "pending", "cascade": sorted(downstream)},
        edited_by,
    )
    await db.commit()
    logger.info(
        "node_reset job=%s node=%s downstream=%s", job_id, node_key, sorted(downstream),
    )
    return {"status": "ok", "node_key": node_key, "reset": reset_keys,
            "downstream_reset": sorted(downstream),
            "downstream_kept": sorted(all_downstream - downstream)}


async def revise_decision(
    job_id: str, node_key: str, *, choice: str, note: str = "",
    edited_by: str | None = None, db: AsyncSession,
) -> dict:
    """§17.1241 — the operator changed their mind, and there was no way to say so.

    A decision is recorded once, into the node's ``output_text``, and downstream
    steps read it as their specification. Nothing could revise it. Live: ADD99
    ("what should the control panel do for you?") was answered, and the operator
    then corrected the answer — four capabilities down to three. The recorded
    decision still said four, ADD100 read the record over the corrected
    description, and built four. Getting the record right took resetting the
    decision node, running the job until the executor re-parked it, and
    answering the pause a second time — three steps and a full run, for a
    sentence.

    So: rewrite the record in place, keep the pre-image (§17.1211), and reset the
    steps that were built on the old answer so they build on the new one. That
    cascade is the POINT here, unlike §17.1226's — work derived from a decision
    the operator has changed is work against a spec that no longer exists.

    The node must be a ``decision`` that has already been answered; anything else
    is a different operation (``reset_node`` to ask again, ``mark_satisfied`` to
    correct a status).
    """
    if not (choice or "").strip():
        return {"error": "choice is required — a revision has to say what the answer is now",
                "http_status": 422}
    nodes = await _load_nodes(db, job_id)
    node = next((n for n in nodes if n["node_key"] == node_key), None)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}
    if (node.get("node_type") or "") != "decision":
        return {"error": f"{node_key} is not a decision node", "http_status": 409}
    if node["status"] != "done":
        return {"error": f"{node_key} has not been answered yet (it is {node['status']}) — "
                         f"there is nothing to revise", "http_status": 409}

    from app.modules.decision_pause import decision_record
    prior = (await db.execute(
        text("SELECT output_text FROM dag_nodes WHERE job_id = :j AND node_key = :nk"),
        {"j": job_id, "nk": node_key})).scalar() or ""
    record = decision_record(choice, note, None)

    await db.execute(
        text("UPDATE dag_nodes SET output_text = :out, updated_at = NOW(), "
             "last_verification_reason = :why WHERE job_id = :j AND node_key = :nk"),
        {"j": job_id, "nk": node_key, "out": record,
         "why": "decision revised by the operator (§17.1241)"})
    # the steps built on the old answer have to be built again
    downstream = sorted(_transitive_downstream(nodes, node_key))
    if downstream:
        await _reset_keys(db, job_id, downstream)
        await _reopen_job(db, job_id)
    await _audit(db, job_id, node_key, "revise_decision",
                 {"output_text": prior[:8000]},
                 {"output_text": record[:8000], "cascade": downstream}, edited_by)
    await db.commit()
    logger.info("decision_revised job=%s node=%s cascade=%d", job_id, node_key, len(downstream))
    return {"status": "ok", "node_key": node_key, "decision": record,
            "rebuilt": downstream}
