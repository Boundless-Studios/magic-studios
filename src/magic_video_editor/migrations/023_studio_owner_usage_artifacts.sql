-- Per-artifact usage records, so counting an artifact is idempotent: a retried
-- or concurrent count of the same key adds nothing, and a release subtracts
-- exactly what was recorded. Used for turnaround videos, whose count runs after
-- the take is stored and may be retried by reconciliation.
-- Add-only and idempotent: this table drops nothing and alters no other table.
CREATE TABLE IF NOT EXISTS studio_owner_usage_artifacts (
    owner_id TEXT NOT NULL,
    artifact_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, artifact_key)
);
