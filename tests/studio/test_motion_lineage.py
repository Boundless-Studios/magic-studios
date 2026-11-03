"""Subject lineage on motion candidates (studio-ux-flow phase 4):
`MotionCandidate.subject_revision_id` set at settlement, upload, and the
frame-revision path, plus the release-time guard against a stale selection.
"""

from hashlib import sha256
from io import BytesIO

from PIL import Image

from magic_video_editor.contracts import VideoJobResult
from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import (
    approve_set,
    approve_subject,
    create_project_manifest,
    set_digest,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    FindingSeverity,
    FrameSlot,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    MotionState,
    QualityAssessment,
    SubjectRevision,
)
from magic_video_editor.studio.service import StudioService


def _motion_sheet() -> bytes:
    output = BytesIO()
    image = Image.new("RGBA", (256, 64), (0, 0, 0, 0))
    for index in range(4):
        image.paste((255, 0, 0, 255), (index * 64 + 10, 10, index * 64 + 30, 50))
    image.save(output, format="PNG")
    return output.getvalue()


async def _service_with_approved_subject(
    *, dispatcher=None
) -> tuple[StudioService, MemoryStudioStore, object, MemoryAssetStore, object]:
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    project = await store.create(manifest)
    assets = MemoryAssetStore()

    class _Dispatcher:
        async def enqueue(self, job_id: str) -> None:
            return None

    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=dispatcher or _Dispatcher(),
        motion_generation_available=True,
    )
    return service, store, jobs, assets, project


async def test_settlement_sets_subject_revision_id_from_job_payload() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1", request_id="request-1"
    )
    assert job.payload["subject_revision_id"] == "subject-1"
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_sheet()

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    idle = next(m for m in settled.manifest.motions if m.motion_id == "idle")
    assert idle.candidates[0].subject_revision_id == "subject-1"


async def test_upload_candidate_sets_subject_revision_id_from_current_subject() -> None:
    service, _, _, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.PRESET,
            preset=MotionPreset.IDLE,
        ),
        "user-1",
    )
    upload_target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )
    artifact = _motion_sheet()
    assets.assets[upload_target.key] = artifact

    settled = await service.settle_candidate_upload(
        "project-1",
        project.revision,
        "idle",
        upload_target.key,
        "user-1",
        frame_count=4,
    )

    idle = next(m for m in settled.manifest.motions if m.motion_id == "idle")
    assert idle.candidates[0].subject_revision_id == "subject-1"


async def test_frame_revision_carries_subject_revision_id_from_source() -> None:
    service, store, _, _, project = await _service_with_approved_subject()
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.with_motion(
            MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            )
        ),
    )
    source = MotionCandidate(
        candidate_id="source-a",
        artifact_key="outputs/studio/project-1/motions/idle/source-a.png",
        sha256="c" * 64,
        duration_ms=400,
        subject_revision_id="subject-1",
        metadata=MotionFrameMetadata(
            decodable=True, media_kind=MediaKind.SHEET, frame_count=4,
            width=256, height=64, constant_geometry=True, has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
    )
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.model_copy(
            update={
                "motions": (
                    project.manifest.motions[0].model_copy(
                        update={
                            "candidates": (source,),
                            "selected_candidate_id": "source-a",
                        }
                    ),
                )
            }
        ),
    )

    revised = await service.create_motion_revision(
        "project-1",
        project.revision,
        "idle",
        "source-a",
        "user-1",
        ordered_frames=(FrameSlot(index=1), FrameSlot(index=0)),
        frame_duration_ms=90,
        loop_policy=LoopPolicy.LOOP,
    )

    motion = revised.manifest.motions[0]
    assert motion.selected_candidate is not None
    assert motion.selected_candidate.subject_revision_id == "subject-1"


def _sheet_candidate(candidate_id: str, *, subject_revision_id: str | None) -> MotionCandidate:
    artifact = _motion_sheet()
    metadata = MotionFrameMetadata(
        decodable=True, media_kind=MediaKind.SHEET, frame_count=4,
        width=256, height=64, constant_geometry=True, has_alpha=True,
        packing_estimate=ExportPackingEstimate(width=256, height=64),
    )
    return MotionCandidate(
        candidate_id=candidate_id,
        artifact_key=f"outputs/studio/project-1/motions/idle/{candidate_id}.png",
        sha256=sha256(artifact).hexdigest(),
        duration_ms=800,
        metadata=metadata,
        subject_revision_id=subject_revision_id,
    )


