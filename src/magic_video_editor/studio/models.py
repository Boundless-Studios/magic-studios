"""Immutable, provider-neutral values used by the Studio manifest."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    model_serializer,
)


class ApprovalState(StrEnum):
    NOT_READY = "not_ready"
    MEASURING = "measuring"
    NEEDS_CORRECTION = "needs_correction"
    READY_FOR_SIGNOFF = "ready_for_signoff"
    APPROVED = "approved"
    WAIVED = "waived"


class Topology(StrEnum):
    BIPED = "biped"
    QUADRUPED = "quadruped"
    SERPENTINE = "serpentine"
    FLYING = "flying"
    VEHICLE = "vehicle"
    AMORPHOUS = "amorphous"
    STATIONARY_OBJECT = "stationary_object"
    CUSTOM = "custom"


class MotionKind(StrEnum):
    PRESET = "preset"
    CUSTOM = "custom"


class PromptSource(StrEnum):
    """Whether a preset motion's `prompt` is still the compiled preset
    template or has been hand-edited by the operator (T16). Only meaningful
    for `MotionKind.PRESET` motions -- a custom motion's prompt is always the
    operator's own text and is never recompiled.
    """

    PRESET = "preset"
    CUSTOM = "custom"


class MotionPreset(StrEnum):
    IDLE = "idle"
    WALK = "walk"
    RUN = "run"
    JUMP = "jump"
    ATTACK = "attack"
    GUARD = "guard"
    CAST = "cast"
    HURT = "hurt"
    DEATH = "death"
    TRIP = "trip"


class SpriteLayout(StrEnum):
    UNASSIGNED = "unassigned"
    SINGLE_VIEW = "single_view"
    SIDE_VIEW = "side_view"
    ISOMETRIC = "isometric"


class ViewId(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    N = "N"
    NE = "NE"
    E = "E"
    SE = "SE"
    S = "S"
    SW = "SW"
    W = "W"
    NW = "NW"


class AnimationType(StrEnum):
    """What the Video Lab render pipeline treats a motion as (parity: `videoLabHandoff.js`)."""

    MOVEMENT = "movement"
    ATTACK = "attack"
    SPELLCAST = "spellcast"


class LoopPolicy(StrEnum):
    ONCE = "once"
    LOOP = "loop"
    PING_PONG = "ping_pong"


class Facing(StrEnum):
    """Which way a candidate's silhouette faces. A source candidate always starts RIGHT;
    each flip toggles it."""

    LEFT = "left"
    RIGHT = "right"


class MediaKind(StrEnum):
    """How a settled artifact was measured, independent of its storage key extension."""

    VIDEO = "video"
    SHEET = "sheet"


class CompositionModel(StrEnum):
    """Image-composition providers ported from the reference Video Lab's
    `COMPOSITION_MODELS` (`frontend/src/components/admin/VideoLab.jsx:59-64`)."""

    NANOBANANA_2_LITE = "nanobanana_2_lite"
    NANOBANANA_2_FLASH = "nanobanana_2_flash"
    NANOBANANA_2 = "nanobanana_2"
    SEEDREAM_5_0_LITE = "seedream_5_0_lite"


class CompositionApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class MotionReferenceRole(StrEnum):
    START = "start"
    CONTEXT = "context"
    END = "end"


class StylePreset(StrEnum):
    """The reference Video Lab's ten named styles (`VideoLab.jsx:43-52`)."""

    HIGH_FANTASY = "high_fantasy"
    CYBERPUNK = "cyberpunk"
    STEAMPUNK = "steampunk"
    SPACE_CORPORATE = "space_corporate"
    GRIMDARK = "grimdark"
    POST_APOCALYPTIC = "post_apocalyptic"
    NOIR = "noir"
    PSYCHEDELIC = "psychedelic"
    ANCIENT_MYTHOLOGY = "ancient_mythology"
    WESTERN = "western"


