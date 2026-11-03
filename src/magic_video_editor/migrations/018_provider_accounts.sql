-- Bring-your-own-key generation providers. A credential is stored only as
-- ciphertext under the service's provider-credential secret; the plaintext is
-- never returned. One credential per owner and provider.
CREATE TABLE IF NOT EXISTS studio_provider_credentials (
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    ciphertext BYTEA NOT NULL,
    key_hint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, provider)
);

-- The provider's own request ID for a ledger job. The row is reserved before
-- the provider is called and the ID recorded after it answers, so a job whose
-- submission outcome is unknown is visible and is failed, never resubmitted.
CREATE TABLE IF NOT EXISTS studio_provider_requests (
    job_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    request_id TEXT,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at TIMESTAMPTZ
);
