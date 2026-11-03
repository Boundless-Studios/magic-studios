-- The studio's own sign-in: users and server-side sessions. Only the hash of a
-- session token is stored; the plaintext lives in the user's cookie and nowhere else.
-- `studio_login_tokens` is unused since sign-in moved to Auth0; the table is left in
-- place rather than dropped, so replaying this migration stays a no-op everywhere.
CREATE TABLE IF NOT EXISTS studio_users (
    user_id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    google_sub TEXT UNIQUE,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sign_in_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS studio_sessions (
    session_id TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES studio_users(user_id) ON DELETE CASCADE,
    sign_in_method TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS studio_sessions_user_idx
    ON studio_sessions (user_id);
CREATE INDEX IF NOT EXISTS studio_sessions_expires_idx
    ON studio_sessions (expires_at);

CREATE TABLE IF NOT EXISTS studio_login_tokens (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    return_to TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    request_ip TEXT
);

CREATE INDEX IF NOT EXISTS studio_login_tokens_email_created_idx
    ON studio_login_tokens (email, created_at DESC);
CREATE INDEX IF NOT EXISTS studio_login_tokens_ip_created_idx
    ON studio_login_tokens (request_ip, created_at DESC);
