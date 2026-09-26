"""§17.1117 (Phase 1 ledger U-9) — failures must not render as "nothing to show".

The ledger found 40+ `catch` blocks in the SPA whose body was a comment or
nothing: a failed reconciliation fetch looked like "no plan changes", a failed
job search like "no matches", a failed event-log replay like an empty run.
The four named sites now render their failure. This ratchet counts catch
blocks with NO statement (comments/whitespace only) across app/ui/static and
lets the number only go DOWN — a new swallowed failure must at least log, or
lower the ceiling by fixing another. Static, ci-tier-0.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"
_CATCH = re.compile(r"\bcatch\s*(\([^)]*\))?\s*\{")
# 2026-09-19 §17.1117: 25 → 21 (plan ledger, run replay, palette search, compare search fixed).
CEILING = 20   # §17.1180 — 21 → 20: converting the native dialogs (audit U6) removed a swallowed failure with them.


def empty_catches(src: str) -> list[tuple[int, str]]:
    out = []
    for m in _CATCH.finditer(src):
        i, depth = m.end(), 1
        while i < len(src) and depth:
            c = src[i]
            depth += 1 if c == "{" else -1 if c == "}" else 0
            i += 1
        body = src[m.end():i - 1]
        stripped = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        stripped = re.sub(r"//[^\n]*", "", stripped)
        if not stripped.strip():
            out.append((src.count("\n", 0, m.start()) + 1, body.strip()[:60]))
    return out


def _all() -> dict[str, list[tuple[int, str]]]:
    return {js.name: empty_catches(js.read_text(encoding="utf-8"))
            for js in sorted(_STATIC.rglob("*.js")) if "/tests/" not in str(js)}


def test_parser_hits_the_shapes():
    assert empty_catches("try {} catch { /* transient */ }") == [(1, "/* transient */")]
    assert empty_catches("try {} catch (e) {\n  // nothing\n}") and not empty_catches("try {} catch (e) { log(e); }")
    assert not empty_catches("try {} catch { x = 1; }")


def test_the_four_ledger_sites_now_report_their_failure():
    plan = (_STATIC / "views" / "plan.js").read_text(encoding="utf-8")
    assert "Could not load the plan change ledger" in plan
    theater = (_STATIC / "views" / "theater.js").read_text(encoding="utf-8")
    assert "Could not load this run's event log" in theater
    palette = (_STATIC / "command_palette.js").read_text(encoding="utf-8")
    assert "Job search failed" in palette
    compare = (_STATIC / "views" / "compare.js").read_text(encoding="utf-8")
    assert "Search failed:" in compare


def test_empty_catch_blocks_only_go_down():
    per = {k: v for k, v in _all().items() if v}
    total = sum(len(v) for v in per.values())
    assert total <= CEILING, (
        f"{total} empty catch blocks (ceiling {CEILING}). A swallowed failure must at least say so "
        f"(log line, inline error, toast); if you fixed some, lower the ceiling. Sites: {per}")
    assert total == CEILING, f"ceiling is loose: set it to {total}"
