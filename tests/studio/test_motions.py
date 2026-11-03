from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from magic_video_editor.app import create_app
from magic_video_editor.contracts import (
    AssetTooLarge,
    GpuGenerationResult,
    JobOperation,
    JobRecord,
    JobStatus,
    VideoJobResult,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.plugins.gpu_http import TerminalGpuError
from magic_video_editor.studio.limits import (
    DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
    UNLIMITED,
    OwnerLimits,
)
from magic_video_editor.studio.manifest import (
    DuplicateMotionIds,
    add_motions,
    approve_subject,
    create_project_manifest,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    Facing,
    FrameSlot,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    SubjectRevision,
)
from magic_video_editor.studio.routes import AddMotionRequest, OperationResponse
from magic_video_editor.studio.service import (
    MotionAssetError,
    MotionGenerationUnavailable,
    MotionLineageConflict,
    MotionRequestBlocked,
    OperationAlreadyTerminal,
    ProjectNotFound,
    StudioService,
    motion_spec_digest,
)


class _Dispatcher:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.job_ids: list[str] = []
        self.fail_once = fail_once

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("dispatch timed out")


class _Gpu:
    async def generate(self, request: object) -> GpuGenerationResult:
        return GpuGenerationResult(output_key=request.output_key)


class _RejectingGpu:
    async def generate(self, request: object) -> GpuGenerationResult:
        raise TerminalGpuError("provider rejected secret=sk-do-not-expose")


async def test_operation_view_keeps_all_live_work_and_bounds_terminal_history() -> None:
    service, _, jobs, _, _ = await _service_with_approved_subject()
    statuses = [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.COMPLETED]
    for index in range(31):
        job_id = f"active-{index:02d}"
        jobs.records[job_id] = JobRecord(
            job_id=job_id,
            idempotency_key=job_id,
            operation=JobOperation.GENERATE_MOTION,
            status=statuses[index % len(statuses)],
            source_key="sources/studio/project-1/subjects/source.png",
            output_key=f"outputs/studio/project-1/motions/idle/{job_id}.mp4",
            attempts=1,
            project_id="project-1",
            manifest_operation_id=job_id,
            payload={"motion_id": "idle"},
        )
    for index in range(120):
        job_id = f"terminal-{index:03d}"
        jobs.records[job_id] = JobRecord(
            job_id=job_id,
            idempotency_key=job_id,
            operation=JobOperation.GENERATE_MOTION,
            status=JobStatus.FAILED,
            source_key="sources/studio/project-1/subjects/source.png",
            output_key=f"outputs/studio/project-1/motions/idle/{job_id}.mp4",
            attempts=5,
            project_id="project-1",
            payload={"motion_id": "idle"},
        )

    operations = [
        listing.record for listing in await service.list_operations("project-1", "user-1")
    ]
    active = [item for item in operations if item.status is not JobStatus.FAILED]
    terminal = [item for item in operations if item.status is JobStatus.FAILED]

    assert len(active) == 31
    assert active[0].job_id == "active-00"
    assert [operation.job_id for operation in terminal] == [
        f"terminal-{index:03d}" for index in range(110, 120)
    ]


async def _service_with_approved_subject(
    *, concurrent_job_limit: int = DEFAULT_OWNER_CONCURRENT_JOB_LIMIT
) -> tuple[
    StudioService, MemoryStudioStore, MemoryJobStore, MemoryAssetStore, object
]:
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
    return (
        StudioService(
            store=store,
            assets=assets,
            jobs=jobs,
            dispatcher=_Dispatcher(),
            motion_generation_available=True,
            owner_limits=OwnerLimits(concurrent_job_limit=concurrent_job_limit),
        ),
        store,
        jobs,
        assets,
        project,
    )


def _motion_png() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(output, format="PNG")
    return output.getvalue()


async def test_completed_motion_is_added_without_auto_selection() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(
            motion_id="idle",
            name="Idle",
            kind=MotionKind.PRESET,
            preset=MotionPreset.IDLE,
            loop_policy="loop",
            required=True,
        ),
        "user-1",
    )
    settings = MotionGenerationSettings(
        preset="high", steps=28, seed=123, width=768, height=768,
        duration_seconds=6, provider_parameters={"motion_strength": 0.75},
    )
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1", settings=settings
    )
    project = await service.get_project("project-1", "user-1")
    assert job.payload["preset"] == "high"
    assert job.payload["steps"] == 28
    assert job.payload["seed"] == 123
    assert job.payload["width"] == 768
    assert job.payload["height"] == 768
    assert job.payload["duration_seconds"] == 6
    assert job.payload["provider_parameters"] == {"motion_strength": 0.75}
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    idle = next(m for m in settled.manifest.motions if m.motion_id == "idle")
    assert len(idle.candidates) == 1
    assert idle.selected_candidate_id is None
    assert idle.candidates[0].metadata is not None
    assert idle.candidates[0].metadata.decodable is True
    assert idle.candidates[0].generation_settings == settings
    # The stored bytes are a PNG sprite sheet even though the job's output key ends in .mp4;
    # media_kind must reflect what was measured, not the key extension.
    assert idle.candidates[0].artifact_key.endswith(".mp4")
    assert idle.candidates[0].metadata.media_kind is MediaKind.SHEET


