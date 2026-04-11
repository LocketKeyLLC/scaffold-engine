"""
Ollama LLM client with retry logic for auto-fix operations.
Supports both local (localhost:11434) and cloud (ollama.com/api/chat) endpoints.
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    model: str = "qwen3-vl:235b-instruct-cloud"
    endpoint: str = "https://ollama.com/api/chat"
    api_key: str = ""
    max_retries: int = 3
    base_delay: float = 2.0  # Exponential backoff: 2s, 4s, 8s
    timeout: int = 180
    temperature: float = 0.2  # Low temp for deterministic fixes
    num_predict: Optional[int] = None  # Max tokens to generate (None = model default)
    keep_alive: Optional[int] = None  # Seconds to keep model loaded (-1 = forever)
    think: Optional[bool] = None  # Disable chain-of-thought for reasoning models

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            model=os.getenv("AUTOFIX_MODEL", "qwen3-vl:235b-instruct-cloud"),
            endpoint=os.getenv("AUTOFIX_LLM_ENDPOINT", "https://ollama.com/api/chat"),
            api_key=os.getenv("OLLAMA_API_KEY", ""),
            max_retries=int(os.getenv("AUTOFIX_MAX_RETRIES", "3")),
            timeout=int(os.getenv("AUTOFIX_TIMEOUT", "180")),
        )


def _build_headers(config: LLMConfig) -> dict:
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _normalize_endpoint(endpoint: str) -> str:
    """Ensure the endpoint URL includes the /api/chat path for Ollama."""
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/api/chat"):
        endpoint += "/api/chat"
    return endpoint


def call_llm(prompt: str, system: str = "", config: Optional[LLMConfig] = None) -> Optional[str]:
    """Call Ollama with exponential backoff retry. Returns response text or None."""
    if requests is None:
        raise ImportError("requests library required: pip install requests")

    if config is None:
        config = LLMConfig.from_env()

    endpoint = _normalize_endpoint(config.endpoint)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Build options dict with temperature + optional num_predict
    options = {"temperature": config.temperature}
    if config.num_predict is not None:
        options["num_predict"] = config.num_predict

    payload = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "options": options,
    }

    # Keep model loaded in memory between calls
    if config.keep_alive is not None:
        payload["keep_alive"] = config.keep_alive

    # Disable chain-of-thought for reasoning models
    if config.think is not None:
        payload["think"] = config.think

    for attempt in range(1, config.max_retries + 1):
        try:
            logger.info(f"LLM call attempt {attempt}/{config.max_retries} → {config.model}")
            resp = requests.post(
                endpoint,
                headers=_build_headers(config),
                json=payload,
                timeout=config.timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                # Strip <think> blocks (phi4-mini-reasoning compat)
                content = _strip_think_blocks(content)
                return content.strip()

            if resp.status_code in (429, 500, 502, 503):
                delay = config.base_delay * (2 ** (attempt - 1))
                logger.warning(f"HTTP {resp.status_code}, retrying in {delay}s...")
                time.sleep(delay)
                continue

            logger.error(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        except requests.exceptions.Timeout:
            delay = config.base_delay * (2 ** (attempt - 1))
            logger.warning(f"Timeout on attempt {attempt}, retrying in {delay}s...")
            time.sleep(delay)
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

    logger.error(f"All {config.max_retries} attempts failed for {config.model}")
    return None


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Prompt builders for fix passes 5 and 6
# ---------------------------------------------------------------------------

SYSTEM_FIX = (
    "You are a TOON file repair agent. TOON is a CSV-like format with 7 fields: "
    "id,topic,content,tags,source,verified,last_verified. "
    "Respond ONLY with the corrected value. No explanation, no markdown, no quotes around your answer."
)


def build_url_selection_prompt(urls: list[str], topic: str, content_snippet: str) -> str:
    """Build prompt for Pass 5: select most authoritative source URL."""
    return (
        f"A TOON entry about '{topic}' has multiple source URLs. "
        f"Content snippet: \"{content_snippet[:200]}\"\n\n"
        f"URLs:\n" + "\n".join(f"  {i+1}. {u}" for i, u in enumerate(urls)) + "\n\n"
        f"Which single URL is the most authoritative primary source for this content? "
        f"Respond with ONLY the URL, nothing else."
    )


def build_content_patch_prompt(topic: str, content: str, issues: str) -> str:
    """Build prompt for Pass 6: surgical content patch for fact-check failures."""
    return (
        f"A TOON knowledge base entry about '{topic}' failed fact-checking.\n\n"
        f"Current content:\n\"{content}\"\n\n"
        f"Issues identified:\n{issues}\n\n"
        f"Rewrite ONLY the problematic portions to fix factual accuracy. "
        f"Keep the same style, length, and structure. Do not use backslash-escaped quotes. "
        f"Do not add multiple URLs. Respond with ONLY the corrected content field value."
    )


def llm_fix_urls(content: str, config: Optional["LLMConfig"] = None) -> tuple[str, list[str]]:
    """Pass 5: Find multi-URL source fields and use LLM to select the best one.

    Returns (fixed_content, list_of_fix_descriptions).
    """
    import csv
    import io

    if config is None:
        config = LLMConfig.from_env()

    # Lazy import to avoid circular dependency
    from .core import parse_toon_sections, URL_PATTERN

    sections = parse_toon_sections(content)
    lines = content.split("\n")
    fixes: list[str] = []

    try:
        source_idx = sections["declared_fields"].index("source")
        topic_idx = sections["declared_fields"].index("topic")
        content_idx = sections["declared_fields"].index("content")
    except (ValueError, KeyError):
        source_idx, topic_idx, content_idx = 4, 1, 2

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#") or line_stripped.startswith("@"):
            continue
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
        except (csv.Error, StopIteration):
            continue

        if len(row) <= source_idx:
            continue

        source_val = row[source_idx]
        urls = URL_PATTERN.findall(source_val)
        if len(urls) <= 1:
            continue

        topic = row[topic_idx] if len(row) > topic_idx else "unknown"
        snippet = row[content_idx] if len(row) > content_idx else ""

        prompt = build_url_selection_prompt(urls, topic, snippet)
        selected = call_llm(prompt, system=SYSTEM_FIX, config=config)

        if selected and selected.startswith("http"):
            selected = selected.strip().split()[0].rstrip(".,;)")
            row[source_idx] = selected
        else:
            row[source_idx] = urls[0]  # Deterministic fallback: first URL

        buf = io.StringIO()
        csv.writer(buf).writerow(row)
        lines[i] = buf.getvalue().strip()
        fixes.append(f"Line {i + 1}: Reduced {len(urls)} URLs → '{row[source_idx]}'")

    return "\n".join(lines), fixes


def llm_fix_content(
    content: str,
    config: Optional["LLMConfig"] = None,
    threshold: float = 0.80,
    fact_check_issues: Optional[dict] = None,
) -> tuple[str, list[str]]:
    """Pass 6: Surgical LLM patch for entries with known fact-check issues.

    ``fact_check_issues`` maps str(line_num) → issue_description.
    When called without pre-computed issues (e.g. standalone CLI), returns
    unchanged content so the call succeeds gracefully.

    Returns (fixed_content, list_of_fix_descriptions).
    """
    import csv
    import io

    if not fact_check_issues:
        return content, []

    if config is None:
        config = LLMConfig.from_env()

    from .core import parse_toon_sections

    sections = parse_toon_sections(content)
    lines = content.split("\n")
    fixes: list[str] = []

    try:
        topic_idx = sections["declared_fields"].index("topic")
        content_idx = sections["declared_fields"].index("content")
    except (ValueError, KeyError):
        topic_idx, content_idx = 1, 2

    for line_num_str, issue_desc in fact_check_issues.items():
        line_idx = int(line_num_str) - 1
        if line_idx >= len(lines):
            continue
        try:
            reader = csv.reader(io.StringIO(lines[line_idx]))
            row = next(reader)
        except (csv.Error, StopIteration):
            continue

        if len(row) <= content_idx:
            continue

        topic = row[topic_idx] if len(row) > topic_idx else "unknown"
        old_content = row[content_idx]
        prompt = build_content_patch_prompt(topic, old_content, issue_desc)
        patched = call_llm(prompt, system=SYSTEM_FIX, config=config)

        if patched:
            patched = patched.strip().strip('"').replace('\\"', '""')
            row[content_idx] = patched
            buf = io.StringIO()
            csv.writer(buf).writerow(row)
            lines[line_idx] = buf.getvalue().strip()
            fixes.append(f"Line {line_num_str}: Patched content for '{topic}'")

    return "\n".join(lines), fixes


def build_contradiction_prompt(new_entry: str, existing_entries: list[str]) -> str:
    """Build prompt for contradiction detection gate."""
    existing_text = "\n---\n".join(existing_entries)
    return (
        f"You are checking a knowledge base for contradictions.\n\n"
        f"NEW ENTRY:\n{new_entry}\n\n"
        f"EXISTING ENTRIES:\n{existing_text}\n\n"
        f"Pay close attention to contradictions. Do any facts in the new entry "
        f"contradict facts in the existing entries? "
        f"Respond in this exact JSON format:\n"
        f'{{"has_contradiction": true/false, "details": "explanation or empty string"}}'
    )
