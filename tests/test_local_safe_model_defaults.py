"""§17.819 (audit plan 6.2) — local-safe model defaults + wizard pointer.

Two guarantees:
  1. Every switchable role's *code default* is a local tag (no ":...-cloud"
     suffix) so a fresh install with no .env pins makes zero cloud calls.
     Pinned envs (the operator's box) are unaffected — this asserts the
     Field defaults, not the live settings singleton.
  2. /health grows an advisory warning when role tags aren't present in the
     daemon's pulled list, pointing at the connect-models wizard. Unit-tested
     via the extracted _model_role_warnings helper (the endpoint's checks are
     request-scoped closures).
"""
from __future__ import annotations

from unittest.mock import patch

from app.config import SWITCHABLE_ROLE_FIELDS, Settings, settings
from app.main import _model_role_warnings


class TestLocalSafeDefaults:
    def test_every_switchable_role_code_default_is_local(self):
        """Field defaults (NOT the env-influenced singleton) carry no cloud tag."""
        defaults = {
            role: Settings.model_fields[role].default
            for role in SWITCHABLE_ROLE_FIELDS
        }
        cloudy = {r: m for r, m in defaults.items() if m.endswith("-cloud") or ":cloud" in m}
        assert not cloudy, f"cloud tags in code defaults break fresh-install local safety: {cloudy}"

    def test_defaults_are_nonempty_strings(self):
        for role in SWITCHABLE_ROLE_FIELDS:
            d = Settings.model_fields[role].default
            assert isinstance(d, str) and d.strip(), f"{role} default is blank"


class TestModelRoleWarnings:
    def test_all_pulled_yields_no_warning(self):
        pulled = {getattr(settings, role) for role in SWITCHABLE_ROLE_FIELDS}
        assert _model_role_warnings(pulled) == []

    def test_missing_tag_warns_and_points_at_wizard(self):
        with patch.object(settings, "model_coder", "not-pulled:0b"):
            pulled = {
                getattr(settings, role)
                for role in SWITCHABLE_ROLE_FIELDS
                if role != "model_coder"
            }
            warnings = _model_role_warnings(pulled)
        assert len(warnings) == 1
        assert "model_coder=not-pulled:0b" in warnings[0]
        assert "/ui/#/setup" in warnings[0]

    def test_empty_daemon_lists_every_switchable_role(self):
        warnings = _model_role_warnings(set())
        assert len(warnings) == 1
        for role in SWITCHABLE_ROLE_FIELDS:
            assert role in warnings[0]
