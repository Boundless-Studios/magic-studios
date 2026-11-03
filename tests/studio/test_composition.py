"""T15: opening-frame composition and the shared-starting-pose batch."""

from io import BytesIO

import pytest
from PIL import Image

from magic_video_editor.contracts import JobOperation, VideoJobResult
from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    CompositionApprovalState,
    CompositionModel,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    SubjectRevision,
)
from magic_video_editor.studio.service import (
    CompositionApprovalBlocked,
    CompositionGenerationUnavailable,
    MotionAssetError,
    MotionRequestBlocked,
    StudioService,
)


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


def _still_png() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (512, 512), (10, 20, 30, 255)).save(output, format="PNG")
    return output.getvalue()


def _motion_png() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (384, 96), (10, 20, 30, 255)).save(output, format="PNG")
    return output.getvalue()


async def _service_with_approved_subject(*, composition_available: bool = True):
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
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=_Dispatcher(),
        motion_generation_available=True,
        composition_generation_available=composition_available,
    )
    return service, store, jobs, assets, project


async def _settle_composition(service, store, jobs, assets, job):
    claimed = await jobs.claim(job.job_id)
    await jobs.complete(job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key))
    assets.assets[job.output_key] = _still_png()
    current = await store.get("project-1")
    return await service.settle_composition("project-1", job.job_id, current.revision, "user-1")


async def test_compose_dispatches_and_settles_a_pending_composition() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()

    job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    assert job.operation is JobOperation.COMPOSE_FRAME
    assert job.output_key.startswith("outputs/studio/project-1/compositions/")
    assert job.output_key.endswith(".png")
    assert job.payload["reference_keys"] == ["sources/studio/project-1/subjects/source.png"]

    settled = await _settle_composition(service, store, jobs, assets, job)
    composition = settled.manifest.composition.current
    assert composition is not None
    assert composition.approval is CompositionApprovalState.PENDING
    assert composition.parent_subject_revision_id == "subject-1"
    assert composition.artifact_key == job.output_key


def _encode(image: Image.Image, *, format: str) -> bytes:
    output = BytesIO()
    image.save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize(
    "malformed_bytes",
    [
        pytest.param(b"not a png at all", id="undecodable"),
        pytest.param(
            _encode(Image.new("RGB", (512, 512), (1, 2, 3)), format="JPEG"),
            id="opaque-jpeg",
        ),
        pytest.param(
            _encode(Image.new("RGB", (512, 512), (1, 2, 3)), format="PNG"),
            id="rgb-png-without-alpha",
        ),
    ],
)
async def test_settle_composition_rejects_a_malformed_or_opaque_output(
    malformed_bytes: bytes,
) -> None:
    """T3: settlement must decode and validate the composed bytes as an RGBA
    PNG before recording a CompositionRevision, or a broken provider output
    silently becomes an approvable still."""
    service, store, jobs, assets, project = await _service_with_approved_subject()
    job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    claimed = await jobs.claim(job.job_id)
    await jobs.complete(job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key))
    assets.assets[job.output_key] = malformed_bytes
    current = await store.get("project-1")

    with pytest.raises(MotionAssetError):
        await service.settle_composition("project-1", job.job_id, current.revision, "user-1")

    unchanged = await store.get("project-1")
    assert unchanged.manifest.composition.current is None


async def test_composition_generation_unavailable_raises() -> None:
    service, _, _, _, project = await _service_with_approved_subject(composition_available=False)
    with pytest.raises(CompositionGenerationUnavailable):
        await service.compose_opening_frame(
            "project-1", project.revision, "Ember at rest",
            CompositionModel.NANOBANANA_2_LITE, "user-1", "req-1",
        )


async def test_approve_composition_pins_the_current_revision() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    settled = await _settle_composition(service, store, jobs, assets, job)

    approved = await service.approve_composition("project-1", settled.revision, "user-1")

    assert approved.manifest.composition.current.approval is CompositionApprovalState.APPROVED


