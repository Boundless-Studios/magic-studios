import json
from dataclasses import asdict
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from magic_video_editor.contracts import (
    IdempotencyConflict,
    InvalidActivityCursor,
    JobFailureCode,
    JobLeaseActive,
    JobOperation,
    JobRecord,
    JobStatus,
    JobSubmission,
    SpriteSheetMetadata,
    SubmissionResult,
    VideoJob,
    VideoJobResult,
    decode_job_activity_cursor,
    validate_job_submission,
)


class PostgresJobStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._owns_pool = False

    async def start(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._database_url, min_size=1, max_size=4
            )
            self._owns_pool = True
        async with self._pool.acquire() as connection:
            migrations = files("magic_video_editor.migrations")
            await connection.execute(
                "SELECT pg_advisory_lock(hashtext('magic_video_editor.video_jobs.migrations'))"
            )
            try:
                await connection.execute(
                    migrations.joinpath("001_video_jobs.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("003_operation_jobs.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("004_job_public_failure.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("005_job_operation_visibility.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("006_worker_pool.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("007_pool_moves_to_library.sql").read_text()
                )
                await connection.execute(
                    migrations.joinpath("011_job_settlement_note.sql").read_text()
                )
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtext('magic_video_editor.video_jobs.migrations'))"
                )

    async def stop(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
            self._owns_pool = False

    async def submit(self, submission: JobSubmission) -> SubmissionResult:
        validate_job_submission(submission)
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO video_jobs (
                    job_id, idempotency_key, operation, source_key, output_key,
                    requested_outputs, operation_version, project_id,
                    manifest_operation_id, payload, attempt_budget
                )
                VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb,
                        $11)
                ON CONFLICT (idempotency_key) DO UPDATE
                    SET idempotency_key = EXCLUDED.idempotency_key
                    WHERE video_jobs.operation = EXCLUDED.operation
                      AND video_jobs.source_key = EXCLUDED.source_key
                      AND video_jobs.output_key = EXCLUDED.output_key
                      AND video_jobs.requested_outputs = EXCLUDED.requested_outputs
                      AND video_jobs.operation_version = EXCLUDED.operation_version
                      AND video_jobs.project_id IS NOT DISTINCT FROM EXCLUDED.project_id
                      AND video_jobs.manifest_operation_id IS NOT DISTINCT FROM EXCLUDED.manifest_operation_id
                      AND video_jobs.payload = EXCLUDED.payload
                      AND video_jobs.attempt_budget = EXCLUDED.attempt_budget
                RETURNING *, (xmax = 0) AS created
                """,
                submission.job_id,
                submission.idempotency_key,
                submission.operation,
                submission.source_key,
                submission.output_key,
                json.dumps(submission.requested_outputs),
                submission.operation_version,
                submission.project_id,
                submission.manifest_operation_id,
                json.dumps(submission.payload),
                submission.attempt_budget,
            )
        if row is None:
            raise IdempotencyConflict(submission.idempotency_key)
        return SubmissionResult(record=_record(row), created=row["created"])

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM video_jobs WHERE job_id = $1::uuid", job_id
            )
        return _record(row) if row is not None else None

    async def delete_for_project(self, project_id: str) -> int:
        async with self._require_pool().acquire() as connection:
            status = await connection.execute(
                "DELETE FROM video_jobs WHERE project_id = $1", project_id
            )
        return int(status.rsplit(" ", 1)[-1])

    async def list_for_project(
        self, project_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> tuple[JobRecord, ...]:
        if limit is None:
            async with self._require_pool().acquire() as connection:
                rows = await connection.fetch(
                    """SELECT * FROM video_jobs WHERE project_id = $1
                       ORDER BY created_at DESC, job_id DESC LIMIT 100""",
                    project_id,
                )
            return tuple(reversed([_record(row) for row in rows]))
        if cursor is not None:
            cursor_created_at, cursor_job_id = decode_job_activity_cursor(cursor)
            try:
                # Only the canonical form this store issues; asyncpg rejects
                # braced and URN spellings that `UUID` accepts.
                if str(UUID(cursor_job_id)) != cursor_job_id:
                    raise ValueError("job id is not a canonical UUID")
            except ValueError as exc:
                raise InvalidActivityCursor("activity cursor is malformed") from exc
        async with self._require_pool().acquire() as connection:
            if cursor is None:
                rows = await connection.fetch(
                    """SELECT * FROM video_jobs WHERE project_id = $1
                       ORDER BY created_at DESC, job_id DESC LIMIT $2""",
                    project_id,
                    limit,
                )
            else:
                rows = await connection.fetch(
                    """SELECT * FROM video_jobs WHERE project_id = $1
                       AND (created_at, job_id) < ($2, $3::uuid)
                       ORDER BY created_at DESC, job_id DESC LIMIT $4""",
                    project_id,
                    cursor_created_at,
                    cursor_job_id,
                    limit,
                )
        return tuple(_record(row) for row in rows)

    async def list_integration_relevant_for_project(
        self, project_id: str
    ) -> tuple[JobRecord, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """SELECT * FROM video_jobs
                   WHERE project_id = $1
                     AND status IN ('queued', 'processing', 'completed')
                   ORDER BY created_at, job_id""",
                project_id,
            )
        return tuple(_record(row) for row in rows)

    async def list_terminal_history_for_project(
        self, project_id: str, limit: int
    ) -> tuple[JobRecord, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """SELECT * FROM video_jobs
                   WHERE project_id = $1 AND status IN ('failed', 'cancelled')
                   ORDER BY created_at DESC, job_id DESC LIMIT $2""",
                project_id,
                limit,
            )
        return tuple(reversed([_record(row) for row in rows]))

    async def claim(self, job_id: str) -> VideoJob | None:
        token = uuid4().hex
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE video_jobs SET status = 'processing', attempts = attempts + 1,
                    claim_token = $2, lease_until = now() + interval '20 minutes', updated_at = now()
                WHERE job_id = $1::uuid
                  AND (status = 'queued' OR (status = 'processing' AND lease_until < now()))
                RETURNING job_id::text, source_key, output_key, attempts,
                          claim_token, requested_outputs, operation,
                          operation_version, project_id, manifest_operation_id,
                          payload, idempotency_key, attempt_budget
                """,
                job_id,
                token,
            )
            if row is None:
                status = await connection.fetchval(
                    "SELECT status FROM video_jobs WHERE job_id = $1::uuid",
                    job_id,
                )
                if status in {"queued", "processing"}:
                    raise JobLeaseActive(job_id)
        if row is None:
            return None
        return _video_job(row)

    async def cancel(self, job_id: str) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = 'cancelled', cancelled_at = now(), claim_token = NULL,
                   lease_until = NULL, retryable = TRUE, updated_at = now()
               WHERE job_id = $1::uuid AND status IN ('queued', 'processing')""",
            job_id,
        )

    async def complete(
        self, job_id: str, claim_token: str, result: VideoJobResult
    ) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = 'completed', result = $3::jsonb, error = NULL,
                   claim_token = NULL, lease_until = NULL, updated_at = now()
               WHERE job_id = $1::uuid AND status = 'processing' AND claim_token = $2""",
            job_id,
            claim_token,
            json.dumps(asdict(result)),
        )

    async def release(self, job_id: str, claim_token: str, reason: str) -> bool:
        return await self._settle("queued", job_id, claim_token, reason)

    async def fail(
        self, job_id: str, claim_token: str, reason: str, *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = 'failed', error = $3,
                   failure_code = $4, failure_message = $5, retryable = $6,
                   claim_token = NULL, lease_until = NULL, updated_at = now()
               WHERE job_id = $1::uuid AND status = 'processing' AND claim_token = $2""",
            job_id, claim_token, reason, failure_code, failure_message, retryable,
        )

    async def complete_unclaimed(self, job_id: str, result: VideoJobResult) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = 'completed', result = $2::jsonb, error = NULL,
                   claim_token = NULL, lease_until = NULL, updated_at = now()
               WHERE job_id = $1::uuid AND status IN ('queued', 'processing')
                 AND claim_token IS NULL""",
            job_id,
            json.dumps(asdict(result)),
        )

    async def fail_unclaimed(
        self,
        job_id: str,
        reason: str,
        *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = 'failed', error = $2,
                   failure_code = $3, failure_message = $4, retryable = $5,
                   claim_token = NULL, lease_until = NULL, updated_at = now()
               WHERE job_id = $1::uuid AND status IN ('queued', 'processing')
                 AND claim_token IS NULL""",
            job_id,
            reason,
            failure_code,
            failure_message,
            retryable,
        )

    async def _settle(self, status: str, job_id: str, token: str, reason: str) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET status = $3, error = $4, claim_token = NULL,
                   lease_until = NULL, updated_at = now()
               WHERE job_id = $1::uuid AND status = 'processing' AND claim_token = $2""",
            job_id,
            token,
            status,
            reason,
        )

    async def _execute(self, query: str, *args: Any) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(query, *args)
        return result == "UPDATE 1"

    async def update_progress(
        self, job_id: str, claim_token: str, progress_percent: int | None
    ) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET progress_percent=$3, updated_at=now()
               WHERE job_id=$1::uuid AND claim_token=$2 AND status='processing'""",
            job_id, claim_token, progress_percent,
        )

    async def note_settlement(self, job_id: str, note: str | None) -> bool:
        return await self._execute(
            """UPDATE video_jobs SET settlement_note=$2, updated_at=now()
               WHERE job_id=$1::uuid AND status='completed'""",
            job_id, note,
        )

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL job store has not started")
        return self._pool


def _video_job(row: Any) -> VideoJob:
    values = dict(row)
    values["requested_outputs"] = _json_object(values["requested_outputs"])
    values["operation"] = JobOperation(values["operation"])
    values["payload"] = _json_object(values["payload"])
    return VideoJob(**values)


def _record(row: Any) -> JobRecord:
    raw_result = row["result"]
    if isinstance(raw_result, str):
        raw_result = json.loads(raw_result)
    return JobRecord(
        job_id=str(row["job_id"]),
        idempotency_key=row["idempotency_key"],
        operation=JobOperation(row["operation"]),
        status=JobStatus(row["status"]),
        source_key=row["source_key"],
        output_key=row["output_key"],
        attempts=row["attempts"],
        requested_outputs=_json_object(row["requested_outputs"]),
        operation_version=row["operation_version"],
        project_id=row["project_id"],
        manifest_operation_id=row["manifest_operation_id"],
        payload=_json_object(row["payload"]),
        result=_video_job_result(raw_result),
        error=row["error"],
        progress_percent=row.get("progress_percent"),
        failure_code=(
            JobFailureCode(row["failure_code"]) if row.get("failure_code") else None
        ),
        failure_message=row.get("failure_message"),
        retryable=bool(row.get("retryable", False)),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        attempt_budget=row.get("attempt_budget", 5),
        settlement_note=row.get("settlement_note"),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _video_job_result(raw_result: dict[str, Any] | None) -> VideoJobResult | None:
    if not raw_result:
        return None
    values = dict(raw_result)
    metadata = values.get("sprite_metadata")
    if metadata is not None:
        values["sprite_metadata"] = SpriteSheetMetadata(**metadata)
    return VideoJobResult(**values)
