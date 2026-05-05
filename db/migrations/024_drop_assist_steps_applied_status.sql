-- Migration 024: Drop unused 'applied' value from assist_steps.status CHECK
--
-- Migration 023 introduced assist_steps with a 9-value status CHECK. The
-- 'applied' value was never written by any code path (verified by repo
-- grep of literal "'applied'" / '"applied"'). Removing it tightens the
-- schema so future drift is caught at the constraint layer.
--
-- Idempotent: drops the constraint if present (whether or not 'applied'
-- is in it) and re-adds the 8-value form.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_name = 'assist_steps'
           AND constraint_name = 'assist_steps_status_check'
    ) THEN
        ALTER TABLE assist_steps
            DROP CONSTRAINT assist_steps_status_check;
    END IF;

    ALTER TABLE assist_steps
        ADD CONSTRAINT assist_steps_status_check
        CHECK (status IN (
            'pending', 'presented', 'awaiting_input', 'received',
            'committed', 'skipped', 'escalated', 'handed_off'
        ));
END $$;
