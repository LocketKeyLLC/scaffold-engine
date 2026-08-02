"""§17.286 — assist submit_step surfaces mirror divergence in response dict.

§17.280-🟡-5 audit-tail concern: ``submit_step`` checks the mirror
invariant after committing (assist_steps row updated → dag_nodes row
also updated unless already terminal) and logs a WARNING when the
divergence is detected (``step_res.rowcount==1 and node_res.rowcount==0``).
But the response dict carried no signal — the operator saw a success
reply while the divergence quietly sat in the orchestrator log.

§17.286 adds ``mirror_divergence: bool`` to every ``submit_step`` return
shape:

  - True  → assist_steps was committed/skipped, but the matching
            dag_nodes row was already 'done'/'skipped' from a
            concurrent path. Step evidence is recorded; the DAG node
            was NOT overwritten by this call.
  - False → either no divergence (happy path), or the request was an
            idempotent no-op (no UPDATE to compare).

These tests pin the response-shape contract — both happy and divergent
branches — plus an OWUI render guard that the pipeline appends a
⚠️ block when the field is True.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent


def _result(rowcount: int = 0, mappings_first=None, mappings_all=None, scalar=None):
    """Mirror tests/test_assist_agent.py::_result — local copy so this
    file stays independent of that module's helper layout."""
    r = MagicMock()
    r.rowcount = rowcount
    mappings = MagicMock()
    mappings.first.return_value = mappings_first
    mappings.all.return_value = mappings_all or []
    r.mappings.return_value = mappings
    r.scalar.return_value = scalar
    fetched = MagicMock()
    fetched.fetchall.return_value = []
    r.fetchall = fetched.fetchall
    return r


def _claim_row(status: str = "presented") -> dict:
    return {
        "step_id": "step-1", "status": status,
        "session_id": "sess-1", "job_id": "job-1", "node_key": "T1",
    }


def _replan_session_row() -> dict:
    """The submit_step path hits `SELECT replan_policy FROM assist_sessions`
    after the commit; return a row that disables replan so the test
    doesn't need to mock the full replan branch."""
    return {"replan_policy": "disabled"}


