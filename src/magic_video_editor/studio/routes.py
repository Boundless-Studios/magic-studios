"""Studio API routes.

Deployment must strip any user-controlled copy of `X-Studio-Actor-Id` and inject a
verified actor value from the trusted auth or BFF layer before these private routes
are reached.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field, model_validator

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.contracts import (
    AssetStorageUnavailable,
    GpuTelemetry,
    IdempotencyConflict,
    InvalidActivityCursor,
    JobFailureCode,
    JobOperation,
    JobRecord,
    JobStatus,
    OperationStage,
    PrimaryAction,
    StudioServicePort,
)
from magic_video_editor.studio.setup import (
    ProjectSetup, ProjectSetupPlan, SetupBlocked, SetupConfirmationRequired, SetupConflict,
)

from magic_video_editor.studio.provider_accounts import (
    MAX_PROVIDER_KEY_LENGTH,
    PROVIDERS,
    ProviderAccounts,
    ProviderCredentialRejected,
)
from magic_video_editor.studio.api_keys import (
    MAX_API_KEY_NAME_LENGTH,
    ApiKeyLimitReached,
    ApiKeyRejected,
    MemoryStudioApiKeyStore,
    StudioApiKey,
    StudioApiKeyStore,
    api_key_store_from_app,
    generate_api_key,
    hash_api_key,
    parse_bearer_token,
    resolve_api_key_actor,
)
from magic_video_editor.studio.auth import (
    OAUTH_STATE_BYTES,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    OAuthHandoff,
    ResolvedIdentity,
    SignInMethod,
    auth_service_from_app,
    clear_oauth_cookie,
    clear_session_cookie,
    decode_oauth_handoff,
    encode_oauth_handoff,
    generate_token,
    pkce_pair,
    safe_return_path,
    normalize_email,
    set_oauth_cookie,
    set_session_cookie,
    user_for_actor,
)
from magic_video_editor.studio.auth_service import (
    EmailNotAllowed,
    EmailNotVerified,
    SignInRejected,
    StudioAuthService,
)
from magic_video_editor.studio.enrolment import (
    CredentialRotationRefused,
    EnrolledWorker,
    PairingTokenUnusable,
    StudioEnrolmentStore,
    WorkerLimitReached,
    generate_pairing_token,
    generate_worker_credential,
    hash_token,
    looks_like_pairing_token,
    looks_like_worker_credential,
    normalize_label,
    pairing_token_expiry,
)
from magic_video_editor.studio.worker_package import create_worker_package_router
from magic_video_editor.studio.exports import (
    ExportStorageUnavailable,
    ImmutableExportCollision,
    ReleaseAssetInvalid,
)
from magic_video_editor.studio.accounts import AccountPolicyRefused
from magic_video_editor.studio.limits import (
    OwnerConcurrentJobLimitExceeded,
    OwnerLimitExceeded,
    OwnerUsage,
)
from magic_video_editor.studio.manifest import DuplicateMotionIds, set_digest
from magic_video_editor.studio.models import (
    AnimationType,
    Approval,
    ApprovalState,
    CandidateSource,
    CompositionApprovalState,
    CompositionModel,
    CompositionRevision,
    CompositionState,
    DeliveryStatus,
    DerivationRecipe,
    NormalizationSpec,
    ExportRecord,
    ExternalSubject,
    Facing,
    FindingSeverity,
    FrameSlot,
    HistoryEntry,
    LoopPolicy,
    MotionCandidate,
    MotionFrameMetadata,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    SpriteLayout,
    ViewId,
    ViewReference,
    CharacterView,
    MotionReference,
    MotionReferenceRole,
    MotionSpecification,
    MotionState,
    ProjectActivityStats,
    ProjectSummary,
    PromptSource,
    QualityAssessment,
    Release,
    ReleaseDelivery,
    RequiredOrderedFrames,
    RevisionLifecycle,
    StylePreset,
    SubjectAssessment,
    SubjectFinding,
    SubjectFindingCode,
    SubjectRevision,
    SubjectRevisionEdit,
    SubjectRevisionSource,
    SubjectState,
)
from magic_video_editor.studio.presets import PRESET_DEFINITIONS, PresetDefinition
from magic_video_editor.studio.service import (
    DEFAULT_ACTIVITY_PAGE_LIMIT,
    ActivityPage,
    CompositionApprovalBlocked,
    CompositionGenerationUnavailable,
    CompositionOperationNotReady,
    CompositionRequestBlocked,
    DeliveryNotConfigured,
    ExportNotFound,
    MotionAssetError,
    MotionDerivationError,
    MotionGenerationUnavailable,
    MotionLineageConflict,
    MotionOperationNotReady,
    MotionRequestBlocked,
    MotionUploadValidationError,
    OperationAlreadyTerminal,
    OperationListing,
    ProjectAlreadyBoundToSubject,
    ProjectDeletionNotConfirmed,
    ProjectNotFound,
    ProjectRepairReport,
    ReleaseNotFound,
    ReleaseNotReady,
    SubjectGenerationUnavailable,
    SubjectRevisionNotFound,
    SubjectUploadError,
    SubjectWaiverUnknown,
    UploadTarget,
    motion_spec_digest,
)
from magic_video_editor.studio.store import (
    DuplicateReleaseName,
    ReleaseIntegrityError,
    RevisionConflict,
    StoredManifest,
)
from magic_video_editor.targets import TargetAdapter, TargetRegistry

MAX_STUDIO_ACTOR_ID_LENGTH = 255
MAX_PROJECT_NAME_LENGTH = 200
MAX_CONTENT_TYPE_LENGTH = 100
MAX_ASSET_KEY_LENGTH = 1024
logger = logging.getLogger(__name__)

MAX_RETURN_TO_LENGTH = 2048
MAX_OAUTH_PARAMETER_LENGTH = 2048
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRUSTED_ACTOR_SOURCE_IAP = "iap"
TRUSTED_ACTOR_SOURCE_SESSION = "session"
LOGIN_PATH = "/login"
LOGIN_AUTH0_ERROR_PATH = "/login?error=auth0"
LOGIN_NOT_ALLOWED_PATH = "/login?error=not_allowed"
LOGIN_VERIFY_EMAIL_PATH = "/login?error=verify_email"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_BOOTSTRAP_AUDIENCE = "bootstrap-deny-all"
IAP_ASSERTION_CACHE_MAX_SECONDS = 300.0
IAP_ASSERTION_CACHE_MAX_ENTRIES = 1024
IAP_VERIFY_TIMEOUT_SECONDS = 2.0
IAP_VERIFY_MAX_CONCURRENCY = 8
IAP_CERT_CACHE_MAX_SECONDS = 3600.0
IAP_CERT_FETCH_TIMEOUT_SECONDS = 1.5
_iap_assertion_cache: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
_iap_assertion_cache_lock = threading.Lock()
_iap_verification_semaphore = asyncio.Semaphore(IAP_VERIFY_MAX_CONCURRENCY)


class _CachedGoogleAuthRequest:
    """Google-auth transport that honors signing-certificate cache lifetime."""

    def __init__(self) -> None:
        self._request = GoogleAuthRequest()
        self._lock = threading.Lock()
        self._cached_response: tuple[float, object] | None = None

    def __call__(self, url: str, **kwargs: object) -> object:
        now = time.time()
        with self._lock:
            if (
                url == IAP_CERTS_URL
                and self._cached_response is not None
                and self._cached_response[0] > now
            ):
                return self._cached_response[1]
            kwargs["timeout"] = min(
                float(kwargs.get("timeout", IAP_CERT_FETCH_TIMEOUT_SECONDS)),
                IAP_CERT_FETCH_TIMEOUT_SECONDS,
            )
            response = self._request(url, **kwargs)
            if url == IAP_CERTS_URL and getattr(response, "status", None) == 200:
                headers = getattr(response, "headers", {})
                cache_control = str(
                    headers.get("cache-control", headers.get("Cache-Control", ""))
                )
                match = re.search(r"(?:^|,)\s*max-age=(\d+)", cache_control)
                if match is not None:
                    lifetime = min(float(match.group(1)), IAP_CERT_CACHE_MAX_SECONDS)
                    if lifetime > 0:
                        self._cached_response = (now + lifetime, response)
            return response


_iap_cert_request = _CachedGoogleAuthRequest()


class ExternalSubjectRequest(BaseModel):
    """The wire shape of `ExternalSubject` (T22). `system` and `entity_kind`
    are plain strings on the wire -- validated against the installed
    `TargetRegistry` by the route (`_require_valid_external_subject`), not by
    this model, so no target's name is baked into the request schema."""

    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=50)
    entity_kind: str = Field(min_length=1, max_length=50)
    entity_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)

    def to_domain(self) -> ExternalSubject:
        return ExternalSubject(
            system=self.system,
            entity_kind=self.entity_kind,
            entity_id=self.entity_id,
            display_name=self.display_name,
        )


class CreateStudioProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_PROJECT_NAME_LENGTH)
    external_subject: ExternalSubjectRequest | None = None
    preset_motions: tuple[MotionPreset, ...] = Field(default=(), max_length=32)


class CreateSubjectUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str = Field(min_length=1, max_length=MAX_CONTENT_TYPE_LENGTH)
    expected_revision: int = Field(ge=1)


class SettleSubjectUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    asset_key: str = Field(min_length=1, max_length=MAX_ASSET_KEY_LENGTH)


class GenerateSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    reference_keys: tuple[str, ...] = Field(min_length=1, max_length=6)
    direction: str | None = Field(default=None, max_length=500)
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=100)


class SettleSubjectGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class CreateAssetPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_key: str = Field(min_length=1, max_length=MAX_ASSET_KEY_LENGTH)


class ApproveSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    waived_finding_ids: tuple[str, ...] = Field(default=(), max_length=100)
    waiver_reason: str | None = Field(default=None, max_length=2000)


class SelectSubjectRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ReframeSubjectRevisionRequest(BaseModel):
    """Which pixel fix the Subject page's finding action asked for."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    edit: SubjectRevisionEdit = SubjectRevisionEdit.REFRAME


class PatchProjectRequest(BaseModel):
    """PATCH /studio/projects/{id} (T22): the only project-level field this
    route can change today is `external_subject`. `None` clears the binding;
    the field is required (not `Field(default=None)`) so an omitted body can
    never be misread as "clear it".
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    external_subject: ExternalSubjectRequest | None


class ComposeFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=2000)
    composition_model: CompositionModel = CompositionModel.NANOBANANA_2_LITE
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=100)


class RegenerateCompositionRequest(ComposeFrameRequest):
    pass


class SettleCompositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class ApproveCompositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class StageSetupReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_base64: str = Field(min_length=1, max_length=13_981_016)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    filename: str | None = Field(default=None, max_length=255)


class PreviewSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    desired_setup: ProjectSetup


class AbandonDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    confirm_dispatch_stopped: bool = False


class ApplySetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    plan_token: str = Field(min_length=1)
    confirmed_impacts: tuple[str, ...] = ()


class SetSpriteLayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    sprite_layout: SpriteLayout


class BindViewReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=MAX_ASSET_KEY_LENGTH)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SetCharacterViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    reference: BindViewReferenceRequest | None = None


class MotionReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=MAX_ASSET_KEY_LENGTH)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: MotionReferenceRole


class MotionDefinition(BaseModel):
    """One motion's shape, shared by the single-add route and the batch route (T6)."""

    model_config = ConfigDict(extra="forbid")
    motion_id: str = Field(
        pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100
    )
    name: str = Field(min_length=1, max_length=200)
    kind: MotionKind = MotionKind.PRESET
    preset: MotionPreset | None = None
    view_id: ViewId | None = None
    action: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)
    prompt_source: PromptSource = PromptSource.PRESET
    semantic_slot: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100
    )
    loop_policy: LoopPolicy = LoopPolicy.LOOP
    required: bool = True
    references: tuple[MotionReferenceRequest, ...] = Field(default=(), max_length=16)
    style: StylePreset | None = None
    script: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_motion_definition(self) -> MotionDefinition:
        if self.kind is MotionKind.CUSTOM and not (self.prompt and self.prompt.strip()):
            raise ValueError("custom motions require a nonempty prompt")
        if self.kind is MotionKind.CUSTOM and self.preset is not None:
            raise ValueError("custom motions cannot specify a preset")
        if self.kind is MotionKind.PRESET and self.preset is None:
            raise ValueError("preset motions require a supported explicit preset")
        if self.view_id is not None and any(ref.role is MotionReferenceRole.START for ref in self.references):
            raise ValueError("view motions cannot override the view start reference")
        roles = {reference.role for reference in self.references}
        if (
            MotionReferenceRole.END in roles
            and MotionReferenceRole.START not in roles
            and MotionReferenceRole.CONTEXT not in roles
            and self.view_id is None
        ):
            raise ValueError(
                "an end reference requires a start or context reference to anchor it"
            )
        return self

    def to_specification(self) -> MotionSpecification:
        return MotionSpecification(
            motion_id=self.motion_id,
            name=self.name,
            kind=self.kind,
            preset=self.preset,
            view_id=self.view_id,
            action=self.action,
            prompt=self.prompt,
            prompt_source=self.prompt_source,
            direction=self.prompt if self.kind is MotionKind.CUSTOM else None,
            semantic_slot=self.semantic_slot,
            loop_policy=self.loop_policy,
            required=self.required,
            references=tuple(
                MotionReference(
                    key=reference.key, sha256=reference.sha256, role=reference.role
                )
                for reference in self.references
            ),
            style=self.style,
            script=self.script,
        )


class AddMotionRequest(MotionDefinition):
    expected_revision: int = Field(ge=1)


class UpdateMotionRequest(MotionDefinition):
    """PATCH /motions/{motion_id} (T17): replace the motion's references/style/script (and the
    rest of its definition) while preserving candidates -- the motion_id here is path-derived,
    not sent by the client."""

    expected_revision: int = Field(ge=1)


class CreateMotionReferenceUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: str = Field(min_length=1, max_length=MAX_CONTENT_TYPE_LENGTH)
    expected_revision: int = Field(ge=1)


class AddMotionsBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    motions: tuple[MotionDefinition, ...] = Field(min_length=1, max_length=32)


class TurnaroundEquipmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=40)
    attachment: Literal["hand", "shoulder", "hip", "back", "other"]
    source_picture_side: Literal["left", "right", "unsided"]


class StartTurnaroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    source_view_id: str = Field(min_length=1, max_length=100)
    source_revision_id: str = Field(min_length=1, max_length=255)
    equipment: tuple[TurnaroundEquipmentRequest, ...] = Field(default=(), max_length=4)
    settings: MotionGenerationSettings | None = None
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class GenerateMotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    request_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", min_length=1, max_length=100
    )
    settings: MotionGenerationSettings = MotionGenerationSettings()
    # The operator's edited "what happens" line, persisted before this dispatch.
    action: str | None = Field(default=None, max_length=200)


class GenerateMotionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    request_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", min_length=1, max_length=100
    )
    motion_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    settings: MotionGenerationSettings = MotionGenerationSettings()
    # T15: every dispatched motion shares the approved composed opening frame
    # bound to the current subject revision.
    shared_starting_pose: bool = False
    # motion_id -> the operator's edited "what happens" line, persisted as one revision
    # before this batch dispatches.
    actions: dict[str, Annotated[str, Field(max_length=200)]] = Field(
        default_factory=dict, max_length=32
    )


class SelectMotionCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class BodyCalibrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_frame: int = Field(ge=0, le=511)
    reference_motion_id: str = Field(default="walk", min_length=1, max_length=255)
    reference_candidate_id: str = Field(min_length=1, max_length=255)
    reference_frame: int = Field(default=0, ge=0, le=511)


class LoopSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["idle", "walk", "attack"]
    target_frame_count: Literal[8, 12, 16] = 12


class CreateMotionRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    source_candidate_id: str = Field(min_length=1, max_length=255)
    ordered_frames: RequiredOrderedFrames = Field(min_length=1, max_length=10_000)
    frame_duration_ms: int = Field(gt=0, le=60_000)
    loop_policy: LoopPolicy
    flip_horizontal: bool = False
    target_frame_count: Literal[4, 8, 12, 16, 32] | None = None
    cell_size: Literal[256, 512, 1024] | None = None
    normalization: NormalizationSpec | None = None


class SettleMotionOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class DeriveAlphaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class CreateCandidateUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: str = Field(min_length=1, max_length=MAX_CONTENT_TYPE_LENGTH)
    expected_revision: int = Field(ge=1)


class SettleCandidateUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    asset_key: str = Field(min_length=1, max_length=MAX_ASSET_KEY_LENGTH)
    frame_count: int | None = Field(default=None, ge=1, le=2000)


class MotionCandidateResponse(BaseModel):
    execution_provenance: dict[str, Any] = Field(default_factory=dict)
    candidate_id: str
    artifact_key: str
    sha256: str
    metadata: MotionFrameMetadata | None
    assessment: QualityAssessment | None
    duration_ms: int | None
    ordered_frames: tuple[int, ...] | None
    frame_holds: tuple[int, ...] | None
    frame_duration_ms: int | None
    loop_policy: LoopPolicy | None
    generation_settings: MotionGenerationSettings | None
    facing: Facing
    composition_revision_id: str | None = None
    recipe: DerivationRecipe | None
    source: CandidateSource
    subject_revision_id: str | None = None
    motion_spec_digest: str | None = None

    @classmethod
    def from_domain(cls, candidate: MotionCandidate) -> MotionCandidateResponse:
        slots: tuple[FrameSlot, ...] | None = candidate.ordered_frames
        return cls(
            execution_provenance=candidate.execution_provenance,
            candidate_id=candidate.candidate_id,
            artifact_key=candidate.artifact_key,
            sha256=candidate.sha256,
            metadata=candidate.metadata,
            assessment=candidate.assessment,
            duration_ms=candidate.duration_ms,
            ordered_frames=tuple(slot.index for slot in slots)
            if slots is not None
            else None,
            frame_holds=tuple(slot.hold for slot in slots)
            if slots is not None
            else None,
            frame_duration_ms=candidate.frame_duration_ms,
            loop_policy=candidate.loop_policy,
            generation_settings=candidate.generation_settings,
            facing=candidate.facing,
            composition_revision_id=candidate.composition_revision_id,
            recipe=candidate.recipe,
            source=candidate.source,
            subject_revision_id=candidate.subject_revision_id,
            motion_spec_digest=candidate.motion_spec_digest,
        )


class AssessSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class ApproveSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    waived_finding_ids: tuple[str, ...] = Field(default=(), max_length=100)
    waiver_reason: str | None = Field(default=None, max_length=2000)


class CreateReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)


class CreateExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = Field(default="png-json", min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class ReleaseDeliveryResponse(BaseModel):
    release_id: str
    target: str
    status: DeliveryStatus
    delivered_at: datetime | None
    summary: str
    details: dict[str, object]
    error: str | None
    retryable: bool

    @classmethod
    def from_domain(cls, delivery: ReleaseDelivery) -> ReleaseDeliveryResponse:
        return cls(
            release_id=delivery.release_id,
            target=delivery.target,
            status=delivery.status,
            delivered_at=delivery.delivered_at,
            summary=delivery.summary,
            details=dict(delivery.details),
            error=delivery.error,
            retryable=delivery.retryable,
        )


class ReleaseResponse(BaseModel):
    release_id: str
    project_id: str
    name: str
    source_revision: int
    manifest_digest: str
    release_digest: str
    subject_revision_id: str | None
    motion_revision_ids: dict[str, str]
    quality_policy_versions: tuple[str, ...]
    set_approval: ApprovalResponse
    deliveries: tuple[ReleaseDeliveryResponse, ...] = ()
    created_at: datetime

    @classmethod
    def from_domain(
        cls, release: Release, *, deliveries: tuple[ReleaseDelivery, ...] = ()
    ) -> ReleaseResponse:
        return cls(
            release_id=release.release_id,
            project_id=release.project_id,
            name=release.name,
            source_revision=release.source_revision,
            manifest_digest=release.manifest_digest,
            release_digest=release.release_digest,
            subject_revision_id=release.subject_revision_id,
            motion_revision_ids=release.motion_revision_ids,
            quality_policy_versions=release.quality_policy_versions,
            set_approval=ApprovalResponse.from_domain(release.set_approval),
            deliveries=tuple(
                ReleaseDeliveryResponse.from_domain(delivery) for delivery in deliveries
            ),
            created_at=release.created_at,
        )


class ExportResponse(BaseModel):
    export_id: str
    release_id: str
    profile_name: str
    profile_version: str
    artifact_key: str
    sha256: str
    size_bytes: int
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_domain(cls, record: ExportRecord) -> ExportResponse:
        return cls.model_validate(record.model_dump())


class MotionReferenceResponse(BaseModel):
    key: str
    sha256: str
    role: MotionReferenceRole

    @classmethod
    def from_domain(cls, reference: MotionReference) -> MotionReferenceResponse:
        return cls.model_validate(reference.model_dump())


class MotionResponse(BaseModel):
    motion_id: str
    name: str
    kind: MotionKind
    preset: MotionPreset | None
    view_id: str | None = None
    view_heading: ViewId | None = None
    setup_version: int | None = None
    view_reference: ViewReference | None = None
    semantic_slot: str | None
    loop_policy: LoopPolicy
    required: bool
    action: str | None
    prompt: str | None
    prompt_source: PromptSource
    references: tuple[MotionReferenceResponse, ...]
    style: StylePreset | None
    script: str | None
    candidates: tuple[MotionCandidateResponse, ...]
    selected_candidate_id: str | None
    # The digest of the spec as it stands now; a take carrying a different one is older.
    spec_digest: str

    @classmethod
    def from_domain(cls, motion: MotionState) -> MotionResponse:
        spec = motion.specification
        return cls(
            motion_id=motion.motion_id,
            name=spec.name,
            kind=spec.kind,
            preset=spec.preset,
            view_id=spec.view_id,
            view_heading=spec.view_heading,
            setup_version=spec.setup_version,
            view_reference=spec.view_reference,
            semantic_slot=spec.semantic_slot,
            loop_policy=spec.loop_policy,
            required=spec.required,
            action=spec.action,
            prompt=spec.prompt,
            prompt_source=spec.prompt_source,
            references=tuple(
                MotionReferenceResponse.from_domain(reference)
                for reference in spec.references
            ),
            style=spec.style,
            script=spec.script,
            candidates=tuple(
                MotionCandidateResponse.from_domain(item) for item in motion.candidates
            ),
            selected_candidate_id=motion.selected_candidate_id,
            spec_digest=motion_spec_digest(spec),
        )


class PresetResponse(BaseModel):
    preset: MotionPreset
    display_name: str
    description: str
    semantic_slot: str
    loop_policy: LoopPolicy
    animation_type: AnimationType
    default_action: str

    @classmethod
    def from_domain(cls, definition: PresetDefinition) -> PresetResponse:
        return cls(
            preset=definition.preset,
            display_name=definition.display_name,
            description=definition.description,
            semantic_slot=definition.semantic_slot,
            loop_policy=definition.loop_policy,
            animation_type=definition.animation_type,
            default_action=definition.default_action,
        )


class TargetSlotResponse(BaseModel):
    name: str
    accepts: tuple[str, ...]
    winner: str


class TargetResponse(BaseModel):
    system: str
    display_name: str
    entity_kinds: tuple[str, ...]
    slots: tuple[TargetSlotResponse, ...]
    delivery_configured: bool

    @classmethod
    def from_adapter(
        cls, adapter: TargetAdapter, *, delivery_configured: bool
    ) -> TargetResponse:
        return cls(
            system=adapter.descriptor.system,
            display_name=adapter.descriptor.display_name,
            entity_kinds=adapter.descriptor.entity_kinds,
            slots=tuple(
                TargetSlotResponse(
                    name=slot.name, accepts=slot.accepts, winner=slot.winner
                )
                for slot in adapter.descriptor.slots
            ),
            delivery_configured=delivery_configured,
        )


class OperationResponse(BaseModel):
    generation_evidence: dict[str, object] = Field(default_factory=dict)
    requested_settings: dict[str, Any] = Field(default_factory=dict)
    resolved_recipe: dict[str, Any] = Field(default_factory=dict)
    execution_provenance: dict[str, Any] = Field(default_factory=dict)
    job_id: str
    operation: JobOperation
    status: JobStatus
    motion_id: str | None
    turnaround_id: str | None = None
    output_key: str
    progress_percent: int | None
    stage: OperationStage
    failure_code: JobFailureCode | None
    failure_explanation: str | None
    retryable: bool
    created_at: datetime | None
    updated_at: datetime | None
    generation_settings: MotionGenerationSettings | None
    prompt: str | None
    settlement_blocked: str | None

    @classmethod
    def from_listing(cls, listing: OperationListing) -> OperationResponse:
        return cls.from_domain(listing.record, settled=listing.settled)

    @classmethod
    def from_domain(cls, job: JobRecord, *, settled: bool = False) -> OperationResponse:
        motion_id = job.payload.get("motion_id")
        stage = {
            JobStatus.QUEUED: OperationStage.QUEUED,
            JobStatus.PROCESSING: OperationStage.GENERATING,
            JobStatus.COMPLETED: (
                OperationStage.SETTLED
                if settled
                else OperationStage.AWAITING_SETTLEMENT
            ),
            JobStatus.FAILED: OperationStage.FAILED,
            JobStatus.CANCELLED: OperationStage.CANCELLED,
        }[job.status]
        generation_settings, prompt = _motion_generation_summary(job)
        return cls(
            generation_evidence={key: job.payload[key] for key in (
                "subject_revision_id", "subject_artifact_key", "subject_sha256", "motion_spec_digest",
                "view_id", "view_reference", "reference_preparation", "start_frame_key", "start_frame_sha256", "composition_revision_id",
                "source_view_id", "source_reference", "source_backdrop", "prompt_version",
            ) if job.payload.get(key) is not None},
            turnaround_id=job.payload.get("turnaround_id"),
            requested_settings=job.payload.get("requested_settings", {}),
            resolved_recipe=job.payload.get("resolved_recipe", {}),
            execution_provenance=job.result.execution_provenance if job.result else {},
            job_id=job.job_id,
            operation=job.operation,
            status=job.status,
            motion_id=motion_id if isinstance(motion_id, str) else None,
            output_key=job.output_key,
            progress_percent=job.progress_percent,
            stage=stage,
            failure_code=job.failure_code,
            failure_explanation=job.failure_message,
            retryable=job.retryable,
            created_at=job.created_at,
            updated_at=job.updated_at,
            generation_settings=generation_settings,
            prompt=prompt,
            settlement_blocked=(
                job.settlement_note
                if stage is OperationStage.AWAITING_SETTLEMENT
                else None
            ),
        )


def _motion_generation_summary(
    job: JobRecord,
) -> tuple[MotionGenerationSettings | None, str | None]:
    """A `GENERATE_MOTION` or `GENERATE_TURNAROUND` job's dispatch payload already
    carries its resolved settings and prompt (T14); anything else (or a payload that
    fails to parse) reports both as absent rather than guessing.
    """
    if job.operation not in {JobOperation.GENERATE_MOTION, JobOperation.GENERATE_TURNAROUND}:
        return None, None
    prompt = job.payload.get("prompt")
    try:
        settings = MotionGenerationSettings(
            preset=job.payload.get("preset", "standard"),
            steps=job.payload.get("steps", 20),
            seed=job.payload.get("seed", 0),
            width=job.payload.get("width", 512),
            height=job.payload.get("height", 512),
            duration_seconds=job.payload.get("duration_seconds", 4),
            backdrop=job.payload.get("backdrop", "chroma_green"),
            provider_parameters=job.payload.get("provider_parameters", {}),
        )
    except ValueError:
        settings = None
    return settings, prompt if isinstance(prompt, str) else None


class CreateViewDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    turnaround_id: str = Field(pattern=r"^turnaround-[0-9a-f]{32}$")
    pick_revision: int = Field(ge=1)
    heading: ViewId
    standing: bool = False
    request_id: str = Field(min_length=1, max_length=100)


class CheckViewDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    facing: bool
    equipment: bool
    identity: bool


class DiscardViewDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class TurnaroundPickEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_index: int = Field(ge=0)
    confirm_out_of_region: bool = False
    confirm_out_of_order: bool = False


class SaveTurnaroundPicksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_pick_revision: int = Field(ge=0)
    picks: dict[ViewId, TurnaroundPickEntry] = Field(max_length=8)


class QueuedMotionResponse(BaseModel):
    motion_id: str
    operation: OperationResponse


class FailedMotionResponse(BaseModel):
    motion_id: str
    code: str
    message: str


class GenerateMotionsResponse(BaseModel):
    queued: tuple[QueuedMotionResponse, ...]
    failed: tuple[FailedMotionResponse, ...]


class ActivityPageResponse(BaseModel):
    operations: tuple[OperationResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, page: ActivityPage) -> ActivityPageResponse:
        return cls(
            operations=tuple(
                OperationResponse.from_listing(item) for item in page.operations
            ),
            next_cursor=page.next_cursor,
        )


class SkippedOperationResponse(BaseModel):
    job_id: str
    state: JobStatus


class CancelAllOperationsResponse(BaseModel):
    cancelled: tuple[str, ...]
    skipped: tuple[SkippedOperationResponse, ...]


class GpuTelemetryResponse(BaseModel):
    provider: str
    worker_healthy: bool
    device_name: str | None
    vram_used_mb: int | None
    vram_total_mb: int | None
    vram_reserved_mb: int | None
    utilization_percent: int | None
    active_operation_count: int
    sampled_at: datetime
    stale: bool

    @classmethod
    def from_domain(cls, value: GpuTelemetry) -> GpuTelemetryResponse:
        return cls.model_validate(value.__dict__)


class ApprovalResponse(BaseModel):
    state: ApprovalState
    assessment_id: str | None
    approver_id: str | None
    policy_version: str | None
    artifact_sha256: str | None
    manifest_digest: str | None
    waived_finding_ids: tuple[str, ...]
    waiver_reason: str | None

    @classmethod
    def from_domain(cls, approval: Approval) -> ApprovalResponse:
        return cls.model_validate(approval.model_dump())


class SubjectFindingResponse(BaseModel):
    code: SubjectFindingCode
    severity: FindingSeverity
    title: str
    explanation: str
    artifact_sha256: str
    action_id: str | None

    @classmethod
    def from_domain(cls, finding: SubjectFinding) -> SubjectFindingResponse:
        return cls(
            code=finding.code,
            severity=finding.severity,
            title=finding.title,
            explanation=finding.explanation,
            artifact_sha256=finding.artifact_sha256,
            action_id=finding.action_id,
        )


class SubjectAssessmentResponse(BaseModel):
    assessment_id: str
    policy_version: str
    artifact_sha256: str
    width: int
    height: int
    occupancy_ratio: float
    findings: tuple[SubjectFindingResponse, ...]

    @classmethod
    def from_domain(cls, assessment: SubjectAssessment) -> SubjectAssessmentResponse:
        return cls(
            assessment_id=assessment.assessment_id,
            policy_version=assessment.policy_version,
            artifact_sha256=assessment.artifact_sha256,
            width=assessment.width,
            height=assessment.height,
            occupancy_ratio=assessment.occupancy_ratio,
            findings=tuple(
                SubjectFindingResponse.from_domain(finding)
                for finding in assessment.findings
            ),
        )


class SubjectRevisionResponse(BaseModel):
    revision_id: str
    artifact_key: str
    sha256: str
    width: int
    height: int
    lifecycle: RevisionLifecycle
    parent_revision_ids: tuple[str, ...]
    source: SubjectRevisionSource
    direction: str | None
    created_at: datetime | None
    assessment: SubjectAssessmentResponse | None = None

    @classmethod
    def from_domain(
        cls,
        revision: SubjectRevision,
        *,
        assessment: SubjectAssessment | None = None,
    ) -> SubjectRevisionResponse:
        return cls(
            revision_id=revision.revision_id,
            artifact_key=revision.artifact_key,
            sha256=revision.sha256,
            width=revision.width,
            height=revision.height,
            lifecycle=revision.lifecycle,
            parent_revision_ids=revision.parent_revision_ids,
            source=revision.source,
            direction=revision.direction,
            created_at=revision.created_at,
            assessment=(
                SubjectAssessmentResponse.from_domain(assessment)
                if assessment is not None and assessment.artifact_sha256 == revision.sha256
                else None
            ),
        )


class SubjectStateResponse(BaseModel):
    current_revision: SubjectRevisionResponse | None
    revisions: tuple[SubjectRevisionResponse, ...]
    assessment: SubjectAssessmentResponse | None
    approval: ApprovalResponse

    @classmethod
    def from_domain(cls, state: SubjectState) -> SubjectStateResponse:
        return cls(
            current_revision=(
                SubjectRevisionResponse.from_domain(
                    state.current_revision, assessment=state.assessment
                )
                if state.current_revision is not None
                else None
            ),
            revisions=tuple(
                SubjectRevisionResponse.from_domain(
                    revision,
                    assessment=(
                        state.assessment
                        if revision.revision_id == state.current_revision_id
                        else None
                    ),
                )
                for revision in reversed(state.revisions)
            ),
            assessment=(
                SubjectAssessmentResponse.from_domain(state.assessment)
                if state.assessment is not None
                else None
            ),
            approval=ApprovalResponse.from_domain(state.approval),
        )


class CompositionRevisionResponse(BaseModel):
    revision_id: str
    artifact_key: str
    sha256: str
    prompt: str
    model: CompositionModel
    parent_subject_revision_id: str
    approval: CompositionApprovalState

    @classmethod
    def from_domain(cls, revision: CompositionRevision) -> CompositionRevisionResponse:
        return cls.model_validate(revision.model_dump())


class CompositionStateResponse(BaseModel):
    current: CompositionRevisionResponse | None

    @classmethod
    def from_domain(cls, state: CompositionState) -> CompositionStateResponse:
        return cls(
            current=(
                CompositionRevisionResponse.from_domain(state.current)
                if state.current is not None
                else None
            )
        )


class ExternalSubjectResponse(BaseModel):
    system: str
    entity_kind: str
    entity_id: str
    display_name: str

    @classmethod
    def from_domain(cls, subject: ExternalSubject) -> ExternalSubjectResponse:
        return cls(
            system=subject.system,
            entity_kind=subject.entity_kind,
            entity_id=subject.entity_id,
            display_name=subject.display_name,
        )


class StudioManifestSummaryResponse(BaseModel):
    sprite_layout: SpriteLayout = SpriteLayout.UNASSIGNED
    views: tuple[CharacterView, ...] = ()
    project_id: str
    name: str
    owner_id: str | None
    external_subject: ExternalSubjectResponse | None
    subject: SubjectStateResponse
    composition: CompositionStateResponse
    motions: tuple[MotionResponse, ...]
    set_assessment: QualityAssessment | None
    set_assessment_id: str | None
    set_assessment_digest: str | None
    set_assessment_fresh: bool
    set_approval: ApprovalResponse
    set_approval_fresh: bool


class StudioProjectResponse(BaseModel):
    project_id: str
    revision: int
    manifest_digest: str
    updated_at: datetime
    manifest: StudioManifestSummaryResponse
    active_operations: tuple[OperationResponse, ...] = ()
    primary_action: PrimaryAction

    @classmethod
    def from_domain(
        cls,
        stored: StoredManifest,
        operations: tuple[OperationListing, ...] = (),
        has_release: bool = False,
    ) -> StudioProjectResponse:
        return cls(
            project_id=stored.project_id,
            revision=stored.revision,
            manifest_digest=stored.manifest_digest,
            updated_at=stored.updated_at,
            manifest=StudioManifestSummaryResponse(
                sprite_layout=stored.manifest.sprite_layout,
                views=stored.manifest.views,                project_id=stored.manifest.project_id,
                name=stored.manifest.name,
                owner_id=stored.manifest.owner_id,
                external_subject=(
                    ExternalSubjectResponse.from_domain(
                        stored.manifest.external_subject
                    )
                    if stored.manifest.external_subject is not None
                    else None
                ),
                subject=SubjectStateResponse.from_domain(stored.manifest.subject),
                composition=CompositionStateResponse.from_domain(
                    stored.manifest.composition
                ),
                motions=tuple(
                    MotionResponse.from_domain(item) for item in stored.manifest.motions
                ),
                set_assessment=stored.manifest.set_assessment,
                set_assessment_id=stored.manifest.set_assessment_id,
                set_assessment_digest=stored.manifest.set_assessment_digest,
                set_assessment_fresh=(
                    stored.manifest.set_assessment is not None
                    and stored.manifest.set_assessment_digest
                    == set_digest(stored.manifest)
                ),
                set_approval=ApprovalResponse.from_domain(stored.manifest.set_approval),
                set_approval_fresh=(
                    stored.manifest.set_approval.state
                    in {ApprovalState.APPROVED, ApprovalState.WAIVED}
                    and stored.manifest.set_approval.manifest_digest
                    == set_digest(stored.manifest)
                ),
            ),
            active_operations=tuple(
                OperationResponse.from_listing(operation)
                for operation in operations
                if operation.record.status
                in {
                    JobStatus.QUEUED,
                    JobStatus.PROCESSING,
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                }
            ),
            primary_action=_primary_action(stored, has_release),
        )


class ProjectActivityStatsResponse(BaseModel):
    running: int
    failed: int
    completed: int
    last_activity_at: datetime | None

    @classmethod
    def from_domain(cls, stats: ProjectActivityStats) -> ProjectActivityStatsResponse:
        return cls(
            running=stats.running,
            failed=stats.failed,
            completed=stats.completed,
            last_activity_at=stats.last_activity_at,
        )


class ProjectSummaryResponse(BaseModel):
    project_id: str
    name: str
    revision: int
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    subject_state: Literal["approved", "pending", "none"]
    motion_count: int
    release_count: int
    external_subject: ExternalSubjectResponse | None
    activity: ProjectActivityStatsResponse
    unreadable: bool = False
    expires_at: datetime | None = None

    @classmethod
    def from_domain(cls, summary: ProjectSummary) -> ProjectSummaryResponse:
        return cls(
            project_id=summary.project_id,
            name=summary.name,
            revision=summary.revision,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
            created_by=summary.created_by,
            subject_state=summary.subject_state,
            motion_count=summary.motion_count,
            release_count=summary.release_count,
            external_subject=(
                ExternalSubjectResponse.from_domain(summary.external_subject)
                if summary.external_subject is not None
                else None
            ),
            activity=ProjectActivityStatsResponse.from_domain(summary.activity),
            unreadable=summary.unreadable,
            expires_at=summary.expires_at,
        )


class ConfirmAccountDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_email: str = Field(min_length=1, max_length=320)


class ConfirmDeletionRequest(BaseModel):
    """Deletion is permanent, so the caller repeats the name of what it deletes."""

    model_config = ConfigDict(extra="forbid")

    confirm_name: str = Field(min_length=1, max_length=500)


class UploadTargetResponse(BaseModel):
    key: str
    upload_url: str
    # Send these with the PUT, unchanged; empty when the grant has no ceiling.
    upload_headers: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, target: UploadTarget) -> UploadTargetResponse:
        return cls.model_validate(target.model_dump())


class BadRevisionResponse(BaseModel):
    revision: int
    created_at: datetime
    error_summary: str
    null_paths: tuple[str, ...]


class ProjectRepairReportResponse(BaseModel):
    project_id: str
    current_revision_before: int
    restored_from_revision: int | None
    new_revision: int | None
    repaired: bool
    bad_revisions: tuple[BadRevisionResponse, ...]

    @classmethod
    def from_domain(cls, report: ProjectRepairReport) -> ProjectRepairReportResponse:
        return cls(
            project_id=report.project_id,
            current_revision_before=report.current_revision_before,
            restored_from_revision=report.restored_from_revision,
            new_revision=report.new_revision,
            repaired=report.repaired,
            bad_revisions=tuple(
                BadRevisionResponse(
                    revision=item.revision,
                    created_at=item.created_at,
                    error_summary=item.error_summary,
                    null_paths=item.null_paths,
                )
                for item in report.bad_revisions
            ),
        )


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_API_KEY_NAME_LENGTH)


class ApiKeyResponse(BaseModel):
    key_id: str
    actor_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_domain(cls, key: StudioApiKey) -> ApiKeyResponse:
        return cls.model_validate(key.model_dump())


class SaveProviderKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=3, max_length=MAX_PROVIDER_KEY_LENGTH)


class ProviderKeyResponse(BaseModel):
    """A saved provider key is described by its last characters only."""

    provider: str
    available: bool
    configured: bool
    key_hint: str | None = None
    updated_at: datetime | None = None


class CreatedApiKeyResponse(ApiKeyResponse):
    """The plaintext `key` is returned here and nowhere else, ever again."""

    key: str


class PairingTokenResponse(BaseModel):
    """The plaintext `token` is returned here and nowhere else, ever again.

    There is no route to list or re-read a live token: a token is minted when
    someone asks for one, and a fresh ask mints a fresh one.
    """

    token: str
    expires_at: datetime


class EnrolledWorkerResponse(BaseModel):
    worker_id: str
    label: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    # The agent renews itself well before this; it is shown so a machine that has
    # been off long enough to fall out of its window can be recognised as such
    # rather than looking mysteriously idle.
    credential_expires_at: datetime | None = None
    rotated_at: datetime | None = None

    @classmethod
    def from_domain(cls, worker: EnrolledWorker) -> EnrolledWorkerResponse:
        return cls.model_validate(worker.model_dump())


class RedeemedWorkerResponse(EnrolledWorkerResponse):
    """What the agent stores: its durable credential and the owner whose work it
    may lease. The credential is shown exactly once."""

    credential: str
    tenant_id: str


