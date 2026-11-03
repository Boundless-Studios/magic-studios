from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest

from magic_video_editor.contracts import GpuTelemetry
from magic_video_editor.studio.telemetry import MemoryGpuTelemetryStore


@pytest.mark.asyncio
async def test_telemetry_snapshot_becomes_stale_after_ttl() -> None:
    store = MemoryGpuTelemetryStore(ttl=timedelta(seconds=30))
    sampled = datetime(2026, 8, 31, tzinfo=UTC)
    await store.save("project-1", GpuTelemetry(provider="local", worker_healthy=True, device_name="Simulated GPU", vram_used_mb=512, vram_total_mb=2048, vram_reserved_mb=640, utilization_percent=25, active_operation_count=1, sampled_at=sampled, stale=False))

    fresh = await store.get("project-1", now=sampled + timedelta(seconds=29))
    stale = await store.get("project-1", now=sampled + timedelta(seconds=31))

    assert fresh is not None and fresh.stale is False
    assert stale is not None and stale.stale is True


@pytest.mark.asyncio
async def test_telemetry_snapshot_does_not_regress_to_an_older_sample() -> None:
    store = MemoryGpuTelemetryStore()
    sampled = datetime(2026, 8, 31, tzinfo=UTC)
    newer = GpuTelemetry(provider="local", worker_healthy=True, device_name=None, vram_used_mb=900, vram_total_mb=2048, vram_reserved_mb=1000, utilization_percent=70, active_operation_count=2, sampled_at=sampled + timedelta(seconds=2), stale=False)
    older = GpuTelemetry(provider="local", worker_healthy=True, device_name=None, vram_used_mb=100, vram_total_mb=2048, vram_reserved_mb=200, utilization_percent=10, active_operation_count=0, sampled_at=sampled, stale=False)

    await store.save("project-1", newer)
    await store.save("project-1", older)

    saved = await store.get("project-1", now=sampled + timedelta(seconds=3))
    assert saved is not None
    assert saved.sampled_at == newer.sampled_at
    assert saved.vram_used_mb == 900


def test_postgres_telemetry_migrates_project_dependency_before_snapshot_table() -> None:
    source = files("magic_video_editor").joinpath("studio/telemetry.py").read_text()
    studio = source.index('joinpath("002_studio_manifests.sql")')
    telemetry = source.index('joinpath("006_gpu_telemetry.sql")')
    constraints = source.index('joinpath("007_gpu_telemetry_constraints.sql")')
    assert "pg_advisory_xact_lock" in source
    assert "connection.transaction()" in source
    assert studio < telemetry < constraints
    assert "await pool.close()" in source


def test_postgres_telemetry_upsert_is_monotonic() -> None:
    source = files("magic_video_editor").joinpath("studio/telemetry.py").read_text()
    assert "studio_gpu_telemetry.sampled_at <= EXCLUDED.sampled_at" in source


def test_studio_stores_share_one_schema_lock() -> None:
    postgres = files("magic_video_editor").joinpath("studio/postgres.py").read_text()
    telemetry = files("magic_video_editor").joinpath("studio/telemetry.py").read_text()
    assert "STUDIO_SCHEMA_ADVISORY_LOCK" in postgres
    assert "STUDIO_SCHEMA_ADVISORY_LOCK" in telemetry
    assert "studio-release-digest-v1" not in postgres
    assert "gpu_telemetry.migrations" not in telemetry


def test_telemetry_constraints_upgrade_existing_table_idempotently() -> None:
    migration = files("magic_video_editor.migrations").joinpath(
        "007_gpu_telemetry_constraints.sql"
    ).read_text()
    assert migration.count("conrelid = 'studio_gpu_telemetry'::regclass") == 2
    assert "studio_gpu_telemetry_used_within_total" in migration
    assert "studio_gpu_telemetry_reserved_within_total" in migration
    assert migration.count("IF NOT EXISTS") == 2
    cleanup = migration.index("DELETE FROM studio_gpu_telemetry")
    constraints = migration.index("DO $$ BEGIN")
    assert cleanup < constraints
    assert "vram_total_mb IS NOT NULL" in migration
    assert "vram_used_mb > vram_total_mb" in migration
    assert "vram_reserved_mb > vram_total_mb" in migration
