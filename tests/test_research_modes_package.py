"""§17.298 — research_modes/ package contracts.

§17.280-🟢-3 closeout. The four producer modes (OpenAPI, GitHub, HF,
Forum) were lifted from ``app/modules/research_agent.py`` into a new
``app/modules/research_modes/`` package. Each mode exports a single
``run_research_<mode>_mode`` coroutine; ``research_agent`` re-binds
them under their pre-§17.298 underscore-private aliases so the
dispatch in ``run_research`` (plus every test patching the aliased
names) keeps working.

These tests pin the package boundary so the next audit pass finds:

  1. Each ``research_modes/<mode>.py`` exports the expected coroutine
     under the expected name.
  2. ``research_agent``'s underscore-aliased name resolves to the
     vendor coroutine (identity match) — so a future "move it back"
     refactor or a typo in the alias is visible.
  3. The four mode bodies are absent from ``research_agent.py``
     (source-shape regression guard against revert).
"""
from __future__ import annotations

import inspect

import pytest


_MODE_NAMES = ["openapi", "github", "hf", "forum"]


@pytest.mark.smoke
class TestResearchModesPackageExports:
    """§17.298 — each mode module exports its run_* coroutine."""

    @pytest.mark.parametrize("mode_name", _MODE_NAMES)
    def test_mode_module_imports(self, mode_name):
        """The mode module is importable via the normal package path."""
        mod = __import__(
            f"app.modules.research_modes.{mode_name}",
            fromlist=[f"run_research_{mode_name}_mode"],
        )
        assert hasattr(mod, f"run_research_{mode_name}_mode"), (
            f"§17.298: app.modules.research_modes.{mode_name} no longer "
            f"exports run_research_{mode_name}_mode. The dispatch in "
            f"research_agent re-binds this name; missing it is a fatal "
            f"import error at orchestrator startup."
        )

    @pytest.mark.parametrize("mode_name", _MODE_NAMES)
    def test_mode_function_is_async_generator(self, mode_name):
        """Each mode runner must be an async-generator coroutine —
        ``run_research`` uses ``async for evt in <fn>(...)``."""
        mod = __import__(
            f"app.modules.research_modes.{mode_name}",
            fromlist=[f"run_research_{mode_name}_mode"],
        )
        fn = getattr(mod, f"run_research_{mode_name}_mode")
        assert inspect.isasyncgenfunction(fn), (
            f"§17.298: run_research_{mode_name}_mode is no longer an "
            f"async-generator function. The dispatch in research_agent "
            f"iterates with `async for evt in fn(...)`; a regular "
            f"coroutine would break the dispatch."
        )


@pytest.mark.smoke
class TestResearchAgentAliasesIdentity:
    """§17.298 — research_agent's _run_research_*_mode names point at
    the vendor module functions, not duplicate inline copies."""

    @pytest.mark.parametrize("mode_name", _MODE_NAMES)
    def test_alias_identical_to_vendor_function(self, mode_name):
        """``research_agent._run_research_<mode>_mode is research_modes.<mode>.run_research_<mode>_mode``.

        Identity check — not equality — because the audit invariant is
        that the SAME function object is reached via both the legacy
        alias and the new public name. A reverted refactor that re-
        inlined the body would produce DIFFERENT function objects with
        identical behavior; the identity check catches that drift.
        """
        from app.modules import research_agent
        vendor_mod = __import__(
            f"app.modules.research_modes.{mode_name}",
            fromlist=[f"run_research_{mode_name}_mode"],
        )
        alias = getattr(research_agent, f"_run_research_{mode_name}_mode")
        canonical = getattr(vendor_mod, f"run_research_{mode_name}_mode")
        assert alias is canonical, (
            f"§17.298 regression: research_agent._run_research_{mode_name}_mode "
            f"is NOT the vendor's run_research_{mode_name}_mode function. "
            f"Either the alias was renamed or the vendor body was "
            f"re-inlined into research_agent.py."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.298 — research_agent.py no longer carries the 4 mode bodies."""

    def test_no_inline_openapi_mode_body(self):
        from app.modules import research_agent
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()
        # The first-line marker of the pre-§17.298 OpenAPI body — used
        # in the original function and unique to it. Other modules
        # don't `from app.utils.openapi_ingest import fetch_and_parse_spec`.
        assert "from app.utils.openapi_ingest import fetch_and_parse_spec" not in src, (
            "§17.298 regression: OpenAPI fetch_and_parse_spec import "
            "has reappeared in research_agent.py. The mode body must "
            "live in app/modules/research_modes/openapi.py only."
        )

    def test_no_inline_github_mode_body(self):
        from app.modules import research_agent
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()
        # Anchor on the github_ingest import bundle that was unique to
        # the github mode body (URL mode doesn't pull these together).
        assert "fetch_repo_discussions" not in src, (
            "§17.298 regression: GitHub mode-specific imports have "
            "reappeared in research_agent.py. The mode body must live "
            "in app/modules/research_modes/github.py only."
        )

    def test_no_inline_hf_mode_body(self):
        from app.modules import research_agent
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()
        # `fetch_hf` is the unique entry point of the HF mode body.
        assert "from app.utils.hf_ingest import fetch_hf" not in src, (
            "§17.298 regression: HF mode-specific imports have "
            "reappeared in research_agent.py. The mode body must live "
            "in app/modules/research_modes/hf.py only."
        )

    def test_no_inline_forum_mode_body(self):
        from app.modules import research_agent
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()
        # The 5-fetcher import bundle was unique to the forum body.
        assert (
            "fetch_arxiv" in src
            and "fetch_so_answers" in src
            and "fetch_wiki_pages" in src
        ) is False, (
            "§17.298 regression: forum mode-specific imports "
            "(fetch_arxiv / fetch_so_answers / fetch_wiki_pages) have "
            "reappeared in research_agent.py. The mode body must live "
            "in app/modules/research_modes/forum.py only."
        )

    def test_alias_assignment_block_present(self):
        """The 4 alias assignments must be visible in research_agent.py
        source so a future maintainer reading the dispatch can trace
        back to the vendor file."""
        from app.modules import research_agent
        with open(research_agent.__file__, encoding="utf-8") as f:
            src = f.read()
        for name in _MODE_NAMES:
            anchor = f"_run_research_{name}_mode = _{name}_mode.run_research_{name}_mode"
            assert anchor in src, (
                f"§17.298: alias assignment for {name} mode is missing "
                f"from research_agent.py. Expected line: {anchor!r}"
            )