class RedeemPairingTokenRequest(BaseModel):
    token: str
    label: str | None = None


class RotateCredentialRequest(BaseModel):
    """The agent presents its current credential and receives its next one.

    Sent in the body rather than an `Authorization` header so that a rotation is
    never mistaken for an ordinary authenticated call, and cannot be replayed by
    a proxy that happens to forward auth headers.
    """

    credential: str


class RenameWorkerRequest(BaseModel):
    label: str | None = None


class SignOutResponse(BaseModel):
    """`logout_url` ends the Auth0 session too; the browser follows it after the
    studio's own cookie is gone."""

    logout_url: str | None = None


class OwnerUsageResponse(BaseModel):
    """The signed-in operator's own usage against the per-owner caps. `0` on a
    limit means unlimited. `account_tier` is `invited` or `public`."""

    stored_bytes: int
    storage_limit_bytes: int
    running_jobs: int
    concurrent_job_limit: int
    account_tier: str = "invited"

    @classmethod
    def from_domain(cls, usage: OwnerUsage) -> OwnerUsageResponse:
        return cls.model_validate(usage.model_dump())


class AuthSessionResponse(BaseModel):
    actor_id: str
    email: str | None = None
    display_name: str | None = None
    sign_in_method: SignInMethod | None = None
    usage: OwnerUsageResponse | None = None
    admin: bool = False

    @classmethod
    def from_identity(
        cls, identity: ResolvedIdentity, usage: OwnerUsageResponse | None = None, admin: bool = False
    ) -> AuthSessionResponse:
        return cls.model_validate({**identity.model_dump(), "usage": usage, "admin": admin})


