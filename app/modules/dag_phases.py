"""§17.1007 — chunk a DAG's nodes into phases, computed on read.

Why this exists
---------------
The assist walkthrough showed one counter for the whole session: ``13/41 steps
done``. Effort rises as a goal comes into view, but 12→13 of 41 is
perceptually indistinguishable from 13→14, so that gradient never engages. A
long flat bar reads as "this will never end" — which is the honest reading of
it, and the demotivating one. Chunking the same 41 steps into six phases gives
the operator a summit they can actually reach, six times per session instead of
once.

Why it is derived, not stored
-----------------------------
Two alternatives were rejected:

* **Ask the planner for a phase name.** The DAG generator is few-shot prompted
  (``dag_generator.py``), so a new field means editing every worked example and
  hoping the model fills it. That changes planner output quality in ways this
  change has no business changing, and yields nothing for the DAGs that already
  exist.
* **A ``phase`` column plus a backfill.** Same derivation as below, but frozen
  at write time and needing a migration. Nothing else wants to query by phase.

So this is compute-on-read, the same call the §17.811 progress/ETA snapshot
makes. It is O(nodes + edges) over at most a few dozen rows.

How the chunking works
----------------------
Phase boundaries follow the dependency graph, never a fixed stride: a node's
*level* is the longest path from any root, so two steps that can run in
parallel always land in the same phase and a phase boundary is always a real
"this cannot start until that finishes" line.

Raw levels alone are not enough. This engine's plans are frequently a near
linear chain (``T1 → T2 → … → T41``), where every node is its own level and
"Phase 13 of 41" is exactly the bar we set out to replace. So adjacent levels
are merged into at most ``TARGET_PHASES`` buckets. The merge walks levels in
order and closes a bucket once it holds its share, which keeps whole levels
intact — a parallel fan-out is never split down the middle.

That rule had a cost: a plan whose work sat almost entirely in ONE wide level
(25 steps all depending on the same predecessor and nothing else) collapsed to
a phase holding 25 steps — the flat bar again, wearing a phase label. §17.1008
splits such a level, and only such a level. It is safe precisely because a
level has no INTERNAL dependencies: every node in it is ready at the same
moment, so a boundary drawn inside one orders steps that were already
unordered, and orders nothing that was ordered. Boundaries between different
levels still mean "this could not start until that finished"; a boundary
inside one means "this is as much of it as you need to think about at once",
and the badge reads the same either way.

A cycle (which the well-formedness passes should prevent, §17.668–670) degrades
to a single phase rather than raising: this decorates a progress badge, and no
display concern should be able to break the walkthrough.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Any, Iterable

# Aim for a handful of phases. Six is a compromise: enough that a boundary
# arrives within a working session, few enough that "Phase 2 of 6" is a shape
# the operator can hold in their head.
TARGET_PHASES = 6

# Below this, chunking is noise — an 8-step plan is already legible as a whole,
# and splitting it into six phases of one or two steps would add ceremony
# without adding a gradient.
MIN_NODES_FOR_PHASES = 9

# A single level wider than this multiple of an even share is cut into pieces
# (§17.1008). 1.5 rather than 2: at twice a share the phase is already the
# thing this module exists to prevent.
OVERSIZE_FACTOR = 1.5

# ...but never a level this small, whatever the ratio says. On a 16-node plan an
# even share is 2.7, so the ratio alone splits a group of five — and five
# parallel steps are a group an operator reads as one thing. Splitting them buys
# no gradient and loses the true statement that they can be done in any order.
# The shape this splitting exists for is 25-of-27, not 5-of-16.
MIN_SPLITTABLE_LEVEL = 6


def _plan_order(node: dict[str, Any]) -> tuple:
    """The order the plan runs, tolerating a null execution_order."""
    order = node.get("execution_order")
    return (
        order if order is not None else 1 << 30,
        len(str(node["node_key"])),
        str(node["node_key"]),
    )


def _levels(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Longest-path depth per node_key. Unknown deps are treated as roots."""
    keys = {n["node_key"] for n in nodes}
    deps: dict[str, list[str]] = {
        n["node_key"]: [d for d in (n.get("depends_on") or []) if d in keys]
        for n in nodes
    }
    dependents: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {k: 0 for k in keys}
    for key, ds in deps.items():
        for d in ds:
            dependents[d].append(key)
            indegree[key] += 1

    level = {k: 0 for k in keys}
    queue = deque(sorted(k for k, v in indegree.items() if v == 0))
    seen = 0
    while queue:
        key = queue.popleft()
        seen += 1
        for child in dependents.get(key, ()):
            # Longest path: a node sits one below its DEEPEST prerequisite.
            if level[child] < level[key] + 1:
                level[child] = level[key] + 1
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if seen != len(keys):
        # Cycle — every node collapses to one phase. See module docstring.
        return {k: 0 for k in keys}
    return level


