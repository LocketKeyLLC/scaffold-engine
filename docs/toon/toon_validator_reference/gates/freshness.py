"""
Gate F: Content Freshness Check.
Flags entries where last_verified is 'pending' or older than threshold.
Zero compute cost — pure date comparison.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..core import parse_toon_sections

logger = logging.getLogger(__name__)

DEFAULT_STALE_DAYS = 90


@dataclass
class FreshnessEntry:
    line: int
    entry_id: str
    topic: str
    last_verified: str
    is_pending: bool
    is_stale: bool
    days_old: Optional[int] = None


@dataclass
class FreshnessResult:
    total_entries: int
    pending_count: int
    stale_count: int
    fresh_count: int
    entries: list[FreshnessEntry] = field(default_factory=list)

    @property
    def healthy_ratio(self) -> float:
        if self.total_entries == 0:
            return 1.0
        return self.fresh_count / self.total_entries

    def summary(self) -> str:
        return (
            f"Freshness: {self.fresh_count}/{self.total_entries} fresh, "
            f"{self.pending_count} pending, {self.stale_count} stale "
            f"({self.healthy_ratio:.0%} healthy)"
        )


def check_freshness(
    content: str,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> FreshnessResult:
    """
    Check content freshness of all entries in a TOON file.

    Args:
        content: TOON file content
        stale_days: Number of days before an entry is considered stale

    Returns:
        FreshnessResult with per-entry details
    """
    sections = parse_toon_sections(content)
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=stale_days)

    # Locate field indices
    try:
        id_idx = sections["declared_fields"].index("id")
        topic_idx = sections["declared_fields"].index("topic")
        lv_idx = sections["declared_fields"].index("last_verified")
    except (ValueError, IndexError):
        id_idx, topic_idx, lv_idx = 0, 1, 6

    entries = []
    pending = stale = fresh = 0

    for line_num, line in sections["data_lines"]:
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
        except (csv.Error, StopIteration):
            continue

        if len(row) <= lv_idx:
            continue

        entry_id = row[id_idx] if len(row) > id_idx else "?"
        topic = row[topic_idx] if len(row) > topic_idx else "?"
        lv_raw = row[lv_idx].strip()

        is_pending = lv_raw.lower() in ("pending", "")
        is_stale = False
        days_old = None

        if is_pending:
            pending += 1
        else:
            try:
                # Support ISO format: 2026-03-16T00:00:00Z or 2026-03-16
                lv_date = datetime.fromisoformat(lv_raw.replace("Z", "+00:00"))
                days_old = (now - lv_date).days
                is_stale = lv_date < threshold
                if is_stale:
                    stale += 1
                else:
                    fresh += 1
            except ValueError:
                # Unparseable date — treat as pending
                is_pending = True
                pending += 1

        entries.append(FreshnessEntry(
            line=line_num + 1, entry_id=entry_id, topic=topic,
            last_verified=lv_raw, is_pending=is_pending,
            is_stale=is_stale, days_old=days_old,
        ))

    return FreshnessResult(
        total_entries=len(entries), pending_count=pending,
        stale_count=stale, fresh_count=fresh, entries=entries,
    )
