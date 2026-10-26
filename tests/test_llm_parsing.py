"""Tests for app/utils/llm_parsing.py (#9.20)."""
import pytest

from app.utils.llm_parsing import (
    parse_json_array,
    parse_json_object,
    parse_json_object_for_schema,
    parse_tool_call_text,
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


# ---------------------------------------------------------------------------
# parse_json_object_for_schema (§17.1824)
# ---------------------------------------------------------------------------
_VERDICT = {
    "type": "object",
    "properties": {"pass": {"type": "boolean"}, "reason": {"type": "string"},
                   "confidence": {"type": "number"}},
    "required": ["pass", "reason", "confidence"],
}

# The shape of the live reply (job 5b4880c0 T6, 2026-10-26): prose that quotes the step's code, no verdict.
_QUOTING_PROSE = (
    "Let me analyze this task carefully.\n\nThe batch records a rejection:\n```python\n"
    "await write_risk_check(\n    proposal_id=proposal_id,\n    passed=False,\n"
    '    metrics={"rejected": True},\n)\n```\n\nAnd the approval it reads back:\n'
    '`{"operator_id": "op", "decision": "approved", "reason": ""}`. Hmm, let me'
)


def test_schema_parse_ignores_literals_the_reply_quotes():
    assert parse_json_object(_QUOTING_PROSE) == {"rejected": True}   # what the coaxed verifier used to get
    assert parse_json_object_for_schema(_QUOTING_PROSE, _VERDICT) is None


def test_schema_parse_takes_the_verdict_after_quoted_code():
    raw = _QUOTING_PROSE + ' decide.\n\n{"pass": false, "reason": "no timestamps written", "confidence": 0.8}'
    assert parse_json_object_for_schema(raw, _VERDICT) == {
        "pass": False, "reason": "no timestamps written", "confidence": 0.8}


def test_schema_parse_prefers_the_complete_and_the_last_answer():
    raw = ('first {"pass": true} then {"pass": false, "reason": "a", "confidence": 0.5}'
           ' and {"pass": true, "reason": "b", "confidence": 0.9}')
    assert parse_json_object_for_schema(raw, _VERDICT)["reason"] == "b"


def test_schema_parse_keeps_a_partial_answer_of_the_schemas_own_keys():
    assert parse_json_object_for_schema('{"pass": true}', _VERDICT) == {"pass": True}
    assert parse_json_object_for_schema('{"pass": true, "reason": "ok", "confidence": 1, "x": 1}', _VERDICT)["x"] == 1


def test_schema_parse_without_properties_is_parse_json_object():
    assert parse_json_object_for_schema('{"a": 1}', {"type": "object"}) == {"a": 1}


def test_schema_parse_keeps_a_verdict_only_repair_can_close():
    """A reply cut off before its closing brace has no balanced span; the repaired whole-text parse is the answer."""
    raw = '{"pass": false, "reason": "the batch never writes the data timestamp", "confidence": 0.7'
    assert parse_json_object_for_schema(raw, _VERDICT) == {
        "pass": False, "reason": "the batch never writes the data timestamp", "confidence": 0.7}


# ---------------------------------------------------------------------------
# parse_tool_call_text (§17.1827)
# ---------------------------------------------------------------------------
# The live tail (job 5b4880c0 T6, 2026-10-26): reasoning, then the call written as text with a keyword-named arg.
_WRITTEN_VERDICT = (
    "Let me analyze this task carefully. The output calls `load_batch_context(timestamp=now, db=db)` and "
    "`record_verification(pass=true)` would be wrong here.\n\nThat's a bit long but one sentence. Fine.</think>"
    "record_verification(pass=false, reason=\"The batch reads ctx['equity'] (a key the data layer never returns), "
    "so it always skips; and \\\"pass=\\\" in a reason (or a comma, ) paren) stays text.\", confidence=0.8)"
)


def test_a_call_written_as_text_is_the_tool_call():
    args = parse_tool_call_text(_WRITTEN_VERDICT, "record_verification", _VERDICT)
    assert args["pass"] is False and args["confidence"] == 0.8
    assert args["reason"].startswith("The batch reads ctx['equity']") and 'pass=' in args["reason"]


def test_a_written_call_with_a_dict_argument_and_python_literals():
    raw = 'Done.\nrecord_verification({"pass": True, "reason": "ok", "confidence": 1})'
    assert parse_tool_call_text(raw, "record_verification", _VERDICT) == {"pass": True, "reason": "ok", "confidence": 1}


def test_a_written_call_must_answer_the_schema_and_be_that_tool():
    assert parse_tool_call_text("record_verification(rejected=True)", "record_verification", _VERDICT) is None
    assert parse_tool_call_text("my_record_verification(pass=true, reason='x', confidence=1)",
                                "record_verification", _VERDICT) is None
    assert parse_tool_call_text("record_verification(pass=true, reason='x'", "record_verification", _VERDICT) is None
    assert parse_tool_call_text("no call here", "record_verification", _VERDICT) is None
