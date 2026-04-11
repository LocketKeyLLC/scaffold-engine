"""
Auto-fix agent: orchestrates 6-pass repair pipeline.
Passes 1-4: deterministic (core.py)
Pass 5: LLM URL selection
Pass 6: LLM content patching
"""

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .core import (
    ValidationResult, ErrorType, apply_deterministic_fixes,
    validate, parse_toon_sections, URL_PATTERN, EXPECTED_FIELDS
)
from .llm_client import (
    LLMConfig, call_llm, SYSTEM_FIX,
    build_url_selection_prompt, build_content_patch_prompt
)

logger = logging.getLogger(__name__)


@dataclass
class FixReport:
    original_valid: bool
    final_valid: bool
    deterministic_fixes: list[str] = field(default_factory=list)
    llm_fixes: list[str] = field(default_factory=list)
    failed_fixes: list[str] = field(default_factory=list)
    passes_run: int = 0

    @property
    def all_fixes(self) -> list[str]:
        return self.deterministic_fixes + self.llm_fixes

    def summary(self) -> str:
        status = "FIXED" if self.final_valid else "PARTIAL"
        lines = [f"[{status}] {len(self.all_fixes)} fixes applied in {self.passes_run} passes"]
        for f in self.deterministic_fixes:
            lines.append(f"  [DETERMINISTIC] {f}")
        for f in self.llm_fixes:
            lines.append(f"  [LLM] {f}")
        for f in self.failed_fixes:
            lines.append(f"  [FAILED] {f}")
        return "\n".join(lines)


def run_fix_pipeline(
    content: str,
    llm_config: Optional[LLMConfig] = None,
    fact_check_issues: Optional[dict] = None,
    max_passes: int = 3,
) -> tuple[str, FixReport]:
    """
    Run the full 6-pass fix pipeline.

    Args:
        content: Raw TOON file content
        llm_config: Ollama config (defaults to env vars)
        fact_check_issues: Dict of {line_num: issue_description} from fact-checker
        max_passes: Max fix iterations (retry counter / loop prevention layer 5)

    Returns:
        (fixed_content, fix_report)
    """
    if llm_config is None:
        llm_config = LLMConfig.from_env()

    # Initial validation
    initial_result = validate(content)
    report = FixReport(original_valid=initial_result.valid, final_valid=False)

    if initial_result.valid and not fact_check_issues:
        report.final_valid = True
        return content, report

    for pass_num in range(1, max_passes + 1):
        report.passes_run = pass_num
        logger.info(f"Fix pass {pass_num}/{max_passes}")

        # Passes 1-4: Deterministic
        content, det_fixes = apply_deterministic_fixes(content)
        report.deterministic_fixes.extend(det_fixes)

        # Re-validate after deterministic fixes
        result = validate(content)

        # Pass 5: LLM URL selection for multi-URL errors
        url_errors = [e for e in result.errors if e.error_type == ErrorType.MULTI_URL]
        if url_errors:
            content = _fix_multi_urls(content, url_errors, llm_config, report)

        # Pass 6: LLM content patching for fact-check failures
        if fact_check_issues:
            content = _fix_content(content, fact_check_issues, llm_config, report)
            fact_check_issues = None  # Only attempt once per pipeline run

        # Final validation
        result = validate(content)
        if result.valid:
            report.final_valid = True
            logger.info(f"File fixed after {pass_num} pass(es)")
            break

        remaining = [e for e in result.errors if e.error_type not in (ErrorType.MISSING_URL,)]
        if not remaining:
            break

        logger.info(f"Pass {pass_num} complete, {len(remaining)} errors remain")

    if not report.final_valid:
        result = validate(content)
        for e in result.errors:
            report.failed_fixes.append(f"Line {e.line}: {e.message}")

    return content, report


def _fix_multi_urls(
    content: str, errors: list, llm_config: LLMConfig, report: FixReport
) -> str:
    """Pass 5: Use LLM to select best URL from multi-URL source fields."""
    sections = parse_toon_sections(content)
    lines = content.split("\n")

    # Find source + topic + content field indices
    try:
        source_idx = sections["declared_fields"].index("source")
        topic_idx = sections["declared_fields"].index("topic")
        content_idx = sections["declared_fields"].index("content")
    except (ValueError, IndexError):
        source_idx, topic_idx, content_idx = 4, 1, 2

    for err in errors:
        line_idx = err.line - 1
        if line_idx >= len(lines):
            continue

        try:
            reader = csv.reader(io.StringIO(lines[line_idx]))
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
        selected = call_llm(prompt, system=SYSTEM_FIX, config=llm_config)

        if selected and selected.startswith("http"):
            # Clean — take only the URL, no surrounding text
            selected = selected.strip().split()[0].rstrip(".,;)")
            row[source_idx] = selected
            # Rebuild CSV line
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(row)
            lines[line_idx] = buf.getvalue().strip()
            report.llm_fixes.append(
                f"Line {err.line}: Selected URL '{selected}' from {len(urls)} candidates"
            )
        else:
            # Fallback: pick first URL deterministically
            row[source_idx] = urls[0]
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(row)
            lines[line_idx] = buf.getvalue().strip()
            report.deterministic_fixes.append(
                f"Line {err.line}: LLM unavailable, fell back to first URL"
            )

    return "\n".join(lines)


