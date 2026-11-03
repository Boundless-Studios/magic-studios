from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from targets.test_contract_dummy import DummyTargetAdapter

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import (
    AssetStorageUnavailable,
    JobFailureCode,
    JobOperation,
    JobRecord,
    JobStatus,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.exports import (
    ExportStorageUnavailable,
    ImmutableExportCollision,
    ReleaseAssetInvalid,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.postgres import ManifestUnreadable
from magic_video_editor.studio.routes import OperationResponse, create_studio_router
from magic_video_editor.studio.service import (
    ReleaseNotFound,
    StudioService,
    project_file_path,
)
from magic_video_editor.studio.store import ReleaseIntegrityError, RevisionConflict
from magic_video_editor.targets import TargetRegistry
from magic_video_editor.task_dispatch import NullTaskDispatcher


def test_studio_project_round_trip_allows_subject_upload_settlement_and_approval() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")

    created = client.post("/studio/projects", json={"name": "Ember Knight"})

    assert created.status_code == 200
    project = created.json()
    project_id = project["project_id"]
    assert project["revision"] == 1
    assert project["manifest"]["owner_id"] == "owner-1"
    assert project["active_operations"] == []
    assert project["primary_action"] == "add_subject"

    stale_upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 2},
    )
    assert stale_upload.status_code == 409
    assert stale_upload.json()["detail"]["current_revision"] == 1

    fetched = client.get(f"/studio/projects/{project_id}")

    assert fetched.status_code == 200
    assert fetched.json()["project_id"] == project_id

    upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    )

    assert upload.status_code == 200
    upload_key = upload.json()["key"]
    assert upload_key.startswith(f"sources/studio/{project_id}/subjects/")
    assert upload_key.endswith(".png")
    assert upload.json()["upload_url"] == f"memory://upload/{upload_key}"

    runtime.assets_store.assets[upload_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )
    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": upload_key},
    )

    assert settled.status_code == 200
    settled_project = settled.json()
    assert settled_project["revision"] == 2
    assert (
        settled_project["manifest"]["subject"]["current_revision"]["artifact_key"]
        == upload_key
    )
    assert settled_project["manifest"]["subject"]["assessment"]["findings"] == []

    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": 2},
    )

    assert approved.status_code == 200
    assert approved.json()["revision"] == 3
    assert approved.json()["manifest"]["subject"]["approval"]["state"] == "approved"
    assert (
        approved.json()["manifest"]["subject"]["approval"]["approver_id"]
        == "owner-1"
    )
    preview = client.post(
        f"/studio/projects/{project_id}/assets/preview",
        json={"asset_key": upload_key},
    )
    assert preview.status_code == 200
    assert preview.json() == {
        "key": upload_key,
        "upload_url": project_file_path(project_id, upload_key),
        "upload_headers": {},
    }
    arbitrary = client.post(
        f"/studio/projects/{project_id}/assets/preview",
        json={"asset_key": f"outputs/studio/{project_id}/secret.mp4"},
    )
    assert arbitrary.status_code == 422
    assert arbitrary.json()["detail"]["code"] == "asset_preview_not_allowed"

    download = client.post(
        f"/studio/projects/{project_id}/assets/download",
        json={"asset_key": upload_key},
    )
    assert download.status_code == 200
    assert download.json()["key"] == upload_key
    assert download.json()["upload_url"] == project_file_path(
        project_id, upload_key, "ember-knight-subject-take-1.png"
    )
    denied = client.post(
        f"/studio/projects/{project_id}/assets/download",
        json={"asset_key": f"outputs/studio/{project_id}/secret.mp4"},
    )
    assert denied.status_code == 422
    assert denied.json()["detail"]["code"] == "asset_preview_not_allowed"


def test_studio_routes_require_a_nonempty_trusted_actor_header() -> None:
    client = TestClient(create_app(_studio_runtime(), service_role=ServiceRole.API))

    missing = client.post("/studio/projects", json={"name": "Ember Knight"})
    blank = client.post(
        "/studio/projects",
        headers={"X-Studio-Actor-Id": "   "},
        json={"name": "Ember Knight"},
    )

    assert missing.status_code == 422
    assert blank.status_code == 422
    assert missing.json() == {
        "detail": {
            "code": "studio_actor_id_required",
            "message": "X-Studio-Actor-Id header is required",
        }
    }
    assert blank.json() == missing.json()


def test_auth_session_and_bootstrap_preserve_only_safe_same_origin_paths() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    session = client.get("/studio/auth/session")
    safe = client.get(
        "/studio/auth/bootstrap",
        params={"return_to": "/projects/project-1/motions?tab=ready"},
        follow_redirects=False,
    )
    unsafe = client.get(
        "/studio/auth/bootstrap",
        params={"return_to": "//evil.invalid/steal"},
        follow_redirects=False,
    )

    assert session.json()["actor_id"] == "owner-1"
    assert safe.status_code == 303
    assert safe.headers["location"] == "/projects/project-1/motions?tab=ready"
    assert unsafe.status_code == 400


def test_studio_routes_reject_an_overlong_trusted_actor_header() -> None:
    client = TestClient(create_app(_studio_runtime(), service_role=ServiceRole.API))

    response = client.post(
        "/studio/projects",
        headers={"X-Studio-Actor-Id": "a" * 256},
        json={"name": "Ember Knight"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "studio_actor_id_invalid",
            "message": "X-Studio-Actor-Id header must be 255 characters or fewer",
        }
    }


def test_studio_routes_reject_spoofed_body_identity_fields() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    create = client.post(
        "/studio/projects",
        json={"name": "Ember Knight", "owner_id": "spoofed"},
    )

    assert create.status_code == 422


