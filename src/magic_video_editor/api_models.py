from pathlib import PurePosixPath

from outbound_gpu_worker_pool import (
    MAX_ASSET_KEY_LENGTH,
    MAX_CAPABILITY_ID_LENGTH,
    MAX_EXECUTION_DEADLINE_SECONDS,
    MAX_JOB_ATTEMPT_BUDGET,
    MAX_JOB_ID_LENGTH,
    MAX_JOB_INPUT_KEYS,
    MAX_JOB_PRIORITY,
    MIN_EXECUTION_DEADLINE_SECONDS,
    MIN_JOB_ATTEMPT_BUDGET,
    MIN_JOB_PRIORITY,
    JobFailureCode as PoolJobFailureCode,
    JobPayload as PoolJobPayload,
    JobRecord as PoolJobRecord,
    JobStatus as PoolJobStatus,
    validate_capability_id,
    validate_job_payload as validate_pool_job_payload,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from magic_video_editor.contracts import (
    JobOperation,
    JobPayload,
    JobRecord,
    JobStatus,
    VideoJobResult,
    validate_job_payload,
    validate_requested_outputs,
)
from magic_video_editor.studio.models import (
    CompositionModel,
    MotionReferenceRole,
    StylePreset,
)

POOL_SOURCE_PREFIX = "sources/pool/"
POOL_OUTPUT_PREFIX = "outputs/pool/"
STUDIO_SOURCE_PREFIX = "sources/studio/"


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=255)
    operation: JobOperation
    source_key: str = Field(max_length=1024)
    output_key: str = Field(max_length=1024)
    requested_outputs: dict[str, StrictBool] = Field(default_factory=dict)
    operation_version: int = Field(default=1, ge=1)
    project_id: str | None = Field(default=None, min_length=1, max_length=255)
    manifest_operation_id: str | None = Field(
        default=None, min_length=1, max_length=255
    )
    payload: JobPayload = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: JobPayload) -> JobPayload:
        validate_job_payload(value)
        return value

    @field_validator("requested_outputs")
    @classmethod
    def bounded_requested_outputs(cls, value: dict[str, bool]) -> dict[str, bool]:
        validate_requested_outputs(value)
        return value

    @field_validator("source_key", "output_key")
    @classmethod
    def normalized_key(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or str(path) != value
            or ".." in path.parts
        ):
            raise ValueError("asset key must be a normalized bucket-local path")
        return value


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    operation: JobOperation
    source_key: str
    output_key: str
    attempts: int
    operation_version: int
    project_id: str | None
    manifest_operation_id: str | None
    payload: JobPayload
    result: VideoJobResult | None
    error: str | None

    @classmethod
    def from_record(cls, record: JobRecord) -> "JobResponse":
        return cls(**record.__dict__)


class MotionReferencePayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: MotionReferenceRole

    _normalized_key = field_validator("key")(CreateJobRequest.normalized_key.__func__)


