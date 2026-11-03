"""Application service for Studio project subject flows."""

import asyncio
import io
import json
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import replace, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, Protocol
from urllib.parse import quote
from uuid import uuid4

from outbound_gpu_worker_pool.contracts import (
    JobRecord as PoolJobRecord,
)
from outbound_gpu_worker_pool.contracts import (
    JobRequirements,
)
from outbound_gpu_worker_pool.contracts import (
    JobStatus as PoolJobStatus,
)
from outbound_gpu_worker_pool.contracts import (
    JobSubmission as PoolJobSubmission,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field, ValidationError

from magic_video_editor.api_models import (
    ComposeFramePayloadV1,
    GenerateMotionPayloadV1,
    GenerateSubjectPayloadV1,
    MotionReferencePayloadV1,
)
from magic_video_editor.contracts import (
    AssetNotFound,
    AssetStorageUnavailable,
    AssetStore,
    AssetTooLarge,
    GpuTelemetry,
    GpuTelemetryStore,
    IdempotencyConflict,
    JobFailureCode,
    JobOperation,
    JobRecord,
    JobStatus,
    JobStore,
    JobSubmission,
    VideoJobResult,
    decode_job_activity_cursor,
    job_activity_cursor,
    upload_size_headers,
)
from magic_video_editor.plugins.ffmpeg import (
    ChromakeyDerivationFailed,
    FrameExtractionFailed,
    SheetUnpackFailed,
    chromakey_video_to_alpha_webm,
    extract_video_frames,
    render_sheet_to_gif,
    render_sheet_to_webm,
)
from magic_video_editor.plugins import higgsfield
from magic_video_editor.plugins.subject_pool import (
    keep_main_subject,
    normalise_subject_framing,
)
from magic_video_editor.retention import PROJECT, Retention
from magic_video_editor.studio.postgres import ManifestUnreadable
from magic_video_editor.safety import PromptRefused, PromptScreening, PromptScreenUnavailable
from magic_video_editor.machine_features import MachineCannotRun, subject_route
from magic_video_editor.studio.accounts import (
    AccountPolicy,
    machine_cannot_run,
    own_compute_required,
)
from magic_video_editor.studio.operators import OperatorAccess
from magic_video_editor.studio.exports import (
    GAIA_V1,
    MAX_EXPORT_TOTAL_BYTES,
    PNG_JSON_V2,
    ImmutableExportCollision,
    ReleaseExporter,
    candidate_playback_frame_count,
    chromakey_sheet,
    even_sample_with_padding,
    expand_ordered_sequence,
    pack_cell_sheet,
    render_candidate_sheet,
    register_sheet_pivot,
    resolve_candidate_pivot,
    split_sheet_into_tiles,
)
from magic_video_editor.studio.enrolment import StudioEnrolmentStore
from magic_video_editor.studio.limits import (
    UNLIMITED,
    MemoryStudioOwnerUsageStore,
    OwnerConcurrentJobLimitExceeded,
    OwnerLimits,
    OwnerStorageLimitExceeded,
    OwnerUsage,
    StudioOwnerUsageStore,
)
from magic_video_editor.studio.manifest import (
    add_composition,
    add_derived_motion_candidate,
    add_motion_candidate,
    approve_set,
    approve_subject,
    create_project_manifest,
    deliveries_for,
    latest_release_delivery,
    recompile_preset_motion_prompts,
    record_release_delivery,
    reject_subject_revision,
    restore_subject_revision,
    select_motion_candidate,
    select_subject_revision,
    set_digest,
    set_external_subject,
    summarize_manifest_change,
    update_motion_specification,
)
from magic_video_editor.studio.manifest import (
    add_motions as _apply_add_motions,
)
from magic_video_editor.studio.manifest import (
    approve_composition as _approve_composition_transition,
)
from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    Approval,
    ApprovalState,
    Backdrop,
    CandidateSource,
    CompositionApprovalState,
    CompositionModel,
    CompositionRevision,
    DeliveryStatus,
    DerivationRecipe,
    NormalizationSpec,
    ExportProfile,
    ExportRecord,
    ExternalSubject,
    Facing,
    FrameSlot,
    FrozenModel,
    HistoryEntry,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionReferenceRole,
    SpriteLayout,
    ViewId,
    ViewReference,
    CharacterView,
    view_matches_layout,
    MotionSpecification,
    MotionState,
    ProjectActivityStats,
    ProjectSummary,
    PromptSource,
    Release,
    ReleaseDelivery,
    RevisionLifecycle,
    SubjectAssessment,
    SubjectRevision,
    SubjectRevisionEdit,
    SubjectRevisionSource,
)
from magic_video_editor.studio.opening_frame import render_subject_opening_frame
from magic_video_editor.studio.provider_accounts import (
    HIGGSFIELD,
    ProviderAccounts,
    ProviderCredentialRejected,
    ProviderRequest,
)
from magic_video_editor.studio.presets import PRESET_DEFINITIONS
from magic_video_editor.studio.quality import (
    SUBJECT_POLICY_V1,
    MotionMeasurementBudgetExceeded,
    SubjectImageValidationError,
    assess_motion,
    assess_set,
    assess_subject_png,
    inspect_motion_artifact,
    inspect_sprite_png,
    require_rgba_png,
)
from magic_video_editor.studio.store import (
    ManifestMismatch,
    RevisionConflict,
    StoredManifest,
    StudioStore,
)
from magic_video_editor.targets import DeliveryRequest, TargetRegistry
from magic_video_editor.task_dispatch import TaskDispatcher

MAX_SUBJECT_UPLOAD_BYTES = SUBJECT_POLICY_V1.max_bytes
MAX_SUBJECT_REFERENCE_BYTES = 20_000_000
MAX_MOTION_OUTPUT_BYTES = 100_000_000
MAX_COMPOSITION_OUTPUT_BYTES = 20_000_000
_MOTION_MEASUREMENT_LIMIT = asyncio.Semaphore(2)
MOTION_POOL_CAPABILITY_ID = "video.minimax_h3.text_to_video.v1"
# The production profile retains its 24 GB requirement. Tested quantized
# workers opt into the separate bounded 16 GB capability below.
MOTION_POOL_REQUIREMENTS = JobRequirements(min_vram_mb=24_000)
MOTION_16GB_CAPABILITY_ID = "video.minimax_h3.text_to_video.w4a8_16gb.v1"
MOTION_LIGHTX8_CAPABILITY_ID = "video.minimax_h3.text_to_video.w4a8_16gb.lightx8.v1"


def _motion_pool_profile(settings: MotionGenerationSettings) -> tuple[str, JobRequirements]:
    """Route explicitly tested quantized jobs only to workers serving that profile."""
    profile = settings.provider_parameters.get("execution_profile")
    if profile in {"h3-w4a8-16gb", "h3-w4a8-16gb-lightx8"}:
        if settings.width > 512 or settings.height > 512 or settings.duration_seconds > 4 or settings.steps > 20:
            raise ValueError("the verified 16 GB profile supports at most 512x512 pixels, 4 seconds and 20 steps")
        if profile == "h3-w4a8-16gb-lightx8":
            if settings.steps != 8:
                raise ValueError("the LightX2V 8-step profile requires exactly 8 steps")
            return MOTION_LIGHTX8_CAPABILITY_ID, JobRequirements(min_vram_mb=16_000)
        return MOTION_16GB_CAPABILITY_ID, JobRequirements(min_vram_mb=16_000)
    return MOTION_POOL_CAPABILITY_ID, MOTION_POOL_REQUIREMENTS
_IN_FLIGHT_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PROCESSING})
# A reserved Higgsfield submission that never recorded a request ID is failed
# after this long, never resubmitted: the provider may have accepted it.
_UNCONFIRMED_PROVIDER_SUBMISSION = timedelta(minutes=10)
# The comfy-workflow template's declared input bounds
# (outbound-gpu-worker-pool, video.minimax_h3.text_to_video.v1).
_PROMPT_MAX_CHARS = 2000
_DIMENSION_BOUNDS = (256, 1344)
_STEPS_BOUNDS = (4, 40)
_LENGTH_BOUNDS = (17, 161)
_FPS_BOUNDS = (8, 30)
_DEFAULT_FPS = 24
_DEFAULT_LENGTH = 56
_DEFAULT_WIDTH = 512
_DEFAULT_HEIGHT = 768
_DEFAULT_STEPS = 20
# The reference render contract's chroma-green (#00B140, plugins/ffmpeg.py
# CHROMAKEY_FILTER) for CHROMA_GREEN; a flat neutral for backdrops that make
# no color promise in `_motion_prompt`.
_OPENING_FRAME_BACKDROP_RGB: dict[Backdrop, tuple[int, int, int]] = {
    Backdrop.CHROMA_GREEN: (0, 177, 64),
    Backdrop.NATIVE_ALPHA: (127, 127, 127),
}

logger = logging.getLogger(__name__)


class MotionPool(Protocol):
    """The outbound GPU worker pool surface motion dispatch and its
    status sync need. `WorkerPoolService` satisfies this; tests substitute a
    fake with the same three calls.
    """

    async def submit(self, submission: PoolJobSubmission) -> PoolJobRecord: ...

    async def get(self, job_id: str) -> PoolJobRecord | None: ...

    async def cancel(self, job_id: str) -> bool: ...


def _clamp(value: int, bounds: tuple[int, int], default: int) -> int:
    low, high = bounds
    if value < low:
        return low
    if value > high:
        return high
    return value


def _pinned_requirements(requirements: JobRequirements, capability_id: str,
                         parameters: dict[str, Any]) -> JobRequirements:
    """Bind a pool job to the resolved worker, recipe and configuration. A closed
    loop on the standard route also requires the worker's declared last-frame
    feature, so a worker that never reported it cannot lease the job; ordinary
    jobs never ask for it."""
    from outbound_gpu_worker_pool.machine_profiles import CONFIG_KEY, LAST_FRAME_FEATURE_KEY, RECIPE_KEY
    versions = {RECIPE_KEY: str(parameters["recipe_version"]),
                CONFIG_KEY: str(parameters["configuration_version"])}
    if parameters.get("closed_loop") is True and capability_id == MOTION_POOL_CAPABILITY_ID:
        versions[LAST_FRAME_FEATURE_KEY] = "1"
    return replace(requirements, worker_ids=(str(parameters["worker_id"]),), runtime_versions=versions)


