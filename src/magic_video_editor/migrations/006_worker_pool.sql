-- 2026-09-06 incident (Sentry MAGIC-VIDEO-EDITOR-BACKEND-F): this file used to add five
-- pool columns, an index and two tables that 007 then dropped again, on EVERY start.
-- Postgres keeps a catalog slot for each dropped column until the table is rewritten,
-- so each start burned five of video_jobs' 1600 slots; when the ceiling was reached,
-- every service failed its startup probe and production went down. Only the one
-- column that 007 keeps may be added here. Reclaiming the burned slots needs a table
-- rewrite (VACUUM FULL video_jobs) by an operator; startup no longer depends on it.

ALTER TABLE video_jobs
    ADD COLUMN IF NOT EXISTS attempt_budget integer NOT NULL DEFAULT 5;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'video_jobs_attempt_budget_range'
          AND conrelid = 'video_jobs'::regclass
    ) THEN
        ALTER TABLE video_jobs ADD CONSTRAINT video_jobs_attempt_budget_range
            CHECK (attempt_budget BETWEEN 1 AND 20);
    END IF;
END $$;