def create_studio_router(
    service: StudioServicePort,
    *,
    registry: TargetRegistry | None = None,
    api_keys: StudioApiKeyStore | None = None,
    auth: StudioAuthService | None = None,
    enrolment: StudioEnrolmentStore | None = None,
    pool_enrolment: Callable[[EnrolledWorker], Awaitable[None]] | None = None,
    provider_accounts: ProviderAccounts | None = None,
    owner_erasers: Sequence[Callable[[str], Awaitable[None]]] = (),
) -> APIRouter:
    """`pool_enrolment` approves a redeemed machine in the worker pool; the pool
    admits no machine without that server-side approval. Omitted when no pool
    is configured, since then no machine can attach at all. `owner_erasers`
    delete what other services (movies) hold for an account being deleted."""
    router = APIRouter(prefix="/studio", tags=["studio"])
    api_key_store = api_keys if api_keys is not None else MemoryStudioApiKeyStore()
    enrolment_store = enrolment
    target_registry = (
        registry if registry is not None else TargetRegistry.from_entry_points()
    )

    def _require_valid_external_subject(request: ExternalSubjectRequest) -> None:
        adapter = target_registry.get(request.system)
        if adapter is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_target", "system": request.system},
            )
        if request.entity_kind not in adapter.descriptor.entity_kinds:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unknown_entity_kind",
                    "entity_kind": request.entity_kind,
                },
            )

    async def _is_admin(actor_id: str) -> bool:
        operators = getattr(service, "operators", None)
        return operators is not None and await operators.is_admin(actor_id)

    def _require_auth() -> StudioAuthService:
        if auth is None:
            raise HTTPException(
                status_code=503, detail="studio sign-in is not configured"
            )
        return auth

    @router.get("/auth/session", response_model=AuthSessionResponse)
    async def auth_session(
        request: Request,
        actor_id: str = Depends(studio_actor_id),
    ) -> AuthSessionResponse:
        usage = OwnerUsageResponse.from_domain(await service.owner_usage(actor_id))
        admin = await _is_admin(actor_id)
        identity = getattr(request.state, "studio_identity", None)
        if identity is None:
            return AuthSessionResponse(actor_id=actor_id, usage=usage, admin=admin)
        return AuthSessionResponse.from_identity(identity, usage, admin)

    @router.delete("/account", response_model=SignOutResponse)
    async def delete_account_route(
        request: Request,
        body: ConfirmAccountDeletionRequest,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> Response:
        """Permanently delete the signed-in account: every project, movie and
        scene flight with their files, saved provider keys, API keys, attached
        machines, sessions and the user record. Browser session only, and the
        caller types their email, so neither a key nor a stray click can do it."""
        user = None if auth is None else await user_for_actor(auth.store, actor_id)
        expected = user.email if user is not None else actor_id
        if normalize_email(body.confirm_email) != normalize_email(expected):
            raise HTTPException(
                status_code=422,
                detail={"code": "deletion_not_confirmed", "message": "type your account email to delete it"},
            )
        await service.erase_owner(actor_id)
        for erase in owner_erasers:
            await erase(actor_id)
        await api_key_store.forget_owner(actor_id)
        if user is not None and auth is not None:
            await auth.store.delete_user(user.user_id)
        provider = None if auth is None else auth.auth0
        body_out = SignOutResponse(
            logout_url=(
                None
                if provider is None
                else provider.logout_url(f"{_public_base_url(request)}{LOGIN_PATH}")
            )
        )
        response = JSONResponse(body_out.model_dump())
        clear_session_cookie(response)
        return response

    @router.post("/auth/sign-out", response_model=SignOutResponse)
    async def sign_out_route(request: Request) -> Response:
        """Revoke the session row and clear the cookie, and hand back the Auth0
        logout URL so the browser can end the tenant session too. Idempotent:
        signing out without a session is not an error."""
        _require_same_origin(request)
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie and auth is not None:
            await auth.sign_out(cookie)
        provider = None if auth is None else auth.auth0
        body = SignOutResponse(
            logout_url=(
                None
                if provider is None
                else provider.logout_url(f"{_public_base_url(request)}{LOGIN_PATH}")
            )
        )
        response = JSONResponse(body.model_dump())
        clear_session_cookie(response)
        return response

    @router.get("/auth/login")
    async def auth0_login_route(
        request: Request,
        return_to: str | None = Query(default=None, max_length=MAX_RETURN_TO_LENGTH),
        signup: bool = Query(default=False),
    ) -> RedirectResponse:
        provider = _require_auth().auth0
        if provider is None:
            raise HTTPException(status_code=503, detail="sign-in is not configured")
        verifier, challenge = pkce_pair()
        handoff = OAuthHandoff(
            state=generate_token(OAUTH_STATE_BYTES),
            code_verifier=verifier,
            return_to=safe_return_path(return_to),
        )
        response = RedirectResponse(
            provider.authorization_url(
                state=handoff.state,
                code_challenge=challenge,
                redirect_uri=_auth0_redirect_uri(request),
                signup=signup,
            ),
            status_code=302,
        )
        set_oauth_cookie(response, encode_oauth_handoff(handoff))
        return response

    @router.get("/auth/callback")
    async def auth0_callback_route(
        request: Request,
        code: str | None = Query(default=None, max_length=MAX_OAUTH_PARAMETER_LENGTH),
        state: str | None = Query(default=None, max_length=MAX_OAUTH_PARAMETER_LENGTH),
    ) -> RedirectResponse:
        service_auth = _require_auth()
        handoff = _valid_oauth_handoff(request, state)
        if handoff is None or not code or service_auth.auth0 is None:
            return _sign_in_error_response(LOGIN_AUTH0_ERROR_PATH)
        try:
            identity = await service_auth.auth0.exchange(
                code=code,
                code_verifier=handoff.code_verifier,
                redirect_uri=_auth0_redirect_uri(request),
            )
            user, sign_in_method = await service_auth.sign_in_with_auth0(identity)
        except EmailNotAllowed:
            logger.info("a sign-in was refused because the address is not allowlisted")
            return _sign_in_error_response(LOGIN_NOT_ALLOWED_PATH)
        except EmailNotVerified:
            logger.info("a sign-in was refused until the address is verified")
            return _sign_in_error_response(LOGIN_VERIFY_EMAIL_PATH)
        except (SignInRejected, ValueError, OSError, httpx.HTTPError):
            logger.warning("Auth0 sign-in did not complete", exc_info=True)
            return _sign_in_error_response(LOGIN_AUTH0_ERROR_PATH)
        cookie = await service_auth.issue_session(
            user,
            sign_in_method=sign_in_method,
            user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(handoff.return_to or "/", status_code=303)
        set_session_cookie(response, cookie)
        clear_oauth_cookie(response)
        return response

    @router.get("/auth/bootstrap")
    async def auth_bootstrap(
        return_to: str = "/",
        _actor_id: str = Depends(studio_actor_id),
    ) -> RedirectResponse:
        if not _safe_same_origin_return_path(return_to):
            raise HTTPException(status_code=400, detail="invalid return path")
        return RedirectResponse(return_to, status_code=303)

    @router.post("/api-keys", response_model=CreatedApiKeyResponse)
    async def create_api_key_route(
        request: CreateApiKeyRequest,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> CreatedApiKeyResponse:
        """Mint a key for the caller. Browser-authenticated only: a key must never
        be able to mint another key, or revoking the leaked one would not be enough."""
        plaintext = generate_api_key()
        try:
            created = await api_key_store.create(
                actor_id=actor_id,
                name=request.name,
                key_hash=hash_api_key(plaintext),
            )
        except ApiKeyLimitReached as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "api_key_limit_reached", "limit": exc.limit},
            ) from exc
        return CreatedApiKeyResponse(
            **ApiKeyResponse.from_domain(created).model_dump(), key=plaintext
        )

    # Served by the app rather than as static assets on purpose. The web bucket
    # sits behind a CDN that holds a response for an hour and cannot be purged
    # from a deploy, so a changed attach script would keep handing out the old
    # one -- and the first version of this shipped as a static file and came back
    # as the studio's HTML instead. Under /studio/ these are always current.
    @router.get("/attach.sh", response_class=PlainTextResponse)
    async def attach_script_route() -> str:
        """The one command's payload: installs the worker and attaches a machine."""
        return files("magic_video_editor").joinpath("attach.sh").read_text()

    @router.get("/attach.ps1", response_class=PlainTextResponse)
    async def attach_script_windows_route() -> str:
        """The Windows equivalent. `curl … | sh` is not a thing in PowerShell --
        it fails with "The term 'sh' is not recognized", which is what the first
        Windows user hit."""
        return files("magic_video_editor").joinpath("attach.ps1").read_text()

    @router.get("/worker.py", response_class=PlainTextResponse)
    async def worker_runner_route() -> str:
        """The runner an attached machine executes. Served because that machine
        cannot install this private package; it is the module the tests cover."""
        return files("magic_video_editor").joinpath("worker_runner.py").read_text()

    @router.get("/cpu-worker.py", response_class=PlainTextResponse)
    async def cpu_worker_route() -> str:
        return files("magic_video_editor").joinpath("cpu_worker.py").read_text()

    @router.get("/worker-update.py", response_class=PlainTextResponse)
    async def worker_update_route() -> str:
        return files("magic_video_editor").joinpath("worker_update.py").read_text()

    @router.get("/worker-runtime.py", response_class=PlainTextResponse)
    async def worker_runtime_route() -> str:
        return files("magic_video_editor").joinpath("worker_runtime.py").read_text()

    @router.get("/worker-install.py", response_class=PlainTextResponse)
    async def worker_installer_route() -> str:
        """Public bootstrap logic; the wheel itself requires a pairing code."""
        return files("magic_video_editor").joinpath("worker_install.py").read_text()

    @router.get("/api.md", response_class=PlainTextResponse)
    async def api_documentation_route() -> str:
        """The API reference the keys page links to. Served here because the
        repository it used to link to is private: for anyone without access that
        link was a login wall, not an answer."""
        return files("magic_video_editor").joinpath("api.md").read_text()

    @router.post("/workers/pairing-tokens", response_model=PairingTokenResponse)
    async def mint_pairing_token_route(
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> PairingTokenResponse:
        """Mint a pairing token for the caller: single use, one hour.

        Browser-authenticated only. A machine credential must never mint a
        pairing token, or one compromised machine could enrol others.
        """
        if enrolment_store is None:
            raise HTTPException(status_code=503, detail="enrolment is not configured")
        plaintext = generate_pairing_token()
        expiry = pairing_token_expiry()
        await enrolment_store.mint_pairing_token(
            owner_id=actor_id,
            token_hash=hash_token(plaintext),
            expires_at=expiry,
        )
        return PairingTokenResponse(token=plaintext, expires_at=expiry)

    @router.post("/workers/redeem", response_model=RedeemedWorkerResponse)
    async def redeem_pairing_token_route(
        request: RedeemPairingTokenRequest,
    ) -> RedeemedWorkerResponse:
        """Exchange a pairing token for a machine credential.

        Deliberately unauthenticated: the agent on the machine holds the pairing
        token and nothing else -- that token is the proof. It is spent here, so a
        second attempt with the same token fails like any bad one.
        """
        if enrolment_store is None:
            raise HTTPException(status_code=503, detail="enrolment is not configured")
        if not looks_like_pairing_token(request.token):
            raise HTTPException(status_code=400, detail="pairing token is not usable")
        credential = generate_worker_credential()
        try:
            worker = await enrolment_store.redeem_pairing_token(
                token_hash=hash_token(request.token),
                worker_id=str(uuid4()),
                label=normalize_label(request.label),
                credential_hash=hash_token(credential),
                now=datetime.now(UTC),
            )
        except PairingTokenUnusable as exc:
            # Unknown, spent and expired answer identically on purpose.
            raise HTTPException(
                status_code=400, detail="pairing token is not usable"
            ) from exc
        except WorkerLimitReached as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "worker_limit_reached", "limit": exc.limit},
            ) from exc
        if pool_enrolment is not None:
            try:
                await pool_enrolment(worker)
            except Exception as exc:
                # The credential has not left the server yet; revoke the machine
                # rather than hand out one the pool will never admit.
                logger.exception("pool enrollment failed for a redeemed machine")
                await enrolment_store.revoke_worker(
                    owner_id=worker.owner_id, worker_id=worker.worker_id
                )
                raise HTTPException(
                    status_code=503,
                    detail="machine could not be attached; mint a new pairing code and try again",
                ) from exc
        return RedeemedWorkerResponse(
            **EnrolledWorkerResponse.from_domain(worker).model_dump(),
            credential=credential,
            tenant_id=worker.owner_id,
        )

    @router.post("/workers/rotate", response_model=RedeemedWorkerResponse)
    async def rotate_worker_credential_route(
        request: RotateCredentialRequest,
    ) -> RedeemedWorkerResponse:
        """Exchange a machine's current credential for its next one.

        Unauthenticated in the session sense, and authenticated in the only sense
        that matters here: the caller must already hold a live credential. This is
        how a machine keeps itself attached without anyone touching a token again.
        """
        if enrolment_store is None:
            raise HTTPException(status_code=503, detail="enrolment is not configured")
        if not looks_like_worker_credential(request.credential):
            raise HTTPException(status_code=400, detail="credential is not usable")
        replacement = generate_worker_credential()
        try:
            worker = await enrolment_store.rotate_credential(
                credential_hash=hash_token(request.credential),
                new_credential_hash=hash_token(replacement),
                now=datetime.now(UTC),
            )
        except CredentialRotationRefused as exc:
            # Includes the case where this credential has already been rotated
            # away: the real machine moved on, so whoever holds this copy is not
            # it. Same wording as any other refusal -- it is not an oracle.
            raise HTTPException(
                status_code=400, detail="credential is not usable"
            ) from exc
        return RedeemedWorkerResponse(
            **EnrolledWorkerResponse.from_domain(worker).model_dump(),
            credential=replacement,
            tenant_id=worker.owner_id,
        )

    @router.get("/workers", response_model=tuple[EnrolledWorkerResponse, ...])
    async def list_workers_route(
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> tuple[EnrolledWorkerResponse, ...]:
        if enrolment_store is None:
            return ()
        workers = await enrolment_store.list_workers(actor_id)
        return tuple(EnrolledWorkerResponse.from_domain(w) for w in workers)

    @router.patch("/workers/{worker_id}", response_model=EnrolledWorkerResponse)
    async def rename_worker_route(
        worker_id: str,
        request: RenameWorkerRequest,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> EnrolledWorkerResponse:
        if enrolment_store is None:
            raise HTTPException(status_code=404, detail="machine not found")
        renamed = await enrolment_store.rename_worker(
            owner_id=actor_id,
            worker_id=worker_id,
            label=normalize_label(request.label),
        )
        if renamed is None:
            raise HTTPException(status_code=404, detail="machine not found")
        return EnrolledWorkerResponse.from_domain(renamed)

    @router.delete("/workers/{worker_id}", response_model=EnrolledWorkerResponse)
    async def revoke_worker_route(
        worker_id: str,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> EnrolledWorkerResponse:
        if enrolment_store is None:
            raise HTTPException(status_code=404, detail="machine not found")
        revoked = await enrolment_store.revoke_worker(
            owner_id=actor_id, worker_id=worker_id
        )
        if revoked is None:
            raise HTTPException(status_code=404, detail="machine not found")
        return EnrolledWorkerResponse.from_domain(revoked)

    async def _provider_status(actor_id: str) -> tuple[ProviderKeyResponse, ...]:
        available = provider_accounts is not None and provider_accounts.available
        saved = {c.provider: c for c in await provider_accounts.describe(actor_id)} if available else {}
        return tuple(
            ProviderKeyResponse(
                provider=provider, available=available, configured=provider in saved,
                key_hint=saved[provider].key_hint if provider in saved else None,
                updated_at=saved[provider].updated_at if provider in saved else None,
            )
            for provider in sorted(PROVIDERS)
        )

    @router.get("/providers", response_model=tuple[ProviderKeyResponse, ...])
    async def list_provider_keys_route(
        actor_id: str = Depends(studio_actor_id),
    ) -> tuple[ProviderKeyResponse, ...]:
        return await _provider_status(actor_id)

    @router.put("/providers/{provider}", response_model=ProviderKeyResponse)
    async def save_provider_key_route(
        provider: str,
        request: SaveProviderKeyRequest,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> ProviderKeyResponse:
        """Browser-authenticated only, like minting an API key: a leaked Studio
        key must not be able to swap in someone else's billing account."""
        if provider not in PROVIDERS:
            raise HTTPException(status_code=404, detail="provider not found")
        if provider_accounts is None or not provider_accounts.available:
            raise HTTPException(status_code=503, detail="provider keys are not enabled on this server")
        try:
            await provider_accounts.save_key(actor_id, provider, request.api_key)
        except ProviderCredentialRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return next(item for item in await _provider_status(actor_id) if item.provider == provider)

    @router.delete("/providers/{provider}", response_model=ProviderKeyResponse)
    async def delete_provider_key_route(
        provider: str,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> ProviderKeyResponse:
        if provider not in PROVIDERS:
            raise HTTPException(status_code=404, detail="provider not found")
        # Deleting ciphertext needs no decryption, so it happens even when the
        # credential secret is unavailable.
        if provider_accounts is not None:
            await provider_accounts.delete_key(actor_id, provider)
        return next(item for item in await _provider_status(actor_id) if item.provider == provider)

    @router.get("/api-keys", response_model=tuple[ApiKeyResponse, ...])
    async def list_api_keys_route(
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> tuple[ApiKeyResponse, ...]:
        keys = await api_key_store.list_for_actor(actor_id)
        return tuple(ApiKeyResponse.from_domain(key) for key in keys)

    @router.delete("/api-keys/{key_id}", response_model=ApiKeyResponse)
    async def revoke_api_key_route(
        key_id: str,
        actor_id: str = Depends(studio_browser_actor_id),
    ) -> ApiKeyResponse:
        try:
            UUID(key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="api key not found") from exc
        revoked = await api_key_store.revoke(actor_id=actor_id, key_id=key_id)
        if revoked is None:
            raise HTTPException(status_code=404, detail="api key not found")
        return ApiKeyResponse.from_domain(revoked)

    @router.post("/projects", response_model=StudioProjectResponse)
    async def create_project(
        request: CreateStudioProjectRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        if request.external_subject is not None:
            _require_valid_external_subject(request.external_subject)
        try:
            stored = await service.create_project(
                request.name,
                actor_id,
                external_subject=(
                    request.external_subject.to_domain()
                    if request.external_subject
                    else None
                ),
                preset_motions=request.preset_motions,
            )
        except ProjectAlreadyBoundToSubject as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "external_subject_already_bound",
                    "project_id": exc.project_id,
                },
            ) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.get("/projects", response_model=tuple[ProjectSummaryResponse, ...])
    async def list_projects_route(
        actor_id: str = Depends(studio_actor_id),
    ) -> tuple[ProjectSummaryResponse, ...]:
        """The project home list (T-project-home): every project the caller
        owns, newest activity first, so a returning user can find what they
        made without hunting for a stale deep link."""
        summaries = await service.list_projects(actor_id)
        return tuple(ProjectSummaryResponse.from_domain(item) for item in summaries)

    @router.get("/projects/for-external-subject", response_model=StudioProjectResponse)
    async def project_for_external_subject(
        entity_kind: Annotated[str, Query(min_length=1, max_length=50)],
        entity_id: Annotated[str, Query(min_length=1, max_length=200)],
        system: Annotated[str, Query(min_length=1, max_length=50)],
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        """Deep-link lookup (Part A): the owner's project already bound to this
        external subject, or 404 when none exists yet -- the caller then falls
        back to `POST /studio/projects` with `external_subject` set."""
        stored = await service.find_project_for_external_subject(
            actor_id, system=system, entity_kind=entity_kind, entity_id=entity_id
        )
        if stored is None:
            raise HTTPException(
                status_code=404, detail="no project is bound to that external subject"
            )
        return StudioProjectResponse.from_domain(stored)

    @router.get("/projects/{project_id}", response_model=StudioProjectResponse)
    async def get_project(
        project_id: str,
        setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        stored = await service.get_project(project_id, actor_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="studio project not found")
        if _uses_versioned_setup(stored):
            _require_setup_version(setup_version)
        operations = await service.list_operations(project_id, actor_id)
        releases = await service.list_releases(project_id, actor_id)
        return StudioProjectResponse.from_domain(
            stored, operations=operations, has_release=bool(releases)
        )

    @router.delete("/projects/{project_id}", status_code=204)
    async def delete_project_route(
        project_id: str,
        request: ConfirmDeletionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> Response:
        """Permanently delete a project and every file, job, release and export."""
        try:
            await service.delete_project(project_id, actor_id, request.confirm_name)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail="studio project not found") from exc
        except ProjectDeletionNotConfirmed as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "deletion_not_confirmed", "message": str(exc)},
            ) from exc
        return Response(status_code=204)

    @router.patch("/projects/{project_id}", response_model=StudioProjectResponse)
    async def patch_project_route(
        project_id: str,
        request: PatchProjectRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        if request.external_subject is not None:
            _require_valid_external_subject(request.external_subject)
        try:
            stored = await service.update_external_subject(
                project_id,
                request.expected_revision,
                request.external_subject.to_domain()
                if request.external_subject
                else None,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.get(
        "/projects/{project_id}/gpu-telemetry",
        response_model=GpuTelemetryResponse | None,
    )
    async def get_gpu_telemetry_route(
        project_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> GpuTelemetryResponse | None:
        try:
            telemetry = await service.get_gpu_telemetry(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return GpuTelemetryResponse.from_domain(telemetry) if telemetry else None

    @router.post(
        "/projects/{project_id}/subject/uploads",
        response_model=UploadTargetResponse,
    )
    async def create_subject_upload(
        project_id: str,
        request: CreateSubjectUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.create_subject_upload(
                project_id,
                request.content_type,
                actor_id,
                request.expected_revision,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/subject/references/uploads",
        response_model=UploadTargetResponse,
    )
    async def create_subject_reference_upload(
        project_id: str,
        request: CreateSubjectUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.create_subject_reference_upload(
                project_id, request.content_type, actor_id, request.expected_revision
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/subject/generate", response_model=OperationResponse
    )
    async def generate_subject_route(
        project_id: str,
        request: GenerateSubjectRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> OperationResponse:
        try:
            job = await service.generate_subject(
                project_id,
                request.expected_revision,
                request.reference_keys,
                request.direction,
                request.request_id,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except IdempotencyConflict as exc:
            raise _request_id_reused_exception() from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except SubjectGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return OperationResponse.from_domain(job)

    @router.post(
        "/projects/{project_id}/subject/operations/{job_id}/settle",
        response_model=StudioProjectResponse,
    )
    async def settle_subject_generation_route(
        project_id: str,
        job_id: str,
        request: SettleSubjectGenerationRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.settle_subject_generation(
                project_id, job_id, request.expected_revision, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/assets/preview",
        response_model=UploadTargetResponse,
    )
    async def create_asset_preview_route(
        project_id: str,
        request: CreateAssetPreviewRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.create_asset_preview(
                project_id, request.asset_key, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return UploadTargetResponse.from_domain(target)

    @router.get("/projects/{project_id}/files")
    async def project_file_route(
        project_id: str,
        http_request: Request,
        key: str = Query(min_length=1, max_length=1024),
        download: str | None = Query(default=None, max_length=255),
        actor_id: str = Depends(studio_actor_id),
    ) -> Response:
        """Stream one of the caller's own project files. Every request is
        authorized, so a copied link is useless to anyone else."""
        try:
            await service.authorize_project_file(project_id, key, actor_id)
        except (ProjectNotFound, SubjectUploadError) as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        return await asset_response(service.assets, key, http_request, filename=download)

    @router.post(
        "/projects/{project_id}/assets/download",
        response_model=UploadTargetResponse,
    )
    async def create_asset_download_route(
        project_id: str,
        request: CreateAssetPreviewRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.create_asset_download(
                project_id, request.asset_key, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/subject/settle",
        response_model=StudioProjectResponse,
    )
    async def settle_subject_upload(
        project_id: str,
        request: SettleSubjectUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.settle_subject_upload(
                project_id,
                request.expected_revision,
                request.asset_key,
                actor_id,
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RevisionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "expected revision is stale",
                    "current_revision": exc.current_revision,
                },
            ) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/subject/approve",
        response_model=StudioProjectResponse,
    )
    async def approve_subject_route(
        project_id: str,
        request: ApproveSubjectRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.approve_subject(
                project_id,
                request.expected_revision,
                actor_id,
                request.waived_finding_ids,
                request.waiver_reason,
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RevisionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "expected revision is stale",
                    "current_revision": exc.current_revision,
                },
            ) from exc
        except SubjectWaiverUnknown as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "subject_waiver_unknown",
                    "message": str(exc),
                    "finding_ids": list(exc.finding_ids),
                },
            ) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/subject/revisions/{revision_id}/select",
        response_model=StudioProjectResponse,
    )
    async def select_subject_revision_route(
        project_id: str,
        revision_id: str,
        request: SelectSubjectRevisionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.select_subject_revision(
                project_id,
                request.expected_revision,
                revision_id,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except SubjectRevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/subject/revisions/{revision_id}/reframe",
        response_model=StudioProjectResponse,
    )
    async def reframe_subject_revision_route(
        project_id: str,
        revision_id: str,
        request: ReframeSubjectRevisionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.edit_subject_revision(
                project_id,
                request.expected_revision,
                revision_id,
                request.edit,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except SubjectRevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/subject/revisions/{revision_id}/reject",
        response_model=StudioProjectResponse,
    )
    async def reject_subject_revision_route(
        project_id: str,
        revision_id: str,
        request: SelectSubjectRevisionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.reject_subject_revision(
                project_id,
                request.expected_revision,
                revision_id,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except SubjectRevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/subject/revisions/{revision_id}/restore",
        response_model=StudioProjectResponse,
    )
    async def restore_subject_revision_route(
        project_id: str,
        revision_id: str,
        request: SelectSubjectRevisionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.restore_subject_revision(
                project_id,
                request.expected_revision,
                revision_id,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except SubjectRevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/composition/compose",
        response_model=OperationResponse,
    )
    async def compose_frame_route(
        project_id: str,
        request: ComposeFrameRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> OperationResponse:
        try:
            job = await service.compose_opening_frame(
                project_id,
                request.expected_revision,
                request.prompt,
                request.composition_model,
                actor_id,
                request.request_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except IdempotencyConflict as exc:
            raise _request_id_reused_exception() from exc
        except (CompositionRequestBlocked, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CompositionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return OperationResponse.from_domain(job)

    @router.post(
        "/projects/{project_id}/composition/operations/{job_id}/settle",
        response_model=StudioProjectResponse,
    )
    async def settle_composition_route(
        project_id: str,
        job_id: str,
        request: SettleCompositionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.settle_composition(
                project_id, job_id, request.expected_revision, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (CompositionOperationNotReady, CompositionRequestBlocked) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MotionAssetError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "message": str(exc)}
            ) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/composition/approve",
        response_model=StudioProjectResponse,
    )
    async def approve_composition_route(
        project_id: str,
        request: ApproveCompositionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.approve_composition(
                project_id, request.expected_revision, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except CompositionApprovalBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/composition/regenerate",
        response_model=OperationResponse,
    )
    async def regenerate_composition_route(
        project_id: str,
        request: RegenerateCompositionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> OperationResponse:
        try:
            job = await service.regenerate_composition(
                project_id,
                request.expected_revision,
                request.prompt,
                request.composition_model,
                actor_id,
                request.request_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except IdempotencyConflict as exc:
            raise _request_id_reused_exception() from exc
        except (CompositionRequestBlocked, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CompositionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return OperationResponse.from_domain(job)

    @router.get("/presets", response_model=tuple[PresetResponse, ...])
    async def list_presets_route() -> tuple[PresetResponse, ...]:
        return tuple(
            PresetResponse.from_domain(PRESET_DEFINITIONS[preset])
            for preset in MotionPreset
        )

    @router.get("/targets", response_model=tuple[TargetResponse, ...])
    async def list_targets_route() -> tuple[TargetResponse, ...]:
        return tuple(
            TargetResponse.from_adapter(
                adapter, delivery_configured=target_registry.delivery_configured(system)
            )
            for system, adapter in (
                (system, target_registry.get(system))
                for system in target_registry.systems()
            )
            if adapter is not None
        )

    @router.get("/setup-capabilities")
    async def setup_capabilities(actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        return {"schema_versions": [1], "current_schema_version": 1,
                "conventions": ["unassigned", "single_view", "side_view", "isometric"],
                "partial_matrix": True, "atomic_reconciliation": True,
                "reference_binding": "verified_revision_id",
                "version_header": "X-Studio-Setup-Version", "reference_staging": True}

    @router.post("/projects/{project_id}/setup/dispatches/{operation_id}/abandon", response_model=StudioProjectResponse)
    async def abandon_setup_dispatch(project_id: str, operation_id: str, request: AbandonDispatchRequest,
            setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
            actor_id: str = Depends(studio_actor_id)) -> StudioProjectResponse:
        _require_setup_version(setup_version)
        try:
            stored = await service.abandon_motion_dispatch(project_id, request.expected_revision,
                operation_id, actor_id, confirm_dispatch_stopped=request.confirm_dispatch_stopped)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (SetupConflict, SetupBlocked, SetupConfirmationRequired) as exc:
            raise _setup_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post("/projects/{project_id}/setup/references")
    async def stage_setup_reference(project_id: str, request: StageSetupReferenceRequest,
            setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
            actor_id: str = Depends(studio_actor_id)) -> Any:
        _require_setup_version(setup_version)
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="invalid base64 image") from exc
        if not content or len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="image must contain at most 10 MB")
        try:
            return await service.stage_setup_reference(project_id, content, request.content_type, actor_id)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except (SetupConflict, SetupBlocked, SetupConfirmationRequired) as exc:
            raise _setup_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/setup")
    async def get_setup(project_id: str,
                        setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
                        actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        _require_setup_version(setup_version)
        try:
            revision, setup = await service.get_project_setup_snapshot(project_id, actor_id)
        except ProjectNotFound as exc:
            raise _project_mutation_exception(exc) from exc
        return {"revision": revision, "setup": setup.model_dump(mode="json")}

    @router.post("/projects/{project_id}/setup/preview", response_model=ProjectSetupPlan)
    async def preview_setup(project_id: str, request: PreviewSetupRequest,
                            setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
                            actor_id: str = Depends(studio_actor_id)) -> ProjectSetupPlan:
        _require_setup_version(setup_version)
        try:
            return await service.preview_project_setup(project_id, request.expected_revision, request.desired_setup, actor_id)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (SetupConflict, SetupBlocked, SetupConfirmationRequired) as exc:
            raise _setup_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/setup/apply", response_model=StudioProjectResponse)
    async def apply_setup(project_id: str, request: ApplySetupRequest,
                          setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
                          actor_id: str = Depends(studio_actor_id)) -> StudioProjectResponse:
        _require_setup_version(setup_version)
        try:
            stored = await service.apply_project_setup(project_id, request.expected_revision,
                request.plan_token, request.confirmed_impacts, actor_id)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (SetupConflict, SetupBlocked, SetupConfirmationRequired) as exc:
            raise _setup_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    async def require_known_project_setup(project_id: str,
            setup_version: str | None = Header(default=None, alias="X-Studio-Setup-Version"),
            actor_id: str = Depends(studio_actor_id)) -> None:
        stored = await service.get_project(project_id, actor_id)
        if stored is not None and _uses_versioned_setup(stored):
            _require_setup_version(setup_version)

    @router.put("/projects/{project_id}/sprite-layout", response_model=StudioProjectResponse, dependencies=[Depends(require_known_project_setup)])
    async def set_sprite_layout_route(project_id: str, request: SetSpriteLayoutRequest,
                                      actor_id: str = Depends(studio_actor_id)) -> StudioProjectResponse:
        try:
            stored = await service.set_sprite_layout(project_id, request.expected_revision, request.sprite_layout, actor_id)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.put("/projects/{project_id}/views/{view_id}", response_model=StudioProjectResponse, dependencies=[Depends(require_known_project_setup)])
    async def set_character_view_route(project_id: str, view_id: ViewId, request: SetCharacterViewRequest,
                                       actor_id: str = Depends(studio_actor_id)) -> StudioProjectResponse:
        try:
            stored = await service.set_character_view(
                project_id, request.expected_revision, view_id, actor_id, name=request.name,
                reference=(request.reference.key, request.reference.sha256) if request.reference else None,
                replace_reference="reference" in request.model_fields_set,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post("/projects/{project_id}/views/{view_id}/reference/uploads", response_model=UploadTargetResponse)
    async def create_view_reference_upload_route(project_id: str, view_id: ViewId, request: CreateSubjectUploadRequest,
                                                actor_id: str = Depends(studio_actor_id)) -> UploadTargetResponse:
        if request.expected_revision is None:
            raise HTTPException(status_code=422, detail="expected_revision is required")
        try:
            target = await service.create_view_reference_upload(project_id, request.expected_revision, view_id, request.content_type, actor_id)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UploadTargetResponse.from_domain(target)

    @router.post("/projects/{project_id}/motions", response_model=StudioProjectResponse, dependencies=[Depends(require_known_project_setup)])
    async def add_motion_route(
        project_id: str,
        request: AddMotionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.add_motion(
                project_id,
                request.expected_revision,
                request.to_specification(),
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except MotionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.patch(
        "/projects/{project_id}/motions/{motion_id}",
        response_model=StudioProjectResponse,
    )
    async def update_motion_route(
        project_id: str,
        motion_id: str,
        request: UpdateMotionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        if request.motion_id != motion_id:
            raise HTTPException(
                status_code=422, detail="motion_id in the body must match the path"
            )
        try:
            stored = await service.update_motion(
                project_id,
                request.expected_revision,
                motion_id,
                request.to_specification(),
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/references/uploads",
        response_model=UploadTargetResponse,
    )
    async def create_motion_reference_upload_route(
        project_id: str,
        motion_id: str,
        request: CreateMotionReferenceUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        del (
            motion_id
        )  # the upload namespace is project-scoped; the motion binds it via PATCH
        try:
            target = await service.create_motion_reference_upload(
                project_id,
                request.content_type,
                actor_id,
                request.expected_revision,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except SubjectUploadError as exc:
            raise _subject_upload_exception(exc)
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/motions/batch", response_model=StudioProjectResponse
    )
    async def add_motions_route(
        project_id: str,
        request: AddMotionsBatchRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        specifications = tuple(
            definition.to_specification() for definition in request.motions
        )
        try:
            stored = await service.add_motions(
                project_id, request.expected_revision, specifications, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except DuplicateMotionIds as exc:
            raise HTTPException(
                status_code=409,
                detail={"message": str(exc), "motion_ids": list(exc.motion_ids)},
            ) from exc
        except MotionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/motions/generate-batch",
        response_model=GenerateMotionsResponse,
    )
    async def generate_motions_route(
        project_id: str,
        request: GenerateMotionsRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> GenerateMotionsResponse:
        try:
            queued, failed = await service.generate_motions(
                project_id,
                request.expected_revision,
                request.motion_ids,
                actor_id,
                request.request_id,
                request.settings,
                request.shared_starting_pose,
                request.actions,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except (MotionRequestBlocked, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MotionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return GenerateMotionsResponse(
            queued=tuple(
                QueuedMotionResponse(
                    motion_id=item.motion_id,
                    operation=OperationResponse.from_domain(item.operation),
                )
                for item in queued
            ),
            failed=tuple(
                FailedMotionResponse(
                    motion_id=item.motion_id, code=item.code, message=item.message
                )
                for item in failed
            ),
        )

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/generate",
        response_model=OperationResponse,
    )
    async def generate_motion_route(
        project_id: str,
        motion_id: str,
        request: GenerateMotionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> OperationResponse:
        try:
            job = await service.generate_motion(
                project_id,
                request.expected_revision,
                motion_id,
                actor_id,
                request.request_id,
                request.settings,
                request.action,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except (MotionRequestBlocked, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MotionGenerationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return OperationResponse.from_domain(job)

    @router.post("/projects/{project_id}/turnarounds")
    async def start_turnaround_route(
        project_id: str, request: StartTurnaroundRequest, actor_id: str = Depends(studio_actor_id),
    ) -> dict[str, Any]:
        from .models import TurnaroundEquipment
        from .turnarounds import TurnaroundUnavailable
        try:
            equipment = tuple(TurnaroundEquipment(**row.model_dump()) for row in request.equipment)
            take, job = await service.start_turnaround(
                project_id, request.expected_revision, request.source_view_id, request.source_revision_id,
                equipment, actor_id, request.request_id, request.settings)
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except (TurnaroundUnavailable, MotionGenerationUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"turnaround_id": take.turnaround_id, "operation": OperationResponse.from_domain(job).model_dump(mode="json")}

    @router.get("/projects/{project_id}/turnarounds")
    async def list_turnarounds_route(project_id: str, actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        try:
            return {"turnarounds": await service.list_turnarounds(project_id, actor_id)}
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/turnarounds/{turnaround_id}")
    async def get_turnaround_route(project_id: str, turnaround_id: str,
                                   actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        try:
            return await service.get_turnaround(project_id, turnaround_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/turnarounds/{turnaround_id}/frames")
    async def turnaround_frames_route(
        project_id: str, turnaround_id: str,
        start: int = Query(default=0, ge=0), limit: int = Query(default=12, ge=1, le=32),
        stride: int = Query(default=1, ge=1, le=512),
        actor_id: str = Depends(studio_actor_id),
    ) -> dict:
        try:
            result = await service.turnaround_frames(project_id, turnaround_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionOperationNotReady, MotionAssetError, MotionDerivationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**result, "frames": result["frames"][start:start + limit * stride:stride]}

    def _draft_errors(call):
        async def handled(*args, **kwargs):
            from .view_drafts import DraftUnavailable
            try:
                return await call(*args, **kwargs)
            except ProjectNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except RevisionConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except OwnerLimitExceeded as exc:
                raise _owner_limit_exception(exc) from exc
            except AssetStorageUnavailable as exc:
                raise HTTPException(status_code=503, detail={
                    "code": "draft_storage_unavailable",
                    "message": "draft storage is temporarily unavailable; try again"}) from exc
            except (DraftUnavailable, MotionOperationNotReady, MotionAssetError, MotionDerivationError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return handled

    @router.post("/projects/{project_id}/view-drafts")
    async def create_view_draft_route(project_id: str, request: CreateViewDraftRequest,
                                      actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        draft = await _draft_errors(service.create_view_draft)(
            project_id, request.expected_revision, request.turnaround_id, request.pick_revision,
            request.heading, request.standing, request.request_id, actor_id)
        return {"draft_id": draft["draft_id"], "draft": draft}

    @router.get("/projects/{project_id}/view-drafts")
    async def list_view_drafts_route(project_id: str, actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        return {"drafts": await _draft_errors(service.list_view_drafts)(project_id, actor_id)}

    @router.post("/projects/{project_id}/view-drafts/{draft_id}/check")
    async def check_view_draft_route(project_id: str, draft_id: str, request: CheckViewDraftRequest,
                                     actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        return await _draft_errors(service.check_view_draft)(
            project_id, draft_id, request.expected_revision, request.facing, request.equipment,
            request.identity, actor_id)

    @router.post("/projects/{project_id}/view-drafts/{draft_id}/discard")
    async def discard_view_draft_route(project_id: str, draft_id: str, request: DiscardViewDraftRequest,
                                       actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        return await _draft_errors(service.discard_view_draft)(project_id, draft_id, request.expected_revision,
                                                               actor_id)

    @router.put("/projects/{project_id}/turnarounds/{turnaround_id}/picks")
    async def save_turnaround_picks_route(project_id: str, turnaround_id: str, request: SaveTurnaroundPicksRequest,
                                          actor_id: str = Depends(studio_actor_id)) -> dict[str, Any]:
        from .models import TurnaroundPick
        from .turnaround_picks import PickSetRejected
        from .turnarounds import PickRevisionConflict

        picks = tuple(TurnaroundPick(heading=heading, **entry.model_dump()) for heading, entry in request.picks.items())
        try:
            return await service.save_turnaround_picks(project_id, turnaround_id, request.expected_pick_revision,
                                                       picks, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PickRevisionConflict as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "pick_revision": exc.current}) from exc
        except PickSetRejected as exc:
            raise HTTPException(status_code=422, detail={"message": str(exc), "problems": exc.problems}) from exc
        except (MotionOperationNotReady, MotionAssetError, MotionDerivationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/projects/{project_id}/turnarounds/{turnaround_id}/cancel", response_model=OperationResponse)
    async def cancel_turnaround_route(project_id: str, turnaround_id: str,
                                      actor_id: str = Depends(studio_actor_id)) -> OperationResponse:
        try:
            return OperationResponse.from_domain(await service.cancel_turnaround(project_id, turnaround_id, actor_id))
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/operations/{job_id}/cancel",
        response_model=OperationResponse,
    )
    async def cancel_motion_operation_route(
        project_id: str,
        motion_id: str,
        job_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> OperationResponse:
        try:
            job = await service.cancel_motion_operation(
                project_id, motion_id, job_id, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OperationAlreadyTerminal as exc:
            raise HTTPException(
                status_code=409,
                detail=f"operation is already {exc.state.value}",
            ) from exc
        return OperationResponse.from_domain(job)

    @router.post(
        "/projects/{project_id}/operations/cancel-all",
        response_model=CancelAllOperationsResponse,
    )
    async def cancel_all_operations_route(
        project_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> CancelAllOperationsResponse:
        try:
            result = await service.cancel_all_operations(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return CancelAllOperationsResponse(
            cancelled=result.cancelled,
            skipped=tuple(
                SkippedOperationResponse(job_id=item.job_id, state=item.state)
                for item in result.skipped
            ),
        )

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/select",
        response_model=StudioProjectResponse,
    )
    async def select_motion_candidate_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        request: SelectMotionCandidateRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.select_candidate(
                project_id, request.expected_revision, motion_id, candidate_id, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/revisions",
        response_model=StudioProjectResponse,
    )
    async def create_motion_revision_route(
        project_id: str,
        motion_id: str,
        request: CreateMotionRevisionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.create_motion_revision(
                project_id,
                request.expected_revision,
                motion_id,
                request.source_candidate_id,
                actor_id,
                ordered_frames=request.ordered_frames,
                frame_duration_ms=request.frame_duration_ms,
                loop_policy=request.loop_policy,
                flip_horizontal=request.flip_horizontal,
                target_frame_count=request.target_frame_count,
                cell_size=request.cell_size,
                normalization=request.normalization,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (MotionDerivationError, MotionAssetError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/derive-alpha",
        response_model=StudioProjectResponse,
    )
    async def derive_alpha_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        request: DeriveAlphaRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.derive_alpha(
                project_id,
                request.expected_revision,
                motion_id,
                candidate_id,
                actor_id,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except MotionDerivationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except MotionAssetError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post("/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/body-calibration")
    async def candidate_body_calibration_route(
        project_id: str, motion_id: str, candidate_id: str, request: BodyCalibrationRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> dict:
        from magic_video_editor.studio.frame_analysis import calibrate_body
        try:
            await service.require_same_view_calibration(project_id, motion_id, request.reference_motion_id, actor_id)
            source = await service.analyze_candidate_frames(project_id, motion_id, candidate_id, actor_id)
            reference = await service.analyze_candidate_frames(
                project_id, request.reference_motion_id, request.reference_candidate_id, actor_id)
            return await asyncio.to_thread(calibrate_body, source, reference, request.source_frame, request.reference_frame)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionAssetError, MotionDerivationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/frames")
    async def candidate_frames_route(
        project_id: str, motion_id: str, candidate_id: str,
        start: int = Query(default=0, ge=0), limit: int = Query(default=12, ge=1, le=32),
        stride: int = Query(default=1, ge=1, le=512),
        actor_id: str = Depends(studio_actor_id),
    ) -> dict:
        try:
            result = await service.analyze_candidate_frames(project_id, motion_id, candidate_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionAssetError, MotionDerivationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**result, "frames": result["frames"][start:start + limit * stride:stride]}

    @router.post("/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/loop-suggestion")
    async def candidate_loop_suggestion_route(
        project_id: str, motion_id: str, candidate_id: str, request: LoopSuggestionRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> dict:
        try:
            return await service.suggest_candidate_loop(
                project_id, motion_id, candidate_id, actor_id,
                mode=request.mode, target_frame_count=request.target_frame_count,
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionAssetError, MotionDerivationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get(
        "/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/sheet.png",
        response_model=UploadTargetResponse,
    )
    async def candidate_sheet_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.download_candidate_sheet(
                project_id, motion_id, candidate_id, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionAssetError, MotionDerivationError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UploadTargetResponse.from_domain(target)

    async def _candidate_preview_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        fmt: str,
        actor_id: str,
    ) -> UploadTargetResponse:
        try:
            target = await service.download_candidate_preview(
                project_id, motion_id, candidate_id, fmt, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionAssetError, MotionDerivationError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UploadTargetResponse.from_domain(target)

    @router.get(
        "/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/preview.webm",
        response_model=UploadTargetResponse,
    )
    async def candidate_preview_webm_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        return await _candidate_preview_route(
            project_id, motion_id, candidate_id, "webm", actor_id
        )

    @router.get(
        "/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}/preview.gif",
        response_model=UploadTargetResponse,
    )
    async def candidate_preview_gif_route(
        project_id: str,
        motion_id: str,
        candidate_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        return await _candidate_preview_route(
            project_id, motion_id, candidate_id, "gif", actor_id
        )

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/candidates/uploads",
        response_model=UploadTargetResponse,
    )
    async def create_candidate_upload_route(
        project_id: str,
        motion_id: str,
        request: CreateCandidateUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> UploadTargetResponse:
        try:
            target = await service.create_candidate_upload(
                project_id,
                motion_id,
                request.content_type,
                actor_id,
                request.expected_revision,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except OwnerLimitExceeded as exc:
            raise _owner_limit_exception(exc) from exc
        except MotionUploadValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/candidates/settle-upload",
        response_model=StudioProjectResponse,
    )
    async def settle_candidate_upload_route(
        project_id: str,
        motion_id: str,
        request: SettleCandidateUploadRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.settle_candidate_upload(
                project_id,
                request.expected_revision,
                motion_id,
                request.asset_key,
                actor_id,
                request.frame_count,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (MotionUploadValidationError, MotionAssetError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.get(
        "/projects/{project_id}/operations",
        response_model=tuple[OperationResponse, ...],
    )
    async def list_operations_route(
        project_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> tuple[OperationResponse, ...]:
        try:
            operations = await service.list_operations(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return tuple(OperationResponse.from_listing(item) for item in operations)

    @router.get(
        "/projects/{project_id}/activity",
        response_model=ActivityPageResponse,
    )
    async def list_activity_route(
        project_id: str,
        cursor: str | None = None,
        limit: int = Query(default=DEFAULT_ACTIVITY_PAGE_LIMIT, ge=1, le=100),
        motion_id: str | None = None,
        actor_id: str = Depends(studio_actor_id),
    ) -> ActivityPageResponse:
        try:
            page = await service.list_activity_operations(
                project_id,
                actor_id,
                cursor=cursor,
                limit=limit,
                motion_id=motion_id,
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidActivityCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_cursor", "message": str(exc)},
            ) from exc
        return ActivityPageResponse.from_domain(page)

    @router.post(
        "/projects/{project_id}/activity/reconcile",
        response_model=ActivityPageResponse,
    )
    async def reconcile_activity_route(
        project_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> ActivityPageResponse:
        try:
            await service.reconcile_project_operations(project_id, actor_id)
            page = await service.list_activity_operations(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ActivityPageResponse.from_domain(page)

    @router.get(
        "/projects/{project_id}/history",
        response_model=tuple[HistoryEntry, ...],
    )
    async def project_history_route(
        project_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> tuple[HistoryEntry, ...]:
        try:
            return await service.list_project_history(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post(
        "/projects/{project_id}/motions/{motion_id}/operations/{job_id}/settle",
        response_model=StudioProjectResponse,
    )
    async def settle_motion_operation_route(
        project_id: str,
        motion_id: str,
        job_id: str,
        request: SettleMotionOperationRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.settle_project_motion_candidate(
                project_id, motion_id, job_id, request.expected_revision, actor_id
            )
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MotionOperationNotReady, MotionLineageConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RevisionConflict as exc:
            raise _project_mutation_exception(exc) from exc
        except MotionAssetError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/quality/set",
        response_model=StudioProjectResponse,
    )
    async def assess_set_quality_route(
        project_id: str,
        request: AssessSetRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.assess_set_quality(
                project_id, request.expected_revision, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post(
        "/projects/{project_id}/set/approve",
        response_model=StudioProjectResponse,
    )
    async def approve_set_route(
        project_id: str,
        request: ApproveSetRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> StudioProjectResponse:
        try:
            stored = await service.approve_set(
                project_id,
                request.expected_revision,
                actor_id,
                request.waived_finding_ids,
                request.waiver_reason,
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StudioProjectResponse.from_domain(stored)

    @router.post("/projects/{project_id}/releases", response_model=ReleaseResponse)
    async def create_release_route(
        project_id: str,
        request: CreateReleaseRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> ReleaseResponse:
        try:
            release = await service.create_release(
                project_id, request.expected_revision, request.name, actor_id
            )
            deliveries = await service.list_release_deliveries(
                release.release_id, actor_id
            )
        except (ProjectNotFound, RevisionConflict) as exc:
            raise _project_mutation_exception(exc) from exc
        except (ReleaseNotReady, DuplicateReleaseName) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ReleaseResponse.from_domain(release, deliveries=deliveries)

    @router.get(
        "/projects/{project_id}/releases",
        response_model=tuple[ReleaseResponse, ...],
    )
    async def list_releases_route(
        project_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> tuple[ReleaseResponse, ...]:
        try:
            releases = await service.list_releases(project_id, actor_id)
            deliveries = {
                release.release_id: await service.list_release_deliveries(
                    release.release_id, actor_id
                )
                for release in releases
            }
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return tuple(
            ReleaseResponse.from_domain(
                item, deliveries=deliveries.get(item.release_id, ())
            )
            for item in releases
        )

    @router.get(
        "/releases/{release_id}/exports",
        response_model=tuple[ExportResponse, ...],
    )
    async def list_release_exports_route(
        release_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> tuple[ExportResponse, ...]:
        try:
            records = await service.list_release_exports(release_id, actor_id)
        except ReleaseNotFound as exc:
            raise HTTPException(status_code=404, detail="release not found") from exc
        return tuple(ExportResponse.from_domain(record) for record in records)

    @router.post(
        "/releases/{release_id}/deliveries/{target}",
        response_model=ReleaseDeliveryResponse,
    )
    async def retry_delivery_route(
        release_id: str, target: str, actor_id: str = Depends(studio_actor_id)
    ) -> ReleaseDeliveryResponse:
        try:
            delivery = await service.retry_delivery(release_id, target, actor_id)
        except ReleaseNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeliveryNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ReleaseDeliveryResponse.from_domain(delivery)

    @router.post("/releases/{release_id}/exports", response_model=ExportResponse)
    async def create_export_route(
        release_id: str,
        request: CreateExportRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> ExportResponse:
        try:
            record = await service.create_export(
                release_id, request.profile, actor_id, request.expected_revision
            )
        except ReleaseNotFound as exc:
            raise HTTPException(status_code=404, detail="release not found") from exc
        except RevisionConflict as exc:
            raise _project_mutation_exception(exc) from exc
        except ReleaseIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "release_integrity_error",
                    "message": "stored release failed integrity verification",
                },
            ) from exc
        except ReleaseAssetInvalid as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ImmutableExportCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "immutable_export_collision",
                    "message": "immutable export integrity conflict",
                },
            ) from exc
        except ExportStorageUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "export_storage_unavailable",
                    "message": "export storage is temporarily unavailable",
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ExportResponse.from_domain(record)

    @router.get("/exports/{export_id}", response_model=ExportResponse)
    async def get_export_route(
        export_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> ExportResponse:
        try:
            record = await service.get_export(export_id, actor_id)
        except ExportNotFound as exc:
            raise HTTPException(status_code=404, detail="export not found") from exc
        except _EXPORT_READ_ERRORS as exc:
            raise _export_read_exception(exc) from exc
        return ExportResponse.from_domain(record)

    @router.get("/exports/{export_id}/file")
    async def export_file_route(
        export_id: str, http_request: Request, actor_id: str = Depends(studio_actor_id)
    ) -> Response:
        """Stream an export to its owner; the link works for nobody else."""
        try:
            key, filename = await service.export_file(export_id, actor_id)
            return await asset_response(
                service.assets, key, http_request, filename=filename
            )
        except ExportNotFound as exc:
            raise HTTPException(status_code=404, detail="export not found") from exc
        except _EXPORT_READ_ERRORS as exc:
            raise _export_read_exception(exc) from exc

    @router.get("/exports/{export_id}/download", response_model=UploadTargetResponse)
    async def create_export_download_route(
        export_id: str, actor_id: str = Depends(studio_actor_id)
    ) -> UploadTargetResponse:
        try:
            target = await service.create_export_download(export_id, actor_id)
        except ExportNotFound as exc:
            raise HTTPException(status_code=404, detail="export not found") from exc
        except _EXPORT_READ_ERRORS as exc:
            raise _export_read_exception(exc) from exc
        return UploadTargetResponse.from_domain(target)

    @router.post(
        "/projects/{project_id}/repair",
        response_model=ProjectRepairReportResponse,
    )
    async def repair_project_route(
        project_id: str,
        actor_id: str = Depends(studio_actor_id),
    ) -> ProjectRepairReportResponse:
        """Restore a project whose current manifest revision cannot be read
        (MAGIC-VIDEO-EDITOR-BACKEND-5) from the newest earlier revision that
        still validates. 404s the same way a stranger's `GET` would rather
        than leaking whether the project exists."""
        try:
            report = await service.repair_project(project_id, actor_id)
        except ProjectNotFound as exc:
            raise HTTPException(
                status_code=404, detail="studio project not found"
            ) from exc
        except RevisionConflict as exc:
            # Another write landed while the repair walked the revisions.
            raise _project_mutation_exception(exc) from exc
        return ProjectRepairReportResponse.from_domain(report)

    router.include_router(create_worker_package_router(enrolment_store))
    return router


async def studio_actor_id(
    request: Request,
    response: Response,
    actor_id: Annotated[str | None, Header(alias="X-Studio-Actor-Id")] = None,
    iap_actor_id: Annotated[
        str | None, Header(alias="X-Goog-Authenticated-User-Email")
    ] = None,
    iap_assertion: Annotated[
        str | None, Header(alias="X-Goog-IAP-JWT-Assertion")
    ] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    """The actor every `/studio/*` route authorizes against: an API key's owner when
    a bearer key is presented, otherwise the browser identity (session cookie, then
    IAP, then the local dev gateway header)."""
    token = parse_bearer_token(authorization)
    if token is not None:
        store = api_key_store_from_app(request.app)
        if store is None:
            raise HTTPException(
                status_code=401, detail="API key authentication is not configured"
            )
        try:
            # An API key is not a cookie, so it is exempt from the CSRF check.
            resolved = await resolve_api_key_actor(store, token)
        except ApiKeyRejected as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        # A key acts for its user only while that user may still sign in, so access
        # taken back also stops a key minted while it was being taken back.
        auth = auth_service_from_app(request.app)
        if auth is not None:
            user = await user_for_actor(auth.store, resolved)
            if user is not None and not await auth.may_sign_in(user.email):
                raise HTTPException(status_code=401, detail="this account no longer has access")
        request.state.studio_identity = ResolvedIdentity(actor_id=resolved)
        return resolved
    return await studio_browser_actor_id(
        request, response, actor_id, iap_actor_id, iap_assertion
    )


async def studio_browser_actor_id(
    request: Request,
    response: Response,
    actor_id: Annotated[str | None, Header(alias="X-Studio-Actor-Id")] = None,
    iap_actor_id: Annotated[
        str | None, Header(alias="X-Goog-Authenticated-User-Email")
    ] = None,
    iap_assertion: Annotated[
        str | None, Header(alias="X-Goog-IAP-JWT-Assertion")
    ] = None,
) -> str:
    """The browser identity only -- a session cookie, then IAP. API-key management
    routes depend on this so a key can never mint or revoke keys."""
    identity = await _resolve_browser_identity(
        request, response, actor_id, iap_assertion
    )
    request.state.studio_identity = identity
    return identity.actor_id


async def _resolve_browser_identity(
    request: Request,
    response: Response,
    actor_id: str | None,
    iap_assertion: str | None,
) -> ResolvedIdentity:
    trusted_source = os.getenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", "gateway")
    session_identity = await _session_cookie_identity(request, response)
    if session_identity is not None:
        _require_same_origin(request)
        return session_identity
    if trusted_source == TRUSTED_ACTOR_SOURCE_IAP:
        return await _iap_identity(request, response, iap_assertion, required=True)
    if trusted_source == TRUSTED_ACTOR_SOURCE_SESSION:
        audience = os.getenv("MAGIC_VIDEO_EDITOR_IAP_AUDIENCE")
        if not audience or audience == IAP_BOOTSTRAP_AUDIENCE:
            raise HTTPException(status_code=401, detail="sign-in required")
        return await _iap_identity(request, response, iap_assertion, required=True)
    return ResolvedIdentity(actor_id=_validated_gateway_actor(actor_id))


async def _session_cookie_identity(
    request: Request, response: Response
) -> ResolvedIdentity | None:
    """The signed-in user behind `mvs_session`, or `None`. A cookie that no longer
    resolves is cleared so the browser stops presenting it."""
    auth = auth_service_from_app(request.app)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if auth is None or not cookie:
        return None
    resolved = await auth.resolve_session(cookie)
    if resolved is None:
        clear_session_cookie(response)
        return None
    user, sign_in_method = resolved
    return ResolvedIdentity(
        actor_id=user.actor_id,
        email=user.email,
        display_name=user.display_name,
        sign_in_method=sign_in_method,
    )


def _require_same_origin(request: Request) -> None:
    """CSRF: a cookie-authenticated state change must come from our own origin.

    `SameSite=Lax` already blocks cross-site POSTs in current browsers; this is the
    server-side half that does not depend on the browser getting it right.
    """
    if request.method in CSRF_SAFE_METHODS:
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    if source is None or not host or urlparse(source).netloc != host:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "csrf_origin_mismatch",
                "message": "cookie-authenticated writes must come from this origin",
            },
        )


async def _iap_identity(
    request: Request,
    response: Response,
    iap_assertion: str | None,
    *,
    required: bool,
) -> ResolvedIdentity:
    audience = os.getenv("MAGIC_VIDEO_EDITOR_IAP_AUDIENCE")
    if not audience or audience == IAP_BOOTSTRAP_AUDIENCE:
        raise HTTPException(status_code=503, detail="IAP audience is not configured")
    if not iap_assertion:
        if not required:
            raise HTTPException(status_code=401, detail="sign-in required")
        raise HTTPException(status_code=401, detail="verified IAP assertion required")
    claims = await _iap_claims(iap_assertion, audience)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=401, detail="invalid IAP subject")
    email = claims.get("email")
    return await _adopt_iap_identity(
        request, response, subject=subject.strip(), email=email
    )


async def _adopt_iap_identity(
    request: Request,
    response: Response,
    *,
    subject: str,
    email: object,
) -> ResolvedIdentity:
    """Turn a live IAP assertion into a studio session, so the day IAP comes off
    nobody is signed out."""
    auth = auth_service_from_app(request.app)
    if auth is None or not isinstance(email, str) or not email.strip():
        return ResolvedIdentity(actor_id=f"iap:{subject}")
    user = await auth.provision_from_iap(subject=subject, email=email.strip())
    token = await auth.issue_session(
        user,
        sign_in_method=SignInMethod.IAP,
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, token)
    return ResolvedIdentity(
        actor_id=user.actor_id,
        email=user.email,
        display_name=user.display_name,
        sign_in_method=SignInMethod.IAP,
    )


async def _iap_claims(iap_assertion: str, audience: str) -> dict[str, object]:
    try:
        async with _iap_verification_semaphore:
            claims = await asyncio.wait_for(
                asyncio.to_thread(_verify_iap_assertion, iap_assertion, audience),
                timeout=IAP_VERIFY_TIMEOUT_SECONDS,
            )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="IAP identity verification timed out"
        ) from exc
    except TransportError as exc:
        raise HTTPException(
            status_code=503, detail="IAP identity verification is unavailable"
        ) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=401, detail="invalid IAP assertion") from exc
    if claims.get("iss") != IAP_ISSUER:
        raise HTTPException(status_code=401, detail="invalid IAP assertion issuer")
    return claims


def _validated_gateway_actor(actor_id: str | None) -> str:
    if actor_id is None or not actor_id.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "studio_actor_id_required",
                "message": "X-Studio-Actor-Id header is required",
            },
        )
    normalized = actor_id.strip()
    if len(normalized) > MAX_STUDIO_ACTOR_ID_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "studio_actor_id_invalid",
                "message": (
                    "X-Studio-Actor-Id header must be "
                    f"{MAX_STUDIO_ACTOR_ID_LENGTH} characters or fewer"
                ),
            },
        )
    return normalized


def _verify_iap_assertion(assertion: str, audience: str) -> dict[str, object]:
    now = time.time()
    cache_key = (assertion, audience)
    with _iap_assertion_cache_lock:
        cached = _iap_assertion_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return dict(cached[1])
        if cached is not None:
            del _iap_assertion_cache[cache_key]

    claims = id_token.verify_token(
        assertion,
        _iap_cert_request,
        audience=audience,
        certs_url=IAP_CERTS_URL,
    )
    expires_at = claims.get("exp")
    if isinstance(expires_at, (int, float)) and expires_at > now:
        cache_until = min(float(expires_at), now + IAP_ASSERTION_CACHE_MAX_SECONDS)
        with _iap_assertion_cache_lock:
            if len(_iap_assertion_cache) >= IAP_ASSERTION_CACHE_MAX_ENTRIES:
                expired_keys = [
                    key
                    for key, (expiry, _) in _iap_assertion_cache.items()
                    if expiry <= now
                ]
                for key in expired_keys:
                    del _iap_assertion_cache[key]
                if len(_iap_assertion_cache) >= IAP_ASSERTION_CACHE_MAX_ENTRIES:
                    oldest = min(
                        _iap_assertion_cache,
                        key=lambda key: _iap_assertion_cache[key][0],
                    )
                    del _iap_assertion_cache[oldest]
            _iap_assertion_cache[cache_key] = (cache_until, dict(claims))
    return claims


def _public_base_url(request: Request) -> str:
    """Where a browser reaches this API. Behind a load balancer the request's own
    base URL is the internal one, so the deployed host is configured explicitly."""
    configured = os.getenv("MAGIC_VIDEO_EDITOR_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _auth0_redirect_uri(request: Request) -> str:
    return f"{_public_base_url(request)}/studio/auth/callback"


def _valid_oauth_handoff(request: Request, state: str | None) -> OAuthHandoff | None:
    encoded = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    if not encoded or not state:
        return None
    handoff = decode_oauth_handoff(encoded)
    if handoff is None or not secrets.compare_digest(handoff.state, state):
        return None
    return handoff


def _sign_in_error_response(path: str) -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    clear_oauth_cookie(response)
    return response


def _safe_same_origin_return_path(return_to: str) -> bool:
    return (
        return_to.startswith("/")
        and not return_to.startswith("//")
        and "\\" not in return_to
        and "\x00" not in return_to
    )


def _owner_limit_exception(error: OwnerLimitExceeded) -> HTTPException:
    """429 for "too much at once", 507 for "no room", 403 for work this account
    may not start at all -- each refuses only new work."""
    if isinstance(error, AccountPolicyRefused):
        status = 403
    else:
        status = 429 if isinstance(error, OwnerConcurrentJobLimitExceeded) else 507
    return HTTPException(
        status_code=status,
        detail={"code": error.code, "message": error.message},
    )


def _request_id_reused_exception() -> HTTPException:
    """A `request_id` names one request: jobs are keyed by project, operation
    and `request_id`. Re-sending it with other contents, or after the project
    moved to a new revision (the job payload records the revision it was asked
    against), is a different request, so it is refused rather than starting
    another job or answering with one queued for an older revision. Identical
    retries at the same revision still return the original operation."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "request_id_reused",
            "message": "request_id was already used for a different request, such "
            "as one at an earlier project revision; send a new request_id",
        },
    )


_EXPORT_READ_ERRORS = (
    ReleaseIntegrityError,
    ImmutableExportCollision,
    ExportStorageUnavailable,
    AssetStorageUnavailable,
)


def _export_read_exception(error: Exception) -> HTTPException:
    """409 for stored bytes that fail verification, 503 for storage that
    cannot be reached right now."""
    if isinstance(error, ReleaseIntegrityError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "release_integrity_error",
                "message": "stored release failed integrity verification",
            },
        )
    if isinstance(error, ImmutableExportCollision):
        return HTTPException(
            status_code=409,
            detail={
                "code": "immutable_export_collision",
                "message": "immutable export integrity conflict",
            },
        )
    return HTTPException(
        status_code=503,
        detail={
            "code": "export_storage_unavailable",
            "message": "export storage is temporarily unavailable",
        },
    )


def _subject_upload_exception(error: SubjectUploadError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": error.code, "message": error.message},
    )


def _project_mutation_exception(
    error: ProjectNotFound | RevisionConflict,
) -> HTTPException:
    if isinstance(error, ProjectNotFound):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(
        status_code=409,
        detail={
            "message": "expected revision is stale",
            "current_revision": error.current_revision,
        },
    )


def _primary_action(stored: StoredManifest, has_release: bool) -> PrimaryAction:
    manifest = stored.manifest
    if manifest.subject.current_revision is None:
        return PrimaryAction.ADD_SUBJECT
    if manifest.subject.approval.state not in {
        ApprovalState.APPROVED,
        ApprovalState.WAIVED,
    }:
        return PrimaryAction.APPROVE_SUBJECT
    if not manifest.motions:
        return PrimaryAction.ADD_MOTION
    if any(not motion.candidates for motion in manifest.motions):
        return PrimaryAction.GENERATE_MOTION
    if any(
        motion.selected_candidate_id is None
        for motion in manifest.motions
        if motion.required
    ):
        return PrimaryAction.SELECT_CANDIDATES
    if manifest.set_assessment_digest != set_digest(manifest):
        return PrimaryAction.ASSESS_SET
    if manifest.set_approval.manifest_digest != set_digest(manifest):
        return PrimaryAction.APPROVE_SET
    return PrimaryAction.EXPORT if has_release else PrimaryAction.CREATE_RELEASE


def _require_setup_version(version: str | None) -> None:
    if version != "1":
        raise HTTPException(status_code=409, detail={"code": "unsupported_setup_version",
            "supported_versions": [1], "message": "Send X-Studio-Setup-Version: 1 for this setup schema"})


def _setup_exception(error: ValueError) -> HTTPException:
    code = ("setup_blocked" if isinstance(error, SetupBlocked) else
            "setup_confirmation_required" if isinstance(error, SetupConfirmationRequired) else "setup_conflict")
    return HTTPException(status_code=409, detail={"code": code, "message": str(error)})


def _uses_versioned_setup(stored: StoredManifest) -> bool:
    manifest = stored.manifest
    return (manifest.sprite_layout.value == "single_view"
            or manifest.identity_reference is not None
            or any(view.setup_version == 1 for view in manifest.views)
            or any(motion.specification.setup_version == 1 for motion in manifest.motions))