def test_studio_routes_reject_overlong_project_names() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    response = client.post("/studio/projects", json={"name": "E" * 201})

    assert response.status_code == 422


def test_studio_routes_reject_overlong_asset_keys() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]

    response = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={
            "expected_revision": 1,
            "asset_key": "s" * 1025,
        },
    )

    assert response.status_code == 422


def test_subject_upload_creation_rejects_non_png_content_types_before_signing() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]

    response = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/jpeg", "expected_revision": 1},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "subject_content_type_invalid",
            "message": "subject uploads must use image/png",
        }
    }


def test_subject_settlement_rejects_a_missing_uploaded_asset() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]

    response = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={
            "expected_revision": 1,
            "asset_key": f"sources/studio/{project_id}/subjects/missing.png",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "subject_asset_missing",
            "message": "subject upload is missing from asset storage",
        }
    }


def test_subject_settlement_rejects_a_non_png_upload() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/uploaded.png"
    runtime.assets_store.assets[asset_key] = _jpeg_bytes(size=(1024, 1024))

    response = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "subject_png_format_invalid",
            "message": "subject upload must be a PNG image",
        }
    }


def test_subject_settlement_rejects_an_undecodable_png() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/uploaded.png"
    runtime.assets_store.assets[asset_key] = b"not a png"

    response = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "subject_png_decode_failed",
            "message": "subject upload must be a decodable PNG",
        }
    }


def test_subject_settlement_rejects_asset_keys_outside_subject_source_namespace() -> None:
    assets = _RecordingAssetStore()
    store = MemoryStudioStore()
    service = StudioService(store=store, assets=assets)
    client = _studio_client(
        InMemoryRuntime(MemoryJobStore(), assets, studio_service=service),
        actor_id="owner-1",
    )
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    assets.assets["sources/not-allowed.png"] = b"unexpected"

    response = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": "sources/not-allowed.png"},
    )
    fetched = client.get(f"/studio/projects/{project_id}")

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "subject_asset_key_invalid",
            "message": "subject upload key must be in the project subject source namespace",
        }
    }
    assert assets.read_calls == []
    assert fetched.json()["revision"] == 1
    assert fetched.json()["manifest"]["subject"]["current_revision"] is None


def test_subject_settlement_returns_404_for_missing_projects() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    response = client.post(
        "/studio/projects/missing-project/subject/settle",
        json={
            "expected_revision": 1,
            "asset_key": "sources/studio/missing-project/subjects/uploaded.png",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "studio project 'missing-project' was not found"}


def test_subject_settlement_maps_stale_expected_revisions_to_http_409() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    first_key = f"sources/studio/{project_id}/subjects/first.png"
    second_key = f"sources/studio/{project_id}/subjects/second.png"
    runtime.assets_store.assets[first_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )
    runtime.assets_store.assets[second_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(240, 240, 784, 784),
        mode="RGBA",
    )

    first = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": first_key},
    )
    stale = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": second_key},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 2


def test_subject_approval_with_a_finding_succeeds_and_records_it_acknowledged() -> None:
    """Quality findings are warnings, never blockers (operator decision, 2026-09-05):
    approving a subject with an edge-touching finding succeeds outright and the
    audit trail records the finding as acknowledged."""
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/edge-touching.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(0, 256, 768, 768),
        mode="RGBA",
    )

    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )
    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": 2},
    )

    assert settled.status_code == 200
    assert approved.status_code == 200
    approval = approved.json()["manifest"]["subject"]["approval"]
    assert approval["state"] == "waived"
    assert approval["waived_finding_ids"] == ["subject_touches_canvas_edge"]
    assert approval["waiver_reason"] == "acknowledged"


def test_subject_approval_with_a_custom_note_records_that_reason() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/edge-touching.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(0, 256, 768, 768),
        mode="RGBA",
    )

    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )
    assert settled.status_code == 200

    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={
            "expected_revision": 2,
            "waiver_reason": "Edge touch is intentional for this pose.",
        },
    )

    assert approved.status_code == 200
    approval = approved.json()["manifest"]["subject"]["approval"]
    assert approval["state"] == "waived"
    assert approval["waived_finding_ids"] == ["subject_touches_canvas_edge"]
    assert approval["waiver_reason"] == "Edge touch is intentional for this pose."


def test_subject_approval_rejects_unknown_waiver_id() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/edge-touching.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(0, 256, 768, 768),
        mode="RGBA",
    )

    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )
    assert settled.status_code == 200

    approved = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={
            "expected_revision": 2,
            "waived_finding_ids": ["not_a_real_finding"],
            "waiver_reason": "does not matter",
        },
    )

    assert approved.status_code == 422
    assert approved.json()["detail"]["code"] == "subject_waiver_unknown"
    assert approved.json()["detail"]["finding_ids"] == ["not_a_real_finding"]


def test_subject_approval_revision_conflict_still_409() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/uploaded.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )
    client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )

    response = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["current_revision"] == 2


def test_subject_approval_returns_404_for_missing_projects() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    response = client.post(
        "/studio/projects/missing-project/subject/approve",
        json={"expected_revision": 1},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "studio project 'missing-project' was not found"}


def test_studio_routes_hide_projects_from_other_actors() -> None:
    runtime = _studio_runtime()
    owner = _studio_client(runtime, actor_id="owner-1")
    other = _studio_client(runtime, actor_id="owner-2")
    project_id = owner.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/uploaded.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )

    assert other.get(f"/studio/projects/{project_id}").status_code == 404
    assert other.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    ).status_code == 404
    assert other.post(
        f"/studio/projects/{project_id}/assets/preview",
        json={"asset_key": asset_key},
    ).status_code == 404
    assert other.post(
        f"/studio/projects/{project_id}/assets/download",
        json={"asset_key": asset_key},
    ).status_code == 404
    assert other.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    ).status_code == 404
    assert other.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": 1},
    ).status_code == 404