def _merge_levels(
    ordered_levels: list[int], sizes: dict[int, int], target: int
) -> dict[int, int]:
    """Map each level → a phase index, merging adjacent levels into ≤ target
    buckets while never splitting a level across two phases.

    Balances by NODE COUNT, not level count. Balancing by level count is the
    obvious implementation and it is wrong here: on a real 41-node plan it put
    18 of the 41 steps in one phase, because a single wide level counts the
    same as a narrow one. A phase holding nearly half the work is the flat bar
    this module exists to break up, just with a different label on it.

    Proportional, not greedy. A greedy "close the bucket once it holds its
    share" pass recomputes the share as it goes, closes every bucket one node
    early, and dumps the accumulated remainder in the tail — 41 linear nodes
    came out [6, 6, 6, 6, 6, 11] and 100 came out with a final phase double the
    others. Placing each level by the fraction of the plan that precedes it has
    no such drift: the boundaries fall where they should by construction.
    """
    if len(ordered_levels) <= target:
        return {lv: i for i, lv in enumerate(ordered_levels)}
    total = sum(sizes.values()) or 1
    mapping: dict[int, int] = {}
    preceding = 0
    for lv in ordered_levels:
        # Place a level by its MIDPOINT, not by the work that precedes it.
        # Keyed on `preceding` alone, a wide level always joins the bucket
        # before it (its own nodes are not yet counted), which on the real
        # 41-node plan produced a 15-step opening phase. The midpoint puts a
        # level in the bucket its own mass falls in. Monotonic, since the
        # midpoint only ever increases.
        midpoint = preceding + sizes[lv] / 2
        mapping[lv] = min(target - 1, int(midpoint * target / total))
        preceding += sizes[lv]
    return mapping


def compute_phases(nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """``{node_key: {phase, phase_total, phase_pos, phase_size}}`` — all
    1-indexed for display, because these numbers are read by a person.

    Returns ``{}`` for a plan too small to be worth chunking, which callers
    should treat as "render the plain total", not as an error.
    """
    nodes = [n for n in nodes if n.get("node_key")]
    if len(nodes) < MIN_NODES_FOR_PHASES:
        return {}

    level = _levels(nodes)
    ordered_levels = sorted(set(level.values()))
    if len(ordered_levels) < 2:
        return {}  # one level — a flat fan-out has no sequence to chunk
    level_sizes: dict[int, int] = defaultdict(int)
    for node in nodes:
        level_sizes[level[node["node_key"]]] += 1
    level_to_phase = _merge_levels(ordered_levels, level_sizes, TARGET_PHASES)

    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        members[level_to_phase[level[node["node_key"]]]].append(node)

    # §17.1008 — split a level too wide to be one phase.
    #
    # Only a bucket holding a SINGLE level qualifies: that is the degenerate
    # shape (nothing was merged into it, it is simply enormous), and it is the
    # only case where cutting inside is safe. `share` is the size a phase would
    # be if the plan divided evenly; a bucket beyond OVERSIZE_FACTOR of it gets
    # cut into even pieces.
    share = max(1, len(nodes) / TARGET_PHASES)
    split_members: dict[float, list[dict[str, Any]]] = {}
    for bucket in sorted(members):
        group = members[bucket]
        levels_here = {level[node["node_key"]] for node in group}
        if (
            len(levels_here) > 1
            or len(group) < MIN_SPLITTABLE_LEVEL
            or len(group) <= share * OVERSIZE_FACTOR
        ):
            split_members[float(bucket)] = group
            continue
        pieces = max(2, round(len(group) / share))
        ordered = sorted(group, key=_plan_order)
        for i in range(pieces):
            chunk = ordered[i * len(ordered) // pieces:(i + 1) * len(ordered) // pieces]
            if chunk:
                # Fractional keys keep the new pieces in place between the
                # surrounding buckets; the dense renumber below cleans up.
                split_members[bucket + i / (pieces + 1)] = chunk
    members = split_members  # type: ignore[assignment]

    # Renumber densely (a merge can leave a bucket empty) and order each
    # phase's steps the way the plan runs, so "step 3 of 7" counts in the same
    # direction the operator walks.
    phases = [p for p in sorted(members) if members[p]]
    total = len(phases)
    out: dict[str, dict[str, int]] = {}
    for display_index, phase in enumerate(phases, start=1):
        group = sorted(members[phase], key=_plan_order)
        for pos, node in enumerate(group, start=1):
            out[node["node_key"]] = {
                "phase": display_index,
                "phase_total": total,
                "phase_pos": pos,
                "phase_size": len(group),
            }
    return out
