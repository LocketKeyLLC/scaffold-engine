"""
CLI entry point for toon_validator.
Usage: python -m toon_validator.cli <command> [args]

Commands:
  validate <file>                        Validate a TOON file (exits 1 on failure)
  fix <file>                             Run deterministic fixes (passes 1-4), write in-place
  fix-urls <file> --model --endpoint     Pass 5: LLM URL repair
  fix-content <file> --model --endpoint  Pass 6: LLM content repair
  gate-freshness <dir> --max-age-days    Gate F: flag stale entries
  gate-urls <dir>                        Gate E: validate source URLs
  gate-contradictions <dir> --model --endpoint  Gate D: contradiction detection
"""

import argparse
import csv
import glob
import io
import os
import sys
from pathlib import Path

from .core import validate, apply_deterministic_fixes, parse_toon_sections

def cmd_validate(args):
    """Validate a single TOON file. Exit 1 if invalid."""
    content = Path(args.file).read_text(encoding="utf-8")
    result = validate(content)

    if result.warnings:
        for w in result.warnings:
            print(f"  WARN: {w}")

    if result.errors:
        for e in result.errors:
            print(f"  ERROR line {e.line} [{e.error_type.value}]: {e.message}")
        print(f"\n❌ {args.file} — INVALID ({len(result.errors)} error(s))")
        return 1

    print(f"✅ {args.file} — VALID")
    return 0

def cmd_fix(args):
    """Run deterministic fixes (passes 1-4) on a TOON file. Writes in-place."""
    path = Path(args.file)
    content = path.read_text(encoding="utf-8")
    fixed, fixes = apply_deterministic_fixes(content)

    if not fixes:
        print(f"  No deterministic fixes needed for {args.file}")
        return 0

    path.write_text(fixed, encoding="utf-8")
    for f in fixes:
        print(f"  FIX: {f}")
    print(f"  Applied {len(fixes)} fix(es) to {args.file}")
    return 0

def cmd_fix_urls(args):
    """Pass 5: LLM-powered URL repair for multi-URL source fields."""
    try:
        from .llm_client import LLMConfig
        from .fix_agent import fix_urls
    except ImportError:
        print("ERROR: llm_client module not available")
        return 1

    path = Path(args.file)
    content = path.read_text(encoding="utf-8")
    config = LLMConfig(model=args.model, endpoint=args.endpoint)

    fixed, fixes = fix_urls(content, config)
    if not fixes:
        print(f"  No URL fixes needed for {args.file}")
        return 0

    path.write_text(fixed, encoding="utf-8")
    for f in fixes:
        print(f"  FIX: {f}")
    return 0

def cmd_fix_content(args):
    """Pass 6: LLM-powered content repair for low-scoring claims."""
    try:
        from .llm_client import LLMConfig
        from .fix_agent import fix_content
    except ImportError:
        print("ERROR: llm_client module not available")
        return 1

    path = Path(args.file)
    content = path.read_text(encoding="utf-8")
    config = LLMConfig(model=args.model, endpoint=args.endpoint)

    fixed, fixes = fix_content(content, config, threshold=args.threshold)
    if not fixes:
        print(f"  No content fixes needed for {args.file}")
        return 0

    path.write_text(fixed, encoding="utf-8")
    for f in fixes:
        print(f"  FIX: {f}")
    return 0

def cmd_gate_freshness(args):
    """Gate F: Flag entries not modified in N days."""
    try:
        from .gates.freshness import check_freshness
    except ImportError:
        print("ERROR: gates.freshness module not available")
        return 1

    toon_files = glob.glob(os.path.join(args.directory, "**/*.toon"), recursive=True)
    if not toon_files:
        print(f"  No .toon files found in {args.directory}")
        return 0

    failures = 0
    for f in toon_files:
        content = Path(f).read_text(encoding="utf-8")
        result = check_freshness(content, stale_days=args.max_age_days)
        for entry in result.entries:
            if entry.is_stale or entry.is_pending:
                label = "pending" if entry.is_pending else f"{entry.days_old} days old"
                print(f"  STALE: {f} entry {entry.entry_id} — {label}")
                failures += 1

    if failures:
        print(f"\n⚠️  {failures} stale entry/entries found (>{args.max_age_days} days)")
        # Freshness is a warning, not a hard fail
        return 0
    print("✅ All entries within freshness threshold")
    return 0

