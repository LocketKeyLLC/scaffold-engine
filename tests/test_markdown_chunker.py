"""Tests for §17.119 — markdown split-by-kind."""
from __future__ import annotations

import pytest

from app.utils.markdown_chunker import split_markdown_by_kind


@pytest.mark.smoke
class TestSplitMarkdownByKind:
    def test_empty_input_returns_empty(self):
        assert split_markdown_by_kind("") == []
        assert split_markdown_by_kind("   \n\n  ") == []

    def test_no_fences_returns_single_prose_chunk(self):
        text = "This is just prose. No code at all."
        result = split_markdown_by_kind(text)
        assert result == [(text, "prose")]

    def test_single_fence_splits_prose_code_prose(self):
        text = (
            "Intro paragraph here.\n\n"
            "```python\n"
            "def foo():\n"
            "    return 42\n"
            "```\n\n"
            "Closing paragraph."
        )
        result = split_markdown_by_kind(text)
        assert len(result) == 3
        assert result[0] == ("Intro paragraph here.", "prose")
        assert result[1] == ("def foo():\n    return 42", "code")
        assert result[2] == ("Closing paragraph.", "prose")

    def test_leading_fence_no_prose_prefix(self):
        text = "```bash\necho hi\n```\n\nAfter."
        result = split_markdown_by_kind(text)
        assert result == [("echo hi", "code"), ("After.", "prose")]

    def test_trailing_fence_no_prose_suffix(self):
        text = "Before.\n\n```\nraw\n```"
        result = split_markdown_by_kind(text)
        assert result == [("Before.", "prose"), ("raw", "code")]

    def test_multiple_fences(self):
        text = (
            "First.\n\n"
            "```py\nx = 1\n```\n\n"
            "Middle.\n\n"
            "```py\ny = 2\n```\n\n"
            "End."
        )
        result = split_markdown_by_kind(text)
        kinds = [k for _, k in result]
        assert kinds == ["prose", "code", "prose", "code", "prose"]
        assert result[1][0] == "x = 1"
        assert result[3][0] == "y = 2"

    def test_fence_with_language_tag_dropped(self):
        text = "```python\ncode\n```"
        result = split_markdown_by_kind(text)
        assert result == [("code", "code")]

    def test_fence_without_language_tag(self):
        text = "```\nplain code\n```"
        result = split_markdown_by_kind(text)
        assert result == [("plain code", "code")]

    def test_empty_fence_dropped(self):
        text = "Before.\n\n```\n\n```\n\nAfter."
        result = split_markdown_by_kind(text)
        # Empty code body → dropped; only the two prose chunks remain.
        kinds = [k for _, k in result]
        assert "code" not in kinds
        assert kinds == ["prose", "prose"]

    def test_consecutive_fences_no_prose_between(self):
        text = "```\nA\n```\n```\nB\n```"
        result = split_markdown_by_kind(text)
        kinds = [k for _, k in result]
        # Two code chunks, no prose interleaved (whitespace between
        # fences is stripped to nothing).
        assert kinds == ["code", "code"]
        assert result[0][0] == "A"
        assert result[1][0] == "B"

    def test_real_world_readme_shape(self):
        """Sample README structure: title, install, usage with code, license."""
        text = (
            "# My Library\n\n"
            "A short description.\n\n"
            "## Install\n\n"
            "```bash\n"
            "pip install mylib\n"
            "```\n\n"
            "## Usage\n\n"
            "Call `foo`:\n\n"
            "```python\n"
            "import mylib\n"
            "mylib.foo()\n"
            "```\n\n"
            "## License\n\n"
            "MIT."
        )
        result = split_markdown_by_kind(text)
        kinds = [k for _, k in result]
        # 3 prose chunks (title+desc+Install header, Usage prose, License),
        # 2 code chunks (pip install, import+call).
        assert kinds.count("code") == 2
        assert kinds.count("prose") == 3
        code_chunks = [t for t, k in result if k == "code"]
        assert "pip install mylib" in code_chunks
        assert "import mylib\nmylib.foo()" in code_chunks
