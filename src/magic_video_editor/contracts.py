import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from outbound_gpu_worker_pool.service import WorkerPoolService

    from magic_video_editor.extensions import Extension
    from magic_video_editor.studio.api_keys import StudioApiKeyStore
    from magic_video_editor.studio.auth import StudioAuthStore
    from magic_video_editor.studio.enrolment import StudioEnrolmentStore
    from magic_video_editor.studio.limits import OwnerUsage
    from magic_video_editor.studio.models import (
        ExportRecord,
        ExternalSubject,
        HistoryEntry,
        MotionPreset,
        MotionSpecification,
        ProjectSummary,
        Release,
        ReleaseDelivery,
    )
    from magic_video_editor.studio.service import ProjectRepairReport, UploadTarget
    from magic_video_editor.studio.store import StoredManifest


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobOperation(StrEnum):
    PACKAGE = "package"
    GENERATE_SUBJECT = "generate_subject"
    GENERATE_MOTION = "generate_motion"
    COMPOSE_FRAME = "compose_frame"
    GENERATE_TURNAROUND = "generate_turnaround"
    ASSESS = "assess"
    DERIVE = "derive"
    EXPORT = "export"


class OperationStage(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    AWAITING_SETTLEMENT = "awaiting_settlement"
    SETTLED = "settled"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PrimaryAction(StrEnum):
    ADD_SUBJECT = "add_subject"
    APPROVE_SUBJECT = "approve_subject"
    ADD_MOTION = "add_motion"
    GENERATE_MOTION = "generate_motion"
    SELECT_CANDIDATES = "select_candidates"
    ASSESS_SET = "assess_set"
    APPROVE_SET = "approve_set"
    CREATE_RELEASE = "create_release"
    EXPORT = "export"


class JobFailureCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TEMPORARY_FAILURE = "temporary_failure"


type JobPayloadValue = (
    str
    | int
    | float
    | bool
    | None
    | list["JobPayloadValue"]
    | dict[str, "JobPayloadValue"]
)
type JobPayload = dict[str, JobPayloadValue]

MAX_JOB_ID_LENGTH = 255
MAX_ASSET_KEY_LENGTH = 1024
MAX_JOB_PAYLOAD_BYTES = 16_384
MAX_JOB_PAYLOAD_DEPTH = 8
MAX_REQUESTED_OUTPUTS = 16
MAX_REQUESTED_OUTPUT_KEY_LENGTH = 64
MAX_REQUESTED_OUTPUTS_BYTES = 2_048
MIN_JOB_ATTEMPT_BUDGET = 1
MAX_JOB_ATTEMPT_BUDGET = 20


@dataclass(frozen=True)
class JobSubmission:
    job_id: str
    idempotency_key: str
    operation: JobOperation
    source_key: str
    output_key: str
    requested_outputs: dict[str, bool]
    operation_version: int = 1
    project_id: str | None = None
    manifest_operation_id: str | None = None
    payload: JobPayload = field(default_factory=dict)
    attempt_budget: int = 5


def validate_job_submission(submission: JobSubmission) -> None:
    for label, value, maximum in (
        ("idempotency_key", submission.idempotency_key, MAX_JOB_ID_LENGTH),
        ("project_id", submission.project_id, MAX_JOB_ID_LENGTH),
        ("manifest_operation_id", submission.manifest_operation_id, MAX_JOB_ID_LENGTH),
        ("source_key", submission.source_key, MAX_ASSET_KEY_LENGTH),
        ("output_key", submission.output_key, MAX_ASSET_KEY_LENGTH),
    ):
        if value is not None and (not value or len(value) > maximum):
            raise ValueError(f"{label} must contain 1 to {maximum} characters")
    if submission.operation_version < 1:
        raise ValueError("operation_version must be positive")
    if not (
        MIN_JOB_ATTEMPT_BUDGET
        <= submission.attempt_budget
        <= MAX_JOB_ATTEMPT_BUDGET
    ):
        raise ValueError(
            "attempt_budget must be between "
            f"{MIN_JOB_ATTEMPT_BUDGET} and {MAX_JOB_ATTEMPT_BUDGET}"
        )
    validate_requested_outputs(submission.requested_outputs)
    validate_job_payload(submission.payload)
    if submission.operation is JobOperation.GENERATE_MOTION:
        _validate_motion_submission_provenance(submission)
    if submission.operation is JobOperation.GENERATE_SUBJECT:
        _validate_subject_submission_provenance(submission)
    if submission.operation is JobOperation.COMPOSE_FRAME:
        _validate_compose_frame_submission_provenance(submission)
    if submission.operation is JobOperation.GENERATE_TURNAROUND:
        _validate_turnaround_submission_provenance(submission)


def _validate_subject_submission_provenance(submission: JobSubmission) -> None:
    if submission.project_id is None or submission.manifest_operation_id is None:
        raise ValueError("generate_subject requires project and operation identifiers")
    references = submission.payload.get("reference_keys")
    if not isinstance(references, list) or not references:
        raise ValueError("generate_subject requires reference keys")
    upload_prefix = f"sources/studio/{submission.project_id}/subject-references/"
    revision_prefix = f"outputs/studio/{submission.project_id}/subjects/"
    if any(
        not isinstance(key, str)
        or not key.startswith((upload_prefix, revision_prefix))
        for key in references
    ):
        raise ValueError("generate_subject reference is outside the project namespace")
    if submission.source_key != references[0]:
        raise ValueError("generate_subject source must be the primary reference")
    expected = f"outputs/studio/{submission.project_id}/subjects/{submission.manifest_operation_id}.png"
    if submission.output_key != expected:
        raise ValueError("generate_subject output key is not immutable")


def _validate_compose_frame_submission_provenance(submission: JobSubmission) -> None:
    if submission.project_id is None or submission.manifest_operation_id is None:
        raise ValueError("compose_frame requires project and operation identifiers")
    subject_key = submission.payload.get("subject_key")
    if not isinstance(subject_key, str) or subject_key != submission.source_key:
        raise ValueError("compose_frame subject key must match the ledger source key")
    references = submission.payload.get("reference_keys")
    if not isinstance(references, list) or not references:
        raise ValueError("compose_frame requires reference keys")
    prefixes = (
        f"sources/studio/{submission.project_id}/subjects/",
        f"outputs/studio/{submission.project_id}/subjects/",
    )
    if any(
        not isinstance(key, str) or not key.startswith(prefixes) for key in references
    ):
        raise ValueError("compose_frame reference is outside the project namespace")
    expected = (
        f"outputs/studio/{submission.project_id}/compositions/"
        f"{submission.manifest_operation_id}.png"
    )
    if submission.output_key != expected:
        raise ValueError("compose_frame output key is not immutable")


def _validate_turnaround_submission_provenance(submission: JobSubmission) -> None:
    """A turnaround renders the prepared source frame; it is never a motion."""
    if submission.project_id is None or submission.manifest_operation_id is None:
        raise ValueError("generate_turnaround requires project and operation identifiers")
    turnaround_id = submission.payload.get("turnaround_id")
    if turnaround_id != f"turnaround-{submission.manifest_operation_id}":
        raise ValueError("generate_turnaround identifier must name its operation")
    if "motion_id" in submission.payload:
        raise ValueError("generate_turnaround never carries a motion identifier")
    if submission.payload.get("start_frame_key") != submission.source_key:
        raise ValueError("generate_turnaround source must be its prepared frame")
    if not submission.source_key.startswith(f"outputs/studio/{submission.project_id}/opening-frames/"):
        raise ValueError("generate_turnaround source key is outside the project namespace")
    expected_output = (
        f"outputs/studio/{submission.project_id}/turnarounds/{turnaround_id}/"
        f"{submission.manifest_operation_id}.mp4"
    )
    if submission.output_key != expected_output:
        raise ValueError("generate_turnaround output key is not the immutable operation key")


def _validate_motion_submission_provenance(submission: JobSubmission) -> None:
    if submission.project_id is None or submission.manifest_operation_id is None:
        raise ValueError(
            "generate_motion requires project and manifest operation identifiers"
        )
    subject_key = submission.payload.get("subject_key")
    subject_artifact_key = submission.payload.get("subject_artifact_key")
    motion_id = submission.payload.get("motion_id")
    if not isinstance(subject_key, str) or subject_key != submission.source_key:
        raise ValueError("generate_motion subject key must match the ledger source key")
    if subject_artifact_key != subject_key:
        raise ValueError(
            "generate_motion subject artifact key must match the ledger source key"
        )
    if not isinstance(motion_id, str) or not motion_id:
        raise ValueError("generate_motion requires a motion identifier")
    source_prefixes = (
        f"sources/studio/{submission.project_id}/subjects/",
        f"outputs/studio/{submission.project_id}/subjects/",
    )
    expected_output = (
        f"outputs/studio/{submission.project_id}/motions/{motion_id}/"
        f"{submission.manifest_operation_id}.mp4"
    )
    if not submission.source_key.startswith(source_prefixes):
        raise ValueError("generate_motion source key is outside the project namespace")
    if submission.output_key != expected_output:
        raise ValueError(
            "generate_motion output key is not the immutable operation key"
        )


def validate_job_payload(payload: JobPayload) -> None:
    _validate_json_tree(payload)
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(encoded) > MAX_JOB_PAYLOAD_BYTES:
        raise ValueError(f"job payload exceeds {MAX_JOB_PAYLOAD_BYTES} bytes")


def validate_requested_outputs(requested_outputs: dict[str, bool]) -> None:
    if len(requested_outputs) > MAX_REQUESTED_OUTPUTS:
        raise ValueError(f"requested_outputs exceeds {MAX_REQUESTED_OUTPUTS} entries")
    for key, enabled in requested_outputs.items():
        if not key or len(key) > MAX_REQUESTED_OUTPUT_KEY_LENGTH:
            raise ValueError("requested_outputs key is empty or too long")
        if type(enabled) is not bool:
            raise ValueError("requested_outputs values must be booleans")
    encoded = json.dumps(
        requested_outputs,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(encoded) > MAX_REQUESTED_OUTPUTS_BYTES:
        raise ValueError(
            f"requested_outputs exceeds {MAX_REQUESTED_OUTPUTS_BYTES} bytes"
        )


def _validate_json_tree(root: JobPayloadValue) -> None:
    stack: list[tuple[JobPayloadValue, int]] = [(root, 1)]
    seen_containers: set[int] = set()
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JOB_PAYLOAD_DEPTH:
            raise ValueError(f"job payload exceeds depth {MAX_JOB_PAYLOAD_DEPTH}")
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                raise ValueError("job payload contains a repeated or cyclic container")
            seen_containers.add(identity)
            if any(not isinstance(key, str) for key in value):
                raise ValueError("job payload object keys must be strings")
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                raise ValueError("job payload contains a repeated or cyclic container")
            seen_containers.add(identity)
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("job payload numbers must be finite")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("job payload contains an unsupported value")


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    idempotency_key: str
    operation: JobOperation
    status: JobStatus
    source_key: str
    output_key: str
    attempts: int
    requested_outputs: dict[str, bool] = field(default_factory=dict)
    operation_version: int = 1
    project_id: str | None = None
    manifest_operation_id: str | None = None
    payload: JobPayload = field(default_factory=dict)
    result: "VideoJobResult | None" = None
    error: str | None = None
    progress_percent: int | None = None
    failure_code: JobFailureCode | None = None
    failure_message: str | None = None
    retryable: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    attempt_budget: int = 5
    # Why a completed job has not folded into the manifest yet; never changes status.
    settlement_note: str | None = None


@dataclass(frozen=True)
class SubmissionResult:
    record: JobRecord
    created: bool


@dataclass(frozen=True)
class VideoJob:
    job_id: str
    source_key: str
    output_key: str
    attempts: int = 0
    claim_token: str = ""
    requested_outputs: dict[str, bool] = field(
        default_factory=lambda: {"alpha": True, "sprite": True}
    )
    operation: JobOperation = JobOperation.PACKAGE
    operation_version: int = 1
    project_id: str | None = None
    manifest_operation_id: str | None = None
    payload: JobPayload = field(default_factory=dict)
    idempotency_key: str = ""
    attempt_budget: int = 5


@dataclass(frozen=True)
class SpriteSheetMetadata:
    frame_count: int
    grid_columns: int
    grid_rows: int
    frame_width: int
    frame_height: int
    frame_duration_ms: int


@dataclass(frozen=True)
class VideoJobResult:
    output_key: str
    alpha_video_path: str | None = None
    sprite_sheet_path: str | None = None
    sprite_metadata: SpriteSheetMetadata | None = None
    derivative_error: str | None = None
    execution_provenance: dict[str, JobPayloadValue] = field(default_factory=dict)


def job_activity_cursor(record: JobRecord) -> str:
    """The opaque, durable cursor (T11/T14): derived from stored `created_at` /
    `job_id`, not a row offset, so it stays valid across restarts and across a
    store that paginates past any single-query row cap.
    """
    stamp = record.created_at.isoformat() if record.created_at is not None else ""
    return f"{stamp}|{record.job_id}"


class InvalidActivityCursor(ValueError):
    """A cursor that `job_activity_cursor` could not have produced."""


def decode_job_activity_cursor(cursor: str) -> tuple[datetime | None, str]:
    stamp, separator, job_id = cursor.partition("|")
    try:
        if not separator or not job_id:
            raise ValueError("missing job id")
        created_at = datetime.fromisoformat(stamp) if stamp else None
        # Issued cursors always carry an offset; a naive one cannot be
        # compared with `timestamptz`.
        if created_at is not None and created_at.utcoffset() is None:
            raise ValueError("timestamp has no offset")
    except ValueError as exc:
        raise InvalidActivityCursor("activity cursor is malformed") from exc
    return created_at, job_id


class JobStore(Protocol):
    async def submit(self, submission: JobSubmission) -> SubmissionResult: ...

    async def get(self, job_id: str) -> JobRecord | None: ...

    async def list_for_project(
        self, project_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> tuple[JobRecord, ...]:
        """All (or, when `limit` is given, one newest-first page after `cursor`
        via `job_activity_cursor`) records for a project. `limit=None` is the
        legacy unpaged shape used by callers that need every record.
        """
        ...

    async def list_integration_relevant_for_project(
        self, project_id: str
    ) -> tuple[JobRecord, ...]: ...

    async def list_terminal_history_for_project(
        self, project_id: str, limit: int
    ) -> tuple[JobRecord, ...]: ...

    async def cancel(self, job_id: str) -> bool: ...

    async def delete_for_project(self, project_id: str) -> int:
        """Remove every job record (prompts included) of a deleted project."""
        ...

    async def claim(self, job_id: str) -> VideoJob | None:
        """Claim a job and atomically increment its durable attempt count."""
        ...

    async def complete(
        self, job_id: str, claim_token: str, result: VideoJobResult
    ) -> bool: ...

    async def release(self, job_id: str, claim_token: str, reason: str) -> bool: ...

    async def fail(
        self,
        job_id: str,
        claim_token: str,
        reason: str,
        *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool: ...

    async def update_progress(
        self, job_id: str, claim_token: str, progress_percent: int | None
    ) -> bool: ...

    async def note_settlement(self, job_id: str, note: str | None) -> bool:
        """Record why a completed job could not settle, or clear that note."""
        ...

    async def complete_unclaimed(self, job_id: str, result: VideoJobResult) -> bool:
        """Settle a job the outbound worker pool executed.

        The pool never claims the `video_jobs` row -- Cloud Tasks does -- so a
        pool-executed job sits `queued` with no claim token. The fence here is
        the coordinator's own output attestation, not a claim token; the status
        guard keeps this from ever settling a row the CPU worker holds.
        """
        ...

    async def fail_unclaimed(
        self,
        job_id: str,
        reason: str,
        *,
        failure_code: JobFailureCode = JobFailureCode.TEMPORARY_FAILURE,
        failure_message: str = "Generation failed. Try this motion again.",
        retryable: bool = True,
    ) -> bool: ...


@dataclass(frozen=True)
class AssetDescriptor:
    key: str
    size: int
    content_type: str | None


# A signed PUT can carry a byte ceiling. The client must send this header with
# exactly the value it was granted, and storage refuses a body outside the range.
UPLOAD_SIZE_HEADER = "x-goog-content-length-range"


def upload_size_headers(max_bytes: int | None) -> dict[str, str]:
    """The headers a client sends with a size-limited upload; none when unlimited."""
    return {UPLOAD_SIZE_HEADER: f"0,{max_bytes}"} if max_bytes is not None else {}


# One project's, movie's or audio set's own storage, and nothing wider.
DELETABLE_PREFIX = re.compile(
    r"^(sources/studio|outputs/studio|exports/studio|uploads/movies|outputs/movies|uploads/audio|outputs/audio)"
    r"/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/$"
)


def require_deletable_prefix(prefix: str) -> str:
    if DELETABLE_PREFIX.fullmatch(prefix) is None:
        raise ValueError(f"refusing to delete outside one project or movie: {prefix!r}")
    return prefix


class AssetStore(Protocol):
    async def create_upload_url(
        self, key: str, content_type: str, *, max_bytes: int | None = None
    ) -> str: ...
    async def create_read_url(self, key: str) -> str:
        """Short-lived GET grant for one pool job input. Only the worker pool's
        lease calls it; Studio code never hands a browser a bearer read URL."""
        ...

    async def create_output_upload_url(self, key: str, content_type: str) -> str:
        """Short-lived create-once PUT grant for one exact key under outputs/."""
        ...

    async def describe(self, key: str) -> AssetDescriptor | None:
        """Size and content type of an existing object, or None when absent."""
        ...

    async def read(self, key: str) -> bytes: ...

    """Read immutable bytes or raise AssetNotFound when the key is absent."""

    async def read_limited(self, key: str, max_bytes: int) -> bytes: ...

    """Read at most max_bytes or raise AssetTooLarge/AssetNotFound."""

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        """Bytes ``start`` to ``end`` inclusive, or AssetNotFound. Lets the API
        stream an object to its owner without minting a bearer URL."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Permanently delete every object under one project's or movie's
        prefix, including every stored version; returns how many were deleted.
        Only `DELETABLE_PREFIX` shapes are accepted, so a bug cannot empty the
        bucket."""
        ...

    async def write_once(self, key: str, content: bytes, content_type: str) -> bool:
        """Create an immutable object; False means that exact key already exists."""
        ...


@dataclass(frozen=True)
class VideoDerivatives:
    alpha_webm: bytes | None = None
    sprite_sheet_png: bytes | None = None
    sprite_metadata: SpriteSheetMetadata | None = None


class VideoProcessor(Protocol):
    async def prepare_source(self, source: bytes) -> bytes: ...

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives: ...


@dataclass(frozen=True)
class MotionReferencePayload:
    """One reference image conditioning a GPU generation request (T2)."""

    key: str
    sha256: str
    role: str


@dataclass(frozen=True)
class GpuGenerationRequest:
    source_key: str
    output_key: str
    prompt: str
    width: int
    height: int
    duration_seconds: int
    seed: int
    idempotency_key: str
    motion_id: str
    loop: bool
    preset: str
    steps: int
    provider_parameters: dict[str, bool | int | float | str]
    start_frame_key: str | None = None
    start_frame_sha256: str | None = None
    composition_revision_id: str | None = None
    references: tuple[MotionReferencePayload, ...] = ()


@dataclass(frozen=True)
class GpuGenerationResult:
    output_key: str


class GpuProcessor(Protocol):
    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        """Idempotently create request.output_key once per idempotency_key.

        Replays must return the original immutable artifact, and a different
        request for a used key must be rejected. Billing behavior remains a
        provider responsibility and is not guaranteed by this interface.
        """
        ...


@dataclass(frozen=True)
class GpuOperationProgress:
    percent: int | None
    stage: str


@dataclass(frozen=True)
class GpuTelemetry:
    provider: str
    worker_healthy: bool
    device_name: str | None
    vram_used_mb: int | None
    vram_total_mb: int | None
    vram_reserved_mb: int | None
    utilization_percent: int | None
    active_operation_count: int
    sampled_at: datetime
    stale: bool = False
    operation_progress: dict[str, GpuOperationProgress] = field(default_factory=dict)


class GpuTelemetryProvider(Protocol):
    async def status(self) -> GpuTelemetry | None: ...


class GpuTelemetryStore(Protocol):
    async def save(self, project_id: str, telemetry: GpuTelemetry) -> None: ...
    async def get(self, project_id: str) -> GpuTelemetry | None: ...


@dataclass(frozen=True)
class SubjectGenerationRequest:
    reference_keys: tuple[str, ...]
    output_key: str
    direction: str | None
    idempotency_key: str
    # The project owner, so a pool generator can bind the job to the machines
    # that person enrolled. None keeps the job untenanted, for house machines.
    owner_id: str | None = None


class SubjectGenerator(Protocol):
    async def generate_subject(self, request: SubjectGenerationRequest) -> str:
        """Create one immutable canonical RGBA PNG and return its exact key."""
        ...


@dataclass(frozen=True)
class CompositionGenerationRequest:
    subject_key: str
    reference_keys: tuple[str, ...]
    output_key: str
    prompt: str
    model: str
    idempotency_key: str


class CompositionGenerator(Protocol):
    async def compose_frame(self, request: CompositionGenerationRequest) -> str:
        """Create one immutable canonical RGBA PNG opening-frame still and return its key."""
        ...


class RuntimeLifecycle(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class TerminalVideoError(RuntimeError):
    """A deterministic input or processing failure that must not be retried."""


class JobLeaseActive(RuntimeError):
    """The job is owned by another worker and must be delivered again."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different canonical request."""


class AssetNotFound(FileNotFoundError):
    """The requested immutable asset is absent from storage."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"asset not found: {key}")


class AssetTooLarge(ValueError):
    """The requested immutable asset exceeds a configured byte budget."""

    def __init__(
        self, key: str, max_bytes: int, actual_bytes: int | None = None
    ) -> None:
        self.key = key
        self.max_bytes = max_bytes
        self.actual_bytes = actual_bytes
        super().__init__(f"asset exceeds {max_bytes} bytes: {key}")


class AssetStorageUnavailable(RuntimeError):
    """The asset provider failed transiently or could not be reached."""


class StudioServicePort(Protocol):
    async def create_project(
        self,
        name: str,
        actor_id: str,
        *,
        external_subject: "ExternalSubject | None" = None,
        preset_motions: "tuple[MotionPreset, ...]" = (),
    ) -> "StoredManifest": ...

    async def find_project_for_external_subject(
        self,
        actor_id: str,
        *,
        system: str,
        entity_kind: str,
        entity_id: str,
    ) -> "StoredManifest | None": ...

    async def retry_delivery(
        self, release_id: str, target: str, actor_id: str
    ) -> "ReleaseDelivery": ...

    async def list_release_deliveries(
        self, release_id: str, actor_id: str
    ) -> "tuple[ReleaseDelivery, ...]": ...

    async def owner_usage(self, actor_id: str) -> "OwnerUsage":
        """The actor's storage and concurrency usage against the per-owner caps."""
        ...

    async def get_project(
        self, project_id: str, actor_id: str
    ) -> "StoredManifest | None": ...

    async def repair_project(
        self, project_id: str, actor_id: str
    ) -> "ProjectRepairReport": ...

    async def list_projects(self, actor_id: str) -> "tuple[ProjectSummary, ...]":
        """The actor's own projects, newest activity first (the project home
        list)."""
        ...

    async def get_gpu_telemetry(
        self,
        project_id: str,
        actor_id: str,
    ) -> GpuTelemetry | None: ...

    async def create_subject_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> "UploadTarget": ...

    async def create_subject_reference_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> "UploadTarget": ...

    async def generate_subject(
        self,
        project_id: str,
        expected_revision: int,
        reference_keys: tuple[str, ...],
        direction: str | None,
        request_id: str,
        actor_id: str,
    ) -> JobRecord: ...

    async def settle_subject_generation(
        self,
        project_id: str,
        job_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def create_asset_preview(
        self,
        project_id: str,
        asset_key: str,
        actor_id: str,
    ) -> "UploadTarget": ...

    async def settle_subject_upload(
        self,
        project_id: str,
        expected_revision: int,
        asset_key: str,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def approve_subject(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def add_motion(
        self,
        project_id: str,
        expected_revision: int,
        specification: "MotionSpecification",
        actor_id: str,
    ) -> "StoredManifest": ...

    async def add_motions(
        self,
        project_id: str,
        expected_revision: int,
        specifications: tuple["MotionSpecification", ...],
        actor_id: str,
    ) -> "StoredManifest": ...

    async def update_motion(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        specification: "MotionSpecification",
        actor_id: str,
    ) -> "StoredManifest": ...

    async def create_motion_reference_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> "UploadTarget": ...

    async def generate_motion(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        actor_id: str,
        request_id: str | None = None,
    ) -> JobRecord: ...

    async def generate_motions(
        self,
        project_id: str,
        expected_revision: int,
        motion_ids: tuple[str, ...],
        actor_id: str,
        request_id: str,
    ) -> tuple[tuple[object, ...], tuple[object, ...]]: ...

    async def compose_opening_frame(
        self,
        project_id: str,
        expected_revision: int,
        prompt: str,
        model: object,
        actor_id: str,
        request_id: str,
    ) -> JobRecord: ...

    async def regenerate_composition(
        self,
        project_id: str,
        expected_revision: int,
        prompt: str,
        model: object,
        actor_id: str,
        request_id: str,
    ) -> JobRecord: ...

    async def settle_composition(
        self,
        project_id: str,
        job_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def approve_composition(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def settle_project_motion_candidate(
        self,
        project_id: str,
        motion_id: str,
        job_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def derive_alpha(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        candidate_id: str,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def analyze_candidate_frames(
        self, project_id: str, motion_id: str, candidate_id: str, actor_id: str
    ) -> dict: ...

    async def suggest_candidate_loop(
        self, project_id: str, motion_id: str, candidate_id: str, actor_id: str,
        *, mode: str, target_frame_count: int,
    ) -> dict: ...

    async def download_candidate_sheet(
        self,
        project_id: str,
        motion_id: str,
        candidate_id: str,
        actor_id: str,
    ) -> "UploadTarget": ...

    async def download_candidate_preview(
        self,
        project_id: str,
        motion_id: str,
        candidate_id: str,
        fmt: str,
        actor_id: str,
    ) -> "UploadTarget": ...

    async def create_candidate_upload(
        self,
        project_id: str,
        motion_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> "UploadTarget": ...

    async def settle_candidate_upload(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        asset_key: str,
        actor_id: str,
        frame_count: int | None = None,
    ) -> "StoredManifest": ...

    async def assess_set_quality(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def select_candidate(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        candidate_id: str,
        actor_id: str,
    ) -> "StoredManifest": ...

    async def list_operations(
        self,
        project_id: str,
        actor_id: str,
    ) -> tuple[JobRecord, ...]: ...

    async def list_activity_operations(
        self,
        project_id: str,
        actor_id: str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> object: ...

    async def cancel_motion_operation(
        self,
        project_id: str,
        motion_id: str,
        job_id: str,
        actor_id: str,
    ) -> JobRecord: ...

    async def cancel_all_operations(
        self,
        project_id: str,
        actor_id: str,
    ) -> object: ...

    async def reconcile_project_operations(
        self,
        project_id: str,
        actor_id: str,
    ) -> None: ...

    async def list_project_history(
        self,
        project_id: str,
        actor_id: str,
    ) -> tuple["HistoryEntry", ...]: ...

    async def approve_set(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
        waived_finding_ids: tuple[str, ...] = (),
        waiver_reason: str | None = None,
    ) -> "StoredManifest": ...

    async def create_release(
        self,
        project_id: str,
        expected_revision: int,
        name: str,
        actor_id: str,
    ) -> "Release": ...

    async def list_releases(
        self,
        project_id: str,
        actor_id: str,
    ) -> tuple["Release", ...]: ...

    async def create_export(
        self,
        release_id: str,
        profile_name: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> "ExportRecord": ...

    async def get_export(self, export_id: str, actor_id: str) -> "ExportRecord": ...

    async def create_export_download(
        self,
        export_id: str,
        actor_id: str,
    ) -> "UploadTarget": ...


@dataclass
class Runtime:
    jobs: JobStore
    assets: AssetStore
    processor: VideoProcessor
    ready: bool = True
    lifecycle: RuntimeLifecycle | None = None
    dispatcher: object | None = None
    gpu: GpuProcessor | None = None
    studio_service: StudioServicePort | None = None
    subject_generator: SubjectGenerator | None = None
    subject_operations_enabled: bool = False
    gpu_telemetry_provider: GpuTelemetryProvider | None = None
    gpu_telemetry_store: GpuTelemetryStore | None = None
    composition_generator: CompositionGenerator | None = None
    composition_operations_enabled: bool = False
    motion_operations_enabled: bool = False
    worker_pool: "WorkerPoolService | None" = None
    studio_api_keys: "StudioApiKeyStore | None" = None
    studio_auth_store: "StudioAuthStore | None" = None
    studio_enrolment: "StudioEnrolmentStore | None" = None
    movie_service: object | None = None
    audio_service: object | None = None
    # The extensions this runtime was built with (see `magic_video_editor.extensions`).
    extensions: "tuple[Extension, ...]" = ()
    # The hourly inactivity sweep, and who may call it: (OIDC audience, the
    # Cloud Scheduler service account's email). Both unset leaves it off.
    retention_sweeper: object | None = None
    retention_sweep_caller: tuple[str, str] | None = None

    @property
    def motion_generation_available(self) -> bool:
        return self.motion_operations_enabled and self.gpu is not None

    @property
    def subject_generation_available(self) -> bool:
        return self.subject_operations_enabled and self.subject_generator is not None

    @property
    def composition_generation_available(self) -> bool:
        return (
            self.composition_operations_enabled
            and self.composition_generator is not None
        )
