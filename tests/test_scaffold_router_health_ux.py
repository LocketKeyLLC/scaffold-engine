"""§17.317 — `/health` discoverability polish.

Operators land on /health from §17.302's recovery hints
("orchestrator unreachable → try /health"). Pre-§17.317 they
saw a bare subsystem table with no overall verdict, no recovery
commands when something was down, and a JSON dump on errors.

§17.317 adds three additive affordances:

  1. Single-line verdict above the table — "✅ All N subsystems up."
     or "⚠️ N of M subsystems down: `name1`, `name2`."
  2. Recovery footer when anything is down — generic docker compose
     ps / restart / logs / retry commands.
  3. Friendly error path when /health itself is unreachable —
     timeout / connection-refused render the recovery footer with
     scaffold-orchestrator-specific commands instead of a JSON
     dump.

These tests pin each surface + existing render preserved +
source-shape guards.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _health_response(checks: dict) -> MagicMock:
    """Mock the orchestrator's /health response."""
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {"checks": checks}
    return r


def _up(latency: int = 5) -> dict:
    return {"status": "up", "latency_ms": latency}


def _down(latency: int = 5000) -> dict:
    return {"status": "down", "latency_ms": latency}


# ---------------------------------------------------------------------------
# All-up verdict
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestAllUpVerdict:

    def test_verdict_present_when_all_up(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "postgresql": _up(), "ollama": _up(),
                "milvus": _up(), "redis": _up(),
            })
            out = pipe._handle_health()
        # Verdict with count.
        assert "All 4 subsystems up" in out
        assert "✅" in out

    def test_verdict_above_table(self, pipe):
        """Operators scan top-down — verdict must appear BEFORE the
        details table so they see "broken or not" at a glance."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({"postgresql": _up()})
            out = pipe._handle_health()
        verdict_idx = out.index("All 1 subsystems up")
        table_idx = out.index("| Subsystem")
        assert verdict_idx < table_idx

    def test_no_recovery_footer_when_all_up(self, pipe):
        """All-up case stays clean — no recovery footer needed."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "postgresql": _up(), "ollama": _up(),
            })
            out = pipe._handle_health()
        assert "Recovery:" not in out
        assert "docker compose ps" not in out


# ---------------------------------------------------------------------------
# Some-down verdict + recovery footer
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSomeDownVerdict:

    def test_verdict_names_down_subsystems(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "postgresql": _down(), "ollama": _up(),
                "milvus": _up(), "redis": _down(),
            })
            out = pipe._handle_health()
        # Header reflects count + names.
        assert "2 of 4 subsystems down" in out
        assert "`postgresql`" in out
        assert "`redis`" in out
        assert "⚠️" in out

    def test_recovery_footer_present_when_down(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "postgresql": _down(), "ollama": _up(),
            })
            out = pipe._handle_health()
        # Recovery footer surfaces.
        assert "💡 **Recovery:**" in out
        # Generic docker compose commands.
        assert "docker compose ps" in out
        assert "docker compose restart" in out
        assert "docker compose logs" in out
        # Retry hint.
        assert "Retry: `/health`" in out

    def test_milvus_slow_boot_hint_present(self, pipe):
        """Milvus has a known long boot time — the retry hint must
        mention it so operators don't bounce too fast."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({"milvus": _down()})
            out = pipe._handle_health()
        assert "milvus boots slowly" in out.lower()


# ---------------------------------------------------------------------------
# Orchestrator unreachable (/health itself failed)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestOrchestratorUnreachable:

    def test_connection_error_returns_recovery_shape(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.side_effect = requests.exceptions.ConnectionError("refused")
            out = pipe._handle_health()
        # Banner naming the failed reach.
        assert "Cannot reach orchestrator" in out
        assert "refused" in out
        # Scaffold-orchestrator-specific recovery commands.
        assert "scaffold-orchestrator" in out
        assert "docker compose ps" in out
        assert "docker compose restart scaffold-orchestrator" in out
        assert "docker compose logs --tail=50 scaffold-orchestrator" in out

    def test_timeout_returns_recovery_shape(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.side_effect = requests.exceptions.Timeout()
            out = pipe._handle_health()
        assert "Cannot reach orchestrator" in out
        assert "timed out" in out
        assert "docker compose restart scaffold-orchestrator" in out

    def test_4xx_response_includes_recovery_footer(self, pipe):
        """When orchestrator is reachable but /health itself errors
        (e.g., 500 from a broken probe), pair the error with the
        generic recovery footer."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            r = MagicMock(status_code=500, text="")
            r.json.return_value = {"detail": "probe crashed"}
            mg.return_value = r
            out = pipe._handle_health()
        # The error itself surfaces.
        assert "500" in out
        # Plus the recovery footer.
        assert "💡 **Recovery:**" in out


# ---------------------------------------------------------------------------
# Pre-existing render preserved
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExistingRenderPreserved:
    """§17.317 is additive — pin that the pre-§17.317 subsystem table
    still renders with the same shape (header, row format, icons)."""

    def test_header_preserved(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({"postgresql": _up()})
            out = pipe._handle_health()
        assert "## 🩺 Health" in out

    def test_table_columns_preserved(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({"postgresql": _up()})
            out = pipe._handle_health()
        assert "| Subsystem | Status | Latency |" in out

    def test_row_format_preserved(self, pipe):
        """Status icons + latency format unchanged."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "postgresql": _up(latency=12),
                "milvus": _down(latency=5000),
            })
            out = pipe._handle_health()
        # Pre-§17.317 row shape: | name | icon status | ms |
        assert "| postgresql | ✅ up | 12 ms |" in out
        assert "| milvus | ❌ down | 5000 ms |" in out

    def test_unknown_status_uses_info_icon(self, pipe):
        """Pre-§17.317 contract: status not in UP/DOWN sets = ℹ️."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _health_response({
                "weird": {"status": "starting"},
            })
            out = pipe._handle_health()
        assert "ℹ️" in out
        # Neither up nor down → no verdict-side counts move.
        # 0 up + 0 down out of 1 total = "0 down" but verdict path
        # checks down_names emptiness, so all-up shape fires.
        assert "All 1 subsystems up" in out  # quirk: ℹ️ counted as "up"
                                              # for verdict purposes
                                              # (matches the "is anything
                                              # broken?" intent)


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_recovery_footer_helper_anchored(self):
        src = self._src()
        assert "def _render_health_recovery_footer" in src

    def test_unreachable_helper_anchored(self):
        src = self._src()
        assert "def _render_health_unreachable" in src

    def test_verdict_phrasing_anchored(self):
        """Pin the verdict phrasing so refactors that drop "subsystems
        up/down" wording trip review."""
        src = self._src()
        assert "All {total} subsystems up" in src
        assert "subsystems down:" in src

    def test_docker_compose_recovery_commands_anchored(self):
        """Pin the recovery footer's 4 commands — load-bearing for
        operators who land here without docker knowledge."""
        src = self._src()
        assert "docker compose ps" in src
        assert "docker compose restart" in src
        assert "docker compose logs" in src
        # Milvus-specific hint.
        assert "milvus boots slowly" in src

    def test_orchestrator_unreachable_uses_specific_service_name(self):
        """scaffold-orchestrator is the only definitively-named service
        across deployments — the unreachable path can name it
        directly. Pin the literal."""
        src = self._src()
        assert "scaffold-orchestrator" in src