class MotionGenerationPreset(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    HIGH = "high"
    CINEMATIC = "cinematic"


class Backdrop(StrEnum):
    """How a motion's output is expected to carry transparency. `CHROMA_GREEN` is the
    default (parity with the reference H3 render contract): the provider renders against a flat
    chroma-green backdrop and the studio keys it out with `derive_alpha`. `NATIVE_ALPHA`
    trusts the provider to return alpha directly and never runs the automatic pass."""

    NATIVE_ALPHA = "native_alpha"
    CHROMA_GREEN = "chroma_green"


class FindingSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class SubjectFindingCode(StrEnum):
    SUBJECT_DIMENSION_TOO_SMALL = "subject_dimension_too_small"
    SUBJECT_MISSING_ALPHA = "subject_missing_alpha"
    SUBJECT_TOUCHES_CANVAS_EDGE = "subject_touches_canvas_edge"
    SUBJECT_OCCUPANCY_TOO_LOW = "subject_occupancy_too_low"
    SUBJECT_OCCUPANCY_TOO_HIGH = "subject_occupancy_too_high"


class RevisionLifecycle(StrEnum):
    DRAFT = "draft"
    MEASURING = "measuring"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class FrozenModel(BaseModel):
    """Base class ensuring domain values cannot be changed in place."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MotionFrameMetadata(FrozenModel):
    """Bounded metadata extracted from a sprite/video artifact."""

    decodable: bool
    media_kind: MediaKind
    frame_count: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    constant_geometry: bool
    has_alpha: bool
    duplicate_terminal_frame: bool = False
    baseline_y: int | None = Field(default=None, ge=0)
    pivot_x: int | None = Field(default=None, ge=0)
    pivot_y: int | None = Field(default=None, ge=0)
    packing_estimate: "ExportPackingEstimate"
    frame_rate: str | None = None
    time_base: str | None = None
    duration_ms: float | None = Field(default=None, gt=0)



class ExportPackingEstimate(FrozenModel):
    profile: str = "horizontal-strip-v1"
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class QualityFinding(FrozenModel):
    code: str
    severity: FindingSeverity
    message: str
    action_id: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _read_blocking_as_warning(cls, value: object) -> object:
        """Quality findings are warnings, never blockers (operator decision,
        2026-09-05). Old stored data may still carry `severity: "blocking"` --
        normalize it to a warning wherever it is read."""
        if value == FindingSeverity.BLOCKING or value == "blocking":
            return FindingSeverity.WARNING
        return value


class QualityAssessment(FrozenModel):
    policy_version: str
    findings: tuple[QualityFinding, ...] = ()

    @property
    def blocking_findings(self) -> tuple[QualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.BLOCKING)


class HistoryEntry(FrozenModel):
    """One row on the project History page: a saved manifest revision or a terminal operation."""

    revision: int | None = None
    timestamp: datetime | None = None
    actor: str | None = None
    summary: str
    failed: bool = False


class Approval(FrozenModel):
    state: ApprovalState = ApprovalState.NOT_READY
    assessment_id: str | None = None
    approver_id: str | None = None
    policy_version: str | None = None
    artifact_sha256: str | None = None
    manifest_digest: str | None = None
    waived_finding_ids: tuple[str, ...] = ()
    waiver_reason: str | None = None


class SubjectRevisionEdit(StrEnum):
    """A pixel fix re-run on an existing take, producing a new `EDITED` one."""

    REFRAME = "reframe"
    CLEANUP = "cleanup"


class SubjectRevisionSource(StrEnum):
    """Where a subject revision's artifact came from (studio-ux-flow phase 3)."""

    GENERATED = "generated"
    UPLOADED = "uploaded"
    EDITED = "edited"


class SubjectRevision(FrozenModel):
    revision_id: str
    artifact_key: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    topology: Topology | None = None
    lifecycle: RevisionLifecycle = RevisionLifecycle.READY
    parent_revision_ids: tuple[str, ...] = ()
    source: SubjectRevisionSource = SubjectRevisionSource.GENERATED
    direction: str | None = None
    created_at: datetime | None = None


class SubjectFinding(FrozenModel):
    code: SubjectFindingCode
    severity: FindingSeverity
    title: str
    explanation: str
    artifact_sha256: str
    action_id: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _read_blocking_as_warning(cls, value: object) -> object:
        """Quality findings are warnings, never blockers (operator decision,
        2026-09-05). Old stored data may still carry `severity: "blocking"` --
        normalize it to a warning wherever it is read."""
        if value == FindingSeverity.BLOCKING or value == "blocking":
            return FindingSeverity.WARNING
        return value


class SubjectAssessment(FrozenModel):
    assessment_id: str
    policy_version: str
    artifact_sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    occupancy_ratio: float = Field(ge=0.0, le=1.0)
    findings: tuple[SubjectFinding, ...] = ()


class SubjectState(FrozenModel):
    revisions: tuple[SubjectRevision, ...] = ()
    current_revision: SubjectRevision | None = None
    assessment: SubjectAssessment | None = None
    approval: Approval = Approval()

    @property
    def current_revision_id(self) -> str | None:
        return self.current_revision.revision_id if self.current_revision else None

    def with_revision(self, revision: SubjectRevision) -> Self:
        return self.model_copy(
            update={
                "revisions": (*self.revisions, revision),
                "current_revision": revision,
                "assessment": None,
                "approval": Approval(),
            }
        )

    def with_assessment(
        self,
        assessment: SubjectAssessment,
        *,
        approval: Approval,
    ) -> Self:
        return self.model_copy(
            update={
                "assessment": assessment,
                "approval": approval,
            }
        )


class CompositionRevision(FrozenModel):
    """One composed opening-frame still (T15). Parity with the reference Video
    Lab's composition routes: a still is always generated pending, then approved
    or superseded by a regenerate, never approved-in-place."""

    revision_id: str
    artifact_key: str
    sha256: str
    prompt: str = Field(min_length=1, max_length=2000)
    model: CompositionModel = CompositionModel.NANOBANANA_2_LITE
    parent_subject_revision_id: str
    approval: CompositionApprovalState = CompositionApprovalState.PENDING


class CompositionState(FrozenModel):
    revisions: tuple[CompositionRevision, ...] = ()
    current: CompositionRevision | None = None

    @property
    def current_revision_id(self) -> str | None:
        return self.current.revision_id if self.current else None

    def with_composition(self, revision: CompositionRevision) -> Self:
        """Append a newly settled still, superseding whatever was current."""
        revisions = self.revisions
        if self.current is not None:
            superseded = self.current.model_copy(
                update={"approval": CompositionApprovalState.SUPERSEDED}
            )
            revisions = tuple(
                superseded if item.revision_id == self.current.revision_id else item
                for item in revisions
            )
        return self.model_copy(
            update={"revisions": (*revisions, revision), "current": revision}
        )

    def with_approval(self, revision_id: str) -> Self:
        if self.current is None or self.current.revision_id != revision_id:
            raise ValueError(
                "composition approval requires the current composition revision"
            )
        approved = self.current.model_copy(
            update={"approval": CompositionApprovalState.APPROVED}
        )
        revisions = tuple(
            approved if item.revision_id == revision_id else item
            for item in self.revisions
        )
        return self.model_copy(update={"revisions": revisions, "current": approved})


class MotionReference(FrozenModel):
    """One reference image bound to a motion generation (T17)."""

    key: str = Field(max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: MotionReferenceRole


def _standard_recipe_version() -> str:
    from outbound_gpu_worker_pool.machine_profiles import STANDARD, describe_profile
    return describe_profile(STANDARD)["recipe_version"]


class MotionGenerationSettings(FrozenModel):
    """Steps/size seeded from `PRESET_DEFAULTS`; parity with the reference Video Lab's `PRESET_DEFAULTS`
    (`VideoLab.jsx:73-78`). The server never silently overrides an explicit request value — a
    preset only supplies defaults for `for_preset()` callers, it does not re-clamp stored settings.
    """

    preset: MotionGenerationPreset = MotionGenerationPreset.STANDARD
    steps: int = Field(default=20, ge=4, le=60)
    seed: int = Field(default=42, ge=0)
    width: int = Field(default=512, ge=64, le=4096)
    height: int = Field(default=512, ge=64, le=4096)
    duration_seconds: int = Field(default=4, ge=1, le=120)
    backdrop: Backdrop = Backdrop.CHROMA_GREEN
    provider_parameters: dict[str, bool | int | float | str] = Field(
        default_factory=dict, max_length=32
    )

    PRESET_DEFAULTS: ClassVar[dict[MotionGenerationPreset, dict[str, int]]] = {
        MotionGenerationPreset.QUICK: {"steps": 16, "width": 864, "height": 480},
        MotionGenerationPreset.STANDARD: {"steps": 20, "width": 1344, "height": 768},
        MotionGenerationPreset.HIGH: {"steps": 28, "width": 1344, "height": 768},
        MotionGenerationPreset.CINEMATIC: {"steps": 32, "width": 1344, "height": 768},
    }

    @classmethod
    def for_preset(cls, preset: MotionGenerationPreset, **overrides: object) -> Self:
        """Build settings seeded from a preset's defaults, explicit overrides winning."""
        defaults = cls.PRESET_DEFAULTS[preset]
        return cls(preset=preset, **{**defaults, **overrides})

    @model_validator(mode="after")
    def bounded_provider_parameters(self) -> Self:
        if "framing_fill" in self.provider_parameters:
            fill = self.provider_parameters["framing_fill"]
            if isinstance(fill, bool) or not isinstance(fill, (int, float)) or not 0.3 <= fill <= 0.9:
                raise ValueError("reference fill must be between 0.3 and 0.9")
        profile = self.provider_parameters.get("execution_profile")
        # Higgsfield models declare their own last-frame support; the plugin
        # checks the chosen model before any job exists.
        hosted = self.provider_parameters.get("execution_provider") == "higgsfield"
        # A request pinned to the verified standard recipe may ask for a closed loop;
        # machine resolution then requires the pinned worker to report last-frame
        # support. Any other recipe keeps needing its declared 16 GB profile.
        pinned = bool(self.provider_parameters.get("worker_id")
                      and self.provider_parameters.get("recipe_version") == _standard_recipe_version())
        if (self.provider_parameters.get("closed_loop") is True and not hosted and not pinned
                and profile not in {"h3-w4a8-16gb", "h3-w4a8-16gb-lightx8"}):
            raise ValueError("last-frame conditioning requires the declared h3-w4a8-16gb workflow profile or a machine pinned to a verified recipe")
        if profile in {"h3-w4a8-16gb", "h3-w4a8-16gb-lightx8"}:
            if self.width > 512 or self.height > 512 or self.duration_seconds > 4 or self.steps > 20:
                raise ValueError("the verified 16 GB profile supports at most 512x512 pixels, 4 seconds and 20 steps")
        if profile == "h3-w4a8-16gb-lightx8" and self.steps != 8:
            raise ValueError("the LightX2V 8-step profile requires exactly 8 steps")
        if any(not key or len(key) > 100 for key in self.provider_parameters):
            raise ValueError(
                "provider parameter names must contain 1 to 100 characters"
            )
        if any(
            isinstance(value, str) and len(value) > 500
            for value in self.provider_parameters.values()
        ):
            raise ValueError("provider parameter values must not exceed 500 characters")
        return self


class FrameSlot(FrozenModel):
    """One included frame in playback order, held for `hold` consecutive display steps."""

    index: int = Field(ge=0)
    hold: int = Field(default=1, ge=1, le=8)


def _coerce_frame_slot(value: object) -> object:
    """Accept a bare frame index as input flexibility: a plain int normalises to hold=1.
    This is not a compatibility shim for a prior wire format -- it is the documented input
    shape alongside the richer {index, hold} object, both always accepted."""
    if isinstance(value, bool):
        raise TypeError("frame slot index must be an int, not a bool")
    if isinstance(value, int):
        return {"index": value, "hold": 1}
    return value


def _coerce_ordered_frames(value: object) -> object:
    if value is None or isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Sequence):
        return tuple(_coerce_frame_slot(item) for item in value)
    return value


OrderedFrames = Annotated[
    tuple[FrameSlot, ...] | None, BeforeValidator(_coerce_ordered_frames)
]
RequiredOrderedFrames = Annotated[
    tuple[FrameSlot, ...], BeforeValidator(_coerce_ordered_frames)
]


class NormalizationSpec(FrozenModel):
    """One calibrated transform shared by every animation frame, in pixel units."""

    scale: float = Field(gt=0, le=8, allow_inf_nan=False)
    source_anchor_x: float = Field(ge=0, le=4096, allow_inf_nan=False)
    source_anchor_y: float = Field(ge=0, le=4096, allow_inf_nan=False)
    target_anchor_x: float = Field(ge=0, le=4096, allow_inf_nan=False)
    target_anchor_y: float = Field(ge=0, le=4096, allow_inf_nan=False)
    padding: int = Field(default=8, ge=0, le=128)
    cleanup_green_edges: bool = False
    reference_candidate_id: str | None = Field(default=None, min_length=1, max_length=255)
    reference_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def paired_reference(self) -> Self:
        if (self.reference_candidate_id is None) != (self.reference_sha256 is None):
            raise ValueError("normalization reference requires both candidate id and sha256")
        return self


class DerivationRecipe(FrozenModel):
    """How a derived candidate's artifact was produced from its parent (T18/T19)."""

    op: str
    key: str | None = None
    similarity: float | None = None
    blend: float | None = None
    target_frame_count: int | None = None
    cell_size: int | None = None
    normalization: NormalizationSpec | None = None
    source_ordered_frames: OrderedFrames = None
    sampled_source_indices: tuple[int, ...] | None = None


class CandidateSource(StrEnum):
    """Where a candidate's artifact came from (T21)."""

    GENERATED = "generated"
    UPLOAD = "upload"
    DERIVED = "derived"


class MotionCandidate(FrozenModel):
    @model_serializer(mode="wrap")
    def preserve_provenance_presence(self, handler):
        data = handler(self)
        # Releases made before execution attestation did not sign this field.
        # Retain an explicitly persisted empty object, but never inject it into
        # an older snapshot merely by loading it with the current model.
        if not self.execution_provenance and "execution_provenance" not in self.model_fields_set:
            data.pop("execution_provenance", None)
        return data

    execution_provenance: dict[str, Any] = Field(default_factory=dict)
    candidate_id: str
    artifact_key: str
    sha256: str
    lifecycle: RevisionLifecycle = RevisionLifecycle.READY
    parent_revision_ids: tuple[str, ...] = ()
    duration_ms: int | None = Field(default=None, gt=0)
    metadata: MotionFrameMetadata | None = None
    assessment: QualityAssessment | None = None
    ordered_frames: OrderedFrames = Field(default=None, min_length=1, max_length=10_000)
    frame_duration_ms: int | None = Field(default=None, gt=0, le=60_000)
    loop_policy: LoopPolicy | None = None
    generation_settings: MotionGenerationSettings | None = None
    facing: Facing = Facing.RIGHT
    composition_revision_id: str | None = None
    recipe: DerivationRecipe | None = None
    source: CandidateSource = CandidateSource.GENERATED
    subject_revision_id: str | None = None
    # The spec digest this take was rendered from; a later spec edit makes it read as older.
    motion_spec_digest: str | None = None

    @model_validator(mode="after")
    def valid_frame_order(self) -> Self:
        if self.ordered_frames is None:
            return self
        indexes = [slot.index for slot in self.ordered_frames]
        if len(set(indexes)) != len(indexes):
            raise ValueError("frame indexes must be unique")
        if self.metadata is None or any(
            index < 0 or index >= self.metadata.frame_count for index in indexes
        ):
            raise ValueError("frame indexes must be within measured frame bounds")
        return self

    @property
    def effective_ordered_frames(self) -> tuple[FrameSlot, ...]:
        if self.ordered_frames is not None:
            return self.ordered_frames
        return (
            tuple(FrameSlot(index=index) for index in range(self.metadata.frame_count))
            if self.metadata
            else ()
        )


class ViewReference(FrozenModel):
    revision_id: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)


