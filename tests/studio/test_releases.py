import io
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from PIL import Image

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.manifest import (
    approve_set,
    approve_subject,
    create_project_manifest,
    set_digest,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    Approval,
    ApprovalState,
    ExportPackingEstimate,
    Facing,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionPreset,
    MotionSpecification,
    MotionState,
    QualityAssessment,
    SubjectRevision,
    canonical_release_digest,
)
from magic_video_editor.studio.service import (
    ProjectNotFound,
    ReleaseNotReady,
    StudioService,
)
from magic_video_editor.studio.store import DuplicateReleaseName

_sheet_buffer = io.BytesIO()
Image.new("RGBA", (256, 64), (255, 0, 0, 255)).save(_sheet_buffer, format="PNG")
PNG_SHEET = _sheet_buffer.getvalue()
def releasable_manifest(
    *,
    ordered_frames: tuple[int, ...] | None = None,
    frame_duration_ms: int | None = None,
    candidate_loop_policy: LoopPolicy | None = None,
    facing: Facing = Facing.RIGHT,
):
    subject = SubjectRevision(
        revision_id="subject-1", artifact_key="sources/subject.png",
        sha256="a" * 64, width=64, height=64,
    )
    candidate = MotionCandidate(
        candidate_id="candidate-1", artifact_key="outputs/idle.png",
        sha256=sha256(PNG_SHEET).hexdigest(),
        duration_ms=800,
        metadata=MotionFrameMetadata(
            decodable=True, media_kind=MediaKind.SHEET, frame_count=4, width=256, height=64,
            constant_geometry=True, has_alpha=True, baseline_y=55,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
        assessment=QualityAssessment(policy_version="motion-policy-v1"),
        ordered_frames=ordered_frames,
        frame_duration_ms=frame_duration_ms,
        loop_policy=candidate_loop_policy,
        facing=facing,
    )
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    manifest = manifest.with_subject(subject)
    manifest = approve_subject(
        manifest, revision_id="subject-1", assessment_id="subject-assessment",
        approver_id="user-1", policy_version="subject-policy-v1",
    )
    manifest = manifest.with_motion(MotionState(
        specification=MotionSpecification(
            motion_id="idle", name="Idle", preset=MotionPreset.IDLE,
            semantic_slot="idle", loop_policy=LoopPolicy.LOOP,
        ),
        candidates=(candidate,), selected_candidate_id="candidate-1",
    ))
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "set-assessment", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    return approve_set(
        manifest, assessment_id="set-assessment", approver_id="user-1",
        policy_version="set-policy-v1",
    )


async def test_release_pins_exact_signed_inputs() -> None:
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=MemoryAssetStore())

    release = await service.create_release(
        "project-1", created.revision, "first-playable", "user-1"
    )

    assert release.source_revision == created.revision
    assert release.subject_revision_id == "subject-1"
    assert release.motion_revision_ids == {"idle": "candidate-1"}
    assert release.set_approval.state is ApprovalState.APPROVED
    assert release.quality_policy_versions == (
        "subject-policy-v1", "motion-policy-v1", "set-policy-v1"
    )
    assert release.manifest_digest == created.manifest_digest
    assert release.release_digest == canonical_release_digest(release)
    with pytest.raises(TypeError):
        release.motion_revision_ids["idle"] = "tampered"  # type: ignore[index]


async def test_release_is_stamped_with_a_creation_timestamp() -> None:
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=MemoryAssetStore())

    before = datetime.now(UTC)
    release = await service.create_release(
        "project-1", created.revision, "first-playable", "user-1"
    )
    after = datetime.now(UTC)

    assert isinstance(release.created_at, datetime)
    assert before <= release.created_at <= after

    fetched = await service.list_releases("project-1", "user-1")
    assert fetched[0].created_at == release.created_at


async def test_release_requires_fresh_explicit_set_signoff_and_unique_name() -> None:
    store = MemoryStudioStore()
    unsigned = releasable_manifest().model_copy(update={"set_approval": Approval()})
    created = await store.create(unsigned)
    service = StudioService(store=store, assets=MemoryAssetStore())
    with pytest.raises(ReleaseNotReady):
        await service.create_release("project-1", created.revision, "v1", "user-1")


async def test_release_and_listing_are_owner_scoped() -> None:
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=MemoryAssetStore())

    with pytest.raises(ProjectNotFound, match="not found"):
        await service.create_release(
            "project-1", created.revision, "stolen", "other-user"
        )
    with pytest.raises(ProjectNotFound, match="not found"):
        await service.list_releases("project-1", "other-user")

    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=MemoryAssetStore())
    await service.create_release("project-1", created.revision, "v1", "user-1")
    with pytest.raises(DuplicateReleaseName):
        await service.create_release("project-1", created.revision, "v1", "user-1")


def test_waiver_reason_without_waiver_ids_is_rejected() -> None:
    manifest = releasable_manifest().model_copy(update={"set_approval": Approval()})
    with pytest.raises(ValueError, match="reason requires"):
        approve_set(
            manifest, assessment_id="set-assessment", approver_id="user-1",
            policy_version="set-policy-v1", waiver_reason="not needed",
        )


def test_duplicate_waiver_ids_are_rejected() -> None:
    manifest = releasable_manifest().model_copy(update={"set_approval": Approval()})
    with pytest.raises(ValueError, match="unique"):
        approve_set(
            manifest, assessment_id="set-assessment", approver_id="user-1",
            policy_version="set-policy-v1",
            waived_finding_ids=("x", "x"), waiver_reason="reviewed",
        )
