"""Tests for app/utils/llm_parsing.py (#9.20)."""
import pytest

from app.utils.llm_parsing import (
    parse_json_array,
    parse_json_object,
    strip_think_tags,
)


# ---------------------------------------------------------------------------
# strip_think_tags
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_strip_think_tag_pair():
    assert strip_think_tags("hello <think>reason</think> world") == "hello  world"


@pytest.mark.smoke
def test_strip_thinking_tag_pair():
    assert strip_think_tags("<thinking>blah</thinking>out") == "out"


@pytest.mark.smoke
def test_strip_unterminated_think_tag():
    """If the close tag is missing, everything from open-tag onward is removed."""
    assert strip_think_tags("keep this <think>then truncated forever") == "keep this"


@pytest.mark.smoke
def test_strip_multiline_think_block():
    src = "before\n<think>line1\nline2\n</think>\nafter"
    assert strip_think_tags(src) == "before\n\nafter"


# ---------------------------------------------------------------------------
# parse_json_object — 4-step fallback chain
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_parse_object_plain_json():
    result = parse_json_object('{"a": 1, "b": "two"}')
    assert result == {"a": 1, "b": "two"}


@pytest.mark.smoke
def test_parse_object_strips_markdown_fences():
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_object(raw) == {"a": 1}


@pytest.mark.smoke
def test_parse_object_strips_think_then_parses():
    raw = '<think>first I need to think</think>\n{"ok": true}'
    assert parse_json_object(raw) == {"ok": True}


@pytest.mark.smoke
def test_parse_object_repairs_trailing_comma():
    """json.loads fails on trailing commas; json_repair handles it."""
    result = parse_json_object('{"a": 1, "b": 2,}')
    assert result == {"a": 1, "b": 2}


@pytest.mark.smoke
def test_parse_object_brace_extract_from_preamble():
    raw = 'Here is the JSON you asked for:\n{"x": 42}\nThanks.'
    assert parse_json_object(raw) == {"x": 42}


@pytest.mark.smoke
def test_parse_object_returns_none_on_garbage():
    assert parse_json_object("not json at all") is None


@pytest.mark.smoke
def test_parse_object_rejects_list_root():
    """A top-level list is not a JSON object."""
    assert parse_json_object("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# parse_json_array — 4-step fallback chain
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_parse_array_plain():
    assert parse_json_array("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.smoke
def test_parse_array_strips_markdown_fences():
    assert parse_json_array('```\n[1, 2]\n```') == [1, 2]


@pytest.mark.smoke
def test_parse_array_bracket_extract_from_preamble():
    raw = 'Results:\n["a", "b", "c"]\nEnd.'
    assert parse_json_array(raw) == ["a", "b", "c"]


@pytest.mark.smoke
def test_parse_array_rejects_object_root():
    assert parse_json_array('{"a": 1}') is None


@pytest.mark.smoke
def test_parse_array_returns_none_on_garbage():
    assert parse_json_array("definitely not json") is None


# ---------------------------------------------------------------------------
# §17.601 — raw-first fast path: don't corrupt valid JSON whose string values
# legitimately contain "<think>" or triple-backticks
# ---------------------------------------------------------------------------
def test_parse_object_preserves_literal_think_substring_in_value():
    raw = '{"code": "if flag: print(\\"<think>\\")", "ok": true}'
    result = parse_json_object(raw)
    assert result == {"code": 'if flag: print("<think>")', "ok": True}


def test_parse_object_preserves_backticks_in_value():
    raw = '{"snippet": "run ```py``` here", "ok": true}'
    result = parse_json_object(raw)
    assert result == {"snippet": "run ```py``` here", "ok": True}


def test_parse_array_preserves_literal_think_substring_in_value():
    raw = '["<think>not-a-tag", "b"]'
    assert parse_json_array(raw) == ["<think>not-a-tag", "b"]


def test_parse_object_still_strips_fences_when_raw_invalid():
    # Regression guard: the fence/think strip path still works when the raw
    # text isn't valid JSON verbatim.
    assert parse_json_object("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert parse_json_object("<think>reasoning</think>{\"ok\": true}") == {"ok": True}
