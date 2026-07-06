"""§17.303 — `/idea` success surfaces a pre-filled Next-block.

Pre-§17.303 `/idea` returned a raw JSON dump via `_fmt(r)`. Operators
saw the job_id buried in the JSON and had to scan to find it, then
type `/confirm <job_id>` from memory (or copy-paste a long UUID).

§17.303 introduces `_render_ideate_response(r)` which extracts the
job_id + brief summary + feasibility verdict and renders a focused
markdown response with a pre-filled Next-block:

  - /confirm <job_id> — auto-chain Phase 2
  - /confirm <job_id> <feedback> — adjust before proceeding
  - /results <job_id> — peek at state
  - /cost <job_id> — see costs so far

Each command has the actual job_id filled in (NOT the literal
`<job_id>` placeholder the orchestrator's `message` field uses).

These tests pin: success-path rendering, error fallback to `_fmt`,
no-job-id fallback, feasibility verdict surfacing, full-JSON footer
still present.
"""
from unittest.mock import MagicMock

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _resp(status: int, body) -> MagicMock:
    r = MagicMock(status_code=status, text="")
    r.json.return_value = body
    return r


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"


def _success_payload(**overrides) -> dict:
    """Default /ideate success body — mirror what analyze_and_confirm
    returns (see app/modules/ideation_workflow.py:170)."""
    payload = {
        "job_id": _SAMPLE_JOB_ID,
        "status": "awaiting_confirmation",
        "refined_brief": {
            "title": "Build a CLI that converts screenshots to PDF",
            "domain": "eng",
        },
        "feasibility": {"feasible": True, "confidence": 0.85},
        "message": (
            "Review the analysis. Reply /confirm <job_id> to proceed, "
            "or /confirm <job_id> <feedback> to adjust."
        ),
    }
    payload.update(overrides)
    return payload


@pytest.mark.smoke
class TestIdeateSuccessRendering:
    """§17.303 — the load-bearing case: 200 with job_id."""

    def test_actual_job_id_surfaced_at_top(self, pipe):
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        assert "Job created" in out
        # The actual id, NOT the literal `<job_id>` placeholder.
        assert _SAMPLE_JOB_ID in out
        assert "<job_id>" not in out.split("Next steps:", 1)[0], (
            "§17.303: the orchestrator's `message` field uses the literal "
            "`<job_id>` placeholder. The renderer must extract the real id "
            "and not surface that placeholder above the Next-block."
        )

    def test_refined_brief_title_surfaced(self, pipe):
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        assert "Refined brief" in out
        assert "Build a CLI that converts screenshots to PDF" in out

    def test_feasibility_verdict_with_confidence(self, pipe):
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        assert "feasible" in out
        # Confidence formatted to 2 decimal places.
        assert "0.85" in out

    def test_infeasible_verdict_surfaced(self, pipe):
        payload = _success_payload(
            feasibility={"feasible": False, "confidence": 0.3},
        )
        out = pipe._render_ideate_response(_resp(200, payload))
        assert "infeasible" in out
        assert "0.30" in out

    def test_feasibility_fallback_warning_surfaced(self, pipe):
        """When the orchestrator's `message` carries the
        "Feasibility check failed" prefix, the renderer surfaces it
        as an explicit warning line (above the Next-block)."""
        payload = _success_payload(
            message=(
                "⚠️ Feasibility check failed; using best-effort defaults. "
                "Review the analysis. Reply /confirm <job_id> to proceed."
            ),
        )
        out = pipe._render_ideate_response(_resp(200, payload))
        assert "Feasibility check failed" in out

    def test_next_block_has_all_four_pre_filled_commands(self, pipe):
        """The 4 canonical next commands — each with the REAL id.
        §17.562 — /cost is an advanced command; enable advanced so the full
        next-block renders (guided mode hides /cost — see guided test below)."""
        pipe.valves.advanced_commands_enabled = True
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        for cmd_prefix in ("/confirm", "/results", "/cost"):
            # Each prefix should appear with the real job_id immediately
            # after, never with the literal `<job_id>` placeholder.
            assert f"{cmd_prefix} {_SAMPLE_JOB_ID}" in out, (
                f"§17.303: Next-block must include `{cmd_prefix} <real-id>`. "
                f"Missing or using a placeholder."
            )

    def test_guided_next_block_hides_cost(self, pipe):
        """§17.562 — guided mode (default) keeps the core /confirm + /results
        next steps but hides the gated /cost."""
        pipe.valves.advanced_commands_enabled = False
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        assert f"/confirm {_SAMPLE_JOB_ID}" in out
        assert f"/results {_SAMPLE_JOB_ID}" in out
        assert "/cost" not in out

    def test_next_block_distinguishes_confirm_vs_confirm_feedback(self, pipe):
        """Both `/confirm <id>` (proceed as-is) and `/confirm <id>
        <feedback>` (adjust first) are surfaced — they're distinct
        operator intents."""
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        # The feedback variant has the angle-bracketed `<your feedback>`
        # placeholder (that one IS a placeholder — operator fills it in).
        assert f"/confirm {_SAMPLE_JOB_ID} <your feedback>" in out
        # The plain variant doesn't have it.
        assert f"`/confirm {_SAMPLE_JOB_ID}`" in out

    def test_full_json_footer_still_present(self, pipe):
        """Operators who want the full payload (debugging) still get
        it — collapsed in a <details> block so it doesn't dominate the
        chat real estate."""
        out = pipe._render_ideate_response(_resp(200, _success_payload()))
        assert "Full Phase 1 response" in out
        assert "```json" in out
        # The full payload's signature field is in the JSON dump.
        assert "refined_brief" in out


