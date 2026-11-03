CREATE TABLE IF NOT EXISTS movie_projects (
 movie_id text PRIMARY KEY, owner_id text NOT NULL, current_revision integer NOT NULL CHECK(current_revision>0), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS movie_projects_owner ON movie_projects(owner_id);
CREATE TABLE IF NOT EXISTS movie_revisions (
 movie_id text NOT NULL REFERENCES movie_projects(movie_id), revision integer NOT NULL CHECK(revision>0), snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(movie_id,revision)
);

CREATE TABLE IF NOT EXISTS movie_asset_usage (
 asset_key text PRIMARY KEY, movie_id text NOT NULL REFERENCES movie_projects(movie_id), size_bytes bigint NOT NULL CHECK(size_bytes >= 0)
);
CREATE INDEX IF NOT EXISTS movie_asset_usage_movie ON movie_asset_usage(movie_id);
