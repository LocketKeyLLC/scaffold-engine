"""§17.1180 — the LOW findings from the 2026-09-25 review.

Each is small; several are not. The ones worth reading are the tool-name
comparison (a CodeGen node silently dropped from the deliverable), the
`manual` prerequisite (a recipe that could never be started), and the prune
that did nothing under exactly the load that made the bound matter.
"""
from __future__ import annotations

import inspect
import re

import pytest


def code(src: str) -> str:
    """Source with comments and docstrings stripped.

    Three assertions in this file tripped on their own explanatory comments
    before this existed: the comments name the defect (`if _known:`,
    `'pending','blocked'`) and a substring scan cannot tell prose from code.
    Scan what runs.
    """
    out = []
    for ln in src.split("\n"):
        stripped = ln.lstrip()
        if stripped.startswith("#") or stripped.startswith("--"):
            continue
        out.append(re.sub(r"\s+#\s.*$", "", ln))
    return "\n".join(out)


# ── a case-sensitive compare among two case-insensitive siblings ─────────────

def test_protected_leaf_tools_are_compared_case_insensitively():
    """`_PROTECTED_LEAF_TOOLS` holds "CodeGen"/"LLM" in display case, and
    `_select_dominant_leaves` tested membership exactly while its two siblings
    in the same file (`runbook_count`, `compute_deliverable_kind`) lowercase
    first. A node stored as `codegen` lost the protection the surrounding
    comment says exists to stop it being dropped from the deliverable."""
    from app.modules import execution_compile as ec
    assert ec._PROTECTED_LEAF_TOOLS_LC == {t.lower() for t in ec._PROTECTED_LEAF_TOOLS}
    src = code(inspect.getsource(ec._select_dominant_leaves))
    assert '.lower() in _PROTECTED_LEAF_TOOLS_LC' in src
    assert 'n.get("tool") in _PROTECTED_LEAF_TOOLS' not in src


def test_the_compile_read_orders_deterministically():
    """A NULL `execution_order` sorts last in Postgres ASC and, with no
    tiebreaker, ties came back in scan order — which is what selects the
    Strategy-2 deliverable. `render_plan_preview` two functions above always
    used `NULLS LAST, node_key`."""
    from app.modules import execution_compile as ec
    src = code(inspect.getsource(ec))
    bare = src.count("ORDER BY execution_order\"")
    assert bare == 0, "a compile read still orders by execution_order with no NULLS/tiebreaker"
    assert src.count("ORDER BY execution_order NULLS LAST, node_key") >= 2


def test_the_plan_only_banner_names_an_address_every_surface_has():
    """The banner is prepended to the DELIVERABLE, which is served to the SPA,
    the CLI and the MCP surface as well as OWUI. It hardcoded `/assist <id>`,
    which only OWUI chat accepts."""
    from app.modules import execution_compile as ec
    src = inspect.getsource(ec._prepend_plan_only_banner)
    assert "/job/{job_id}/assist" in src, "the banner must name a route, not only a chat command"
    assert "in OWUI chat" in src, "the chat command should be labelled as the OWUI-only form"


# ── a prerequisite that could never be satisfied ─────────────────────────────

@pytest.mark.asyncio
async def test_a_manual_prerequisite_does_not_block_a_recipe():
    """`manual` means "the engine cannot observe this", not "it is off".
    `_detect_runner_sudo` returns `manual` BY DESIGN (the sudoers rule lives on
    the runner's machine), and `start_recipe` refused anything not `on` — so
    any future recipe requiring `runner_sudo` was impossible to start."""
    from app.modules import engine_setup as es
    src = code(inspect.getsource(es.start_recipe))
    assert 'if st == "manual"' in src, "a manual detector still blocks its dependents"
    assert 'elif st != "on"' in src
    # Assert on the detector's contract, not on a mocked runner state: with
    # the local runner not "on" it returns "blocked" first, so calling it with
    # a stub db would test the wrong branch.
    assert 'return "manual"' in code(inspect.getsource(es._detect_runner_sudo)), (
        "the detector this guard protects no longer reports manual")
    assert "manual" in es.STATUSES


def test_the_job_status_tuple_is_named_for_what_it_holds():
    """It held TERMINAL_JOB_STATUSES under the name `_OPEN_JOB_STATUSES`, and
    its only reader spelled "still open" as `not in _OPEN_JOB_STATUSES` — a
    double negative on an inverted name, in the one place the console's recipe
    status is decided."""
    from app.modules import engine_setup as es
    from app.modules.job_state import TERMINAL_JOB_STATUSES
    assert not hasattr(es, "_OPEN_JOB_STATUSES")
    assert set(es._CLOSED_JOB_STATUSES) == set(TERMINAL_JOB_STATUSES)


def test_a_failed_version_read_is_retried_not_memoized():
    """`expected_helper_version` cached "" on ANY exception, and "" reads the
    same as "no script shipped" — so one transient read error permanently
    disabled the `stale_helper` diagnosis for the life of the process."""
    from app.modules import engine_setup as es
    src = code(inspect.getsource(es.expected_helper_version))
    assert "return None" in src, "a read failure must leave the cache unset"
    assert '_EXPECTED_HELPER_VERSION = ""\n        except' not in src


# ── the prune that did nothing when it mattered ──────────────────────────────