async def test_saving_frame_edits_creates_and_selects_an_immutable_revision() -> None:
    service, store, _, _, project = await _service_with_approved_subject()
    source = MotionCandidate(
        candidate_id="source-a",
        artifact_key="outputs/studio/project-1/motions/idle/source-a.png",
        sha256="c" * 64,
        duration_ms=400,
        metadata=MotionFrameMetadata(
            decodable=True,
            media_kind=MediaKind.SHEET,
            frame_count=4,
            width=256,
            height=64,
            constant_geometry=True,
            has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
    )
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.with_motion(
            MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            )
        ).model_copy(
            update={
                "motions": (
                    project.manifest.with_motion(
                        MotionSpecification(
                            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                        )
                    ).motions[0].model_copy(
                        update={"candidates": (source,), "selected_candidate_id": "source-a"}
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
        ordered_frames=(FrameSlot(index=2), FrameSlot(index=0), FrameSlot(index=1)),
        frame_duration_ms=90,
        loop_policy=LoopPolicy.LOOP,
    )

    motion = revised.manifest.motions[0]
    assert motion.candidates[0] == source
    assert motion.selected_candidate is not None
    assert motion.selected_candidate.parent_revision_ids == ("source-a",)
    assert motion.selected_candidate.ordered_frames == (
        FrameSlot(index=2), FrameSlot(index=0), FrameSlot(index=1),
    )
    assert motion.selected_candidate.frame_duration_ms == 90
    assert motion.selected_candidate.loop_policy is LoopPolicy.LOOP


async def test_frame_holds_extend_the_derived_candidates_duration() -> None:
    service, store, _, _, project = await _service_with_approved_subject()
    source = MotionCandidate(
        candidate_id="source-b",
        artifact_key="outputs/studio/project-1/motions/idle/source-b.png",
        sha256="d" * 64,
        duration_ms=400,
        metadata=MotionFrameMetadata(
            decodable=True,
            media_kind=MediaKind.SHEET,
            frame_count=4,
            width=256,
            height=64,
            constant_geometry=True,
            has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
    )
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.with_motion(
            MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
        ).model_copy(
            update={
                "motions": (
                    project.manifest.with_motion(
                        MotionSpecification(
                            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                        )
                    ).motions[0].model_copy(
                        update={"candidates": (source,), "selected_candidate_id": "source-b"}
                    ),
                )
            }
        ),
    )

    revised = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-b", "user-1",
        ordered_frames=(
            FrameSlot(index=0), FrameSlot(index=1, hold=3), FrameSlot(index=2),
        ),
        frame_duration_ms=100, loop_policy=LoopPolicy.LOOP,
    )

    candidate = revised.manifest.motions[0].selected_candidate
    assert candidate is not None
    # (1 + 3 + 1) held frames at 100ms each.
    assert candidate.duration_ms == 500


async def test_flip_horizontal_toggles_facing_on_a_derived_candidate() -> None:
    service, store, _, _, project = await _service_with_approved_subject()
    source = MotionCandidate(
        candidate_id="source-a",
        artifact_key="outputs/studio/project-1/motions/idle/source-a.png",
        sha256="c" * 64,
        duration_ms=400,
        metadata=MotionFrameMetadata(
            decodable=True,
            media_kind=MediaKind.SHEET,
            frame_count=4,
            width=256,
            height=64,
            constant_geometry=True,
            has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
    )
    assert source.facing is Facing.RIGHT
    project = await store.append(
        "project-1",
        project.revision,
        project.manifest.with_motion(
            MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
        ).model_copy(
            update={
                "motions": (
                    project.manifest.with_motion(
                        MotionSpecification(
                            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                        )
                    ).motions[0].model_copy(
                        update={"candidates": (source,), "selected_candidate_id": "source-a"}
                    ),
                )
            }
        ),
    )

    flipped = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-a", "user-1",
        ordered_frames=(FrameSlot(index=0), FrameSlot(index=1), FrameSlot(index=2), FrameSlot(index=3)), frame_duration_ms=100,
        loop_policy=LoopPolicy.LOOP, flip_horizontal=True,
    )
    assert flipped.manifest.motions[0].selected_candidate.facing is Facing.LEFT

    flipped_back = await service.create_motion_revision(
        "project-1", flipped.revision,
        "idle",
        flipped.manifest.motions[0].selected_candidate_id,
        "user-1",
        ordered_frames=(FrameSlot(index=0), FrameSlot(index=1), FrameSlot(index=2), FrameSlot(index=3)), frame_duration_ms=100,
        loop_policy=LoopPolicy.LOOP, flip_horizontal=True,
    )
    assert flipped_back.manifest.motions[0].selected_candidate.facing is Facing.RIGHT


async def test_reconciliation_integrates_all_completed_motions_with_fresh_revisions() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    for motion_id in ("idle", "wave"):
        project = await service.add_motion(
            "project-1", project.revision,
            MotionSpecification(motion_id=motion_id, name=motion_id.title(), kind="custom", prompt=f"{motion_id} cleanly"),
            "user-1",
        )
    for motion_id in ("idle", "wave"):
        job = await service.generate_motion("project-1", project.revision, motion_id, "user-1")
        project = await service.get_project("project-1", "user-1")
        claimed = await jobs.claim(job.job_id)
        assert claimed is not None
        await jobs.complete(job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key))
        assets.assets[job.output_key] = _motion_png()

    await service.reconcile_project_operations("project-1", "user-1")

    reconciled = await store.get("project-1")
    assert reconciled is not None
    assert [len(motion.candidates) for motion in reconciled.manifest.motions] == [1, 1]
    assert not await service.list_operations("project-1", "user-1")


async def test_reconciliation_records_why_a_completed_job_could_not_settle() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    # No asset was ever written, so settlement raises MotionAssetError.
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )

    await service.reconcile_project_operations("project-1", "user-1")

    record = await jobs.get(job.job_id)
    assert record is not None
    assert record.status is JobStatus.COMPLETED
    assert record.settlement_note == "completed motion artifact is missing"
    listings = await service.list_operations("project-1", "user-1")
    listing = next(item for item in listings if item.record.job_id == job.job_id)
    assert not listing.settled
    assert (
        OperationResponse.from_listing(listing).settlement_blocked
        == "completed motion artifact is missing"
    )