class CharacterView(FrozenModel):
    view_id: ViewId | str
    heading: ViewId | None = None
    setup_version: Literal[1] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        data = handler(self)
        if self.setup_version is None:
            data.pop("heading", None)
            data.pop("setup_version", None)
        return data

    name: str = Field(min_length=1, max_length=200)
    reference: ViewReference | None = None


def view_matches_layout(view_id: ViewId, layout: SpriteLayout) -> bool:
    if layout is SpriteLayout.SINGLE_VIEW:
        return view_id == "single"
    if layout is SpriteLayout.SIDE_VIEW:
        return view_id in {ViewId.LEFT, ViewId.RIGHT}
    if layout is SpriteLayout.ISOMETRIC:
        return view_id in {ViewId.N, ViewId.NE, ViewId.E, ViewId.SE, ViewId.S, ViewId.SW, ViewId.W, ViewId.NW}
    return False


class MotionSpecification(FrozenModel):
    view_id: ViewId | str | None = None
    view_heading: ViewId | None = None
    setup_version: Literal[1] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_fields(self, handler):
        data = handler(self)
        if self.setup_version is None:
            data.pop("view_heading", None)
            data.pop("setup_version", None)
        return data

    view_reference: ViewReference | None = None
    motion_id: str = Field(
        pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100
    )
    name: str = Field(min_length=1, max_length=200)
    kind: MotionKind = MotionKind.PRESET
    semantic_slot: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100
    )
    preset: MotionPreset | None = None
    description: str | None = None
    # The operator's one-line "what happens" for a preset motion, composed into the
    # generation prompt. Custom motions carry their direction in `prompt` instead.
    action: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)
    prompt_source: PromptSource = PromptSource.PRESET
    loop_policy: LoopPolicy = LoopPolicy.LOOP
    direction: str | None = None
    facing: str | None = None
    timing_intent: str | None = None
    required: bool = True
    references: tuple[MotionReference, ...] = Field(default=(), max_length=16)
    style: StylePreset | None = None
    script: str | None = Field(default=None, max_length=2000)

    def model_post_init(self, __context: object, /) -> None:
        if self.view_id is not None and self.setup_version is None:
            ViewId(self.view_id)
        if self.kind is MotionKind.CUSTOM and not (self.prompt and self.prompt.strip()):
            raise ValueError("custom motions require a nonempty prompt")
        if self.kind is MotionKind.CUSTOM and self.preset is not None:
            raise ValueError("custom motions cannot specify a preset")
        if self.kind is MotionKind.PRESET and self.preset is None:
            raise ValueError("preset motions require a supported explicit preset")
        if self.view_reference is not None and self.view_id is None:
            raise ValueError("view_reference requires a view_id")
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