def test_studio_approve_rejects_spoofed_approver_id_field() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]
    asset_key = f"sources/studio/{project_id}/subjects/uploaded.png"
    runtime.assets_store.assets[asset_key] = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )
    client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": asset_key},
    )

    response = client.post(
        f"/studio/projects/{project_id}/subject/approve",
        json={"expected_revision": 2, "approver_id": "spoofed"},
    )

    assert response.status_code == 422


def test_studio_routes_are_hidden_from_worker_role() -> None:
    runtime = _studio_runtime()
    api = _studio_client(runtime, actor_id="owner-1", role=ServiceRole.API)
    worker = _studio_client(runtime, actor_id="owner-1", role=ServiceRole.WORKER)
    project_id = api.post(
        "/studio/projects",
        json={"name": "Ember Knight"},
    ).json()["project_id"]

    assert worker.post("/studio/projects", json={"name": "Worker"}).status_code == 404
    assert worker.get(f"/studio/projects/{project_id}").status_code == 404


def test_studio_api_exposes_release_and_export_routes() -> None:
    app = create_app(_studio_runtime(), service_role=ServiceRole.API)
    methods_by_path: dict[str, set[str]] = {}
    for route in app.routes:
        if hasattr(route, "methods"):
            methods_by_path.setdefault(route.path, set()).update(route.methods)

    assert "POST" in methods_by_path["/studio/projects/{project_id}/set/approve"]
    assert "POST" in methods_by_path["/studio/projects/{project_id}/releases"]
    assert "GET" in methods_by_path["/studio/projects/{project_id}/releases"]
    assert "POST" in methods_by_path["/studio/releases/{release_id}/exports"]
    assert "GET" in methods_by_path["/studio/releases/{release_id}/exports"]
    assert "GET" in methods_by_path["/studio/exports/{export_id}"]


def test_project_history_lists_revisions_newest_action_last_created_first() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()["project_id"]

    missing = client.get("/studio/projects/missing-project/history")
    assert missing.status_code == 404

    history = client.get(f"/studio/projects/{project_id}/history")

    assert history.status_code == 200
    entries = history.json()
    assert len(entries) == 1
    assert entries[0]["timestamp"] is not None
    del entries[0]["timestamp"]
    assert entries == [
        {"revision": 1, "actor": "owner-1", "summary": "Created the project", "failed": False}
    ]


def test_release_export_not_found_response_does_not_leak_identifier() -> None:
    client = _export_error_client(ReleaseNotFound("secret-release-id"))

    response = client.post(
        "/studio/releases/secret-release-id/exports",
        json={"profile": "png-json", "expected_revision": 1},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "release not found"}


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            ReleaseAssetInvalid("release_asset_missing", "selected asset missing"),
            422,
            {"code": "release_asset_missing", "message": "selected asset missing"},
        ),
        (
            ImmutableExportCollision("private collision detail"),
            409,
            {
                "code": "immutable_export_collision",
                "message": "immutable export integrity conflict",
            },
        ),
        (
            ExportStorageUnavailable("private provider detail"),
            503,
            {
                "code": "export_storage_unavailable",
                "message": "export storage is temporarily unavailable",
            },
        ),
    ],
)
def test_release_export_storage_errors_have_stable_responses(
    error: Exception, status: int, detail: dict[str, str]
) -> None:
    client = _export_error_client(error)

    response = client.post(
        "/studio/releases/release-id/exports",
        json={"profile": "png-json", "expected_revision": 1},
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_list_release_exports_route_does_not_leak_a_strangers_release_identity() -> None:
    class _Service:
        async def list_release_exports(self, *_args: object) -> None:
            raise ReleaseNotFound("secret-release-id")

    app = FastAPI()
    app.include_router(create_studio_router(_Service()))
    client = TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.get("/studio/releases/secret-release-id/exports")

    assert response.status_code == 404
    assert response.json() == {"detail": "release not found"}


def _studio_client(
    runtime: InMemoryRuntime,
    *,
    actor_id: str,
    role: ServiceRole = ServiceRole.API,
) -> TestClient:
    return TestClient(
        create_app(runtime, service_role=role),
        headers={"X-Studio-Actor-Id": actor_id},
    )


def _studio_runtime() -> InMemoryRuntime:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    service = StudioService(store=store, assets=assets)
    return InMemoryRuntime(
        MemoryJobStore(),
        assets,
        studio_service=service,
    )


def _studio_runtime_with_motion_generation() -> InMemoryRuntime:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=NullTaskDispatcher(),
        motion_generation_available=True,
    )
    return InMemoryRuntime(jobs, assets, studio_service=service)


def _export_error_client(error: Exception) -> TestClient:
    class _Service:
        async def create_export(self, *_args: object) -> None:
            raise error

    app = FastAPI()
    app.include_router(create_studio_router(_Service()))
    return TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})


