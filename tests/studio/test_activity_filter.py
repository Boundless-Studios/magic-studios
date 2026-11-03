"""`GET /projects/{id}/activity?motion_id=` scopes the log to one object
(studio-ux-flow phase 3): a motion's own id to that motion's operations, and
the literal value `subject` to operations with no motion (subject generation
and opening-frame composition)."""

from magic_video_editor.contracts import JobOperation, JobRecord, JobStatus
from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


async def _service_with_mixed_activity() -> tuple[StudioService, MemoryJobStore]:
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    await store.create(manifest)
    jobs.records["subject-job"] = JobRecord(
        job_id="subject-job",
        idempotency_key="subject-job",
        operation=JobOperation.GENERATE_SUBJECT,
        status=JobStatus.COMPLETED,
        source_key="sources/studio/project-1/subject-references/ref.png",
        output_key="outputs/studio/project-1/subjects/subject-job.png",
        attempts=1,
        project_id="project-1",
        manifest_operation_id="subject-job",
        payload={},
    )
    jobs.records["idle-job"] = JobRecord(
        job_id="idle-job",
        idempotency_key="idle-job",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.COMPLETED,
        source_key="sources/studio/project-1/subjects/source.png",
        output_key="outputs/studio/project-1/motions/idle/idle-job.mp4",
        attempts=1,
        project_id="project-1",
        manifest_operation_id="idle-job",
        payload={"motion_id": "idle"},
    )
    jobs.records["walk-job"] = JobRecord(
        job_id="walk-job",
        idempotency_key="walk-job",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.COMPLETED,
        source_key="sources/studio/project-1/subjects/source.png",
        output_key="outputs/studio/project-1/motions/walk/walk-job.mp4",
        attempts=1,
        project_id="project-1",
        manifest_operation_id="walk-job",
        payload={"motion_id": "walk"},
    )
    service = StudioService(store=store, assets=MemoryAssetStore(), jobs=jobs)
    return service, jobs


async def test_activity_motion_filter_returns_only_that_motions_operations() -> None:
    service, _ = await _service_with_mixed_activity()

    page = await service.list_activity_operations(
        "project-1", "user-1", motion_id="idle"
    )

    assert [listing.record.job_id for listing in page.operations] == ["idle-job"]


async def test_activity_subject_filter_returns_operations_with_no_motion_id() -> None:
    service, _ = await _service_with_mixed_activity()

    page = await service.list_activity_operations(
        "project-1", "user-1", motion_id="subject"
    )

    assert [listing.record.job_id for listing in page.operations] == ["subject-job"]


async def test_activity_without_a_filter_returns_every_studio_operation() -> None:
    service, _ = await _service_with_mixed_activity()

    page = await service.list_activity_operations("project-1", "user-1")

    assert {listing.record.job_id for listing in page.operations} == {
        "subject-job",
        "idle-job",
        "walk-job",
    }


async def test_activity_motion_filter_still_pages_with_a_cursor() -> None:
    service, jobs = await _service_with_mixed_activity()
    for index in range(3):
        job_id = f"idle-extra-{index}"
        jobs.records[job_id] = JobRecord(
            job_id=job_id,
            idempotency_key=job_id,
            operation=JobOperation.GENERATE_MOTION,
            status=JobStatus.COMPLETED,
            source_key="sources/studio/project-1/subjects/source.png",
            output_key=f"outputs/studio/project-1/motions/idle/{job_id}.mp4",
            attempts=1,
            project_id="project-1",
            manifest_operation_id=job_id,
            payload={"motion_id": "idle"},
        )

    first_page = await service.list_activity_operations(
        "project-1", "user-1", motion_id="idle", limit=2
    )
    assert len(first_page.operations) == 2
    assert first_page.next_cursor is not None
    assert all(
        listing.record.payload.get("motion_id") == "idle"
        for listing in first_page.operations
    )

    second_page = await service.list_activity_operations(
        "project-1",
        "user-1",
        motion_id="idle",
        limit=2,
        cursor=first_page.next_cursor,
    )
    assert all(
        listing.record.payload.get("motion_id") == "idle"
        for listing in second_page.operations
    )
    seen = {listing.record.job_id for listing in first_page.operations} | {
        listing.record.job_id for listing in second_page.operations
    }
    assert seen == {"idle-job", "idle-extra-0", "idle-extra-1", "idle-extra-2"}
