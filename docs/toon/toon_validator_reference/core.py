"""
Pure validation and deterministic fix logic for TOON files.
No I/O, no HTTP, no CI dependencies. Accepts/returns strings and dicts.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class ErrorType(Enum):
    ENCODING = "encoding"
    STRUCTURE = "structure"
    QUOTE = "quote"
    FIELD_COUNT = "field_count"
    MULTI_URL = "multi_url"
    MISSING_URL = "missing_url"
    ROW_COUNT = "row_count"
    CONTENT = "content"

@dataclass
class ValidationError:
    error_type: ErrorType
    line: int
    message: str
    raw_line: str = ""
    fixable: bool = True

@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def add_error(self, error_type: ErrorType, line: int, message: str,
                  raw_line: str = "", fixable: bool = True):
        self.errors.append(ValidationError(error_type, line, message, raw_line, fixable))
        self.valid = False

    def add_warning(self, msg: str):
        self.warnings.append(msg)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_FIELDS = 7  # id, topic, content, tags, source, verified, last_verified
META_KEYS = {"schema_v", "source", "timestamp", "content_type", "category", "description"}
URL_PATTERN = re.compile(r"https?://[^\s,\"]+")
KNOWLEDGE_HEADER_PATTERN = re.compile(
    r"^knowledge\[(\d+)\]\{?\{([^}]+)\}?\}:$"
)

def preprocess_content(content: str) -> str:
    """Restore newlines when content arrives as single-line from chat input."""
    lines = content.strip().split('\n')
    if len(lines) > 3:
        return content
    result = content
    # Split knowledge header onto its own line (single or double braces)
    result = re.sub(r'(knowledge\[\d+\]\{?\{[^}]+\}?\}:)\s*', r'\1\n', result)
    # Split meta: onto its own line
    result = re.sub(r'\s+(meta:)\s+', r'\n\1\n', result)
    # Split known meta keys onto their own lines (only match keys NOT preceded by :// to avoid URLs)
    for key in ['schema_v', 'source', 'timestamp', 'content_type',
                'description', 'category', 'format']:
        result = re.sub(rf'(?<!/)(?<!/)\s({key}:)\s', rf'\n  \1 ', result)
    # Split data rows (start with a quote or digit after whitespace)
    result = re.sub(r'\s("[\w-]+?",)', r'\n\1', result)
    result = re.sub(r'\s(\d+,[\w-]+?,)', r'\n\1', result)
    return result.strip() + '\n'

# ---------------------------------------------------------------------------
# Pass 1: Encoding normalization
# ---------------------------------------------------------------------------

def normalize_encoding(content: str) -> str:
    """Strip BOM, normalize line endings to LF."""
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content

# ---------------------------------------------------------------------------
# Pass 2: Structural pre-cleaning
# ---------------------------------------------------------------------------

def clean_structure(content: str) -> tuple[str, list[str]]:
    """Remove blank lines inside array blocks, strip trailing whitespace.
    Returns (cleaned_content, list_of_fixes_applied).
    """
    fixes = []
    lines = content.split("\n")
    cleaned = []
    in_array = False

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        # Detect knowledge header
        if KNOWLEDGE_HEADER_PATTERN.match(stripped):
            in_array = True
            cleaned.append(stripped)
            continue

        # Detect end of array (next meta block or EOF)
        if in_array and stripped.startswith("meta:"):
            in_array = False

        # Remove blank lines inside array blocks
        if in_array and stripped == "":
            fixes.append(f"Removed blank line {i + 1} inside array block")
            continue

        cleaned.append(stripped)

    # Remove trailing blank lines
    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    return "\n".join(cleaned) + "\n", fixes

# ---------------------------------------------------------------------------
# Pass 3: Quote repair
# ---------------------------------------------------------------------------

def normalize_escaped_quotes(content: str) -> tuple[str, int]:
    """Convert backslash-escaped quotes to CSV-standard doubled quotes.
    Returns (fixed_content, count_of_replacements).
    """
    count = content.count('\\"')
    if count > 0:
        content = content.replace('\\"', '""')
    return content, count

def fix_unbalanced_quotes(line: str) -> tuple[str, bool]:
    """Detect and fix unbalanced quotes in a data row.
    Returns (fixed_line, was_fixed).
    """
    # Count quotes outside of doubled-quote pairs
    in_quote = False
    quote_count = 0
    i = 0
    while i < len(line):
        if line[i] == '"':
            if i + 1 < len(line) and line[i + 1] == '"':
                i += 2  # Skip doubled quote
                continue
            quote_count += 1
            in_quote = not in_quote
        i += 1

    if quote_count % 2 != 0:
        # Unbalanced — attempt to close the last open quote before the last comma
        last_comma = line.rfind(",")
        if last_comma > 0 and in_quote:
            line = line[:last_comma] + '"' + line[last_comma:]
            return line, True

    return line, False

# ---------------------------------------------------------------------------
# Pass 4: Field count validation + row count header
# ---------------------------------------------------------------------------

def parse_toon_sections(content: str) -> dict:
    """Parse TOON file into meta dict and data rows.
    Handles both orderings: header → meta → data  OR  meta → header → data.
    """
    lines = content.strip().split("\n")
    sections = {"meta": {}, "header_line": -1, "declared_count": 0,
                "declared_fields": [], "data_lines": [], "data_start": -1,
                "raw_lines": lines}

    i = 0

    # Phase 1: Find the knowledge header (may be at line 0 or after a meta block)
    # First, try meta-first ordering
    if i < len(lines) and lines[i].strip() == "meta:":
        i += 1
        while i < len(lines) and not lines[i].strip().startswith("knowledge["):
            kv = lines[i].strip()
            if ":" in kv:
                key, val = kv.split(":", 1)
                sections["meta"][key.strip()] = val.strip()
            i += 1

    # Look for knowledge header at current position
    if i < len(lines):
        match = KNOWLEDGE_HEADER_PATTERN.match(lines[i].strip())
        if match:
            sections["header_line"] = i
            sections["declared_count"] = int(match.group(1))
            sections["declared_fields"] = [f.strip() for f in match.group(2).split(",")]
            i += 1

    # Phase 2: If header found, check for meta block AFTER header (TOON standard order)
    if sections["header_line"] >= 0 and not sections["meta"]:
        if i < len(lines) and lines[i].strip() == "meta:":
            i += 1
            while i < len(lines):
                kv = lines[i].strip()
                # Stop at data rows (start with quote) or blank lines before data
                if kv.startswith('"') or kv == "":
                    break
                if ":" in kv:
                    key, val = kv.split(":", 1)
                    sections["meta"][key.strip()] = val.strip()
                i += 1

    # Phase 3: Parse data rows (CSV lines — start with quote or digit)
    sections["data_start"] = i
    while i < len(lines):
        line = lines[i].strip()
        if line and (line[0] == '"' or line[0].isdigit()):
            sections["data_lines"].append((i, line))
        i += 1

    return sections

def validate_field_counts(sections: dict) -> list[ValidationError]:
    """Check each data row has exactly EXPECTED_FIELDS fields."""
    errors = []
    expected = len(sections["declared_fields"]) if sections["declared_fields"] else EXPECTED_FIELDS

    for line_num, line in sections["data_lines"]:
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
            if len(row) != expected:
                errors.append(ValidationError(
                    ErrorType.FIELD_COUNT, line_num + 1,
                    f"Row has {len(row)} fields, expected {expected}",
                    raw_line=line
                ))
        except csv.Error as e:
            errors.append(ValidationError(
                ErrorType.QUOTE, line_num + 1,
                f"CSV parse error: {e}",
                raw_line=line
            ))
    return errors

def validate_row_count(sections: dict) -> Optional[ValidationError]:
    """Check declared [N] matches actual data row count."""
    actual = len(sections["data_lines"])
    declared = sections["declared_count"]
    if declared > 0 and actual != declared:
        return ValidationError(
            ErrorType.ROW_COUNT, sections["header_line"] + 1,
            f"Header declares [{declared}] but found {actual} rows"
        )
    return None

def fix_row_count_header(content: str, sections: dict) -> str:
    """Update [N] in knowledge header to match actual row count."""
    actual = len(sections["data_lines"])
    lines = content.split("\n")
    if sections["header_line"] >= 0:
        old_line = lines[sections["header_line"]]
        new_line = re.sub(r"\[\d+\]", f"[{actual}]", old_line)
        lines[sections["header_line"]] = new_line
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Pass 5: Source URL detection (detection only — fix requires LLM)
# ---------------------------------------------------------------------------

def detect_url_issues(sections: dict) -> list[ValidationError]:
    """Find rows with multi-URL or missing-URL source fields."""
    errors = []
    expected = len(sections["declared_fields"]) if sections["declared_fields"] else EXPECTED_FIELDS

    # Find source field index
    try:
        source_idx = sections["declared_fields"].index("source")
    except (ValueError, AttributeError):
        source_idx = 4  # Default position in 7-field schema

    for line_num, line in sections["data_lines"]:
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
            if len(row) <= source_idx:
                continue
            source_val = row[source_idx].strip()
            urls = URL_PATTERN.findall(source_val)

            if len(urls) == 0:
                # Placeholder values are warnings, not errors
                KNOWN_PLACEHOLDERS = {"pending-verification", "pending", "none", "n/a", "tbd"}
                if source_val.lower() in KNOWN_PLACEHOLDERS:
                    # Collected separately — caller can add as warning
                    pass
                else:
                    errors.append(ValidationError(
                        ErrorType.MISSING_URL, line_num + 1,
                        f"Source field has no URL: '{source_val}'",
                        raw_line=line, fixable=False
                    ))
            elif len(urls) > 1:
                errors.append(ValidationError(
                    ErrorType.MULTI_URL, line_num + 1,
                    f"Source field has {len(urls)} URLs: {urls}",
                    raw_line=line
                ))
        except (csv.Error, StopIteration):
            pass

    return errors

# ---------------------------------------------------------------------------
# Full validation pipeline
# ---------------------------------------------------------------------------

def validate(content: str) -> ValidationResult:
    """Run all validation checks on TOON content. Returns ValidationResult."""
    result = ValidationResult(valid=True)
    content = preprocess_content(content)

    # Parse
    sections = parse_toon_sections(content)

    # Check meta
    missing_meta = META_KEYS - set(sections["meta"].keys())
    if missing_meta:
        result.add_warning(f"Missing meta keys: {missing_meta}")

    # Check knowledge header exists
    if sections["header_line"] < 0:
        result.add_error(ErrorType.STRUCTURE, 0, "No knowledge[N]{...}: header found")
        return result

    # Check field declarations
    if len(sections["declared_fields"]) != EXPECTED_FIELDS:
        result.add_error(
            ErrorType.STRUCTURE, sections["header_line"] + 1,
            f"Header declares {len(sections['declared_fields'])} fields, expected {EXPECTED_FIELDS}"
        )

    # Row count
    rc_error = validate_row_count(sections)
    if rc_error:
        result.add_error(rc_error.error_type, rc_error.line, rc_error.message)

    # Field counts per row
    for err in validate_field_counts(sections):
        result.add_error(err.error_type, err.line, err.message, err.raw_line)

    # URL issues
    for err in detect_url_issues(sections):
        result.add_error(err.error_type, err.line, err.message, err.raw_line, err.fixable)

    return result

# ---------------------------------------------------------------------------
# Deterministic fix pipeline (passes 1-4)
# ---------------------------------------------------------------------------

def apply_deterministic_fixes(content: str) -> tuple[str, list[str]]:
    """Run passes 1-4 deterministic fixes. Returns (fixed_content, fixes_applied)."""
    all_fixes = []
    content = preprocess_content(content)

    # Pass 1: Encoding
    content = normalize_encoding(content)

    # Pass 2: Structure
    content, struct_fixes = clean_structure(content)
    all_fixes.extend(struct_fixes)

    # Pass 3: Quotes
    content, quote_count = normalize_escaped_quotes(content)
    if quote_count > 0:
        all_fixes.append(f"Normalized {quote_count} escaped quotes (\\\" → \"\")")

    # Fix unbalanced quotes per data row
    sections = parse_toon_sections(content)
    lines = content.split("\n")
    for line_num, line in sections["data_lines"]:
        fixed_line, was_fixed = fix_unbalanced_quotes(line)
        if was_fixed:
            lines[line_num] = fixed_line
            all_fixes.append(f"Fixed unbalanced quote on line {line_num + 1}")
    content = "\n".join(lines)

    # Pass 4: Row count header
    sections = parse_toon_sections(content)
    rc_error = validate_row_count(sections)
    if rc_error:
        content = fix_row_count_header(content, sections)
        all_fixes.append(f"Updated row count header: [{sections['declared_count']}] → [{len(sections['data_lines'])}]")

    return content, all_fixes