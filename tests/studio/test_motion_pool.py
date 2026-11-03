import io
from datetime import UTC, datetime

from outbound_gpu_worker_pool.contracts import (
    JobRecord as PoolJobRecord,
)
from outbound_gpu_worker_pool.contracts import (
    JobStatus as PoolJobStatus,
)
from outbound_gpu_worker_pool.contracts import (
    JobSubmission as PoolJobSubmission,
)
from PIL import Image

from magic_video_editor.contracts import JobStatus
from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
)
from magic_video_editor.studio.service import (
    MOTION_POOL_CAPABILITY_ID,
    StudioService,
    _motion_pool_payload,
)

PROJECT = "project-1"


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class _Pool:
    def __init__(self) -> None:
        self.submissions: list[PoolJobSubmission] = []
        self.records: dict[str, PoolJobRecord] = {}
        self.fail_submit = False
        self.cancelled: list[str] = []

    async def submit(self, submission: PoolJobSubmission) -> PoolJobRecord:
        if self.fail_submit:
            raise RuntimeError("pool unreachable")
        self.submissions.append(submission)
        record = self._record(
            submission.job_id, PoolJobStatus.QUEUED, output_key=submission.output_key
        )
        self.records[submission.job_id] = record
        return record

    async def get(self, job_id: str) -> PoolJobRecord | None:
        return self.records.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True

    def _record(
        self,
        job_id: str,
        status: PoolJobStatus,
        *,
        output_key: str = "",
        error: str | None = None,
    ) -> PoolJobRecord:
        return PoolJobRecord(
            job_id=job_id,
            idempotency_key=f"{job_id}-key",
            capability_id=MOTION_POOL_CAPABILITY_ID,
            status=status,
            input_keys=(),
            output_key=output_key or f"outputs/studio/{PROJECT}/motions/idle/{job_id}.mp4",
            attempts=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            error=error,
        )


def _pool_record(job_id: str, status: PoolJobStatus, output_key: str) -> PoolJobRecord:
    return PoolJobRecord(
        job_id=job_id,
        idempotency_key=f"{job_id}-key",
        capability_id=MOTION_POOL_CAPABILITY_ID,
        status=status,
        input_keys=(),
        output_key=output_key,
        attempts=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _motion_payload() -> dict:
    return {
        "subject_key": "sources/studio/project-1/subjects/source.png",
        "motion_id": "idle",
        "prompt": "subtle breathing idle",
        "loop": True,
        "width": 768,
        "height": 768,
        "duration_seconds": 6,
        "seed": 123,
        "preset": "high",
        "steps": 28,
        "provider_parameters": {},
    }


async def _service_with_motion(
    pool: _Pool | None = None,
    enrolment: object | None = None,
) -> tuple[StudioService, MemoryJobStore, _Dispatcher, object]:
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    dispatcher = _Dispatcher()
    from magic_video_editor.studio.manifest import (
        approve_subject,
        create_project_manifest,
    )
    from magic_video_editor.studio.models import (
        ApprovalState,
        SubjectRevision,
    )

    manifest = create_project_manifest(PROJECT, "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    assert approved.subject.approval.state is ApprovalState.APPROVED
    project = await store.create(approved)
    manifest = await store.get(PROJECT)
    assert manifest is not None
    service = StudioService(
        store=store,
        assets=MemoryAssetStore(),
        jobs=jobs,
        dispatcher=dispatcher,
        motion_generation_available=True,
        composition_generation_available=True,
        motion_pool=pool,
        enrolment=enrolment,
    )
    _ = manifest
    return service, jobs, dispatcher, project


async def test_motion_dispatch_submits_a_pool_job_without_a_cloud_task() -> None:
    pool = _Pool()
    service, jobs, dispatcher, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
        duration_seconds=6,
    )

    job = await service.generate_motion(
        PROJECT, project.revision, "idle", "user-1", settings=settings
    )
    project = await service.get_project(PROJECT, 'user-1')

    assert dispatcher.job_ids == []
    assert len(pool.submissions) == 1
    submission = pool.submissions[0]
    assert submission.job_id == job.job_id
    assert submission.idempotency_key == job.idempotency_key
    assert submission.capability_id == MOTION_POOL_CAPABILITY_ID
    assert submission.output_key == job.output_key
    assert submission.input_keys == ()
    assert "images" not in submission.payload
    assert submission.payload["length"] == 144
    assert submission.payload["steps"] == 28
    assert submission.payload["seed"] == 123
    assert (await jobs.get(job.job_id)).status is JobStatus.QUEUED  # type: ignore[union-attr]
    # Untenanted, because this owner has attached no machine: the shared house
    # machines must be able to take it. Binding it to an owner with no hardware
    # would leave it leasable by nobody, which is what broke production when
    # ownership first shipped.
    assert submission.tenant_id is None
    assert submission.requirements.min_vram_mb == 24_000


async def test_a_job_is_bound_to_its_owner_once_they_have_a_machine() -> None:
    """Affinity: with hardware attached, their work runs on their hardware."""
    from datetime import UTC, datetime

    from magic_video_editor.studio.enrolment import (
        MemoryStudioEnrolmentStore,
        generate_pairing_token,
        hash_token,
        pairing_token_expiry,
    )

    enrolment = MemoryStudioEnrolmentStore()
    token = generate_pairing_token()
    await enrolment.mint_pairing_token(
        owner_id="user-1",
        token_hash=hash_token(token),
        expires_at=pairing_token_expiry(),
    )
    await enrolment.redeem_pairing_token(
        token_hash=hash_token(token),
        worker_id="their-box",
        label="Their box",
        credential_hash=hash_token("mvsw_" + "z" * 32),
        now=datetime.now(UTC),
    )

    pool = _Pool()
    service, _jobs, _dispatcher, project = await _service_with_motion(pool, enrolment)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
        preset="high", steps=28, seed=123, width=768, height=768, duration_seconds=6
    )

    await service.generate_motion(
        PROJECT, project.revision, "idle", "user-1", settings=settings
    )

    assert pool.submissions[0].tenant_id == "user-1"