class MotionState(FrozenModel):
    specification: MotionSpecification
    candidates: tuple[MotionCandidate, ...] = ()
    selected_candidate_id: str | None = None

    @property
    def motion_id(self) -> str:
        return self.specification.motion_id

    @property
    def selected_candidate(self) -> MotionCandidate | None:
        return next(
            (
                candidate
                for candidate in self.candidates
                if candidate.candidate_id == self.selected_candidate_id
            ),
            None,
        )

    @property
    def required(self) -> bool:
        return self.specification.required


_SYSTEM_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"


class ExternalSubject(FrozenModel):
    """Ties the project's subject to a row in another target's roster (T22). A
    pure tag on the manifest: setting or clearing it does not touch the subject
    revision, any candidate, or any approval -- a target adapter's export reads
    it to decide whether to emit its own bindings document, nothing more.
    `system` and `entity_kind` are validated against the installed
    `TargetRegistry` at the API boundary (`routes.py`), not here -- the model
    layer stays free of any particular adapter's vocabulary."""

    system: str = Field(min_length=1, max_length=50, pattern=_SYSTEM_PATTERN)
    entity_kind: str = Field(min_length=1, max_length=50, pattern=_SYSTEM_PATTERN)
    entity_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class DeliveryStatus(StrEnum):
    """Where one delivery attempt of a release to a target adapter landed."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class ReleaseDelivery(FrozenModel):
    """One attempt to deliver a release's target adapter package to that
    target's importer (Part B). Recorded append-only on the manifest
    (`manifest.py`'s `record_release_delivery`) -- a retry adds a new entry
    rather than mutating the last one, so the delivery history for a release
    survives exactly like every other manifest revision.

    `summary` and `details` are adapter-neutral: the studio core never
    interprets what a target's importer reported, it only stores and shows
    what the adapter's `DeliveryReceipt` handed back."""

    release_id: str
    target: str = Field(min_length=1, max_length=50, pattern=_SYSTEM_PATTERN)
    status: DeliveryStatus
    delivered_at: datetime | None = None
    summary: str = ""
    details: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)
    retryable: bool = False


