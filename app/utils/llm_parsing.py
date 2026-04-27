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
    """Pull the outermost bracket-delimited substring from text."""
    start = text.find(open_b)
    end = text.rfind(close_b)
    if start != -1 and end > start:
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
