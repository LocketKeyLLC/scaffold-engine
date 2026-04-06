"""Shared LLM output parsing utilities."""

import re

def strip_think_tags(text: str) -> str:
    """Remove <think>/<thinking> reasoning blocks from LLM output."""
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', cleaned, flags=re.DOTALL)
    # Handle unclosed tags (model cut off mid-think)
    cleaned = re.sub(r'<think>.*', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<thinking>.*', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()
