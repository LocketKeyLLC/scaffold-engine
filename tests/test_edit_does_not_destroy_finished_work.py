"""§17.1211 — an edit must not quietly destroy work it did not invalidate.

Cutting eight spurious dependency edges out of a tangled 131-node plan reset 31
FINISHED nodes to pending and wiped their output, timestamps and failure
reasons — on the operator's live job. Three separate faults, and the middle one
is why it was nearly unrecoverable:

1. `edit_node` reset on ANY `depends_on` change. Removing a prerequisite a node
   never needed does not make what it produced stale.
2. `_reset_keys` destroyed status/output/timestamps and recorded NOTHING. The
   work was only recoverable because `assist_steps` happened to hold the same
   record; an autonomous job has no second copy.
3. `_reopen_job` swept `blocked` — which is not terminal — into `executing`, so
   every surface said a run was in flight when zero nodes were running.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import node_editor as ne


# ── 1. removing a dependency invalidates nothing ─────────────────────────

def test_a_pure_removal_does_not_reset():
    src = inspect.getsource(ne.edit_node)
    blk = src[src.index("touched = set(updates)"):src.index("INVALIDATING_FIELDS & touched")]
    assert 'if touched == {"depends_on"}:' in blk, blk
    assert "if now <= was:" in blk, "a subset of the old deps is a removal"
    assert "touched = set()" in blk, "a removal must clear the invalidating set"


def test_adding_a_dependency_still_resets():
    """The guard is narrow on purpose: a NEW prerequisite means the node may
    have run too early, so its output really is suspect."""
    src = inspect.getsource(ne.edit_node)
    blk = src[src.index("touched = set(updates)"):src.index("INVALIDATING_FIELDS & touched")]
    assert "now <= was" in blk and "now < was" not in blk, \
        "must compare as a subset, so an ADDED dep falls through to the reset"


def test_editing_any_other_field_still_resets():
    """Only a lone depends_on edit is exempt — changing the instructions means
    the work was done against something else."""
    src = inspect.getsource(ne.edit_node)
    assert 'touched == {"depends_on"}' in src, \
        "the exemption must require depends_on to be the ONLY field edited"


# ── 2. the reset writes down what it destroys ────────────────────────────

def test_the_reset_records_a_preimage_before_it_writes():
    src = inspect.getsource(ne._reset_keys)
    assert "SELECT node_key, status, output_text" in src, "it must read before it destroys"
    assert '"reset"' in src, "the pre-image is audited as a reset row"
    assert src.index("SELECT node_key, status") < src.index("UPDATE dag_nodes SET status = 'pending'"), \
        "the read must precede the write or there is nothing left to record"


def test_the_preimage_carries_every_field_the_reset_nulls():
    src = inspect.getsource(ne._reset_keys)
    nulled = ("status", "output_text", "started_at", "completed_at", "last_verification_reason")
    for f in nulled:
        assert f'"{f}"' in src, f"{f} is destroyed but not recorded"


def test_a_node_already_pending_is_not_audited():
    """Nothing to record — and 31 noise rows per edit would bury the real ones."""
    assert "status <> 'pending'" in inspect.getsource(ne._reset_keys)


# ── 3. blocked is not terminal ───────────────────────────────────────────

def test_reopen_leaves_a_blocked_job_alone():
    src = inspect.getsource(ne._reopen_job)
    assert "'completed', 'failed', 'cancelled'" in src
    assert "'blocked'" not in src.split('"""', 2)[-1], \
        "blocked is re-enterable already; rewriting it to executing is a false claim"


def test_reopen_still_revives_the_three_terminal_statuses():
    src = inspect.getsource(ne._reopen_job)
    for s in ("completed", "failed", "cancelled"):
        assert f"'{s}'" in src, s


def test_reopen_says_when_it_acted():
    """A status change nobody asked for must at least be findable in the log."""
    assert "node_edit_reopened_terminal_job" in inspect.getsource(ne._reopen_job)
