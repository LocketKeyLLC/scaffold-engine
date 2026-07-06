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


def _force_no_synthesis(monkeypatch):
    """§17.518 — synthesis is gated per-job via `_resolve_synthesis_enabled`
    (it reads `jobs.compile_synthesis_override` from the DB), NOT just the global
    `compile_synthesis_enabled` setting. With the mock DB that read returns junk,
    so synthesis can fire a live LLM call → a 30s pytest-timeout flake under
    load. §17.513 disabled the wrong (global) gate; patch the real resolver off
    so the compile path is deterministic and never hits the network."""
    import app.modules.execution_compile as _ec
    monkeypatch.setattr(_ec, "_resolve_synthesis_enabled",
                        AsyncMock(return_value=False))


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
        _force_no_synthesis(monkeypatch)
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


class TestAssistCompletedBanner:
    """§17.516 — assist-completed compile suppresses the plan-only banner and
    adds a positive 'Completed via Assist Mode' header (the operator executed
    the steps), so /results shows a real summary instead of NULL."""

    def test_helper_none_unchanged(self):
        from app.modules.execution_compile import _prepend_assist_completed_banner
        assert _prepend_assist_completed_banner(None, 3) is None

    def test_helper_banner_present(self):
        from app.modules.execution_compile import _prepend_assist_completed_banner
        out = _prepend_assist_completed_banner("BODY", 3)
        assert "Completed via Assist Mode" in out
        assert "3 steps" in out
        assert out.endswith("BODY")

    def test_helper_singular(self):
        from app.modules.execution_compile import _prepend_assist_completed_banner
        assert "1 step on" in _prepend_assist_completed_banner("B", 1)


class TestCompileOutputAssistCompleted:
    """End-to-end: assist_completed=True path through _compile_output."""

    @staticmethod
    def _prep(monkeypatch, **kw):
        from app.modules.execution_agent import _compile_output
        sett = _compile_output.__globals__["settings"]
        kw.setdefault("compile_synthesis_enabled", False)
        kw.setdefault("shell_tool_enabled", False)
        for k, v in kw.items():
            monkeypatch.setattr(sett, k, v)
        _force_no_synthesis(monkeypatch)
        return _compile_output

    async def test_assist_completed_suppresses_plan_banner_adds_header(self, monkeypatch):
        _compile_output = self._prep(monkeypatch)
        db = make_mock_db([  # noqa: F405
            {"node_key": "T1", "title": "Install", "tool": "Shell",
             "status": "done", "output_text": "ran it; verified active"},
            {"node_key": "T2", "title": "Document", "tool": "LLM",
             "status": "done", "output_text": "wrote SETUP.md"},
        ])
        result, _ = await _compile_output("job-assist", db, assist_completed=True)
        assert "PLAN — NOT EXECUTED" not in result      # suppressed for assist
        assert "Completed via Assist Mode" in result     # positive header
        assert "2 steps" in result                       # done_count

    async def test_default_path_still_gets_plan_banner(self, monkeypatch):
        # Same shell job WITHOUT assist_completed → the §17.506 banner still fires.
        _compile_output = self._prep(monkeypatch)
        db = make_mock_db([  # noqa: F405
            {"node_key": "T1", "title": "Install", "tool": "Shell",
             "status": "done", "output_text": "## Run this\n..."},
        ])
        result, _ = await _compile_output("job-auto", db)
        assert "PLAN — NOT EXECUTED" in result
        assert "Completed via Assist Mode" not in result


class TestComputeDeliverableKind:
    """§17.519 — machine-readable deliverable kind set on jobs.deliverable_kind."""

    async def test_assist_completed(self):
        from app.modules.execution_compile import compute_deliverable_kind
        db = AsyncMock()  # noqa: F405 — assist path returns before any query
        assert await compute_deliverable_kind("j", db, assist_completed=True) \
            == "assist_completed"

    async def test_plan_only_when_shell_done_and_no_backend(self, monkeypatch):
        from app.modules import execution_compile as ec
        monkeypatch.setattr(ec.settings, "shell_tool_enabled", False)
        db = AsyncMock(); row = MagicMock(); row.scalar = MagicMock(return_value=3)  # noqa: F405
        db.execute = AsyncMock(return_value=row)  # noqa: F405
        assert await ec.compute_deliverable_kind("j", db) == "plan_only"

    async def test_executed_when_no_shell_nodes(self, monkeypatch):
        from app.modules import execution_compile as ec
        monkeypatch.setattr(ec.settings, "shell_tool_enabled", False)
        db = AsyncMock(); row = MagicMock(); row.scalar = MagicMock(return_value=0)  # noqa: F405
        db.execute = AsyncMock(return_value=row)  # noqa: F405
        assert await ec.compute_deliverable_kind("j", db) == "executed"

    async def test_shell_backend_enabled_is_executed(self, monkeypatch):
        from app.modules import execution_compile as ec
        monkeypatch.setattr(ec.settings, "shell_tool_enabled", True)
        db = AsyncMock(); row = MagicMock(); row.scalar = MagicMock(return_value=5)  # noqa: F405
        db.execute = AsyncMock(return_value=row)  # noqa: F405
        assert await ec.compute_deliverable_kind("j", db) == "executed"