def cmd_gate_urls(args):
    """Gate E: Validate source URLs via HTTP HEAD."""
    try:
        from .gates.url_validation import check_urls
    except ImportError:
        print("ERROR: gates.url_validation module not available")
        return 1

    toon_files = glob.glob(os.path.join(args.directory, "**/*.toon"), recursive=True)
    failures = 0
    for f in toon_files:
        content = Path(f).read_text(encoding="utf-8")
        result = check_urls(content)
        for r in result.results:
            if not r.reachable:
                print(f"  BROKEN: {f} line {r.line} [{r.topic}] — {r.url} ({r.error or f'HTTP {r.status}'})")
                failures += 1

    if failures:
        print(f"\n❌ {failures} broken URL(s) found")
        return 1
    print("✅ All URLs valid")
    return 0

def cmd_gate_contradictions(args):
    """Gate D: Detect contradictions between entries via LLM."""
    try:
        from .llm_client import LLMConfig
        from .gates.contradiction import check_contradictions
    except ImportError:
        print("ERROR: gates.contradiction or llm_client module not available")
        return 1

    config = LLMConfig(model=args.model, endpoint=args.endpoint)
    toon_files = glob.glob(os.path.join(args.directory, "**/*.toon"), recursive=True)
    failures = 0
    for f in toon_files:
        content = Path(f).read_text(encoding="utf-8")
        sections = parse_toon_sections(content)

        try:
            topic_idx = sections["declared_fields"].index("topic")
            content_idx = sections["declared_fields"].index("content")
        except (ValueError, IndexError):
            topic_idx, content_idx = 1, 2

        for line_num, line in sections["data_lines"]:
            try:
                reader = csv.reader(io.StringIO(line))
                row = next(reader)
            except Exception:
                continue
            if len(row) <= content_idx:
                continue
            topic = row[topic_idx] if len(row) > topic_idx else "unknown"
            entry_content = row[content_idx]
            entry_text = f"[{topic}] {entry_content}"

            result = check_contradictions(entry_text, config)
            if result.has_contradiction:
                print(f"  CONTRADICTION: {f} line {line_num + 1} [{topic}] — {result.details}")
                failures += 1

    if failures:
        print(f"\n❌ {failures} contradiction(s) found")
        return 1
    print("✅ No contradictions detected")
    return 0

def main():
    parser = argparse.ArgumentParser(prog="toon_validator", description="TOON file validator and fixer")
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    p_val = sub.add_parser("validate", help="Validate a TOON file")
    p_val.add_argument("file", help="Path to .toon file")

    # fix (deterministic passes 1-4)
    p_fix = sub.add_parser("fix", help="Run deterministic fixes (passes 1-4)")
    p_fix.add_argument("file", help="Path to .toon file")

    # fix-urls (pass 5)
    p_urls = sub.add_parser("fix-urls", help="LLM URL repair (pass 5)")
    p_urls.add_argument("file", help="Path to .toon file")
    p_urls.add_argument("--model", default="qwen3.5:0.8b", help="Ollama model name")
    p_urls.add_argument("--endpoint", default="http://localhost:11434/api/chat", help="Ollama endpoint")

    # fix-content (pass 6)
    p_content = sub.add_parser("fix-content", help="LLM content repair (pass 6)")
    p_content.add_argument("file", help="Path to .toon file")
    p_content.add_argument("--model", default="qwen3.5:0.8b", help="Ollama model name")
    p_content.add_argument("--endpoint", default="http://localhost:11434/api/chat", help="Ollama endpoint")
    p_content.add_argument("--threshold", type=float, default=0.80, help="Min score threshold")

    # gate-freshness
    p_fresh = sub.add_parser("gate-freshness", help="Check entry freshness")
    p_fresh.add_argument("directory", help="Knowledge directory path")
    p_fresh.add_argument("--max-age-days", type=int, default=90, help="Max age in days")

    # gate-urls
    p_gurls = sub.add_parser("gate-urls", help="Validate source URLs")
    p_gurls.add_argument("directory", help="Knowledge directory path")

    # gate-contradictions
    p_contra = sub.add_parser("gate-contradictions", help="Detect contradictions via LLM")
    p_contra.add_argument("directory", help="Knowledge directory path")
    p_contra.add_argument("--model", default="qwen3.5:0.8b", help="Ollama model name")
    p_contra.add_argument("--endpoint", default="http://localhost:11434/api/chat", help="Ollama endpoint")

    args = parser.parse_args()

    commands = {
        "validate": cmd_validate,
        "fix": cmd_fix,
        "fix-urls": cmd_fix_urls,
        "fix-content": cmd_fix_content,
        "gate-freshness": cmd_gate_freshness,
        "gate-urls": cmd_gate_urls,
        "gate-contradictions": cmd_gate_contradictions,
    }

    sys.exit(commands[args.command](args))

if __name__ == "__main__":
    main()