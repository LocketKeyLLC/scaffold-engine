"""§17.1121 — the research feed renders the fields the agent actually emits.

The feed read `d.extracted`, `d.results`, `d.queries` … while the agent emits
`entries_extracted`, `results_found`, `total_urls`; five event kinds had no
renderer and printed their raw names. This gate parses every `_sse("<event>",
{...})` literal in the research modules (AST) and the `case "<event>":`
blocks of `feedText` in research.js, and asserts (1) every emitted event has a
renderer or is deliberately ignored, and (2) every `d.<field>` a renderer reads
is a field SOME emitter of that event sends. Static, ci-tier-0.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_RESEARCH_PY = [ROOT / "app" / "modules" / f for f in ("research_agent.py", "research_state.py", "research_extractors.py")]
_VIEW = ROOT / "app" / "ui" / "static" / "views" / "research.js"
# events the feed deliberately does not render as lines
IGNORED = {"heartbeat", "progress"}
# fields that are read but come from a helper-built payload (dict returned by a
# function, not a literal) — checked against that function's literal instead
_HELPER_PAYLOADS = {"research_complete": "_build_research_complete_payload"}


def _emitted() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for p in _RESEARCH_PY:
        if not p.exists():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        helper_keys: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in _HELPER_PAYLOADS.values():
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        helper_keys.setdefault(node.name, set()).update(k.value for k in sub.keys if isinstance(k, ast.Constant))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_sse" and node.args and isinstance(node.args[0], ast.Constant):
                ev = node.args[0].value
                keys.setdefault(ev, set())
                if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                    keys[ev].update(k.value for k in node.args[1].keys if isinstance(k, ast.Constant))
                elif len(node.args) > 1 and isinstance(node.args[1], ast.Call) and getattr(node.args[1].func, "id", None) in helper_keys:
                    keys[ev].update(helper_keys[node.args[1].func.id])
        for ev, fn in _HELPER_PAYLOADS.items():
            if ev in keys and fn in helper_keys:
                keys[ev].update(helper_keys[fn])
    return keys


def _renderers() -> dict[str, str]:
    src = _VIEW.read_text(encoding="utf-8")
    body = src[src.index("export function feedText"):]
    body = body[:body.index("\n}\n") + 3]
    out: dict[str, str] = {}
    cases = list(re.finditer(r'case "([a-z_]+)":', body))
    for i, m in enumerate(cases):
        end = cases[i + 1].start() if i + 1 < len(cases) else body.index("default:")
        out[m.group(1)] = body[m.end():end]
    return out


def test_inventories_are_not_blind():
    em, rn = _emitted(), _renderers()
    assert len(em) >= 15 and "ingestion_complete" in em and "entries_extracted" in em["extraction_complete"]
    assert len(rn) >= 15 and "research_fetch" in rn


def test_every_emitted_event_has_a_renderer_or_is_ignored():
    em, rn = _emitted(), _renderers()
    src = _VIEW.read_text(encoding="utf-8")
    ignored = set(re.findall(r'"([a-z_]+)"', src[src.index("RESEARCH_FEED_IGNORED = new Set(["):src.index("]);", src.index("RESEARCH_FEED_IGNORED"))]))
    assert ignored == IGNORED
    missing = sorted(ev for ev in em if ev not in rn and ev not in ignored)
    assert not missing, f"research events with no feed renderer (they would print as raw names): {missing}"


def test_every_field_a_renderer_reads_is_emitted():
    em, rn = _emitted(), _renderers()
    bad = []
    for ev, block in rn.items():
        if ev in ("error", "warning"):
            continue                              # payloads built from exceptions; message/stage/error are all seen
        reads = set(re.findall(r"\bd\.([a-z_]+)", block))
        # a chained fallback `d.a ?? d.b` is fine as long as EACH name is emitted by some site
        unknown = sorted(r for r in reads if r not in em.get(ev, set()))
        if unknown:
            bad.append(f"{ev}: reads {unknown}, emitted {sorted(em.get(ev, set()))}")
    assert not bad, "the feed reads fields the agent never sends (they render as '?'):\n  " + "\n  ".join(bad)