async def test_approve_composition_without_a_pending_still_is_blocked() -> None:
    service, _, _, _, project = await _service_with_approved_subject()
    with pytest.raises(CompositionApprovalBlocked):
        await service.approve_composition("project-1", project.revision, "user-1")


async def test_regenerate_supersedes_the_previous_composition() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    settled = await _settle_composition(service, store, jobs, assets, job)
    first_revision_id = settled.manifest.composition.current.revision_id
    approved = await service.approve_composition("project-1", settled.revision, "user-1")

    job2 = await service.regenerate_composition(
        "project-1", approved.revision, "Ember, brighter", CompositionModel.SEEDREAM_5_0_LITE,
        "user-1", "req-2",
    )
    claimed = await jobs.claim(job2.job_id)
    await jobs.complete(job2.job_id, claimed.claim_token, VideoJobResult(output_key=job2.output_key))
    assets.assets[job2.output_key] = _still_png()
    regenerated = await service.settle_composition("project-1", job2.job_id, approved.revision, "user-1")

    state = regenerated.manifest.composition
    assert state.current.revision_id != first_revision_id
    assert state.current.approval is CompositionApprovalState.PENDING
    previous = next(r for r in state.revisions if r.revision_id == first_revision_id)
    assert previous.approval is CompositionApprovalState.SUPERSEDED


async def test_shared_starting_pose_requires_an_approved_composition() -> None:
    service, _, _, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", kind=MotionKind.PRESET, preset=MotionPreset.IDLE),
        "user-1",
    )

    with pytest.raises(MotionRequestBlocked):
        await service.generate_motions(
            "project-1", project.revision, ("idle",), "user-1", "req-1",
            shared_starting_pose=True,
        )


async def test_shared_starting_pose_batch_binds_the_composed_still_to_every_candidate() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    for motion_id, preset in (("idle", MotionPreset.IDLE), ("walk", MotionPreset.WALK)):
        project = await service.add_motion(
            "project-1", project.revision,
            MotionSpecification(motion_id=motion_id, name=motion_id.title(), preset=preset),
            "user-1",
        )
    compose_job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    settled = await _settle_composition(service, store, jobs, assets, compose_job)
    approved = await service.approve_composition("project-1", settled.revision, "user-1")
    composition_revision_id = approved.manifest.composition.current.revision_id
    composition_key = approved.manifest.composition.current.artifact_key

    queued, failed = await service.generate_motions(
        "project-1", approved.revision, ("idle", "walk"), "user-1", "req-2",
        shared_starting_pose=True,
    )
    assert not failed
    assert len(queued) == 2
    for item in queued:
        assert item.operation.payload["start_frame_key"] == composition_key
        assert item.operation.payload["composition_revision_id"] == composition_revision_id

    current = await service.get_project("project-1", "user-1")
    for item in queued:
        job = item.operation
        claimed = await jobs.claim(job.job_id)
        await jobs.complete(job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key))
        assets.assets[job.output_key] = _motion_png()
        current = await service.settle_project_motion_candidate(
            "project-1", item.motion_id, job.job_id, current.revision, "user-1"
        )

    for motion_id in ("idle", "walk"):
        motion = next(m for m in current.manifest.motions if m.motion_id == motion_id)
        assert motion.candidates[-1].composition_revision_id == composition_revision_id


async def test_subject_change_invalidates_the_current_composition() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    job = await service.compose_opening_frame(
        "project-1", project.revision, "Ember at rest", CompositionModel.NANOBANANA_2_LITE,
        "user-1", "req-1",
    )
    settled = await _settle_composition(service, store, jobs, assets, job)
    approved = await service.approve_composition("project-1", settled.revision, "user-1")
    assert approved.manifest.composition.current is not None

    new_subject = SubjectRevision(
        revision_id="subject-2",
        artifact_key="sources/studio/project-1/subjects/source-2.png",
        sha256="b" * 64,
        width=1024,
        height=1024,
    )
    replaced = await store.append(
        "project-1", approved.revision, approved.manifest.with_subject(new_subject)
    )

    assert replaced.manifest.composition.current is None
    assert replaced.manifest.composition.revisions == ()
