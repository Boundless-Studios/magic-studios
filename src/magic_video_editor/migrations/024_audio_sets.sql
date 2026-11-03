CREATE TABLE IF NOT EXISTS audio_sets (
 set_id text PRIMARY KEY, owner_id text NOT NULL, current_revision integer NOT NULL CHECK(current_revision>0), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audio_sets_owner ON audio_sets(owner_id);
CREATE TABLE IF NOT EXISTS audio_set_revisions (
 set_id text NOT NULL REFERENCES audio_sets(set_id), revision integer NOT NULL CHECK(revision>0), snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(set_id,revision)
);

CREATE TABLE IF NOT EXISTS audio_asset_usage (
 asset_key text PRIMARY KEY, set_id text NOT NULL REFERENCES audio_sets(set_id), size_bytes bigint NOT NULL CHECK(size_bytes >= 0)
);
CREATE INDEX IF NOT EXISTS audio_asset_usage_set ON audio_asset_usage(set_id);