async def test_selecting_candidate_does_not_replace_another_motion_selection() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    for motion_id in ("idle", "walk"):
        project = await service.add_motion(
            "project-1",
            project.revision,
            MotionSpecification(
                motion_id=motion_id,
                name=motion_id.title(),
                kind="custom",
                prompt=f"{motion_id} cleanly",
                semantic_slot=motion_id,
            ),
            "user-1",
        )
        job = await service.generate_motion(
            "project-1", project.revision, motion_id, "user-1"
        )
        project = await service.get_project("project-1", "user-1")
        claimed = await jobs.claim(job.job_id)
        assert claimed is not None
        await jobs.complete(
            job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
        )
        assets.assets[job.output_key] = _motion_png()
        project = await service.settle_motion_candidate(job.job_id, "user-1")
        candidate_id = (
            next(m for m in project.manifest.motions if m.motion_id == motion_id)
            .candidates[0]
            .candidate_id
        )
        project = await service.select_candidate(
            "project-1", project.revision, motion_id, candidate_id, "user-1"
        )

    assert all(m.selected_candidate_id for m in project.manifest.motions)


async def test_settlement_lands_the_take_after_a_subject_replacement() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    replacement = SubjectRevision(
        revision_id="subject-2",
        artifact_key="sources/studio/project-1/subjects/two.png",
        sha256="b" * 64,
        width=1024,
        height=1024,
    )
    await store.append(
        "project-1", project.revision, project.manifest.with_subject(replacement)
    )

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    candidates = settled.manifest.motions[0].candidates
    assert [candidate.subject_revision_id for candidate in candidates] == ["subject-1"]


async def test_settlement_lands_the_take_after_the_subject_is_reapproved() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    replacement = SubjectRevision(
        revision_id="subject-2",
        artifact_key="sources/studio/project-1/subjects/two.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    changed = approve_subject(
        project.manifest.with_subject(replacement),
        revision_id="subject-2",
        assessment_id="assessment-2",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    await store.append("project-1", project.revision, changed)

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    assert settled.manifest.motions[0].candidates[0].subject_revision_id == "subject-1"


async def test_settlement_rejects_result_output_key_mismatch() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    mismatched_key = "outputs/studio/project-1/motions/idle/untrusted.mp4"
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=mismatched_key)
    )
    assets.assets[mismatched_key] = _motion_png()

    with pytest.raises(MotionLineageConflict, match="output key"):
        await service.settle_motion_candidate(job.job_id, "user-1")


async def test_settlement_lands_the_take_after_the_motion_spec_is_edited() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(
            motion_id="idle", name="Idle revised", preset=MotionPreset.IDLE
        ),
        "user-1",
    )

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    motion = settled.manifest.motions[0]
    assert motion.specification.name == "Idle revised"
    assert [candidate.candidate_id for candidate in motion.candidates] == [
        job.manifest_operation_id
    ]