class _RecordingAssetStore(MemoryAssetStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls: list[str] = []

    async def read(self, key: str) -> bytes:
        self.read_calls.append(key)
        return await super().read(key)


def _png_bytes(
    *,
    size: tuple[int, int],
    subject_box: tuple[int, int, int, int],
    mode: str,
) -> bytes:
    background = (0, 0, 0, 0) if mode == "RGBA" else (255, 255, 255)
    foreground = (255, 64, 64, 255) if mode == "RGBA" else (255, 64, 64)
    image = Image.new(mode, size, background)
    subject = Image.new(
        mode,
        (subject_box[2] - subject_box[0], subject_box[3] - subject_box[1]),
        foreground,
    )
    image.paste(subject, (subject_box[0], subject_box[1]))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(*, size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (255, 64, 64))
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("code", "message", "retryable"),
    [
        (
            JobFailureCode.TEMPORARY_FAILURE,
            "Generation was interrupted by a temporary service problem. Retry this motion.",
            True,
        ),
        (
            JobFailureCode.INVALID_INPUT,
            "The motion settings are invalid. Inspect and edit the motion before retrying.",
            False,
        ),
    ],
)
def test_operation_failure_response_exposes_only_stable_public_details(
    code: JobFailureCode, message: str, retryable: bool
) -> None:
    response = OperationResponse.from_domain(
        JobRecord(
            job_id="job-1",
            idempotency_key="request-1",
            operation=JobOperation.GENERATE_MOTION,
            status=JobStatus.FAILED,
            source_key="sources/studio/project-1/subjects/subject.png",
            output_key="outputs/studio/project-1/motions/idle/result.mp4",
            attempts=5,
            project_id="project-1",
            payload={"motion_id": "idle"},
            error="provider secret token sk-do-not-expose",
            failure_code=code,
            failure_message=message,
            retryable=retryable,
        )
    ).model_dump(mode="json")

    assert response["failure_code"] == code
    assert response["failure_explanation"] == message
    assert response["retryable"] is retryable
    assert "sk-do-not-expose" not in str(response)


def test_operation_response_carries_generation_settings_and_prompt_for_a_motion_job() -> None:
    response = OperationResponse.from_domain(
        JobRecord(
            job_id="job-1",
            idempotency_key="request-1",
            operation=JobOperation.GENERATE_MOTION,
            status=JobStatus.QUEUED,
            source_key="sources/studio/project-1/subjects/subject.png",
            output_key="outputs/studio/project-1/motions/idle/result.mp4",
            attempts=0,
            project_id="project-1",
            payload={
                "motion_id": "idle",
                "prompt": "subtle breathing idle",
                "preset": "high",
                "steps": 28,
                "seed": 123,
                "width": 768,
                "height": 768,
                "duration_seconds": 6,
                "provider_parameters": {"motion_strength": 0.75},
            },
        )
    ).model_dump(mode="json")

    assert response["prompt"] == "subtle breathing idle"
    assert response["generation_settings"] == {
        "preset": "high", "steps": 28, "seed": 123, "width": 768, "height": 768,
        "duration_seconds": 6, "backdrop": "chroma_green",
        "provider_parameters": {"motion_strength": 0.75},
    }


def test_operation_response_preserves_native_alpha_backdrop_for_a_motion_job() -> None:
    """T4: an operation generated with backdrop='native_alpha' must not be
    reconstructed with the chroma_green default -- History and
    'Regenerate with these settings' both read this field."""
    response = OperationResponse.from_domain(
        JobRecord(
            job_id="job-3",
            idempotency_key="request-3",
            operation=JobOperation.GENERATE_MOTION,
            status=JobStatus.QUEUED,
            source_key="sources/studio/project-1/subjects/subject.png",
            output_key="outputs/studio/project-1/motions/idle/result.mp4",
            attempts=0,
            project_id="project-1",
            payload={
                "motion_id": "idle",
                "prompt": "subtle breathing idle",
                "backdrop": "native_alpha",
            },
        )
    ).model_dump(mode="json")

    assert response["generation_settings"]["backdrop"] == "native_alpha"


def test_operation_response_omits_generation_settings_for_a_subject_job() -> None:
    response = OperationResponse.from_domain(
        JobRecord(
            job_id="job-2",
            idempotency_key="request-2",
            operation=JobOperation.GENERATE_SUBJECT,
            status=JobStatus.QUEUED,
            source_key="sources/studio/project-1/subject-references/ref.png",
            output_key="outputs/studio/project-1/subjects/op-1.png",
            attempts=0,
            project_id="project-1",
            payload={},
        )
    ).model_dump(mode="json")

    assert response["prompt"] is None
    assert response["generation_settings"] is None


def test_activity_route_paginates_with_a_cursor_and_limit() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    ).json()
    for index in range(3):
        current = client.get(f"/studio/projects/{project_id}").json()
        generated = client.post(
            f"/studio/projects/{project_id}/motions/idle/generate",
            json={"expected_revision": current["revision"], "request_id": f"req-{index}"},
        )
        assert generated.status_code == 200, generated.text

    first_page = client.get(
        f"/studio/projects/{project_id}/activity", params={"limit": 2}
    ).json()
    assert len(first_page["operations"]) == 2
    assert first_page["next_cursor"] is not None

    second_page = client.get(
        f"/studio/projects/{project_id}/activity",
        params={"limit": 2, "cursor": first_page["next_cursor"]},
    ).json()
    assert len(second_page["operations"]) == 1
    assert second_page["next_cursor"] is None
    assert {op["job_id"] for op in first_page["operations"]} | {
        op["job_id"] for op in second_page["operations"]
    } == {
        op["job_id"]
        for op in client.get(f"/studio/projects/{project_id}/operations").json()
    }


def test_list_projects_route_is_empty_for_an_owner_with_no_projects() -> None:
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    response = client.get("/studio/projects")

    assert response.status_code == 200
    assert response.json() == []


