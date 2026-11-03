ALTER TABLE video_jobs
    ADD COLUMN IF NOT EXISTS progress_percent integer,
    ADD COLUMN IF NOT EXISTS failure_code text,
    ADD COLUMN IF NOT EXISTS failure_message text,
    ADD COLUMN IF NOT EXISTS retryable boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'video_jobs_progress_percent_range'
          AND conrelid = 'video_jobs'::regclass
    ) THEN
        ALTER TABLE video_jobs ADD CONSTRAINT video_jobs_progress_percent_range
            CHECK (progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100);
    END IF;
END $$;
