"""Shared LLM output parsing utilities."""

import json
import logging
import re
from typing import Optional

from json_repair import repair_json

logger = logging.getLogger("scaffold.parsing")


def strip_think_tags(text: str) -> str:
    """Remove <think>/<thinking> reasoning blocks from LLM output."""
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<think>.*', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<thinking>.*', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown fences from text."""
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text


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
    cleaned = _strip_markdown_fences(cleaned.strip())

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
    cleaned = _strip_markdown_fences(cleaned.strip())

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
