"""Project nickname store + slug generation (Sprint U.4).

The orchestrator addresses jobs by UUID. Humans don't. This module gives
the CLI a tiny client-side mapping from a friendly nickname to the
underlying UUID so users can type ``scaffold project resume markdown-linter``
instead of pasting a 36-character hex string.

Storage: ``~/.scaffold/nicknames.json`` (or ``$XDG_CONFIG_HOME/scaffold/...``
when set, mirroring the existing config-resolution chain). One JSON
object: ``{<nickname>: <uuid>, ...}``.

The store is intentionally simple — local cache, lossy if the user
clones onto a new machine. The orchestrator's `jobs.title` remains the
authoritative human label; nicknames are an additive convenience.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

# A UUID has hex chars + 4 dashes in canonical form.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _config_dir() -> Path:
    """Return the directory where the nicknames file lives.

    Mirrors ``cli.config.resolve_config`` precedence: respect
    XDG_CONFIG_HOME if set, else ~/.scaffold.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "scaffold"
    return Path.home() / ".scaffold"


def _store_path() -> Path:
    return _config_dir() / "nicknames.json"


def looks_like_uuid(value: str) -> bool:
    """True if ``value`` is shaped like a canonical UUID (case-insensitive)."""
    return bool(_UUID_RE.match(value or ""))


def slugify(text: str, *, max_chars: int = 40) -> str:
    """Slug-form an idea string for use as a nickname seed.

    - Lowercase
    - Non-alphanumeric → hyphen
    - Collapse repeated hyphens
    - Truncate to ``max_chars``
    - Strip leading/trailing hyphens (after truncation, so a cut in the
      middle of a hyphen group doesn't leave a trailing dash)
    """
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text[:max_chars].strip("-")
    return text or "project"


def make_nickname(idea_text: str, job_id: str) -> str:
    """Generate a friendly nickname for a fresh job.

    Combines a slug of the idea with a 4-char hash suffix derived from
    the job_id. The suffix prevents collisions when two ideas slugify
    to the same string.
    """
    slug = slugify(idea_text)
    short_hash = hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:4]
    return f"{slug}-{short_hash}"


def load_store() -> dict[str, str]:
    """Read the nickname store. Returns ``{}`` if the file is missing
    or unreadable — callers shouldn't crash on a fresh install."""
    path = _store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_store(store: dict[str, str]) -> None:
    """Persist the nickname store. Creates the directory if needed.
    Atomic-ish via temp-file rename so a crash mid-write doesn't
    corrupt the file."""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def add_nickname(nickname: str, job_id: str) -> None:
    """Add a (nickname → job_id) entry. Overwrites existing nickname."""
    store = load_store()
    store[nickname] = job_id
    save_store(store)


def resolve(value: str) -> str | None:
    """Resolve ``value`` to a UUID.

    - If ``value`` already looks like a UUID, return it unchanged.
    - Otherwise look up the nickname store; return the mapped UUID or None.

    Callers should treat ``None`` as "not a known nickname locally"
    and fall back to whatever (e.g. server-side title search).
    """
    if not value:
        return None
    if looks_like_uuid(value):
        return value
    return load_store().get(value)


def reverse_lookup(job_id: str) -> str | None:
    """Find the nickname for a job_id, if one is registered locally.

    Used to render `(nickname)` annotations in `scaffold project list`
    and similar surfaces.
    """
    if not job_id:
        return None
    store = load_store()
    for nick, jid in store.items():
        if jid == job_id:
            return nick
    return None


# ---------------------------------------------------------------------------
# Status explanations — for `scaffold explain <status>`.
#
# Mirrors app/modules/recovery.py NEXT_ACTIONS but adds a plain-English
# description per status. Kept locally so `scaffold explain` works
# offline. If the orchestrator gains new statuses, this dict needs
# updating alongside JobStatus and NEXT_ACTIONS.
# ---------------------------------------------------------------------------

STATUS_EXPLAIN: dict[str, dict[str, Any]] = {
    "pending": {
        "headline": "Just created; refinement hasn't started yet.",
        "what_happens": "The system will pick this up within a few seconds and move it to `refining`.",
        "valid_actions": ["wait"],
    },
    "refining": {
        "headline": "The LLM is producing a structured brief.",
        "what_happens": "Takes 1–3 minutes. When done, the job moves to `awaiting_confirmation`.",
        "valid_actions": ["wait"],
    },
    "awaiting_confirmation": {
        "headline": "Phase 1 is done; waiting for you to approve the plan.",
        "what_happens": "The job stays here indefinitely. Approve to start Phase 2 (research, planning, execution), or abandon to remove.",
        "valid_actions": ["confirm", "delete"],
    },
    "researching": {
        "headline": "Phase 2 — the system is searching, fetching, and ingesting sources.",
        "what_happens": "10–25 minutes on CPU. Watch progress with `scaffold jobs status <job_id>`.",
        "valid_actions": ["wait"],
    },
    "planning": {
        "headline": "Generating the DAG of execution steps.",
        "what_happens": "7–9 minutes. When done, execution starts automatically.",
        "valid_actions": ["wait"],
    },
    "executing": {
        "headline": "Running DAG nodes in dependency order.",
        "what_happens": "Each node has its own timeout; failures auto-retry up to 3 times. Total time depends on node count.",
        "valid_actions": ["wait", "skip_node"],
    },
    "running": {
        "headline": "Same as executing — a node is in flight.",
        "what_happens": "Same lifecycle as executing.",
        "valid_actions": ["wait", "skip_node"],
    },
    "completed": {
        "headline": "Done. The compiled output is in the job record.",
        "what_happens": "Read it with `scaffold jobs status <job_id>` or via `/results <job_id>` in chat.",
        "valid_actions": ["view_output"],
    },
    "failed": {
        "headline": "Something went unrecoverably wrong.",
        "what_happens": "Either a single node failed verification 3 times, or an upstream phase (research / planning) hit an unrecoverable error. Check the failure summary in `scaffold jobs status`.",
        "valid_actions": ["retry_node", "skip_node", "delete"],
    },
    "blocked": {
        "headline": "A node failed all retries; downstream is held back.",
        "what_happens": "Retry the blocked node manually with `/exec retry`, or skip it to let downstream continue.",
        "valid_actions": ["retry_node", "skip_node"],
    },
    "cancelled": {
        "headline": "The job was abandoned (manually or by the reaper).",
        "what_happens": "Terminal state. No further work happens. Remove with `/jobs delete <job_id>`.",
        "valid_actions": ["delete"],
    },
    "assisted_executing": {
        "headline": "Job is being walked through manually in Assist Mode.",
        "what_happens": "You're driving each step; the system acts as co-pilot. Use `/assist next` to claim the next step.",
        "valid_actions": ["next_step", "pause"],
    },
    "assisted_running": {
        "headline": "Same as assisted_executing — a step is in flight.",
        "what_happens": "Submit your evidence with `/assist submit`, or pause/skip.",
        "valid_actions": ["next_step", "submit"],
    },
    "assisted_paused": {
        "headline": "Assist session is paused.",
        "what_happens": "Resume any time, or abandon.",
        "valid_actions": ["resume", "abandon"],
    },
}
