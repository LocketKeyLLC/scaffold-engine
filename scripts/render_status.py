#!/usr/bin/env python3
"""Pretty-print the orchestrator's /status response as a table.

Reads JSON on stdin, writes a human-readable summary on stdout. Used by
`make status` (Sprint U.3) so the default operator surface isn't a raw
JSON dump.

For machine-readable JSON output, use the underlying `curl /status`
directly, or `scaffold jobs list --json`.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"render_status: stdin is not valid JSON ({exc})", file=sys.stderr)
        return 1

    counts: dict[str, int] = data.get("status_counts") or {}
    recent: list[dict] = data.get("recent_jobs") or []
    total: int = data.get("total_jobs", 0)

    print(f"Total jobs: {total}")
    print()
    print(f"{'status':<26} {'count':>6}")
    print("-" * 34)
    if counts:
        for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{status:<26} {count:>6}")
    else:
        print("(no jobs yet)")

    print()
    print("Most recent jobs:")
    print(f"{'job_id':<38} {'status':<24} title")
    print("-" * 90)
    if recent:
        for job in recent[:10]:
            jid = str(job.get("id", ""))[:36]
            status = str(job.get("status", ""))[:22]
            title = (job.get("title") or job.get("idea") or "")[:40]
            print(f"{jid:<38} {status:<24} {title}")
    else:
        print("(no recent jobs)")

    # Helpful next-step hint if any job is awaiting confirmation.
    awaiting = [j for j in recent if j.get("status") == "awaiting_confirmation"]
    if awaiting:
        print()
        print(f"Next: scaffold confirm {awaiting[0]['id']}")
        print("  (or `scaffold jobs list --status awaiting_confirmation` to see all)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
