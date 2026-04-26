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
    def test_distill_system_emits_title(self):
        assert '"title"' in gt.DISTILL_SYSTEM
        assert '"topic"' not in gt.DISTILL_SYSTEM

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
        fake_search = [{"title": "t", "url": "https://x.test/1", "content": "c"}]
        fake_resp = MagicMock(
            success=True,
            text='[{"title":"fact","content":"c","tags":"a","source":"u"}]',
            model="qwen3:4b",
            total_duration_ms=123,
            error=None,
        )
        with patch.object(gt, "search_searxng", AsyncMock(return_value=fake_search)), \
             patch.object(gt.model_router, "generate", AsyncMock(return_value=fake_resp)) as gen:
            await gt.extract_ground_truths("rag systems")

        assert gen.call_count == 1
        assert gen.call_args.kwargs["model"] == gt.settings.model_router


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
