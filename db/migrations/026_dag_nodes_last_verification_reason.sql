-- 026_dag_nodes_last_verification_reason.sql
-- Sprint W.1 — verifier-feedback loop on retry.
--
-- Add a column that holds the most recent verifier rejection reason for a
-- node. _set_node_status writes this on `failed`; _build_prompt reads it
-- when retry_count > 0 to prepend a "Reviewer feedback" block to the next
-- attempt's prompt. retry_failed_node intentionally does NOT null this
-- column on reset — the whole point of the feedback loop is that the next
-- attempt sees what the previous one got wrong.
--
-- Idempotent (IF NOT EXISTS) so re-applying is a no-op. No backfill —
-- existing nodes have no recorded reason, which is the correct null state
-- (we never verified them under this scheme).

ALTER TABLE dag_nodes
    ADD COLUMN IF NOT EXISTS last_verification_reason TEXT;
