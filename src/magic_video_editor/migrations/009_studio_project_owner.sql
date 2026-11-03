-- Record the creating actor durably on studio_projects, independent of the
-- manifest JSON's owner_id, so the project home list can attribute a project
-- without loading its manifest. Existing rows predate this column and stay
-- NULL -- no owner is invented for them.
ALTER TABLE studio_projects
    ADD COLUMN IF NOT EXISTS created_by TEXT;
