"""Smoke tests for gt_extractor.py (Phase 6 audit fixes).

Covers:
    #6.3  — distillation uses model_router (regression guard)
    #6.14 — GitHub target comes from settings, not hardcoded
    #6.17 — sanitize_toon_content escapes \\n \\t " \\
    #6.18 — DISTILL_SYSTEM + row format use 'title', not legacy 'topic'
"""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_candidates = [
    Path(__file__).resolve().parent.parent / "app" / "modules" / "gt_extractor.py",
    Path("/code/app/modules/gt_extractor.py"),
]
_path = next((p for p in _candidates if p.exists()), None)
if _path is None:
    pytest.skip("gt_extractor.py not found", allow_module_level=True)

spec = importlib.util.spec_from_file_location("gt_extractor_mod", _path)
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)


@pytest.mark.smoke
class TestSanitizeToonContent:
    def test_escapes_newlines(self):
        assert gt.sanitize_toon_content("line1\nline2") == "line1\\nline2"

    def test_escapes_tabs(self):
        assert gt.sanitize_toon_content("col1\tcol2") == "col1\\tcol2"

    def test_escapes_quotes(self):
        assert gt.sanitize_toon_content('say "hi"') == 'say \\"hi\\"'

    def test_escapes_backslashes_first(self):
        assert gt.sanitize_toon_content("a\\nb") == "a\\\\nb"

    def test_round_trip_multiline(self):
        raw = 'hello\nworld\ttab"q\\back'
        out = gt.sanitize_toon_content(raw)
        assert "\n" not in out and "\t" not in out
        assert "\\n" in out and "\\t" in out and '\\"' in out and "\\\\" in out


@pytest.mark.smoke
class TestTitleFieldConsistency:
    def test_distill_tool_schema_uses_title_not_topic(self):
        """Sprint X.12: the JSON schema moved from DISTILL_SYSTEM prose
        into RECORD_DISTILLED_ENTRIES_TOOL.input_schema. The naming
        consistency check now lives there — `title` is the canonical
        field, `topic` is the legacy alias _normalize_legacy_keys
        rewrites."""
        item_props = (
            gt.RECORD_DISTILLED_ENTRIES_TOOL.input_schema
            ["properties"]["entries"]["items"]["properties"]
        )
        assert "title" in item_props
        assert "topic" not in item_props

    def test_new_header_uses_title(self):
        hdr = gt._new_toon_header("knowledge/rag-systems.toon")
        assert "id,title,content" in hdr
        assert "id,topic,content" not in hdr

    def test_legacy_key_normalized(self):
        entries = [{"topic": "legacy", "content": "x"}]
        out = gt._normalize_legacy_keys(entries)
        assert out[0]["title"] == "legacy"
        assert "topic" not in out[0]

    def test_format_rows_reads_title(self):
        rows = gt._format_toon_rows([{"title": "my-title", "content": "fact", "tags": "t1"}])
        assert "my-title" in rows[0]


@pytest.mark.smoke
class TestDistillationUsesRouterModel:
    @pytest.mark.asyncio
    async def test_extract_uses_model_router(self):
        """Sprint E.7 + X.12: distill defaults to role="model_router". The
        call site uses model_router.tool_call (post-X.12) but the role/
        overrides kwargs threading is identical."""
        fake_search = [{"title": "t", "url": "https://x.test/1", "content": "c"}]
        fake_call = MagicMock()
        fake_call.arguments = {
            "entries": [
                {"title": "fact", "content": "c", "tags": "a", "source": "u"},
            ],
        }
        fake_resp = MagicMock(
            success=True,
            text="",
            model="qwen3:4b",
            total_duration_ms=123,
            error=None,
            tool_calls=[fake_call],
        )
        with patch.object(gt, "search_searxng", AsyncMock(return_value=fake_search)), \
             patch.object(gt.model_router, "tool_call", AsyncMock(return_value=fake_resp)) as tc:
            await gt.extract_ground_truths("rag systems")

        assert tc.call_count == 1
        assert tc.call_args.kwargs.get("role") == "model_router"
        assert "model" not in tc.call_args.kwargs


@pytest.mark.smoke
class TestGitHubTargetConfig:
    def test_push_signature_has_owner_repo_kwargs(self):
        import inspect
        sig = inspect.signature(gt._push_to_github)
        for name in ("owner", "repo", "branch"):
            assert name in sig.parameters
            assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
            assert sig.parameters[name].default is None

    def test_new_header_uses_settings_source(self):
        hdr = gt._new_toon_header("knowledge/rag-systems.toon")
        expected = f"{gt.settings.gt_github_owner}/{gt.settings.gt_github_repo}"
        assert expected in hdr
