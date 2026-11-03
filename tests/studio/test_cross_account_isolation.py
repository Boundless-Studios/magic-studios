"""One bucket and one database hold every operator's projects, so the tenancy
boundary is application code alone. These tests walk it exhaustively.

Two properties are locked here:

1. *Every* owner-scoped studio route refuses a second actor. The route table is
   checked against the live app, so adding a route that carries `{project_id}`,
   `{release_id}` or `{export_id}` without an ownership check fails
   `test_every_owner_scoped_route_is_covered` until it is probed here.
2. A caller-supplied asset key belonging to another project is refused even when
   the caller owns the project id in the path -- owning the path is not owning
   the key.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import JobOperation, JobSubmission
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher

OWNER = "owner-1"
STRANGER = "owner-2"

REFUSAL_STATUSES = frozenset({403, 404})

_MOTION_ID = "idle"
_MOTION = {
    "motion_id": _MOTION_ID,
    "name": "Idle",
    "kind": "preset",
    "preset": "idle",
    "prompt_source": "preset",
}

_CANDIDATE = (
    "/studio/projects/{project_id}/motions/{motion_id}/candidates/{candidate_id}"
)

# path template -> (method, body). The body only has to be schema-valid: the
# ownership check must fire before anything else, so a stranger never reaches
# the parts of a request that depend on real manifest content.
OWNER_SCOPED_ROUTES: dict[tuple[str, str], dict[str, Any] | None] = {
    ("GET", "/studio/projects/{project_id}"): None,
    # Even with the right name, a stranger can never delete someone's project.
    ("DELETE", "/studio/projects/{project_id}"): {"confirm_name": "Ember Knight"},
    ("GET", "/studio/projects/{project_id}/setup"): None,
    ("POST", "/studio/projects/{project_id}/setup/preview"): {
        "expected_revision": 1, "desired_setup": {"schema_version": 1, "sprite_layout": "single_view", "views": []}},
    ("POST", "/studio/projects/{project_id}/setup/apply"): {"expected_revision": 1, "plan_token": "foreign"},
    ("POST", "/studio/projects/{project_id}/setup/references"): {"content_base64": "cG5n", "content_type": "image/png"},
    ("POST", "/studio/projects/{project_id}/setup/dispatches/{operation_id}/abandon"): {
        "expected_revision": 1, "confirm_dispatch_stopped": True},
    ("PATCH", "/studio/projects/{project_id}"): {
        "expected_revision": 1,
        "external_subject": None,
    },
    ("PUT", "/studio/projects/{project_id}/sprite-layout"): {
        "expected_revision": 1, "sprite_layout": "isometric",
    },
    ("PUT", "/studio/projects/{project_id}/views/{view_id}"): {
        "expected_revision": 1, "name": "North",
        "reference": {"key": "sources/studio/other/views/N/references/a.png", "sha256": "a" * 64},
    },
    ("POST", "/studio/projects/{project_id}/views/{view_id}/reference/uploads"): {
        "expected_revision": 1, "content_type": "image/png",
    },
    ("GET", "/studio/projects/{project_id}/activity"): None,
    ("POST", "/studio/projects/{project_id}/activity/reconcile"): {},
    ("POST", "/studio/projects/{project_id}/assets/download"): {
        "asset_key": "outputs/studio/other/subjects/a.png"
    },
    ("POST", "/studio/projects/{project_id}/assets/preview"): {
        "asset_key": "outputs/studio/other/subjects/a.png"
    },
    ("POST", "/studio/projects/{project_id}/composition/approve"): {
        "expected_revision": 1
    },
    ("POST", "/studio/projects/{project_id}/composition/compose"): {
        "expected_revision": 1,
        "prompt": "a hero",
        "request_id": "req1",
    },
    (
        "POST",
        "/studio/projects/{project_id}/composition/operations/{job_id}/settle",
    ): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/composition/regenerate"): {
        "expected_revision": 1,
        "prompt": "a hero",
        "request_id": "req1",
    },
    ("GET", "/studio/projects/{project_id}/gpu-telemetry"): None,
    ("GET", "/studio/projects/{project_id}/history"): None,
    ("POST", "/studio/projects/{project_id}/motions"): {
        **_MOTION,
        "expected_revision": 1,
    },
    ("POST", "/studio/projects/{project_id}/motions/batch"): {
        "expected_revision": 1,
        "motions": [_MOTION],
    },
    ("POST", "/studio/projects/{project_id}/motions/generate-batch"): {
        "expected_revision": 1,
        "request_id": "req1",
        "motion_ids": [_MOTION_ID],
    },
    ("PATCH", "/studio/projects/{project_id}/motions/{motion_id}"): {
        **_MOTION,
        "expected_revision": 1,
    },
    (
        "POST",
        "/studio/projects/{project_id}/motions/{motion_id}/candidates/settle-upload",
    ): {
        "expected_revision": 1,
        "asset_key": "sources/studio/other/motion-uploads/a.png",
        "frame_count": 4,
    },
    (
        "POST",
        "/studio/projects/{project_id}/motions/{motion_id}/candidates/uploads",
    ): {"expected_revision": 1, "content_type": "image/png"},
    ("POST", f"{_CANDIDATE}/derive-alpha"): {"expected_revision": 1},
    ("GET", f"{_CANDIDATE}/preview.gif"): None,
    ("GET", f"{_CANDIDATE}/preview.webm"): None,
    ("POST", f"{_CANDIDATE}/select"): {"expected_revision": 1},
    ("GET", f"{_CANDIDATE}/sheet.png"): None,
    ("GET", f"{_CANDIDATE}/frames"): None,
    ("POST", f"{_CANDIDATE}/loop-suggestion"): {"mode": "attack", "target_frame_count": 12},
    ("POST", f"{_CANDIDATE}/body-calibration"): {
        "source_frame": 0, "reference_motion_id": "walk", "reference_candidate_id": "reference",
    },
    ("POST", "/studio/projects/{project_id}/motions/{motion_id}/generate"): {
        "expected_revision": 1,
        "request_id": "req1",
    },
    (
        "POST",
        "/studio/projects/{project_id}/motions/{motion_id}/operations/{job_id}/cancel",
    ): {},
    (
        "POST",
        "/studio/projects/{project_id}/motions/{motion_id}/operations/{job_id}/settle",
    ): {"expected_revision": 1},
    (
        "POST",
        "/studio/projects/{project_id}/motions/{motion_id}/references/uploads",
    ): {"expected_revision": 1, "content_type": "image/png"},
    ("POST", "/studio/projects/{project_id}/motions/{motion_id}/revisions"): {
        "expected_revision": 1,
        "source_candidate_id": "candidate-1",
        "ordered_frames": [0, 1],
        "frame_duration_ms": 100,
        "loop_policy": "loop",
    },
    ("GET", "/studio/projects/{project_id}/operations"): None,
    ("POST", "/studio/projects/{project_id}/operations/cancel-all"): {},
    # A stranger can neither start, read, inspect, pick from nor cancel someone's turnaround.
    ("POST", "/studio/projects/{project_id}/turnarounds"): {
        "expected_revision": 1, "source_view_id": "S", "source_revision_id": "foreign", "request_id": "probe"},
    ("GET", "/studio/projects/{project_id}/turnarounds"): None,
    ("GET", "/studio/projects/{project_id}/turnarounds/{turnaround_id}"): None,
    ("GET", "/studio/projects/{project_id}/turnarounds/{turnaround_id}/frames"): None,
    ("POST", "/studio/projects/{project_id}/turnarounds/{turnaround_id}/cancel"): None,
    ("PUT", "/studio/projects/{project_id}/turnarounds/{turnaround_id}/picks"): {
        "expected_pick_revision": 0, "picks": {"N": {"frame_index": 1}}},
    # Nor make, list, check or discard someone's direction drafts.
    ("POST", "/studio/projects/{project_id}/view-drafts"): {
        "expected_revision": 1, "turnaround_id": "turnaround-" + "0" * 32, "pick_revision": 1,
        "heading": "N", "request_id": "probe"},
    ("GET", "/studio/projects/{project_id}/view-drafts"): None,
    ("POST", "/studio/projects/{project_id}/view-drafts/{draft_id}/check"): {
        "expected_revision": 1, "facing": True, "equipment": True, "identity": True},
    ("POST", "/studio/projects/{project_id}/view-drafts/{draft_id}/discard"): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/quality/set"): {"expected_revision": 1},
    ("GET", "/studio/projects/{project_id}/releases"): None,
    ("POST", "/studio/projects/{project_id}/releases"): {
        "expected_revision": 1,
        "name": "v1",
    },
    ("POST", "/studio/projects/{project_id}/repair"): {},
    ("POST", "/studio/projects/{project_id}/set/approve"): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/subject/approve"): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/subject/generate"): {
        "expected_revision": 1,
        "reference_keys": ["sources/studio/other/subject-references/a.png"],
        "request_id": "req1",
    },
    (
        "POST",
        "/studio/projects/{project_id}/subject/operations/{job_id}/settle",
    ): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/subject/references/uploads"): {
        "expected_revision": 1,
        "content_type": "image/png",
    },
    (
        "POST",
        "/studio/projects/{project_id}/subject/revisions/{revision_id}/reframe",
    ): {"expected_revision": 1},
    (
        "POST",
        "/studio/projects/{project_id}/subject/revisions/{revision_id}/reject",
    ): {"expected_revision": 1},
    (
        "POST",
        "/studio/projects/{project_id}/subject/revisions/{revision_id}/restore",
    ): {"expected_revision": 1},
    (
        "POST",
        "/studio/projects/{project_id}/subject/revisions/{revision_id}/select",
    ): {"expected_revision": 1},
    ("POST", "/studio/projects/{project_id}/subject/settle"): {
        "expected_revision": 1,
        "asset_key": "sources/studio/other/subjects/a.png",
    },
    ("POST", "/studio/projects/{project_id}/subject/uploads"): {
        "expected_revision": 1,
        "content_type": "image/png",
    },
    ("GET", "/studio/exports/{export_id}"): None,
    ("GET", "/studio/exports/{export_id}/download"): None,
    ("GET", "/studio/exports/{export_id}/file"): None,
    # `path()` adds the owner's real subject key as `?key=`.
    ("GET", "/studio/projects/{project_id}/files"): None,
    ("POST", "/studio/releases/{release_id}/deliveries/{target}"): {},
    ("GET", "/studio/releases/{release_id}/exports"): None,
    ("POST", "/studio/releases/{release_id}/exports"): {
        "profile": "png-json",
        "expected_revision": 1,
    },
}


class _Fixture:
    def __init__(self) -> None:
        assets = MemoryAssetStore()
        jobs = MemoryJobStore()
        service = StudioService(
            store=MemoryStudioStore(),
            assets=assets,
            jobs=jobs,
            dispatcher=NullTaskDispatcher(),
            motion_generation_available=True,
            subject_generation_available=True,
            composition_generation_available=True,
        )
        self.assets = assets
        runtime = InMemoryRuntime(jobs, assets, studio_service=service)
        app = create_app(runtime, service_role=ServiceRole.API)
        self.owner = TestClient(app, headers={"X-Studio-Actor-Id": OWNER})
        self.stranger = TestClient(app, headers={"X-Studio-Actor-Id": STRANGER})

        self.project_id = self._create_project(self.owner, "Ember Knight")
        self.subject_key = self._settle_subject(self.project_id)
        self.stranger_project_id = self._create_project(self.stranger, "Ash Rider")
        # Release and export ids the stranger will try to reach. The service
        # refuses before either has to exist, so opaque ids are honest probes.
        self.release_id = "release-not-yours"
        self.export_id = "export-not-yours"

    def _create_project(self, client: TestClient, name: str) -> str:
        response = client.post("/studio/projects", json={"name": name})
        assert response.status_code == 200, response.text
        return str(response.json()["project_id"])

    def _settle_subject(self, project_id: str) -> str:
        upload = self.owner.post(
            f"/studio/projects/{project_id}/subject/uploads",
            json={"content_type": "image/png", "expected_revision": 1},
        )
        assert upload.status_code == 200, upload.text
        key = str(upload.json()["key"])
        self.assets.assets[key] = _png_bytes()
        settled = self.owner.post(
            f"/studio/projects/{project_id}/subject/settle",
            json={"expected_revision": 1, "asset_key": key},
        )
        assert settled.status_code == 200, settled.text
        return key

    def path(self, template: str) -> str:
        if template.endswith("/files"):
            return self._format(template) + f"?key={self.subject_key}"
        return self._format(template)

    def _format(self, template: str) -> str:
        return template.format(
            project_id=self.project_id,
            motion_id=_MOTION_ID,
            view_id="N",
            candidate_id="candidate-1",
            turnaround_id="turnaround-" + "0" * 32,
            draft_id="draft-" + "0" * 32,
            job_id="job-1",
            operation_id="job-1",
            revision_id="revision-1",
            release_id=self.release_id,
            export_id=self.export_id,
            target="example",
        )


@pytest.fixture(name="fixture", scope="module")
def _fixture() -> _Fixture:
    return _Fixture()


def _png_bytes() -> bytes:
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (624, 624), (255, 64, 64, 255)), (200, 200))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _studio_owner_scoped_paths() -> set[tuple[str, str]]:
    assets = MemoryAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    app = create_app(runtime, service_role=ServiceRole.API)
    scoped: set[tuple[str, str]] = set()
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        if not path.startswith("/studio/"):
            continue
        if not any(
            token in path
            for token in ("{project_id}", "{release_id}", "{export_id}")
        ):
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            scoped.add((method, path))
    return scoped


def test_every_owner_scoped_route_is_covered() -> None:
    """Adding a route that carries another actor's id must break here."""
    live = _studio_owner_scoped_paths()
    covered = set(OWNER_SCOPED_ROUTES)

    assert live - covered == set(), (
        "owner-scoped studio route(s) with no cross-account probe: "
        f"{sorted(live - covered)}. Add them to OWNER_SCOPED_ROUTES."
    )
    assert covered - live == set(), (
        f"OWNER_SCOPED_ROUTES names route(s) that no longer exist: "
        f"{sorted(covered - live)}"
    )