async def test_settlement_reports_missing_and_undecodable_artifacts() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )

    with pytest.raises(MotionAssetError) as missing:
        await service.settle_motion_candidate(job.job_id, "user-1")
    assert missing.value.code == "motion_asset_missing"

    assets.assets[job.output_key] = b"not an image or video"
    with pytest.raises(MotionAssetError) as undecodable:
        await service.settle_motion_candidate(job.job_id, "user-1")
    assert undecodable.value.code == "motion_asset_undecodable"


class _OversizeAssets(MemoryAssetStore):
    async def read_limited(self, key: str, max_bytes: int) -> bytes:
        raise AssetTooLarge(key, max_bytes, max_bytes + 1)


async def test_settlement_reports_oversize_artifact() -> None:
    _, store, jobs, _, project = await _service_with_approved_subject()
    service = StudioService(
        store=store,
        assets=_OversizeAssets(),
        jobs=jobs,
        dispatcher=_Dispatcher(),
        motion_generation_available=True,
    )
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )

    with pytest.raises(MotionAssetError) as oversize:
        await service.settle_motion_candidate(job.job_id, "user-1")
    assert oversize.value.code == "motion_asset_too_large"


async def test_motion_generation_requires_current_approved_subject() -> None:
    store = MemoryStudioStore()
    project = await store.create(
        create_project_manifest("project-1", "Ember", "user-1")
    )
    service = StudioService(
        store=store,
        assets=MemoryAssetStore(),
        jobs=MemoryJobStore(),
        dispatcher=_Dispatcher(),
        motion_generation_available=True,
    )
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    with pytest.raises(MotionRequestBlocked):
        await service.generate_motion("project-1", project.revision, "idle", "user-1")


def test_custom_motion_requires_name_and_prompt() -> None:
    with pytest.raises(ValueError):
        MotionSpecification(motion_id="attack", name="", kind="custom", prompt="")

    with pytest.raises(ValueError, match="preset"):
        MotionSpecification(
            motion_id="attack",
            name="Attack",
            kind="custom",
            prompt="attack",
            preset=MotionPreset.IDLE,
        )


def test_preset_motion_requires_supported_explicit_preset() -> None:
    with pytest.raises(ValueError, match="preset"):
        MotionSpecification(motion_id="idle", name="Idle", kind="preset")


def test_custom_motion_route_payload_requires_prompt() -> None:
    with pytest.raises(ValidationError):
        AddMotionRequest(
            expected_revision=1,
            motion_id="attack",
            name="Attack",
            kind="custom",
        )


async def test_generate_dispatch_timeout_retry_reuses_one_job() -> None:
    _, store, jobs, assets, project = await _service_with_approved_subject()
    dispatcher = _Dispatcher(fail_once=True)
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=dispatcher,
        motion_generation_available=True,
    )
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )

    with pytest.raises(TimeoutError):
        await service.generate_motion(
            "project-1", project.revision, "idle", "user-1", "request-1"
        )
    project = await service.get_project("project-1", "user-1")
    retried = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1", "request-1"
    )
    project = await service.get_project("project-1", "user-1")

    assert len(jobs.records) == 1
    assert dispatcher.job_ids == [retried.job_id, retried.job_id]


async def test_generation_capability_gate_precedes_persistence_and_dispatch() -> None:
    _, store, jobs, assets, project = await _service_with_approved_subject()
    dispatcher = _Dispatcher()
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=dispatcher,
        motion_generation_available=False,
    )
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )

    with pytest.raises(MotionGenerationUnavailable):
        await service.generate_motion(
            "project-1", project.revision, "idle", "user-1", "request-1"
        )
    assert jobs.records == {}
    assert dispatcher.job_ids == []


async def test_settlement_rejects_forged_subject_key_even_with_same_project() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1", "request-1"
    )
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    forged = dict(jobs.records[job.job_id].payload)
    forged["subject_key"] = "sources/studio/project-1/subjects/unapproved.png"
    jobs.records[job.job_id] = replace(jobs.records[job.job_id], payload=forged)

    with pytest.raises(MotionLineageConflict, match="ledger source"):
        await service.settle_motion_candidate(job.job_id, "user-1")


