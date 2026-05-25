"""§17.305 — /go's awaiting_confirmation pause harmonized with §17.303.

Pre-§17.305 the /go auto-chain showed 3 options at the awaiting_-
confirmation gate: proceed, adjust, restart. §17.303's /idea Next-
block surfaces 4 commands: proceed, adjust, /results, /cost.

The two entry points lead to the SAME orchestrator state (Phase 1
job awaiting confirmation) but offered DIFFERENT discovery surfaces.
§17.305 harmonizes /go to the same 4-command set + keeps the
"start over" line (unique to /go's chat-history-driven entry path).

These tests pin that:
  - Both entry points surface the canonical 4 commands
  - /go preserves the "start over" affordance
  - Each command has the real job_id pre-filled (no `<job_id>` leak)
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"


def _ideate_resp(**overrides) -> MagicMock:
    """Default /ideate awaiting_confirmation response — mirror
    analyze_and_confirm's success body."""
    body = {
        "job_id": _SAMPLE_JOB_ID,
        "status": "awaiting_confirmation",
        "refined_brief": {
            "title": "Build a CLI that converts screenshots to PDF",
            "description": "A tool that ingests PNG screenshots and emits a searchable PDF.",
        },
        "feasibility": {
            "feasible": True,
            "confidence": 0.85,
            "risks": ["OCR accuracy on screenshots varies"],
            "clarifications_needed": [],
        },
    }
    body.update(overrides)
    r = MagicMock(status_code=200)
    r.json.return_value = body
    return r


def _drive_auto_chain(pipe, ideate_response) -> str:
    """Run _auto_chain with a stubbed /ideate POST; collect output."""
    # _post_with_keepalive returns (ok, response). Stub it directly so
    # we don't go through the keepalive threading.
    def _stub_post(url, payload, timeout, *, progress_label=None):
        return (True, ideate_response)
        yield  # make it a generator (matches _post_with_keepalive's shape)

    with patch.object(pipe, "_post_with_keepalive", _stub_post):
        chunks = list(pipe._auto_chain("Build a CLI that converts screenshots to PDF"))
    return "".join(chunks)


@pytest.mark.smoke
class TestGoAwaitingConfirmationNextBlock:
    """§17.305 — /go's pause-block surfaces the same 4 commands as §17.303."""

    def test_all_four_canonical_commands_present(self, pipe):
        out = _drive_auto_chain(pipe, _ideate_resp())
        # Same 4 commands as §17.303's /idea Next-block.
        for cmd_prefix in ("/confirm", "/results", "/cost"):
            assert f"{cmd_prefix} {_SAMPLE_JOB_ID}" in out, (
                f"§17.305: /go awaiting_confirmation block must include "
                f"`{cmd_prefix} <real-id>` to match §17.303's /idea "
                f"Next-block. Operators landing here from chat → /go vs "
                f"`/idea <text>` should see the same discovery surface."
            )

    def test_confirm_with_feedback_distinguished(self, pipe):
        """Both `/confirm <id>` (proceed as-is) and `/confirm <id>
        <feedback>` (adjust first) are surfaced — mirror of §17.303."""
        out = _drive_auto_chain(pipe, _ideate_resp())
        assert f"/confirm {_SAMPLE_JOB_ID} <your adjustments>" in out
        # The plain variant — anchored to the code-formatted backtick.
        assert f"`/confirm {_SAMPLE_JOB_ID}`" in out

    def test_pre_fills_real_id_not_placeholder(self, pipe):
        """Mirror of §17.303's placeholder-leak guard: no `<job_id>`
        literal above the start-over line (which doesn't carry an id
        because it restarts from scratch)."""
        out = _drive_auto_chain(pipe, _ideate_resp())
        # Split at the start-over hint and check the upper block.
        upper = out.split("start over", 1)[0]
        assert "<job_id>" not in upper, (
            "§17.305: /go awaiting_confirmation block leaked the literal "
            "`<job_id>` placeholder above the start-over hint. The 4-command "
            "block must always substitute the real id."
        )

    def test_start_over_affordance_preserved(self, pipe):
        """/go is the chat-history-driven path. "Start over" is unique to
        this entry — restarting means describing a NEW idea and typing
        /go again (not /idea). Keep it as the 5th line."""
        out = _drive_auto_chain(pipe, _ideate_resp())
        assert "start over" in out.lower()
        assert "/go" in out

    def test_feasibility_summary_preserved(self, pipe):
        """The pre-§17.305 feasibility + risks + clarifications block
        is operator context for the decision — must not be regressed
        by the Next-block harmonization."""
        out = _drive_auto_chain(pipe, _ideate_resp())
        assert "Feasibility" in out
        # 85% confidence rendered as `85%` (.0% format).
        assert "85%" in out
        assert "OCR accuracy" in out  # the sample risk


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_four_canonical_commands_anchored_in_auto_chain(self):
        """The 4-command Next-block template lines must remain in
        _auto_chain's awaiting_confirmation branch."""
        src = self._src()
        # Pin the three NEW lines added by §17.305 (the two existing
        # /confirm lines were already there; /results and /cost are
        # the additions).
        assert "/results {job_id}` — peek at current state" in src, (
            "§17.305 regression: the /results suggestion line is gone "
            "from /go's awaiting_confirmation pause. Operators lose "
            "the inspection affordance that §17.303 introduced for /idea."
        )
        assert "/cost {job_id}` — see refinement costs so far" in src, (
            "§17.305 regression: the /cost suggestion line is gone "
            "from /go's awaiting_confirmation pause."
        )

    def test_start_over_line_anchored(self):
        """The /go-unique restart affordance — anchor so a future
        'consolidate with /idea' refactor doesn't drop it."""
        src = self._src()
        assert "start over: describe a new idea and type `/go` again" in src, (
            "§17.305 regression: the /go-specific 'start over' "
            "affordance is gone from _auto_chain. Operators landing "
            "from chat → /go lose the restart path."
        )
