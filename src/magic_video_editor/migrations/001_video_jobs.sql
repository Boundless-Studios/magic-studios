CREATE TABLE IF NOT EXISTS video_jobs (
    job_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    operation text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
    source_key text NOT NULL,
    output_key text NOT NULL,
    requested_outputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0,
    claim_token text,
    lease_until timestamptz,
    result jsonb,
    error text,
    cancelled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS video_jobs_claimable_idx
    ON video_jobs (status, lease_until, created_at)
    WHERE status IN ('queued', 'processing');

