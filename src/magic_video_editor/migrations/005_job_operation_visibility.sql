CREATE INDEX IF NOT EXISTS video_jobs_project_status_created_idx
    ON video_jobs (project_id, status, created_at, job_id)
    WHERE project_id IS NOT NULL;
