"""§17.428 — unit tests for the CodeGen Python-syntax gate.

Pure, offline (no LLM, no services). Exercises extract_code_blocks,
check_python_syntax, and format_syntax_reason directly. Part of the
default suite + the smoke tier.
"""
import pytest

from app.modules.execution_codegen_gate import (
    SyntaxFinding,
    check_python_syntax,
    extract_code_blocks,
    format_syntax_reason,
)

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# extract_code_blocks
# ---------------------------------------------------------------------------

def test_extract_returns_lang_and_code():
    out = "preamble\n```python\nx = 1\n```\ntrailer"
    blocks = extract_code_blocks(out)
    assert blocks == [("python", "x = 1\n")]


def test_extract_lowercases_and_strips_lang():
    out = "```Python3 \ny = 2\n```"
    assert extract_code_blocks(out)[0][0] == "python3"


def test_extract_unlabeled_fence_has_empty_lang():
    out = "```\nplain text\n```"
    assert extract_code_blocks(out)[0][0] == ""


def test_extract_multiple_blocks():
    out = "```python\na = 1\n```\nmid\n```js\nconst b = 2;\n```"
    blocks = extract_code_blocks(out)
    assert [b[0] for b in blocks] == ["python", "js"]


def test_extract_empty_output():
    assert extract_code_blocks("") == []
    assert extract_code_blocks(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# check_python_syntax — clean cases
# ---------------------------------------------------------------------------

def test_valid_module_passes():
    out = "```python\ndef add(a, b):\n    return a + b\n```"
    assert check_python_syntax(out) == []


def test_signature_stub_passes():
    # §17.374 module-only outputs / ... ellipsis stubs are valid Python.
    out = "```python\ndef merge_sorted(a, b): ...\n```"
    assert check_python_syntax(out) == []


def test_non_python_fence_is_noop():
    out = "```javascript\nconst x = ;\n```"  # invalid JS — but not our job
    assert check_python_syntax(out) == []


def test_unlabeled_fence_is_noop():
    out = "```\nthis is not parsed: def (\n```"
    assert check_python_syntax(out) == []


def test_no_fence_is_noop():
    assert check_python_syntax("just prose, no code at all") == []


def test_py_and_python3_tags_are_parsed():
    bad = "def f(\n"  # unterminated
    for tag in ("py", "python3"):
        out = f"```{tag}\n{bad}\n```"
        assert check_python_syntax(out), f"{tag} block should be parsed"


# ---------------------------------------------------------------------------
# check_python_syntax — failures
# ---------------------------------------------------------------------------

def test_broken_syntax_fails_with_location():
    out = "```python\ndef broken(:\n    pass\n```"
    findings = check_python_syntax(out)
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, SyntaxFinding)
    assert f.block_index == 1
    assert f.lineno is not None
    assert f.message  # non-empty SyntaxError message


def test_unterminated_string_fails():
    out = '```python\nmsg = "unterminated\n```'
    assert len(check_python_syntax(out)) == 1


def test_one_broken_among_many_blocks():
    out = (
        "```python\nok = 1\n```\n"
        "```python\ndef bad(:\n```\n"
        "```python\nfine = 2\n```"
    )
    findings = check_python_syntax(out)
    assert len(findings) == 1
    # block index counts ALL fenced blocks, so the bad one is block 2.
    assert findings[0].block_index == 2


# ---------------------------------------------------------------------------
# format_syntax_reason
# ---------------------------------------------------------------------------

def test_format_reason_empty_for_no_findings():
    assert format_syntax_reason([]) == ""


def test_format_reason_names_block_line_and_message():
    findings = check_python_syntax("```python\ndef bad(:\n```")
    reason = format_syntax_reason(findings)
    assert "§17.428" in reason
    assert "block 1" in reason
    assert "line" in reason
    assert "```python" in reason  # tells the model how to re-emit
