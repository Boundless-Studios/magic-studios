-- The outbound GPU worker pool now lives in the outbound-gpu-worker-pool library,
-- which owns pool_jobs, pool_workers, and pool_audit_events in this same database.
-- The product's video_jobs table goes back to being the ledger for its own CPU and
-- GPU operations only. attempt_budget stays: the product still bounds its retries.

DROP TABLE IF EXISTS worker_audit_events;

DROP TABLE IF EXISTS gpu_workers;

DROP INDEX IF EXISTS video_jobs_pool_leasable_idx;

ALTER TABLE video_jobs
    DROP CONSTRAINT IF EXISTS video_jobs_priority_range,
    DROP CONSTRAINT IF EXISTS video_jobs_execution_deadline_range;

ALTER TABLE video_jobs
    DROP COLUMN IF EXISTS capability_id,
    DROP COLUMN IF EXISTS priority,
    DROP COLUMN IF EXISTS execution_deadline_seconds,
    DROP COLUMN IF EXISTS request_digest,
    DROP COLUMN IF EXISTS leased_by;