def test_list_projects_route_orders_newest_first_and_filters_to_the_caller() -> None:
    runtime = _studio_runtime()
    owner_client = _studio_client(runtime, actor_id="owner-1")
    other_client = _studio_client(runtime, actor_id="owner-2")

    older = owner_client.post("/studio/projects", json={"name": "Older"}).json()
    newer = owner_client.post("/studio/projects", json={"name": "Newer"}).json()
    other_client.post("/studio/projects", json={"name": "Not Mine"})

    response = owner_client.get("/studio/projects")

    assert response.status_code == 200
    body = response.json()
    assert [item["project_id"] for item in body] == [
        newer["project_id"],
        older["project_id"],
    ]
    listed = body[0]
    assert listed["name"] == "Newer"
    assert listed["revision"] == 1
    assert listed["created_by"] == "owner-1"
    assert listed["subject_state"] == "none"
    assert listed["motion_count"] == 0
    assert listed["release_count"] == 0
    assert listed["external_subject"] is None
    assert listed["activity"] == {
        "running": 0, "failed": 0, "completed": 0, "last_activity_at": None,
    }

    # Touching the older project's revision must move it back to the front.
    upload = owner_client.post(
        f"/studio/projects/{older['project_id']}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    ).json()
    runtime.assets_store.assets[upload["key"]] = _png_bytes(
        size=(1024, 1024), subject_box=(200, 200, 824, 824), mode="RGBA",
    )
    owner_client.post(
        f"/studio/projects/{older['project_id']}/subject/settle",
        json={"expected_revision": 1, "asset_key": upload["key"]},
    )

    reordered = owner_client.get("/studio/projects").json()
    assert reordered[0]["project_id"] == older["project_id"]
    assert reordered[0]["subject_state"] == "pending"


def test_list_projects_route_derives_activity_stats_from_the_operations_ledger() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    ).json()
    generated = client.post(
        f"/studio/projects/{project_id}/motions/idle/generate",
        json={"expected_revision": added["revision"], "request_id": "req-running"},
    ).json()
    assert generated["status"] == "queued"

    runtime.jobs.records["job-failed"] = JobRecord(
        job_id="job-failed",
        idempotency_key="req-failed",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.FAILED,
        source_key="sources/studio/x",
        output_key="outputs/studio/x",
        attempts=1,
        project_id=project_id,
        payload={"motion_id": "idle"},
        failure_code=JobFailureCode.TEMPORARY_FAILURE,
        retryable=True,
    )
    runtime.jobs.records["job-completed"] = JobRecord(
        job_id="job-completed",
        idempotency_key="req-completed",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.COMPLETED,
        source_key="sources/studio/x",
        output_key="outputs/studio/x",
        attempts=1,
        project_id=project_id,
        payload={"motion_id": "idle"},
    )

    response = client.get("/studio/projects")

    assert response.status_code == 200
    activity = response.json()[0]["activity"]
    assert activity["running"] == 1
    assert activity["failed"] == 1
    assert activity["completed"] == 1
    assert activity["last_activity_at"] is not None


async def test_list_projects_route_flags_an_unreadable_project_instead_of_failing() -> None:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    service = StudioService(store=store, assets=assets)
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = _studio_client(runtime, actor_id="owner-1")

    readable = client.post("/studio/projects", json={"name": "Readable"}).json()
    broken = client.post("/studio/projects", json={"name": "Broken"}).json()
    broken_id = broken["project_id"]

    stored = await store.get(broken_id)
    assert stored is not None
    corrupt = stored.manifest.model_dump(mode="json")
    corrupt["name"] = "Broken raw name"
    corrupt["motions"] = [None]
    await store.append_raw(broken_id, corrupt)

    response = client.get("/studio/projects")

    assert response.status_code == 200
    body = {item["project_id"]: item for item in response.json()}
    assert set(body) == {readable["project_id"], broken_id}

    readable_item = body[readable["project_id"]]
    assert readable_item["unreadable"] is False

    broken_item = body[broken_id]
    assert broken_item["unreadable"] is True
    assert broken_item["name"] == "Broken raw name"
    assert broken_item["subject_state"] == "none"
    assert broken_item["motion_count"] == 0
    assert broken_item["release_count"] == 0
    assert broken_item["revision"] == 2


def _project_with_approved_subject(
    client: TestClient, runtime: InMemoryRuntime
) -> tuple[str, int]:
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


def test_presets_route_lists_the_full_library() -> None:
    """Slot aliasing moved to each target adapter's own descriptor, surfaced
    through `GET /studio/targets` -- see tests/targets/contract.py and the
    reference adapter's own tests for that."""
    client = _studio_client(_studio_runtime(), actor_id="owner-1")

    response = client.get("/studio/presets")

    assert response.status_code == 200
    by_preset = {item["preset"]: item for item in response.json()}
    assert set(by_preset) == {
        "idle", "walk", "run", "jump", "attack", "guard", "cast", "hurt", "death", "trip",
    }
    assert by_preset["attack"]["animation_type"] == "attack"
    assert by_preset["cast"]["animation_type"] == "spellcast"


def test_targets_route_lists_every_registered_adapter() -> None:
    """Route-layer proof that `GET /studio/targets` renders from whatever
    registry it is handed -- no target's name is baked into the route. Which
    adapter is actually installed in production is a separate, adapter-owned
    test (the reference adapter's own registry/contract tests)."""
    registry = TargetRegistry()
    registry.register(DummyTargetAdapter())
    app = FastAPI()
    app.include_router(
        create_studio_router(_studio_runtime().studio_service, registry=registry)
    )
    client = TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.get("/studio/targets")

    assert response.status_code == 200
    by_system = {item["system"]: item for item in response.json()}
    assert "dummy" in by_system
    dummy = by_system["dummy"]
    assert dummy["display_name"] == "Dummy Target"
    assert set(dummy["entity_kinds"]) == {"widget"}
    slot_names = {slot["name"] for slot in dummy["slots"]}
    assert slot_names == {"spin", "rest"}
    spin_slot = next(slot for slot in dummy["slots"] if slot["name"] == "spin")
    assert set(spin_slot["accepts"]) == {"spin", "twirl"}
    assert spin_slot["winner"] == "spin"
    assert isinstance(dummy["delivery_configured"], bool)


