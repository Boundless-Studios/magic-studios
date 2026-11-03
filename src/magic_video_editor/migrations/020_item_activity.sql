-- When each project or movie was last opened by its owner, for inactivity
-- expiry. Rows for deleted items are dropped by the next retention sweep.
-- expiring_since is set while the sweep deletes the item; owner access is
-- refused meanwhile, so nothing can be opened underneath the deletion.
CREATE TABLE IF NOT EXISTS studio_item_activity (
    kind text NOT NULL CHECK (kind IN ('project', 'movie')),
    item_id text NOT NULL,
    owner_id text NOT NULL,
    last_active_at timestamptz NOT NULL,
    expiring_since timestamptz,
    PRIMARY KEY (kind, item_id)
);
ALTER TABLE studio_item_activity ADD COLUMN IF NOT EXISTS expiring_since timestamptz;
