CREATE TABLE IF NOT EXISTS studio_gpu_telemetry (
    project_id text PRIMARY KEY,
    provider text NOT NULL,
    worker_healthy boolean NOT NULL,
    device_name text,
    vram_used_mb integer CHECK (vram_used_mb IS NULL OR vram_used_mb >= 0),
    vram_total_mb integer CHECK (vram_total_mb IS NULL OR vram_total_mb > 0),
    vram_reserved_mb integer CHECK (vram_reserved_mb IS NULL OR vram_reserved_mb >= 0),
    utilization_percent integer CHECK (utilization_percent IS NULL OR utilization_percent BETWEEN 0 AND 100),
    active_operation_count integer NOT NULL CHECK (active_operation_count >= 0),
    sampled_at timestamptz NOT NULL
);

DO $$ BEGIN
    ALTER TABLE studio_gpu_telemetry ADD CONSTRAINT studio_gpu_telemetry_project_fk
        FOREIGN KEY (project_id) REFERENCES studio_projects(project_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