class MotionDispatchReservation(FrozenModel):
    operation_id: str
    motion_id: str
    requested_revision: int


class TurnaroundEquipment(FrozenModel):
    """One piece of equipment the turnaround must keep attached. Hands, shoulders
    and hips are sided; only back and other items may be unsided."""

    item: str = Field(min_length=1, max_length=40)
    attachment: Literal["hand", "shoulder", "hip", "back", "other"]
    source_picture_side: Literal["left", "right", "unsided"]

    @model_validator(mode="after")
    def sided_attachments_name_a_side(self):
        if self.source_picture_side == "unsided" and self.attachment in {"hand", "shoulder", "hip"}:
            raise ValueError(f"{self.attachment} equipment must say left or right")
        return self


class TurnaroundPreparedFrame(FrozenModel):
    """The exact bytes sent to the worker for both template slots."""

    key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    algorithm: str
    fill: float
    slots: tuple[Literal["first_frame", "last_frame"], ...] = ("first_frame", "last_frame")


class TurnaroundPick(FrozenModel):
    """One heading's chosen frame, with any override the person confirmed."""

    heading: ViewId
    frame_index: int = Field(ge=0)
    confirm_out_of_region: bool = False
    confirm_out_of_order: bool = False


class TurnaroundPickSet(FrozenModel):
    """The whole set of picks, replaced atomically; an override is valid only in
    the revision that carries it."""

    revision: int = Field(ge=1)
    picks: tuple[TurnaroundPick, ...] = Field(min_length=7, max_length=7)
    # The overrides this revision used, with how far outside the rule each pick was.
    findings: tuple[dict[str, Any], ...] = ()
    saved_by: str
    saved_at: datetime


