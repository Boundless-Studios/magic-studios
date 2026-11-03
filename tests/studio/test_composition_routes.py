"""T15 route-level coverage for the composition gate endpoints."""

import asyncio
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import _execute, create_app
from magic_video_editor.local_studio import (
    DeterministicCompositionGenerator,
    LocalAssetStore,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


def _studio_runtime_with_composition_generation(
    jobs: MemoryJobStore | None = None,
) -> InMemoryRuntime:
    assets = LocalAssetStore()
    store = MemoryStudioStore()
    jobs = jobs or MemoryJobStore()
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=_NullDispatcher(),
        motion_generation_available=True,
        composition_generation_available=True,
    )
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    runtime.composition_generator = DeterministicCompositionGenerator(assets)
    runtime.composition_operations_enabled = True
    return runtime


class _NullDispatcher:
    async def enqueue(self, job_id: str) -> None:
        return None


def _client(runtime: InMemoryRuntime) -> TestClient:
    return TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "owner-1"})


def _png_bytes(
    *, size: tuple[int, int], subject_box: tuple[int, int, int, int], mode: str,
) -> bytes:
    background = (0, 0, 0, 0) if mode == "RGBA" else (255, 255, 255)
    foreground = (255, 64, 64, 255) if mode == "RGBA" else (255, 64, 64)
    image = Image.new(mode, size, background)
    subject = Image.new(
        mode, (subject_box[2] - subject_box[0], subject_box[3] - subject_box[1]), foreground,
    )
    image.paste(subject, (subject_box[0], subject_box[1]))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _project_with_approved_subject(client: TestClient, runtime: InMemoryRuntime) -> tuple:
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()["project_id"]
    upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    ).json()
    upload_key = upload["key"]
    runtime.assets_store.assets[upload_key] = _png_bytes(
        size=(1024, 1024), subject_box=(200, 200, 824, 824), mode="RGBA",
    )
    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": upload_key},
    ).json()
    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": settled["revision"]},
    ).json()
    return project_id, approved["revision"]


def _compose_and_settle(client: TestClient, runtime: InMemoryRuntime, project_id: str, revision: int) -> dict:
    composed = client.post(
        f"/studio/projects/{project_id}/composition/compose",
        json={
            "expected_revision": revision, "prompt": "Ember at rest",
            "request_id": "req-1",
        },
    ).json()
    result = asyncio.run(_execute(runtime, composed["job_id"]))
    assert result.status.value == "completed", result
    response = client.post(
        f"/studio/projects/{project_id}/composition/operations/{composed['job_id']}/settle",
        json={"expected_revision": revision},
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_compose_settle_and_approve_round_trip() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)

    settled = _compose_and_settle(client, runtime, project_id, revision)
    assert settled["manifest"]["composition"]["current"]["approval"] == "pending"

    # The gate shows the still through the same signed-preview route as subjects and
    # candidates, so a composition key must be previewable.
    preview = client.post(
        f"/studio/projects/{project_id}/assets/preview",
        json={"asset_key": settled["manifest"]["composition"]["current"]["artifact_key"]},
    )
    assert preview.status_code == 200, preview.json()

    approved = client.post(
        f"/studio/projects/{project_id}/composition/approve",
        json={"expected_revision": settled["revision"]},
    ).json()
    assert approved["manifest"]["composition"]["current"]["approval"] == "approved"


def test_generate_batch_with_shared_pose_requires_approval_then_succeeds() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions/batch",
        json={
            "expected_revision": revision,
            "motions": [
                {"motion_id": "idle", "name": "Idle", "kind": "preset", "preset": "idle"},
                {"motion_id": "walk", "name": "Walk", "kind": "preset", "preset": "walk"},
            ],
        },
    ).json()

    blocked = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": added["revision"], "request_id": "req-1",
            "motion_ids": ["idle", "walk"], "shared_starting_pose": True,
        },
    )
    assert blocked.status_code == 422

    settled = _compose_and_settle(client, runtime, project_id, added["revision"])
    approved = client.post(
        f"/studio/projects/{project_id}/composition/approve",
        json={"expected_revision": settled["revision"]},
    ).json()

    response = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": approved["revision"], "request_id": "req-2",
            "motion_ids": ["idle", "walk"], "shared_starting_pose": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert {item["motion_id"] for item in body["queued"]} == {"idle", "walk"}


def test_regenerate_composition_supersedes_and_replaces_the_gate_still() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    settled = _compose_and_settle(client, runtime, project_id, revision)
    approved = client.post(
        f"/studio/projects/{project_id}/composition/approve",
        json={"expected_revision": settled["revision"]},
    ).json()

    regenerated_op = client.post(
        f"/studio/projects/{project_id}/composition/regenerate",
        json={
            "expected_revision": approved["revision"], "prompt": "Ember, brighter",
            "request_id": "req-2",
        },
    ).json()
    result = asyncio.run(_execute(runtime, regenerated_op["job_id"]))
    assert result.status.value == "completed", result
    regenerated = client.post(
        f"/studio/projects/{project_id}/composition/operations/{regenerated_op['job_id']}/settle",
        json={"expected_revision": approved["revision"]},
    ).json()

    assert regenerated["manifest"]["composition"]["current"]["approval"] == "pending"
    assert (
        regenerated["manifest"]["composition"]["current"]["artifact_key"]
        != approved["manifest"]["composition"]["current"]["artifact_key"]
    )