def _fix_content(
    content: str, issues: dict, llm_config: LLMConfig, report: FixReport
) -> str:
    """Pass 6: Surgical LLM patches on entries that failed fact-checking."""
    sections = parse_toon_sections(content)
    lines = content.split("\n")

    try:
        topic_idx = sections["declared_fields"].index("topic")
        content_idx = sections["declared_fields"].index("content")
    except (ValueError, IndexError):
        topic_idx, content_idx = 1, 2

    for line_num_str, issue_desc in issues.items():
        line_idx = int(line_num_str) - 1
        if line_idx >= len(lines):
            continue

        try:
            reader = csv.reader(io.StringIO(lines[line_idx]))
            row = next(reader)
        except (csv.Error, StopIteration):
            report.failed_fixes.append(f"Line {line_num_str}: CSV parse error, cannot patch")
            continue

        if len(row) <= content_idx:
            continue

        topic = row[topic_idx] if len(row) > topic_idx else "unknown"
        old_content = row[content_idx]

        prompt = build_content_patch_prompt(topic, old_content, issue_desc)
        patched = call_llm(prompt, system=SYSTEM_FIX, config=llm_config)

        if patched:
            # Sanitize LLM output: no escaped quotes, no leading/trailing quotes
            patched = patched.strip().strip('"').replace('\\"', '""')
            row[content_idx] = patched
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(row)
            lines[line_idx] = buf.getvalue().strip()
            report.llm_fixes.append(f"Line {line_num_str}: Patched content for '{topic}'")
        else:
            report.failed_fixes.append(
                f"Line {line_num_str}: LLM failed to patch content for '{topic}'"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public wrappers for CLI commands (fix-urls, fix-content)
# ---------------------------------------------------------------------------

def fix_urls(content: str, config: Optional[LLMConfig] = None) -> tuple[str, list[str]]:
    """
    Public wrapper for Pass 5: LLM URL selection for multi-URL source fields.

    Returns:
        (fixed_content, list_of_fix_descriptions)
    """
    if config is None:
        config = LLMConfig.from_env()

    result = validate(content)
    url_errors = [e for e in result.errors if e.error_type == ErrorType.MULTI_URL]
    if not url_errors:
        return content, []

    report = FixReport(original_valid=result.valid, final_valid=False)
    fixed = _fix_multi_urls(content, url_errors, config, report)
    return fixed, report.llm_fixes + report.deterministic_fixes


def fix_content(
    content: str, config: Optional[LLMConfig] = None, threshold: float = 0.80
) -> tuple[str, list[str]]:
    """
    Public wrapper for Pass 6: LLM content patching via fact-check.

    Runs a per-entry fact-check using the LLM. Entries scoring below
    *threshold* are patched automatically.

    Returns:
        (fixed_content, list_of_fix_descriptions)
    """
    if config is None:
        config = LLMConfig.from_env()

    sections = parse_toon_sections(content)
    if not sections.get("data_lines"):
        return content, []

    try:
        topic_idx = sections["declared_fields"].index("topic")
        content_idx = sections["declared_fields"].index("content")
    except (ValueError, IndexError):
        # Standard TOON field order: id(0), topic(1), content(2), ...
        topic_idx, content_idx = 1, 2
    fact_issues: dict[str, str] = {}
    lines = content.split("\n")

    for line_num, line_text in sections["data_lines"]:
        line_idx = line_num
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
        entry_content = row[content_idx]

        # Sanitise inputs before building the LLM prompt
        safe_topic = topic[:200].replace('"', "'")
        safe_content = entry_content[:500].replace('"', "'")

        # Quick LLM fact-check
        check_prompt = (
            f"Rate the factual accuracy of this knowledge base entry on a scale "
            f"of 0.0 to 1.0. Topic: '{safe_topic}'. Content: \"{safe_content}\"\n\n"
            f"Respond with ONLY a JSON object: "
            f'{{\"confidence\": 0.0, \"issues\": \"description or empty\"}}'
        )
        response = call_llm(check_prompt, system=SYSTEM_FIX, config=config)
        if response:
            try:
                data = json.loads(response)
                confidence = float(data.get("confidence", 1.0))
                issues = data.get("issues", "")
                if confidence < threshold and issues:
                    fact_issues[str(line_num + 1)] = issues
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    if not fact_issues:
        return content, []

    report = FixReport(original_valid=False, final_valid=False)
    fixed = _fix_content(content, fact_issues, config, report)
    return fixed, report.llm_fixes
