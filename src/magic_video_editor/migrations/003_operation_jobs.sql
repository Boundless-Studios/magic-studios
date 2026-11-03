ALTER TABLE video_jobs
    ADD COLUMN IF NOT EXISTS operation_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS project_id text,
    ADD COLUMN IF NOT EXISTS manifest_operation_id text,
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'video_jobs_operation_version_positive'
          AND conrelid = 'video_jobs'::regclass
    ) THEN
        ALTER TABLE video_jobs ADD CONSTRAINT video_jobs_operation_version_positive
            CHECK (operation_version > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'video_jobs_payload_object'
          AND conrelid = 'video_jobs'::regclass
    ) THEN
        ALTER TABLE video_jobs ADD CONSTRAINT video_jobs_payload_object
            CHECK (jsonb_typeof(payload) = 'object');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS video_jobs_project_idx
    ON video_jobs (project_id, created_at)
    WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS video_jobs_manifest_operation_idx
    ON video_jobs (manifest_operation_id)
    WHERE manifest_operation_id IS NOT NULL;
