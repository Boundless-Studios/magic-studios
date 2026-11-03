from io import BytesIO

import pytest
from PIL import Image

from magic_video_editor.app import _execute
from magic_video_editor.contracts import JobOperation, JobStatus
from magic_video_editor.local_studio import create_local_studio_runtime
from magic_video_editor.studio.service import SubjectUploadError


def _jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (128, 96), (210, 120, 70)).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_reference_upload_accepts_jpeg_and_uses_reference_namespace() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    project = await runtime.studio_service.create_project("Hero", "owner-1")

    target = await runtime.studio_service.create_subject_reference_upload(
        project.project_id, "image/jpeg", "owner-1", project.revision
    )

    assert target.key.startswith(
        f"sources/studio/{project.project_id}/subject-references/"
    )
    assert target.key.endswith(".jpg")


@pytest.mark.asyncio
async def test_reference_upload_rejects_unsupported_content_type() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    project = await runtime.studio_service.create_project("Hero", "owner-1")

    with pytest.raises(SubjectUploadError, match="PNG, JPEG, or WebP"):
        await runtime.studio_service.create_subject_reference_upload(
            project.project_id, "image/gif", "owner-1", project.revision
        )


@pytest.mark.asyncio
async def test_generate_subject_job_settles_exact_reference_lineage() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = runtime.studio_service
    project = await service.create_project("Hero", "owner-1")
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", "owner-1", project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()

    job = await service.generate_subject(
        project.project_id,
        project.revision,
        (target.key,),
        "Warm illustrated adventurer",
        "request-1",
        "owner-1",
    )
    assert job.operation is JobOperation.GENERATE_SUBJECT
    assert job.payload["reference_keys"] == [target.key]
    assert job.output_key.startswith(
        f"outputs/studio/{project.project_id}/subjects/"
    )
    assert job.output_key.endswith(".png")
    replay = await service.generate_subject(
        project.project_id, project.revision, (target.key,),
        "Warm illustrated adventurer", "request-1", "owner-1",
    )
    assert replay.job_id == job.job_id

    result = await _execute(runtime, job.job_id)
    assert result.status.value == "completed"
    completed = await runtime.jobs.get(job.job_id)
    assert completed is not None and completed.status is JobStatus.COMPLETED

    settled = await service.settle_subject_generation(
        project.project_id, completed.job_id, project.revision, "owner-1"
    )
    revision = settled.manifest.subject.current_revision
    assert revision is not None
    assert revision.artifact_key == completed.output_key
    assert revision.width >= 64 and revision.height >= 64
    assert settled.manifest.subject.assessment is not None
    assert settled.manifest.subject.approval.state.value == "ready_for_signoff"