class TurnaroundTake(FrozenModel):
    """A closed-loop turn of one bound view reference. Never a motion: it is
    excluded from motions, exports and completeness."""

    turnaround_id: str = Field(pattern=r"^turnaround-[0-9a-f]{32}$")
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_view_id: str
    source_heading: ViewId
    source_reference: ViewReference
    source_backdrop: Literal["transparent", "chroma"]
    equipment: tuple[TurnaroundEquipment, ...] = Field(default=(), max_length=4)
    prompt_version: Literal["turnaround_prompt.v1"] = "turnaround_prompt.v1"
    prompt: str
    prepared_frame: TurnaroundPreparedFrame
    requested_revision: int
    created_at: datetime | None = None
    candidate: MotionCandidate | None = None
    # Set once the candidate's bytes are in the owner's storage total. Settling
    # stores the candidate first and counts it after, so a failed count is
    # retried by the next settle or reconcile instead of being lost.
    stored_bytes_recorded: bool = False
    pick_set: TurnaroundPickSet | None = None


class ViewDraftEntry(FrozenModel):
    """Where to find a direction draft. The draft itself (image, descriptor,
    check receipts, discard tombstone) is immutable assets; this only indexes it."""

    draft_id: str = Field(pattern=r"^draft-[0-9a-f]{32}$")
    turnaround_id: str = Field(pattern=r"^turnaround-[0-9a-f]{32}$")
    heading: ViewId
    check_count: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    # Set by discard inside the revision fence; a discarded draft is never bound.
    discarded_at: datetime | None = None