def test_full_studio_generate_execute_and_owner_scoped_settle_flow() -> None:
    import asyncio

    service, _, jobs, assets, project = asyncio.run(_service_with_approved_subject())
    project = asyncio.run(
        service.add_motion(
            "project-1",
            project.revision,
            MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            "user-1",
        )
    )
    job = asyncio.run(
        service.generate_motion(
            "project-1", project.revision, "idle", "user-1", "request-1"
        )
    )
    project = asyncio.run(service.get_project("project-1", "user-1"))
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    runtime.gpu = _Gpu()
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    jobs.records["old-failure"] = JobRecord(
        job_id="old-failure",
        idempotency_key="old-failure",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.FAILED,
        source_key=job.source_key,
        output_key="outputs/studio/project-1/motions/idle/old.mp4",
        attempts=5,
        project_id="project-1",
        manifest_operation_id="old-operation",
        payload={"motion_id": "idle"},
    )
    before_retry = client.get(
        "/studio/projects/project-1/operations",
        headers={"X-Studio-Actor-Id": "user-1"},
    ).json()
    assert [operation["job_id"] for operation in before_retry] == [
        job.job_id,
        "old-failure",
    ]
    assert before_retry[0]["status"] == "queued"

    assert client.post(f"/tasks/jobs/{job.job_id}").json()["status"] == "completed"
    assets.assets[job.output_key] = _motion_png()
    operation = client.get(
        "/studio/projects/project-1/operations",
        headers={"X-Studio-Actor-Id": "user-1"},
    ).json()[0]
    assert operation["progress_percent"] is None
    assert operation["stage"] == "awaiting_settlement"
    assert operation["failure_explanation"] is None
    assert operation["retryable"] is False
    response = client.post(
        f"/studio/projects/project-1/motions/idle/operations/{job.job_id}/settle",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={"expected_revision": project.revision},
    )

    assert response.status_code == 200
    candidate = response.json()["manifest"]["motions"][0]["candidates"][0]
    assert candidate["assessment"]["policy_version"] == "motion-policy-v1"
    settled_operations = client.get(
        "/studio/projects/project-1/operations",
        headers={"X-Studio-Actor-Id": "user-1"},
    ).json()
    assert [operation["job_id"] for operation in settled_operations] == [
        "old-failure"
    ]
    # Activity keeps the finished job as history, but reports it as settled so a
    # client knows there is nothing left to reconcile and can stop polling.
    activity = {
        operation["job_id"]: operation["stage"]
        for operation in client.get(
            "/studio/projects/project-1/activity",
            headers={"X-Studio-Actor-Id": "user-1"},
        ).json()["operations"]
    }
    assert activity[job.job_id] == "settled"
    assert activity["old-failure"] == "failed"


def test_worker_persists_safe_nonretryable_provider_rejection() -> None:
    import asyncio

    service, _, jobs, assets, project = asyncio.run(_service_with_approved_subject())
    project = asyncio.run(
        service.add_motion(
            "project-1",
            project.revision,
            MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            "user-1",
        )
    )
    job = asyncio.run(
        service.generate_motion(
            "project-1", project.revision, "idle", "user-1", "request-rejected"
        )
    )
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    runtime.gpu = _RejectingGpu()
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    assert client.post(f"/tasks/jobs/{job.job_id}").json()["status"] == "failed"
    operation = client.get(
        "/studio/projects/project-1/operations",
        headers={"X-Studio-Actor-Id": "user-1"},
    ).json()[0]

    assert operation["failure_code"] == "invalid_input"
    assert operation["failure_explanation"] == (
        "The provider could not process this motion. Inspect the input and settings."
    )
    assert operation["retryable"] is False
    assert "sk-do-not-expose" not in str(operation)


@pytest.mark.parametrize(
    "invalid", ["../idle", "idle/run", "idle run", ".idle", "idle\nrun"]
)
def test_motion_identifiers_reject_unsafe_key_segments(invalid: str) -> None:
    with pytest.raises(ValidationError):
        MotionSpecification(motion_id=invalid, name="Idle", preset=MotionPreset.IDLE)


async def test_project_history_lists_manifest_revisions_and_terminal_operations() -> None:
    service, store, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.PRESET,
            preset=MotionPreset.IDLE, loop_policy="loop", required=True,
        ),
        "user-1",
    )
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1"
    )
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    await service.settle_motion_candidate(job.job_id, "user-1")

    failed_job = await service.generate_motion(
        "project-1", (await store.get("project-1")).revision, "idle", "user-1"
    )
    project = await service.get_project("project-1", "user-1")
    failed_claim = await jobs.claim(failed_job.job_id)
    assert failed_claim is not None
    await jobs.fail(
        failed_job.job_id, failed_claim.claim_token, "boom",
        failure_message="Generation failed before a result was created.",
    )

    history = await service.list_project_history("project-1", "user-1")

    revisions = [entry.revision for entry in history if entry.revision is not None]
    assert revisions == sorted(revisions)
    summaries = [entry.summary for entry in history]
    assert "Created the project" in summaries
    assert "Added motion Idle" in summaries
    assert "Generated a candidate for Idle" in summaries
    failed_entries = [entry for entry in history if entry.failed]
    assert len(failed_entries) == 1
    assert failed_entries[0].summary == "Generation failed before a result was created."
    assert failed_entries[0].revision is None


# --- T5: preset motion library -------------------------------------------------


