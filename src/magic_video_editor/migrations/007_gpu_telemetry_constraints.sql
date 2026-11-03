DELETE FROM studio_gpu_telemetry
WHERE vram_total_mb IS NOT NULL
  AND (
      (vram_used_mb IS NOT NULL AND vram_used_mb > vram_total_mb)
      OR (vram_reserved_mb IS NOT NULL AND vram_reserved_mb > vram_total_mb)
  );

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'studio_gpu_telemetry_used_within_total'
          AND conrelid = 'studio_gpu_telemetry'::regclass
    ) THEN
        ALTER TABLE studio_gpu_telemetry
            ADD CONSTRAINT studio_gpu_telemetry_used_within_total
            CHECK (vram_total_mb IS NULL OR vram_used_mb IS NULL OR vram_used_mb <= vram_total_mb);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'studio_gpu_telemetry_reserved_within_total'
          AND conrelid = 'studio_gpu_telemetry'::regclass
    ) THEN
        ALTER TABLE studio_gpu_telemetry
            ADD CONSTRAINT studio_gpu_telemetry_reserved_within_total
            CHECK (vram_total_mb IS NULL OR vram_reserved_mb IS NULL OR vram_reserved_mb <= vram_total_mb);
    END IF;
END $$;
