"""Subject takes gallery and choosing a take (studio-ux-flow phase 3):
`manifest.subject.revisions[]` in the project response, and
`POST /subject/revisions/{revision_id}/select`.
"""

from hashlib import sha256

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.manifest import (
    approve_subject,
    create_project_manifest,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ApprovalState,
    SubjectRevision,
    SubjectRevisionSource,
)
from magic_video_editor.studio.service import (
    ProjectNotFound,
    StudioService,
    SubjectRevisionNotFound,
)
from magic_video_editor.studio.store import RevisionConflict
from studio.test_routes import _png_bytes, _studio_client, _studio_runtime


def test_response_lists_subject_revisions_newest_first_with_new_fields() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]

    def _upload_and_settle(expected_revision: int, box: tuple[int, int, int, int]) -> None:
        upload = client.post(
            f"/studio/projects/{project_id}/subject/uploads",
            json={"content_type": "image/png", "expected_revision": expected_revision},
        )
        key = upload.json()["key"]
        runtime.assets_store.assets[key] = _png_bytes(
            size=(1024, 1024), subject_box=box, mode="RGBA"
        )
        settled = client.post(
            f"/studio/projects/{project_id}/subject/settle",
            json={"expected_revision": expected_revision, "asset_key": key},
        )
        assert settled.status_code == 200

    _upload_and_settle(1, (200, 200, 824, 824))
    _upload_and_settle(2, (100, 100, 700, 700))

    fetched = client.get(f"/studio/projects/{project_id}").json()
    revisions = fetched["manifest"]["subject"]["revisions"]

    assert len(revisions) == 2
    # Newest first: the second upload is the current revision and leads.
    assert revisions[0]["revision_id"] == fetched["manifest"]["subject"][
        "current_revision"
    ]["revision_id"]
    assert revisions[0]["source"] == "uploaded"
    assert revisions[0]["direction"] is None
    assert revisions[0]["created_at"] is not None
    assert revisions[0]["assessment"] is not None
    # The superseded revision keeps its identity but carries no assessment --
    # the manifest only ever stores one (the current revision's).
    assert revisions[1]["assessment"] is None
    assert revisions[1]["revision_id"] != revisions[0]["revision_id"]


def test_old_manifest_without_new_optional_fields_still_loads_and_serializes() -> None:
    """A `SubjectRevision` built the way pre-phase-3 stored manifests were --
    with no `source`, `direction`, or `created_at` in the payload -- still
    validates, defaulting `source` to `generated` and the rest to `None`."""
    revision = SubjectRevision.model_validate(
        {
            "revision_id": "old-rev",
            "artifact_key": "outputs/studio/project-1/subjects/old.png",
            "sha256": "a" * 64,
            "width": 512,
            "height": 512,
        }
    )
    assert revision.source is SubjectRevisionSource.GENERATED
    assert revision.direction is None
    assert revision.created_at is None


_OLDER_PNG = _png_bytes(size=(512, 512), subject_box=(64, 64, 448, 448), mode="RGBA")
_OLDER_SHA = sha256(_OLDER_PNG).hexdigest()
_CURRENT_PNG = _png_bytes(size=(512, 512), subject_box=(32, 32, 480, 480), mode="RGBA")
_CURRENT_SHA = sha256(_CURRENT_PNG).hexdigest()


async def _project_with_two_revisions(
    *, current_sha: str | None = None, seed_assets: bool = True
) -> tuple[StudioService, str, int]:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    older = SubjectRevision(
        revision_id="rev-older",
        artifact_key="outputs/studio/project-1/subjects/older.png",
        sha256=_OLDER_SHA,
        width=512,
        height=512,
    )
    current = SubjectRevision(
        revision_id="rev-current",
        artifact_key="outputs/studio/project-1/subjects/current.png",
        sha256=current_sha if current_sha is not None else _CURRENT_SHA,
        width=512,
        height=512,
    )
    manifest = manifest.with_subject(older).with_subject(current)
    created = await store.create(manifest)
    assets = MemoryAssetStore()
    if seed_assets:
        assets.assets[older.artifact_key] = _OLDER_PNG
        assets.assets[current.artifact_key] = _CURRENT_PNG
    service = StudioService(store=store, assets=assets)
    return service, "project-1", created.revision


async def test_select_resets_approval_when_switching_to_a_different_take() -> None:
    service, project_id, _revision = await _project_with_two_revisions()
    store = service._store
    stored = await store.get(project_id)
    assert stored is not None
    approved_manifest = approve_subject(
        stored.manifest,
        revision_id="rev-current",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-policy-v1",
    )
    approved = await store.append(project_id, stored.revision, approved_manifest)
    assert approved.manifest.subject.approval.state is ApprovalState.APPROVED

    settled = await service.select_subject_revision(
        project_id, approved.revision, "rev-older", "user-1"
    )

    # rev-older's sha does not match the approved sha, so approval resets.
    assert settled.manifest.subject.current_revision_id == "rev-older"
    assert settled.manifest.subject.approval.state is not ApprovalState.APPROVED
    assert settled.manifest.subject.approval.artifact_sha256 != approved.manifest.subject.approval.artifact_sha256


