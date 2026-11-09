CREATE TABLE IF NOT EXISTS assets_3d (
 asset_id text PRIMARY KEY, owner_id text NOT NULL, current_revision integer NOT NULL CHECK(current_revision>0), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assets_3d_owner ON assets_3d(owner_id);
CREATE TABLE IF NOT EXISTS asset_3d_revisions (
 asset_id text NOT NULL REFERENCES assets_3d(asset_id), revision integer NOT NULL CHECK(revision>0), snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(asset_id,revision)
);

CREATE TABLE IF NOT EXISTS asset_3d_usage (
 asset_key text PRIMARY KEY, asset_id text NOT NULL REFERENCES assets_3d(asset_id), size_bytes bigint NOT NULL CHECK(size_bytes >= 0)
);
CREATE INDEX IF NOT EXISTS asset_3d_usage_asset ON asset_3d_usage(asset_id);
