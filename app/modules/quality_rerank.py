"""Quality-signal-weighted rerank (§17.120).

Final phase-4 commit. Applies a per-source-type multiplicative score bump
based on the ``quality_signal`` dict recorded in §17.114's provenance
sidecar — letting a Stack Overflow answer with 200 votes outrank a
generic prose chunk at equal embedding similarity.

Bumps cap at ×1.20 to keep retrieval from being purely vote-driven.
Embedding similarity remains the primary signal; quality just breaks
ties and gives validated content a margin.

Per-source thresholds (chosen by the empirical distribution shape rather
than calibration — calibration is a follow-up):

| source_type     | signal              | bump tiers |
|-----------------|---------------------|------------|
| so_answer       | score, is_accepted  | accepted +0.10, score≥50 +0.05, score≥200 +0.05 |
| hn_comment      | points              | ≥100 +0.05, ≥500 +0.10 |
| reddit_post     | score               | ≥100 +0.05, ≥500 +0.10 |
| community (GH)  | positive_reactions  | ≥5 +0.05, ≥20 +0.10 |
| model_card      | likes               | ≥100 +0.05, ≥1000 +0.10 |
| dataset_card    | likes               | ≥100 +0.05, ≥1000 +0.10 |
| paper_abstract  | upvotes (HF papers) | ≥50 +0.05 |
| anything else   | —                   | 1.00 (no bump) |

Entries with no provenance row (pre-§17.104 or non-research ingest)
return 1.00 too — the rerank is opt-in, not punitive.
"""
from __future__ import annotations

from typing import Any

_BUMP_CAP = 1.20


def quality_bump(source_type: str, quality_signal: dict[str, Any] | None) -> float:
    """Return a multiplicative score bump in ``[1.00, 1.20]``.

    ``quality_signal=None`` (no provenance) or unknown ``source_type`` →
    1.00. Bumps cap at ×1.20.
    """
    if not quality_signal:
        return 1.0
    bump = 1.0

    if source_type == "so_answer":
        if quality_signal.get("is_accepted"):
            bump += 0.10
        score = int(quality_signal.get("score") or 0)
        if score >= 200:
            bump += 0.10
        elif score >= 50:
            bump += 0.05
    elif source_type == "hn_comment":
        points = int(quality_signal.get("points") or 0)
        if points >= 500:
            bump += 0.10
        elif points >= 100:
            bump += 0.05
    elif source_type == "reddit_post":
        score = int(quality_signal.get("score") or 0)
        if score >= 500:
            bump += 0.10
        elif score >= 100:
            bump += 0.05
    elif source_type == "community":
        # GH issues/PRs — positive_reactions populated by
        # fetch_repo_issues_and_prs.
        reactions = int(quality_signal.get("positive_reactions") or 0)
        if reactions >= 20:
            bump += 0.10
        elif reactions >= 5:
            bump += 0.05
    elif source_type in ("model_card", "dataset_card"):
        likes = int(quality_signal.get("likes") or 0)
        if likes >= 1000:
            bump += 0.10
        elif likes >= 100:
            bump += 0.05
    elif source_type == "paper_abstract":
        upvotes = int(quality_signal.get("upvotes") or 0)
        if upvotes >= 50:
            bump += 0.05

    return min(bump, _BUMP_CAP)
