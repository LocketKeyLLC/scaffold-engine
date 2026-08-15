-- §17.797 — domain-aware scheduled research.
-- Adds an optional partition-domain override to scheduled jobs so a recurring
-- /research run can pin its ingest domain (e.g. eng_design, which has no
-- classifier route — §17.796 — and thus can only be fed by an explicit domain).
-- NULL preserves the existing behavior: the scheduler passes domain=None and
-- _detect_domain() auto-classifies (never eng_design). Single-statement per the
-- migration-runner's prepared-statement path.
ALTER TABLE scheduled_jobs ADD COLUMN IF NOT EXISTS domain TEXT;