def test_add_motions_batch_route_adds_the_recommended_set_in_one_revision() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)

    response = client.post(
        f"/studio/projects/{project_id}/motions/batch",
        json={
            "expected_revision": revision,
            "motions": [
                {"motion_id": "idle", "name": "Idle", "kind": "preset", "preset": "idle"},
                {"motion_id": "walk", "name": "Walk", "kind": "preset", "preset": "walk"},
                {"motion_id": "attack", "name": "Attack", "kind": "preset", "preset": "attack"},
                {"motion_id": "guard", "name": "Guard", "kind": "preset", "preset": "guard"},
                {"motion_id": "cast", "name": "Cast", "kind": "preset", "preset": "cast"},
                {"motion_id": "hurt", "name": "Hurt", "kind": "preset", "preset": "hurt"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [m["motion_id"] for m in body["manifest"]["motions"]] == [
        "idle", "walk", "attack", "guard", "cast", "hurt",
    ]


def test_add_motions_batch_route_rejects_duplicate_motion_ids_with_409() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    )

    response = client.post(
        f"/studio/projects/{project_id}/motions/batch",
        json={
            "expected_revision": revision + 1,
            "motions": [
                {"motion_id": "idle", "name": "Idle again", "kind": "preset", "preset": "idle"},
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["motion_ids"] == ["idle"]


def test_generate_motions_batch_route_queues_every_motion_and_rejects_shared_pose() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
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

    shared_pose = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": added["revision"],
            "request_id": "req-1",
            "motion_ids": ["idle", "walk"],
            "shared_starting_pose": True,
        },
    )
    assert shared_pose.status_code == 422

    response = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": added["revision"],
            "request_id": "req-1",
            "motion_ids": ["idle", "walk"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert {item["motion_id"] for item in body["queued"]} == {"idle", "walk"}
    assert body["failed"] == []


def test_generate_motions_batch_route_persists_the_what_happens_lines_first() -> None:
    """The batch confirm sends the operator's edited lines with the dispatch: one revision
    stores them, and every queued job renders from the recompiled prompt."""
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions/batch",
        json={
            "expected_revision": revision,
            "motions": [
                {"motion_id": "attack", "name": "Attack", "kind": "preset", "preset": "attack"},
                {"motion_id": "walk", "name": "Walk", "kind": "preset", "preset": "walk"},
            ],
        },
    ).json()
    attack_before = next(
        motion for motion in added["manifest"]["motions"] if motion["motion_id"] == "attack"
    )
    assert attack_before["action"] == "swings a weapon"

    response = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": added["revision"],
            "request_id": "req-1",
            "motion_ids": ["attack", "walk"],
            "actions": {"attack": "draws and fires the bow"},
        },
    )

    assert response.status_code == 200
    assert {item["motion_id"] for item in response.json()["queued"]} == {"attack", "walk"}
    project = client.get(f"/studio/projects/{project_id}").json()
    attack = next(
        motion for motion in project["manifest"]["motions"] if motion["motion_id"] == "attack"
    )
    walk = next(
        motion for motion in project["manifest"]["motions"] if motion["motion_id"] == "walk"
    )
    assert attack["action"] == "draws and fires the bow"
    assert attack["prompt"].startswith("The character draws and fires the bow:")
    assert "returns to the initial ready stance" in attack["prompt"]
    # An untouched motion keeps its preset default; each queued dispatch also reserves a revision.
    assert walk["action"] == "walks in place"
    assert project["revision"] == added["revision"] + 1 + len(response.json()["queued"])


@pytest.mark.parametrize("action_length", [200, 201])
def test_generate_motions_batch_route_validates_action_length_before_mutation(
    action_length: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue = AsyncMock()
    monkeypatch.setattr(NullTaskDispatcher, "enqueue", enqueue)
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added_response = client.post(
        f"/studio/projects/{project_id}/motions/batch",
        json={
            "expected_revision": revision,
            "motions": [
                {"motion_id": "attack", "name": "Attack", "kind": "preset", "preset": "attack"},
                {"motion_id": "walk", "name": "Walk", "kind": "preset", "preset": "walk"},
            ],
        },
    )
    assert added_response.status_code == 200
    before = client.get(f"/studio/projects/{project_id}").json()
    jobs_before = dict(runtime.jobs.records)
    action = "a" * action_length

    response = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": before["revision"],
            "request_id": "req-action-length",
            "motion_ids": ["attack", "walk"],
            "actions": {"attack": "draws a bow", "walk": action},
        },
    )

    after = client.get(f"/studio/projects/{project_id}").json()
    if action_length > 200:
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "actions", "walk"]
        assert response.json()["detail"][0]["type"] == "string_too_long"
        assert after == before
        assert runtime.jobs.records == jobs_before
        enqueue.assert_not_awaited()
    else:
        assert response.status_code == 200
        assert response.json()["failed"] == []
        assert {item["motion_id"] for item in response.json()["queued"]} == {
            "attack",
            "walk",
        }
        assert after["revision"] == before["revision"] + 1 + len(response.json()["queued"])
        walk = next(
            motion
            for motion in after["manifest"]["motions"]
            if motion["motion_id"] == "walk"
        )
        assert walk["action"] == action
        assert enqueue.await_count == 2
        assert len(runtime.jobs.records) == len(jobs_before) + 2