async def test_select_keeps_approval_when_sha_matches_the_reselected_revision() -> None:
    shared_sha = "c" * 64
    service, project_id, _ = await _project_with_two_revisions(current_sha=shared_sha)
    store = service._store
    stored = await store.get(project_id)
    assert stored is not None
    # Approve the current revision, then reassign its sha to a revision made
    # from the exact same bytes -- the case this exercises is "choosing a take
    # whose image already carries an approved assessment".
    manifest = stored.manifest.model_copy(
        update={
            "subject": stored.manifest.subject.model_copy(
                update={
                    "revisions": tuple(
                        rev.model_copy(update={"sha256": shared_sha})
                        for rev in stored.manifest.subject.revisions
                    )
                }
            )
        }
    )
    approved_manifest = approve_subject(
        manifest,
        revision_id="rev-current",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-policy-v1",
    )
    approved = await store.append(project_id, stored.revision, approved_manifest)
    assert approved.manifest.subject.approval.artifact_sha256 == shared_sha

    settled = await service.select_subject_revision(
        project_id, approved.revision, "rev-older", "user-1"
    )

    assert settled.manifest.subject.current_revision_id == "rev-older"
    assert settled.manifest.subject.approval.state is ApprovalState.APPROVED
    assert settled.manifest.subject.approval.artifact_sha256 == shared_sha


async def test_select_recomputes_assessment_for_a_revision_never_assessed() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    settled = await service.select_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )

    assert settled.manifest.subject.current_revision_id == "rev-older"
    assert settled.manifest.subject.assessment is not None
    assert settled.manifest.subject.assessment.artifact_sha256 == _OLDER_SHA
    assert (
        settled.manifest.subject.assessment.artifact_sha256
        == settled.manifest.subject.current_revision.sha256
    )


async def test_select_unknown_revision_raises_not_found() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    try:
        await service.select_subject_revision(
            project_id, revision, "does-not-exist", "user-1"
        )
    except SubjectRevisionNotFound:
        pass
    else:
        raise AssertionError("expected SubjectRevisionNotFound")


async def test_select_stale_expected_revision_raises_conflict() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    try:
        await service.select_subject_revision(
            project_id, revision + 1, "rev-older", "user-1"
        )
    except RevisionConflict as exc:
        assert exc.current_revision == revision
    else:
        raise AssertionError("expected RevisionConflict")


async def test_select_missing_project_raises_project_not_found() -> None:
    service, _, _ = await _project_with_two_revisions()

    try:
        await service.select_subject_revision(
            "missing-project", 1, "rev-older", "user-1"
        )
    except ProjectNotFound:
        pass
    else:
        raise AssertionError("expected ProjectNotFound")


def test_select_route_returns_404_for_unknown_revision() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]
    upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    )
    key = upload.json()["key"]
    runtime.assets_store.assets[key] = _png_bytes(
        size=(512, 512), subject_box=(64, 64, 448, 448), mode="RGBA"
    )
    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": key},
    ).json()

    response = client.post(
        f"/studio/projects/{project_id}/subject/revisions/does-not-exist/select",
        json={"expected_revision": settled["revision"]},
    )

    assert response.status_code == 404


def test_select_route_returns_409_for_stale_expected_revision() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]
    upload = client.post(
        f"/studio/projects/{project_id}/subject/uploads",
        json={"content_type": "image/png", "expected_revision": 1},
    )
    key = upload.json()["key"]
    runtime.assets_store.assets[key] = _png_bytes(
        size=(512, 512), subject_box=(64, 64, 448, 448), mode="RGBA"
    )
    settled = client.post(
        f"/studio/projects/{project_id}/subject/settle",
        json={"expected_revision": 1, "asset_key": key},
    ).json()
    revision_id = settled["manifest"]["subject"]["current_revision"]["revision_id"]

    response = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{revision_id}/select",
        json={"expected_revision": settled["revision"] + 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["current_revision"] == settled["revision"]


def test_select_route_happy_path_switches_the_current_revision() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]

    def _upload_and_settle(expected_revision: int, box: tuple[int, int, int, int]) -> dict:
        upload = client.post(
            f"/studio/projects/{project_id}/subject/uploads",
            json={"content_type": "image/png", "expected_revision": expected_revision},
        )
        key = upload.json()["key"]
        runtime.assets_store.assets[key] = _png_bytes(
            size=(1024, 1024), subject_box=box, mode="RGBA"
        )
        return client.post(
            f"/studio/projects/{project_id}/subject/settle",
            json={"expected_revision": expected_revision, "asset_key": key},
        ).json()

    first = _upload_and_settle(1, (200, 200, 824, 824))
    first_revision_id = first["manifest"]["subject"]["current_revision"]["revision_id"]
    second = _upload_and_settle(2, (100, 100, 700, 700))
    assert second["manifest"]["subject"]["current_revision"]["revision_id"] != (
        first_revision_id
    )

    response = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{first_revision_id}/select",
        json={"expected_revision": second["revision"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manifest"]["subject"]["current_revision"]["revision_id"] == (
        first_revision_id
    )
    # Both revisions are still listed; selecting an older take never drops one.
    assert {
        rev["revision_id"] for rev in body["manifest"]["subject"]["revisions"]
    } == {first_revision_id, second["manifest"]["subject"]["current_revision"][
        "revision_id"
    ]}
