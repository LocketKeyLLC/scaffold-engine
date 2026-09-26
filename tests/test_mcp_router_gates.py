"""§17.1172 — the `/mcp` router is admin-only, and `/call` re-applies the
read-only gate.

Audit 2026-09-25 found the router carrying no dependency at all, on the
reasoning its own docstring stated: *"Registry CRUD is always available (it only
writes DB rows)."* That premise is false. A row with ``transport="stdio"`` is a
command line: ``mcp_client._open_session`` turns it into
``StdioServerParameters(command=…, args=…, env=…)`` → ``stdio_client(params)``,
a subprocess INSIDE the orchestrator container, which holds DATABASE_URL,
SCAFFOLD_API_KEY, GITHUB_TOKEN and the Fernet secret. With
``multi_user_enabled`` (live on the operator's box) any issued key — role
``user`` included — could register one, and merely entering a session
(``GET /servers/{name}/tools``) spawned it.

The second half: ``POST /servers/{name}/call`` called ``mcp_client.call_tool``
straight through. It is the one door to a registered local runner with NO
``read_only_command`` anywhere in its path — the helper's own gate was the only
check, and the same audit measured that gate accepting ``bash -lc '<anything>'``
against a helper systemd ran as root.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.authz import require_admin
from app.routers import mcp as mcp_router

pytestmark = pytest.mark.smoke


def _deps(router):
    return {getattr(d, "dependency", None) for d in (router.dependencies or [])}


class TestAdminOnly:
    def test_the_router_itself_requires_admin(self):
        assert require_admin in _deps(mcp_router.router)

    def test_every_route_inherits_it(self):
        """Router-level rather than per-endpoint on purpose: a new endpoint
        added to this file must not be able to ship ungated."""
        for route in mcp_router.router.routes:
            names = {getattr(d.dependency, "__name__", "") for d in route.dependencies}
            assert "require_admin" in names, route.path

    def test_the_mutating_endpoints_are_covered(self):
        """The three the audit named, by path, so a refactor that drops the
        router dependency fails here with the reason attached."""
        paths = {(r.path, tuple(sorted(r.methods))) for r in mcp_router.router.routes}
        for want in [("/mcp/servers", ("POST",)), ("/mcp/servers/{name}", ("DELETE",)),
                     ("/mcp/servers/{name}/call", ("POST",))]:
            assert want in paths, want


class TestCallSurfaceReGates:
    """`run_readonly` takes a shell command as an argument. Invoking it here
    must be judged by the SAME gate the assist paths use, so the engine never
    SENDS what it would not send from a walkthrough."""

    def _body(self, command, tool="run_readonly"):
        return mcp_router.McpToolCallInput(tool=tool, arguments={"command": command})

    @pytest.mark.parametrize("cmd", [
        "rm -rf /tmp/x",
        "bash -lc 'rm -rf /'",              # the §17.1171 bypass, refused here too
        "apt install -y jq",
        "cat /etc/hosts > /tmp/out",
        "bash /tmp/whatever.sh",
    ])
    def test_a_writing_command_is_refused_with_422(self, cmd):
        with pytest.raises(HTTPException) as exc:
            mcp_router._require_read_only(self._body(cmd))
        assert exc.value.status_code == 422
        assert "read-only" in str(exc.value.detail)

    @pytest.mark.parametrize("cmd", [
        "cat /etc/hostname",
        "pct config 110 | grep -E 'net0|hostpci'",
        "sh -c 'cat /etc/os-release'",
        "systemctl status caddy --no-pager",
    ])
    def test_a_reading_command_passes(self, cmd):
        mcp_router._require_read_only(self._body(cmd))   # must not raise

    def test_a_tool_that_is_not_a_shell_surface_is_not_gated(self):
        """The gate is keyed on the tool whose ARGUMENT is a command; an
        ordinary MCP tool's arguments are not shell and must not be judged as
        though they were."""
        mcp_router._require_read_only(
            mcp_router.McpToolCallInput(tool="search_docs", arguments={"command": "rm -rf /"}))

    def test_a_missing_or_empty_command_is_refused_not_passed_through(self):
        """Fail closed: `run_readonly` with nothing to judge is not a read."""
        for args in ({}, {"command": ""}, {"command": "   "}):
            with pytest.raises(HTTPException):
                mcp_router._require_read_only(
                    mcp_router.McpToolCallInput(tool="run_readonly", arguments=args))

    def test_the_gated_tool_table_names_the_runner_tool(self):
        assert mcp_router._SHELL_TOOL_ARGS["run_readonly"] == "command"
