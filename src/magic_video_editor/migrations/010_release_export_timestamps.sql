-- studio_releases already carries created_at (migration 002). studio_exports
-- needs completed_at (nullable -- set when the export becomes ready) plus an
-- index so listing a release's exports newest-first doesn't full-scan.
ALTER TABLE studio_exports
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS studio_exports_release_id_created_at_idx
    ON studio_exports (release_id, created_at DESC);