def test_a_request_id_resent_after_the_revision_moved_is_refused_with_409() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    for route in ("compose", "regenerate"):
        current = client.get(f"/studio/projects/{project_id}").json()["revision"]
        body = {
            "expected_revision": current, "prompt": "Ember at rest",
            "request_id": f"req-{route}",
        }
        first = client.post(f"/studio/projects/{project_id}/composition/{route}", json=body)
        assert first.status_code == 200, first.text
        # A retry at the same revision is the same request: the original job.
        retry = client.post(f"/studio/projects/{project_id}/composition/{route}", json=body)
        assert retry.json()["job_id"] == first.json()["job_id"]
        moved = client.post(
            f"/studio/projects/{project_id}/motions",
            json={
                "expected_revision": current, "motion_id": f"idle-{route}",
                "name": "Idle", "kind": "preset", "preset": "idle",
            },
        )
        assert moved.status_code == 200, moved.text

        for changed in ({}, {"prompt": "Ember mid-stride"}):
            resent = client.post(
                f"/studio/projects/{project_id}/composition/{route}",
                json={**body, **changed, "expected_revision": moved.json()["revision"]},
            )

            assert resent.status_code == 409
            assert resent.json()["detail"]["code"] == "request_id_reused"
    jobs = runtime.jobs.records.values()
    assert sum(job.operation.value == "compose_frame" for job in jobs) == 2


def test_a_request_id_reused_with_a_different_prompt_starts_nothing() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    body = {"expected_revision": revision, "prompt": "Ember at rest", "request_id": "req-1"}
    first = client.post(f"/studio/projects/{project_id}/composition/compose", json=body)
    assert first.status_code == 200, first.text

    for route in ("compose", "regenerate"):
        reused = client.post(
            f"/studio/projects/{project_id}/composition/{route}",
            json={**body, "prompt": "Ember mid-stride"},
        )

        assert reused.status_code == 409
        assert reused.json()["detail"]["code"] == "request_id_reused"
    assert len(runtime.jobs.records) == 1


def _as_legacy_keyed(runtime: InMemoryRuntime, job_id: str, scope: str) -> None:
    """Rewrite a job into the shape the code before request_id keys stored."""
    from dataclasses import replace

    record = runtime.jobs.records[job_id]
    runtime.jobs.records[job_id] = replace(
        record,
        idempotency_key=f"{record.project_id}:{scope}:{record.manifest_operation_id}",
    )


def test_a_composition_queued_under_the_legacy_key_is_still_idempotent() -> None:
    runtime = _studio_runtime_with_composition_generation()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    body = {"expected_revision": revision, "prompt": "Ember at rest", "request_id": "req-1"}
    first = client.post(f"/studio/projects/{project_id}/composition/compose", json=body)
    assert first.status_code == 200, first.text
    _as_legacy_keyed(runtime, first.json()["job_id"], "composition")

    for route in ("compose", "regenerate"):
        retry = client.post(f"/studio/projects/{project_id}/composition/{route}", json=body)
        assert retry.status_code == 200, retry.text
        assert retry.json()["job_id"] == first.json()["job_id"]
    changed = client.post(
        f"/studio/projects/{project_id}/composition/compose",
        json={**body, "prompt": "Ember mid-stride"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "request_id_reused"
    moved = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle",
            "name": "Idle", "kind": "preset", "preset": "idle",
        },
    )
    assert moved.status_code == 200, moved.text
    later = client.post(
        f"/studio/projects/{project_id}/composition/compose",
        json={**body, "expected_revision": moved.json()["revision"]},
    )
    assert later.status_code == 409
    assert later.json()["detail"]["code"] == "request_id_reused"
    assert len(runtime.jobs.records) == 1


def test_a_legacy_composition_behind_more_than_a_page_of_newer_jobs_is_found() -> None:
    from dataclasses import replace
    from datetime import timedelta

    from magic_video_editor.contracts import JobOperation, JobStatus, JobSubmission

    class _CappedJobStore(MemoryJobStore):
        """PostgresJobStore's contract: an unpaged listing stops at the newest
        100 rows; a paged one reaches everything through the cursor."""

        async def list_for_project(self, project_id, *, cursor=None, limit=None):
            if limit is None:
                return tuple(reversed(await super().list_for_project(project_id, limit=100)))
            return await super().list_for_project(project_id, cursor=cursor, limit=limit)

    runtime = _studio_runtime_with_composition_generation(_CappedJobStore())
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    body = {"expected_revision": revision, "prompt": "Ember at rest", "request_id": "req-1"}
    first = client.post(f"/studio/projects/{project_id}/composition/compose", json=body)
    assert first.status_code == 200, first.text
    _as_legacy_keyed(runtime, first.json()["job_id"], "composition")
    legacy = runtime.jobs.records[first.json()["job_id"]]
    # 150 finished jobs newer than the legacy one: more than one 100-row page.
    for index in range(150):
        job_id = f"00000000-0000-0000-0000-{index:012d}"
        asyncio.run(runtime.jobs.submit(JobSubmission(
            job_id=job_id,
            idempotency_key=f"newer-{index}",
            operation=JobOperation.PACKAGE,
            source_key=f"sources/studio/{project_id}/a.png",
            output_key=f"outputs/studio/{project_id}/{index}.mp4",
            requested_outputs={},
            project_id=project_id,
            manifest_operation_id=f"op-{index}",
        )))
        runtime.jobs.records[job_id] = replace(
            runtime.jobs.records[job_id],
            status=JobStatus.COMPLETED,
            created_at=legacy.created_at + timedelta(seconds=index + 1),
        )

    retry = client.post(f"/studio/projects/{project_id}/composition/compose", json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["job_id"] == legacy.job_id
    changed = client.post(
        f"/studio/projects/{project_id}/composition/compose",
        json={**body, "prompt": "Ember mid-stride"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "request_id_reused"
    assert sum(
        job.operation is JobOperation.COMPOSE_FRAME for job in runtime.jobs.records.values()
    ) == 1
