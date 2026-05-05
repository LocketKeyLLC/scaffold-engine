-- Migration 025: Drop unused 'model_failure' and 'structural' values from
-- error_logs.error_type CHECK.
--
-- Phase 3a/3c grep confirmed no application code writes either value.
-- The error-classifying middleware (app/middleware/error_logging.py) only
-- emits 'transient', 'timeout', 'validation', 'unrecoverable'. Tightening
-- the CHECK so future drift is caught at the constraint layer, mirroring
-- the cluster-F treatment of assist_steps.status='applied'.
--
-- Defensive: in case any existing row carries one of the dead values,
-- normalize to 'unrecoverable' before tightening the constraint so the
-- ALTER doesn't fail on legacy data.
--
-- Idempotent: drops constraint if present, normalizes legacy rows, re-adds
-- the 4-value form.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_name = 'error_logs'
           AND constraint_name = 'error_logs_error_type_check'
    ) THEN
        ALTER TABLE error_logs
            DROP CONSTRAINT error_logs_error_type_check;
    END IF;

    UPDATE error_logs
       SET error_type = 'unrecoverable'
     WHERE error_type IN ('model_failure', 'structural');

    ALTER TABLE error_logs
        ADD CONSTRAINT error_logs_error_type_check
        CHECK (error_type IN (
            'transient', 'timeout', 'validation', 'unrecoverable'
        ));
END $$;