@pytest.mark.parametrize(
    ("method", "template"), sorted(OWNER_SCOPED_ROUTES), ids=lambda value: str(value)
)
def test_a_stranger_is_refused_on_every_owner_scoped_route(
    fixture: _Fixture, method: str, template: str
) -> None:
    body = OWNER_SCOPED_ROUTES[(method, template)]
    url = fixture.path(template)

    response = fixture.stranger.request(
        method, url, json=body if body is not None else None, headers={"X-Studio-Setup-Version": "1"}
    )

    assert response.status_code in REFUSAL_STATUSES, (
        f"{method} {template} answered {response.status_code} to a stranger: "
        f"{response.text}"
    )


@pytest.mark.parametrize(("method", "template"), [
    ("PUT", "/studio/projects/{project_id}/sprite-layout"),
    ("PUT", "/studio/projects/{project_id}/views/{view_id}"),
    ("POST", "/studio/projects/{project_id}/views/{view_id}/reference/uploads"),
])
@pytest.mark.parametrize("missing_project", [False, True], ids=["foreign-project", "nonexistent-project"])
def test_view_configuration_refuses_foreign_and_nonexistent_projects_before_mint(
    fixture: _Fixture, method: str, template: str, missing_project: bool
) -> None:
    url = fixture.path(template)
    if missing_project:
        url = url.replace(fixture.project_id, "nonexistent-project")
    before_mints = len(fixture.assets.minted_url_ttls)
    before = fixture.owner.get(f"/studio/projects/{fixture.project_id}").json()
    response = fixture.stranger.request(method, url, json=OWNER_SCOPED_ROUTES[(method, template)])
    assert response.status_code == 404, response.text
    assert len(fixture.assets.minted_url_ttls) == before_mints
    after = fixture.owner.get(f"/studio/projects/{fixture.project_id}").json()
    assert after["revision"] == before["revision"]
    assert after["manifest"] == before["manifest"]