@pytest.mark.smoke
class TestMirrorDivergenceResponseShape:
    """§17.286 — the response dict always carries ``mirror_divergence``."""

    async def test_happy_submit_returns_false(self):
        """Both assist_steps and dag_nodes updated (rowcount=1 each) →
        no divergence; ``mirror_divergence`` is False."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),       # SELECT step FOR UPDATE
            _result(rowcount=1),                        # UPDATE assist_steps
            _result(rowcount=1),                        # UPDATE dag_nodes (mirrors)
            _result(),                                   # UPDATE assist_sessions activity
            _result(mappings_first=_replan_session_row()),  # SELECT replan policy
            _result(mappings_first={"node_key": "T2"}),  # next_pending — non-None skips finalize
            _result(),                                   # §17.638 UPDATE current_node_key -> T2
        ]
        out = await assist_agent.submit_step(
            session_id="sess-1", node_key="T1",
            evidence="some-output", action="submit", db=db,
        )
        assert out["mirror_divergence"] is False
        assert out["no_op"] is False
        assert out["status"] == "committed"

    async def test_submit_divergence_surfaced_when_dag_node_already_terminal(self):
        """§17.286 load-bearing case: assist_steps updated (rowcount=1)
        but dag_nodes was already terminal so its UPDATE matched 0 rows.
        ``mirror_divergence`` is True; the response is still success."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),
            _result(rowcount=1),                        # UPDATE assist_steps
            _result(rowcount=0),                        # UPDATE dag_nodes — NO ROW (already terminal)
            _result(),
            _result(mappings_first=_replan_session_row()),
            _result(mappings_first={"node_key": "T2"}),  # next_pending — skip finalize
            _result(),                                   # §17.638 UPDATE current_node_key -> T2
        ]
        out = await assist_agent.submit_step(
            session_id="sess-1", node_key="T1",
            evidence="some-output", action="submit", db=db,
        )
        assert out["mirror_divergence"] is True
        assert out["status"] == "committed"
        # Operator can still see all the standard fields — divergence is
        # a SIGNAL, not a failure.
        assert out["no_op"] is False
        assert out["session_id"] == "sess-1"
        assert out["node_key"] == "T1"

    async def test_skip_divergence_surfaced_same_way(self):
        """Skip path uses the same mirror check; same response shape."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),
            _result(rowcount=1),                        # UPDATE assist_steps to skipped
            _result(rowcount=0),                        # UPDATE dag_nodes — already done
            _result(),                                   # session activity touch
            _result(mappings_first={"node_key": "T2"}),  # next_pending — skip finalize
            _result(),                                   # §17.638 UPDATE current_node_key -> T2
        ]
        out = await assist_agent.submit_step(
            session_id="sess-1", node_key="T1",
            evidence="", action="skip", db=db,
        )
        assert out["mirror_divergence"] is True
        assert out["status"] == "skipped"

    async def test_failed_verdict_skips_divergence_replan(self):
        """§17.708 — a submit whose verify verdict is FAILED (command errored)
        must NOT run divergence detection: it's a recover-and-retry situation,
        not a plan divergence. `_maybe_replan` is never called."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),        # SELECT step FOR UPDATE
            _result(rowcount=1),                         # UPDATE assist_steps
            _result(rowcount=1),                         # UPDATE dag_nodes
            _result(),                                    # session activity touch
            _result(mappings_first={"node_key": "T2"}),  # next_pending (no policy SELECT — replan skipped)
            _result(),                                    # current_node_key -> T2
        ]
        with patch.object(assist_agent, "_maybe_replan",
                          new=AsyncMock(return_value=None)) as mr:
            out = await assist_agent.submit_step(
                session_id="sess-1", node_key="T1",
                evidence="pveum user list\nipcc_send_rec[1] failed: Connection refused",
                action="submit", verdict_failed=True, db=db,
            )
        mr.assert_not_called()
        assert out["status"] == "committed"
        assert out["replan"] is None

    async def test_non_failed_verdict_runs_divergence_replan(self):
        """§17.708 — the default (non-failed) path still runs divergence detection."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),
            _result(rowcount=1),
            _result(rowcount=1),
            _result(),
            _result(mappings_first={"node_key": "T2"}),
            _result(),
        ]
        with patch.object(assist_agent, "_maybe_replan",
                          new=AsyncMock(return_value=None)) as mr:
            await assist_agent.submit_step(
                session_id="sess-1", node_key="T1", evidence="all good, 0 errors",
                action="submit", verdict_failed=False, db=db,
            )
        mr.assert_awaited_once()

    async def test_skip_no_divergence_when_both_rows_updated(self):
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row()),
            _result(rowcount=1),                        # UPDATE assist_steps
            _result(rowcount=1),                        # UPDATE dag_nodes
            _result(),
            _result(mappings_first={"node_key": "T2"}),  # next_pending
            _result(),                                   # §17.638 UPDATE current_node_key -> T2
        ]
        out = await assist_agent.submit_step(
            session_id="sess-1", node_key="T1",
            evidence="", action="skip", db=db,
        )
        assert out["mirror_divergence"] is False
        assert out["status"] == "skipped"

    async def test_idempotent_noop_returns_false_divergence(self):
        """No UPDATE happens on the already-committed idempotent path →
        ``mirror_divergence`` is False (no race detected because no
        race could happen). Keeps the response key always-present so
        callers can rely on it without ``.get(..., default)``."""
        db = AsyncMock()
        db.execute.side_effect = [
            _result(mappings_first=_claim_row(status="committed")),
        ]
        out = await assist_agent.submit_step(
            session_id="sess-1", node_key="T1",
            evidence="x", action="submit", db=db,
        )
        assert out["no_op"] is True
        assert out["mirror_divergence"] is False


@pytest.mark.smoke
class TestPipelineRendersDivergence:
    """§17.286 — scaffold_router's `_assist_submit` / `_assist_skip` append
    a ⚠️ block when the orchestrator reply carries ``mirror_divergence``.

    Pinned via source inspection rather than runtime invocation because
    the pipeline file is loaded by OWUI with module-level side-effects
    that aren't trivial to set up under pytest. The two warning strings
    are unique enough that a drive-by removal would be visible.
    """

    def test_submit_pipeline_renders_warning_when_divergence_flag_set(self):
        """§17.286 chat-render anchor.

        §17.296 lifted the /assist handlers from scaffold_router.py into
        pipelines/_vendor/_assist_handlers.py. The string anchors moved
        with them. Check both files — scaffold_router still has the
        thin-delegate methods but the literal source strings now live
        in the vendor module.
        """
        from pathlib import Path
        from pipelines import scaffold_router

        scaffold_src = Path(scaffold_router.__file__).read_text(encoding="utf-8")
        vendor_path = Path(scaffold_router.__file__).parent / "_vendor" / "_assist_handlers.py"
        vendor_src = vendor_path.read_text(encoding="utf-8")
        combined = scaffold_src + vendor_src

        assert 'd.get("mirror_divergence")' in combined, (
            "§17.286: the /assist submit + skip handlers must read the "
            "``mirror_divergence`` field from the orchestrator's response "
            "and surface a warning to the chat. Otherwise the field is "
            "set on the server side but invisible to the user. (Post-"
            "§17.296 the handlers live in pipelines/_vendor/_assist_"
            "handlers.py; check both files.)"
        )
        assert "Mirror divergence" in combined, (
            "§17.286: the operator-facing warning string for mirror "
            "divergence must remain in the assist handlers — it's the "
            "visible surface of the audit fix."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.286 — anchor that ``submit_step`` keeps the response field."""

    def test_response_dict_contains_mirror_divergence_key(self):
        from app.modules import assist_agent as agent

        with open(agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert '"mirror_divergence"' in src, (
            "§17.286 regression: the ``mirror_divergence`` key was removed "
            "from submit_step's return dict. The audit fix REQUIRES this "
            "field be set on every return path so callers can rely on it "
            "(see §17.280-🟡-5 — pre-§17.286 the divergence was only "
            "logged, never surfaced to the operator)."
        )
        assert "mirror_divergence = (step_res.rowcount" in src, (
            "§17.286 regression: the mirror_divergence local-variable "
            "assignment (the load-bearing rowcount comparison that drives "
            "the flag) was removed. The divergence check must compute the "
            "boolean BEFORE the log + response so the same value reaches "
            "both."
        )