def test_the_derive_map_is_bounded_even_when_nothing_has_expired():
    """The prune removed only EXPIRED entries and only once over the cap. With
    more than 256 distinct keys live inside the 300 s TTL — the load the cap
    exists for — nothing was expired, so it freed nothing and ran a full scan
    on every call while the map grew."""
    import time

    from app.modules import assist_memory as am
    am._RECENT_DERIVES.clear()
    now = time.monotonic()
    for i in range(am._RECENT_DERIVES_MAX + 50):
        am._RECENT_DERIVES[("s", i)] = now          # all LIVE, none expired
    am._derived_recently("s", "trigger the prune")
    assert len(am._RECENT_DERIVES) <= am._RECENT_DERIVES_MAX + 1, (
        f"map grew to {len(am._RECENT_DERIVES)} with nothing expired"
    )
    am._RECENT_DERIVES.clear()


def test_the_system_state_log_is_not_nested_in_the_file_writes_branch():
    """§17.914's capture logged nothing for a session that had never recorded a
    file write, and `resources=[]` for one that had while observing no state."""
    from app.modules import assist_memory as am
    src = code(inspect.getsource(am.derive_turn_memory))
    state_at = src.index("assist_system_state_observed")
    known_at = src.index("if _known:")
    assert state_at < known_at, "the log still sits inside the file-writes branch"


def test_no_query_filters_on_a_node_status_that_cannot_exist():
    """`status IN ('pending','blocked')` — there is no `blocked` NODE status;
    the CHECK constraint is ('pending','running','done','failed','skipped').
    `blocked` is a JOB status and the vocabularies were crossed."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "modules"
    bad = [p.name for p in root.glob("*.py")
           if "'pending', 'blocked'" in code(p.read_text())
           or "'pending','blocked'" in code(p.read_text())]
    assert not bad, f"a dead 'blocked' node-status arm survives in {bad}"


# ── the model call paid for before validating its own argument ───────────────

@pytest.mark.asyncio
async def test_an_unknown_replan_policy_costs_no_inference():
    """The ValueError lived at the BOTTOM of `maybe_replan`, after
    `detect_divergence` had already made its model call."""
    from unittest.mock import AsyncMock, patch

    from app.modules import assist_replan
    with patch.object(assist_replan, "detect_divergence", new=AsyncMock()) as det:
        with pytest.raises(ValueError, match="unknown replan_policy"):
            await assist_replan.maybe_replan(
                db=AsyncMock(), session_id="s", job_id="j", node_key="T1",
                title="t", prompt="p", evidence="e", policy="nonsense")
    det.assert_not_awaited(), "a bad policy still paid for a model call"


def test_the_cross_module_prompt_strip_fails_loud():
    """`_decide_system` strips a sentence out of `assist_guide`'s prompt by
    string replace, and the guarding test only asserted the OLD wording was
    absent from the result — which is also true when the replace matches
    nothing. A reword there made this a silent no-op, leaving the decide prompt
    telling the model to call the CLASSIFIER's tool."""
    from app.modules import assist_decide, assist_guide
    assert assist_decide._CLASSIFY_TOOL_SENTENCE in assist_guide._CLASSIFY_SYSTEM
    src = code(inspect.getsource(assist_decide._decide_system))
    assert "raise RuntimeError" in src


# ── unbounded jsonb arrays (M11), and the one that must NOT be capped ────────

def test_the_two_cappable_arrays_are_capped():
    from app.modules import assist_replan, assist_state_check
    assert "LIMIT 30" in inspect.getsource(assist_replan)
    assert "LIMIT 30" in inspect.getsource(assist_state_check.resolve_state_check)


def test_the_reconciliation_ledger_documents_why_it_is_not_capped():
    """Entries are addressed BY POSITION: `revert_reconciliation` writes
    `jsonb_set(metadata, ['reconciliation', str(index)], …)` using an index the
    operator chose from a listing. Trimming from the front renumbers every
    survivor, so a revert would put back a DIFFERENT change — the cheap fix is
    the dangerous one here."""
    from app.modules import plan_reconcile
    doc = inspect.getdoc(plan_reconcile.revert_reconciliation) or ""
    assert "BY POSITION" in doc and "renumbers" in doc


# ── findings that did NOT survive verification ───────────────────────────────

@pytest.mark.asyncio
async def test_the_evidence_footer_is_not_a_dead_field():
    """Listed as "empty by construction on that path". It is not:
    `annotate=False` stops the footer being APPLIED to `output_text`
    (§17.1037d), but the report still carries the rendered text, and the
    executor persists it so the operator-facing reason survives without
    contaminating the node output. Finding withdrawn."""
    from app.modules import execution_evidence
    src = inspect.getsource(execution_evidence.evidence_summary)
    assert '"footer": report.get("footer")' in src, "the footer carries real content"


def test_clear_pending_state_check_is_used():
    """Listed as "exists and is never used" — it is called by
    `assist_turn`. Its other half was right in shape but wrong in remedy: the
    one inlined removal is part of a single atomic UPDATE that also appends the
    state-check record, so replacing it with the helper would make two writes
    out of one. Finding withdrawn."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "modules" / "assist_turn.py").read_text()
    assert "clear_pending_state_check(" in src
