from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from magic_video_editor.contracts import (
    AssetDescriptor,
    AssetNotFound,
    AssetTooLarge,
    IdempotencyConflict,
    JobFailureCode,
    JobRecord,
    JobStatus,
    JobSubmission,
    Runtime,
    SubmissionResult,
    VideoDerivatives,
    VideoJob,
    VideoJobResult,
    job_activity_cursor,
    require_deletable_prefix,
    validate_job_submission,
)
from magic_video_editor.plugins.gcs import (
    POOL_INPUT_PREFIXES,
    SIGNED_READ_URL_TTL,
    SIGNED_UPLOAD_URL_TTL,
)

DEFAULT_ASSET_CONTENT_TYPE = "application/octet-stream"


@dataclass
class MemoryJobStore:
    pending: dict[str, VideoJob] = field(default_factory=dict)
    claimed: dict[str, VideoJob] = field(default_factory=dict)
    completed: dict[str, str] = field(default_factory=dict)
    completed_results: dict[str, VideoJobResult] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    fail_next_complete: bool = False
    reject_next_complete: bool = False
    records: dict[str, JobRecord] = field(default_factory=dict)

    async def submit(self, submission: JobSubmission) -> SubmissionResult:
        validate_job_submission(submission)
        for record in self.records.values():
            if record.idempotency_key == submission.idempotency_key:
                if not _same_submission(record, submission):
                    raise IdempotencyConflict(submission.idempotency_key)
                return SubmissionResult(record=record, created=False)
        record = JobRecord(
            job_id=submission.job_id,
            idempotency_key=submission.idempotency_key,
            operation=submission.operation,
            status=JobStatus.QUEUED,
            source_key=submission.source_key,
            output_key=submission.output_key,
            attempts=0,
            requested_outputs=deepcopy(submission.requested_outputs),
            operation_version=submission.operation_version,
            project_id=submission.project_id,
            manifest_operation_id=submission.manifest_operation_id,
            payload=deepcopy(submission.payload),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            attempt_budget=submission.attempt_budget,
        )
        self.records[record.job_id] = record
        self.pending[record.job_id] = VideoJob(
            record.job_id,
            record.source_key,
            record.output_key,
            requested_outputs=deepcopy(submission.requested_outputs),
            operation=submission.operation,
            operation_version=submission.operation_version,
            project_id=submission.project_id,
            manifest_operation_id=submission.manifest_operation_id,
            payload=deepcopy(submission.payload),
            idempotency_key=submission.idempotency_key,
            attempt_budget=submission.attempt_budget,
        )
        return SubmissionResult(record=record, created=True)

    async def get(self, job_id: str) -> JobRecord | None:
        return self.records.get(job_id)

    async def delete_for_project(self, project_id: str) -> int:
        doomed = [job_id for job_id, record in self.records.items() if record.project_id == project_id]
        for job_id in doomed:
            del self.records[job_id]
        return len(doomed)

    async def list_for_project(
        self, project_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> tuple[JobRecord, ...]:
        records = [
            record
            for record in self.records.values()
            if record.project_id == project_id
        ]
        if limit is None:
            return tuple(records)
        records.sort(key=job_activity_cursor, reverse=True)
        if cursor is not None:
            start = next(
                (
                    index + 1
                    for index, record in enumerate(records)
                    if job_activity_cursor(record) == cursor
                ),
                len(records),
            )
            records = records[start:]
        return tuple(records[:limit])

    async def list_integration_relevant_for_project(
        self, project_id: str
    ) -> tuple[JobRecord, ...]:
        return tuple(
            record for record in self.records.values()
            if record.project_id == project_id
            and record.status in {
                JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.COMPLETED
            }
        )

    async def list_terminal_history_for_project(
        self, project_id: str, limit: int
    ) -> tuple[JobRecord, ...]:
        return tuple(
            record for record in self.records.values()
            if record.project_id == project_id
            and record.status in {JobStatus.FAILED, JobStatus.CANCELLED}
        )[-limit:]

    async def cancel(self, job_id: str) -> bool:
        record = self.records.get(job_id)
        if record is None or record.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            return False
        self.pending.pop(job_id, None)
        self.claimed.pop(job_id, None)
        self.records[job_id] = replace(
            record, status=JobStatus.CANCELLED, retryable=True
        )
        return True

    async def claim(self, job_id: str) -> VideoJob | None:
        job = self.pending.pop(job_id, None)
        if job is not None:
            job = replace(
                job,
                attempts=job.attempts + 1,
                claim_token=uuid4().hex,
            )
            self.claimed[job_id] = job
            record = self.records.get(job_id)
            if record is not None:
                self.records[job_id] = replace(
                    record, status=JobStatus.PROCESSING, attempts=job.attempts
                )
        return job

    async def complete(
        self, job_id: str, claim_token: str, result: VideoJobResult
    ) -> bool:
        job = self.claimed.get(job_id)
        if job is None or job.claim_token != claim_token:
            return False
        if self.reject_next_complete:
            self.reject_next_complete = False
            return False
        if self.fail_next_complete:
            self.fail_next_complete = False
            raise OSError("transient completion-store failure")
        self.claimed.pop(job_id, None)
        self.completed[job_id] = result.output_key
        self.completed_results[job_id] = result
        record = self.records.get(job_id)
        if record is not None:
            self.records[job_id] = replace(
                record, status=JobStatus.COMPLETED, result=result
            )
        return True

    async def release(self, job_id: str, claim_token: str, reason: str) -> bool:
        job = self.claimed.get(job_id)
        if job is None or job.claim_token != claim_token:
            return False
        self.claimed.pop(job_id)
        self.pending[job_id] = job
        record = self.records.get(job_id)
        if record is not None:
            self.records[job_id] = replace(
                record, status=JobStatus.QUEUED, attempts=job.attempts, error=reason
            )
        return True

    async def fail(
        self, job_id: str, claim_token: str, reason: str, *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool:
        job = self.claimed.get(job_id)
        if job is None or job.claim_token != claim_token:
            return False
        self.claimed.pop(job_id, None)
        self.failures[job_id] = reason
        record = self.records.get(job_id)
        if record is not None:
            self.records[job_id] = replace(
                record, status=JobStatus.FAILED, attempts=job.attempts, error=reason,
                failure_code=failure_code, failure_message=failure_message,
                retryable=retryable,
            )
        return True

    async def complete_unclaimed(self, job_id: str, result: VideoJobResult) -> bool:
        record = self.records.get(job_id)
        if record is None or record.status not in {
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
        }:
            return False
        self.completed[job_id] = result.output_key
        self.completed_results[job_id] = result
        self.records[job_id] = replace(record, status=JobStatus.COMPLETED, result=result)
        return True

    async def fail_unclaimed(
        self,
        job_id: str,
        reason: str,
        *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool:
        record = self.records.get(job_id)
        if record is None or record.status not in {
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
        }:
            return False
        self.failures[job_id] = reason
        self.records[job_id] = replace(
            record,
            status=JobStatus.FAILED,
            error=reason,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
        )
        return True

    async def update_progress(
        self, job_id: str, claim_token: str, progress_percent: int | None
    ) -> bool:
        job = self.claimed.get(job_id)
        record = self.records.get(job_id)
        if job is None or record is None or job.claim_token != claim_token:
            return False
        self.records[job_id] = replace(
            record, progress_percent=progress_percent, updated_at=datetime.now(UTC)
        )
        return True

    async def note_settlement(self, job_id: str, note: str | None) -> bool:
        record = self.records.get(job_id)
        if record is None:
            return False
        self.records[job_id] = replace(record, settlement_note=note)
        return True


@dataclass
class MemoryAssetStore:
    assets: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    publish_count: int = 0
    # Mirrors the GCS store's bearer-capability lifetimes so tests cover the shape.
    minted_url_ttls: list[tuple[str, timedelta]] = field(default_factory=list)
    # The byte ceiling each upload grant was signed with (None when unlimited).
    upload_limits: dict[str, int | None] = field(default_factory=dict)

    async def create_upload_url(
        self, key: str, content_type: str, *, max_bytes: int | None = None
    ) -> str:
        self.minted_url_ttls.append((key, SIGNED_UPLOAD_URL_TTL))
        self.upload_limits[key] = max_bytes
        return f"memory://upload/{key}"

    async def create_read_url(self, key: str) -> str:
        if not key.startswith(POOL_INPUT_PREFIXES):
            raise ValueError("signed read key must be a pool job input")
        if key not in self.assets:
            raise AssetNotFound(key)
        self.minted_url_ttls.append((key, SIGNED_READ_URL_TTL))
        return f"memory://read/{key}"

    async def create_output_upload_url(self, key: str, content_type: str) -> str:
        if not key.startswith("outputs/"):
            raise ValueError("signed output upload key must be in outputs/")
        self.minted_url_ttls.append((key, SIGNED_UPLOAD_URL_TTL))
        return f"memory://upload/{key}"

    async def describe(self, key: str) -> AssetDescriptor | None:
        content = self.assets.get(key)
        if content is None:
            return None
        return AssetDescriptor(
            key=key,
            size=len(content),
            content_type=self.content_types.get(key, DEFAULT_ASSET_CONTENT_TYPE),
        )

    async def read(self, key: str) -> bytes:
        try:
            return self.assets[key]
        except KeyError as exc:
            raise AssetNotFound(key) from exc

    async def read_limited(self, key: str, max_bytes: int) -> bytes:
        content = await self.read(key)
        if len(content) > max_bytes:
            raise AssetTooLarge(key, max_bytes, len(content))
        return content

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        return (await self.read(key))[start : end + 1]

    async def delete_prefix(self, prefix: str) -> int:
        require_deletable_prefix(prefix)
        doomed = [key for key in list(self.assets) if key.startswith(prefix)]
        for key in doomed:
            del self.assets[key]
            if key in self.content_types:
                del self.content_types[key]
        return len(doomed)

    async def write_once(self, key: str, content: bytes, content_type: str) -> bool:
        if key in self.assets:
            return False
        self.assets[key] = content
        self.content_types[key] = content_type
        self.publish_count += 1
        return True


def _same_submission(record: JobRecord, submission: JobSubmission) -> bool:
    return (
        record.operation is submission.operation
        and record.source_key == submission.source_key
        and record.output_key == submission.output_key
        and record.requested_outputs == submission.requested_outputs
        and record.operation_version == submission.operation_version
        and record.project_id == submission.project_id
        and record.manifest_operation_id == submission.manifest_operation_id
        and record.payload == submission.payload
        and record.attempt_budget == submission.attempt_budget
    )


class PassthroughVideoProcessor:
    async def prepare_source(self, source: bytes) -> bytes:
        return source

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        return VideoDerivatives()


class InMemoryRuntime(Runtime):
    jobs: MemoryJobStore
    assets_store: MemoryAssetStore

    def __init__(
        self,
        jobs: MemoryJobStore,
        assets: MemoryAssetStore,
        *,
        studio_service: object | None = None,
        worker_pool: object | None = None,
        studio_api_keys: object | None = None,
        studio_enrolment: object | None = None,
        extensions: tuple = (),
    ) -> None:
        self.jobs = jobs
        self.assets_store = assets
        super().__init__(
            jobs=jobs,
            assets=assets,
            processor=PassthroughVideoProcessor(),
            studio_service=studio_service,
            worker_pool=worker_pool,
            studio_api_keys=studio_api_keys,
            studio_enrolment=studio_enrolment,
            extensions=extensions,
        )

    @classmethod
    def with_job(cls, job_id: str, source: bytes) -> "InMemoryRuntime":
        source_key = f"inputs/{job_id}.mp4"
        output_key = f"outputs/{job_id}.mp4"
        jobs = MemoryJobStore(
            pending={job_id: VideoJob(job_id, source_key, output_key)}
        )
        return cls(jobs, MemoryAssetStore(assets={source_key: source}))

    @property
    def completed(self) -> dict[str, str]:
        return self.jobs.completed

    @property
    def asset_bytes(self) -> dict[str, bytes]:
        return self.assets_store.assets

    @property
    def publish_count(self) -> int:
        return self.assets_store.publish_count

    @property
    def failures(self) -> dict[str, str]:
        return self.jobs.failures