def test_owner_can_configure_view_and_mint_only_its_project_upload() -> None:
    fixture = _Fixture()
    base = f"/studio/projects/{fixture.project_id}"
    revision = fixture.owner.get(base).json()["revision"]
    layout = fixture.owner.put(f"{base}/sprite-layout", json={
        "expected_revision": revision, "sprite_layout": "isometric",
    })
    assert layout.status_code == 200, layout.text
    view = fixture.owner.put(f"{base}/views/N", json={
        "expected_revision": layout.json()["revision"], "name": "North",
    })
    assert view.status_code == 200, view.text
    before_mints = len(fixture.assets.minted_url_ttls)
    upload = fixture.owner.post(f"{base}/views/N/reference/uploads", json={
        "expected_revision": view.json()["revision"], "content_type": "image/png",
    })
    assert upload.status_code == 200, upload.text
    assert upload.json()["key"].startswith(f"sources/studio/{fixture.project_id}/views/N/references/")
    assert len(fixture.assets.minted_url_ttls) == before_mints + 1
    assert fixture.owner.get(base).json()["revision"] == view.json()["revision"]


def test_the_owner_is_not_refused_on_the_same_routes(fixture: _Fixture) -> None:
    """Guards the guard: a fixture that refused everyone would pass vacuously."""
    response = fixture.owner.get(f"/studio/projects/{fixture.project_id}")

    assert response.status_code == 200


