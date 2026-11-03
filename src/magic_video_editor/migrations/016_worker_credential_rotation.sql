-- Machine credentials rotate, so nobody ever handles one by hand after the
-- first attach.
--
-- Add-only and replayed on every start: no ADD COLUMN that a later migration
-- takes away again (see 006, whose churn burned the 1600-column ceiling and
-- took production down on 2026-09-06).

-- When the current credential stops being accepted. NULL means a credential
-- minted before rotation existed: still valid, and it acquires an expiry the
-- first time it rotates.
ALTER TABLE studio_workers
    ADD COLUMN IF NOT EXISTS credential_expires_at timestamptz;

-- The previous credential stays valid for a short overlap so a rotation that
-- crosses a restart, or a reply lost in flight, cannot lock a machine out.
ALTER TABLE studio_workers
    ADD COLUMN IF NOT EXISTS previous_credential_hash text;

ALTER TABLE studio_workers
    ADD COLUMN IF NOT EXISTS previous_credential_expires_at timestamptz;

ALTER TABLE studio_workers
    ADD COLUMN IF NOT EXISTS rotated_at timestamptz;

-- A machine authenticates by presenting either hash, so both need an index.
CREATE INDEX IF NOT EXISTS studio_workers_previous_credential_idx
    ON studio_workers (previous_credential_hash)
    WHERE previous_credential_hash IS NOT NULL;
