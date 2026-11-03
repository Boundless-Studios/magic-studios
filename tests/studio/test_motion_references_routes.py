"""T17 route-level coverage: reference role validation and the motion PATCH endpoint."""

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


def _studio_runtime() -> InMemoryRuntime:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    service = StudioService(
        store=store, assets=assets, jobs=jobs, dispatcher=_NullDispatcher(),
        motion_generation_available=True,
    )
    return InMemoryRuntime(jobs, assets, studio_service=service)


class _NullDispatcher:
    async def enqueue(self, job_id: str) -> None:
        return None


def _client(runtime: InMemoryRuntime) -> TestClient:
    return TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "owner-1"})


def _png_bytes() -> bytes:
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    subject = Image.new("RGBA", (624, 624), (255, 64, 64, 255))
    image.paste(subject, (200, 200))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _project_with_approved_subject(client: TestClient, runtime: InMemoryRuntime) -> tuple:
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()["project_id"]
    upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    ).json()
    runtime.assets_store.assets[upload["key"]] = _png_bytes()
    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": upload["key"]},
    ).json()
    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": settled["revision"]},
    ).json()
    return project_id, approved["revision"]


def test_adding_a_motion_with_a_lone_end_reference_is_rejected() -> None:
    runtime = _studio_runtime()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)

    response = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
            "references": [{"key": "sources/studio/x/motion-references/a.png", "sha256": "a" * 64, "role": "end"}],
        },
    )

    assert response.status_code == 422


def test_motion_reference_upload_and_patch_round_trip() -> None:
    runtime = _studio_runtime()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={"expected_revision": revision, "motion_id": "idle", "name": "Idle", "kind": "preset", "preset": "idle"},
    ).json()

    upload = client.post(
        f"/studio/projects/{project_id}/motions/idle/references/uploads",
        json={"content_type": "image/jpeg", "expected_revision": added["revision"]},
    ).json()
    assert upload["key"].startswith(f"sources/studio/{project_id}/motion-references/")

    patched = client.patch(
        f"/studio/projects/{project_id}/motions/idle",
        json={
            "expected_revision": added["revision"], "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle", "style": "grimdark",
            "script": "A cold wind before the loop begins.",
            "references": [{"key": upload["key"], "sha256": "a" * 64, "role": "context"}],
        },
    )

    assert patched.status_code == 200, patched.json()
    idle = next(m for m in patched.json()["manifest"]["motions"] if m["motion_id"] == "idle")
    assert idle["style"] == "grimdark"
    assert idle["script"] == "A cold wind before the loop begins."
    assert idle["references"] == [{"key": upload["key"], "sha256": "a" * 64, "role": "context"}]
