"""Rejecting and restoring subject takes:
`POST /subject/revisions/{revision_id}/reject` and `.../restore`.
"""

from magic_video_editor.studio.manifest import approve_subject
from magic_video_editor.studio.models import ApprovalState, RevisionLifecycle
from magic_video_editor.studio.service import SubjectRevisionNotFound
from studio.test_routes import _png_bytes, _studio_client, _studio_runtime
from studio.test_subject_revisions import _project_with_two_revisions


def _upload_and_settle(
    client, runtime, project_id: str, expected_revision: int, box: tuple[int, int, int, int]
) -> dict:
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


def _project_with_two_takes() -> tuple:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")
    project_id = client.post("/studio/projects", json={"name": "Ember Knight"}).json()[
        "project_id"
    ]
    first = _upload_and_settle(client, runtime, project_id, 1, (200, 200, 824, 824))
    second = _upload_and_settle(client, runtime, project_id, 2, (100, 100, 700, 700))
    return client, project_id, first, second


async def test_reject_current_falls_back_to_previous_take_and_resets_approval() -> None:
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

    settled = await service.reject_subject_revision(
        project_id, approved.revision, "rev-current", "user-1"
    )

    subject = settled.manifest.subject
    assert subject.current_revision_id == "rev-older"
    assert subject.assessment is None
    assert subject.approval.state is not ApprovalState.APPROVED
    assert subject.approval.artifact_sha256 is None
    rejected = next(rev for rev in subject.revisions if rev.revision_id == "rev-current")
    assert rejected.lifecycle is RevisionLifecycle.REJECTED
    # Nothing is deleted: both takes remain on record.
    assert {rev.revision_id for rev in subject.revisions} == {"rev-older", "rev-current"}


async def test_reject_every_take_leaves_no_current_revision() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    first = await service.reject_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )
    second = await service.reject_subject_revision(
        project_id, first.revision, "rev-current", "user-1"
    )

    assert second.manifest.subject.current_revision is None


async def test_reject_non_current_take_leaves_the_selection_alone() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    settled = await service.reject_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )

    assert settled.manifest.subject.current_revision_id == "rev-current"
    rejected = next(
        rev
        for rev in settled.manifest.subject.revisions
        if rev.revision_id == "rev-older"
    )
    assert rejected.lifecycle is RevisionLifecycle.REJECTED


async def test_selecting_a_rejected_take_is_refused() -> None:
    service, project_id, revision = await _project_with_two_revisions()
    rejected = await service.reject_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )

    try:
        await service.select_subject_revision(
            project_id, rejected.revision, "rev-older", "user-1"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a rejected revision")


async def test_rejecting_twice_is_refused() -> None:
    service, project_id, revision = await _project_with_two_revisions()
    rejected = await service.reject_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )

    try:
        await service.reject_subject_revision(
            project_id, rejected.revision, "rev-older", "user-1"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an already-rejected revision")


async def test_reject_unknown_revision_raises_not_found() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    try:
        await service.reject_subject_revision(
            project_id, revision, "does-not-exist", "user-1"
        )
    except SubjectRevisionNotFound:
        pass
    else:
        raise AssertionError("expected SubjectRevisionNotFound")


async def test_restore_makes_a_rejected_take_selectable_again() -> None:
    service, project_id, revision = await _project_with_two_revisions()
    rejected = await service.reject_subject_revision(
        project_id, revision, "rev-older", "user-1"
    )

    restored = await service.restore_subject_revision(
        project_id, rejected.revision, "rev-older", "user-1"
    )

    record = next(
        rev
        for rev in restored.manifest.subject.revisions
        if rev.revision_id == "rev-older"
    )
    assert record.lifecycle is RevisionLifecycle.READY
    # Restoring never changes the selection on its own.
    assert restored.manifest.subject.current_revision_id == "rev-current"

    selected = await service.select_subject_revision(
        project_id, restored.revision, "rev-older", "user-1"
    )
    assert selected.manifest.subject.current_revision_id == "rev-older"


async def test_restoring_a_take_that_is_not_rejected_is_refused() -> None:
    service, project_id, revision = await _project_with_two_revisions()

    try:
        await service.restore_subject_revision(
            project_id, revision, "rev-older", "user-1"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a take that is not rejected")


def test_reject_and_restore_routes_round_trip() -> None:
    client, project_id, first, second = _project_with_two_takes()
    first_id = first["manifest"]["subject"]["current_revision"]["revision_id"]
    second_id = second["manifest"]["subject"]["current_revision"]["revision_id"]

    rejected = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{second_id}/reject",
        json={"expected_revision": second["revision"]},
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["manifest"]["subject"]["current_revision"]["revision_id"] == first_id
    lifecycles = {
        rev["revision_id"]: rev["lifecycle"]
        for rev in body["manifest"]["subject"]["revisions"]
    }
    assert lifecycles[second_id] == "rejected"

    restored = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{second_id}/restore",
        json={"expected_revision": body["revision"]},
    )
    assert restored.status_code == 200
    restored_body = restored.json()
    assert restored_body["manifest"]["subject"]["current_revision"]["revision_id"] == (
        first_id
    )
    assert all(
        rev["lifecycle"] == "ready"
        for rev in restored_body["manifest"]["subject"]["revisions"]
    )

    selected = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{second_id}/select",
        json={"expected_revision": restored_body["revision"]},
    )
    assert selected.status_code == 200


def test_reject_route_conflict_and_not_found() -> None:
    client, project_id, first, second = _project_with_two_takes()
    first_id = first["manifest"]["subject"]["current_revision"]["revision_id"]

    conflict = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{first_id}/reject",
        json={"expected_revision": second["revision"] + 1},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == second["revision"]

    missing = client.post(
        f"/studio/projects/{project_id}/subject/revisions/nope/restore",
        json={"expected_revision": second["revision"]},
    )
    assert missing.status_code == 404


def test_select_route_returns_422_for_a_rejected_revision() -> None:
    client, project_id, first, second = _project_with_two_takes()
    first_id = first["manifest"]["subject"]["current_revision"]["revision_id"]

    rejected = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{first_id}/reject",
        json={"expected_revision": second["revision"]},
    ).json()

    response = client.post(
        f"/studio/projects/{project_id}/subject/revisions/{first_id}/select",
        json={"expected_revision": rejected["revision"]},
    )

    assert response.status_code == 422