async def _settle_new_subject_revision(
    runtime, project, reference_key: str, actor_id: str = "owner-1"
):
    """Drives one reference through generate + execute + settle and returns
    the resulting manifest along with the freshly settled revision."""
    service = runtime.studio_service
    job = await service.generate_subject(
        project.project_id,
        project.revision,
        (reference_key,),
        "Warm illustrated adventurer",
        f"request-{reference_key}",
        actor_id,
    )
    await _execute(runtime, job.job_id)
    completed = await runtime.jobs.get(job.job_id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
    settled = await service.settle_subject_generation(
        project.project_id, completed.job_id, project.revision, actor_id
    )
    return settled


@pytest.mark.asyncio
async def test_generate_subject_accepts_chosen_take_as_its_own_reference() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = runtime.studio_service
    project = await service.create_project("Hero", "owner-1")
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", "owner-1", project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()

    settled = await _settle_new_subject_revision(runtime, project, target.key)
    chosen_take = settled.manifest.subject.current_revision
    assert chosen_take is not None

    job = await service.generate_subject(
        settled.project_id,
        settled.revision,
        (chosen_take.artifact_key,),
        "Give the adventurer a scar",
        "request-correction",
        "owner-1",
    )
    assert job.operation is JobOperation.GENERATE_SUBJECT
    assert job.payload["reference_keys"] == [chosen_take.artifact_key]

    result = await _execute(runtime, job.job_id)
    assert result.status.value == "completed"
    completed = await runtime.jobs.get(job.job_id)
    assert completed is not None and completed.status is JobStatus.COMPLETED

    corrected = await service.settle_subject_generation(
        settled.project_id, completed.job_id, settled.revision, "owner-1"
    )
    revision = corrected.manifest.subject.current_revision
    assert revision is not None
    assert revision.artifact_key == completed.output_key


@pytest.mark.asyncio
async def test_generate_subject_rejects_another_projects_subject_key() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = runtime.studio_service
    other_project = await service.create_project("Other Hero", "owner-1")
    other_target = await service.create_subject_reference_upload(
        other_project.project_id, "image/jpeg", "owner-1", other_project.revision
    )
    runtime.assets.assets[other_target.key] = _jpeg()
    other_settled = await _settle_new_subject_revision(
        runtime, other_project, other_target.key
    )
    other_revision = other_settled.manifest.subject.current_revision
    assert other_revision is not None

    project = await service.create_project("Hero", "owner-1")

    with pytest.raises(SubjectUploadError, match="does not belong to this project"):
        await service.generate_subject(
            project.project_id,
            project.revision,
            (other_revision.artifact_key,),
            None,
            "request-cross-project",
            "owner-1",
        )


@pytest.mark.asyncio
async def test_generate_subject_rejects_arbitrary_outputs_key() -> None:
    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = runtime.studio_service
    project = await service.create_project("Hero", "owner-1")

    arbitrary_key = f"outputs/studio/{project.project_id}/subjects/not-a-revision.png"

    with pytest.raises(SubjectUploadError, match="does not belong to this project"):
        await service.generate_subject(
            project.project_id,
            project.revision,
            (arbitrary_key,),
            None,
            "request-arbitrary",
            "owner-1",
        )


def test_generate_subject_route_refuses_a_request_id_reused_at_a_later_revision() -> None:
    from fastapi.testclient import TestClient

    from magic_video_editor.app import create_app

    runtime = create_local_studio_runtime(inline_dispatch=False)
    client = TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "owner-1"})
    project = client.post("/studio/projects", json={"name": "Hero"}).json()
    base = f"/studio/projects/{project['project_id']}"
    target = client.post(
        f"{base}/subject/references/uploads",
        json={"content_type": "image/jpeg", "expected_revision": project["revision"]},
    ).json()
    runtime.assets.assets[target["key"]] = _jpeg()
    body = {
        "expected_revision": project["revision"],
        "reference_keys": [target["key"]],
        "direction": "Warm illustrated adventurer",
        "request_id": "request-1",
    }
    first = client.post(f"{base}/subject/generate", json=body)
    assert first.status_code == 200, first.text
    moved = client.post(
        f"{base}/motions",
        json={
            "expected_revision": project["revision"], "motion_id": "idle",
            "name": "Idle", "kind": "preset", "preset": "idle",
        },
    )
    assert moved.status_code == 200, moved.text

    for changed in ({}, {"direction": "Cool painted ranger"}):
        resent = client.post(
            f"{base}/subject/generate",
            json={**body, **changed, "expected_revision": moved.json()["revision"]},
        )

        assert resent.status_code == 409
        assert resent.json()["detail"]["code"] == "request_id_reused"
    assert len(runtime.jobs.records) == 1


def test_a_subject_generation_queued_under_the_legacy_key_is_still_idempotent() -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from magic_video_editor.app import create_app

    runtime = create_local_studio_runtime(inline_dispatch=False)
    client = TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "owner-1"})
    project = client.post("/studio/projects", json={"name": "Hero"}).json()
    base = f"/studio/projects/{project['project_id']}"
    target = client.post(
        f"{base}/subject/references/uploads",
        json={"content_type": "image/jpeg", "expected_revision": project["revision"]},
    ).json()
    runtime.assets.assets[target["key"]] = _jpeg()
    body = {
        "expected_revision": project["revision"],
        "reference_keys": [target["key"]],
        "direction": "Warm illustrated adventurer",
        "request_id": "request-1",
    }
    first = client.post(f"{base}/subject/generate", json=body)
    assert first.status_code == 200, first.text
    # The shape the code before request_id keys stored.
    record = runtime.jobs.records[first.json()["job_id"]]
    runtime.jobs.records[record.job_id] = replace(
        record,
        idempotency_key=f"{record.project_id}:subject:{record.manifest_operation_id}",
    )

    retry = client.post(f"{base}/subject/generate", json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["job_id"] == record.job_id
    changed = client.post(
        f"{base}/subject/generate", json={**body, "direction": "Cool painted ranger"}
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "request_id_reused"
    moved = client.post(
        f"{base}/motions",
        json={
            "expected_revision": project["revision"], "motion_id": "idle",
            "name": "Idle", "kind": "preset", "preset": "idle",
        },
    )
    assert moved.status_code == 200, moved.text
    later = client.post(
        f"{base}/subject/generate",
        json={**body, "expected_revision": moved.json()["revision"]},
    )
    assert later.status_code == 409
    assert later.json()["detail"]["code"] == "request_id_reused"
    assert len(runtime.jobs.records) == 1
