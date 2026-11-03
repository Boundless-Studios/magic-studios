CREATE TABLE IF NOT EXISTS studio_projects (
    project_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS studio_manifest_revisions (
    project_id TEXT NOT NULL REFERENCES studio_projects(project_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    manifest_digest TEXT NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, revision)
);

CREATE TABLE IF NOT EXISTS studio_releases (
    release_id UUID PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL,
    release_digest TEXT,
    release_digest_version SMALLINT,
    release_signed_payload TEXT,
    manifest_snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name),
    FOREIGN KEY (project_id, source_revision)
        REFERENCES studio_manifest_revisions(project_id, revision)
);

ALTER TABLE studio_releases
    ADD COLUMN IF NOT EXISTS release_digest TEXT;
ALTER TABLE studio_releases
    ADD COLUMN IF NOT EXISTS release_digest_version SMALLINT;
ALTER TABLE studio_releases
    ADD COLUMN IF NOT EXISTS release_signed_payload TEXT;

CREATE TABLE IF NOT EXISTS studio_exports (
    export_id TEXT PRIMARY KEY,
    release_id UUID NOT NULL REFERENCES studio_releases(release_id),
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    artifact_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