async def test_pool_submission_failure_falls_back_to_the_cloud_task() -> None:
    pool = _Pool()
    pool.fail_submit = True
    service, _, dispatcher, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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

    job = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')

    assert pool.submissions == []
    assert dispatcher.job_ids == [job.job_id]


async def test_listing_syncs_pool_outcomes_into_the_operations_ledger() -> None:
    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
    completed = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')
    failed = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')
    cancelled = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')
    running = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')

    pool.records[completed.job_id] = _pool_record(completed.job_id, PoolJobStatus.COMPLETED, completed.output_key)
    pool.records[failed.job_id] = _pool_record(failed.job_id, PoolJobStatus.FAILED, failed.output_key)
    pool.records[failed.job_id] = PoolJobRecord(
        **{
            **pool.records[failed.job_id].__dict__,
            "error": "worker offline",
            "retryable": True,
        }
    )
    pool.records[cancelled.job_id] = _pool_record(cancelled.job_id, PoolJobStatus.CANCELLED, cancelled.output_key)
    pool.records[running.job_id] = _pool_record(running.job_id, PoolJobStatus.PROCESSING, running.output_key)

    await service.list_operations(PROJECT, "user-1")

    assert (await jobs.get(completed.job_id)).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    assert (
        await jobs.get(completed.job_id)  # type: ignore[union-attr]
    ).result.output_key == completed.output_key
    failed_record = await jobs.get(failed.job_id)
    assert failed_record is not None and failed_record.status is JobStatus.FAILED
    assert failed_record.retryable is True
    assert (await jobs.get(cancelled.job_id)).status is JobStatus.CANCELLED  # type: ignore[union-attr]
    assert (await jobs.get(running.job_id)).status is JobStatus.QUEUED  # type: ignore[union-attr]


async def test_listing_logs_and_continues_when_one_pool_record_read_fails(caplog) -> None:
    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
    broken = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')
    healthy = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')

    pool.records[healthy.job_id] = _pool_record(
        healthy.job_id, PoolJobStatus.COMPLETED, healthy.output_key
    )

    real_get = pool.get

    async def flaky_get(job_id: str):
        if job_id == broken.job_id:
            raise RuntimeError("pool unreachable")
        return await real_get(job_id)

    pool.get = flaky_get  # type: ignore[method-assign]

    import logging

    with caplog.at_level(logging.WARNING):
        await service.list_operations(PROJECT, "user-1")

    broken_record = await jobs.get(broken.job_id)
    assert broken_record is not None and broken_record.status is JobStatus.QUEUED
    healthy_record = await jobs.get(healthy.job_id)
    assert healthy_record is not None and healthy_record.status is JobStatus.COMPLETED
    assert any(
        broken.job_id in record.message and record.levelname == "WARNING"
        for record in caplog.records
    )


