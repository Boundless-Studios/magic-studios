"""Deleting a project removes every file (every version), job, release, export
and revision it owns, and only after the owner repeats its name."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import JobOperation, JobSubmission
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.limits import MemoryStudioOwnerUsageStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher

from .test_cross_account_isolation import OWNER, STRANGER, _png_bytes


class _Studio:
    def __init__(self) -> None:
        self.assets = MemoryAssetStore()
        self.jobs = MemoryJobStore()
        self.store = MemoryStudioStore()
        self.usage = MemoryStudioOwnerUsageStore()
        self.service = StudioService(
            store=self.store, assets=self.assets, jobs=self.jobs,
            dispatcher=NullTaskDispatcher(), owner_usage=self.usage,
        )
        app = create_app(
            InMemoryRuntime(self.jobs, self.assets, studio_service=self.service),
            service_role=ServiceRole.API,
        )
        self.owner = TestClient(app, headers={"X-Studio-Actor-Id": OWNER})
        self.stranger = TestClient(app, headers={"X-Studio-Actor-Id": STRANGER})

    def project_with_subject(self, client: TestClient, name: str) -> tuple[str, str]:
        project_id = client.post("/studio/projects", json={"name": name}).json()["project_id"]
        upload = client.post(
            f"/studio/projects/{project_id}/subject/uploads",
            json={"content_type": "image/png", "expected_revision": 1},
        ).json()
        self.assets.assets[upload["key"]] = _png_bytes()
        settled = client.post(
            f"/studio/projects/{project_id}/subject/settle",
            json={"expected_revision": 1, "asset_key": upload["key"]},
        )
        assert settled.status_code == 200, settled.text
        return project_id, upload["key"]


def _reference(project_id: str) -> str:
    return f"sources/studio/{project_id}/subject-references/r.png"


def _delete(client: TestClient, project_id: str, name: str):
    return client.request("DELETE", f"/studio/projects/{project_id}", json={"confirm_name": name})


def test_deleting_a_project_removes_everything_it_owns_and_nothing_else() -> None:
    studio = _Studio()
    project_id, subject_key = studio.project_with_subject(studio.owner, "Ember Knight")
    kept_id, kept_key = studio.project_with_subject(studio.owner, "Ash Rider")
    for key in (
        f"outputs/studio/{project_id}/motions/idle/take.mp4",
        f"exports/studio/{project_id}/export.zip",
    ):
        studio.assets.assets[key] = b"bytes"
    asyncio.run(studio.jobs.submit(JobSubmission(
        job_id="00000000-0000-0000-0000-0000000000d1", idempotency_key="k1",
        operation=JobOperation.GENERATE_SUBJECT, source_key=_reference(project_id),
        output_key=f"outputs/studio/{project_id}/subjects/op.png", requested_outputs={},
        project_id=project_id, manifest_operation_id="op",
        payload={"reference_keys": [_reference(project_id)], "reference_sha256": ["a" * 64],
                 "requested_revision": 1},
    )))
    usage_before = asyncio.run(studio.usage.stored_bytes(OWNER))
    assert usage_before == 2 * len(_png_bytes())

    assert _delete(studio.owner, project_id, "ember knight").status_code == 422
    assert _delete(studio.stranger, project_id, "Ember Knight").status_code == 404
    assert subject_key in studio.assets.assets

    assert _delete(studio.owner, project_id, "Ember Knight").status_code == 204

    assert studio.owner.get(f"/studio/projects/{project_id}").status_code == 404
    assert [key for key in studio.assets.assets if project_id in key] == []
    assert asyncio.run(studio.jobs.list_for_project(project_id)) == ()
    assert asyncio.run(studio.usage.stored_bytes(OWNER)) == usage_before - len(_png_bytes())
    # The owner's other project is untouched.
    assert kept_key in studio.assets.assets
    assert studio.owner.get(f"/studio/projects/{kept_id}").status_code == 200
    assert [p["project_id"] for p in studio.owner.get("/studio/projects").json()] == [kept_id]
    # Deleting again is a plain 404, not an error.
    assert _delete(studio.owner, project_id, "Ember Knight").status_code == 404


def test_deleting_a_project_cancels_its_running_work_first() -> None:
    studio = _Studio()
    project_id, _ = studio.project_with_subject(studio.owner, "Ember Knight")
    job_id = "00000000-0000-0000-0000-0000000000d2"
    asyncio.run(studio.jobs.submit(JobSubmission(
        job_id=job_id, idempotency_key="k2", operation=JobOperation.GENERATE_SUBJECT,
        source_key=_reference(project_id),
        output_key=f"outputs/studio/{project_id}/subjects/op2.png",
        requested_outputs={}, project_id=project_id, manifest_operation_id="op2",
        payload={"reference_keys": [_reference(project_id)], "reference_sha256": ["a" * 64],
                 "requested_revision": 1},
    )))
    cancelled: list[str] = []
    original = studio.jobs.cancel

    async def record_cancel(job: str) -> bool:
        cancelled.append(job)
        return await original(job)

    studio.jobs.cancel = record_cancel  # type: ignore[method-assign]

    assert _delete(studio.owner, project_id, "Ember Knight").status_code == 204
    assert cancelled == [job_id]


async def test_the_memory_store_refuses_to_delete_outside_one_project() -> None:
    assets = MemoryAssetStore()
    assets.assets["outputs/studio/p1/a.png"] = b"a"
    with pytest.raises(ValueError, match="refusing to delete"):
        await assets.delete_prefix("outputs/studio/")
    assert assets.assets
