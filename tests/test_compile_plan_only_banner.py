"""§17.506 — plan-only banner for unexecuted Shell/runbook nodes.

Autonomous execution of a hands-on-hardware job (install Proxmox, configure a
firewall) only generates runbooks via the Shell tool (shell_tool_enabled=False
default) and marks the nodes ``done`` — so the job rolls up to ``completed``
and the compiled output reads like a finished build when nothing was executed.
These tests pin the banner that closes that "hallucinated completion" gap.
"""
from tests._execution_agent_shared import *  # noqa: F401,F403

import pytest

from app.modules.execution_compile import _prepend_plan_only_banner


class TestPlanOnlyBannerHelper:
    def test_zero_runbook_unchanged(self):
        assert _prepend_plan_only_banner("body", 0, 5, "job-1") == "body"

    def test_none_text_unchanged(self):
        assert _prepend_plan_only_banner(None, 3, 5, "job-1") is None

    def test_banner_present_and_actionable(self):
        out = _prepend_plan_only_banner("BODY", 3, 5, "job-xyz")
        assert "PLAN — NOT EXECUTED" in out
        assert "/assist job-xyz" in out      # actionable: names the command + job
        assert "3 of 5" in out
        assert out.endswith("BODY")          # original deliverable preserved below

    def test_singular_grammar(self):
        out = _prepend_plan_only_banner("BODY", 1, 4, "j")
        assert "1 of 4 step" in out
        assert "is a runbook" in out


@pytest.mark.smoke
class TestCompileOutputPlanOnly:
    """End-to-end through _compile_output with mocked node rows."""

    @staticmethod
    def _prep(monkeypatch, **settings_kw):
        """§17.513 — patch settings on the EXACT object _compile_output reads:
        its own ``__globals__["settings"]``. This can't miss the object even
        when another test reloads app.config / execution_compile (which
        decouples module-level `settings` references). Synthesis is disabled so
        the compile path is deterministic and never makes a live LLM call.
        Returns the same `_compile_output` whose globals were patched."""
        from app.modules.execution_agent import _compile_output
        sett = _compile_output.__globals__["settings"]
        settings_kw.setdefault("compile_synthesis_enabled", False)
        for k, v in settings_kw.items():
            monkeypatch.setattr(sett, k, v)
        return _compile_output

    async def test_shell_runbook_job_gets_banner(self, monkeypatch):
        _compile_output = self._prep(monkeypatch, shell_tool_enabled=False)
        db = make_mock_db([  # noqa: F405
            {"node_key": "T1", "title": "Install Proxmox", "tool": "Shell",
             "status": "done", "output_text": "## Run this\n```bash\n...\n```"},
            {"node_key": "T2", "title": "Document topology", "tool": "LLM",
             "status": "done", "output_text": "topology overview"},
        ])
        result, _ = await _compile_output("job-homelab", db)
        assert "PLAN — NOT EXECUTED" in result
        assert "/assist job-homelab" in result
        assert "1 of 2" in result  # only T1 is a Shell/runbook node

    async def test_pure_text_job_no_banner(self, monkeypatch):
        _compile_output = self._prep(monkeypatch, shell_tool_enabled=False)
        db = make_mock_db([  # noqa: F405
            {"node_key": "T1", "title": "Research", "tool": "SearXNG",
             "status": "done", "output_text": "data"},
            {"node_key": "T2", "title": "Write summary", "tool": "LLM",
             "status": "done", "output_text": "the summary"},
        ])
        result, _ = await _compile_output("job-research", db)
        assert "PLAN — NOT EXECUTED" not in result

    async def test_shell_enabled_suppresses_banner(self, monkeypatch):
        # A real shell backend (shell_tool_enabled=True) DID execute — no banner.
        _compile_output = self._prep(monkeypatch, shell_tool_enabled=True)
        db = make_mock_db([  # noqa: F405
            {"node_key": "T1", "title": "Install", "tool": "Shell",
             "status": "done", "output_text": "executed output"},
        ])
        result, _ = await _compile_output("job-real", db)
        assert "PLAN — NOT EXECUTED" not in result