def test_generate_motion_route_accepts_an_edited_what_happens_line() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision,
            "motion_id": "cast",
            "name": "Cast",
            "kind": "preset",
            "preset": "cast",
        },
    ).json()

    response = client.post(
        f"/studio/projects/{project_id}/motions/cast/generate",
        json={
            "expected_revision": added["revision"],
            "request_id": "req-1",
            "action": "calls down a pillar of light",
        },
    )

    assert response.status_code == 200
    project = client.get(f"/studio/projects/{project_id}").json()
    cast = next(
        motion for motion in project["manifest"]["motions"] if motion["motion_id"] == "cast"
    )
    assert cast["action"] == "calls down a pillar of light"
    assert "calls down a pillar of light: hands rise" in cast["prompt"]


def test_project_response_lists_live_operations() -> None:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    service = StudioService(
        store=store,
        assets=assets,
        jobs=jobs,
        dispatcher=NullTaskDispatcher(),
        motion_generation_available=True,
    )
    client = _studio_client(
        InMemoryRuntime(jobs, assets, studio_service=service), actor_id="owner-1"
    )
    project_id = client.post("/studio/projects", json={"name": "Ember"}).json()["project_id"]
    jobs.records["queued-1"] = JobRecord(
        job_id="queued-1",
        idempotency_key="queued-1",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.QUEUED,
        source_key=f"sources/studio/{project_id}/subjects/source.png",
        output_key=f"outputs/studio/{project_id}/motions/idle/queued-1.mp4",
        attempts=1,
        project_id=project_id,
        manifest_operation_id="queued-1",
        payload={"motion_id": "idle"},
    )

    project = client.get(f"/studio/projects/{project_id}").json()

    assert [(op["job_id"], op["stage"]) for op in project["active_operations"]] == [
        ("queued-1", "queued")
    ]


def test_cancel_motion_operation_route_cancels_a_queued_job() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    ).json()
    generated = client.post(
        f"/studio/projects/{project_id}/motions/idle/generate",
        json={"expected_revision": added["revision"], "request_id": "req-1"},
    ).json()

    response = client.post(
        f"/studio/projects/{project_id}/motions/idle/operations/{generated['job_id']}/cancel",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["stage"] == "cancelled"
    assert body["retryable"] is True


def test_cancel_motion_operation_route_rejects_a_terminal_job_with_409() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    ).json()
    generated = client.post(
        f"/studio/projects/{project_id}/motions/idle/generate",
        json={"expected_revision": added["revision"], "request_id": "req-1"},
    ).json()
    job_id = generated["job_id"]
    first_cancel = client.post(
        f"/studio/projects/{project_id}/motions/idle/operations/{job_id}/cancel",
    )
    assert first_cancel.status_code == 200

    response = client.post(
        f"/studio/projects/{project_id}/motions/idle/operations/{job_id}/cancel",
    )

    assert response.status_code == 409
    assert "cancelled" in response.json()["detail"]


def test_cancel_motion_operation_route_returns_404_for_a_foreign_actor() -> None:
    runtime = _studio_runtime_with_motion_generation()
    owner_client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(owner_client, runtime)
    added = owner_client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": revision, "motion_id": "idle", "name": "Idle",
            "kind": "preset", "preset": "idle",
        },
    ).json()
    generated = owner_client.post(
        f"/studio/projects/{project_id}/motions/idle/generate",
        json={"expected_revision": added["revision"], "request_id": "req-1"},
    ).json()

    other_client = _studio_client(runtime, actor_id="someone-else")
    response = other_client.post(
        f"/studio/projects/{project_id}/motions/idle/operations/{generated['job_id']}/cancel",
    )

    assert response.status_code == 404


def test_cancel_all_operations_route_cancels_every_live_job() -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
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
    batch = client.post(
        f"/studio/projects/{project_id}/motions/generate-batch",
        json={
            "expected_revision": added["revision"], "request_id": "req-1",
            "motion_ids": ["idle", "walk"],
        },
    ).json()
    assert len(batch["queued"]) == 2

    response = client.post(f"/studio/projects/{project_id}/operations/cancel-all")

    assert response.status_code == 200
    body = response.json()
    assert set(body["cancelled"]) == {item["operation"]["job_id"] for item in batch["queued"]}
    assert body["skipped"] == []

    operations = client.get(f"/studio/projects/{project_id}/operations").json()
    assert {op["job_id"]: op["status"] for op in operations} == {
        item["operation"]["job_id"]: "cancelled" for item in batch["queued"]
    }


def test_patch_project_route_binds_and_clears_external_subject_without_touching_approval() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)

    bound = client.patch(
        f"/studio/projects/{project_id}",
        json={
            "expected_revision": revision,
            "external_subject": {
                "system": "example", "entity_kind": "character", "entity_id": "char-42",
                "display_name": "Ser Ember",
            },
        },
    )

    assert bound.status_code == 200
    body = bound.json()
    assert body["manifest"]["external_subject"] == {
        "system": "example", "entity_kind": "character",
        "entity_id": "char-42", "display_name": "Ser Ember",
    }
    # Binding is a pure re-tag: the subject approval this project already earned
    # (`_project_with_approved_subject`) survives untouched.
    assert body["manifest"]["subject"]["approval"]["state"] == "approved"

    cleared = client.patch(
        f"/studio/projects/{project_id}",
        json={"expected_revision": body["revision"], "external_subject": None},
    )

    assert cleared.status_code == 200
    assert cleared.json()["manifest"]["external_subject"] is None


