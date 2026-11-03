import json
from importlib.resources import files

import pytest

from magic_video_editor.contracts import (
    InvalidActivityCursor,
    JobLeaseActive,
    JobOperation,
    JobSubmission,
    SpriteSheetMetadata,
    VideoJobResult,
)
from magic_video_editor.plugins.postgres import PostgresJobStore


class _Connection:
    def __init__(self) -> None:
        self.fetchrow_result = {
            "job_id": "00000000-0000-0000-0000-000000000001",
            "source_key": "sources/input.mp4", "output_key": "outputs/job-1.mp4",
            "attempts": 2, "claim_token": "token-1",
            "requested_outputs": {"alpha": False, "sprite": True},
            "operation": "package", "operation_version": 1,
            "project_id": None, "manifest_operation_id": None, "payload": {},
        }
        self.execute_result = "UPDATE 1"
        self.status_result = "completed"
        self.last_args = ()
        self.last_query = ""

    async def fetchrow(self, query: str, *args):
        self.last_query, self.last_args = query, args
        return self.fetchrow_result

    async def execute(self, query: str, *args):
        self.last_query, self.last_args = query, args
        return self.execute_result

    async def fetchval(self, query: str, *args):
        self.last_query, self.last_args = query, args
        return self.status_result


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


async def test_submit_is_idempotent_on_client_key() -> None:
    connection = _Connection()
    connection.fetchrow_result = {
        "job_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "request-1", "operation": "package", "status": "queued",
        "source_key": "sources/input.mp4", "output_key": "outputs/job-1.mp4",
        "attempts": 0, "result": None, "error": None,
        "requested_outputs": {"alpha": True},
        "operation_version": 1, "project_id": None,
        "manifest_operation_id": None, "payload": {},
        "created": True,
    }
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    record = await store.submit(JobSubmission(
        job_id="00000000-0000-0000-0000-000000000001", idempotency_key="request-1",
        operation="package", source_key="sources/input.mp4",
        output_key="outputs/job-1.mp4", requested_outputs={"alpha": True},
    ))

    assert record.created is True
    assert record.record.idempotency_key == "request-1"
    assert "ON CONFLICT (idempotency_key)" in connection.last_query


async def test_submit_persists_versioned_motion_operation_payload() -> None:
    connection = _Connection()
    connection.fetchrow_result = {
        "job_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "project-1:motion-1:attempt-1",
        "operation": "generate_motion", "status": "queued",
        "source_key": "sources/studio/project-1/subjects/canonical.png",
        "output_key": "outputs/studio/project-1/motions/motion-1/operation-1.mp4",
        "attempts": 0, "result": None, "error": None, "created": True,
        "requested_outputs": {},
        "operation_version": 1, "project_id": "project-1",
        "manifest_operation_id": "operation-1", "payload": {
            "subject_key": "sources/studio/project-1/subjects/canonical.png",
            "subject_artifact_key": "sources/studio/project-1/subjects/canonical.png",
            "motion_id": "motion-1",
        },
    }
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    submitted = await store.submit(JobSubmission(
        job_id="00000000-0000-0000-0000-000000000001",
        idempotency_key="project-1:motion-1:attempt-1",
        operation=JobOperation.GENERATE_MOTION,
        source_key="sources/studio/project-1/subjects/canonical.png",
        output_key="outputs/studio/project-1/motions/motion-1/operation-1.mp4",
        requested_outputs={},
        operation_version=1,
        project_id="project-1",
        manifest_operation_id="operation-1",
        payload={
                "subject_key": "sources/studio/project-1/subjects/canonical.png",
                "subject_artifact_key": "sources/studio/project-1/subjects/canonical.png",
                "motion_id": "motion-1",
        },
    ))

    assert submitted.record.operation is JobOperation.GENERATE_MOTION
    assert submitted.record.payload["motion_id"] == "motion-1"
    assert connection.last_args[6:9] == (1, "project-1", "operation-1")
    assert json.loads(connection.last_args[9])["motion_id"] == "motion-1"


async def test_claim_returns_durable_attempt_and_fencing_token() -> None:
    connection = _Connection()
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    job = await store.claim("00000000-0000-0000-0000-000000000001")

    assert job is not None
    assert job.attempts == 2
    assert job.claim_token == "token-1"
    assert job.requested_outputs == {"alpha": False, "sprite": True}
    assert job.operation is JobOperation.PACKAGE
    assert job.operation_version == 1
    assert "UPDATE video_jobs" in connection.last_query