class AnimationProjectManifest(FrozenModel):
    # Appended fields are serialized only for explicitly configured view projects.
    sprite_layout: SpriteLayout = SpriteLayout.UNASSIGNED
    views: tuple[CharacterView, ...] = ()
    identity_reference: ViewReference | None = None
    removed_motion_ids: tuple[str, ...] = ()
    dispatch_reservations: tuple[MotionDispatchReservation, ...] = ()
    abandoned_dispatches: tuple[str, ...] = ()
    turnarounds: tuple[TurnaroundTake, ...] = ()
    view_drafts: tuple[ViewDraftEntry, ...] = ()

    @model_serializer(mode="wrap")
    def serialize_layout(self, handler):
        data = handler(self)
        if not self.turnarounds:
            data.pop("turnarounds", None)
        if not self.view_drafts:
            data.pop("view_drafts", None)
        if self.identity_reference is None:
            data.pop("identity_reference", None)
        if not self.abandoned_dispatches:
            data.pop("abandoned_dispatches", None)
        if not self.dispatch_reservations:
            data.pop("dispatch_reservations", None)
        if not self.removed_motion_ids:
            data.pop("removed_motion_ids", None)
        if self.sprite_layout is SpriteLayout.UNASSIGNED and not self.views:
            data.pop("sprite_layout", None)
            data.pop("views", None)
        return data

    @model_validator(mode="after")
    def valid_views(self):
        ids = [view.view_id for view in self.views]
        if len(set(ids)) != len(ids):
            raise ValueError("view IDs must be unique")
        if any(not view_matches_layout(view.heading if view.setup_version else view.view_id, self.sprite_layout)
               if self.sprite_layout is not SpriteLayout.SINGLE_VIEW else view.heading is not None or view.view_id != "single"
               for view in self.views):
            raise ValueError("view does not match sprite layout")
        if any(motion.specification.view_id is not None and motion.specification.view_id not in ids for motion in self.motions):
            raise ValueError("motion view is not declared in the project")
        return self

    project_id: str
    name: str
    owner_id: str | None = None
    external_subject: ExternalSubject | None = None
    subject: SubjectState = SubjectState()
    composition: CompositionState = CompositionState()
    motions: tuple[MotionState, ...] = ()
    set_approval: Approval = Approval()
    set_assessment_id: str | None = None
    set_assessment_digest: str | None = None
    set_assessment: QualityAssessment | None = None
    releases: tuple["Release", ...] = ()
    release_deliveries: tuple[ReleaseDelivery, ...] = ()

    def with_subject(self, revision: SubjectRevision) -> Self:
        """Return a manifest with a new current subject revision.

        Motions -- their candidates and selection -- are left untouched.
        Every candidate records the subject revision it was made from
        (`MotionCandidate.subject_revision_id`), so a selection made for an
        earlier subject becomes stale rather than being silently dropped:
        `assess_set` surfaces it as a warning finding (`motion_stale_subject`),
        and release creation records the same warning rather than refusing."""
        return self.model_copy(
            update={
                "subject": self.subject.with_revision(revision),
                "composition": CompositionState(),
                "set_approval": Approval(),
                "set_assessment_id": None,
                "set_assessment_digest": None,
                "set_assessment": None,
            }
        )

    def with_motion(self, motion: MotionSpecification | MotionState) -> Self:
        """Add or replace a motion while preserving immutable history."""
        state = (
            motion
            if isinstance(motion, MotionState)
            else MotionState(
                specification=motion,
            )
        )
        motions = tuple(
            state if existing.motion_id == state.motion_id else existing
            for existing in self.motions
        )
        if all(existing.motion_id != state.motion_id for existing in self.motions):
            motions = (*self.motions, state)
        return self.model_copy(
            update={
                "motions": motions,
                "set_approval": Approval(),
                "set_assessment_id": None,
                "set_assessment_digest": None,
                "set_assessment": None,
            }
        )

    def with_set_assessment(
        self,
        assessment_id: str,
        assessment_digest: str,
        assessment: QualityAssessment | None = None,
    ) -> Self:
        """Attach quality evidence to the working set without approving it."""
        return self.model_copy(
            update={
                "set_assessment_id": assessment_id,
                "set_assessment_digest": assessment_digest,
                "set_assessment": assessment,
                "set_approval": Approval(),
            }
        )


class Release(FrozenModel):
    release_id: str
    name: str
    manifest_digest: str
    manifest: AnimationProjectManifest
    release_digest: str = ""
    release_digest_version: int = Field(default=1, gt=0)
    signed_payload: str = ""
    project_id: str = ""
    source_revision: int = Field(default=1, gt=0)
    subject_revision_id: str | None = None
    motion_revision_pins: tuple[tuple[str, str], ...] = ()
    quality_policy_versions: tuple[str, ...] = ()
    set_approval: Approval = Approval()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def motion_revision_ids(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.motion_revision_pins))


