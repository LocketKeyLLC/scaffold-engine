-- Migration 008: Add 'awaiting_confirmation' and 'researching' to jobs status CHECK constraint
DO $$
DECLARE
    constraint_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO constraint_def
    FROM pg_constraint
    WHERE conrelid = 'jobs'::regclass AND contype = 'c' AND conname LIKE '%status%';
    IF constraint_def IS NOT NULL AND (
        constraint_def NOT LIKE '%awaiting_confirmation%' OR constraint_def NOT LIKE '%researching%'
    ) THEN
        EXECUTE 'ALTER TABLE jobs DROP CONSTRAINT ' ||
            (SELECT conname FROM pg_constraint
             WHERE conrelid = 'jobs'::regclass AND contype = 'c' AND conname LIKE '%status%');
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('pending','refining','planning','executing','running','completed','failed','cancelled','blocked','awaiting_confirmation','researching'));
    ELSIF constraint_def IS NULL THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('pending','refining','planning','executing','running','completed','failed','cancelled','blocked','awaiting_confirmation','researching'));
    END IF;
END;
$$;
