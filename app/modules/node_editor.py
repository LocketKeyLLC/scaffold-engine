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


def _validate_graph(deps_by_key: dict[str, list[str]]) -> str | None:
    """Return an error message if any dep ref is unknown or the graph has a
    cycle (Kahn's), else None."""
    keys = set(deps_by_key)
    for k, deps in deps_by_key.items():
        for d in deps:
            if d not in keys:
                return f"node {k} depends on unknown node {d}"
            if d == k:
                return f"node {k} depends on itself"
    # Kahn's topo sort: in-degree of a node = number of its dependencies;
    # an edge dep -> node lets us decrement as deps are satisfied.
    indeg = {k: len(deps_by_key[k]) for k in keys}
    adj: dict[str, list[str]] = {k: [] for k in keys}
    for k, deps in deps_by_key.items():
        for d in deps:
            adj[d].append(k)
    queue = deque([k for k in keys if indeg[k] == 0])
    visited = 0
    while queue:
        k = queue.popleft()
        visited += 1
        for nxt in adj[k]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited != len(keys):
        return "edit would create a dependency cycle"
    return None


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
    if not keys:
        return
    await db.execute(
        text(
            "UPDATE dag_nodes SET status = 'pending', output_text = NULL, "
            "started_at = NULL, completed_at = NULL, "
            "last_verification_reason = NULL, updated_at = now() "
            "WHERE job_id = :j AND node_key = ANY(:keys)"
        ),
        {"j": job_id, "keys": keys},
    )


async def _reopen_job(db: AsyncSession, job_id: str) -> None:
    """A reset/edit that invalidates output re-opens a terminal job so the
    executor will pick the reset nodes back up."""
    await db.execute(
        text(
            "UPDATE jobs SET status = 'executing', compiled_output = NULL, "
            "updated_at = now() "
            "WHERE id = :j AND status IN ('completed', 'failed', 'blocked', 'cancelled')"
        ),
        {"j": job_id},
    )


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
    db: AsyncSession,
) -> dict:
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
        deps_by_key = {n["node_key"]: list(n["depends_on"] or []) for n in nodes}
        deps_by_key[node_key] = new_deps
        err = _validate_graph(deps_by_key)
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
    if INVALIDATING_FIELDS & set(updates) and node["status"] != "pending":
        # Compute downstream over the POST-edit graph (the node's new deps
        # don't affect who depends on IT, but keep the snapshot consistent).
        post_nodes = [dict(n) for n in nodes]
        if "depends_on" in updates:
            for n in post_nodes:
                if n["node_key"] == node_key:
                    n["depends_on"] = list(updates["depends_on"] or [])
        downstream = _transitive_downstream(post_nodes, node_key)
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
            "reset": reset_keys}


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
    deps_by_key = {n["node_key"]: list(n["depends_on"] or []) for n in nodes}
    deps_by_key[node_key] = new_deps
    err = _validate_graph(deps_by_key)
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
    err = _validate_graph(deps_by_key)
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


async def reset_node(
    job_id: str, node_key: str, *, edited_by: str | None = None, db: AsyncSession,
) -> dict:
    """Reset ANY-status node to pending + cascade transitive downstream.
    Generalizes retry_failed_node beyond FAILED; does NOT bump retry_count."""
    nodes = await _load_nodes(db, job_id)
    node = next((n for n in nodes if n["node_key"] == node_key), None)
    if not node:
        return {"error": f"node {node_key} not found", "http_status": 404}

    downstream = _transitive_downstream(nodes, node_key)
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
            "downstream_reset": sorted(downstream)}
