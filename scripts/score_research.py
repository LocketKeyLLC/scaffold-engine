"""§17.558 — research-output quality eval: grounding + coverage.

Scores the research SYNTHESIS step in isolation. For each golden
``{topic, entries, expected_facets}`` it drives the real ``_generate_summary``
(SUMMARY_SYSTEM_V1) over the golden entries, then measures two axes:

  • COVERAGE (deterministic) — fraction of ``expected_facets`` addressed in the
    summary, via case-insensitive AND-substring match (the §17.550 golden shape).
  • GROUNDING (faithfulness) — ``score_faithfulness(summary, entries_text)`` →
    the fraction of summary claims supported by the collected entries. This is
    the real anti-hallucination signal and the exact §17.522 failure mode
    ("research grounding 100% broken"): a summary can cover every facet and
    still be fabricated. "What good is data if it's a hallucination?"

Synthesis is scored in isolation (golden entries in, not a live search) so the
number is repeatable and isolates the summary step from SearXNG/fetch variance.
Grounding is LLM-judged (qwen3.5 coaxed), so treat it as a measured eval with a
soft floor, not a byte-deterministic gate.

Runs INSIDE the orchestrator/dev container (needs app imports + Ollama):

    docker exec scaffold-orchestrator python scripts/score_research.py
    python scripts/score_research.py --golden tests/fixtures/research_goldens.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.faithfulness import score_faithfulness
from app.modules.research_agent import _generate_summary
from app.utils.http_clients import init_clients

_DEFAULT_GOLDENS = Path(__file__).resolve().parent.parent / "tests/fixtures/research_goldens.json"


# ---------------------------------------------------------------------------
# Coverage (deterministic, unit-testable) — §17.550 AND-substring shape
# ---------------------------------------------------------------------------

def _facet_hit(summary: str, contains: list[str]) -> bool:
    """True iff every substring appears in the summary (case-insensitive AND)."""
    if not contains:
        return False
    s = summary.lower()
    return all(sub.lower() in s for sub in contains)


def facet_coverage(summary: str, expected_facets: list[dict]) -> dict:
    """Pure: fraction of expected facets addressed in the summary."""
    if not expected_facets:
        return {"covered": 0, "total": 0, "coverage": 0.0, "missed": []}
    hits = [f for f in expected_facets if _facet_hit(summary, f.get("contains", []))]
    missed = [f["name"] for f in expected_facets if f not in hits]
    return {
        "covered": len(hits),
        "total": len(expected_facets),
        "coverage": len(hits) / len(expected_facets),
        "missed": missed,
    }


@dataclass
class ResearchScore:
    id: str
    topic: str
    synthesis_ok: bool
    coverage: float | None
    covered: int
    total_facets: int
    missed_facets: list[str]
    grounding: float | None
    supported: int | None
    total_claims: int | None
    unsupported: list[str] = field(default_factory=list)
    summary_chars: int = 0


async def score_one(golden: dict) -> ResearchScore:
    entries = golden["entries"]
    state = SimpleNamespace(topic=golden["topic"], all_entries=entries)
    summary = await _generate_summary(state)

    # §17.558 — an empty summary is a SYNTHESIS FAILURE (the thinking model can
    # burn its token budget on reasoning and return success+empty — the
    # "thinking model empty content" issue). Score it as such, distinct from
    # "covered 0 facets" or "ungrounded", so the means aren't polluted by it.
    if not (summary or "").strip():
        return ResearchScore(
            id=golden["id"], topic=golden["topic"], synthesis_ok=False,
            coverage=None, covered=0, total_facets=len(golden.get("expected_facets", [])),
            missed_facets=[f["name"] for f in golden.get("expected_facets", [])],
            grounding=None, supported=None, total_claims=None, summary_chars=0,
        )

    cov = facet_coverage(summary, golden.get("expected_facets", []))
    context = "\n\n".join(e.get("content", "") for e in entries)
    faith = await score_faithfulness(summary, context)

    return ResearchScore(
        id=golden["id"], topic=golden["topic"], synthesis_ok=True,
        coverage=cov["coverage"], covered=cov["covered"],
        total_facets=cov["total"], missed_facets=cov["missed"],
        grounding=(faith["score"] if faith else None),
        supported=(faith["supported"] if faith else None),
        total_claims=(faith["total"] if faith else None),
        unsupported=(faith["unsupported_claims"][:5] if faith else []),
        summary_chars=len(summary or ""),
    )


async def run(golden_path: Path, output_path: Path) -> dict:
    init_clients()
    data = json.loads(golden_path.read_text())
    goldens = data["goldens"] if isinstance(data, dict) else data
    results = [await score_one(g) for g in goldens]

    synthd = [r for r in results if r.synthesis_ok]
    covered = [r.coverage for r in synthd if r.coverage is not None]
    grounded = [r.grounding for r in synthd if r.grounding is not None]
    summary = {
        "schema": "research_quality_v1",
        "total_topics": len(results),
        "synthesis_ok": len(synthd),
        "synthesis_failed": [r.id for r in results if not r.synthesis_ok],
        "mean_coverage": mean(covered) if covered else 0.0,
        "mean_grounding": mean(grounded) if grounded else 0.0,
        "grounding_scored": len(grounded),
        "per_topic": [asdict(r) for r in results],
    }
    output_path.write_text(json.dumps(summary, indent=2))
    return summary


def _print_report(s: dict) -> None:
    print("=" * 60)
    print("Research Quality — grounding + coverage (§17.558)")
    print("=" * 60)
    print(f"Topics:          {s['total_topics']}  (synthesis_ok={s['synthesis_ok']})")
    if s["synthesis_failed"]:
        print(f"SYNTHESIS FAILED: {s['synthesis_failed']}  (empty summary — see §17.558)")
    print(f"Mean coverage:   {s['mean_coverage']:.1%}  (over synthesized topics)")
    print(f"Mean grounding:  {s['mean_grounding']:.1%}  "
          f"({s['grounding_scored']}/{s['synthesis_ok']} scored)")
    print("-" * 60)
    for r in s["per_topic"]:
        if not r["synthesis_ok"]:
            print(f"  {r['id']:<22} SYNTHESIS FAILED (empty summary)")
            continue
        g = f"{r['grounding']:.0%}" if r["grounding"] is not None else "n/a"
        print(f"  {r['id']:<22} cov={r['coverage']:.0%} "
              f"({r['covered']}/{r['total_facets']})  ground={g}"
              + (f"  missed={r['missed_facets']}" if r["missed_facets"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=_DEFAULT_GOLDENS, type=Path)
    ap.add_argument("--output", default=Path("/tmp/research_quality.json"), type=Path)
    args = ap.parse_args()
    if not args.golden.exists():
        print(f"ERROR: golden set not found at {args.golden}", file=sys.stderr)
        return 1
    s = asyncio.run(run(args.golden, args.output))
    _print_report(s)
    print(f"\nFull report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