def _motion_pool_payload(
    payload: GenerateMotionPayloadV1,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Map a generate_motion v1 payload onto the comfy-workflow template's
    declared inputs, clamped into the template's validation bounds.
    """
    return _pool_template_payload(
        prompt=payload.prompt, width=payload.width, height=payload.height,
        duration_seconds=payload.duration_seconds, steps=payload.steps, seed=payload.seed,
        provider_parameters=payload.provider_parameters, start_frame_key=payload.start_frame_key)


def _pool_template_payload(
    *, prompt: str, width: int, height: int, duration_seconds: int, steps: int, seed: int,
    provider_parameters: Mapping[str, Any], start_frame_key: str | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The H3 template's declared inputs, shared by motions and turnarounds."""
    raw_fps = provider_parameters.get("fps", _DEFAULT_FPS)
    try:
        fps = _clamp(int(raw_fps), _FPS_BOUNDS, _DEFAULT_FPS)
    except (TypeError, ValueError):
        fps = _DEFAULT_FPS
    frame_bounds = (17, 362) if provider_parameters.get('recipe_version') else _LENGTH_BOUNDS
    length = _clamp(
        round(duration_seconds * fps), frame_bounds, _DEFAULT_LENGTH
    )
    template_payload: dict[str, Any] = {
        "prompt": prompt[:_PROMPT_MAX_CHARS],
        "width": _clamp(width, _DIMENSION_BOUNDS, _DEFAULT_WIDTH),
        "height": _clamp(height, _DIMENSION_BOUNDS, _DEFAULT_HEIGHT),
        "length": length,
        "steps": _clamp(steps, _STEPS_BOUNDS, _DEFAULT_STEPS),
        "seed": max(0, seed),
        "fps": fps,
    }
    input_keys: tuple[str, ...] = ()
    if start_frame_key:
        # The template binds exactly one image slot; a granted input no slot
        # binds is a terminal rejection, so nothing else is ever granted.
        template_payload["images"] = {"first_frame": start_frame_key}
        if provider_parameters.get("closed_loop") is True:
            template_payload["images"]["last_frame"] = start_frame_key
        input_keys = (start_frame_key,)
    return template_payload, input_keys


# T15: "png-json" resolves to the current (v2) profile for new exports. v1 is
# never chosen here -- it stays readable/verifiable only through the stored
# ExportRecord of an export created before this change, never re-created.
_EXPORT_PROFILES_BY_NAME = {PNG_JSON_V2.name: PNG_JSON_V2, GAIA_V1.name: GAIA_V1}


class ProjectDeletionNotConfirmed(ValueError):
    pass


class ProjectNotFound(Exception):
    """Raised when a requested Studio project does not exist."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"studio project {project_id!r} was not found")


@dataclass(frozen=True, slots=True)
class BadRevision:
    """One durable revision that failed `AnimationProjectManifest.model_validate`
    during `StudioService.repair_project`."""

    revision: int
    created_at: datetime
    error_summary: str
    null_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectRepairReport:
    """What `StudioService.repair_project` found and did for one project."""

    project_id: str
    current_revision_before: int
    restored_from_revision: int | None
    new_revision: int | None
    repaired: bool
    bad_revisions: tuple[BadRevision, ...]


_NULL_PATH_CAP = 20


def _null_paths(raw: dict[str, Any]) -> tuple[str, ...]:
    """Dotted paths of `None` entries inside a raw manifest's lists, top-level
    (`motions[0]`) and one level nested (`subject.findings[0]`), capped at
    `_NULL_PATH_CAP` -- enough to point a human at what a corrupt revision is
    missing without dumping the whole document."""
    paths: list[str] = []

    def _scan_list(prefix: str, items: list[Any]) -> bool:
        for index, item in enumerate(items):
            if item is None:
                paths.append(f"{prefix}[{index}]")
                if len(paths) >= _NULL_PATH_CAP:
                    return True
        return False

    for key, value in raw.items():
        if isinstance(value, list):
            if _scan_list(key, value):
                return tuple(paths)
        elif isinstance(value, dict):
            for subkey, subvalue in value.items():
                if isinstance(subvalue, list) and _scan_list(
                    f"{key}.{subkey}", subvalue
                ):
                    return tuple(paths)
    return tuple(paths)


@dataclass(frozen=True, slots=True)
class OperationListing:
    """A job record plus whether its result is already folded into the manifest."""

    record: JobRecord
    settled: bool


@dataclass(frozen=True, slots=True)
class ActivityPage:
    """One page of `list_activity_operations` (T14): newest-first operations plus an
    opaque cursor for the next page, or `None` once there is nothing further back."""

    operations: tuple[OperationListing, ...]
    next_cursor: str | None


DEFAULT_ACTIVITY_PAGE_LIMIT = 20


_activity_cursor = job_activity_cursor

_ACTIVITY_OPERATIONS = frozenset(
    {
        JobOperation.GENERATE_SUBJECT,
        JobOperation.GENERATE_MOTION,
        JobOperation.COMPOSE_FRAME,
        JobOperation.GENERATE_TURNAROUND,
    }
)
_SUBJECT_OPERATIONS = frozenset({JobOperation.GENERATE_SUBJECT, JobOperation.COMPOSE_FRAME})
_POOL_OPERATIONS = frozenset({JobOperation.GENERATE_MOTION, JobOperation.GENERATE_TURNAROUND})


def _matches_activity_scope(record: JobRecord, motion_id: str | None) -> bool:
    """Whether `record` belongs to the object `motion_id` scopes the activity log to.

    `None` means unscoped (every studio-relevant operation). The literal value
    `"subject"` scopes to subject generation and opening-frame composition by
    operation, never by a missing `motion_id` (a turnaround has none either);
    `"turnaround:{id}"` scopes to that turnaround; any other value scopes to that
    motion's own operations (`GENERATE_MOTION` payloads carry `motion_id`).
    """
    if motion_id is None:
        return True
    if motion_id == "subject":
        return record.operation in _SUBJECT_OPERATIONS
    if motion_id.startswith("turnaround:"):
        return (record.operation is JobOperation.GENERATE_TURNAROUND
                and record.payload.get("turnaround_id") == motion_id.removeprefix("turnaround:"))
    return record.operation is JobOperation.GENERATE_MOTION and record.payload.get("motion_id") == motion_id


def _subject_state(
    manifest: AnimationProjectManifest,
) -> Literal["approved", "pending", "none"]:
    if manifest.subject.current_revision is None:
        return "none"
    if manifest.subject.approval.state in {
        ApprovalState.APPROVED,
        ApprovalState.WAIVED,
    }:
        return "approved"
    return "pending"


def is_settled(record: JobRecord, manifest: AnimationProjectManifest) -> bool:
    """A completed job is settled once its output is a candidate or subject revision."""
    if record.status is not JobStatus.COMPLETED:
        return False
    if record.operation is JobOperation.GENERATE_SUBJECT:
        return any(
            revision.artifact_key == record.output_key
            for revision in manifest.subject.revisions
        )
    if record.operation is JobOperation.COMPOSE_FRAME:
        return any(
            revision.artifact_key == record.output_key
            for revision in manifest.composition.revisions
        )
    if record.operation is JobOperation.GENERATE_TURNAROUND:
        return any(take.operation_id == record.manifest_operation_id and take.candidate is not None
                   and take.stored_bytes_recorded for take in manifest.turnarounds)
    return any(
        candidate.candidate_id == record.manifest_operation_id
        for motion in manifest.motions
        for candidate in motion.candidates
    )


class SubjectUploadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class SubjectWaiverUnknown(Exception):
    """A waived finding id was not present in the current subject assessment."""

    def __init__(self, finding_ids: tuple[str, ...]) -> None:
        self.finding_ids = finding_ids
        super().__init__(
            "waived finding ids do not match the current subject assessment"
        )


class SubjectRevisionNotFound(Exception):
    """The requested subject revision id is not present on this project."""


class MotionRequestBlocked(ValueError):
    """Motion generation requires a current approved subject."""


class MotionGenerationUnavailable(RuntimeError):
    """Compatible motion execution is not enabled for this runtime."""


class SubjectGenerationUnavailable(RuntimeError):
    """Compatible subject generation is not enabled for this runtime."""


class CompositionGenerationUnavailable(RuntimeError):
    """Compatible frame composition is not enabled for this runtime."""


class CompositionRequestBlocked(ValueError):
    """Composing or approving a still requires the current approved subject."""


class CompositionOperationNotReady(ValueError):
    """A composition job cannot be settled before successful completion."""


class CompositionApprovalBlocked(ValueError):
    """Approval requires the current, pending composition revision."""


class MotionOperationNotReady(ValueError):
    """A motion job cannot be settled before successful completion."""


class OperationAlreadyTerminal(ValueError):
    """A cancel was requested against a job that already reached a terminal state."""

    def __init__(self, state: JobStatus) -> None:
        self.state = state
        super().__init__(f"operation is already {state.value}")


class MotionLineageConflict(ValueError):
    """Completed output no longer matches its requested creative lineage."""


class MotionAssetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MotionDerivationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SuggestionsUnavailable(MotionDerivationError):
    """A usable turnaround whose directions could not be measured; the person picks by hand."""


class MotionUploadValidationError(ValueError):
    """An ingested candidate upload could not be measured or accepted as-is (T21).
    The upload itself is never deleted on this error -- only the settle is refused."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReleaseNotReady(ValueError):
    """The current project revision has no fresh explicit set signoff."""


class ReleaseNotFound(Exception):
    """A release is absent or belongs to another owner."""


class ExportNotFound(Exception):
    """An export is absent or belongs to another owner."""


class ProjectAlreadyBoundToSubject(Exception):
    """Creating a project for an external subject that already has one (Part A,
    deep-link idempotency): the caller should be sent to the existing project,
    not given a duplicate."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"external subject is already bound to project {project_id!r}")


class DeliveryNotConfigured(Exception):
    """Delivery to a target was requested but no adapter is installed for it,
    or the installed adapter has no `MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_URL`/
    `_TOKEN` configured."""


class UploadTarget(FrozenModel):
    key: str
    upload_url: str
    # Headers the client must send with the PUT (a signed size ceiling).
    upload_headers: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class QueuedMotionDispatch:
    """One motion whose GENERATE_MOTION job was accepted by `generate_motions` (T6)."""

    motion_id: str
    operation: JobRecord


@dataclass(frozen=True)
class FailedMotionDispatch:
    """One motion whose dispatch failed without sinking the rest of the batch (T6)."""

    motion_id: str
    code: str
    message: str


@dataclass(frozen=True)
class SkippedCancellation:
    """A live-operation listing that could not be cancelled by `cancel_all_operations`
    (T13) — typically because it settled between the listing read and the cancel call."""

    job_id: str
    state: JobStatus


@dataclass(frozen=True)
class CancelAllResult:
    """Outcome of `cancel_all_operations` (T13): job ids actually cancelled, plus any
    that were skipped because they were no longer live by the time cancel ran."""

    cancelled: tuple[str, ...]
    skipped: tuple[SkippedCancellation, ...]


class StudioService:
    def __init__(
        self,
        *,
        store: StudioStore,
        assets: AssetStore,
        jobs: JobStore | None = None,
        dispatcher: TaskDispatcher | None = None,
        motion_generation_available: bool = False,
        subject_generation_available: bool = False,
        composition_generation_available: bool = False,
        gpu_telemetry_store: GpuTelemetryStore | None = None,
        target_registry: TargetRegistry | None = None,
        motion_pool: MotionPool | None = None,
        owner_usage: StudioOwnerUsageStore | None = None,
        owner_limits: OwnerLimits | None = None,
        enrolment: StudioEnrolmentStore | None = None,
        operators: OperatorAccess | None = None,
        provider_accounts: ProviderAccounts | None = None,
        account_policy: AccountPolicy | None = None,
        subject_generation_on_pool: bool = False,
        prompt_screening: PromptScreening | None = None,
        retention: Retention | None = None,
    ) -> None:
        self._store = store
        # Inactivity expiry for unreleased projects; None leaves projects forever.
        self.retention = retention
        # Every prompt is screened against the acceptable-use line before it is
        # queued; off (no classifier) in local, desktop and test builds.
        self.prompt_screening = prompt_screening if prompt_screening is not None else PromptScreening(None)
        # Unconfigured means everyone is invited (desktop, local and tests).
        self.account_policy = account_policy if account_policy is not None else AccountPolicy()
        # Whether subject jobs run on tenant-bound pool workers rather than a
        # shared HTTP endpoint; only the former can serve a public account.
        self._subject_generation_on_pool = subject_generation_on_pool
        self.provider_accounts = provider_accounts
        # Replaced in tests; production calls Higgsfield's public API.
        self.higgsfield_client: Callable[[str], higgsfield.HiggsfieldClient] = higgsfield.HiggsfieldClient
        # Process-wide: overlapping polls share one bound on concurrent result
        # downloads, and a job already being collected is not collected twice.
        self._hosted_sync_limit = asyncio.Semaphore(2)
        self._hosted_syncs_in_flight: set[str] = set()
        self._assets = assets
        self._enrolment = enrolment
        # Who operates the service, and any compute their work shares (see
        # `studio.operators`); None in desktop, local and most tests.
        self.operators = operators
        self._jobs = jobs
        self._dispatcher = dispatcher
        self.additional_running_jobs: Callable[[str], Awaitable[int]] | None = None
        self.additional_stored_bytes: Callable[[str], Awaitable[int]] | None = None
        self._owner_usage = (
            owner_usage if owner_usage is not None else MemoryStudioOwnerUsageStore()
        )
        self._owner_limits = owner_limits if owner_limits is not None else OwnerLimits()
        self._motion_generation_available = motion_generation_available
        self._subject_generation_available = subject_generation_available
        self._composition_generation_available = composition_generation_available
        self._gpu_telemetry_store = gpu_telemetry_store
        self._motion_pool = motion_pool
        # Optional local policy, resolved before recording payload/provenance.
        # Production has no override and retains its existing 24 GB defaults.
        self.machine_defaults = None
        self.motion_settings_resolver: Callable[[MotionGenerationSettings], MotionGenerationSettings] | None = None
        self._target_registry = (
            target_registry
            if target_registry is not None
            else TargetRegistry.from_entry_points()
        )
        self._exporter = ReleaseExporter(assets, registry=self._target_registry)

    async def create_project(
        self,
        name: str,
        actor_id: str,
        *,
        external_subject: ExternalSubject | None = None,
        preset_motions: tuple[MotionPreset, ...] = (),
    ) -> StoredManifest:
        """Create a project, optionally already bound to an external subject and
        pre-populated with preset motions, in one durable revision (Part A deep
        link). Idempotent on the subject binding: a caller retrying the same deep
        link for an already-bound subject gets `ProjectAlreadyBoundToSubject`
        pointing at the existing project rather than a duplicate.
        """
        if external_subject is not None:
            existing = await self._store.find_by_external_subject(
                owner_id=actor_id,
                system=external_subject.system,
                entity_kind=external_subject.entity_kind,
                entity_id=external_subject.entity_id,
            )
            if existing is not None:
                raise ProjectAlreadyBoundToSubject(existing.project_id)
        manifest = create_project_manifest(
            project_id=str(uuid4()),
            name=name,
            owner_id=actor_id,
        )
        if external_subject is not None:
            manifest = set_external_subject(manifest, external_subject=external_subject)
        if preset_motions:
            subject_name = _subject_display_name(manifest)
            specifications = tuple(
                _compile_preset_specification(
                    MotionSpecification(
                        motion_id=preset.value,
                        name=PRESET_DEFINITIONS[preset].display_name,
                        kind=MotionKind.PRESET,
                        preset=preset,
                    ),
                    subject_name,
                )
                for preset in preset_motions
            )
            manifest = _apply_add_motions(manifest, specifications)
        created = await self._store.create(manifest)
        # Best effort here only: the project already exists, so failing the
        # request would invite a duplicate, and an untracked project gets a
        # full period when the sweep first sees it.
        try:
            await self._touch(created.project_id, created.manifest.owner_id)
        except Exception:
            logger.warning("retention.touch_failed_on_create", exc_info=True)
        return created

    async def find_project_for_external_subject(
        self,
        actor_id: str,
        *,
        system: str,
        entity_kind: str,
        entity_id: str,
    ) -> StoredManifest | None:
        """The owner's project already bound to this subject, if any (Part A
        deep link's "look up before create" step)."""
        found = await self._store.find_by_external_subject(
            owner_id=actor_id,
            system=system,
            entity_kind=entity_kind,
            entity_id=entity_id,
        )
        if found is not None:
            await self._touch(found.project_id, actor_id)
        return found

    async def update_external_subject(
        self,
        project_id: str,
        expected_revision: int,
        external_subject: ExternalSubject | None,
        actor_id: str,
    ) -> StoredManifest:
        """Bind or clear the project's external target identity (T22). Pure re-tag --
        does not touch the subject, any candidate, or any approval, so it never
        invalidates set signoff (`manifest.set_external_subject`).

        Preset motion prompts are recompiled against the new display name
        (T16): `{subject_name}` in a preset's prompt template must track
        whichever name is now authoritative, unless the operator already
        gave that motion its own custom prompt.
        """
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        manifest = set_external_subject(
            current.manifest, external_subject=external_subject
        )
        manifest = recompile_preset_motion_prompts(
            manifest, subject_name=_subject_display_name(manifest)
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def get_project(
        self, project_id: str, actor_id: str
    ) -> StoredManifest | None:
        project = await self._store.get(project_id)
        if project is None:
            return None
        if project.manifest.owner_id != actor_id:
            return None
        await self._touch(project_id, actor_id)
        return project

    async def _touch(self, project_id: str, owner_id: str) -> None:
        if self.retention is not None:
            await self.retention.touch(PROJECT, project_id, owner_id)

    @property
    def _shared_tenants(self) -> frozenset[str]:
        return frozenset() if self.operators is None else self.operators.shared_tenants

    async def _tenant_for(self, owner_id: str) -> str | None:
        shared = None if self.operators is None else await self.operators.shared_tenant(owner_id)
        if shared is not None:
            return shared
        own = await self._own_tenant_for(owner_id)
        if own is None and await self.account_policy.is_public(owner_id):
            # Never untenanted: a public account has no claim on the house
            # machines, so its work waits for its own hardware. Dispatch
            # refuses this up front (`_require_account_compute`); this is
            # the backstop if a machine is revoked after that check.
            return owner_id
        return own

    async def _require_account_compute(
        self, owner_id: str, *, h3: bool = False, shared_only: bool = False,
        message: str | None = None,
    ) -> None:
        """Refuse new work a public account may not start: MiniMax H3 without
        the operator's licence flag, or anything that would run on house
        compute. `shared_only` marks a provider with no per-owner machines.
        Invited accounts pass unchanged."""
        policy = self.account_policy
        if not await policy.is_public(owner_id):
            return
        if h3:
            await policy.require_h3(owner_id)
        if shared_only or await self._own_tenant_for(owner_id) is None:
            raise own_compute_required(message) if message else own_compute_required()

    async def _require_subject_machine(self, tenant_id: str | None) -> None:
        """Refuse a subject none of the owner's machines can run, with the reason,
        instead of queueing a pool job no machine will ever lease."""
        if not self._subject_generation_on_pool or self._motion_pool is None:
            return
        labels: dict[str, str] = {}
        removed: set[str] = set()
        if tenant_id is not None and self._enrolment is not None:
            for machine in await self._enrolment.list_workers(tenant_id):
                labels[machine.worker_id] = machine.label
                if machine.revoked:
                    removed.add(machine.worker_id)

        async def revoked(worker_id: str) -> bool:
            return worker_id in removed

        try:
            await subject_route(self._motion_pool, tenant_id, labels=labels,
                                house_tenants=self._shared_tenants,
                                revoked=revoked)
        except MachineCannotRun as exc:
            raise machine_cannot_run(exc.message) from exc

    async def _require_motion_compute(
        self, owner_id: str, settings: MotionGenerationSettings
    ) -> None:
        # Higgsfield runs on the owner's own provider account, never pool compute.
        if higgsfield.is_higgsfield(settings.provider_parameters):
            return
        # Every GPU motion recipe is MiniMax H3; without a pool the job would
        # run on the shared GPU endpoint.
        await self._require_account_compute(
            owner_id, h3=True, shared_only=self._motion_pool is None
        )

    async def account_capabilities(self, owner_id: str) -> dict[str, Any]:
        """What generation discovery tells the UI about this account."""
        public = await self.account_policy.is_public(owner_id)
        return {
            "tier": (await self.account_policy.tier(owner_id)).value,
            "house_compute": not public,
            "h3": await self.account_policy.h3_allowed(owner_id),
            "own_machine": await self._own_tenant_for(owner_id) is not None,
        }

    async def _own_tenant_for(self, owner_id: str) -> str | None:
        """Which machines may run this person's work.

        Affinity, not isolation: if they have attached a machine, their jobs are
        bound to it. If they have not, the job goes out untenanted so the shared
        house machines can take it -- otherwise binding an owner to hardware they
        do not have would leave the job unleasable by anyone, which is exactly
        what happened when ownership first shipped.
        """
        if self._enrolment is None:
            return None
        machines = await self._enrolment.list_workers(owner_id)
        live = [machine for machine in machines if not machine.revoked]
        return owner_id if live else None

    @property
    def assets(self) -> AssetStore:
        """The asset store, for routes that stream an already-authorized file."""
        return self._assets

    async def owner_of(self, project_id: str) -> str | None:
        """The project's owner, for binding pool work to the machines that
        person enrolled. Not an authorization check: the execution worker has
        already been trusted to run this job, and needs the owner, not consent."""
        project = await self._store.get(project_id)
        return None if project is None else project.manifest.owner_id

    async def repair_project(
        self, project_id: str, actor_id: str
    ) -> ProjectRepairReport:
        """Restore a project whose current manifest revision cannot be read
        (MAGIC-VIDEO-EDITOR-BACKEND-5). Revisions are append-only, so a
        previous one is always still intact: this walks raw revisions newest
        to oldest, finds the first that still validates, and appends it as a
        new current revision.

        Deliberately does not call `_require_owned_project` -- that loads the
        manifest through `model_validate`, which is exactly what is broken.
        Ownership is instead established from the raw revisions themselves:
        the actor must own at least one durable revision of this project, or
        this raises `ProjectNotFound` the same way a stranger's `get_project`
        call would, rather than leaking that the project exists.
        """
        raw_revisions = await self._store.list_raw_revisions(project_id)
        if not raw_revisions or not any(
            raw.manifest.get("owner_id") == actor_id for raw in raw_revisions
        ):
            raise ProjectNotFound(project_id)
        # Repair bypasses `_require_owned_project` (the current manifest cannot be
        # loaded), so it records the owner's activity itself: a just-repaired
        # project must not be expired by the next sweep.
        await self._touch(project_id, actor_id)

        current_revision_before = await self._store.current_revision_number(project_id)
        if current_revision_before is None:
            raise ProjectNotFound(project_id)

        bad_revisions: list[BadRevision] = []
        restored: AnimationProjectManifest | None = None
        restored_from_revision: int | None = None
        for raw in raw_revisions:
            try:
                restored = AnimationProjectManifest.model_validate(raw.manifest)
            except ValidationError as exc:
                bad_revisions.append(
                    BadRevision(
                        revision=raw.revision,
                        created_at=raw.created_at,
                        error_summary=str(exc)[:300],
                        null_paths=_null_paths(raw.manifest),
                    )
                )
                continue
            restored_from_revision = raw.revision
            break

        if restored is None or restored_from_revision == current_revision_before:
            return ProjectRepairReport(
                project_id=project_id,
                current_revision_before=current_revision_before,
                restored_from_revision=restored_from_revision,
                new_revision=None,
                repaired=False,
                bad_revisions=tuple(bad_revisions),
            )

        stored = await self._store.append(
            project_id,
            expected_revision=current_revision_before,
            manifest=restored,
        )
        newest_bad_revision = raw_revisions[0]
        logger.error(
            "project manifest repaired",
            extra={
                "project_id": project_id,
                "current_revision_before": current_revision_before,
                "restored_from_revision": restored_from_revision,
                "new_revision": stored.revision,
                "bad_revision_numbers": [item.revision for item in bad_revisions],
                "bad_revision_raw_json": json.dumps(newest_bad_revision.manifest)[
                    :16000
                ],
            },
        )
        return ProjectRepairReport(
            project_id=project_id,
            current_revision_before=current_revision_before,
            restored_from_revision=restored_from_revision,
            new_revision=stored.revision,
            repaired=True,
            bad_revisions=tuple(bad_revisions),
        )

    async def reassign_project_ownership(
        self, previous_owner_id: str, new_owner_id: str
    ) -> tuple[str, ...]:
        """Move every project owned by `previous_owner_id` to `new_owner_id`, once,
        when an `email:` user turns out to be the same person as a Google sign-in.

        Ownership lives in the manifest, so each move is a normal appended revision
        and keeps a valid digest; an unreadable current revision is left alone for
        `repair_project` rather than overwritten.
        """
        if previous_owner_id == new_owner_id:
            return ()
        moved: list[str] = []
        for record in await self._store.list_projects(previous_owner_id):
            if record.unreadable or record.stored is None:
                logger.warning(
                    "studio project %s was not reassigned: its current revision "
                    "is unreadable and needs repair first",
                    record.project_id,
                )
                continue
            manifest = record.stored.manifest.model_copy(
                update={"owner_id": new_owner_id}
            )
            await self._store.append(record.project_id, record.revision, manifest)
            moved.append(record.project_id)
        await self._store.reassign_owner(previous_owner_id, new_owner_id)
        return tuple(moved)

    async def list_projects(self, actor_id: str) -> tuple[ProjectSummary, ...]:
        """The actor's own projects for the home list, newest activity first.

        Applies the same visibility rule as `get_project`: `owner_id` must
        match `actor_id` exactly (ownership is enforced, not audit-only), so
        this can never surface another actor's project.
        """
        records = await self._store.list_projects(actor_id)
        expiries = (
            await self.retention.expiring(PROJECT, [r.project_id for r in records])
            if self.retention is not None else {}
        )
        summaries = []
        for record in records:
            activity = await self._project_activity_stats(record.project_id)
            if record.unreadable or record.stored is None:
                logger.warning(
                    "studio project %s revision %s is unreadable in the home "
                    "list; surfacing it as needing repair",
                    record.project_id,
                    record.revision,
                )
                summaries.append(
                    ProjectSummary(
                        project_id=record.project_id,
                        name=record.raw_name or "Untitled project",
                        revision=record.revision,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        created_by=record.created_by,
                        subject_state="none",
                        motion_count=0,
                        release_count=0,
                        external_subject=None,
                        activity=activity,
                        unreadable=True,
                        # Unreadable work still expires (by ID), so it is warned
                        # about like any other unless it has a release.
                        expires_at=None if await self._store.list_releases(record.project_id)
                        else expiries.get(record.project_id),
                    )
                )
                continue
            stored = record.stored
            manifest = stored.manifest
            releases = await self._store.list_releases(stored.project_id)
            summaries.append(
                ProjectSummary(
                    project_id=stored.project_id,
                    name=manifest.name,
                    revision=stored.revision,
                    created_at=record.created_at,
                    updated_at=stored.updated_at,
                    created_by=record.created_by,
                    subject_state=_subject_state(manifest),
                    motion_count=len(manifest.motions),
                    release_count=len(releases),
                    external_subject=manifest.external_subject,
                    activity=activity,
                    # Released work is kept, so it never expires.
                    expires_at=None if releases else expiries.get(stored.project_id),
                )
            )
        return tuple(summaries)

    async def _project_activity_stats(self, project_id: str) -> ProjectActivityStats:
        if self._jobs is None:
            return ProjectActivityStats(running=0, failed=0, completed=0)
        records = [
            record
            for record in await self._jobs.list_for_project(project_id)
            if record.operation in _ACTIVITY_OPERATIONS
        ]
        running = sum(
            1
            for record in records
            if record.status in {JobStatus.QUEUED, JobStatus.PROCESSING}
        )
        failed = sum(1 for record in records if record.status is JobStatus.FAILED)
        completed = sum(1 for record in records if record.status is JobStatus.COMPLETED)
        timestamps = [
            timestamp
            for record in records
            if (timestamp := record.updated_at or record.created_at) is not None
        ]
        last_activity_at = max(timestamps) if timestamps else None
        return ProjectActivityStats(
            running=running,
            failed=failed,
            completed=completed,
            last_activity_at=last_activity_at,
        )

    async def get_gpu_telemetry(
        self, project_id: str, actor_id: str
    ) -> GpuTelemetry | None:
        await self._require_owned_project(project_id, actor_id)
        if self._gpu_telemetry_store is None:
            return None
        return await self._gpu_telemetry_store.get(project_id)

    async def create_subject_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> UploadTarget:
        project = await self._require_owned_project(
            project_id, actor_id=actor_id, expected_revision=expected_revision
        )
        await self._require_storage_headroom(project.manifest.owner_id)
        _require_png_content_type(content_type)
        key = _subject_upload_key(project.project_id)
        return await self._upload_target(
            project.manifest.owner_id, key, content_type, MAX_SUBJECT_UPLOAD_BYTES
        )

    async def create_subject_reference_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> UploadTarget:
        project = await self._require_owned_project(
            project_id, actor_id=actor_id, expected_revision=expected_revision
        )
        await self._require_storage_headroom(project.manifest.owner_id)
        extension = _reference_extension(content_type)
        key = (
            f"sources/studio/{project.project_id}/subject-references/"
            f"{uuid4().hex}{extension}"
        )
        return await self._upload_target(
            project.manifest.owner_id, key, content_type, MAX_SUBJECT_REFERENCE_BYTES
        )

    async def generate_subject(
        self,
        project_id: str,
        expected_revision: int,
        reference_keys: tuple[str, ...],
        direction: str | None,
        request_id: str,
        actor_id: str,
    ) -> JobRecord:
        if self._jobs is None or self._dispatcher is None:
            raise RuntimeError("subject execution pipeline is not configured")
        if not self._subject_generation_available:
            raise SubjectGenerationUnavailable(
                "subject generation is unavailable until an attested provider is configured"
            )
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        await self._require_headroom_to_start_work(current.manifest.owner_id)
        # FLUX.2 Klein is not H3; a public owner may run it on their own machine.
        await self._require_account_compute(
            current.manifest.owner_id,
            shared_only=not self._subject_generation_on_pool,
        )
        if not 1 <= len(reference_keys) <= 6:
            raise SubjectUploadError(
                "subject_reference_count_invalid", "Choose 1 to 6 reference images"
            )
        tenant_id = await self._tenant_for(current.manifest.owner_id)
        await self._require_subject_machine(tenant_id)
        if len(set(reference_keys)) != len(reference_keys) or any(
            not _is_valid_subject_reference_key(
                key, project_id, current.manifest.subject.revisions
            )
            for key in reference_keys
        ):
            raise SubjectUploadError(
                "subject_reference_key_invalid",
                "A reference does not belong to this project",
            )
        digests: list[str] = []
        for key in reference_keys:
            try:
                content = await self._assets.read_limited(
                    key, MAX_SUBJECT_REFERENCE_BYTES
                )
            except (AssetNotFound, AssetTooLarge) as exc:
                raise SubjectUploadError(
                    "subject_reference_invalid", "A reference is missing or too large"
                ) from exc
            digests.append(sha256(content).hexdigest())
        await self.prompt_screening.require_allowed(current.manifest.owner_id, "subject", [direction])
        lineage = ":".join((*reference_keys, *digests, direction or "", request_id))
        operation_id = sha256(f"{project_id}:{lineage}".encode()).hexdigest()[:32]
        output_key = f"outputs/studio/{project_id}/subjects/{operation_id}.png"
        payload = GenerateSubjectPayloadV1(
            reference_keys=reference_keys,
            reference_sha256=tuple(digests),
            direction=direction.strip() if direction and direction.strip() else None,
            requested_revision=current.revision,
            tenant_id=tenant_id,
        )
        def submission(body: GenerateSubjectPayloadV1) -> JobSubmission:
            return JobSubmission(
                job_id=str(uuid4()),
                # Keyed by the caller's request_id alone, so reusing it for a
                # different request is refused instead of starting another job.
                idempotency_key=f"{project_id}:subject:request:{request_id}",
                operation=JobOperation.GENERATE_SUBJECT,
                source_key=reference_keys[0],
                output_key=output_key,
                requested_outputs={},
                project_id=project_id,
                manifest_operation_id=operation_id,
                # Untenanted payloads keep their previous shape.
                payload=body.model_dump(
                    mode="json", exclude={"tenant_id"} if body.tenant_id is None else None
                ),
            )

        def legacy_operation_id(job: JobRecord) -> str | None:
            try:
                body = GenerateSubjectPayloadV1.model_validate(job.payload)
            except ValidationError:
                return None
            lineage = ":".join(
                (*body.reference_keys, *body.reference_sha256, body.direction or "", request_id)
            )
            return sha256(f"{project_id}:{lineage}".encode()).hexdigest()[:32]

        accepted = [submission(payload).payload]
        if payload.tenant_id is not None:
            accepted.append(submission(payload.model_copy(update={"tenant_id": None})).payload)
        legacy = await self._legacy_request_job(
            project_id, JobOperation.GENERATE_SUBJECT, "subject", operation_id,
            legacy_operation_id, output_key, accepted,
        )
        if legacy is not None:
            await self._dispatcher.enqueue(legacy.job_id)
            return legacy
        try:
            submitted = await self._jobs.submit(submission(payload))
        except IdempotencyConflict:
            if payload.tenant_id is None:
                raise
            # A retry of a request queued before tenants were recorded: return
            # that original job rather than refusing the replay.
            submitted = await self._jobs.submit(
                submission(payload.model_copy(update={"tenant_id": None}))
            )
        await self._dispatcher.enqueue(submitted.record.job_id)
        return submitted.record

    async def _legacy_request_job(
        self,
        project_id: str,
        operation: JobOperation,
        scope: str,
        operation_id: str,
        legacy_operation_id: Callable[[JobRecord], str | None],
        output_key: str,
        accepted_payloads: list[dict[str, object]],
    ) -> JobRecord | None:
        """The job this request_id started before jobs were keyed by request_id.

        Such jobs are keyed `{project}:{scope}:{operation_id}`, with the
        request_id hashed into `operation_id` alongside the request's contents,
        so a retry's new `:request:` key misses them. A legacy job belongs to
        this request_id when its operation id matches this request's, or the one
        recomputed from its stored payload with this request_id. An identical
        retry gets that job back; anything else raises `IdempotencyConflict`
        rather than starting a second, possibly billable, job.

        Pages through every job in the project: a changed retry can only be
        recognized by recomputing each stored payload's operation id, so no
        single key lookup finds it.

        Remove this, and its callers' `legacy_operation_id`, once no job queued
        before request_id keys deployed can still be retried.
        """
        assert self._jobs is not None
        cursor = None
        while True:
            page = await self._jobs.list_for_project(project_id, cursor=cursor, limit=100)
            for job in page:
                if (
                    job.operation is not operation
                    or job.manifest_operation_id is None
                    or job.idempotency_key != f"{project_id}:{scope}:{job.manifest_operation_id}"
                    or job.manifest_operation_id not in {operation_id, legacy_operation_id(job)}
                ):
                    continue
                if (
                    job.manifest_operation_id == operation_id
                    and job.output_key == output_key
                    and job.payload in accepted_payloads
                ):
                    return job
                raise IdempotencyConflict(job.idempotency_key)
            if len(page) < 100:
                return None
            cursor = job_activity_cursor(page[-1])

    async def settle_subject_generation(
        self, project_id: str, job_id: str, expected_revision: int, actor_id: str
    ) -> StoredManifest:
        if self._jobs is None:
            raise RuntimeError("subject execution store is not configured")
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        job = await self._jobs.get(job_id)
        if (
            job is None
            or job.operation is not JobOperation.GENERATE_SUBJECT
            or job.status is not JobStatus.COMPLETED
            or job.result is None
            or job.project_id != project_id
            or job.result.output_key != job.output_key
        ):
            raise SubjectUploadError(
                "subject_operation_not_ready",
                "Subject generation is not ready to settle",
            )
        payload = GenerateSubjectPayloadV1.model_validate(job.payload)
        if any(
            revision.artifact_key == job.output_key
            for revision in current.manifest.subject.revisions
        ):
            return current
        if any(
            not _is_valid_subject_reference_key(
                key, project_id, current.manifest.subject.revisions
            )
            for key in payload.reference_keys
        ):
            raise SubjectUploadError(
                "subject_reference_lineage_invalid",
                "Subject reference lineage does not match this project",
            )
        for key, expected_digest in zip(
            payload.reference_keys, payload.reference_sha256, strict=True
        ):
            try:
                content = await self._assets.read_limited(
                    key, MAX_SUBJECT_REFERENCE_BYTES
                )
            except (AssetNotFound, AssetTooLarge) as exc:
                raise SubjectUploadError(
                    "subject_reference_lineage_invalid",
                    "Subject reference lineage is unavailable",
                ) from exc
            if sha256(content).hexdigest() != expected_digest:
                raise SubjectUploadError(
                    "subject_reference_lineage_invalid",
                    "A subject reference changed after generation",
                )
        return await self._settle_canonical_subject(
            current, job.output_key, direction=payload.direction
        )

    async def _settle_canonical_subject(
        self,
        current: StoredManifest,
        asset_key: str,
        *,
        direction: str | None = None,
    ) -> StoredManifest:
        try:
            png_bytes = await self._assets.read_limited(
                asset_key, MAX_SUBJECT_UPLOAD_BYTES
            )
        except (AssetNotFound, AssetTooLarge) as exc:
            raise SubjectUploadError(
                "subject_generated_asset_invalid",
                "Generated subject is missing or too large",
            ) from exc
        try:
            assessment = await asyncio.to_thread(
                assess_subject_png, png_bytes, SUBJECT_POLICY_V1
            )
        except SubjectImageValidationError as exc:
            raise SubjectUploadError(exc.code, exc.message) from exc
        revision = SubjectRevision(
            revision_id=str(uuid4()),
            artifact_key=asset_key,
            sha256=assessment.artifact_sha256,
            width=assessment.width,
            height=assessment.height,
            source=SubjectRevisionSource.GENERATED,
            direction=direction,
            created_at=datetime.now(UTC),
        )
        manifest = current.manifest.with_subject(revision)
        manifest = manifest.model_copy(
            update={
                "subject": manifest.subject.with_assessment(
                    assessment, approval=_subject_approval_from_assessment(assessment)
                )
            }
        )
        await self._record_stored_bytes(manifest.owner_id, asset_key)
        return await self._store.append(current.project_id, current.revision, manifest)

    async def create_asset_preview(
        self, project_id: str, asset_key: str, actor_id: str
    ) -> UploadTarget:
        """Inline read grant -- it feeds <img>/<video>, so never an attachment."""
        await self._require_manifest_asset(project_id, asset_key, actor_id)
        return await self._project_file_target(project_id, asset_key, None)

    async def create_asset_download(
        self, project_id: str, asset_key: str, actor_id: str
    ) -> UploadTarget:
        manifest = await self._require_manifest_asset(project_id, asset_key, actor_id)
        take = _subject_take_number(manifest, asset_key)
        filename = f"{_slug(manifest.name)}-subject-take-{take}.png"
        return await self._project_file_target(project_id, asset_key, filename)

    async def _require_manifest_asset(
        self, project_id: str, asset_key: str, actor_id: str
    ) -> AnimationProjectManifest:
        project = await self._require_owned_project(project_id, actor_id)
        allowed = {
            revision.artifact_key for revision in project.manifest.subject.revisions
        }
        allowed.update(
            candidate.artifact_key
            for motion in project.manifest.motions
            for candidate in motion.candidates
        )
        allowed.update(
            revision.artifact_key for revision in project.manifest.composition.revisions
        )
        # Current authoritative references are previewable after binding. Uploaded
        # drafts and superseded view revisions confer no new read capability.
        for view in project.manifest.views:
            reference = view.reference
            if reference is None or reference.key != asset_key:
                continue
            try:
                _, width, height = await self._read_view_reference(
                    project.manifest, view.view_id, reference.key, reference.sha256
                )
                if (width, height) != (reference.width, reference.height):
                    raise ValueError("view reference dimensions differ from the bound revision")
            except ValueError as exc:
                raise SubjectUploadError(
                    "asset_preview_reference_invalid",
                    "bound view reference failed integrity validation",
                ) from exc
            allowed.add(reference.key)
        if asset_key not in allowed:
            raise SubjectUploadError(
                "asset_preview_not_allowed",
                "asset is not part of this project manifest",
            )
        return project.manifest

    async def _project_file_target(
        self, project_id: str, asset_key: str, download_filename: str | None
    ) -> UploadTarget:
        """A link to an owned file. It is an API path, not a signed storage URL:
        the file route authorizes the caller again on every request, so the
        link works only for the person it was given to."""
        if await self._assets.describe(asset_key) is None:
            raise SubjectUploadError("asset_preview_missing", "preview asset is missing")
        return UploadTarget(
            key=asset_key, upload_url=self._file_link(project_id, asset_key, download_filename)
        )

    def _file_link(self, project_id: str, key: str, download_filename: str | None) -> str:
        local = getattr(self._assets, "local_read_path", None)
        if local is not None:
            return local(key, download_filename)
        return project_file_path(project_id, key, download_filename)

    async def authorize_project_file(self, project_id: str, key: str, actor_id: str) -> None:
        """Whether the caller may read `key`: they own the project and the key is
        in that project's own storage, or is one of its manifest assets."""
        await self._require_owned_project(project_id, actor_id)
        parts = key.split("/")
        if "" not in parts and ".." not in parts and key.startswith(
            (f"outputs/studio/{project_id}/", f"sources/studio/{project_id}/")
        ):
            return
        await self._require_manifest_asset(project_id, key, actor_id)

    async def settle_subject_upload(
        self,
        project_id: str,
        expected_revision: int,
        asset_key: str,
        actor_id: str,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        if not _is_subject_source_key(project_id, asset_key):
            raise SubjectUploadError(
                "subject_asset_key_invalid",
                "subject upload key must be in the project subject source namespace",
            )
        try:
            png_bytes = await self._assets.read_limited(
                asset_key, MAX_SUBJECT_UPLOAD_BYTES
            )
        except AssetNotFound as exc:
            raise SubjectUploadError(
                "subject_asset_missing",
                "subject upload is missing from asset storage",
            ) from exc
        except AssetTooLarge as exc:
            raise SubjectUploadError(
                "subject_png_bytes_too_large",
                f"subject upload exceeds the {exc.max_bytes} byte limit",
            ) from exc
        try:
            assessment = await asyncio.to_thread(
                assess_subject_png,
                png_bytes,
                SUBJECT_POLICY_V1,
            )
        except SubjectImageValidationError as exc:
            raise SubjectUploadError(exc.code, exc.message) from exc
        revision = SubjectRevision(
            revision_id=str(uuid4()),
            artifact_key=asset_key,
            sha256=assessment.artifact_sha256,
            width=assessment.width,
            height=assessment.height,
            source=SubjectRevisionSource.UPLOADED,
            created_at=datetime.now(UTC),
        )
        manifest = current.manifest.with_subject(revision)
        manifest = manifest.model_copy(
            update={
                "subject": manifest.subject.with_assessment(
                    assessment,
                    approval=_subject_approval_from_assessment(assessment),
                )
            }
        )
        await self._record_stored_bytes(manifest.owner_id, asset_key)
        return await self._store.append(
            project_id,
            expected_revision=current.revision,
            manifest=manifest,
        )

    async def approve_subject(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
        waived_finding_ids: tuple[str, ...] = (),
        waiver_reason: str | None = None,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        subject = current.manifest.subject
        revision = subject.current_revision
        assessment = subject.assessment
        if revision is None or assessment is None:
            raise SubjectUploadError(
                "subject_assessment_missing",
                "subject approval requires a settled subject revision",
            )
        if assessment.artifact_sha256 != revision.sha256:
            raise SubjectUploadError(
                "subject_assessment_mismatch",
                "subject approval requires an assessment for the current subject revision",
            )
        valid_waiver_codes = {finding.code.value for finding in assessment.findings}
        invalid_waivers = set(waived_finding_ids) - valid_waiver_codes
        if invalid_waivers:
            raise SubjectWaiverUnknown(tuple(sorted(invalid_waivers)))
        # Quality findings are warnings, never blockers (operator decision, 2026-09-05):
        # approval always proceeds. Every finding present at approval time is recorded
        # as acknowledged so the audit trail shows what was seen, unless the caller
        # already sent its own (narrower) waiver list.
        resolved_waivers = waived_finding_ids or tuple(sorted(valid_waiver_codes))
        resolved_reason = waiver_reason or (
            "acknowledged" if resolved_waivers else None
        )
        approved_manifest = approve_subject(
            current.manifest,
            revision_id=revision.revision_id,
            assessment_id=assessment.assessment_id,
            approver_id=actor_id,
            policy_version=assessment.policy_version,
            waived_finding_ids=resolved_waivers,
            waiver_reason=resolved_reason,
        )
        return await self._store.append(
            project_id,
            expected_revision=current.revision,
            manifest=approved_manifest,
        )

    async def select_subject_revision(
        self,
        project_id: str,
        expected_revision: int,
        revision_id: str,
        actor_id: str,
    ) -> StoredManifest:
        """Make an existing subject take current (studio-ux-flow phase 3).

        Approval carries over unchanged only when it already covers the exact
        image being chosen (same sha256, currently approved); any other case
        re-derives approval from a (possibly freshly computed) assessment of
        that revision, landing in `needs_correction` or `ready_for_signoff` --
        never silently approved.
        """
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        subject = current.manifest.subject
        revision = next(
            (item for item in subject.revisions if item.revision_id == revision_id),
            None,
        )
        if revision is None:
            raise SubjectRevisionNotFound(
                f"subject revision {revision_id!r} is not present for this project"
            )
        if (
            subject.approval.artifact_sha256 == revision.sha256
            and subject.approval.state in {ApprovalState.APPROVED, ApprovalState.WAIVED}
        ):
            approval = subject.approval
            assessment = (
                subject.assessment
                if subject.assessment is not None
                and subject.assessment.artifact_sha256 == revision.sha256
                else None
            )
        else:
            if (
                subject.assessment is not None
                and subject.assessment.artifact_sha256 == revision.sha256
            ):
                assessment = subject.assessment
            else:
                try:
                    png_bytes = await self._assets.read_limited(
                        revision.artifact_key, MAX_SUBJECT_UPLOAD_BYTES
                    )
                except (AssetNotFound, AssetTooLarge) as exc:
                    raise SubjectUploadError(
                        "subject_generated_asset_invalid",
                        "Subject revision is missing or too large",
                    ) from exc
                try:
                    assessment = await asyncio.to_thread(
                        assess_subject_png, png_bytes, SUBJECT_POLICY_V1
                    )
                except SubjectImageValidationError as exc:
                    raise SubjectUploadError(exc.code, exc.message) from exc
            approval = _subject_approval_from_assessment(assessment)
        manifest = select_subject_revision(
            current.manifest,
            revision_id=revision_id,
            assessment=assessment,
            approval=approval,
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def edit_subject_revision(
        self,
        project_id: str,
        expected_revision: int,
        revision_id: str,
        edit: SubjectRevisionEdit,
        actor_id: str,
    ) -> StoredManifest:
        """Re-run a framing/cleanup pass on a take and append the result as a new,
        selected take (source `edited`, parent = the take it was made from).

        This is what the Subject page's "Fit to frame" / "Clean up" finding actions
        call: the same `normalise_subject_framing` / `keep_main_subject` the
        generation pipeline already runs, applied again to a settled revision.
        """
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        revision = self._require_subject_revision(current.manifest, revision_id)
        if revision.lifecycle is RevisionLifecycle.REJECTED:
            raise ValueError("a rejected subject revision cannot be edited")
        try:
            png_bytes = await self._assets.read_limited(
                revision.artifact_key, MAX_SUBJECT_UPLOAD_BYTES
            )
        except (AssetNotFound, AssetTooLarge) as exc:
            raise SubjectUploadError(
                "subject_generated_asset_invalid",
                "Subject revision is missing or too large",
            ) from exc
        transform = (
            normalise_subject_framing
            if edit is SubjectRevisionEdit.REFRAME
            else keep_main_subject
        )
        edited = await asyncio.to_thread(transform, png_bytes)
        asset_key = _subject_upload_key(project_id)
        await self._assets.write_once(asset_key, edited, "image/png")
        try:
            assessment = await asyncio.to_thread(
                assess_subject_png, edited, SUBJECT_POLICY_V1
            )
        except SubjectImageValidationError as exc:
            raise SubjectUploadError(exc.code, exc.message) from exc
        edited_revision = SubjectRevision(
            revision_id=str(uuid4()),
            artifact_key=asset_key,
            sha256=assessment.artifact_sha256,
            width=assessment.width,
            height=assessment.height,
            source=SubjectRevisionSource.EDITED,
            parent_revision_ids=(revision_id,),
            direction=revision.direction,
            created_at=datetime.now(UTC),
        )
        manifest = current.manifest.with_subject(edited_revision)
        manifest = manifest.model_copy(
            update={
                "subject": manifest.subject.with_assessment(
                    assessment, approval=_subject_approval_from_assessment(assessment)
                )
            }
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def reject_subject_revision(
        self,
        project_id: str,
        expected_revision: int,
        revision_id: str,
        actor_id: str,
    ) -> StoredManifest:
        """Take a subject take out of the choosable set, keeping it on record."""
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        self._require_subject_revision(current.manifest, revision_id)
        manifest = reject_subject_revision(current.manifest, revision_id=revision_id)
        return await self._store.append(project_id, current.revision, manifest)

    async def restore_subject_revision(
        self,
        project_id: str,
        expected_revision: int,
        revision_id: str,
        actor_id: str,
    ) -> StoredManifest:
        """Put a rejected subject take back among the choosable ones."""
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        self._require_subject_revision(current.manifest, revision_id)
        manifest = restore_subject_revision(current.manifest, revision_id=revision_id)
        return await self._store.append(project_id, current.revision, manifest)

    @staticmethod
    def _require_subject_revision(
        manifest: AnimationProjectManifest, revision_id: str
    ) -> SubjectRevision:
        revision = next(
            (
                item
                for item in manifest.subject.revisions
                if item.revision_id == revision_id
            ),
            None,
        )
        if revision is None:
            raise SubjectRevisionNotFound(
                f"subject revision {revision_id!r} is not present for this project"
            )
        return revision

    async def compose_opening_frame(
        self,
        project_id: str,
        expected_revision: int,
        prompt: str,
        model: CompositionModel,
        actor_id: str,
        request_id: str,
    ) -> JobRecord:
        """Dispatch a `COMPOSE_FRAME` job for the current approved subject (T15).

        Also used to regenerate: a fresh compose always supersedes whatever
        composition is current once it settles (`CompositionState.with_composition`).
        """
        if self._jobs is None or self._dispatcher is None:
            raise RuntimeError("composition execution pipeline is not configured")
        if not self._composition_generation_available:
            raise CompositionGenerationUnavailable(
                "frame composition is unavailable until an attested provider is configured"
            )
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        subject = current.manifest.subject.current_revision
        approval = current.manifest.subject.approval
        if (
            subject is None
            or approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}
            or approval.artifact_sha256 != subject.sha256
        ):
            raise CompositionRequestBlocked(
                "frame composition requires the approved current subject"
            )
        await self._require_headroom_to_start_work(current.manifest.owner_id)
        await self._require_account_compute(
            current.manifest.owner_id, shared_only=True,
            message="Opening-pose composition runs on shared machines, which public "
            "accounts cannot use. Generate motions from the approved subject instead.",
        )
        if not request_id or len(request_id) > 100:
            raise ValueError("composition request_id must contain 1 to 100 characters")
        await self.prompt_screening.require_allowed(current.manifest.owner_id, "composition", [prompt])
        operation_id = sha256(
            f"{project_id}:composition:{subject.revision_id}:{prompt}:{model.value}:{request_id}".encode()
        ).hexdigest()[:32]
        output_key = f"outputs/studio/{project_id}/compositions/{operation_id}.png"
        payload = ComposeFramePayloadV1(
            subject_key=subject.artifact_key,
            subject_sha256=subject.sha256,
            reference_keys=(subject.artifact_key,),
            prompt=prompt,
            composition_model=model,
            requested_revision=current.revision,
        )
        revisions = current.manifest.subject.revisions

        def legacy_operation_id(job: JobRecord) -> str | None:
            try:
                body = ComposeFramePayloadV1.model_validate(job.payload)
            except ValidationError:
                return None
            revision_id = next(
                (
                    revision.revision_id for revision in revisions
                    if revision.artifact_key == body.subject_key
                    and revision.sha256 == body.subject_sha256
                ),
                None,
            )
            if revision_id is None:
                return None
            return sha256(
                f"{project_id}:composition:{revision_id}:{body.prompt}:"
                f"{body.composition_model.value}:{request_id}".encode()
            ).hexdigest()[:32]

        legacy = await self._legacy_request_job(
            project_id, JobOperation.COMPOSE_FRAME, "composition", operation_id,
            legacy_operation_id, output_key, [payload.model_dump(mode="json")],
        )
        if legacy is not None:
            await self._dispatcher.enqueue(legacy.job_id)
            return legacy
        submitted = await self._jobs.submit(
            JobSubmission(
                job_id=str(uuid4()),
                # Compose and regenerate share one request_id namespace; see
                # `generate_subject`.
                idempotency_key=f"{project_id}:composition:request:{request_id}",
                operation=JobOperation.COMPOSE_FRAME,
                source_key=subject.artifact_key,
                output_key=output_key,
                requested_outputs={},
                project_id=project_id,
                manifest_operation_id=operation_id,
                payload=payload.model_dump(mode="json"),
            )
        )
        await self._dispatcher.enqueue(submitted.record.job_id)
        return submitted.record

    async def regenerate_composition(
        self,
        project_id: str,
        expected_revision: int,
        prompt: str,
        model: CompositionModel,
        actor_id: str,
        request_id: str,
    ) -> JobRecord:
        """Compose a fresh still. Semantically identical to the first compose --
        settlement is what supersedes the previous current revision."""
        return await self.compose_opening_frame(
            project_id, expected_revision, prompt, model, actor_id, request_id
        )

    async def settle_composition(
        self, project_id: str, job_id: str, expected_revision: int, actor_id: str
    ) -> StoredManifest:
        if self._jobs is None:
            raise RuntimeError("composition execution store is not configured")
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        job = await self._jobs.get(job_id)
        if (
            job is None
            or job.operation is not JobOperation.COMPOSE_FRAME
            or job.status is not JobStatus.COMPLETED
            or job.result is None
            or job.project_id != project_id
            or job.result.output_key != job.output_key
        ):
            raise CompositionOperationNotReady("composition is not ready to settle")
        if any(
            revision.artifact_key == job.output_key
            for revision in current.manifest.composition.revisions
        ):
            return current
        payload = ComposeFramePayloadV1.model_validate(job.payload)
        subject = current.manifest.subject.current_revision
        approval = current.manifest.subject.approval
        if (
            subject is None
            or approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}
            or approval.artifact_sha256 != subject.sha256
            or payload.subject_key != subject.artifact_key
            or payload.subject_sha256 != subject.sha256
        ):
            raise CompositionRequestBlocked(
                "approved subject lineage changed while the still composed"
            )
        try:
            png_bytes = await self._assets.read_limited(
                job.output_key, MAX_COMPOSITION_OUTPUT_BYTES
            )
        except (AssetNotFound, AssetTooLarge) as exc:
            raise MotionAssetError(
                "composition_asset_invalid", "composed still is missing or too large"
            ) from exc
        try:
            await asyncio.to_thread(require_rgba_png, png_bytes, SUBJECT_POLICY_V1)
        except SubjectImageValidationError as exc:
            raise MotionAssetError(exc.code, exc.message) from exc
        revision = CompositionRevision(
            revision_id=job.manifest_operation_id or str(uuid4()),
            artifact_key=job.output_key,
            sha256=sha256(png_bytes).hexdigest(),
            prompt=payload.prompt,
            model=payload.composition_model,
            parent_subject_revision_id=subject.revision_id,
        )
        manifest = add_composition(current.manifest, revision=revision)
        await self._record_stored_bytes(manifest.owner_id, job.output_key)
        return await self._store.append(project_id, current.revision, manifest)

    async def approve_composition(
        self, project_id: str, expected_revision: int, actor_id: str
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        composition = current.manifest.composition.current
        subject = current.manifest.subject.current_revision
        if (
            composition is None
            or subject is None
            or composition.parent_subject_revision_id != subject.revision_id
        ):
            raise CompositionApprovalBlocked(
                "composition approval requires a still composed from the current subject"
            )
        manifest = _approve_composition_transition(
            current.manifest, revision_id=composition.revision_id
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def require_same_view_calibration(self, project_id: str, motion_id: str,
                                            reference_motion_id: str, actor_id: str) -> None:
        current = await self._require_owned_project(project_id, actor_id)
        motions = {motion.motion_id: motion for motion in current.manifest.motions}
        if motion_id not in motions or reference_motion_id not in motions:
            raise ValueError("source and reference motions must exist")
        if motions[motion_id].specification.view_id != motions[reference_motion_id].specification.view_id:
            raise ValueError("body calibration requires an explicit reference in the same view")

    async def _find_dispatch_job(self, project_id: str, operation_id: str):
        cursor = None
        while True:
            page = await self._jobs.list_for_project(project_id, cursor=cursor, limit=100)
            found = next((j for j in page if j.manifest_operation_id == operation_id), None)
            if found is not None or len(page) < 100:
                return found
            cursor = job_activity_cursor(page[-1])

    async def abandon_motion_dispatch(self, project_id: str, expected_revision: int,
                                      operation_id: str, actor_id: str, *,
                                      confirm_dispatch_stopped: bool = False):
        from .setup import SetupBlocked, SetupConfirmationRequired
        current = await self._require_owned_project(project_id, actor_id, expected_revision)
        if not confirm_dispatch_stopped:
            raise SetupConfirmationRequired("confirm the failed dispatch process has stopped before abandoning its reservation")
        if not any(r.operation_id == operation_id for r in current.manifest.dispatch_reservations):
            raise ValueError("dispatch reservation was not found")
        job = await self._find_dispatch_job(project_id, operation_id) if self._jobs is not None else None
        if job is not None and job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise SetupBlocked("cancel or settle the registered operation before abandoning its reservation")
        manifest = current.manifest.model_copy(update={
            "dispatch_reservations": tuple(r for r in current.manifest.dispatch_reservations if r.operation_id != operation_id),
            "abandoned_dispatches": (*current.manifest.abandoned_dispatches, operation_id)})
        return await self._store.append(project_id, current.revision, manifest)

    async def stage_setup_reference(self, project_id: str, content: bytes,
                                    content_type: str, actor_id: str):
        from .setup import stage_reference
        return await stage_reference(self, project_id, content, content_type, actor_id)

    async def get_project_setup_snapshot(self, project_id: str, actor_id: str):
        from .setup import current_setup
        current = await self._require_owned_project(project_id, actor_id)
        return current.revision, current_setup(current.manifest)

    async def get_project_setup(self, project_id: str, actor_id: str):
        from .setup import current_setup
        current = await self._require_owned_project(project_id, actor_id)
        return current_setup(current.manifest)

    async def preview_project_setup(self, project_id: str, expected_revision: int,
                                    desired_setup, actor_id: str):
        from .setup import preview
        return await preview(self, project_id, expected_revision, desired_setup, actor_id)

    async def apply_project_setup(self, project_id: str, expected_revision: int,
                                  plan_token: str, confirmed_impacts, actor_id: str):
        from .setup import apply
        return await apply(self, project_id, expected_revision, plan_token, confirmed_impacts, actor_id)

    async def set_sprite_layout(self, project_id: str, expected_revision: int,
                                sprite_layout: SpriteLayout, actor_id: str) -> StoredManifest:
        current = await self._require_owned_project(project_id, actor_id, expected_revision)
        if any(not view_matches_layout(view.view_id, sprite_layout) for view in current.manifest.views):
            raise ValueError("layout change would orphan existing views; use a separate project")
        manifest = _invalidate_view_set(current.manifest.model_copy(update={"sprite_layout": sprite_layout}))
        return await self._store.append(project_id, current.revision, manifest)

    async def create_view_reference_upload(self, project_id: str, expected_revision: int,
                                          view_id: ViewId, content_type: str, actor_id: str) -> UploadTarget:
        current = await self._require_owned_project(project_id, actor_id, expected_revision)
        if not view_matches_layout(view_id, current.manifest.sprite_layout):
            raise ValueError("view does not match sprite layout")
        await self._require_storage_headroom(current.manifest.owner_id)
        extension = _reference_extension(content_type)
        key = f"sources/studio/{project_id}/views/{view_id}/references/{uuid4().hex}{extension}"
        return await self._upload_target(
            current.manifest.owner_id, key, content_type, MAX_SUBJECT_REFERENCE_BYTES
        )

    async def _read_view_reference(self, manifest: AnimationProjectManifest, view_id: ViewId,
                                   key: str, digest: str) -> tuple[bytes, int, int]:
        prefix = f"sources/studio/{manifest.project_id}/views/{view_id}/references/"
        suffix = key.removeprefix(prefix)
        uploaded = key.startswith(prefix) and bool(suffix) and "/" not in suffix and ".." not in suffix and "\\" not in suffix
        subject_source = any(revision.artifact_key == key and revision.sha256 == digest
                             for revision in manifest.subject.revisions)
        declared_source = any(view.reference is not None and view.reference.key == key and view.reference.sha256 == digest for view in manifest.views)
        if not uploaded and not subject_source and not declared_source:
            raise ValueError("view reference does not belong to this project and view")
        try:
            content = await self._assets.read_limited(key, MAX_SUBJECT_REFERENCE_BYTES)
            if sha256(content).hexdigest() != digest:
                raise ValueError("view reference hash does not match uploaded image")
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("view reference must be a single PNG, JPEG, or WebP image")
                width, height = image.size
                if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 16_777_216:
                    raise ValueError("view reference dimensions exceed limits")
                image.load()
        except (AssetNotFound, AssetTooLarge, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("view reference is missing, oversized, or not a decodable image") from exc
        return content, width, height

    async def set_character_view(self, project_id: str, expected_revision: int, view_id: ViewId,
                                 actor_id: str, *, name: str | None = None,
                                 reference: tuple[str, str] | None = None,
                                 replace_reference: bool = False) -> StoredManifest:
        current = await self._require_owned_project(project_id, actor_id, expected_revision)
        if not view_matches_layout(view_id, current.manifest.sprite_layout):
            raise ValueError("view does not match sprite layout")
        existing = next((view for view in current.manifest.views if view.view_id == view_id), None)
        resolved = existing.reference if existing else None
        if replace_reference:
            resolved = None
            if reference is not None:
                key, digest = reference
                _, width, height = await self._read_view_reference(current.manifest, view_id, key, digest)
                # Reusing subject pixels is explicit and requires the approved current identity.
                if any(item.artifact_key == key for item in current.manifest.subject.revisions):
                    subject = current.manifest.subject.current_revision
                    if subject is None or subject.artifact_key != key or subject.sha256 != digest:
                        raise ValueError("only the approved current subject can be bound as a view reference")
                    approval = current.manifest.subject.approval
                    if approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED} or approval.artifact_sha256 != digest:
                        raise ValueError("subject reference requires the approved current subject")
                resolved = ViewReference(revision_id=uuid4().hex, key=key, sha256=digest, width=width, height=height)
        view = CharacterView(view_id=view_id, name=name or (existing.name if existing else str(view_id)), reference=resolved,
                             **({"heading": existing.heading, "setup_version": existing.setup_version} if existing and existing.setup_version else {}))
        views = tuple(view if item.view_id == view_id else item for item in current.manifest.views)
        if existing is None:
            views = (*views, view)
        motions = tuple(motion.model_copy(update={"specification": motion.specification.model_copy(update={"view_reference": resolved})})
                        if motion.specification.view_id == view_id else motion for motion in current.manifest.motions)
        manifest = _invalidate_view_set(current.manifest.model_copy(update={"views": views, "motions": motions}))
        return await self._store.append(project_id, current.revision, manifest)

    async def add_motion(
        self,
        project_id: str,
        expected_revision: int,
        specification: MotionSpecification,
        actor_id: str,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        compiled = _compile_preset_specification(
            _resolve_motion_view(current.manifest, specification), _subject_display_name(current.manifest)
        )
        return await self._store.append(
            project_id, current.revision, current.manifest.with_motion(compiled)
        )

    async def add_motions(
        self,
        project_id: str,
        expected_revision: int,
        specifications: tuple[MotionSpecification, ...],
        actor_id: str,
    ) -> StoredManifest:
        """Add many motions in one revision (T6). Duplicates of an existing motion_id
        are rejected wholesale so the caller can report exactly which ids collided."""
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        compiled = tuple(
            _compile_preset_specification(
                _resolve_motion_view(current.manifest, specification), _subject_display_name(current.manifest)
            )
            for specification in specifications
        )
        manifest = _apply_add_motions(current.manifest, compiled)
        return await self._store.append(project_id, current.revision, manifest)

    async def update_motion(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        specification: MotionSpecification,
        actor_id: str,
    ) -> StoredManifest:
        """Persist an edited motion specification (references/style/script, T17), preserving
        candidates and selection. Preset motions are recompiled the same way `add_motion` compiles
        them, so an edit cannot silently drop the authored prompt or loop policy."""
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        compiled = _compile_preset_specification(
            _resolve_motion_view(current.manifest, specification), _subject_display_name(current.manifest)
        )
        manifest = update_motion_specification(
            current.manifest, motion_id=motion_id, specification=compiled
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def create_motion_reference_upload(
        self,
        project_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> UploadTarget:
        """Signed upload for a motion reference image (T17), reusing the subject-reference
        namespace convention but under the project's motion-references prefix."""
        project = await self._require_owned_project(
            project_id, actor_id=actor_id, expected_revision=expected_revision
        )
        await self._require_storage_headroom(project.manifest.owner_id)
        extension = _reference_extension(content_type)
        key = (
            f"sources/studio/{project.project_id}/motion-references/"
            f"{uuid4().hex}{extension}"
        )
        return await self._upload_target(
            project.manifest.owner_id, key, content_type, MAX_SUBJECT_REFERENCE_BYTES
        )

    async def _apply_motion_actions(
        self,
        project_id: str,
        current: StoredManifest,
        actions: Mapping[str, str],
    ) -> StoredManifest:
        """Persist the operator's "what happens" lines, then hand back the stored manifest to
        dispatch from -- one revision for the whole edit, so a batch confirm is a single write.

        A preset motion takes the line as its `action` and recompiles its prompt from the
        preset template; a custom motion has no template, so the line *is* its prompt.
        """
        subject_name = _subject_display_name(current.manifest)
        manifest = current.manifest
        changed = False
        for motion_id, raw in actions.items():
            motion = next(
                (item for item in manifest.motions if item.motion_id == motion_id), None
            )
            if motion is None:
                continue
            line = raw.strip()
            specification = motion.specification
            if specification.kind is MotionKind.CUSTOM:
                if not line or line == specification.prompt:
                    continue
                updated = specification.model_copy(
                    update={"prompt": line, "direction": line}
                )
            else:
                updated = _compile_preset_specification(
                    specification.model_copy(update={"action": line or None}),
                    subject_name,
                )
            if updated == specification:
                continue
            manifest = update_motion_specification(
                manifest, motion_id=motion_id, specification=updated
            )
            changed = True
        if not changed:
            return current
        return await self._store.append(project_id, current.revision, manifest)

    async def generate_motions(
        self,
        project_id: str,
        expected_revision: int,
        motion_ids: tuple[str, ...],
        actor_id: str,
        request_id: str,
        settings: MotionGenerationSettings | None = None,
        shared_starting_pose: bool = False,
        actions: Mapping[str, str] | None = None,
    ) -> tuple[tuple["QueuedMotionDispatch", ...], tuple["FailedMotionDispatch", ...]]:
        """Dispatch one GENERATE_MOTION job per motion under one request_id (T6).

        One motion's dispatch failure must not stop the others; the operation_id each
        job is keyed on already folds in `request_id`, so replaying the same request_id
        against the same motions is idempotent (JobStore.submit dedups on it).

        `shared_starting_pose=True` (T15) requires a composition approved against the
        current subject revision; its still is passed as `start_frame_key` to every
        dispatched motion so the whole batch shares one opening pose.
        """
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        if self._dispatcher is None:
            raise RuntimeError("motion task dispatcher is not configured")
        if not self._motion_generation_available:
            raise MotionGenerationUnavailable(
                "motion generation is disabled until compatible workers are ready"
            )
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        subject = current.manifest.subject.current_revision
        approval = current.manifest.subject.approval
        if (
            subject is None
            or approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}
            or approval.artifact_sha256 != subject.sha256
        ):
            raise MotionRequestBlocked(
                "motion generation requires the approved current subject"
            )
        await self._require_headroom_to_start_work(current.manifest.owner_id)
        await self._require_motion_compute(
            current.manifest.owner_id, settings or MotionGenerationSettings()
        )
        compiled_actions = {
            motion.motion_id: motion.specification.action or ""
            for motion in current.manifest.motions
            if motion.motion_id in motion_ids and motion.specification.kind is MotionKind.PRESET
        }
        # Text typed into this request is screened before it is saved; whatever is
        # dispatched is screened again as its final prompt.
        await self.prompt_screening.require_allowed(
            current.manifest.owner_id, "motion", list((actions or {}).values())
        )
        compiled_actions.update(actions or {})
        if compiled_actions:
            current = await self._apply_motion_actions(project_id, current, compiled_actions)
        start_frame: CompositionRevision | None = None
        if shared_starting_pose and any(motion.motion_id in motion_ids and motion.specification.view_id is not None for motion in current.manifest.motions):
            raise MotionRequestBlocked("view motions cannot use a shared starting pose")
        if shared_starting_pose:
            composition = current.manifest.composition.current
            if (
                composition is None
                or composition.approval is not CompositionApprovalState.APPROVED
                or composition.parent_subject_revision_id != subject.revision_id
            ):
                raise MotionRequestBlocked(
                    "a shared starting pose requires an approved composition bound to "
                    "the current subject"
                )
            start_frame = composition
        if not request_id or len(request_id) > 100:
            raise ValueError("motion request_id must contain 1 to 100 characters")
        settings = settings or MotionGenerationSettings()
        motions_by_id = {
            motion.motion_id: motion for motion in current.manifest.motions
        }
        queued: list[QueuedMotionDispatch] = []
        failed: list[FailedMotionDispatch] = []
        # Higgsfield calls wait until every ledger row exists, then run together,
        # so a large batch stays well inside the API request timeout.
        deferred: list[tuple[JobRecord, higgsfield.HiggsfieldModel]] = []
        # The batch is capped as a whole: the cap is on jobs in flight, and a
        # 32-motion batch would otherwise walk straight past a check made once.
        headroom = self._dispatch_headroom(
            await self._running_job_count(current.manifest.owner_id)
        )
        for motion_id in motion_ids:
            if headroom is not None and len(queued) >= headroom:
                limit = self._owner_limits.concurrent_job_limit
                refusal = OwnerConcurrentJobLimitExceeded(limit, limit)
                failed.append(
                    FailedMotionDispatch(
                        motion_id=motion_id,
                        code=refusal.code,
                        message=refusal.message,
                    )
                )
                continue
            motion = motions_by_id.get(motion_id)
            if motion is None:
                failed.append(
                    FailedMotionDispatch(
                        motion_id=motion_id,
                        code="motion_not_found",
                        message="motion is not present in the manifest",
                    )
                )
                continue
            try:
                record = await self._dispatch_motion_generation(
                    project_id=project_id,
                    current_revision=current.revision,
                    motion=motion,
                    subject=subject,
                    request_id=request_id,
                    settings=settings,
                    owner_id=current.manifest.owner_id,
                    start_frame=start_frame,
                    deferred_submissions=deferred,
                )
            except (PromptRefused, PromptScreenUnavailable) as exc:
                failed.append(FailedMotionDispatch(motion_id=motion_id, code=exc.code, message=str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 - one bad dispatch must not sink the batch
                failed.append(
                    FailedMotionDispatch(
                        motion_id=motion_id, code="dispatch_failed", message=str(exc)
                    )
                )
                continue
            queued.append(QueuedMotionDispatch(motion_id=motion_id, operation=record))
        if deferred:
            outcomes = await asyncio.gather(
                *(self._submit_higgsfield(record, current.manifest.owner_id, model) for record, model in deferred),
                return_exceptions=True,
            )
            rejected = {record.job_id: outcome for (record, _), outcome in zip(deferred, outcomes, strict=True)
                        if isinstance(outcome, BaseException)}
            settled: list[QueuedMotionDispatch] = []
            for item in queued:
                if item.operation.job_id in rejected:
                    failed.append(FailedMotionDispatch(motion_id=item.motion_id, code="dispatch_failed",
                                                       message=str(rejected[item.operation.job_id])))
                else:
                    settled.append(QueuedMotionDispatch(
                        motion_id=item.motion_id,
                        operation=(await self._jobs.get(item.operation.job_id)) or item.operation))
            queued = settled
        return tuple(queued), tuple(failed)

    async def _dispatch_motion_generation(
        self,
        *,
        project_id: str,
        current_revision: int,
        motion: MotionState,
        subject: SubjectRevision,
        request_id: str,
        settings: MotionGenerationSettings,
        owner_id: str,
        start_frame: CompositionRevision | None = None,
        deferred_submissions: list[tuple[JobRecord, higgsfield.HiggsfieldModel]] | None = None,
    ) -> JobRecord:
        requested_settings = settings.model_dump(mode="json", exclude_unset=True)
        spec_digest = motion_spec_digest(motion.specification)
        pose_key = start_frame.revision_id if start_frame is not None else ""
        operation_id = sha256(
            f"{project_id}:{motion.motion_id}:{subject.revision_id}:{spec_digest}:"
            f"{request_id}:{pose_key}".encode()
        ).hexdigest()[:32]
        dispatch_current = await self._require_owned_project(project_id, owner_id)
        if operation_id in dispatch_current.manifest.abandoned_dispatches:
            raise MotionRequestBlocked("dispatch was explicitly abandoned; use a new request ID")
        view_attack = motion.specification.view_id is not None and motion.specification.preset is MotionPreset.ATTACK
        # Preserve request identity across preparation versions. A retry must
        # return its original immutable job, never reinterpret or rerender it.
        idempotency_key = f"{project_id}:{motion.motion_id}:{operation_id}"
        cursor = None
        while True:
            page = await self._jobs.list_for_project(project_id, cursor=cursor, limit=100)
            existing = next((job for job in page if job.idempotency_key == idempotency_key), None)
            if existing is not None:
                hosted_job = higgsfield.is_higgsfield(existing.payload.get("provider_parameters") or {})
                if not view_attack and not hosted_job and self._motion_pool is None and existing.status is JobStatus.QUEUED:
                    # A job queued before screening existed is screened before it
                    # is queued again; a refused one is cancelled, never replayed.
                    try:
                        await self.prompt_screening.require_allowed(
                            owner_id, "motion", [str(existing.payload.get("prompt") or "")]
                        )
                    except PromptRefused:
                        await self._jobs.cancel(existing.job_id)
                        raise
                    await self._dispatcher.enqueue(existing.job_id)
                return existing
            if len(page) < 100:
                break
            cursor = job_activity_cursor(page[-1])
        hosted_model: higgsfield.HiggsfieldModel | None = None
        if higgsfield.is_higgsfield(settings.provider_parameters):
            # The owner's Higgsfield account runs this; no machine is resolved
            # and no GPU recipe is claimed.
            settings, hosted_model = higgsfield.resolve_settings(settings)
            await self._require_provider_key(owner_id)
        elif self.machine_defaults is not None:
            settings = await self.machine_defaults.resolve(owner_id, settings)
        elif self.motion_settings_resolver is not None:
            settings = self.motion_settings_resolver(settings)
        if motion.specification.preset is MotionPreset.ATTACK and "framing_fill" not in settings.provider_parameters:
            settings = settings.model_copy(update={"provider_parameters": {**settings.provider_parameters, "framing_fill": 0.58}})
        resolved_recipe = {}
        if hosted_model is not None:
            resolved_recipe = {"provider": HIGGSFIELD, **hosted_model.describe()}
        elif self.machine_defaults is not None:
            from outbound_gpu_worker_pool.machine_profiles import LIGHTX8, STANDARD, describe_profile
            resolved_recipe = next((describe_profile(p) for p in (LIGHTX8, STANDARD)
                if describe_profile(p)['recipe_version'] == settings.provider_parameters.get('recipe_version')), {})
        view_fill = float(settings.provider_parameters["framing_fill"]) if view_attack else 1.0
        output_key = (
            f"outputs/studio/{project_id}/motions/{motion.motion_id}/{operation_id}.mp4"
        )
        prompt = _motion_prompt(motion.specification, settings.backdrop)
        await self.prompt_screening.require_allowed(owner_id, "motion", [prompt])
        payload = GenerateMotionPayloadV1(
            requested_settings=requested_settings,
            resolved_recipe=resolved_recipe,
            subject_key=subject.artifact_key,
            motion_id=motion.motion_id,
            prompt=prompt,
            loop=motion.specification.loop_policy.value == "loop",
            width=settings.width,
            height=settings.height,
            duration_seconds=settings.duration_seconds,
            seed=settings.seed,
            preset=settings.preset.value,
            backdrop=settings.backdrop.value,
            steps=settings.steps,
            provider_parameters=settings.provider_parameters,
            requested_revision=current_revision,
            subject_revision_id=subject.revision_id,
            subject_artifact_key=subject.artifact_key,
            subject_sha256=subject.sha256,
            motion_spec_digest=spec_digest,
            view_id=motion.specification.view_id,
            view_reference=motion.specification.view_reference.model_dump(mode="json") if motion.specification.view_reference else None,
            start_frame_key=start_frame.artifact_key
            if start_frame is not None
            else None,
            start_frame_sha256=start_frame.sha256 if start_frame is not None else None,
            composition_revision_id=start_frame.revision_id
            if start_frame is not None
            else None,
            references=tuple(
                MotionReferencePayloadV1(
                    key=reference.key, sha256=reference.sha256, role=reference.role
                )
                for reference in motion.specification.references
            ),
            style=motion.specification.style,
            script=motion.specification.script,
        )
        if motion.specification.view_id is not None:
            if start_frame is not None:
                raise MotionRequestBlocked("view motions cannot use a shared starting pose")
            reference = motion.specification.view_reference
            if reference is None:
                raise MotionRequestBlocked("upload and bind this view's reference before generation")
            current = await self._require_owned_project(project_id, owner_id)
            authoritative = _resolve_motion_view(current.manifest, motion.specification)
            if authoritative.view_reference != reference:
                raise MotionRequestBlocked("view reference changed before dispatch")
            content, width, height = await self._read_view_reference(current.manifest, motion.specification.view_id, reference.key, reference.sha256)
            if (width, height) != (reference.width, reference.height):
                raise MotionRequestBlocked("view reference dimensions changed before dispatch")
            frame_bytes = await asyncio.to_thread(_render_view_opening_frame, content, settings, fill=view_fill)
            frame_hash = sha256(frame_bytes).hexdigest()
            frame_key = f"outputs/studio/{project_id}/opening-frames/view-{frame_hash}.png"
            await self._assets.write_once(frame_key, frame_bytes, "image/png")
            stored_frame = await self._assets.read_limited(frame_key, MAX_SUBJECT_REFERENCE_BYTES)
            if sha256(stored_frame).hexdigest() != frame_hash:
                raise MotionRequestBlocked("prepared view opening frame failed integrity validation")
            payload = payload.model_copy(update={
                "start_frame_key": frame_key, "start_frame_sha256": frame_hash,
                "reference_preparation": {"algorithm": "full_canvas_contain_fill_v2" if view_attack else "full_canvas_contain_v1",
                    **({"fill": view_fill} if view_attack else {}), "source_width": width,
                    "source_height": height, "canvas_width": _clamp(settings.width, _DIMENSION_BOUNDS, _DEFAULT_WIDTH),
                    "canvas_height": _clamp(settings.height, _DIMENSION_BOUNDS, _DEFAULT_HEIGHT),
                    "backdrop": settings.backdrop.value},
            })
        if (start_frame is None and motion.specification.view_id is None
                and (self._motion_pool is not None or hosted_model is not None)):
            # Only the pool and Higgsfield paths consume an opening frame; without a
            # composed opening pose, animate the approved subject itself so
            # the video model has something of the user's character to work
            # from instead of inventing one from the bare text prompt.
            opening_frame = await self._ensure_subject_opening_frame(
                project_id=project_id,
                motion_id=motion.motion_id,
                subject=subject,
                settings=settings,
            )
            if opening_frame is not None:
                frame_key, frame_sha256 = opening_frame
                payload = payload.model_copy(
                    update={
                        "start_frame_key": frame_key,
                        "start_frame_sha256": frame_sha256,
                    }
                )
        if hosted_model is not None and payload.start_frame_key is None:
            raise MotionRequestBlocked("Higgsfield generation needs an opening frame of the approved subject")
        payload = GenerateMotionPayloadV1.model_validate(payload.model_dump(mode="json"))
        submission = JobSubmission(
            job_id=str(uuid4()),
            idempotency_key=f"{project_id}:{motion.motion_id}:{operation_id}",
            operation=JobOperation.GENERATE_MOTION,
            source_key=subject.artifact_key,
            output_key=output_key,
            requested_outputs={},
            project_id=project_id,
            manifest_operation_id=operation_id,
            payload=payload.model_dump(mode="json"),
        )
        # Durable CAS reservation bridges manifest reconciliation and the job
        # ledger. Setup sees the reservation even while submit is in flight on
        # another replica. Never silently replay a missing reservation job.
        from .models import MotionDispatchReservation
        latest = await self._require_owned_project(project_id, owner_id)
        latest_motion = next((m for m in latest.manifest.motions if m.motion_id == motion.motion_id), None)
        latest_subject = latest.manifest.subject.current_revision
        if (latest_motion is None or operation_id in latest.manifest.abandoned_dispatches or motion.motion_id in latest.manifest.removed_motion_ids
                or motion_spec_digest(latest_motion.specification) != spec_digest
                or latest_subject is None or latest_subject.revision_id != subject.revision_id):
            raise MotionRequestBlocked("motion setup changed before dispatch; review the current request")
        if not any(r.operation_id == operation_id for r in latest.manifest.dispatch_reservations):
            reservation = MotionDispatchReservation(operation_id=operation_id,motion_id=motion.motion_id,requested_revision=current_revision)
            await self._store.append(project_id,latest.revision,latest.manifest.model_copy(update={
                "dispatch_reservations":(*latest.manifest.dispatch_reservations,reservation)}))
        try:
            submitted = await self._jobs.submit(submission)
        except Exception:
            # A returned failure is not an in-flight submission. If the ledger
            # can be read and confirms no row, release our reservation by CAS.
            # If the outcome cannot be established, leave the visible fence.
            try:
                recorded = await self._find_dispatch_job(project_id, operation_id)
                if recorded is None:
                    for _ in range(3):
                        failed = await self._require_owned_project(project_id, owner_id)
                        reservations = tuple(r for r in failed.manifest.dispatch_reservations if r.operation_id != operation_id)
                        if reservations == failed.manifest.dispatch_reservations:
                            break
                        try:
                            await self._store.append(project_id,failed.revision,failed.manifest.model_copy(update={"dispatch_reservations":reservations, "abandoned_dispatches": tuple(sorted(set(failed.manifest.abandoned_dispatches) | {operation_id}))}))
                            break
                        except RevisionConflict:
                            continue
            except Exception:
                pass
            raise
        submitted_current = await self._require_owned_project(project_id, owner_id)
        if operation_id in submitted_current.manifest.abandoned_dispatches:
            await self._jobs.cancel(submitted.record.job_id)
            raise MotionRequestBlocked("dispatch was explicitly abandoned before enqueue")
        if hosted_model is not None:
            # Never a Cloud Task or pool job: the listing/reconcile reads fold
            # the provider's outcome back into this row.
            if submitted.created:
                if deferred_submissions is not None:
                    deferred_submissions.append((submitted.record, hosted_model))
                    return submitted.record
                await self._submit_higgsfield(submitted.record, owner_id, hosted_model)
            return (await self._jobs.get(submitted.record.job_id)) or submitted.record
        if self._motion_pool is not None:
            # The pool is the executor: the worker leases this job by
            # capability, renders the approved template, and the status sync
            # folds the attested result back into this row. No Cloud Task is
            # enqueued, so the CPU worker never claims it.
            pool_payload, input_keys = _motion_pool_payload(payload)
            capability_id, requirements = _motion_pool_profile(settings)
            parameters = settings.provider_parameters
            if parameters.get("worker_id"):
                requirements = _pinned_requirements(requirements, capability_id, parameters)
            try:
                await self._motion_pool.submit(
                    PoolJobSubmission(
                        job_id=submitted.record.job_id,
                        idempotency_key=submission.idempotency_key,
                        capability_id=capability_id,
                        input_keys=input_keys,
                        output_key=output_key,
                        payload=pool_payload,
                        # Their own machine when they have one, the shared house
                        # machines when they do not.
                        tenant_id=await self._tenant_for(owner_id),
                        requirements=requirements,
                    )
                )
            except Exception as exc:
                # A bound recipe or a public account never falls back to the
                # Cloud Task path, which runs on the shared GPU endpoint.
                if parameters.get("worker_id") or await self.account_policy.is_public(owner_id):
                    await self._jobs.fail_unclaimed(submitted.record.job_id, type(exc).__name__,
                        failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                        failure_message="The selected machine could not accept this recipe; no fallback was run.",
                        retryable=False)
                    raise MotionGenerationUnavailable("The selected machine could not accept this recipe; no fallback was run.") from exc
                # The pool is preferred but not load-bearing: fall back to the
                # Cloud Tasks execution path rather than losing the submission.
                await self._dispatcher.enqueue(submitted.record.job_id)
            return submitted.record
        await self._dispatcher.enqueue(submitted.record.job_id)
        return submitted.record

    async def _ensure_subject_opening_frame(
        self,
        *,
        project_id: str,
        motion_id: str,
        subject: SubjectRevision,
        settings: MotionGenerationSettings,
    ) -> tuple[str, str] | None:
        """Render (or reuse) the approved subject flattened onto the motion
        canvas/backdrop as an opening frame, keyed per subject revision so
        repeat dispatches for the same subject share one asset. Returns
        `(key, sha256)`, or `None` if rendering or storage fails -- in which
        case the caller dispatches without a `first_frame`, as it did before
        this existed.
        """
        width = _clamp(settings.width, _DIMENSION_BOUNDS, _DEFAULT_WIDTH)
        height = _clamp(settings.height, _DIMENSION_BOUNDS, _DEFAULT_HEIGHT)
        backdrop_rgb = _OPENING_FRAME_BACKDROP_RGB.get(
            settings.backdrop, (127, 127, 127)
        )
        fill = float(settings.provider_parameters.get("framing_fill", 0.86))
        framing_suffix = f"-fill{round(fill * 10000)}" if fill != 0.86 else ""
        key = (
            f"outputs/studio/{project_id}/opening-frames/"
            f"{subject.revision_id}-{width}x{height}-{settings.backdrop.value}{framing_suffix}.png"
        )
        try:
            existing = await self._assets.describe(key)
            if existing is None:
                subject_png = await self._assets.read_limited(
                    subject.artifact_key, MAX_SUBJECT_UPLOAD_BYTES
                )
                frame_bytes = await asyncio.to_thread(
                    render_subject_opening_frame,
                    subject_png,
                    width=width,
                    height=height,
                    backdrop_rgb=backdrop_rgb,
                    fit_fraction=fill,
                )
                await self._assets.write_once(key, frame_bytes, "image/png")
            else:
                frame_bytes = await self._assets.read_limited(
                    key, MAX_SUBJECT_UPLOAD_BYTES
                )
        except Exception:
            logger.warning(
                "failed to render or store the subject opening frame for "
                "project=%s motion=%s subject_revision=%s",
                project_id,
                motion_id,
                subject.revision_id,
                exc_info=True,
            )
            return None
        return key, sha256(frame_bytes).hexdigest()

    async def hosted_provider_options(self, owner_id: str) -> dict[str, Any]:
        """Bring-your-own-key choices for generation discovery. Never chosen
        for the caller: a request uses Higgsfield only when its settings say so."""
        accounts = self.provider_accounts
        if accounts is None or not accounts.available:
            return {HIGGSFIELD: {"available": False, "configured": False,
                                 "reason": "Provider keys are not enabled on this server."}}
        saved = next((c for c in await accounts.describe(owner_id) if c.provider == HIGGSFIELD), None)
        return {HIGGSFIELD: {
            "available": True,
            "configured": saved is not None,
            "key_hint": saved.key_hint if saved else None,
            "billing": "Charged to your own Higgsfield account; Magic Studios adds no fee.",
            "models": [model.describe() for model in higgsfield.MODELS.values()],
            "settings": higgsfield.default_settings() if saved else None,
            "reason": None if saved else "Save a Higgsfield API key to generate with your Higgsfield account.",
        }}

    async def _require_provider_key(self, owner_id: str) -> str:
        accounts = self.provider_accounts
        if accounts is None or not accounts.available:
            raise MotionGenerationUnavailable("Higgsfield keys are not enabled on this server")
        key = await accounts.key_for(owner_id, HIGGSFIELD)
        if key is None:
            raise MotionRequestBlocked("Save your Higgsfield API key before generating with Higgsfield")
        return key

    async def _submit_higgsfield(
        self, record: JobRecord, owner_id: str, model: higgsfield.HiggsfieldModel
    ) -> None:
        """Send one ledger job to the owner's Higgsfield account, at most once."""
        assert self._jobs is not None and self.provider_accounts is not None
        try:
            key = await self._require_provider_key(owner_id)
            reservation = await self.provider_accounts.store.reserve_request(
                record.job_id, owner_id, HIGGSFIELD, model.model_id
            )
        except Exception as exc:
            # Nothing reached Higgsfield, so say so now rather than letting the
            # unconfirmed-submission sweep call it possibly billed later.
            reason = str(exc) if isinstance(exc, (MotionRequestBlocked, MotionGenerationUnavailable)) else type(exc).__name__
            await self._jobs.fail_unclaimed(
                record.job_id, reason,
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=f"This motion was not sent to Higgsfield: {reason}.",
                retryable=True,
            )
            raise
        if reservation is None:
            return
        payload = record.payload
        parameters = payload.get("provider_parameters") or {}
        submitting = False
        try:
            start_key = payload.get("start_frame_key")
            if not isinstance(start_key, str):
                raise MotionRequestBlocked("Higgsfield generation needs an opening frame")
            frame = await self._assets.read_limited(start_key, MAX_SUBJECT_REFERENCE_BYTES)
            if sha256(frame).hexdigest() != payload.get("start_frame_sha256"):
                raise MotionRequestBlocked("the opening frame failed integrity validation")
            client = self.higgsfield_client(key)
            image_url = await client.upload(frame, "image/png")
            arguments = higgsfield.build_arguments(
                model,
                prompt=str(payload["prompt"]),
                image_url=image_url,
                duration_seconds=int(payload["duration_seconds"]),
                end_image_url=image_url if isinstance(parameters, dict) and parameters.get("closed_loop") is True else None,
            )
            submitting = True
            request_id = await client.submit(model.path, arguments)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (higgsfield.HiggsfieldError, MotionRequestBlocked)) else type(exc).__name__
            # Without an HTTP answer, Higgsfield may still have queued (and will bill) it.
            # No answer, or a server error after the POST: Higgsfield may have queued it.
            status_code = getattr(exc, "status_code", None)
            unanswered = submitting and (status_code is None or status_code >= 500)
            note = " Higgsfield may have received it; check your Higgsfield history before retrying." if unanswered else ""
            await self._jobs.fail_unclaimed(
                record.job_id, reason,
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=f"Higgsfield did not accept this motion: {reason}. It was not resubmitted.{note}",
                retryable=True,
            )
            raise MotionGenerationUnavailable(f"Higgsfield did not accept this motion: {reason}") from exc
        await self.provider_accounts.store.record_request_id(record.job_id, request_id)
        # A cancel that landed while the request was in flight found no provider
        # ID to cancel; honour it now that one exists.
        current = await self._jobs.get(record.job_id)
        if current is not None and current.status is JobStatus.CANCELLED:
            await self._cancel_hosted_request(current)

    async def _sync_hosted_motion_records(self, records: Iterable[JobRecord]) -> None:
        """Fold Higgsfield outcomes into the ledger, as the pool sync does."""
        if self.provider_accounts is None or self._jobs is None:
            return
        hosted = [
            record for record in records
            if record.operation is JobOperation.GENERATE_MOTION
            and record.status in _IN_FLIGHT_JOB_STATUSES
            and higgsfield.is_higgsfield(record.payload.get("provider_parameters") or {})
        ]
        # Independent requests, run concurrently under the service-wide limit so
        # a slow download cannot push one read past the API request timeout and
        # overlapping reads cannot hold many large results in memory at once.
        async def sync(record: JobRecord) -> None:
            if record.job_id in self._hosted_syncs_in_flight:
                return  # another read is already collecting it
            self._hosted_syncs_in_flight.add(record.job_id)
            try:
                async with self._hosted_sync_limit:
                    await self._sync_higgsfield_record(record)
            except Exception:
                # The next poll retries; one unreadable request must not stall the rest.
                logger.warning("higgsfield sync: failed for job %s", record.job_id, exc_info=True)
            finally:
                self._hosted_syncs_in_flight.discard(record.job_id)

        await asyncio.gather(*(sync(record) for record in hosted))

    async def _sync_higgsfield_record(self, record: JobRecord) -> None:
        assert self._jobs is not None and self.provider_accounts is not None
        request = await self.provider_accounts.store.get_request(record.job_id)
        if request is None or request.request_id is None:
            reserved = request.reserved_at if request is not None else record.created_at
            if reserved is not None and datetime.now(UTC) - reserved > _UNCONFIRMED_PROVIDER_SUBMISSION:
                await self._jobs.fail_unclaimed(
                    record.job_id, "provider submission outcome unknown",
                    failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                    failure_message=("Higgsfield never confirmed this request, so it was not resubmitted. "
                                     "Check your Higgsfield history before trying again."),
                    retryable=True,
                )
            return
        try:
            key = await self.provider_accounts.key_for(request.owner_id, HIGGSFIELD)
        except ProviderCredentialRejected:
            # Undecryptable (rotated-away secret or corrupt row): as unusable as a removed key.
            key = None
        if key is None:
            await self._jobs.fail_unclaimed(
                record.job_id, "provider key removed or unreadable",
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=("Your Higgsfield key was removed or can no longer be read, so this motion cannot be "
                                 "collected. Save the key again for future motions. "
                                 "Higgsfield may still complete and bill the request."),
                retryable=True,
            )
            return
        client = self.higgsfield_client(key)
        try:
            status = await client.status(request.request_id)
        except higgsfield.HiggsfieldError as exc:
            # A rejected key or an unknown request will not recover on a later
            # poll; anything else (network, 5xx, 429) is retried next time.
            if exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500:
                raise
            await self._jobs.fail_unclaimed(
                record.job_id, str(exc),
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=(f"Magic Studios can no longer read this Higgsfield request ({exc.status_code}). "
                                 "Higgsfield may still complete and bill it; check your Higgsfield history."),
                retryable=True,
            )
            return
        if not status.terminal:
            return
        if status.status == "canceled":
            await self._jobs.cancel(record.job_id)
            return
        if status.status == "nsfw":
            await self._jobs.fail_unclaimed(
                record.job_id, "provider safety filter",
                failure_code=JobFailureCode.INVALID_INPUT,
                failure_message="Higgsfield's safety filter rejected this motion.",
                retryable=False,
            )
            return
        if status.status != "completed" or status.video_url is None:
            await self._jobs.fail_unclaimed(
                record.job_id, status.error or f"provider status {status.status}",
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message="Higgsfield could not complete this motion.",
                retryable=True,
            )
            return
        try:
            content = await client.download(status.video_url, MAX_MOTION_OUTPUT_BYTES)
        except higgsfield.HiggsfieldError as exc:
            if not exc.permanent:
                raise  # network, 5xx or 429: the next poll retries
            await self._jobs.fail_unclaimed(
                record.job_id, str(exc),
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=(f"Higgsfield finished but its result could not be used ({exc}); nothing was stored. "
                                 "It may still be in your Higgsfield history."),
                retryable=True,
            )
            return
        try:
            # Only a decodable video may become the immutable output.
            async with _MOTION_MEASUREMENT_LIMIT:
                await asyncio.to_thread(inspect_motion_artifact, content)
        except (ValueError, MotionMeasurementBudgetExceeded) as exc:
            await self._jobs.fail_unclaimed(
                record.job_id, f"undecodable provider result: {type(exc).__name__}",
                failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                failure_message=("Higgsfield finished but returned a file that is not a playable video; "
                                 "nothing was stored. It may still be in your Higgsfield history."),
                retryable=True,
            )
            return
        digest = sha256(content).hexdigest()
        latest = await self._jobs.get(record.job_id)
        if latest is None or latest.status not in _IN_FLIGHT_JOB_STATUSES:
            return  # cancelled or settled while downloading: publish nothing
        if not await self._assets.write_once(record.output_key, content, "video/mp4"):
            # A concurrent sync already stored it; accept only identical bytes.
            stored = await self._assets.read_limited(record.output_key, MAX_MOTION_OUTPUT_BYTES)
            if sha256(stored).hexdigest() != digest:
                await self._jobs.fail_unclaimed(
                    record.job_id, "immutable output conflict",
                    failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                    failure_message="A different result already exists for this motion; nothing was overwritten.",
                    retryable=True,
                )
                return
        await self._jobs.complete_unclaimed(
            record.job_id,
            VideoJobResult(
                output_key=record.output_key,
                execution_provenance=_higgsfield_execution_provenance(record, request, digest),
            ),
        )

    async def _cancel_hosted_request(self, record: JobRecord) -> None:
        """Best effort: Higgsfield cancels only requests that have not started.
        A refusal is logged; the UI tells owners a started request is still billed."""
        if self.provider_accounts is None or not higgsfield.is_higgsfield(record.payload.get("provider_parameters") or {}):
            return
        accepted = False
        with suppress(Exception):
            request = await self.provider_accounts.store.get_request(record.job_id)
            if request is not None and request.request_id is not None:
                key = await self.provider_accounts.key_for(request.owner_id, HIGGSFIELD)
                if key is not None:
                    accepted = await self.higgsfield_client(key).cancel(request.request_id)
        if not accepted:
            logger.warning("higgsfield cancel: provider did not confirm cancellation for job %s", record.job_id)

    async def generate_motion(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        actor_id: str,
        request_id: str | None = None,
        settings: MotionGenerationSettings | None = None,
        action: str | None = None,
    ) -> JobRecord:
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        if self._dispatcher is None:
            raise RuntimeError("motion task dispatcher is not configured")
        if not self._motion_generation_available:
            raise MotionGenerationUnavailable(
                "motion generation is disabled until compatible workers are ready"
            )
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        subject = current.manifest.subject.current_revision
        approval = current.manifest.subject.approval
        if (
            subject is None
            or approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}
            or approval.artifact_sha256 != subject.sha256
        ):
            raise MotionRequestBlocked(
                "motion generation requires the approved current subject"
            )
        await self._require_headroom_to_start_work(current.manifest.owner_id)
        await self._require_motion_compute(
            current.manifest.owner_id, settings or MotionGenerationSettings()
        )
        if action is not None:
            await self.prompt_screening.require_allowed(current.manifest.owner_id, "motion", [action])
            current = await self._apply_motion_actions(
                project_id, current, {motion_id: action}
            )
        motion = next(
            (item for item in current.manifest.motions if item.motion_id == motion_id),
            None,
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        if action is None and motion.specification.kind is MotionKind.PRESET:
            current = await self._apply_motion_actions(project_id, current, {motion_id: motion.specification.action or ""})
            motion = next(item for item in current.manifest.motions if item.motion_id == motion_id)
        request_id = request_id or uuid4().hex
        settings = settings or MotionGenerationSettings()
        if not request_id or len(request_id) > 100:
            raise ValueError("motion request_id must contain 1 to 100 characters")
        return await self._dispatch_motion_generation(
            project_id=project_id,
            current_revision=current.revision,
            motion=motion,
            subject=subject,
            request_id=request_id,
            settings=settings,
            owner_id=current.manifest.owner_id,
        )

    async def start_turnaround(self, project_id: str, expected_revision: int, source_view_id: str,
                               source_revision_id: str, equipment, actor_id: str, request_id: str,
                               settings: MotionGenerationSettings | None = None):
        from . import turnarounds
        return await turnarounds.start(self, project_id, expected_revision, source_view_id,
                                       source_revision_id, tuple(equipment), actor_id, request_id, settings)

    async def settle_turnaround(self, job_id: str, actor_id: str) -> StoredManifest:
        from . import turnarounds
        return await turnarounds.settle(self, job_id, actor_id)

    async def get_turnaround(self, project_id: str, turnaround_id: str, actor_id: str) -> dict[str, Any]:
        from . import turnarounds
        return await turnarounds.get(self, project_id, turnaround_id, actor_id)

    async def list_turnarounds(self, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        from . import turnarounds
        return await turnarounds.list_takes(self, project_id, actor_id)

    async def turnaround_frames(self, project_id: str, turnaround_id: str, actor_id: str) -> dict:
        from . import turnarounds
        return await turnarounds.frames(self, project_id, turnaround_id, actor_id)

    async def create_view_draft(self, project_id: str, expected_revision: int, turnaround_id: str,
                                pick_revision: int, heading: ViewId, standing: bool, request_id: str,
                                actor_id: str) -> dict[str, Any]:
        from . import view_drafts
        return await view_drafts.create(self, project_id, expected_revision, turnaround_id, pick_revision,
                                        heading, standing, request_id, actor_id)

    async def list_view_drafts(self, project_id: str, actor_id: str) -> list[dict[str, Any]]:
        from . import view_drafts
        return await view_drafts.list_drafts(self, project_id, actor_id)

    async def check_view_draft(self, project_id: str, draft_id: str, expected_revision: int, facing: bool,
                               equipment: bool, identity: bool, actor_id: str) -> dict[str, Any]:
        from . import view_drafts
        return await view_drafts.check(self, project_id, draft_id, expected_revision, facing, equipment,
                                       identity, actor_id)

    async def discard_view_draft(self, project_id: str, draft_id: str, expected_revision: int,
                                 actor_id: str) -> dict[str, Any]:
        from . import view_drafts
        return await view_drafts.discard(self, project_id, draft_id, expected_revision, actor_id)

    async def save_turnaround_picks(self, project_id: str, turnaround_id: str, expected_pick_revision: int,
                                    picks, actor_id: str) -> dict[str, Any]:
        from . import turnarounds
        return await turnarounds.save_picks(self, project_id, turnaround_id, expected_pick_revision, picks, actor_id)

    async def cancel_turnaround(self, project_id: str, turnaround_id: str, actor_id: str) -> JobRecord:
        from . import turnarounds
        return await turnarounds.cancel(self, project_id, turnaround_id, actor_id)

    async def cancel_motion_operation(
        self, project_id: str, motion_id: str, job_id: str, actor_id: str
    ) -> JobRecord:
        """Cancel a queued or running motion job (T13). Delegates to the job store's
        cancel, which already fences a racing completion via the claim token — a job
        cancelled here can never later settle. An already-terminal job is rejected
        with `OperationAlreadyTerminal` naming its current state rather than silently
        no-opping, so the caller can show the operator what actually happened.
        """
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        job = await self._jobs.get(job_id)
        if (
            job is None
            or job.project_id != project_id
            or job.payload.get("motion_id") != motion_id
        ):
            raise ProjectNotFound(project_id)
        await self._require_owned_project(project_id, actor_id)
        if job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            raise OperationAlreadyTerminal(job.status)
        cancelled = await self._jobs.cancel(job_id)
        if (
            cancelled
            and self._motion_pool is not None
            and job.operation is JobOperation.GENERATE_MOTION
        ):
            # Best effort: a pool render that finishes anyway is reconciled as
            # completed, but the cancelled ledger row can never settle.
            with suppress(Exception):
                await self._motion_pool.cancel(job_id)
        if cancelled:
            await self._cancel_hosted_request(job)
        updated = await self._jobs.get(job_id)
        assert updated is not None
        if not cancelled:
            # The job reached a terminal state between our read and the cancel; report
            # what actually happened rather than a cancellation that never took effect.
            raise OperationAlreadyTerminal(updated.status)
        return updated

    async def cancel_all_operations(
        self, project_id: str, actor_id: str
    ) -> CancelAllResult:
        """Cancel every queued or running operation for a project in one call (T13),
        mirroring the reference game's "cancel all" queue action. Isolates a per-job race the same
        way `generate_motions` isolates a per-motion dispatch failure: one job settling
        between the listing read and the cancel call is reported as skipped rather than
        failing the whole request.
        """
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        await self._require_owned_project(project_id, actor_id)
        return await self._cancel_live_jobs(project_id)

    async def _cancel_live_jobs(self, project_id: str) -> CancelAllResult:
        assert self._jobs is not None
        records = await self._jobs.list_integration_relevant_for_project(project_id)
        live = [
            record
            for record in records
            if record.status in {JobStatus.QUEUED, JobStatus.PROCESSING}
        ]
        cancelled: list[str] = []
        skipped: list[SkippedCancellation] = []
        for record in live:
            accepted = await self._jobs.cancel(record.job_id)
            if accepted:
                if (
                    self._motion_pool is not None
                    and record.operation in _POOL_OPERATIONS
                ):
                    # Best effort, same as cancel_motion_operation.
                    with suppress(Exception):
                        await self._motion_pool.cancel(record.job_id)
                await self._cancel_hosted_request(record)
                cancelled.append(record.job_id)
                continue
            current = await self._jobs.get(record.job_id)
            skipped.append(
                SkippedCancellation(
                    job_id=record.job_id,
                    state=current.status if current is not None else record.status,
                )
            )
        return CancelAllResult(cancelled=tuple(cancelled), skipped=tuple(skipped))

    async def settle_motion_candidate(
        self, job_id: str, actor_id: str
    ) -> StoredManifest:
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        job = await self._jobs.get(job_id)
        if (
            job is None
            or job.operation is not JobOperation.GENERATE_MOTION
            or job.status is not JobStatus.COMPLETED
            or job.result is None
            or job.project_id is None
            or job.manifest_operation_id is None
        ):
            raise MotionOperationNotReady("motion operation is not completed")
        current = await self._require_owned_project(job.project_id, actor_id)
        requested_revision = job.payload.get("requested_revision")
        if (
            not isinstance(requested_revision, int)
            or requested_revision < 1
            or requested_revision > current.revision
        ):
            raise MotionLineageConflict("requested manifest revision is invalid")
        if job.result.output_key != job.output_key:
            raise MotionLineageConflict(
                "completed motion output key differs from its request"
            )
        if (
            job.payload.get("subject_key") != job.source_key
            or job.payload.get("subject_artifact_key") != job.source_key
        ):
            raise MotionLineageConflict(
                "motion operation subject key differs from its ledger source"
            )
        motion_id = job.payload.get("motion_id")
        if not isinstance(motion_id, str):
            raise MotionOperationNotReady("motion operation payload is invalid")
        motion = next(
            (item for item in current.manifest.motions if item.motion_id == motion_id),
            None,
        )
        if (motion is None or motion_id in current.manifest.removed_motion_ids
                or job.manifest_operation_id in current.manifest.abandoned_dispatches):
            raise MotionOperationNotReady("motion definition no longer exists")
        # Drift (a replaced subject, an edited spec, a new opening pose) is a warning,
        # not a blocker: the take lands carrying its own lineage and the set-level
        # "stale lineage" finding raises it at approval time.
        subject = current.manifest.subject.current_revision
        composition_revision_id = job.payload.get("composition_revision_id")
        if any(
            item.candidate_id == job.manifest_operation_id for item in motion.candidates
        ):
            return current
        try:
            artifact = await self._assets.read_limited(
                job.result.output_key, MAX_MOTION_OUTPUT_BYTES
            )
        except AssetNotFound as exc:
            raise MotionAssetError(
                "motion_asset_missing", "completed motion artifact is missing"
            ) from exc
        except AssetTooLarge as exc:
            raise MotionAssetError(
                "motion_asset_too_large",
                "completed motion artifact exceeds measurement limits",
            ) from exc
        try:
            async with _MOTION_MEASUREMENT_LIMIT:
                metadata = await asyncio.to_thread(inspect_motion_artifact, artifact)
        except MotionMeasurementBudgetExceeded as exc:
            raise MotionAssetError("motion_asset_too_large", str(exc)) from exc
        except ValueError as exc:
            raise MotionAssetError(
                "motion_asset_undecodable", "completed motion artifact is not decodable"
            ) from exc
        candidate = MotionCandidate(
            candidate_id=job.manifest_operation_id,
            artifact_key=job.result.output_key,
            sha256=sha256(artifact).hexdigest(),
            execution_provenance=job.result.execution_provenance,
            duration_ms=round(metadata.duration_ms) if metadata.duration_ms is not None else _motion_duration_ms(job),
            metadata=metadata,
            generation_settings=MotionGenerationSettings(
                preset=job.payload.get("preset", "standard"),
                steps=job.payload.get("steps", 20),
                seed=job.payload.get("seed", 0),
                width=job.payload.get(
                    "width", subject.width if subject else _DEFAULT_WIDTH
                ),
                height=job.payload.get(
                    "height", subject.height if subject else _DEFAULT_HEIGHT
                ),
                duration_seconds=job.payload.get("duration_seconds", 4),
                backdrop=job.payload.get("backdrop", "chroma_green"),
                provider_parameters=job.payload.get("provider_parameters", {}),
            ),
            composition_revision_id=(
                composition_revision_id
                if isinstance(composition_revision_id, str)
                else None
            ),
            subject_revision_id=(
                job.payload.get("subject_revision_id")
                if isinstance(job.payload.get("subject_revision_id"), str)
                else None
            ),
            motion_spec_digest=(
                job.payload.get("motion_spec_digest")
                if isinstance(job.payload.get("motion_spec_digest"), str)
                else None
            ),
        )
        candidate = candidate.model_copy(
            update={
                "assessment": assess_motion(candidate, motion.specification.loop_policy)
            }
        )
        manifest = add_motion_candidate(
            current.manifest, motion_id=motion_id, candidate=candidate
        )
        await self._record_stored_bytes(manifest.owner_id, candidate.artifact_key)
        settled = await self._store.append(job.project_id, current.revision, manifest)
        return await self._maybe_auto_derive_alpha(
            settled, motion_id, candidate, actor_id
        )

    async def _maybe_auto_derive_alpha(
        self,
        settled: StoredManifest,
        motion_id: str,
        candidate: MotionCandidate,
        actor_id: str,
    ) -> StoredManifest:
        """After settling a chroma_green candidate with no measured alpha, run the
        alpha key-out once automatically (T18), mirroring the reference Video Lab's behavior.
        Never runs for `native_alpha`; best-effort so a transient derivation failure
        does not fail the settle -- the `motion_missing_alpha` finding's `derive_alpha`
        action stays available for a manual retry.
        """
        settings = candidate.generation_settings
        if (
            settings is None
            or settings.backdrop is not Backdrop.CHROMA_GREEN
            or candidate.metadata is None
            or candidate.metadata.has_alpha
        ):
            return settled
        try:
            return await self.derive_alpha(
                settled.project_id,
                settled.revision,
                motion_id,
                candidate.candidate_id,
                actor_id,
            )
        except (MotionDerivationError, MotionAssetError, ValueError):
            return settled

    async def settle_project_motion_candidate(
        self,
        project_id: str,
        motion_id: str,
        job_id: str,
        expected_revision: int,
        actor_id: str,
    ) -> StoredManifest:
        if self._jobs is None:
            raise RuntimeError("motion execution store is not configured")
        job = await self._jobs.get(job_id)
        if (
            job is None
            or job.project_id != project_id
            or job.payload.get("motion_id") != motion_id
        ):
            raise ProjectNotFound(project_id)
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        if any(
            candidate.candidate_id == job.manifest_operation_id
            for motion in current.manifest.motions
            if motion.motion_id == motion_id
            for candidate in motion.candidates
        ):
            return current
        return await self.settle_motion_candidate(job_id, actor_id)

    async def assess_set_quality(
        self, project_id: str, expected_revision: int, actor_id: str
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        assessment = assess_set(
            current.manifest.motions,
            current_subject_revision_id=current.manifest.subject.current_revision_id,
        )
        digest = set_digest(current.manifest)
        assessment_id = sha256(
            f"{digest}:{assessment.model_dump_json()}".encode()
        ).hexdigest()
        manifest = current.manifest.with_set_assessment(
            assessment_id, digest, assessment
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def select_candidate(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        candidate_id: str,
        actor_id: str,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        manifest = select_motion_candidate(
            current.manifest, motion_id=motion_id, candidate_id=candidate_id
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def create_motion_revision(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        source_candidate_id: str,
        actor_id: str,
        *,
        ordered_frames: tuple[FrameSlot, ...],
        frame_duration_ms: int,
        loop_policy: LoopPolicy,
        flip_horizontal: bool = False,
        target_frame_count: int | None = None,
        cell_size: int | None = None,
        normalization: NormalizationSpec | None = None,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        motion = next(
            (item for item in current.manifest.motions if item.motion_id == motion_id),
            None,
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        source = next(
            (
                candidate
                for candidate in motion.candidates
                if candidate.candidate_id == source_candidate_id
            ),
            None,
        )
        if source is None:
            raise ValueError("source candidate is not present for the requested motion")
        if source.metadata is None:
            raise ValueError("source candidate has no measured frame metadata")
        facing = (
            (Facing.LEFT if source.facing is Facing.RIGHT else Facing.RIGHT)
            if flip_horizontal
            else source.facing
        )
        if normalization is not None and normalization.reference_candidate_id is not None:
            reference_motion = next((item for item in current.manifest.motions
                if any(candidate.candidate_id == normalization.reference_candidate_id for candidate in item.candidates)), None)
            if reference_motion is None:
                raise ValueError("normalization reference candidate is not in this project")
            if reference_motion.specification.view_id != motion.specification.view_id:
                raise ValueError("body normalization requires a reference in the same view")
        if normalization is not None and (target_frame_count is None or cell_size is None):
            raise ValueError("normalization requires target_frame_count and cell_size")
        if target_frame_count is not None or cell_size is not None:
            if target_frame_count is None or cell_size is None:
                raise ValueError(
                    "resampling requires both target_frame_count and cell_size"
                )
            return await self._create_resampled_motion_revision(
                current,
                project_id,
                motion_id,
                source,
                ordered_frames,
                frame_duration_ms,
                loop_policy,
                facing,
                target_frame_count,
                cell_size,
                normalization,
            )
        edit_identity = ":".join(
            (
                source.sha256,
                ",".join(f"{slot.index}:{slot.hold}" for slot in ordered_frames),
                str(frame_duration_ms),
                loop_policy.value,
                facing.value,
            )
        )
        total_hold = sum(slot.hold for slot in ordered_frames)
        candidate = MotionCandidate(
            candidate_id=sha256(edit_identity.encode()).hexdigest()[:32],
            artifact_key=source.artifact_key,
            sha256=source.sha256,
            lifecycle=source.lifecycle,
            parent_revision_ids=(source.candidate_id,),
            duration_ms=frame_duration_ms * total_hold,
            metadata=source.metadata,
            ordered_frames=ordered_frames,
            frame_duration_ms=frame_duration_ms,
            loop_policy=loop_policy,
            generation_settings=source.generation_settings,
            execution_provenance=source.execution_provenance,
            facing=facing,
            subject_revision_id=source.subject_revision_id,
            motion_spec_digest=source.motion_spec_digest,
            composition_revision_id=source.composition_revision_id,
            recipe=source.recipe,
        )
        candidate = candidate.model_copy(
            update={"assessment": assess_motion(candidate, loop_policy)}
        )
        manifest = add_derived_motion_candidate(
            current.manifest, motion_id=motion_id, candidate=candidate
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def _create_resampled_motion_revision(
        self,
        current: StoredManifest,
        project_id: str,
        motion_id: str,
        source: MotionCandidate,
        ordered_frames: tuple[FrameSlot, ...],
        frame_duration_ms: int,
        loop_policy: LoopPolicy,
        facing: Facing,
        target_frame_count: int,
        cell_size: int,
        normalization: NormalizationSpec | None = None,
    ) -> StoredManifest:
        """T19: resample a candidate to a new packed PNG strip at `target_frame_count`
        frames of `cell_size` cells, sampling evenly across the playback order the
        caller supplied (`ordered_frames`/holds), padding a short source by repeating
        its final frame. Always produces a NEW artifact -- unlike the reorder/hold-only
        path above, the pixels themselves change."""
        assert source.metadata is not None
        edit_identity = ":".join(
            (
                source.sha256,
                ",".join(f"{slot.index}:{slot.hold}" for slot in ordered_frames),
                str(frame_duration_ms),
                loop_policy.value,
                facing.value,
                str(target_frame_count),
                str(cell_size),
            )
        )
        if normalization is not None:
            edit_identity += ":normalize-v1:" + normalization.model_dump_json()
            if normalization.cleanup_green_edges:
                from magic_video_editor.studio.matte import MATTE_VERSION

                edit_identity += ":" + MATTE_VERSION
        candidate_id = sha256(edit_identity.encode()).hexdigest()[:32]
        motion = next(
            item for item in current.manifest.motions if item.motion_id == motion_id
        )
        existing = next(
            (item for item in motion.candidates if item.candidate_id == candidate_id),
            None,
        )
        if existing is not None:
            if motion.selected_candidate_id == candidate_id:
                return current
            manifest = select_motion_candidate(
                current.manifest, motion_id=motion_id, candidate_id=candidate_id
            )
            return await self._store.append(project_id, current.revision, manifest)
        try:
            artifact = await self._assets.read_limited(
                source.artifact_key, MAX_MOTION_OUTPUT_BYTES
            )
        except AssetNotFound as exc:
            raise MotionAssetError(
                "motion_asset_missing", "source motion artifact is missing"
            ) from exc
        except AssetTooLarge as exc:
            raise MotionAssetError(
                "motion_asset_too_large",
                "source motion artifact exceeds measurement limits",
            ) from exc
        if source.metadata.media_kind is MediaKind.SHEET:
            raw_tiles = await asyncio.to_thread(
                split_sheet_into_tiles, artifact, source.metadata.frame_count
            )
        else:
            try:
                raw_tiles = await extract_video_frames(
                    artifact, source.metadata.frame_count
                )
            except FrameExtractionFailed as exc:
                raise MotionDerivationError(
                    "motion_resample_failed", "video frame extraction failed"
                ) from exc
        sequence = expand_ordered_sequence(raw_tiles, ordered_frames)
        sampled = even_sample_with_padding(sequence, target_frame_count)
        try:
            packed_bytes = await asyncio.to_thread(
                pack_cell_sheet, sampled, cell_size, normalization=normalization
            )
        except (OSError, ValueError) as exc:
            raise MotionDerivationError(
                "motion_resample_failed", "frame packing failed"
            ) from exc
        output_key = (
            f"outputs/studio/{project_id}/motions/{motion_id}/{candidate_id}.png"
        )
        await self._assets.write_once(output_key, packed_bytes, "image/png")
        try:
            metadata = await asyncio.to_thread(inspect_motion_artifact, packed_bytes)
        except MotionMeasurementBudgetExceeded as exc:
            raise MotionAssetError("motion_asset_too_large", str(exc)) from exc
        except ValueError as exc:
            raise MotionAssetError(
                "motion_asset_undecodable", "resampled artifact is not decodable"
            ) from exc
        candidate = MotionCandidate(
            candidate_id=candidate_id,
            artifact_key=output_key,
            sha256=sha256(packed_bytes).hexdigest(),
            parent_revision_ids=(source.candidate_id,),
            duration_ms=frame_duration_ms * target_frame_count,
            metadata=metadata,
            frame_duration_ms=frame_duration_ms,
            loop_policy=loop_policy,
            generation_settings=source.generation_settings,
            execution_provenance=source.execution_provenance,
            facing=facing,
            source=CandidateSource.DERIVED,
            recipe=DerivationRecipe(
                op="normalize" if normalization is not None else "resample",
                target_frame_count=target_frame_count,
                cell_size=metadata.height,
                normalization=normalization,
                source_ordered_frames=ordered_frames,
                sampled_source_indices=tuple(even_sample_with_padding(
                    [slot.index for slot in ordered_frames for _ in range(slot.hold)], target_frame_count
                )),
            ),
            subject_revision_id=source.subject_revision_id,
            motion_spec_digest=source.motion_spec_digest,
            composition_revision_id=source.composition_revision_id,
        )
        candidate = candidate.model_copy(
            update={"assessment": assess_motion(candidate, loop_policy)}
        )
        manifest = add_derived_motion_candidate(
            current.manifest, motion_id=motion_id, candidate=candidate
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def derive_alpha(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        candidate_id: str,
        actor_id: str,
    ) -> StoredManifest:
        """Key out a chroma-green backdrop into a new derived candidate carrying alpha
        (T18). Deterministic on `(source.sha256, "derive_alpha")`: replaying the same
        request against the same source candidate is a no-op, which is what makes the
        automatic post-settle pass in `settle_motion_candidate` safe to fire more than
        once. Auto-selected only when the source candidate is the motion's current
        selection, matching the reference game's "remove it and publish an alpha clip".
        """
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        motion = next(
            (item for item in current.manifest.motions if item.motion_id == motion_id),
            None,
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        source = next(
            (
                candidate
                for candidate in motion.candidates
                if candidate.candidate_id == candidate_id
            ),
            None,
        )
        if source is None:
            raise ValueError("source candidate is not present for the requested motion")
        if source.metadata is None:
            raise ValueError("source candidate has no measured frame metadata")
        derived_id = _alpha_derivation_identity(source)
        existing = next(
            (item for item in motion.candidates if item.candidate_id == derived_id),
            None,
        )
        if existing is not None:
            return current
        if source.metadata.has_alpha:
            raise MotionDerivationError(
                "motion_already_has_alpha", "source candidate already carries alpha"
            )
        try:
            artifact = await self._assets.read_limited(
                source.artifact_key, MAX_MOTION_OUTPUT_BYTES
            )
        except AssetNotFound as exc:
            raise MotionAssetError(
                "motion_asset_missing", "source motion artifact is missing"
            ) from exc
        except AssetTooLarge as exc:
            raise MotionAssetError(
                "motion_asset_too_large",
                "source motion artifact exceeds measurement limits",
            ) from exc
        if source.metadata.media_kind is MediaKind.SHEET:
            try:
                derived_bytes = await asyncio.to_thread(chromakey_sheet, artifact)
            except (OSError, ValueError) as exc:
                raise MotionDerivationError(
                    "motion_alpha_derivation_failed", "sheet alpha derivation failed"
                ) from exc
            extension = "png"
        else:
            try:
                derived_bytes = await chromakey_video_to_alpha_webm(artifact)
            except ChromakeyDerivationFailed as exc:
                raise MotionDerivationError(
                    "motion_alpha_derivation_failed", "video alpha derivation failed"
                ) from exc
            extension = "webm"
        try:
            metadata = await asyncio.to_thread(inspect_motion_artifact, derived_bytes)
        except MotionMeasurementBudgetExceeded as exc:
            raise MotionAssetError("motion_asset_too_large", str(exc)) from exc
        except ValueError as exc:
            raise MotionAssetError(
                "motion_asset_undecodable", "derived alpha artifact is not decodable"
            ) from exc
        output_key = (
            f"outputs/studio/{project_id}/motions/{motion_id}/{derived_id}.{extension}"
        )
        await self._assets.write_once(
            output_key,
            derived_bytes,
            "image/png" if extension == "png" else "video/webm",
        )
        candidate = MotionCandidate(
            candidate_id=derived_id,
            artifact_key=output_key,
            sha256=sha256(derived_bytes).hexdigest(),
            parent_revision_ids=(source.candidate_id,),
            duration_ms=(round(metadata.duration_ms) if source.frame_duration_ms is None and source.ordered_frames is None and metadata.duration_ms else source.duration_ms),
            metadata=metadata,
            ordered_frames=source.ordered_frames,
            frame_duration_ms=source.frame_duration_ms,
            loop_policy=source.loop_policy,
            generation_settings=source.generation_settings,
            execution_provenance=source.execution_provenance,
            facing=source.facing,
            source=CandidateSource.DERIVED,
            recipe=DerivationRecipe(
                op="derive_alpha",
                key="0x00B140",
                similarity=0.10,
                blend=0.08,
            ),
            subject_revision_id=source.subject_revision_id,
            motion_spec_digest=source.motion_spec_digest,
            composition_revision_id=source.composition_revision_id,
        )
        candidate = candidate.model_copy(
            update={
                "assessment": assess_motion(
                    candidate, source.loop_policy or motion.specification.loop_policy
                )
            }
        )
        if motion.selected_candidate_id == source.candidate_id:
            manifest = add_derived_motion_candidate(
                current.manifest, motion_id=motion_id, candidate=candidate
            )
        else:
            manifest = add_motion_candidate(
                current.manifest, motion_id=motion_id, candidate=candidate
            )
        return await self._store.append(project_id, current.revision, manifest)

    def _find_candidate(
        self, manifest: AnimationProjectManifest, motion_id: str, candidate_id: str
    ) -> MotionCandidate:
        motion = next(
            (item for item in manifest.motions if item.motion_id == motion_id), None
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        candidate = next(
            (item for item in motion.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            raise ValueError("candidate is not present for the requested motion")
        if candidate.metadata is None:
            raise ValueError("candidate has no measured frame metadata")
        return candidate

    async def _cached_candidate_download(
        self,
        project_id: str,
        cache_key: str,
        content_type: str,
        builder: Callable[[], Awaitable[bytes]],
        download_filename: str,
    ) -> UploadTarget:
        # Only reuse a materialized export.
        if await self._assets.describe(cache_key) is None:
            content = await builder()
            await self._assets.write_once(cache_key, content, content_type)
        return UploadTarget(
            key=cache_key, upload_url=self._file_link(project_id, cache_key, download_filename)
        )

    def _candidate_download_filename(
        self,
        manifest: AnimationProjectManifest,
        motion_id: str,
        candidate_id: str,
        extension: str,
    ) -> str:
        motion = next(
            (item for item in manifest.motions if item.motion_id == motion_id), None
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        take = _candidate_take_number(motion, candidate_id)
        return f"{_slug(manifest.name)}-{_slug(motion_id)}-take-{take}.{extension}"

    async def _read_candidate_sheet_bytes(self, candidate: MotionCandidate) -> bytes:
        try:
            artifact = await self._assets.read_limited(
                candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES
            )
        except AssetNotFound as exc:
            raise MotionAssetError(
                "motion_asset_missing", "candidate artifact is missing"
            ) from exc
        except AssetTooLarge as exc:
            raise MotionAssetError(
                "motion_asset_too_large",
                "candidate artifact exceeds measurement limits",
            ) from exc
        return await render_candidate_sheet(artifact, candidate)

    async def analyze_candidate_frames(
        self, project_id: str, motion_id: str, candidate_id: str, actor_id: str
    ) -> dict:
        """Authorize before reading an immutable cache of decoded native frames."""
        from magic_video_editor.studio.frame_analysis import ANALYSIS_VERSION, analyze_candidate_frames

        current = await self._require_owned_project(project_id, actor_id)
        candidate = self._find_candidate(current.manifest, motion_id, candidate_id)
        key = (f"outputs/studio/{project_id}/motions/{motion_id}/{candidate_id}/"
               f"frames-{ANALYSIS_VERSION}-{candidate.sha256[:16]}.json")
        if await self._assets.describe(key) is not None:
            return _candidate_analysis_registration(json.loads(await self._assets.read_limited(key, 100_000_000)), candidate)
        try:
            content = await self._assets.read_limited(candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES)
            if sha256(content).hexdigest() != candidate.sha256:
                raise ValueError("candidate asset digest does not match metadata")
            result = await asyncio.to_thread(analyze_candidate_frames, content, candidate)
        except (AssetNotFound, AssetTooLarge) as exc:
            raise MotionAssetError("motion_asset_missing", "candidate asset cannot be read") from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise MotionDerivationError("frame_analysis_failed", str(exc)) from exc
        encoded = json.dumps(result, separators=(",", ":")).encode()
        if len(encoded) > 100_000_000:
            raise MotionDerivationError("frame_analysis_failed", "frame previews exceed size budget")
        await self._assets.write_once(key, encoded, "application/json")
        return _candidate_analysis_registration(result, candidate)

    async def suggest_candidate_loop(
        self, project_id: str, motion_id: str, candidate_id: str, actor_id: str,
        *, mode: str, target_frame_count: int,
    ) -> dict:
        from magic_video_editor.studio.frame_analysis import suggest_cycle

        analysis = await self.analyze_candidate_frames(project_id, motion_id, candidate_id, actor_id)
        return await asyncio.to_thread(suggest_cycle, analysis, mode, target_frame_count)

    async def download_candidate_sheet(
        self, project_id: str, motion_id: str, candidate_id: str, actor_id: str
    ) -> UploadTarget:
        """T20: packed PNG strip honoring order/holds/facing, cached under an immutable
        per-candidate key -- a repeat request returns exactly the cached bytes."""
        current = await self._require_owned_project(project_id, actor_id)
        candidate = self._find_candidate(current.manifest, motion_id, candidate_id)
        pivot = resolve_candidate_pivot(current.manifest.motions, candidate)
        registration = "" if pivot is None else f"pivot-{pivot[0]}-{pivot[1]}/"
        async def build() -> bytes:
            sheet = await self._read_candidate_sheet_bytes(candidate)
            if pivot is not None:
                x, y = pivot
                if candidate.facing is Facing.LEFT and candidate.metadata is not None:
                    width = candidate.metadata.width
                    if candidate.metadata.media_kind is MediaKind.SHEET:
                        width //= candidate.metadata.frame_count
                    x = width - x
                return await asyncio.to_thread(register_sheet_pivot, sheet, x, y)
            return sheet

        cache_key = (
            f"outputs/studio/{project_id}/motions/{motion_id}/{candidate_id}/{registration}sheet.png"
        )
        return await self._cached_candidate_download(
            project_id,
            cache_key,
            "image/png",
            build,
            self._candidate_download_filename(
                current.manifest, motion_id, candidate_id, "png"
            ),
        )

    async def download_candidate_preview(
        self,
        project_id: str,
        motion_id: str,
        candidate_id: str,
        fmt: str,
        actor_id: str,
    ) -> UploadTarget:
        """T20: a loop clip (webm or gif) rebuilt from the packed strip at the
        candidate's own frame timing, cached under an immutable per-candidate key."""
        if fmt not in {"webm", "gif"}:
            raise ValueError("preview format must be webm or gif")
        current = await self._require_owned_project(project_id, actor_id)
        candidate = self._find_candidate(current.manifest, motion_id, candidate_id)
        resolve_candidate_pivot(current.manifest.motions, candidate)
        frame_duration_ms = _effective_frame_duration_ms(candidate)
        frame_count = candidate_playback_frame_count(candidate)
        cache_version = "timing-v3/"
        cache_key = f"outputs/studio/{project_id}/motions/{motion_id}/{candidate_id}/{cache_version}preview.{fmt}"
        content_type = "video/webm" if fmt == "webm" else "image/gif"

        async def build() -> bytes:
            artifact = await self._assets.read_limited(candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES)
            if sha256(artifact).hexdigest() != candidate.sha256:
                raise MotionAssetError("motion_asset_hash_mismatch", "candidate asset digest does not match metadata")
            unedited = candidate.ordered_frames is None and candidate.frame_duration_ms is None and candidate.facing is Facing.RIGHT
            if fmt == "webm" and unedited and artifact.startswith(b"\x1a\x45\xdf\xa3"):
                return artifact
            rate = None
            if candidate.frame_duration_ms is None and candidate.metadata.media_kind is MediaKind.VIDEO:
                # Probe actual source bytes too: old candidates may record requested duration.
                measured = await asyncio.to_thread(inspect_motion_artifact, artifact)
                rate = measured.frame_rate
            timing = {"frame_rate": rate} if rate else {}
            sheet_bytes = await render_candidate_sheet(artifact, candidate)
            try:
                if fmt == "webm":
                    return await render_sheet_to_webm(
                        sheet_bytes,
                        frame_count=frame_count,
                        frame_duration_ms=frame_duration_ms,
                        **timing,
                    )
                return await render_sheet_to_gif(
                    sheet_bytes,
                    frame_count=frame_count,
                    frame_duration_ms=frame_duration_ms,
                    **timing,
                )
            except SheetUnpackFailed as exc:
                raise MotionDerivationError(
                    "motion_preview_failed", "preview clip could not be built"
                ) from exc

        return await self._cached_candidate_download(
            project_id,
            cache_key,
            content_type,
            build,
            self._candidate_download_filename(
                current.manifest, motion_id, candidate_id, fmt
            ),
        )

    async def create_candidate_upload(
        self,
        project_id: str,
        motion_id: str,
        content_type: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> UploadTarget:
        """T21: a signed PUT for an existing clip/sheet produced elsewhere (Ludo,
        AutoSprite, an artist) to ingest as a candidate."""
        current = await self._require_owned_project(
            project_id, actor_id=actor_id, expected_revision=expected_revision
        )
        if not any(item.motion_id == motion_id for item in current.manifest.motions):
            raise ValueError("motion is not present in the manifest")
        await self._require_storage_headroom(current.manifest.owner_id)
        extension = _candidate_upload_extension(content_type)
        key = f"sources/studio/{project_id}/motion-uploads/{uuid4().hex}{extension}"
        return await self._upload_target(
            current.manifest.owner_id, key, content_type, MAX_MOTION_OUTPUT_BYTES
        )

    async def settle_candidate_upload(
        self,
        project_id: str,
        expected_revision: int,
        motion_id: str,
        asset_key: str,
        actor_id: str,
        frame_count: int | None = None,
    ) -> StoredManifest:
        """T21: measure, assess, and append an ingested upload as a candidate with
        `source=upload` provenance. A PNG strip requires `frame_count` (the caller
        cannot otherwise say how the strip is divided); a bad count is rejected
        without losing the upload -- only the settle fails, never the asset."""
        current = await self._require_owned_project(
            project_id, actor_id=actor_id, expected_revision=expected_revision
        )
        motion = next(
            (item for item in current.manifest.motions if item.motion_id == motion_id),
            None,
        )
        if motion is None:
            raise ValueError("motion is not present in the manifest")
        prefix = f"sources/studio/{project_id}/motion-uploads/"
        if not asset_key.startswith(prefix):
            raise MotionUploadValidationError(
                "motion_upload_key_invalid",
                "upload key must be in this project's motion-uploads namespace",
            )
        try:
            content = await self._assets.read_limited(
                asset_key, MAX_MOTION_OUTPUT_BYTES
            )
        except AssetNotFound as exc:
            raise MotionAssetError(
                "motion_asset_missing", "uploaded artifact is missing"
            ) from exc
        except AssetTooLarge as exc:
            raise MotionAssetError(
                "motion_asset_too_large",
                "uploaded artifact exceeds measurement limits",
            ) from exc
        candidate_id = sha256(content).hexdigest()[:32]
        if any(item.candidate_id == candidate_id for item in motion.candidates):
            return current
        is_png = content.startswith(b"\x89PNG\r\n\x1a\n")
        if is_png and frame_count is None:
            raise MotionUploadValidationError(
                "motion_upload_frame_count_required",
                "a PNG strip upload requires frame_count",
            )
        try:
            if is_png:
                metadata = await asyncio.to_thread(
                    inspect_sprite_png, content, frame_count
                )
            else:
                metadata = await asyncio.to_thread(inspect_motion_artifact, content)
        except ValueError as exc:
            raise MotionUploadValidationError(
                "motion_upload_invalid", str(exc)
            ) from exc
        candidate = MotionCandidate(
            candidate_id=candidate_id,
            artifact_key=asset_key,
            sha256=sha256(content).hexdigest(),
            metadata=metadata,
            source=CandidateSource.UPLOAD,
            motion_spec_digest=motion_spec_digest(motion.specification),
            subject_revision_id=current.manifest.subject.current_revision_id,
        )
        candidate = candidate.model_copy(
            update={
                "assessment": assess_motion(candidate, motion.specification.loop_policy)
            }
        )
        manifest = add_motion_candidate(
            current.manifest, motion_id=motion_id, candidate=candidate
        )
        await self._record_stored_bytes(manifest.owner_id, asset_key)
        return await self._store.append(project_id, current.revision, manifest)

    async def list_operations(
        self, project_id: str, actor_id: str
    ) -> tuple[OperationListing, ...]:
        current = await self._require_owned_project(project_id, actor_id)
        if self._jobs is None:
            return ()
        records = await self._jobs.list_integration_relevant_for_project(project_id)
        await self._sync_pool_motion_records(records)
        await self._sync_hosted_motion_records(records)
        terminal = await self._jobs.list_terminal_history_for_project(project_id, 10)
        relevant = [
            record
            for record in records
            if record.status in {JobStatus.QUEUED, JobStatus.PROCESSING}
            or (
                record.status is JobStatus.COMPLETED
                and not is_settled(record, current.manifest)
            )
        ]
        return tuple(
            OperationListing(
                record=record, settled=is_settled(record, current.manifest)
            )
            for record in (*relevant, *terminal)
        )

    async def _sync_pool_motion_records(self, records: Iterable[JobRecord]) -> None:
        """Fold outbound-pool outcomes back into the operations ledger.

        A pool-executed motion job is never claimed by the Cloud Tasks worker,
        so nothing else would move it out of `queued`. This runs inside the
        listing and reconcile reads the UI already polls while work is in
        flight; the settle path itself is unchanged.
        """
        if self._motion_pool is None or self._jobs is None:
            return
        for record in records:
            if record.operation not in _POOL_OPERATIONS:
                continue
            if record.status not in {JobStatus.QUEUED, JobStatus.PROCESSING}:
                continue
            try:
                pool_record = await self._motion_pool.get(record.job_id)
            except Exception:
                # A transient pool read must never break the listing; the next
                # poll retries. Logged so a persistently broken pool read is
                # visible instead of silently stalling a job at queued/processing.
                logger.warning(
                    "pool motion sync: failed to read pool record for job %s",
                    record.job_id,
                    exc_info=True,
                )
                continue
            if pool_record is None:
                continue
            try:
                if pool_record.status is PoolJobStatus.COMPLETED:
                    await self._jobs.complete_unclaimed(
                        record.job_id,
                        VideoJobResult(
                            output_key=pool_record.output_key,
                            execution_provenance=_pool_execution_provenance(pool_record, record),
                        ),
                    )
                elif pool_record.status is PoolJobStatus.FAILED:
                    await self._jobs.fail_unclaimed(
                        record.job_id,
                        pool_record.error or "GPU generation failed.",
                        failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
                        failure_message=pool_record.failure_message
                        or "The GPU worker could not complete this motion.",
                        retryable=pool_record.retryable,
                    )
                elif pool_record.status is PoolJobStatus.CANCELLED:
                    await self._jobs.cancel(record.job_id)
            except Exception:
                # One bad ledger write must not abort the sync for the rest of
                # the batch; the next poll retries this record.
                logger.warning(
                    "pool motion sync: failed to write ledger update for job %s",
                    record.job_id,
                    exc_info=True,
                )
                continue

    async def list_activity_operations(
        self,
        project_id: str,
        actor_id: str,
        cursor: str | None = None,
        limit: int = DEFAULT_ACTIVITY_PAGE_LIMIT,
        motion_id: str | None = None,
    ) -> ActivityPage:
        """Newest-first, cursor-paginated operation history (T14). The opaque cursor is
        the last-returned record's `created_at`/`job_id`; passing it back resumes right
        after that record. Durable across restarts because it is derived from stored
        fields, not a row offset.

        Pages directly from the durable store rather than paginating an
        already-truncated in-memory listing (T11): `PostgresJobStore.list_for_project`
        hard-limits its unpaged (`limit=None`) query to the newest 100 rows, so a
        project with more history than that must keep asking the store for further
        batches -- with the cursor advanced past every row seen, studio-relevant or
        not -- until enough studio-relevant records are collected or the store is
        exhausted.

        `motion_id` narrows the log to one object (studio-ux-flow phase 3/4):
        a motion's own id scopes to that motion's operations; the literal value
        `"subject"` scopes to operations with no motion (subject generation and
        opening-frame composition).
        """
        current = await self._require_owned_project(project_id, actor_id)
        if cursor is not None:
            # Every store gets the same refusal; Postgres would otherwise fail
            # parsing it mid-query.
            decode_job_activity_cursor(cursor)
        if self._jobs is None:
            return ActivityPage(operations=(), next_cursor=None)
        studio_records: list[JobRecord] = []
        batch_size = max(limit + 1, DEFAULT_ACTIVITY_PAGE_LIMIT)
        page_cursor = cursor
        while len(studio_records) <= limit:
            batch = await self._jobs.list_for_project(
                project_id, cursor=page_cursor, limit=batch_size
            )
            if not batch:
                break
            studio_records.extend(
                record
                for record in batch
                if record.operation in _ACTIVITY_OPERATIONS
                and _matches_activity_scope(record, motion_id)
            )
            page_cursor = _activity_cursor(batch[-1])
            if len(batch) < batch_size:
                break
        page = studio_records[:limit]
        next_cursor = (
            _activity_cursor(page[-1]) if len(studio_records) > limit else None
        )
        return ActivityPage(
            operations=tuple(
                OperationListing(
                    record=record, settled=is_settled(record, current.manifest)
                )
                for record in page
            ),
            next_cursor=next_cursor,
        )

    async def _all_project_records(self, project_id: str) -> list[JobRecord]:
        """Every job of the project. An unpaged listing is capped on PostgreSQL, and
        a completed job beyond the cap would otherwise never settle."""
        assert self._jobs is not None
        records: list[JobRecord] = []
        cursor = None
        while True:
            page = await self._jobs.list_for_project(project_id, cursor=cursor, limit=100)
            records.extend(page)
            if len(page) < 100:
                return records
            cursor = job_activity_cursor(page[-1])

    async def reconcile_project_operations(
        self, project_id: str, actor_id: str
    ) -> None:
        if self._jobs is None:
            return
        current = await self._require_owned_project(project_id, actor_id)
        records = await self._all_project_records(project_id)
        await self._sync_pool_motion_records(records)
        await self._sync_hosted_motion_records(records)
        records = await self._all_project_records(project_id)
        for record in records:
            if record.status is not JobStatus.COMPLETED:
                continue
            try:
                if record.operation is JobOperation.GENERATE_SUBJECT:
                    if any(
                        revision.artifact_key == record.output_key
                        for revision in current.manifest.subject.revisions
                    ):
                        continue
                    await self.settle_subject_generation(
                        project_id, record.job_id, current.revision, actor_id
                    )
                elif record.operation is JobOperation.GENERATE_MOTION:
                    motion_id = record.payload.get("motion_id")
                    if not isinstance(motion_id, str):
                        continue
                    motion = next(
                        (
                            item
                            for item in current.manifest.motions
                            if item.motion_id == motion_id
                        ),
                        None,
                    )
                    if motion is not None and any(
                        candidate.candidate_id == record.manifest_operation_id
                        for candidate in motion.candidates
                    ):
                        continue
                    await self.settle_project_motion_candidate(
                        project_id, motion_id, record.job_id, current.revision, actor_id
                    )
                elif record.operation is JobOperation.COMPOSE_FRAME:
                    if any(
                        revision.artifact_key == record.output_key
                        for revision in current.manifest.composition.revisions
                    ):
                        continue
                    await self.settle_composition(
                        project_id, record.job_id, current.revision, actor_id
                    )
                elif record.operation is JobOperation.GENERATE_TURNAROUND:
                    if is_settled(record, current.manifest):
                        continue
                    await self.settle_turnaround(record.job_id, actor_id)
                else:
                    continue
                current = await self._require_owned_project(project_id, actor_id)
                if record.settlement_note is not None:
                    await self._jobs.note_settlement(record.job_id, None)
            except (
                RevisionConflict,
                SubjectUploadError,
                MotionOperationNotReady,
                MotionLineageConflict,
                MotionAssetError,
                CompositionOperationNotReady,
                CompositionRequestBlocked,
                ValueError,
            ) as exc:
                logger.warning(
                    "reconcile: could not settle job %s (%s): %s",
                    record.job_id,
                    record.operation.value,
                    exc,
                )
                await self._jobs.note_settlement(record.job_id, str(exc))
                continue

    async def list_project_history(
        self, project_id: str, actor_id: str
    ) -> tuple[HistoryEntry, ...]:
        """Durable revisions plus terminal (failed/cancelled) operations, oldest first."""
        current = await self._require_owned_project(project_id, actor_id)
        entries = await self._revision_history_entries(project_id)
        entries.extend(await self._terminal_history_entries(project_id, current))
        entries.sort(
            key=lambda entry: entry.timestamp or datetime.min.replace(tzinfo=UTC)
        )
        return tuple(entries)

    async def _revision_history_entries(self, project_id: str) -> list[HistoryEntry]:
        revisions = await self._store.list_revisions(project_id)
        entries: list[HistoryEntry] = []
        previous = None
        for stored in revisions:
            entries.append(
                HistoryEntry(
                    revision=stored.revision,
                    timestamp=stored.updated_at,
                    actor=stored.manifest.owner_id,
                    summary=summarize_manifest_change(previous, stored.manifest),
                )
            )
            previous = stored.manifest
        return entries

    async def _terminal_history_entries(
        self, project_id: str, current: StoredManifest
    ) -> list[HistoryEntry]:
        if self._jobs is None:
            return []
        terminal = await self._jobs.list_terminal_history_for_project(project_id, 10)
        return [
            HistoryEntry(
                revision=None,
                timestamp=job.updated_at or job.created_at,
                actor=current.manifest.owner_id,
                summary=job.failure_message
                or "Generation failed before a result was created.",
                failed=True,
            )
            for job in terminal
            if job.status is JobStatus.FAILED
        ]

    async def approve_set(
        self,
        project_id: str,
        expected_revision: int,
        actor_id: str,
        waived_finding_ids: tuple[str, ...] = (),
        waiver_reason: str | None = None,
    ) -> StoredManifest:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        if current.manifest.set_assessment is None:
            raise ValueError("set approval requires a current quality assessment")
        # Quality findings are warnings, never blockers (operator decision, 2026-09-05):
        # every finding present at approval time is recorded as acknowledged so the
        # audit trail shows what was seen, unless the caller sent its own waiver list.
        all_finding_codes = {
            finding.code for finding in current.manifest.set_assessment.findings
        }
        if current.manifest.subject.assessment is not None:
            all_finding_codes.update(
                finding.code.value
                for finding in current.manifest.subject.assessment.findings
            )
        resolved_waivers = waived_finding_ids or tuple(sorted(all_finding_codes))
        resolved_reason = waiver_reason or (
            "acknowledged" if resolved_waivers else None
        )
        manifest = approve_set(
            current.manifest,
            assessment_id=current.manifest.set_assessment_id or "",
            approver_id=actor_id,
            policy_version=current.manifest.set_assessment.policy_version,
            waived_finding_ids=resolved_waivers,
            waiver_reason=resolved_reason,
        )
        return await self._store.append(project_id, current.revision, manifest)

    async def create_release(
        self,
        project_id: str,
        expected_revision: int,
        name: str,
        actor_id: str,
    ) -> Release:
        current = await self._require_owned_project(
            project_id, actor_id, expected_revision
        )
        approval = current.manifest.set_approval
        if (
            approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}
            or approval.manifest_digest != set_digest(current.manifest)
            or current.manifest.subject.current_revision is None
        ):
            raise ReleaseNotReady("release requires fresh explicit set approval")
        current_subject_revision_id = (
            current.manifest.subject.current_revision.revision_id
        )
        stale_motion_ids = tuple(
            motion.motion_id
            for motion in current.manifest.motions
            if motion.required
            and motion.selected_candidate is not None
            and motion.selected_candidate.subject_revision_id is not None
            and motion.selected_candidate.subject_revision_id
            != current_subject_revision_id
        )
        if stale_motion_ids:
            # Quality findings are warnings, never blockers (operator decision,
            # 2026-09-05): record the staleness on the set assessment and let the
            # release proceed. `set_approval` and its digest are left untouched --
            # `_set_digest` excludes assessment evidence, so re-assessing here
            # cannot invalidate the approval just checked above.
            fresh_assessment = assess_set(
                current.manifest.motions,
                current_subject_revision_id=current_subject_revision_id,
            )
            manifest_with_warning = current.manifest.model_copy(
                update={"set_assessment": fresh_assessment}
            )
            current = await self._store.append(
                project_id, current.revision, manifest_with_warning
            )
        release = await self._store.create_release(
            project_id=project_id,
            expected_revision=current.revision,
            name=name,
            manifest=current.manifest,
        )
        # Part B: best-effort, never blocks or fails the release itself.
        await self._deliver_release(project_id, release)
        return release

    async def get_release_delivery(
        self, release_id: str, actor_id: str
    ) -> ReleaseDelivery | None:
        release = await self._store.get_release(release_id)
        if release is None:
            raise ReleaseNotFound("release not found")
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ReleaseNotFound("release not found") from exc
        stored = await self._store.get(release.project_id)
        if stored is None:
            return None
        return latest_release_delivery(stored.manifest, release_id=release_id)

    async def list_release_deliveries(
        self, release_id: str, actor_id: str
    ) -> tuple[ReleaseDelivery, ...]:
        """Every delivery attempt for a release, oldest first -- what
        `ReleaseResponse.deliveries` exposes."""
        release = await self._store.get_release(release_id)
        if release is None:
            raise ReleaseNotFound("release not found")
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ReleaseNotFound("release not found") from exc
        stored = await self._store.get(release.project_id)
        if stored is None:
            return ()
        return deliveries_for(stored.manifest, release_id=release_id)

    async def retry_delivery(
        self, release_id: str, target: str, actor_id: str
    ) -> ReleaseDelivery:
        release = await self._store.get_release(release_id)
        if release is None:
            raise ReleaseNotFound("release not found")
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ReleaseNotFound("release not found") from exc
        if not self._target_registry.delivery_configured(target):
            raise DeliveryNotConfigured(f"delivery to {target!r} is not configured")
        await self._deliver_release(release.project_id, release, target=target)
        stored = await self._store.get(release.project_id)
        delivery = (
            latest_release_delivery(stored.manifest, release_id=release_id)
            if stored is not None
            else None
        )
        if delivery is None:
            raise DeliveryNotConfigured(f"delivery to {target!r} is not configured")
        return delivery

    async def _deliver_release(
        self, project_id: str, release: Release, *, target: str | None = None
    ) -> None:
        """Export the bound external subject's target profile and deliver it
        through the matching adapter, recording a `ReleaseDelivery` either way.
        Never raises: a delivery failure must not fail (or even flag)
        `create_release` (Part B).
        """
        subject = release.manifest.external_subject
        system = target or (subject.system if subject is not None else None)
        if system is None:
            return
        if target is None and (subject is None or subject.system != system):
            return
        adapter = self._target_registry.get(system)
        config = self._target_registry.config_for(system)
        if adapter is None or config is None:
            return
        try:
            export_record = await self._exporter.export(
                release,
                profile=ExportProfile(
                    name=adapter.descriptor.system,
                    version=adapter.descriptor.package_version,
                ),
            )
            package = await self._assets.read_limited(
                export_record.artifact_key, MAX_EXPORT_TOTAL_BYTES
            )
            result = await adapter.deliver(
                DeliveryRequest(
                    release_id=release.release_id,
                    system=system,
                    archive=package,
                    config=config,
                )
            )
        except (
            ImmutableExportCollision,
            AssetNotFound,
            AssetTooLarge,
            AssetStorageUnavailable,
        ) as exc:
            await self._record_delivery(
                project_id,
                ReleaseDelivery(
                    release_id=release.release_id,
                    target=system,
                    status=DeliveryStatus.FAILED,
                    error=str(exc)[:2000],
                    retryable=True,
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001 -- delivery is always best-effort
            await self._record_delivery(
                project_id,
                ReleaseDelivery(
                    release_id=release.release_id,
                    target=system,
                    status=DeliveryStatus.FAILED,
                    error=str(exc)[:2000],
                    retryable=True,
                ),
            )
            return
        if not result.is_delivered:
            await self._record_delivery(
                project_id,
                ReleaseDelivery(
                    release_id=release.release_id,
                    target=system,
                    status=DeliveryStatus.FAILED,
                    error=(result.message or "delivery failed")[:2000],
                    retryable=result.retryable,
                ),
            )
            return
        receipt = result.receipt
        await self._record_delivery(
            project_id,
            ReleaseDelivery(
                release_id=release.release_id,
                target=system,
                status=DeliveryStatus.DELIVERED,
                delivered_at=datetime.now(UTC),
                summary=receipt.summary if receipt is not None else "",
                details=dict(receipt.details) if receipt is not None else {},
            ),
        )

    async def _record_delivery(
        self, project_id: str, delivery: ReleaseDelivery
    ) -> None:
        stored = await self._store.get(project_id)
        if stored is None:
            return
        manifest = record_release_delivery(stored.manifest, delivery=delivery)
        try:
            await self._store.append(project_id, stored.revision, manifest)
        except RevisionConflict:
            stored = await self._store.get(project_id)
            if stored is None:
                return
            manifest = record_release_delivery(stored.manifest, delivery=delivery)
            try:
                await self._store.append(project_id, stored.revision, manifest)
            except RevisionConflict:
                return

    async def list_releases(
        self, project_id: str, actor_id: str
    ) -> tuple[Release, ...]:
        await self._require_owned_project(project_id, actor_id)
        return await self._store.list_releases(project_id)

    def _resolve_export_profile(self, profile_name: str) -> ExportProfile | None:
        if profile_name == PNG_JSON_V2.name:
            return PNG_JSON_V2
        adapter = self._target_registry.get(profile_name)
        if adapter is None:
            return None
        return ExportProfile(
            name=adapter.descriptor.system, version=adapter.descriptor.package_version
        )

    async def create_export(
        self,
        release_id: str,
        profile_name: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> ExportRecord:
        release = await self._store.get_release(release_id)
        if release is None:
            raise ReleaseNotFound("release not found")
        if (
            expected_revision is not None
            and release.source_revision != expected_revision
        ):
            raise RevisionConflict(release.source_revision)
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ReleaseNotFound("release not found") from exc
        profile = self._resolve_export_profile(profile_name)
        if profile is None:
            raise ValueError("unsupported export profile")
        record = await self._exporter.export(release, profile=profile)
        try:
            return await self._store.save_export(record)
        except ManifestMismatch as exc:
            raise ImmutableExportCollision(
                "immutable export metadata conflicts with stored record"
            ) from exc

    async def get_export(self, export_id: str, actor_id: str) -> ExportRecord:
        record = await self._store.get_export(export_id)
        if record is None:
            raise ExportNotFound("export not found")
        release = await self._store.get_release(record.release_id)
        if release is None:
            raise ExportNotFound("export not found")
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ExportNotFound("export not found") from exc
        await self._exporter.verify_published(record)
        return record

    async def list_release_exports(
        self, release_id: str, actor_id: str
    ) -> tuple[ExportRecord, ...]:
        """Every export recorded for a release, newest first -- owner-scoped
        like the other release routes."""
        release = await self._store.get_release(release_id)
        if release is None:
            raise ReleaseNotFound("release not found")
        try:
            await self._require_owned_project(release.project_id, actor_id)
        except ProjectNotFound as exc:
            raise ReleaseNotFound("release not found") from exc
        return await self._store.list_release_exports(release_id)

    async def create_export_download(
        self, export_id: str, actor_id: str
    ) -> UploadTarget:
        key, _ = await self.export_file(export_id, actor_id)
        if await self._assets.describe(key) is None:
            raise ExportNotFound("export not found")
        # An API path the export route authorizes on every request.
        return UploadTarget(key=key, upload_url=f"/studio/exports/{quote(export_id, safe='')}/file")

    async def export_file(self, export_id: str, actor_id: str) -> tuple[str, str]:
        """The stored key and download name of an export the caller owns."""
        record = await self.get_export(export_id, actor_id)
        release = await self._store.get_release(record.release_id)
        if release is None:
            raise ExportNotFound("export not found")
        key = record.artifact_key
        extension = key.rsplit(".", 1)[-1] if "." in key.rsplit("/", 1)[-1] else "zip"
        filename = (
            f"{_slug(release.manifest.name)}-{_slug(release.name)}"
            f"-{_slug(record.profile_name)}.{extension}"
        )
        return key, filename

    async def owner_usage(self, actor_id: str) -> OwnerUsage:
        """What this operator is using, and against what ceiling (`/auth/session`)."""
        return OwnerUsage(
            owner_id=actor_id,
            stored_bytes=await self._stored_bytes(actor_id),
            storage_limit_bytes=await self._storage_limit_for(actor_id),
            running_jobs=await self._running_job_count(actor_id),
            concurrent_job_limit=self._owner_limits.concurrent_job_limit,
            account_tier=(await self.account_policy.tier(actor_id)).value,
        )

    async def _storage_limit_for(self, owner_id: str) -> int:
        if await self.account_policy.is_public(owner_id):
            return self._owner_limits.public_storage_limit_bytes
        return self._owner_limits.storage_limit_bytes

    async def _stored_bytes(self, owner_id: str) -> int:
        extra = (
            await self.additional_stored_bytes(owner_id)
            if self.additional_stored_bytes is not None else 0
        )
        return await self._owner_usage.stored_bytes(owner_id) + extra

    async def _upload_target(
        self, owner_id: str, key: str, content_type: str, max_bytes: int
    ) -> UploadTarget:
        """A signed PUT. A public account's grant carries a signed ceiling of the
        most this upload is ever read with, which storage enforces; invited
        grants keep their previous shape so existing clients keep working."""
        if not await self.account_policy.is_public(owner_id):
            return UploadTarget(
                key=key, upload_url=await self._assets.create_upload_url(key, content_type)
            )
        return UploadTarget(
            key=key,
            upload_url=await self._assets.create_upload_url(
                key, content_type, max_bytes=max_bytes
            ),
            upload_headers=upload_size_headers(max_bytes),
        )

    async def _require_storage_headroom(self, owner_id: str) -> None:
        limit = await self._storage_limit_for(owner_id)
        if limit == UNLIMITED:
            return
        stored = await self._stored_bytes(owner_id)
        if stored >= limit:
            raise OwnerStorageLimitExceeded(stored, limit)

    async def _require_job_headroom(self, owner_id: str) -> None:
        limit = self._owner_limits.concurrent_job_limit
        if limit == UNLIMITED:
            return
        running = await self._running_job_count(owner_id)
        if running >= limit:
            raise OwnerConcurrentJobLimitExceeded(running, limit)

    def _dispatch_headroom(self, running_jobs: int) -> int | None:
        """How many more jobs may start now; `None` when the cap is unlimited."""
        limit = self._owner_limits.concurrent_job_limit
        if limit == UNLIMITED:
            return None
        return max(0, limit - running_jobs)

    async def _require_headroom_to_start_work(self, owner_id: str) -> None:
        await self._require_storage_headroom(owner_id)
        await self._require_job_headroom(owner_id)

    async def _running_job_count(self, owner_id: str) -> int:
        total = (
            await self.additional_running_jobs(owner_id)
            if self.additional_running_jobs is not None else 0
        )
        if self._jobs is None:
            return total
        for record in await self._store.list_projects(owner_id):
            total += sum(
                1
                for job in await self._jobs.list_for_project(record.project_id)
                if job.status in _IN_FLIGHT_JOB_STATUSES
            )
        return total

    async def delete_project(self, project_id: str, actor_id: str, confirm_name: str) -> None:
        """Permanently delete one of the caller's projects. The caller repeats the
        project's name, so a stray request cannot destroy work."""
        project = await self._require_owned_project(project_id, actor_id)
        if confirm_name != project.manifest.name:
            raise ProjectDeletionNotConfirmed("type the project's exact name to delete it")
        await self.purge_project(project)

    async def purge_project(self, project: StoredManifest) -> None:
        """Delete everything a project owns: running work is cancelled, every
        stored file (every version) is removed, then its jobs, releases, exports
        and revisions. Files go first, so a failure part-way leaves a project
        that can simply be deleted again. Already-delivered target packages
        live outside our storage and are not recalled."""
        counted = await self._counted_bytes(project.manifest)
        await self._purge_project_id(project.project_id)
        if counted:
            await self._owner_usage.add_bytes(project.manifest.owner_id, -counted)
        await self._release_keyed_artifacts(project.manifest.owner_id, project.project_id)

    async def expire_project(self, project_id: str) -> bool:
        """Delete a project for inactivity: False keeps it (it has a release).
        Starting work counts as opening, so a project idle this long has no work
        of its own in flight; a job still marked live is stale, and the purge
        cancels it. A project whose manifest no longer validates is purged by
        ID: nobody has opened it either, so it gets no longer life than others."""
        try:
            stored = await self._store.get(project_id)
        except ManifestUnreadable:
            stored = None
        if stored is None and await self._store.current_revision_number(project_id) is None:
            return True
        if await self._store.list_releases(project_id):
            return False
        logger.info("retention.expired", extra={"kind": PROJECT, "item_id": project_id})
        if stored is None:
            # Unreadable: count its bytes from the newest revision that still
            # validates, so the owner's storage allowance is given back too.
            readable = await self._last_readable_manifest(project_id)
            counted = await self._counted_bytes(readable) if readable is not None else 0
            await self._purge_project_id(project_id)
            if readable is not None and counted:
                await self._owner_usage.add_bytes(readable.owner_id, -counted)
            if readable is not None:
                await self._release_keyed_artifacts(readable.owner_id, project_id)
        else:
            await self.purge_project(stored)
        return True

    async def _release_keyed_artifacts(self, owner_id: str, project_id: str) -> None:
        """Give back every count recorded under the deleted project, never only the
        ones a snapshot listed: a turnaround that settled while the project was
        being deleted is released too. Runs after the purge, so a settlement that
        counts later finds the project gone and releases its own count."""
        from .view_drafts import draft_key_prefix

        await self._owner_usage.release_artifacts(owner_id, turnaround_key_prefix(project_id))
        await self._owner_usage.release_artifacts(owner_id, draft_key_prefix(project_id))

    async def _counted_bytes(self, manifest: AnimationProjectManifest) -> int:
        counted = 0
        for key in _accounted_keys(manifest):
            with suppress(AssetStorageUnavailable):
                descriptor = await self._assets.describe(key)
                counted += descriptor.size if descriptor is not None else 0
        return counted

    async def _last_readable_manifest(self, project_id: str) -> AnimationProjectManifest | None:
        for raw in await self._store.list_raw_revisions(project_id):
            with suppress(ValidationError):
                return AnimationProjectManifest.model_validate(raw.manifest)
        return None

    async def _purge_project_id(self, project_id: str) -> None:
        if self._jobs is not None:
            await self._cancel_live_jobs(project_id)
        for prefix in (
            f"sources/studio/{project_id}/",
            f"outputs/studio/{project_id}/",
            f"exports/studio/{project_id}/",
        ):
            await self._assets.delete_prefix(prefix)
        if self._jobs is not None:
            await self._jobs.delete_for_project(project_id)
        await self._store.delete_project(project_id)
        if self.retention is not None:
            # After the project is gone: a failure here must not report the
            # delete as failed. A leftover row names an item that no longer exists.
            try:
                await self.retention.forget(PROJECT, project_id)
            except Exception:
                logger.warning("retention.forget_failed", exc_info=True)

    async def erase_owner(self, owner_id: str) -> None:
        """Delete everything the Studio holds for an owner (account deletion):
        machines first, so nothing new is leased, then every project, saved
        provider keys and the usage counter."""
        if self._enrolment is not None:
            await self._enrolment.forget_owner(owner_id)
        # `list_projects` matches the current manifest's owner, and still lists
        # a project whose manifest no longer validates; the usage row goes
        # below, so no per-project accounting is needed.
        for record in await self._store.list_projects(owner_id):
            await self._purge_project_id(record.project_id)
        if self.provider_accounts is not None:
            await self.provider_accounts.store.forget_owner(owner_id)
        await self._owner_usage.forget_owner(owner_id)

    async def _record_stored_bytes(self, owner_id: str, asset_key: str) -> None:
        """Accounting is never load-bearing: a storage backend that cannot describe
        an artifact must not fail the settle that produced it."""
        try:
            descriptor = await self._assets.describe(asset_key)
        except AssetStorageUnavailable:
            return
        if descriptor is not None:
            await self._owner_usage.add_bytes(owner_id, descriptor.size)

    async def _require_owned_project(
        self,
        project_id: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> StoredManifest:
        current = await self._store.get(project_id)
        if current is None or current.manifest.owner_id != actor_id:
            raise ProjectNotFound(project_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise RevisionConflict(current.revision)
        # Every owner operation passes here, so this is what "opened" means
        # for inactivity expiry.
        await self._touch(project_id, actor_id)
        return current


def _slug(value: str) -> str:
    """Filename-safe slug; empty input (or all-punctuation) falls back to "project"."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "project"


def _candidate_take_number(motion: MotionState, candidate_id: str) -> int:
    """Count alpha siblings with their original, matching the gallery's take groups."""
    take = 0
    previous = None
    for candidate in motion.candidates:
        keyed_sibling = (
            previous is not None
            and candidate.recipe is not None and candidate.recipe.op == "derive_alpha"
            and candidate.metadata is not None and candidate.metadata.has_alpha
            and previous.metadata is not None and not previous.metadata.has_alpha
            and candidate.duration_ms is not None and candidate.duration_ms == previous.duration_ms
        )
        if not keyed_sibling:
            take += 1
        if candidate.candidate_id == candidate_id:
            return take
        previous = candidate
    return 0


def _subject_take_number(manifest: AnimationProjectManifest, artifact_key: str) -> int:
    for index, revision in enumerate(manifest.subject.revisions, start=1):
        if revision.artifact_key == artifact_key:
            return index
    return 0


def _subject_upload_key(project_id: str) -> str:
    return f"sources/studio/{project_id}/subjects/{uuid4().hex}.png"


_DEFAULT_FRAME_DURATION_MS = 100


def _effective_frame_duration_ms(candidate: MotionCandidate) -> int:
    """A candidate that has never had a frame-timing edit saved carries no explicit
    `frame_duration_ms` (T20); fall back to its measured pace, matching the web frame
    editor's own default (`frameDraft.ts`'s `measuredFrameDuration`)."""
    if candidate.frame_duration_ms is not None:
        return candidate.frame_duration_ms
    if candidate.duration_ms and candidate.metadata and candidate.metadata.frame_count:
        return max(1, round(candidate.duration_ms / candidate.metadata.frame_count))
    return _DEFAULT_FRAME_DURATION_MS


def _motion_duration_ms(job: JobRecord) -> int:
    duration_seconds = job.payload.get("duration_seconds")
    if not isinstance(duration_seconds, int) or duration_seconds <= 0:
        raise MotionLineageConflict("motion operation duration is invalid")
    return duration_seconds * 1000


def _is_subject_source_key(project_id: str, asset_key: str) -> bool:
    return asset_key.startswith(f"sources/studio/{project_id}/subjects/")


def _accounted_keys(manifest: AnimationProjectManifest) -> set[str]:
    """The artifacts storage accounting counted when they were settled."""
    keys = {revision.artifact_key for revision in manifest.subject.revisions}
    keys.update(revision.artifact_key for revision in manifest.composition.revisions)
    keys.update(
        candidate.artifact_key for motion in manifest.motions for candidate in motion.candidates
    )
    return keys


def turnaround_key_prefix(project_id: str) -> str:
    """Where a project's turnaround videos live; their usage is counted per key."""
    return f"outputs/studio/{project_id}/turnarounds/"


def project_file_path(project_id: str, key: str, download_filename: str | None = None) -> str:
    """The API path that streams `key` to the project's owner."""
    path = f"/studio/projects/{quote(project_id, safe='')}/files?key={quote(key, safe='/')}"
    if download_filename is not None:
        path += f"&download={quote(download_filename, safe='')}"
    return path


def _is_valid_subject_reference_key(
    key: str, project_id: str, revisions: Iterable[SubjectRevision]
) -> bool:
    """A reference key is valid when it's an uploaded reference for this
    project, or it's the artifact_key of one of the project's own subject
    revisions (so "Correct with feedback" can use the chosen take as its own
    reference)."""
    prefix = f"sources/studio/{project_id}/subject-references/"
    if key.startswith(prefix):
        return True
    return any(revision.artifact_key == key for revision in revisions)


def _require_png_content_type(content_type: str) -> None:
    if content_type != "image/png":
        raise SubjectUploadError(
            "subject_content_type_invalid",
            "subject uploads must use image/png",
        )


def _candidate_upload_extension(content_type: str) -> str:
    extensions = {"video/mp4": ".mp4", "video/webm": ".webm", "image/png": ".png"}
    try:
        return extensions[content_type]
    except KeyError as exc:
        raise MotionUploadValidationError(
            "motion_upload_content_type_invalid",
            "Candidate uploads must be MP4, WebM, or PNG",
        ) from exc


def _reference_extension(content_type: str) -> str:
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    try:
        return extensions[content_type]
    except KeyError as exc:
        raise SubjectUploadError(
            "subject_reference_content_type_invalid",
            "Reference images must be PNG, JPEG, or WebP",
        ) from exc


def _subject_approval_from_assessment(assessment: SubjectAssessment) -> Approval:
    """Quality findings are warnings, never blockers (operator decision, 2026-09-05):
    a settled subject revision is always ready for signoff, whatever findings its
    assessment carries."""
    return Approval(
        state=ApprovalState.READY_FOR_SIGNOFF,
        assessment_id=assessment.assessment_id,
        policy_version=assessment.policy_version,
        artifact_sha256=assessment.artifact_sha256,
    )


def _pool_execution_provenance(pool_record: object, record: JobRecord) -> dict[str, Any]:
    """Keep execution reports separate from scheduling requirements and defaults."""
    metadata = getattr(pool_record, "execution_metadata", {})
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    reported_versions = metadata.get("runtime_versions", {})
    reported_versions = dict(reported_versions) if isinstance(reported_versions, Mapping) else {}
    requirements = getattr(pool_record, "requirements", None)
    requested_versions = (requirements.get("runtime_versions", {}) if isinstance(requirements, Mapping)
                          else getattr(requirements, "runtime_versions", {}))
    worker_id = getattr(pool_record, "executed_by", None)
    hosted = isinstance(pool_record, PoolJobRecord)
    return {
        "executing_worker_id": worker_id,
        "worker_attestation": metadata,
        "completion_path": getattr(pool_record, "completion_path", "hosted_pool" if hosted else "unknown_pool"),
        "lease_fenced": hosted,
        "resolved_recipe": record.payload.get("resolved_recipe", {}),
        "capability_id": getattr(pool_record, "capability_id", None),
        "runtime_versions": reported_versions,
        "requested_runtime_versions": dict(requested_versions) if isinstance(requested_versions, Mapping) else {},
        "requested_settings": record.payload.get("requested_settings", {}),
        "resolved_settings": {key: record.payload.get(key) for key in (
            "width", "height", "duration_seconds", "steps", "seed", "provider_parameters"
        )},
        "steps": record.payload.get("steps"),
        "output_sha256": getattr(pool_record, "output_sha256", None),
        "attestation": "worker-reported execution metadata" if worker_id and metadata else "unavailable",
    }


def _higgsfield_execution_provenance(
    record: JobRecord, request: ProviderRequest, output_sha256: str
) -> dict[str, Any]:
    """What was asked of Higgsfield and what came back; no worker attested it."""
    recipe = record.payload.get("resolved_recipe")
    return {
        "executing_worker_id": None,
        "worker_attestation": {},
        "completion_path": "higgsfield_api",
        "lease_fenced": False,
        "provider": HIGGSFIELD,
        "provider_request_id": request.request_id,
        "provider_model": request.model,
        # From the job's immutable recipe: the route actually called, even if the catalog changed since.
        "provider_path": recipe.get("path") if isinstance(recipe, dict) else None,
        "resolved_recipe": record.payload.get("resolved_recipe", {}),
        "requested_settings": record.payload.get("requested_settings", {}),
        "resolved_settings": {key: record.payload.get(key) for key in (
            "width", "height", "duration_seconds", "steps", "seed", "provider_parameters"
        )},
        # Higgsfield sizes the video from the opening frame and runs its own
        # sampler; these settings shaped the frame or were not sent at all.
        "not_sent": ["steps", "seed", "width", "height"],
        "output_sha256": output_sha256,
        "attestation": "provider-reported result; Magic Studios hashed the downloaded output",
    }


def _candidate_analysis_registration(analysis: dict, candidate: MotionCandidate) -> dict:
    # Pixel analysis is immutable/cacheable; registration belongs to the current
    # authorized candidate metadata and must never be trusted from that cache.
    if analysis.get("candidate_id") != candidate.candidate_id or analysis.get("source_sha256") != candidate.sha256:
        raise MotionDerivationError("frame_analysis_failed", "cached analysis does not match the current candidate identity and hash")
    result = {key: value for key, value in analysis.items() if key != "registered_pivot"}
    metadata = candidate.metadata
    if metadata is not None and metadata.pivot_x is not None and metadata.pivot_y is not None:
        width = metadata.width // metadata.frame_count if metadata.media_kind is MediaKind.SHEET else metadata.width
        if not 0 <= metadata.pivot_x <= width or not 0 <= metadata.pivot_y <= metadata.height:
            raise MotionDerivationError("frame_analysis_failed", "registered reference pivot is outside its frame")
        result["registered_pivot"] = {"x": metadata.pivot_x, "y": metadata.pivot_y}
    return result


def _invalidate_view_set(manifest: AnimationProjectManifest) -> AnimationProjectManifest:
    return manifest.model_copy(update={"set_approval": Approval(), "set_assessment_id": None,
                                       "set_assessment_digest": None, "set_assessment": None})


def _resolve_motion_view(manifest: AnimationProjectManifest, specification: MotionSpecification) -> MotionSpecification:
    if specification.motion_id in manifest.removed_motion_ids:
        raise ValueError("removed motion identity cannot be reused")
    if specification.view_id is None:
        if specification.view_reference is not None:
            raise ValueError("view reference requires a declared view")
        return specification
    view = next((view for view in manifest.views if view.view_id == specification.view_id), None)
    if view is None or not view_matches_layout(view.heading if view.setup_version else specification.view_id, manifest.sprite_layout) and manifest.sprite_layout is not SpriteLayout.SINGLE_VIEW:
        raise ValueError("motion view is not declared or does not match sprite layout")
    if any(ref.role is MotionReferenceRole.START for ref in specification.references):
        raise ValueError("view motions cannot override the view start reference")
    updates = {"view_reference": view.reference}
    if view.setup_version:
        updates.update(view_heading=view.heading, setup_version=1)
    return specification.model_copy(update=updates)


def _render_view_opening_frame(content: bytes, settings: MotionGenerationSettings, *, fill: float = 1.0) -> bytes:
    # One deterministic transform of the entire authored canvas; never crop to moving equipment.
    width = _clamp(settings.width, _DIMENSION_BOUNDS, _DEFAULT_WIDTH)
    height = _clamp(settings.height, _DIMENSION_BOUNDS, _DEFAULT_HEIGHT)
    image = Image.open(io.BytesIO(content)).convert("RGBA")
    image = ImageOps.contain(image, (max(1, round(width * fill)), max(1, round(height * fill))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), _OPENING_FRAME_BACKDROP_RGB.get(settings.backdrop, (127, 127, 127)))
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2), image)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _subject_display_name(manifest: AnimationProjectManifest) -> str:
    """The name preset prompt templates fill `{subject_name}` with (T5, T22).

    An external subject (a target's character/journey traveller) has its own display
    name in the other game's roster -- once bound, prompts speak of that name
    rather than the studio project's own, which is workspace-internal and often
    generic ("Untitled project 3"). Falls back to the project name otherwise.
    """
    if manifest.external_subject is not None:
        return manifest.external_subject.display_name
    return manifest.name


def _compile_preset_specification(
    specification: MotionSpecification, subject_name: str
) -> MotionSpecification:
    """Fill a preset motion's authored prompt/loop policy/slot from `presets.py`.

    Custom motions pass through unchanged -- their prompt is the operator's own text.
    Subject name comes from the project's external subject display name when bound,
    else the project name (see `_subject_display_name`).
    """
    if specification.kind is not MotionKind.PRESET:
        return specification
    assert specification.preset is not None
    definition = PRESET_DEFINITIONS[specification.preset]
    action = (specification.action or "").strip() or definition.default_action
    update: dict[str, object] = {
        "loop_policy": definition.loop_policy,
        "semantic_slot": specification.semantic_slot or definition.semantic_slot,
        "action": action,
    }
    # T16: a caller marking this motion's prompt as user-edited keeps its own
    # prompt text instead of the compiled template -- `prompt_source` is the
    # only signal that survives future recompiles (e.g. an external subject
    # binding change) without clobbering that edit.
    if specification.prompt_source is not PromptSource.CUSTOM:
        name = (subject_name or "").strip() or "The subject"
        update["prompt"] = definition.prompt_template.format(
            subject_name=name, action=action
        )
    return specification.model_copy(update=update)


_STYLE_LABELS: dict[str, str] = {
    "high_fantasy": "High Fantasy",
    "cyberpunk": "Cyberpunk",
    "steampunk": "Steampunk",
    "space_corporate": "Space Corporate",
    "grimdark": "Grimdark",
    "post_apocalyptic": "Post-Apocalyptic",
    "noir": "Noir",
    "psychedelic": "Psychedelic",
    "ancient_mythology": "Ancient Mythology",
    "western": "Western",
}

# Ported from the reference game's video_lab.py:4552 backdrop clause, trimmed to the studio's own
# render contract (no Langfuse-managed motion-type contract here).
_CHROMA_BACKDROP_CLAUSE = (
    "Render against a flat chroma green (#00B140) backdrop with even lighting, "
    "no shadows on the backdrop, and no green spill on the subject."
)


_VIEW_SCREEN_HEADINGS: dict[ViewId, str] = {
    ViewId.N: "away from the viewer toward the top of the screen",
    ViewId.NE: "away from the viewer and toward screen-right",
    ViewId.E: "toward screen-right",
    ViewId.SE: "toward the viewer and screen-right",
    ViewId.S: "toward the viewer toward the bottom of the screen",
    ViewId.SW: "toward the viewer and screen-left",
    ViewId.W: "toward screen-left",
    ViewId.NW: "away from the viewer and toward screen-left",
    ViewId.LEFT: "toward screen-left",
    ViewId.RIGHT: "toward screen-right",
}


def _motion_prompt(specification: MotionSpecification, backdrop: Backdrop) -> str:
    """The generation prompt: the authored/custom direction, plus the style clause, any
    screenplay script (T17), and the backdrop clause (T18), ported loosely from the
    reference Video Lab's prompt assembly."""
    assert specification.prompt is not None
    prompt = specification.prompt.strip()
    if specification.view_id is not None:
        prompt = (
            f"{prompt} Maintain the supplied {specification.view_id} reference's "
            "actual body plane and facing throughout. "
            "Preserve its actual camera projection, facing angle and foreshortening, "
            "character identity and all equipment. "
            + (f"Declared screen-space heading {specification.view_heading}: {_VIEW_SCREEN_HEADINGS[specification.view_heading]}. "
             if specification.setup_version and specification.view_heading is not None else
             "Use reference-relative action guidance without assigning a screen-space heading. "
             if specification.setup_version else
             f"Declared screen-space heading {specification.view_id}: {_VIEW_SCREEN_HEADINGS[specification.view_id]}. ")
            +
            "Fixed camera, stationary root, constant body scale and full character/equipment containment. "
            "Natural sway and torso counterrotation retain the same view."
        )
        if specification.preset is MotionPreset.WALK:
            prompt += (
                " Walk forward in place along this projected heading with the root stationary. "
                "Alternate load-bearing left and right stance, with the planted stance foot moving "
                "backward relative to the hips, opposite the implied projected forward travel, "
                "and forward recovery of the other foot along the same projected heading. "
                "Complete a full reciprocal stride with natural weight transfer and a seamless timed wrap."
            )
        elif specification.preset is MotionPreset.ATTACK:
            prompt += (
                (" Direct the strike forward along the supplied reference's actual projected facing. "
                 if specification.setup_version and specification.view_heading is None else
                 " Direct the strike forward along the supplied reference's projected facing and declared screen-space heading. ")
                +
                "Maintain the feet and pelvis's overall facing, allowing natural weight transfer and shoulder/arm counterrotation. "
                "Keep held equipment coherent and connected to its grip throughout; show clearly readable preparation, "
                "committed strike, follow-through and recovery to the original ready stance."
            )
    if specification.style is not None:
        label = _STYLE_LABELS[specification.style.value]
        prompt = f"{prompt} Visual style: {label}."
    if specification.script:
        prompt = f"{prompt} Screenplay: {specification.script.strip()}"
    if backdrop is Backdrop.CHROMA_GREEN:
        prompt = f"{prompt}\n\n{_CHROMA_BACKDROP_CLAUSE}"
    return prompt


def motion_spec_digest(specification: MotionSpecification) -> str:
    return sha256(specification.model_dump_json(exclude_none=True).encode()).hexdigest()


def _alpha_derivation_identity(source: MotionCandidate) -> str:
    """Identity for a source candidate's alpha-derived twin (T6). Two
    non-destructive revisions can share the same source artifact sha256 while
    differing in frame order, holds, timing, or facing; hashing only the
    source id would make the second edit's derivation return the first
    edit's existing candidate. Include the source candidate id and its full
    edit state so each distinct edit derives its own alpha candidate.
    """
    ordered = tuple((slot.index, slot.hold) for slot in source.effective_ordered_frames)
    identity = (
        f"{source.sha256}:{source.candidate_id}:derive_alpha:{ordered}:"
        f"{source.frame_duration_ms}:{source.facing.value}"
    )
    return sha256(identity.encode()).hexdigest()[:32]
