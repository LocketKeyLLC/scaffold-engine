#!/usr/bin/env python3
"""Lint db/migrations/*.sql for the single-statement rule (§17.140).

The migration runner (app/migrations.py) executes each file through asyncpg's
prepared-statement path, which rejects multiple semicolon-separated commands
("cannot insert multiple commands into a prepared statement"). Each migration
must therefore be exactly ONE top-level statement: a single DDL command, a
single comma-separated ALTER, or a single ``DO $tag$ … $tag$`` block.

A multi-statement file fails *silently at lifespan startup* (the runner logs
``migration_failed`` and returns an error dict, but the file already passed
code review) — so this is a pure-text static gate (no DB, no third-party deps)
wired into ``make ci-tier-0``. It runs at pre-push and in the ci.yml smoke job,
catching the mistake before the file is ever shipped to a booting orchestrator.

Parser notes: line/block comments, single-quoted string literals (with the
``''`` escape), and dollar-quoted bodies (``$$ … $$`` / ``$tag$ … $tag$``) are
skipped so their inner semicolons don't count. Only top-level ``;`` terminators
are counted; a trailing terminator + trailing whitespace/comments add no
phantom statement.

Ratchet, not retroactive sweep: migrations ``002``–``033`` predate this rule
(several are genuinely multi-statement) and are folded into the ``db/init.sql``
baseline (§17.94 — "post-migration-033 state"), so they're applied on a fresh
bootstrap only via the legacy reapply path, never authored anew. They are
grandfathered by the ``BASELINE_BAKED_MAX`` cutoff below; every NEW migration
(``> 033``) must obey the single-statement rule. This freezes the existing debt
and blocks new violations without failing CI on committed history.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

# db/init.sql is the baked baseline through migration 033 (§17.94). Files at or
# below this number are exempt — see the module docstring. Do NOT raise this to
# silence a new failing migration; fix the migration instead.
BASELINE_BAKED_MAX = 33

_NUM_PREFIX = re.compile(r"^(\d+)_")


def _dollar_tag_end(sql: str, i: int) -> int | None:
    """If ``sql[i]`` opens a dollar-quote tag (``$`` or ``$tag$``), return the
    index just past its closing ``$``; otherwise None (e.g. a bare ``$1``)."""
    # sql[i] == '$'
    j = i + 1
    while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    if j < len(sql) and sql[j] == "$":
        return j + 1
    return None


def count_statements(sql: str) -> int:
    """Return the number of top-level SQL statements in *sql*."""
    n = len(sql)
    i = 0
    statements = 0
    pending = False  # non-whitespace seen since the last terminator
    while i < n:
        c = sql[i]
        # line comment  -- … \n
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        # block comment  /* … */
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        # single-quoted string literal  '…'  ('' is an escaped quote)
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            pending = True
            continue
        # dollar-quoted body  $tag$ … $tag$
        if c == "$":
            tag_end = _dollar_tag_end(sql, i)
            if tag_end is not None:
                tag = sql[i:tag_end]
                close = sql.find(tag, tag_end)
                i = n if close == -1 else close + len(tag)
                pending = True
                continue
        # statement terminator
        if c == ";":
            if pending:
                statements += 1
                pending = False
            i += 1
            continue
        if not c.isspace():
            pending = True
        i += 1
    if pending:  # trailing statement with no terminator still counts
        statements += 1
    return statements


def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"lint-migrations: directory not found: {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    failures: list[tuple[str, int]] = []
    checked = 0
    for path in files:
        m = _NUM_PREFIX.match(path.name)
        if m and int(m.group(1)) <= BASELINE_BAKED_MAX:
            continue  # grandfathered: folded into init.sql baseline (§17.94)
        checked += 1
        count = count_statements(path.read_text(encoding="utf-8"))
        if count > 1:
            failures.append((path.name, count))

    if failures:
        print(
            "✗ lint-migrations: multi-statement migration(s) found (§17.140 — "
            "asyncpg rejects multiple commands in one prepared statement):",
            file=sys.stderr,
        )
        for name, count in failures:
            print(
                f"    {name}: {count} top-level statements — collapse into a single "
                "comma-separated ALTER or a DO $$ … $$ block.",
                file=sys.stderr,
            )
        return 1

    print(
        f"✓ lint-migrations: {checked} post-baseline migration(s) each a single "
        f"statement ({len(files) - checked} grandfathered ≤ {BASELINE_BAKED_MAX:03d})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
