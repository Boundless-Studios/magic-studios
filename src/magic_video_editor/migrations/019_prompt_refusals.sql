-- Prompts refused by acceptable-use screening. Kept (with the text) to
-- investigate abuse, deliberately outside account deletion, and purged after a
-- year by the retention sweep.
CREATE TABLE IF NOT EXISTS studio_prompt_refusals (
    refusal_id text PRIMARY KEY,
    owner_id text NOT NULL,
    surface text NOT NULL,
    category text NOT NULL,
    reason text NOT NULL,
    prompt_text text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_prompt_refusals_owner
    ON studio_prompt_refusals (owner_id, created_at);
CREATE INDEX IF NOT EXISTS studio_prompt_refusals_created
    ON studio_prompt_refusals (created_at);
