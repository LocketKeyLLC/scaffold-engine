"""§17.820 (plan 5.9) — phase_label_for unit coverage.

The mapping moved from app/web/routes.py to app/modules/job_phase.py during
the /web retirement because GET /work (app/routers/status.py) depends on it.
It had ZERO direct coverage while it lived in the web module — these pin the
label groupings the retired stepper tests exercised indirectly.
"""
from __future__ import annotations

import pytest

from app.modules.job_phase import PIPELINE_STEPS, phase_label_for


class TestPhaseLabelFor:
    @pytest.mark.parametrize("status,label", [
        ("pending", "Refine"),
        ("refining", "Refine"),
        ("awaiting_confirmation", "Review"),
        ("researching", "Research"),
        ("planning", "Plan"),
        ("executing", "Execute"),
        ("running", "Execute"),
        ("completed", "Done"),
    ])
    def test_stepper_statuses(self, status, label):
        assert phase_label_for(status) == label

    @pytest.mark.parametrize("status,label", [
        ("aggregating", "Assemble"),
        ("assisted_executing", "Execute"),
        ("assisted_running", "Execute"),
        ("assisted_paused", "Paused"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
        ("blocked", "Blocked"),
    ])
    def test_extra_statuses(self, status, label):
        assert phase_label_for(status) == label

    def test_unknown_status_degrades_to_title_case(self):
        assert phase_label_for("weird_new_state") == "Weird New State"

    def test_pipeline_steps_shape_is_stable(self):
        """The 6-phase grouping order is a UI contract (SPA phase chips +
        /work labels)."""
        assert [label for label, _ in PIPELINE_STEPS] == [
            "Refine", "Review", "Research", "Plan", "Execute", "Done",
        ]
