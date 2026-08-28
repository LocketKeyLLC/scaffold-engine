"""§17.854 (audit F3/F4/F5) — pipeline drift fixes.

F3: input normalization (`-la`→`--la`, unicode dashes) only applies to slash
    commands, so pasted shell evidence on NL turns is preserved verbatim.
F4: the §17.761 orientation path emits a hidden reference-link session marker so
    history recovery works when the start turn led with an orientation.
F5: the --quick sentinel is an invisible reference-link marker, not a visible
    HTML comment; the legacy comment is still recognized on read.

Loaded via the pipeline harness (needs --noconftest, per §17.807).
"""
import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_UUID = "12345678-1234-1234-1234-123456789abc"


# ── F4 — orientation session marker recovery ───────────────────────────────
class TestAssistSessionMarkerRecovery:
    def test_regex_matches_visible_banner(self, pipe):
        content = f"🤝 **Assist session started** — `{_UUID}`\n\nJob..."
        m = pipe._ASSIST_SESSION_MARKER_RE.search(content)
        assert m and m.group(1) == _UUID

    def test_regex_matches_hidden_orientation_marker(self, pipe):
        # the invisible reference-link marker the orient path now emits
        content = f"Here's where you are...\n\n[asess]: ASSIST_SESSION:{_UUID}\n"
        m = pipe._ASSIST_SESSION_MARKER_RE.search(content)
        assert m and m.group(1) == _UUID


# ── F5 — quick sentinel is invisible + legacy-compatible ───────────────────
class TestQuickSentinel:
    def test_new_marker_is_reference_link_form(self, pipe):
        # reference-link definitions render as nothing in OWUI (not an HTML
        # comment, which OWUI v0.11 shows as literal text).
        assert pipe._QUICK_PENDING_MARKER.startswith("[")
        assert "<!--" not in pipe._QUICK_PENDING_MARKER

    def test_pending_was_quick_recognizes_new_marker(self, pipe):
        brief = pipe._PENDING_BRIEF_MARKER
        msgs = [{"role": "assistant",
                 "content": f"{brief}\n\nsome brief\n\n{pipe._QUICK_PENDING_MARKER}"}]
        assert pipe._pending_was_quick(msgs) is True

    def test_pending_was_quick_recognizes_legacy_comment(self, pipe):
        brief = pipe._PENDING_BRIEF_MARKER
        msgs = [{"role": "assistant",
                 "content": f"{brief}\n\nsome brief\n\n{pipe._QUICK_PENDING_LEGACY}"}]
        assert pipe._pending_was_quick(msgs) is True

    def test_pending_was_quick_false_without_marker(self, pipe):
        brief = pipe._PENDING_BRIEF_MARKER
        msgs = [{"role": "assistant", "content": f"{brief}\n\nplain brief, no quick"}]
        assert pipe._pending_was_quick(msgs) is False


# ── F3 — normalization scoped to slash commands ────────────────────────────
class TestNormalizeScope:
    def test_normalize_input_still_rewrites_flags(self):
        # the helper itself is unchanged — it's the CALL that's now gated.
        from tests._scaffold_router_setup import _mod as _router_mod
        norm, rewrites = _router_mod._normalize_input("run -la now")
        assert "--la" in norm and rewrites

    def test_normalize_call_is_slash_gated(self):
        """The `_normalize_input(msg)` call in pipe() must sit under a
        `msg.startswith("/")` guard so NL evidence isn't rewritten. Source-level
        assertion (the repo's static-scan convention) — a full pipe() drive of an
        NL turn would hit the triage network path."""
        import inspect, re
        from tests._scaffold_router_setup import Pipeline as _P
        src = inspect.getsource(_P.pipe)
        # locate the guarded block and confirm the normalize call lives inside it
        m = re.search(
            r'if msg\.startswith\("/"\):\s*\n\s*msg, _rewrites = _normalize_input\(msg\)',
            src,
        )
        assert m is not None, (
            "the _normalize_input(msg) call must be guarded by "
            'msg.startswith("/") — NL evidence would be corrupted (F3 regression)')