async def test_cancelling_a_motion_operation_cancels_its_pool_job() -> None:
    pool = _Pool()
    service, _, _, project = await _service_with_motion(pool)
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
    job = await service.generate_motion(PROJECT, project.revision, "idle", "user-1")
    project = await service.get_project(PROJECT, 'user-1')

    await service.cancel_motion_operation(PROJECT, "idle", job.job_id, "user-1")

    assert pool.cancelled == [job.job_id]


def test_pool_payload_maps_the_comfy_template_inputs_and_clamps() -> None:
    class _Payload:
        prompt = "x" * 2100
        width = 100
        height = 4096
        duration_seconds = 30
        seed = 5
        steps = 99
        provider_parameters = {"fps": "60"}
        start_frame_key = "outputs/studio/p/composition/frame.png"

    mapped, input_keys = _motion_pool_payload(_Payload())  # type: ignore[arg-type]

    assert mapped["prompt"] == "x" * 2000
    assert mapped["width"] == 256
    assert mapped["height"] == 1344
    assert mapped["steps"] == 40
    assert mapped["fps"] == 30
    assert mapped["length"] == 161
    assert mapped["images"] == {"first_frame": _Payload.start_frame_key}
    assert input_keys == (_Payload.start_frame_key,)


def _subject_png_bytes() -> bytes:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(20, 44):
        for y in range(10, 54):
            image.putpixel((x, y), (10, 20, 30, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def test_pool_dispatch_without_composition_animates_the_subject() -> None:
    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    service._assets.assets["sources/studio/project-1/subjects/source.png"] = (
        _subject_png_bytes()
    )
    from magic_video_editor.studio.models import MotionSpecification

    project = await service.add_motion(
        PROJECT,
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
    settings = MotionGenerationSettings(width=512, height=768)

    job = await service.generate_motion(
        PROJECT, project.revision, "idle", "user-1", settings=settings
    )
    project = await service.get_project(PROJECT, 'user-1')

    expected_key = (
        "outputs/studio/project-1/opening-frames/subject-1-512x768-chroma_green.png"
    )
    assert expected_key in service._assets.assets
    submission = pool.submissions[0]
    assert submission.input_keys == (expected_key,)
    assert submission.payload["images"] == {"first_frame": expected_key}
    stored_job = await jobs.get(job.job_id)
    assert stored_job is not None
    assert stored_job.payload["start_frame_key"] == expected_key
    assert stored_job.payload["start_frame_sha256"]
    assert stored_job.payload["composition_revision_id"] is None

    writes_before = service._assets.publish_count
    job2 = await service.generate_motion(
        PROJECT,
        project.revision,
        "idle",
        "user-1",
        request_id="second-request",
        settings=settings,
    )
    project = await service.get_project(PROJECT, 'user-1')

    assert service._assets.publish_count == writes_before
    submission2 = pool.submissions[-1]
    assert submission2.job_id == job2.job_id
    assert submission2.input_keys == (expected_key,)


async def test_pool_dispatch_with_a_composition_start_frame_is_unaffected() -> None:
    from magic_video_editor.contracts import VideoJobResult
    from magic_video_editor.studio.models import CompositionModel, MotionSpecification

    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    assets = service._assets
    assets.assets["sources/studio/project-1/subjects/source.png"] = _subject_png_bytes()
    project = await service.add_motion(
        PROJECT,
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
    compose_job = await service.compose_opening_frame(
        PROJECT,
        project.revision,
        "Ember at rest",
        CompositionModel.NANOBANANA_2_LITE,
        "user-1",
        "compose-req-1",
    )
    claimed = await jobs.claim(compose_job.job_id)
    await jobs.complete(
        compose_job.job_id,
        claimed.claim_token,
        VideoJobResult(output_key=compose_job.output_key),
    )
    assets.assets[compose_job.output_key] = _subject_png_bytes()
    current = await service._store.get(PROJECT)
    assert current is not None
    settled = await service.settle_composition(
        PROJECT, compose_job.job_id, current.revision, "user-1"
    )
    approved = await service.approve_composition(PROJECT, settled.revision, "user-1")
    composition_key = approved.manifest.composition.current.artifact_key
    composition_revision_id = approved.manifest.composition.current.revision_id

    queued, failed = await service.generate_motions(
        PROJECT,
        approved.revision,
        ("idle",),
        "user-1",
        "motion-req-1",
        settings=MotionGenerationSettings(width=512, height=768),
        shared_starting_pose=True,
    )

    assert failed == ()
    writes_before = assets.publish_count
    submission = pool.submissions[0]
    assert submission.input_keys == (composition_key,)
    assert submission.payload["images"] == {"first_frame": composition_key}
    # No opening frame is rendered when a composition already supplies one.
    assert assets.publish_count == writes_before
    stored_job = await jobs.get(queued[0].operation.job_id)
    assert stored_job is not None
    assert stored_job.payload["composition_revision_id"] == composition_revision_id


def test_pool_payload_has_no_images_without_a_start_frame() -> None:
    class _Payload:
        prompt = "p"
        width = 768
        height = 768
        duration_seconds = 4
        seed = 1
        steps = 20
        provider_parameters = {}
        start_frame_key = None

    mapped, input_keys = _motion_pool_payload(_Payload())  # type: ignore[arg-type]

    assert "images" not in mapped
    assert input_keys == ()


def test_explicit_16gb_profile_uses_separate_capability_and_honest_vram_requirement():
    from magic_video_editor.studio.service import _motion_pool_profile, MOTION_16GB_CAPABILITY_ID
    settings = MotionGenerationSettings(provider_parameters={"execution_profile": "h3-w4a8-16gb"})
    capability, requirements = _motion_pool_profile(settings)
    assert capability == MOTION_16GB_CAPABILITY_ID
    assert requirements.min_vram_mb == 16_000
    default_capability, default_requirements = _motion_pool_profile(MotionGenerationSettings())
    assert default_capability == MOTION_POOL_CAPABILITY_ID
    assert default_requirements.min_vram_mb == 24_000


def test_16gb_profile_rejects_untested_workloads_before_job_creation():
    import pytest
    from pydantic import ValidationError
    for overrides in ({"width": 1024, "height": 256}, {"height": 768}, {"duration_seconds": 5}, {"steps": 21}):
        with pytest.raises(ValidationError, match="verified 16 GB profile"):
            MotionGenerationSettings(provider_parameters={"execution_profile": "h3-w4a8-16gb"}, **overrides)
    with pytest.raises(ValidationError, match="last-frame conditioning"):
        MotionGenerationSettings(provider_parameters={"closed_loop": True})


def test_closed_loop_binds_same_granted_reference_to_first_and_last_slots():
    from magic_video_editor.api_models import GenerateMotionPayloadV1
    payload = GenerateMotionPayloadV1(**{**_motion_payload(), "start_frame_key": "sources/ready.png", "start_frame_sha256": "c" * 64, "composition_revision_id": "composition-1", "requested_revision": 1, "subject_revision_id": "subject-1", "subject_artifact_key": "sources/studio/project-1/subjects/source.png", "subject_sha256": "a" * 64, "motion_spec_digest": "b" * 64, "provider_parameters": {"closed_loop": True, "execution_profile": "h3-w4a8-16gb"}})
    template, granted = _motion_pool_payload(payload)
    assert template["images"] == {"first_frame": "sources/ready.png", "last_frame": "sources/ready.png"}
    assert granted == ("sources/ready.png",)


def test_lightx8_profile_routes_separately_and_rejects_other_step_counts():
    import pytest
    from magic_video_editor.studio.service import _motion_pool_profile, MOTION_LIGHTX8_CAPABILITY_ID
    for steps in (4, 12, 20):
        with pytest.raises(ValueError, match='exactly 8 steps'):
            MotionGenerationSettings(steps=steps, provider_parameters={'execution_profile': 'h3-w4a8-16gb-lightx8'})
    settings = MotionGenerationSettings(steps=8, provider_parameters={'execution_profile': 'h3-w4a8-16gb-lightx8'})
    capability, requirements = _motion_pool_profile(settings)
    assert capability == MOTION_LIGHTX8_CAPABILITY_ID
    assert requirements.min_vram_mb == 16000


async def test_desktop_default_recipe_is_resolved_before_job_and_pool_provenance():
    from magic_video_editor.desktop_studio import balanced_motion_settings
    from magic_video_editor.studio.models import MotionSpecification
    from magic_video_editor.studio.service import MOTION_LIGHTX8_CAPABILITY_ID
    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    service.motion_settings_resolver = balanced_motion_settings
    project = await service.add_motion(PROJECT, project.revision, MotionSpecification(
        motion_id='walk', name='Walk', kind=MotionKind.PRESET, preset=MotionPreset.WALK,
        loop_policy='loop', required=True), 'user-1')
    job = await service.generate_motion(PROJECT, project.revision, 'walk', 'user-1')
    project = await service.get_project(PROJECT, 'user-1')
    persisted = await jobs.get(job.job_id)
    assert persisted.payload['steps'] == 8
    assert persisted.payload['provider_parameters']['execution_profile'] == 'h3-w4a8-16gb-lightx8'
    assert pool.submissions[0].payload['steps'] == 8
    assert pool.submissions[0].capability_id == MOTION_LIGHTX8_CAPABILITY_ID
    assert pool.submissions[0].requirements.min_vram_mb == 16000


async def test_completion_evidence_keeps_reported_runtime_distinct_from_requirements():
    from dataclasses import replace
    from outbound_gpu_worker_pool.contracts import JobRequirements
    from magic_video_editor.studio.models import MotionSpecification
    pool = _Pool()
    service, jobs, _, project = await _service_with_motion(pool)
    project = await service.add_motion(PROJECT, project.revision,
        MotionSpecification(motion_id='idle', name='Idle', preset='idle'), 'user-1')
    job = await service.generate_motion(PROJECT, project.revision, 'idle', 'user-1')
    project = await service.get_project(PROJECT, 'user-1')
    metadata = {'runtime_versions': {'torch': 'actual-version'}, 'template_sha256': 'a' * 64}
    pool.records[job.job_id] = replace(_pool_record(job.job_id, PoolJobStatus.COMPLETED, job.output_key),
        executed_by='worker-1', execution_metadata=metadata,
        requirements=JobRequirements(runtime_versions={'torch': 'requested-version'}), output_sha256='b' * 64)
    await service.list_operations(PROJECT, 'user-1')
    settled = await jobs.get(job.job_id)
    evidence = settled.result.execution_provenance
    assert evidence['worker_attestation'] == metadata
    assert evidence['executing_worker_id'] == 'worker-1'
    assert evidence['runtime_versions'] == {'torch': 'actual-version'}
    assert evidence['requested_runtime_versions'] == {'torch': 'requested-version'}
    assert evidence['completion_path'] == 'hosted_pool' and evidence['lease_fenced'] is True
    assert evidence['output_sha256'] == 'b' * 64
    assert evidence['attestation'] == 'worker-reported execution metadata'


async def test_local_completed_outcome_syncs_without_fabricating_attestation_or_replaying():
    from types import SimpleNamespace
    from magic_video_editor.studio.models import MotionSpecification
    pool = _Pool()
    service, jobs, dispatcher, project = await _service_with_motion(pool)
    project = await service.add_motion(PROJECT, project.revision,
        MotionSpecification(motion_id='idle', name='Idle', preset='idle'), 'user-1')
    job = await service.generate_motion(PROJECT, project.revision, 'idle', 'user-1')
    project = await service.get_project(PROJECT, 'user-1')
    pool.records[job.job_id] = SimpleNamespace(status=PoolJobStatus.COMPLETED, output_key=job.output_key,
        completion_path='desktop_local', requirements={'runtime_versions': {'torch': 'requested-only'}})
    submissions = len(pool.submissions)
    await service.list_operations(PROJECT, 'user-1')
    await service.list_operations(PROJECT, 'user-1')
    settled = await jobs.get(job.job_id)
    assert settled.status is JobStatus.COMPLETED
    evidence = settled.result.execution_provenance
    assert evidence['executing_worker_id'] is None and evidence['worker_attestation'] == {}
    assert evidence['runtime_versions'] == {} and evidence['requested_runtime_versions'] == {'torch': 'requested-only'}
    assert evidence['completion_path'] == 'desktop_local' and evidence['lease_fenced'] is False
    assert evidence['output_sha256'] is None and evidence['attestation'] == 'unavailable'
    assert len(pool.submissions) == submissions and dispatcher.job_ids == []