async def test_adding_a_preset_motion_compiles_its_authored_prompt_and_loop_policy() -> None:
    service, _, _, _, project = await _service_with_approved_subject()

    stored = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="attack", name="Attack", preset=MotionPreset.ATTACK),
        "user-1",
    )

    attack = next(m for m in stored.manifest.motions if m.motion_id == "attack")
    assert attack.specification.semantic_slot == "attack"
    assert attack.specification.loop_policy == LoopPolicy.ONCE
    assert attack.specification.prompt is not None
    assert "The character" in attack.specification.prompt
    assert "Ember" not in attack.specification.prompt
    assert "swings a weapon" in attack.specification.prompt
    assert attack.specification.action == "swings a weapon"


async def test_a_preset_motions_own_action_line_composes_into_its_prompt() -> None:
    """The operator's "what happens" line replaces the preset default in the compiled prompt,
    and an empty line falls back to the default rather than composing a gap."""
    service, _, _, _, project = await _service_with_approved_subject()

    stored = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(
            motion_id="attack",
            name="Attack",
            preset=MotionPreset.ATTACK,
            action="draws and fires the bow",
        ),
        "user-1",
    )
    attack = next(m for m in stored.manifest.motions if m.motion_id == "attack")
    assert attack.specification.action == "draws and fires the bow"
    assert attack.specification.prompt.startswith("The character draws and fires the bow:")
    assert "returns to the initial ready stance" in attack.specification.prompt

    blank = await service.add_motion(
        "project-1",
        stored.revision,
        MotionSpecification(
            motion_id="guard", name="Guard", preset=MotionPreset.GUARD, action="   "
        ),
        "user-1",
    )
    guard = next(m for m in blank.manifest.motions if m.motion_id == "guard")
    assert guard.specification.action == "raises a guard"
    assert guard.specification.prompt is not None
    assert "The character raises a guard" in guard.specification.prompt


async def test_editing_the_action_line_changes_the_spec_digest_without_blocking() -> None:
    """Existing takes stay put; their recorded spec digest simply no longer matches the
    motion's current one, which is what marks them as made from an older spec."""
    service, _, _, _, project = await _service_with_approved_subject()
    stored = await service.add_motion(
        "project-1",
        project.revision,
        MotionSpecification(motion_id="attack", name="Attack", preset=MotionPreset.ATTACK),
        "user-1",
    )
    before = motion_spec_digest(stored.manifest.motions[-1].specification)

    edited = await service.update_motion(
        "project-1",
        stored.revision,
        "attack",
        stored.manifest.motions[-1].specification.model_copy(
            update={"action": "draws and fires the bow"}
        ),
        "user-1",
    )

    after = motion_spec_digest(edited.manifest.motions[-1].specification)
    assert after != before


async def test_adding_the_recommended_six_presets_all_compile_successfully() -> None:
    service, _, _, _, project = await _service_with_approved_subject()
    presets = [
        ("idle", MotionPreset.IDLE), ("walk", MotionPreset.WALK),
        ("attack", MotionPreset.ATTACK), ("guard", MotionPreset.GUARD),
        ("cast", MotionPreset.CAST), ("hurt", MotionPreset.HURT),
    ]
    for motion_id, preset in presets:
        project = await service.add_motion(
            "project-1", project.revision,
            MotionSpecification(motion_id=motion_id, name=motion_id.title(), preset=preset),
            "user-1",
        )
    assert [m.motion_id for m in project.manifest.motions] == [p[0] for p in presets]
    assert all(m.specification.prompt for m in project.manifest.motions)


# --- T6: batch add and batch generate ------------------------------------------


def test_add_motions_batch_transition_appends_every_motion() -> None:
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    specs = (
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        MotionSpecification(motion_id="walk", name="Walk", preset=MotionPreset.WALK),
    )

    updated = add_motions(manifest, specs)

    assert [m.motion_id for m in updated.motions] == ["idle", "walk"]


def test_add_motions_batch_rejects_duplicates_of_an_existing_motion() -> None:
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    manifest = manifest.with_motion(
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
    )
    specs = (
        MotionSpecification(motion_id="idle", name="Idle again", preset=MotionPreset.IDLE),
        MotionSpecification(motion_id="walk", name="Walk", preset=MotionPreset.WALK),
    )

    with pytest.raises(DuplicateMotionIds) as excinfo:
        add_motions(manifest, specs)
    assert excinfo.value.motion_ids == ("idle",)


async def test_generate_motions_batch_isolates_one_dispatch_failure() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    for motion_id, preset in (("idle", MotionPreset.IDLE), ("walk", MotionPreset.WALK)):
        project = await service.add_motion(
            "project-1", project.revision,
            MotionSpecification(motion_id=motion_id, name=motion_id.title(), preset=preset),
            "user-1",
        )
    # Swap in a dispatcher that fails on its first enqueue only.
    service._dispatcher = _Dispatcher(fail_once=True)

    queued, failed = await service.generate_motions(
        "project-1", project.revision, ("idle", "walk"), "user-1", "req-1",
    )
    project = await service.get_project("project-1", "user-1")

    assert len(queued) == 1
    assert len(failed) == 1
    assert failed[0].motion_id in {"idle", "walk"}
    assert failed[0].code == "dispatch_failed"
    # The surviving motion still queued.
    assert queued[0].motion_id in {"idle", "walk"}
    assert queued[0].motion_id != failed[0].motion_id


