"""Shared LLM output parsing utilities.

Regexes are compiled once at import time and reused across all calls.
"""
import json
import logging
import re

from json_repair import repair_json

logger = logging.getLogger("scaffold.parsing")

# Two compiled patterns cover both <think> and <thinking>:
#   - CLOSED: well-formed <tag>...</tag>
#   - OPEN:   stray <tag> with no closing tag (truncated mid-stream)
# Both branches are non-greedy for the closed case and anchored to end-of-text
# for the open case, matching the semantics of the prior four regexes.
_THINK_CLOSED_RE = re.compile(r"<(?:think|thinking)>.*?</(?:think|thinking)>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking)>.*", re.DOTALL)

# Markdown fence stripper: match ``` optionally followed by a language tag,
# anywhere in the string. Previously only stripped leading fences; now also
# catches trailing ``` and fences embedded around prose.
_FENCE_RE = re.compile(r"^[ \t]*```[A-Za-z0-9_+-]*[ \t]*\r?\n?|\r?\n?[ \t]*```[ \t]*$|```", re.MULTILINE)


def strip_think_tags(text: str) -> str:
    """Remove <think>/<thinking> reasoning blocks (closed or open) from LLM output."""
    cleaned = _THINK_CLOSED_RE.sub("", text)
    cleaned = _THINK_OPEN_RE.sub("", cleaned)
    return cleaned.strip()


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown fences from text, wherever they appear."""
    return _FENCE_RE.sub("", text).strip()


def _extract_by_brackets(text: str, open_b: str, close_b: str):
    """Pull the outermost bracket-delimited substring from text.

    Counts nesting depth from the first `open_b` and returns the slice up
    to its matching `close_b`. Trailing junk after the matched close
    bracket is dropped, which matters for malformed LLM output like
    `{"k": "v"}} extra` — `rfind` would have included the second `}`.
    Falls back to `rfind` semantics if no balanced match is found, so
    the downstream `repair_json` still has something to chew on.
    """
    start = text.find(open_b)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_b:
            depth += 1
        elif ch == close_b:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    end = text.rfind(close_b)
    if end > start:
        return text[start : end + 1]
    return None


def parse_json_object(raw: str):
    """Parse a JSON object from raw LLM output (4-step chain)."""
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    try:
        repaired = repair_json(cleaned, return_objects=True)
        if isinstance(repaired, dict):
            logger.info("json_repaired: method=full_text target=object")
            return repaired
    except Exception:
        pass
    fragment = _extract_by_brackets(cleaned, "{", "}")
    if fragment:
        try:
            result = json.loads(fragment)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        try:
            repaired = repair_json(fragment, return_objects=True)
            if isinstance(repaired, dict):
                logger.info("json_repaired: method=brace_extract target=object")
                return repaired
        except Exception:
            pass
    return None


def diagnose_json_object_parse(raw: str) -> dict | None:
    """§17.293 — return diagnostic context for the first ``JSONDecodeError``
    encountered while parsing ``raw`` as a JSON object.

    Mirrors the first step of ``parse_json_object``'s pipeline (strip
    think-tags + markdown fences, then ``json.loads``). Returns a dict
    with ``lineno`` / ``colno`` / ``msg`` / ``pos`` from the exception
    when that first parse fails; returns ``None`` when the first parse
    succeeds (meaning ``parse_json_object`` would also have succeeded).

    Operator-facing usage: when ``parse_json_object`` returns ``None``,
    callers can attach this diagnostic to the failure dict so the
    operator sees ``line 3, col 12, "Expecting ',' delimiter"`` next to
    the truncated raw output, instead of having to scan the snippet by
    eye. The deeper repair / fragment-extract steps in the pipeline
    swallow further errors silently; this helper deliberately reports
    only the first one, which is the most diagnostic for the operator
    (it pinpoints where the LLM first deviated from valid JSON).
    """
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return None
        # Parsed cleanly but wrong shape (e.g., an array) — that's a
        # caller-side concern, not a JSONDecodeError. Report None so the
        # parse_error block doesn't lie about the failure mode.
        return None
    except json.JSONDecodeError as e:
        return {
            "lineno": e.lineno,
            "colno": e.colno,
            "msg": e.msg,
            "pos": e.pos,
        }


def parse_json_array(raw: str):
    """Parse a JSON array from raw LLM output (4-step chain)."""
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    try:
        repaired = repair_json(cleaned, return_objects=True)
        if isinstance(repaired, list):
            logger.info("json_repaired: method=full_text target=array")
            return repaired
    except Exception:
        pass
    fragment = _extract_by_brackets(cleaned, "[", "]")
    if fragment:
        try:
            result = json.loads(fragment)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
        try:
            repaired = repair_json(fragment, return_objects=True)
            if isinstance(repaired, list):
                logger.info("json_repaired: method=bracket_extract target=array")
                return repaired
        except Exception:
            pass
    return None
