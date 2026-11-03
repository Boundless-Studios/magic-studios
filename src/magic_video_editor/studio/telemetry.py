"""Durable bounded GPU telemetry snapshots."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
import asyncpg

from magic_video_editor.contracts import GpuTelemetry
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK


class MemoryGpuTelemetryStore:
    def __init__(self, *, ttl: timedelta = timedelta(seconds=30)) -> None:
        self._snapshots: dict[str, GpuTelemetry] = {}
        self._ttl = ttl

    async def save(self, project_id: str, telemetry: GpuTelemetry) -> None:
        existing = self._snapshots.get(project_id)
        if existing is not None and existing.sampled_at > telemetry.sampled_at:
            return
        self._snapshots[project_id] = replace(telemetry, stale=False)

    async def get(
        self, project_id: str, *, now: datetime | None = None
    ) -> GpuTelemetry | None:
        snapshot = self._snapshots.get(project_id)
        if snapshot is None:
            return None
        current = now or datetime.now(UTC)
        return replace(snapshot, stale=current - snapshot.sampled_at > self._ttl)


class PostgresGpuTelemetryStore:
    def __init__(self, database_url: str, *, ttl: timedelta = timedelta(seconds=30)) -> None:
        self._database_url = database_url
        self._pool: object | None = None
        self._ttl = ttl

    async def start(self) -> None:
        pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=3)
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1))",
                        STUDIO_SCHEMA_ADVISORY_LOCK,
                    )
                    migrations = files("magic_video_editor.migrations")
                    await connection.execute(migrations.joinpath("002_studio_manifests.sql").read_text())
                    await connection.execute(migrations.joinpath("006_gpu_telemetry.sql").read_text())
                    await connection.execute(migrations.joinpath("007_gpu_telemetry_constraints.sql").read_text())
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()  # type: ignore[attr-defined]
            self._pool = None

    def _require_pool(self) -> object:
        if self._pool is None:
            raise RuntimeError("GPU telemetry store has not started")
        return self._pool

    async def save(self, project_id: str, telemetry: GpuTelemetry) -> None:
        async with self._require_pool().acquire() as connection:  # type: ignore[attr-defined]
            await connection.execute(
                """INSERT INTO studio_gpu_telemetry
                   (project_id, provider, worker_healthy, device_name, vram_used_mb,
                    vram_total_mb, vram_reserved_mb, utilization_percent,
                    active_operation_count, sampled_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT (project_id) DO UPDATE SET provider=EXCLUDED.provider,
                    worker_healthy=EXCLUDED.worker_healthy, device_name=EXCLUDED.device_name,
                    vram_used_mb=EXCLUDED.vram_used_mb, vram_total_mb=EXCLUDED.vram_total_mb,
                    vram_reserved_mb=EXCLUDED.vram_reserved_mb,
                    utilization_percent=EXCLUDED.utilization_percent,
                    active_operation_count=EXCLUDED.active_operation_count,
                    sampled_at=EXCLUDED.sampled_at
                   WHERE studio_gpu_telemetry.sampled_at <= EXCLUDED.sampled_at""",
                project_id, telemetry.provider, telemetry.worker_healthy,
                telemetry.device_name, telemetry.vram_used_mb, telemetry.vram_total_mb,
                telemetry.vram_reserved_mb, telemetry.utilization_percent,
                telemetry.active_operation_count, telemetry.sampled_at,
            )

    async def get(self, project_id: str) -> GpuTelemetry | None:
        async with self._require_pool().acquire() as connection:  # type: ignore[attr-defined]
            row = await connection.fetchrow(
                "SELECT * FROM studio_gpu_telemetry WHERE project_id=$1", project_id
            )
        if row is None:
            return None
        sampled_at = row["sampled_at"]
        return GpuTelemetry(
            provider=row["provider"], worker_healthy=row["worker_healthy"],
            device_name=row["device_name"], vram_used_mb=row["vram_used_mb"],
            vram_total_mb=row["vram_total_mb"], vram_reserved_mb=row["vram_reserved_mb"],
            utilization_percent=row["utilization_percent"],
            active_operation_count=row["active_operation_count"], sampled_at=sampled_at,
            stale=datetime.now(UTC) - sampled_at > self._ttl,
        )
