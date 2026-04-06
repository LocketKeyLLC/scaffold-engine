-- Migration 004: Add 'running' and 'blocked' to jobs status CHECK constraint
DO $$
DECLARE
    constraint_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO constraint_def
    FROM pg_constraint
    WHERE conrelid = 'jobs'::regclass AND contype = 'c' AND conname LIKE '%status%';

    IF constraint_def IS NOT NULL AND (
        constraint_def NOT LIKE '%running%' OR constraint_def NOT LIKE '%blocked%'
    ) THEN
        EXECUTE 'ALTER TABLE jobs DROP CONSTRAINT ' ||
            (SELECT conname FROM pg_constraint
             WHERE conrelid = 'jobs'::regclass AND contype = 'c' AND conname LIKE '%status%');
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('pending','refining','planning','executing','running','completed','failed','cancelled','blocked'));
    ELSIF constraint_def IS NULL THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('pending','refining','planning','executing','running','completed','failed','cancelled','blocked'));
    END IF;
END;
$$;