class PoolJobRequest(BaseModel):
    """One unit of work for the outbound GPU worker pool.

    Keys are confined to the pool namespace so a submission can never address a
    studio asset, and every bound is the library's own. A text-to-video job has
    no inputs, so `input_keys` may be empty.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=MAX_JOB_ID_LENGTH)
    capability_id: str = Field(max_length=MAX_CAPABILITY_ID_LENGTH)
    input_keys: list[str] = Field(
        default_factory=list, max_length=MAX_JOB_INPUT_KEYS
    )
    output_key: str = Field(max_length=MAX_ASSET_KEY_LENGTH)
    payload: PoolJobPayload = Field(default_factory=dict)
    priority: int = Field(default=100, ge=MIN_JOB_PRIORITY, le=MAX_JOB_PRIORITY)
    attempt_budget: int = Field(
        default=5, ge=MIN_JOB_ATTEMPT_BUDGET, le=MAX_JOB_ATTEMPT_BUDGET
    )
    execution_deadline_seconds: int = Field(
        default=1200,
        ge=MIN_EXECUTION_DEADLINE_SECONDS,
        le=MAX_EXECUTION_DEADLINE_SECONDS,
    )

    @field_validator("capability_id")
    @classmethod
    def known_capability_id(cls, value: str) -> str:
        return validate_capability_id(value)

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: PoolJobPayload) -> PoolJobPayload:
        validate_pool_job_payload(value)
        return value

    @field_validator("input_keys")
    @classmethod
    def pool_input_keys(cls, value: list[str]) -> list[str]:
        for key in value:
            _require_normalized_key(key, POOL_SOURCE_PREFIX)
        return value

    @field_validator("output_key")
    @classmethod
    def pool_output_key(cls, value: str) -> str:
        return _require_normalized_key(value, POOL_OUTPUT_PREFIX)


class PoolJobResponse(BaseModel):
    """What a pool job looks like to the product's own callers.

    The lease claim token is the worker's fencing secret and is never part of
    this response.
    """

    job_id: str
    status: PoolJobStatus
    capability_id: str
    output_key: str
    attempts: int
    error: str | None
    failure_code: PoolJobFailureCode | None
    retryable: bool
    output_sha256: str | None
    output_byte_length: int | None

    @classmethod
    def from_record(cls, record: PoolJobRecord) -> "PoolJobResponse":
        return cls(
            job_id=record.job_id,
            status=record.status,
            capability_id=record.capability_id,
            output_key=record.output_key,
            attempts=record.attempts,
            error=record.error,
            failure_code=record.failure_code,
            retryable=record.retryable,
            output_sha256=record.output_sha256,
            output_byte_length=record.output_byte_length,
        )


def _require_normalized_key(value: str, prefix: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or str(path) != value
        or ".." in path.parts
    ):
        raise ValueError("asset key must be a normalized bucket-local path")
    if not value.startswith(prefix):
        raise ValueError(f"pool asset key must be in {prefix}")
    return value


class GenerateMotionPayloadV1(BaseModel):
    requested_settings: dict[str, object] = Field(default_factory=dict)
    resolved_recipe: dict[str, object] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    subject_key: str = Field(max_length=1024)
    motion_id: str = Field(
        pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100
    )
    prompt: str = Field(min_length=1, max_length=2_000)
    loop: bool
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    duration_seconds: int = Field(default=4, ge=1, le=60)
    seed: int = Field(default=0, ge=0)
    preset: str = Field(default="standard", pattern="^(quick|standard|high|cinematic)$")
    backdrop: str = Field(default="chroma_green", pattern="^(native_alpha|chroma_green)$")
    steps: int = Field(default=20, ge=4, le=60)
    provider_parameters: dict[str, bool | int | float | str] = Field(
        default_factory=dict, max_length=32
    )
    requested_revision: int = Field(ge=1)
    subject_revision_id: str = Field(min_length=1, max_length=255)
    subject_artifact_key: str = Field(max_length=1024)
    subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    motion_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_frame_key: str | None = Field(default=None, max_length=1024)
    start_frame_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    composition_revision_id: str | None = Field(default=None, max_length=255)
    view_id: str | None = None
    view_reference: dict[str, object] | None = None
    reference_preparation: dict[str, object] | None = None
    references: tuple[MotionReferencePayloadV1, ...] = Field(default=(), max_length=16)
    style: StylePreset | None = None
    script: str | None = Field(default=None, max_length=2000)

    _normalized_subject_key = field_validator("subject_key")(
        CreateJobRequest.normalized_key.__func__
    )
    _normalized_subject_artifact_key = field_validator("subject_artifact_key")(
        CreateJobRequest.normalized_key.__func__
    )

    @model_validator(mode="after")
    def matching_subject_keys(self) -> "GenerateMotionPayloadV1":
        if self.subject_key != self.subject_artifact_key:
            raise ValueError("subject_key and subject_artifact_key must match")
        if (self.start_frame_key is None) != (self.start_frame_sha256 is None):
            raise ValueError("start_frame_key and start_frame_sha256 must be present together")
        if self.composition_revision_id is not None and self.start_frame_key is None:
            raise ValueError("composition_revision_id requires a start frame key and hash")
        return self


class GenerateTurnaroundPayloadV1(BaseModel):
    """One closed-loop turnaround of a bound view reference (never a motion)."""

    model_config = ConfigDict(extra="forbid")

    requested_settings: dict[str, object] = Field(default_factory=dict)
    resolved_recipe: dict[str, object] = Field(default_factory=dict)
    turnaround_id: str = Field(pattern=r"^turnaround-[0-9a-f]{32}$")
    prompt_version: str = Field(pattern=r"^turnaround_prompt\.v1$")
    prompt: str = Field(min_length=1, max_length=2_000)
    width: int = Field(ge=256, le=1344)
    height: int = Field(ge=256, le=1344)
    duration_seconds: int = Field(ge=1, le=15)
    seed: int = Field(ge=0)
    preset: str = Field(default="standard", pattern="^(quick|standard|high|cinematic)$")
    backdrop: str = Field(default="chroma_green", pattern="^chroma_green$")
    steps: int = Field(ge=4, le=40)
    provider_parameters: dict[str, bool | int | float | str] = Field(default_factory=dict, max_length=32)
    requested_revision: int = Field(ge=1)
    source_view_id: str = Field(min_length=1, max_length=100)
    source_heading: str = Field(pattern=r"^(S|SE|SW)$")
    source_reference: dict[str, object]
    source_backdrop: str = Field(pattern=r"^(transparent|chroma)$")
    equipment: tuple[dict[str, str], ...] = Field(default=(), max_length=4)
    start_frame_key: str = Field(max_length=1024)
    start_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_preparation: dict[str, object]

    @model_validator(mode="after")
    def closed_loop_on_one_prepared_frame(self) -> "GenerateTurnaroundPayloadV1":
        if self.provider_parameters.get("closed_loop") is not True:
            raise ValueError("a turnaround is always a closed loop")
        if self.reference_preparation.get("slots") != ["first_frame", "last_frame"]:
            raise ValueError("the prepared frame must fill both template slots")
        return self


class GenerateSubjectPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_keys: tuple[str, ...] = Field(min_length=1, max_length=6)
    reference_sha256: tuple[str, ...] = Field(min_length=1, max_length=6)
    direction: str | None = Field(default=None, max_length=500)
    requested_revision: int = Field(ge=1)
    # Which machines may run it, resolved when the job is queued: the worker
    # role has no Studio service to resolve the owner at execution time.
    tenant_id: str | None = Field(default=None, max_length=256)

    @field_validator("reference_keys")
    @classmethod
    def normalized_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            path = PurePosixPath(key)
            if not key or key.startswith("/") or str(path) != key or ".." in path.parts:
                raise ValueError("reference key must be a normalized bucket-local path")
        if len(set(value)) != len(value):
            raise ValueError("reference keys must be unique")
        return value

    @model_validator(mode="after")
    def matching_lineage(self) -> "GenerateSubjectPayloadV1":
        if len(self.reference_keys) != len(self.reference_sha256):
            raise ValueError("reference digests must match reference keys")
        return self


class ComposeFramePayloadV1(BaseModel):
    """Frozen wire shape for `JobOperation.COMPOSE_FRAME` (T15), mirroring
    `GenerateSubjectPayloadV1`'s reference-lineage shape."""

    model_config = ConfigDict(extra="forbid")

    subject_key: str = Field(max_length=1024)
    subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_keys: tuple[str, ...] = Field(min_length=1, max_length=7)
    prompt: str = Field(min_length=1, max_length=2_000)
    composition_model: CompositionModel = CompositionModel.NANOBANANA_2_LITE
    requested_revision: int = Field(ge=1)

    _normalized_subject_key = field_validator("subject_key")(
        CreateJobRequest.normalized_key.__func__
    )

    @field_validator("reference_keys")
    @classmethod
    def normalized_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            path = PurePosixPath(key)
            if not key or key.startswith("/") or str(path) != key or ".." in path.parts:
                raise ValueError("reference key must be a normalized bucket-local path")
        if len(set(value)) != len(value):
            raise ValueError("reference keys must be unique")
        return value

    @model_validator(mode="after")
    def subject_is_first_reference(self) -> "ComposeFramePayloadV1":
        if self.reference_keys[0] != self.subject_key:
            raise ValueError("the subject key must be the primary composition reference")
        return self


class UploadRequest(BaseModel):
    key: str
    content_type: str

    _normalized_key = field_validator("key")(CreateJobRequest.normalized_key.__func__)

    @field_validator("key")
    @classmethod
    def source_key(cls, value: str) -> str:
        if not value.startswith("sources/"):
            raise ValueError("upload key must be in sources/")
        # This route proves no project ownership, so it must never mint a capability
        # inside a studio project's namespace -- /studio/* has owner-scoped uploads.
        if value.startswith(STUDIO_SOURCE_PREFIX):
            raise ValueError(
                "studio uploads must go through an owner-scoped /studio route"
            )
        return value


class UploadResponse(BaseModel):
    key: str
    upload_url: str