async def test_completion_is_conditioned_on_live_claim_token() -> None:
    connection = _Connection()
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    accepted = await store.complete(
        "00000000-0000-0000-0000-000000000001", "token-1",
        VideoJobResult(
            output_key="outputs/version.mp4",
            sprite_metadata=SpriteSheetMetadata(
                frame_count=32,
                grid_columns=8,
                grid_rows=4,
                frame_width=512,
                frame_height=512,
                frame_duration_ms=140,
            ),
        ),
    )

    assert accepted is True
    assert "status = 'processing' AND claim_token = $2" in connection.last_query
    persisted = json.loads(connection.last_args[2])
    assert persisted["output_key"] == "outputs/version.mp4"
    assert persisted["sprite_metadata"] == {
        "frame_count": 32,
        "grid_columns": 8,
        "grid_rows": 4,
        "frame_width": 512,
        "frame_height": 512,
        "frame_duration_ms": 140,
    }


@pytest.mark.parametrize("value", [None, 0, 100])
async def test_progress_update_is_claim_and_status_fenced(value: int | None) -> None:
    connection = _Connection()
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    assert await store.update_progress(
        "00000000-0000-0000-0000-000000000001", "token-1", value
    )
    assert "claim_token=$2 AND status='processing'" in connection.last_query
    assert connection.last_args[1:] == ("token-1", value)


async def test_progress_update_rejects_stale_claim() -> None:
    connection = _Connection()
    connection.execute_result = "UPDATE 0"
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))
    assert not await store.update_progress(
        "00000000-0000-0000-0000-000000000001", "stale", 50
    )


@pytest.mark.parametrize("status", ["processing", "queued"])
async def test_nonterminal_claim_miss_remains_retryable(status: str) -> None:
    connection = _Connection()
    connection.fetchrow_result = None
    connection.status_result = status
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(JobLeaseActive):
        await store.claim("00000000-0000-0000-0000-000000000001")


async def test_terminal_or_missing_job_is_not_claimed() -> None:
    connection = _Connection()
    connection.fetchrow_result = None
    connection.status_result = "cancelled"
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    assert await store.claim("00000000-0000-0000-0000-000000000001") is None


def test_operation_migration_enforces_version_and_object_payload_constraints() -> None:
    migration = files("magic_video_editor.migrations").joinpath(
        "003_operation_jobs.sql"
    ).read_text()

    assert "CHECK (operation_version > 0)" in migration
    assert "CHECK (jsonb_typeof(payload) = 'object')" in migration
    assert "conrelid = 'video_jobs'::regclass" in migration
    assert "public.video_jobs" not in migration


def test_job_migrations_use_a_serializing_advisory_lock() -> None:
    source = files("magic_video_editor").joinpath("plugins/postgres.py").read_text()

    assert "pg_advisory_lock" in source
    assert "pg_advisory_unlock" in source


@pytest.mark.parametrize(
    "job_id",
    [
        "job-1",
        # `UUID` accepts these spellings; asyncpg does not, and the store
        # never issues them.
        "{00000000-0000-0000-0000-000000000001}",
        "urn:uuid:00000000-0000-0000-0000-000000000001",
        "00000000000000000000000000000001",
    ],
)
async def test_an_activity_cursor_without_a_canonical_uuid_is_refused_before_querying(
    job_id: str,
) -> None:
    connection = _Connection()
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(InvalidActivityCursor):
        await store.list_for_project(
            "project-1", cursor=f"2026-10-21T00:00:00+00:00|{job_id}", limit=5
        )
    assert connection.last_query == ""


async def test_an_issued_activity_cursor_reaches_the_query() -> None:
    class _Listing(_Connection):
        async def fetch(self, query: str, *args):
            self.last_query, self.last_args = query, args
            return []

    connection = _Listing()
    store = PostgresJobStore("postgresql://unused", pool=_Pool(connection))

    await store.list_for_project(
        "project-1",
        cursor="2026-10-21T00:00:00+00:00|00000000-0000-0000-0000-000000000001",
        limit=5,
    )

    assert connection.last_args[2] == "00000000-0000-0000-0000-000000000001"