async def _releasable_manifest_with_candidate(candidate: MotionCandidate):
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1", artifact_key="sources/subject.png",
        sha256="a" * 64, width=64, height=64,
    )
    manifest = manifest.with_subject(subject)
    manifest = approve_subject(
        manifest, revision_id="subject-1", assessment_id="subject-assessment",
        approver_id="user-1", policy_version="subject-policy-v1",
    )
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE,
                semantic_slot="idle", loop_policy=LoopPolicy.LOOP,
            ),
            candidates=(candidate,),
            selected_candidate_id=candidate.candidate_id,
        )
    )
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "set-assessment", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    return approve_set(
        manifest, assessment_id="set-assessment", approver_id="user-1",
        policy_version="set-policy-v1",
    )


async def test_settling_a_new_subject_upload_keeps_takes_and_selection() -> None:
    """Regression for the production bug: correcting a subject by uploading a
    replacement used to wipe every motion's candidates. It must now keep
    them -- the old selection becomes stale rather than disappearing."""
    from studio.test_routes import _png_bytes

    service, store, _jobs, assets, project = await _service_with_approved_subject()
    candidate = _sheet_candidate("candidate-1", subject_revision_id="subject-1")
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.with_motion(
            MotionState(
                specification=MotionSpecification(
                    motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                ),
                candidates=(candidate,),
                selected_candidate_id="candidate-1",
            )
        ),
    )

    new_asset_key = "sources/studio/project-1/subjects/replacement.png"
    assets.assets[new_asset_key] = _png_bytes(
        size=(1024, 1024), subject_box=(200, 200, 824, 824), mode="RGBA"
    )

    settled = await service.settle_subject_upload(
        "project-1", project.revision, new_asset_key, "user-1"
    )

    idle = next(m for m in settled.manifest.motions if m.motion_id == "idle")
    assert idle.candidates == (candidate,)
    assert idle.selected_candidate_id == "candidate-1"
    new_subject_revision_id = settled.manifest.subject.current_revision.revision_id
    assert new_subject_revision_id != "subject-1"
    assert idle.selected_candidate.subject_revision_id != new_subject_revision_id


async def test_create_release_records_a_required_motions_stale_selected_candidate_as_a_warning() -> None:
    """Quality findings are warnings, never blockers (operator decision, 2026-09-05):
    a stale selection no longer refuses the release -- it is recorded as a
    `motion_stale_subject` finding on the set assessment and the release proceeds."""
    candidate = _sheet_candidate("candidate-1", subject_revision_id="a-different-subject")
    manifest = await _releasable_manifest_with_candidate(candidate)
    store = MemoryStudioStore()
    created = await store.create(manifest)
    service = StudioService(store=store, assets=MemoryAssetStore())

    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    codes = {finding.code for finding in release.manifest.set_assessment.findings}
    assert "motion_stale_subject" in codes
    stale_finding = next(
        finding
        for finding in release.manifest.set_assessment.findings
        if finding.code == "motion_stale_subject"
    )
    assert stale_finding.severity == FindingSeverity.WARNING


async def test_create_release_allows_a_candidate_with_no_recorded_subject_revision() -> None:
    """Pre-lineage data: `subject_revision_id=None` never blocks a release."""
    candidate = _sheet_candidate("candidate-1", subject_revision_id=None)
    manifest = await _releasable_manifest_with_candidate(candidate)
    store = MemoryStudioStore()
    created = await store.create(manifest)
    service = StudioService(store=store, assets=MemoryAssetStore())

    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    assert release.motion_revision_ids == {"idle": "candidate-1"}


async def test_create_release_allows_a_candidate_matching_the_current_subject() -> None:
    candidate = _sheet_candidate("candidate-1", subject_revision_id="subject-1")
    manifest = await _releasable_manifest_with_candidate(candidate)
    store = MemoryStudioStore()
    created = await store.create(manifest)
    service = StudioService(store=store, assets=MemoryAssetStore())

    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    assert release.motion_revision_ids == {"idle": "candidate-1"}