def test_the_project_list_never_shows_another_owners_project(
    fixture: _Fixture,
) -> None:
    listed = fixture.stranger.get("/studio/projects")

    assert listed.status_code == 200
    assert [row["project_id"] for row in listed.json()] == [
        fixture.stranger_project_id
    ]


def test_external_subject_lookup_is_scoped_to_the_calling_owner(
    fixture: _Fixture,
) -> None:
    response = fixture.stranger.get(
        "/studio/projects/for-external-subject",
        params={"system": "example", "entity_kind": "hero", "entity_id": "h1"},
    )

    assert response.status_code == 404


# --- caller-supplied asset keys ------------------------------------------------
# Owning the project in the path is not owning the key in the body.


def test_asset_preview_refuses_another_projects_key(fixture: _Fixture) -> None:
    response = fixture.stranger.post(
        f"/studio/projects/{fixture.stranger_project_id}/assets/preview",
        json={"asset_key": fixture.subject_key},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_preview_not_allowed"


def test_asset_download_refuses_another_projects_key(fixture: _Fixture) -> None:
    response = fixture.stranger.post(
        f"/studio/projects/{fixture.stranger_project_id}/assets/download",
        json={"asset_key": fixture.subject_key},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_preview_not_allowed"


def test_subject_settle_refuses_another_projects_source_key(
    fixture: _Fixture,
) -> None:
    response = fixture.stranger.post(
        f"/studio/projects/{fixture.stranger_project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": fixture.subject_key},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "subject_asset_key_invalid"


def test_candidate_settle_upload_refuses_another_projects_upload_key(
    fixture: _Fixture,
) -> None:
    stranger_motion = fixture.stranger.post(
        f"/studio/projects/{fixture.stranger_project_id}/motions",
        json={**_MOTION, "expected_revision": 1},
    )
    assert stranger_motion.status_code == 200, stranger_motion.text
    revision = stranger_motion.json()["revision"]

    response = fixture.stranger.post(
        f"/studio/projects/{fixture.stranger_project_id}/motions/{_MOTION_ID}"
        "/candidates/settle-upload",
        json={
            "expected_revision": revision,
            "asset_key": (
                f"sources/studio/{fixture.project_id}/motion-uploads/planted.png"
            ),
            "frame_count": 4,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "motion_upload_key_invalid"


def test_generic_asset_upload_route_refuses_the_studio_namespace() -> None:
    """The hole this closes: `/assets/uploads` proves no project ownership, so it
    must never mint a capability inside a studio project's source namespace."""
    assets = MemoryAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = TestClient(create_app(runtime, service_role=ServiceRole.API))

    response = client.post(
        "/assets/uploads",
        json={
            "key": "sources/studio/someone-elses-project/subjects/planted.png",
            "content_type": "image/png",
        },
    )

    assert response.status_code == 422
    assert assets.minted_url_ttls == []


@pytest.mark.parametrize("suffix", ["", "/cancel"])
async def test_pool_job_routes_refuse_a_stranger_a_studio_job(suffix: str) -> None:
    """A studio motion runs on the pool under its own studio job id, so `/pool/jobs`
    must answer for it the way `/jobs` does rather than as anonymous infrastructure."""
    assets = MemoryAssetStore()
    jobs = MemoryJobStore()
    service = StudioService(
        store=MemoryStudioStore(),
        assets=assets,
        jobs=jobs,
        dispatcher=NullTaskDispatcher(),
    )
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    app = create_app(runtime, service_role=ServiceRole.API)
    owner = TestClient(app, headers={"X-Studio-Actor-Id": OWNER})
    project_id = str(
        owner.post("/studio/projects", json={"name": "Ember"}).json()["project_id"]
    )
    submitted = await jobs.submit(
        JobSubmission(
            job_id="00000000-0000-0000-0000-0000000000aa",
            idempotency_key="k1",
            operation=JobOperation.PACKAGE,
            source_key=f"sources/studio/{project_id}/subjects/a.png",
            output_key=f"outputs/studio/{project_id}/motions/a.webm",
            requested_outputs={},
            project_id=project_id,
            manifest_operation_id="op-1",
        )
    )
    job_id = submitted.record.job_id

    stranger = TestClient(app, headers={"X-Studio-Actor-Id": STRANGER})
    method = "GET" if suffix == "" else "POST"

    response = stranger.request(method, f"/pool/jobs/{job_id}{suffix}")

    assert response.status_code == 404
    assert response.json() == {"detail": "studio project not found"}


def test_pool_job_keys_cannot_address_the_studio_namespace() -> None:
    assets = MemoryAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = TestClient(create_app(runtime, service_role=ServiceRole.API))

    response = client.post(
        "/pool/jobs",
        json={
            "idempotency_key": "k1",
            "capability_id": "deterministic-echo",
            "input_keys": ["sources/studio/someone-else/subjects/a.png"],
            "output_key": "outputs/studio/someone-else/subjects/b.png",
        },
    )

    assert response.status_code == 422


def test_a_file_link_works_only_for_the_person_it_was_given_to(fixture: _Fixture) -> None:
    """Links are API paths authorized on every request, never bearer URLs."""
    target = fixture.owner.post(
        f"/studio/projects/{fixture.project_id}/assets/preview",
        json={"asset_key": fixture.subject_key},
    ).json()
    link = target["upload_url"]

    assert link.startswith(f"/studio/projects/{fixture.project_id}/files?key=")
    owned = fixture.owner.get(link)
    assert owned.status_code == 200
    assert owned.content == fixture.assets.assets[fixture.subject_key]
    assert owned.headers["cache-control"].startswith("private")
    assert fixture.stranger.get(link).status_code == 404
    # A stranger cannot reach another project's files through their own project either.
    smuggled = link.replace(fixture.project_id, fixture.stranger_project_id, 1)
    assert fixture.stranger.get(smuggled).status_code == 404
