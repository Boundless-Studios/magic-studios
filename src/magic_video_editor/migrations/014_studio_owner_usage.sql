-- Per-owner stored-byte accounting for the internal multi-user storage cap.
-- Tracked incrementally as artifacts are written; listing the shared bucket per
-- request would be slow and would read across the tenancy boundary.
-- Add-only and idempotent: this table drops nothing and alters no other table.
CREATE TABLE IF NOT EXISTS studio_owner_usage (
    owner_id TEXT PRIMARY KEY,
    stored_bytes BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
