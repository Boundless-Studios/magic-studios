-- Why a completed job has not folded into the manifest yet. Advisory only: the
-- job keeps its `completed` status so the UI can offer a retry.
ALTER TABLE video_jobs
    ADD COLUMN IF NOT EXISTS settlement_note text;