async def test_generate_motions_batch_reports_a_missing_motion_without_failing_others() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )

    queued, failed = await service.generate_motions(
        "project-1", project.revision, ("idle", "nonexistent"), "user-1", "req-1",
    )
    project = await service.get_project("project-1", "user-1")

    assert [q.motion_id for q in queued] == ["idle"]
    assert [f.motion_id for f in failed] == ["nonexistent"]
    assert failed[0].code == "motion_not_found"


async def test_generate_motions_batch_is_idempotent_on_request_id() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )

    first_queued, _ = await service.generate_motions(
        "project-1", project.revision, ("idle",), "user-1", "req-1",
    )
    project = await service.get_project("project-1", "user-1")
    second_queued, _ = await service.generate_motions(
        "project-1", project.revision, ("idle",), "user-1", "req-1",
    )
    project = await service.get_project("project-1", "user-1")

    assert first_queued[0].operation.job_id == second_queued[0].operation.job_id


async def test_cancel_motion_operation_marks_queued_job_cancelled_and_retryable() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")

    cancelled = await service.cancel_motion_operation("project-1", "idle", job.job_id, "user-1")

    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.retryable is True
    stored = await jobs.get(job.job_id)
    assert stored is not None
    assert stored.status is JobStatus.CANCELLED


async def test_cancel_motion_operation_on_terminal_job_is_rejected_with_its_state() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _motion_png()
    await service.settle_motion_candidate(job.job_id, "user-1")

    with pytest.raises(OperationAlreadyTerminal) as excinfo:
        await service.cancel_motion_operation("project-1", "idle", job.job_id, "user-1")

    assert excinfo.value.state is JobStatus.COMPLETED


async def test_cancel_motion_operation_rejects_a_foreign_actor() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")

    with pytest.raises(ProjectNotFound):
        await service.cancel_motion_operation("project-1", "idle", job.job_id, "someone-else")


async def test_cancelled_job_is_never_settled_by_reconcile_and_rejects_late_completion() -> None:
    """A cancelled job must never settle: reconcile skips it, and the fencing already
    guarding claim/complete rejects a completion that races the cancel (T13)."""
    service, _, jobs, assets, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None

    await service.cancel_motion_operation("project-1", "idle", job.job_id, "user-1")

    # A completion racing the cancel is fenced off by the claim token: the job store
    # already forgot the claim once cancelled, so it cannot be completed.
    accepted = await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assert accepted is False

    # Even if it somehow completed, reconcile only settles COMPLETED jobs; a cancelled
    # job's status is never COMPLETED, so reconcile leaves it alone.
    await service.reconcile_project_operations("project-1", "user-1")
    current = await service.get_project("project-1", "user-1")
    assert current is not None
    idle = next(m for m in current.manifest.motions if m.motion_id == "idle")
    assert idle.candidates == ()


async def test_cancel_all_operations_cancels_live_jobs_and_reports_skipped() -> None:
    service, _, jobs, assets, project = await _service_with_approved_subject()
    for motion_id, preset in (("idle", MotionPreset.IDLE), ("walk", MotionPreset.WALK)):
        project = await service.add_motion(
            "project-1", project.revision,
            MotionSpecification(motion_id=motion_id, name=motion_id.title(), preset=preset),
            "user-1",
        )
    queued_job = await service.generate_motion("project-1", project.revision, "idle", "user-1")
    project = await service.get_project("project-1", "user-1")
    running_job = await service.generate_motion("project-1", project.revision, "walk", "user-1")
    project = await service.get_project("project-1", "user-1")
    claimed = await jobs.claim(running_job.job_id)
    assert claimed is not None

    result = await service.cancel_all_operations("project-1", "user-1")

    assert set(result.cancelled) == {queued_job.job_id, running_job.job_id}
    assert result.skipped == ()
    queued_after = await jobs.get(queued_job.job_id)
    running_after = await jobs.get(running_job.job_id)
    assert queued_after is not None and queued_after.status is JobStatus.CANCELLED
    assert running_after is not None and running_after.status is JobStatus.CANCELLED


async def test_cancel_all_operations_reports_no_work_when_nothing_is_live() -> None:
    service, _, jobs, _, project = await _service_with_approved_subject()

    result = await service.cancel_all_operations("project-1", "user-1")

    assert result.cancelled == ()
    assert result.skipped == ()