def test_patch_project_route_rejects_stale_revision() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)

    response = client.patch(
        f"/studio/projects/{project_id}",
        json={
            "expected_revision": revision + 5,
            "external_subject": {
                "system": "example", "entity_kind": "character", "entity_id": "char-42",
                "display_name": "Ser Ember",
            },
        },
    )

    assert response.status_code == 409


def test_preset_motion_prompt_remains_anonymous_once_bound() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id, revision = _project_with_approved_subject(client, runtime)
    bound = client.patch(
        f"/studio/projects/{project_id}",
        json={
            "expected_revision": revision,
            "external_subject": {
                "system": "example", "entity_kind": "character", "entity_id": "char-42",
                "display_name": "Ser Ember",
            },
        },
    ).json()

    added = client.post(
        f"/studio/projects/{project_id}/motions",
        json={
            "expected_revision": bound["revision"],
            "motion_id": "idle", "name": "Idle", "kind": "preset", "preset": "idle",
        },
    ).json()

    prompt = added["manifest"]["motions"][0]["prompt"]
    assert prompt.startswith("The character ")
    assert "Ser Ember" not in prompt
    assert "Ember Knight" not in prompt


async def test_repair_project_route_restores_and_returns_report() -> None:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    service = StudioService(store=store, assets=assets)
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = _studio_client(runtime, actor_id="owner-1")

    created = client.post(
        "/studio/projects",
        json={"name": "Ember Knight", "preset_motions": ["idle", "walk"]},
    )
    assert created.status_code == 200
    project_id = created.json()["project_id"]

    stored = await store.get(project_id)
    assert stored is not None
    corrupt = stored.manifest.model_dump(mode="json")
    corrupt["motions"] = [None, None]
    await store.append_raw(project_id, corrupt)

    response = client.post(f"/studio/projects/{project_id}/repair")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["repaired"] is True
    assert body["current_revision_before"] == 2
    assert body["restored_from_revision"] == 1
    assert body["new_revision"] == 3
    assert len(body["bad_revisions"]) == 1
    assert body["bad_revisions"][0]["null_paths"] == ["motions[0]", "motions[1]"]

    second = client.post(f"/studio/projects/{project_id}/repair")
    assert second.status_code == 200
    assert second.json()["repaired"] is False

    stranger_client = _studio_client(runtime, actor_id="someone-else")
    stranger_response = stranger_client.post(f"/studio/projects/{project_id}/repair")
    assert stranger_response.status_code == 404


async def test_repair_project_route_404s_for_unknown_project() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")

    response = client.post("/studio/projects/does-not-exist/repair")

    assert response.status_code == 404


def test_get_project_route_maps_manifest_unreadable_to_503() -> None:
    class _Service:
        async def get_project(self, *_args: object) -> None:
            raise ManifestUnreadable("proj-x", 4)

    runtime = InMemoryRuntime(
        MemoryJobStore(), MemoryAssetStore(), studio_service=_Service()
    )
    client = TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.get("/studio/projects/proj-x")

    assert response.status_code == 503
    assert response.json()["code"] == "manifest_unreadable"


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        "yesterday|00000000-0000-0000-0000-000000000001",
        "2026-10-21T00:00:00+00:00|",
        # No offset: Postgres cannot compare it with `timestamptz`.
        "2026-10-21T00:00:00|00000000-0000-0000-0000-000000000001",
    ],
)
def test_activity_route_rejects_a_malformed_cursor_with_422(cursor: str) -> None:
    runtime = _studio_runtime_with_motion_generation()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember"}).json()["project_id"]

    response = client.get(
        f"/studio/projects/{project_id}/activity", params={"cursor": cursor}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_cursor"


class _ExportReadService:
    """Fails every export read the way a damaged or unreachable store would."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def get_export(self, *_args: object) -> None:
        raise self.error

    async def export_file(self, *_args: object) -> None:
        raise self.error

    async def create_export_download(self, *_args: object) -> None:
        raise self.error


@pytest.mark.parametrize("path", ["", "/file", "/download"])
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ReleaseIntegrityError("digest mismatch"), 409, "release_integrity_error"),
        (ImmutableExportCollision("private detail"), 409, "immutable_export_collision"),
        (ExportStorageUnavailable("private detail"), 503, "export_storage_unavailable"),
    ],
)
def test_export_read_routes_map_storage_errors_alike(
    path: str, error: Exception, status: int, code: str
) -> None:
    app = FastAPI()
    app.include_router(create_studio_router(_ExportReadService(error)))
    client = TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.get(f"/studio/exports/export-1{path}")

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_export_file_route_reports_an_asset_storage_outage_as_503() -> None:
    class _Assets:
        async def describe(self, _key: str) -> None:
            raise AssetStorageUnavailable("bucket unreachable")

    class _Service:
        assets = _Assets()

        async def export_file(self, *_args: object) -> tuple[str, str]:
            return "outputs/studio/p/exports/e.zip", "ember.zip"

    app = FastAPI()
    app.include_router(create_studio_router(_Service()))
    client = TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.get("/studio/exports/export-1/file")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "export_storage_unavailable"


def test_repair_project_route_reports_a_concurrent_write_as_409() -> None:
    class _Service:
        async def repair_project(self, *_args: object) -> None:
            raise RevisionConflict(7)

    app = FastAPI()
    app.include_router(create_studio_router(_Service()))
    client = TestClient(app, headers={"X-Studio-Actor-Id": "owner-1"})

    response = client.post("/studio/projects/project-1/repair")

    assert response.status_code == 409
    assert response.json()["detail"]["current_revision"] == 7