def canonical_release_payload_v1(
    release: Release, *, raw_manifest: object | None = None
) -> str:
    """Frozen v1 bytes; raw_manifest avoids future model normalization."""
    manifest = (
        release.manifest.model_dump(mode="json", exclude_none=True)
        if raw_manifest is None
        else raw_manifest
    )
    canonical = release_payload_fields_v1(
        release_id=release.release_id,
        name=release.name,
        project_id=release.project_id,
        source_revision=release.source_revision,
        manifest_digest=release.manifest_digest,
        raw_manifest=manifest,
    )
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def release_payload_fields_v1(
    *,
    release_id: str,
    name: str,
    project_id: str,
    source_revision: int,
    manifest_digest: str,
    raw_manifest: object,
) -> dict[str, object]:
    """Extract only the frozen v1 signed primitives from raw persisted JSON."""
    if not isinstance(raw_manifest, dict):
        raise TypeError("release manifest snapshot must be a JSON object")
    subject = raw_manifest.get("subject")
    subject = subject if isinstance(subject, dict) else {}
    subject_current = subject.get("current_revision")
    subject_current = subject_current if isinstance(subject_current, dict) else {}
    subject_approval = subject.get("approval")
    subject_approval = subject_approval if isinstance(subject_approval, dict) else {}
    set_approval = raw_manifest.get("set_approval")
    set_approval = set_approval if isinstance(set_approval, dict) else {}
    pins: dict[str, str] = {}
    policies: list[str] = []
    _append_unique(policies, subject_approval.get("policy_version"))
    motions = raw_manifest.get("motions")
    for motion in motions if isinstance(motions, list) else []:
        if not isinstance(motion, dict):
            continue
        specification = motion.get("specification")
        specification = specification if isinstance(specification, dict) else {}
        motion_id = specification.get("motion_id")
        selected_id = motion.get("selected_candidate_id")
        if isinstance(motion_id, str) and isinstance(selected_id, str):
            pins[motion_id] = selected_id
            candidates = motion.get("candidates")
            for candidate in candidates if isinstance(candidates, list) else []:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("candidate_id") == selected_id
                ):
                    assessment = candidate.get("assessment")
                    if isinstance(assessment, dict):
                        _append_unique(policies, assessment.get("policy_version"))
                    break
    _append_unique(policies, set_approval.get("policy_version"))
    return {
        "release_id": release_id,
        "name": name,
        "project_id": project_id,
        "source_revision": source_revision,
        "manifest_digest": manifest_digest,
        "manifest": _drop_none_v1(raw_manifest),
        "subject_revision_id": subject_current.get("revision_id"),
        "motion_revision_ids": pins,
        "quality_policy_versions": policies,
        "waived_finding_ids": set_approval.get("waived_finding_ids", []),
        "waiver_reason": set_approval.get("waiver_reason"),
        "approver_id": set_approval.get("approver_id"),
        "approval_state": set_approval.get("state", "not_ready"),
    }


def _append_unique(values: list[str], value: object) -> None:
    if isinstance(value, str) and value not in values:
        values.append(value)


def _drop_none_v1(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_none_v1(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, list):
        return [_drop_none_v1(item) for item in value]
    return value


def _canonical_release_digest_v1(release: Release) -> str:
    payload = release.signed_payload or canonical_release_payload_v1(release)
    return sha256(payload.encode()).hexdigest()


_RELEASE_DIGEST_VERIFIERS = {1: _canonical_release_digest_v1}


def canonical_release_digest(release: Release) -> str:
    verifier = _RELEASE_DIGEST_VERIFIERS.get(release.release_digest_version)
    if verifier is None:
        raise ValueError(
            f"unsupported release digest version: {release.release_digest_version}"
        )
    return verifier(release)


class ExportProfile(FrozenModel):
    name: str
    version: str


class ExportRecord(FrozenModel):
    export_id: str
    release_id: str
    profile_name: str
    profile_version: str
    artifact_key: str
    sha256: str
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class ProjectActivityStats(FrozenModel):
    """Operations-ledger counts for one project, shown on the home list and as
    the always-visible stats line on the Activity trigger."""

    running: int = Field(ge=0)
    failed: int = Field(ge=0)
    completed: int = Field(ge=0)
    last_activity_at: datetime | None = None


class ProjectSummary(FrozenModel):
    """One row on the project home list: enough to route back into a project
    and show its basic status without a full manifest fetch."""

    project_id: str
    name: str
    revision: int = Field(gt=0)
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    subject_state: Literal["approved", "pending", "none"]
    motion_count: int = Field(ge=0)
    release_count: int = Field(ge=0)
    external_subject: ExternalSubject | None = None
    activity: ProjectActivityStats
    unreadable: bool = False
    # Set during the warning window: when inactivity expiry deletes it unless opened.
    expires_at: datetime | None = None