async def test_activity_operations_paginate_newest_first_with_a_cursor() -> None:
    # Paging needs more history than the per-owner concurrency cap allows at once.
    service, _, jobs, _, project = await _service_with_approved_subject(
        concurrent_job_limit=UNLIMITED
    )
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job_ids: list[str] = []
    for index in range(25):
        job = await service.generate_motion(
            "project-1", project.revision, "idle", "user-1", f"req-{index}",
        )
        project = await service.get_project("project-1", "user-1")
        jobs.records[job.job_id] = replace(
            jobs.records[job.job_id],
            created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        )
        job_ids.append(job.job_id)

    first_page = await service.list_activity_operations("project-1", "user-1", limit=20)

    assert len(first_page.operations) == 20
    assert [item.record.job_id for item in first_page.operations] == list(
        reversed(job_ids[5:])
    )
    assert first_page.next_cursor is not None

    second_page = await service.list_activity_operations(
        "project-1", "user-1", cursor=first_page.next_cursor, limit=20
    )

    assert [item.record.job_id for item in second_page.operations] == list(
        reversed(job_ids[:5])
    )
    assert second_page.next_cursor is None


class _HardCappedProjectStore(MemoryJobStore):
    """A JobStore double reproducing PostgresJobStore's real contract: an
    unpaged (`limit=None`) call is hard-capped at `hard_cap` rows, while a
    paged call (`limit` given) pages properly past that cap using the cursor.
    """

    def __init__(self, *, hard_cap: int) -> None:
        super().__init__()
        self.hard_cap = hard_cap
        self.unpaged_calls = 0
        self.paged_calls = 0

    async def list_for_project(
        self, project_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> tuple[JobRecord, ...]:
        if limit is None:
            self.unpaged_calls += 1
            capped = await super().list_for_project(project_id, limit=self.hard_cap + 1)
            return capped[-self.hard_cap:]
        self.paged_calls += 1
        return await super().list_for_project(project_id, cursor=cursor, limit=limit)


async def test_activity_operations_page_past_a_hard_capped_unpaged_query() -> None:
    """T11: the History cursor API must reach records beyond any single-query
    row cap the durable store applies to its legacy unpaged listing --
    PostgresJobStore hard-limits that query to the newest 100 rows."""
    store = MemoryStudioStore()
    jobs = _HardCappedProjectStore(hard_cap=10)
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=64, height=64,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id="user-1", policy_version="subject-v1",
    )
    project = await store.create(manifest)
    service = StudioService(
        store=store, assets=MemoryAssetStore(), jobs=jobs, dispatcher=_Dispatcher(),
        motion_generation_available=True,
        owner_limits=OwnerLimits(concurrent_job_limit=UNLIMITED),
    )
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE),
        "user-1",
    )
    job_ids: list[str] = []
    for index in range(15):
        job = await service.generate_motion(
            "project-1", project.revision, "idle", "user-1", f"req-{index}",
        )
        project = await service.get_project("project-1", "user-1")
        jobs.records[job.job_id] = replace(
            jobs.records[job.job_id],
            created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        )
        job_ids.append(job.job_id)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await service.list_activity_operations(
            "project-1", "user-1", cursor=cursor, limit=5
        )
        seen.extend(item.record.job_id for item in page.operations)
        cursor = page.next_cursor
        if cursor is None:
            break

    # All 15 records are reachable even though the store's unpaged query
    # would only ever surface the newest 10.
    assert set(seen) == set(job_ids)
    assert jobs.paged_calls > 0
    assert job_ids[0] in seen


async def test_binding_an_external_subject_keeps_prompts_anonymous() -> None:
    """Subject identity changes cannot leak names into preset prompts or rewrite custom prompts."""
    from magic_video_editor.studio.models import ExternalSubject, PromptSource

    service, _, _, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="walk", name="Walk", preset=MotionPreset.WALK),
        "user-1",
    )
    hand_edited = MotionSpecification(
        motion_id="attack", name="Attack", preset=MotionPreset.ATTACK,
        prompt="A hand-authored, deliberately different attack description.",
        prompt_source=PromptSource.CUSTOM,
    )
    project = await service.add_motion("project-1", project.revision, hand_edited, "user-1")
    walk_before = next(
        m for m in project.manifest.motions if m.motion_id == "walk"
    ).specification.prompt
    assert walk_before is not None and "The character" in walk_before
    assert "Ember" not in walk_before

    updated = await service.update_external_subject(
        "project-1", project.revision,
        ExternalSubject(
            system="acme-engine", entity_kind="character",
            entity_id="char-1", display_name="Ser Ember",
        ),
        "user-1",
    )

    walk_after = next(
        m for m in updated.manifest.motions if m.motion_id == "walk"
    ).specification.prompt
    attack_after = next(
        m for m in updated.manifest.motions if m.motion_id == "attack"
    ).specification.prompt
    assert walk_after == walk_before
    assert "Ember" not in walk_after
    assert attack_after == "A hand-authored, deliberately different attack description."
