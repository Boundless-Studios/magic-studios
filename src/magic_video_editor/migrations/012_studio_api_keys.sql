-- Bearer API keys that let a non-browser client act as a studio user. Only the
-- SHA-256 of a key is stored; the plaintext is shown once at creation.
CREATE TABLE IF NOT EXISTS studio_api_keys (
    key_id UUID PRIMARY KEY,
    actor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS studio_api_keys_actor_created_idx
    ON studio_api_keys (actor_id, created_at DESC);
