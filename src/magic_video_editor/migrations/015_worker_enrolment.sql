-- Machine enrolment: a person pairs their own GPU box with the studio.
--
-- Add-only, and replayed on every start: no ADD COLUMN that a later migration
-- drops (see 006, whose add/drop churn burned the 1600-column ceiling and took
-- production down on 2026-09-06).

-- A pairing token is single-use and short-lived: it exists only long enough for
-- the agent on a machine to redeem it once. Only its sha256 is stored.
CREATE TABLE IF NOT EXISTS studio_pairing_tokens (
    token_id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    owner_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    redeemed_worker_id text
);

CREATE INDEX IF NOT EXISTS studio_pairing_tokens_owner_idx
    ON studio_pairing_tokens (owner_id, created_at DESC);

-- An enrolled machine. `credential_hash` is what the agent presents on every
-- pool request; the plaintext is shown once at redemption and never again.
CREATE TABLE IF NOT EXISTS studio_workers (
    worker_id text PRIMARY KEY,
    owner_id text NOT NULL,
    label text NOT NULL,
    credential_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS studio_workers_owner_idx
    ON studio_workers (owner_id, created_at DESC);