@pytest.mark.smoke
class TestIdeateErrorFallback:
    """§17.303 — non-success paths fall through to _fmt unchanged."""

    def test_4xx_falls_back_to_fmt(self, pipe):
        out = pipe._render_ideate_response(_resp(
            422, {"detail": "validation failed"},
        ))
        # _fmt's signature shape for 4xx.
        assert "Error 422" in out
        assert "validation failed" in out
        # No Next-block (this was a failure).
        assert "Next steps" not in out

    def test_5xx_falls_back_to_fmt(self, pipe):
        out = pipe._render_ideate_response(_resp(
            500, {"detail": "phase1 exception: ..."},
        ))
        assert "Error 500" in out

    def test_non_json_falls_back_to_fmt(self, pipe):
        r = MagicMock(status_code=200, text="garbage body")
        r.json.side_effect = ValueError("not json")
        out = pipe._render_ideate_response(r)
        assert "HTTP 200" in out  # _fmt's non-JSON shape

    def test_200_without_job_id_falls_back_to_fmt(self, pipe):
        """Defensive: a 200 with an unexpected shape (no job_id) means
        the orchestrator contract drifted. Fall back to the raw JSON
        dump so the operator can see what came back."""
        payload = {"status": "awaiting_confirmation"}  # job_id missing
        out = pipe._render_ideate_response(_resp(200, payload))
        # _fmt's success shape is the bare JSON block.
        assert "```json" in out
        # No Next-block (we don't know which id to fill in).
        assert "Next steps:" not in out


@pytest.mark.smoke
class TestIdeateRoutedThroughRenderer:
    """§17.303 — pin that the `/idea` command path goes through the
    new renderer, not the generic `_fmt`."""

    def test_handle_command_idea_uses_render_ideate(self, pipe):
        """The success render shape (`Job created` line) only appears
        if `/idea` actually routed through `_render_ideate_response`."""
        with __import__("unittest.mock").mock.patch(
            "scaffold_router._HTTP_SESSION.post"
        ) as mp:
            mp.return_value = _resp(200, _success_payload())
            out = pipe._handle_command("/idea Build a thing")
        assert "Job created" in out, (
            "§17.303 regression: `/idea` no longer routes through "
            "`_render_ideate_response`. It's falling back to the "
            "generic JSON-dump formatter."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_render_ideate_response_anchored(self):
        """The renderer must remain a named method so future audit
        passes can find it by grep."""
        src = self._src()
        assert "def _render_ideate_response" in src, (
            "§17.303 regression: `_render_ideate_response` method is "
            "gone from scaffold_router.py. The Next-block on /idea "
            "success has been collapsed back into the JSON-dump shape."
        )

    def test_idea_command_calls_render_ideate(self):
        """The dispatch site for `/idea` must call the renderer."""
        src = self._src()
        # §17.307 — the call now passes chat_id for active-job memory.
        # Loosen to anchor `_render_ideate_response(r` with optional
        # kwargs trailing.
        assert "self._render_ideate_response(r, chat_id=" in src or (
            "self._render_ideate_response(r)" in src
        ), (
            "§17.303 regression: the `/idea` dispatch no longer calls "
            "`_render_ideate_response`. Pre-§17.303 it called `_fmt`; "
            "reverting to that loses the Next-block UX."
        )

    def test_four_canonical_next_commands_in_renderer(self):
        """Pin all 4 next commands' template lines in the renderer
        source so a drive-by 'simplify' that drops one shows up here.
        """
        src = self._src()
        for expected in (
            "/confirm {job_id}` — auto-chain",
            "/confirm {job_id} <your feedback>",
            "/results {job_id}` — peek at current state",
            "/cost {job_id}` — see costs so far",
        ):
            assert expected in src, (
                f"§17.303 regression: the Next-block template line "
                f"containing {expected!r} is missing from the renderer. "
                f"The 4-command discovery set on /idea success is the "
                f"audit invariant — dropping one regresses first-result UX."
            )
