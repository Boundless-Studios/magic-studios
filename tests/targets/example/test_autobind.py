"""Part A: tag-at-animation-time deep link, exercised against the Example
Engine target -- the neutral machinery itself (registry validation, deep
link parsing) is adapter-agnostic; this file proves it end-to-end with the
reference adapter.

`POST /studio/projects` can create a project already bound to an external
subject and pre-populated with preset motions in one revision; `GET
/studio/projects/for-external-subject` looks up the owner's project already
bound to a subject so a repeated deep link opens it instead of duplicating it.
"""

import pytest
from studio.test_routes import _studio_client, _studio_runtime

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import ExternalSubject, MotionKind, MotionPreset
from magic_video_editor.studio.service import (
    ProjectAlreadyBoundToSubject,
    StudioService,
)

EMBER_KNIGHT = ExternalSubject(
    system="example",
    entity_kind="character",
    entity_id="char-42",
    display_name="Ember Knight",
)


async def test_create_project_binds_subject_and_adds_presets_in_one_revision() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())

    stored = await service.create_project(
        "Untitled",
        "owner-1",
        external_subject=EMBER_KNIGHT,
        preset_motions=(MotionPreset.IDLE, MotionPreset.WALK),
    )

    assert stored.revision == 1
    assert stored.manifest.external_subject == EMBER_KNIGHT
    motion_ids = {motion.motion_id for motion in stored.manifest.motions}
    assert motion_ids == {"idle", "walk"}
    idle = next(m for m in stored.manifest.motions if m.motion_id == "idle")
    assert idle.specification.kind is MotionKind.PRESET
    assert idle.specification.preset is MotionPreset.IDLE
    # Subject identity stays in metadata; generation prompts use a generic subject.
    assert (idle.specification.prompt or "").startswith("The character ")
    assert "Ember Knight" not in (idle.specification.prompt or "")
    assert "Untitled" not in (idle.specification.prompt or "")


async def test_create_project_without_preset_motions_or_subject_is_unaffected() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())

    stored = await service.create_project("Ember Knight", "owner-1")

    assert stored.manifest.external_subject is None
    assert stored.manifest.motions == ()


async def test_create_project_is_idempotent_on_the_external_subject_binding() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())
    first = await service.create_project(
        "Ember Knight", "owner-1", external_subject=EMBER_KNIGHT
    )

    with pytest.raises(ProjectAlreadyBoundToSubject) as exc_info:
        await service.create_project(
            "Ember Knight (duplicate)", "owner-1", external_subject=EMBER_KNIGHT
        )

    assert exc_info.value.project_id == first.project_id


async def test_create_project_binding_is_scoped_per_owner() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())
    await service.create_project(
        "Ember Knight", "owner-1", external_subject=EMBER_KNIGHT
    )

    # A different owner may bind the same subject id without conflict --
    # ownership scoping matches every other Studio route.
    stored = await service.create_project(
        "Ember Knight (other workspace)", "owner-2", external_subject=EMBER_KNIGHT
    )
    assert stored.manifest.external_subject == EMBER_KNIGHT


async def test_find_project_for_external_subject_round_trips() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())
    assert (
        await service.find_project_for_external_subject(
            "owner-1",
            system="example",
            entity_kind="character",
            entity_id="char-42",
        )
        is None
    )

    created = await service.create_project(
        "Ember Knight", "owner-1", external_subject=EMBER_KNIGHT
    )

    found = await service.find_project_for_external_subject(
        "owner-1",
        system="example",
        entity_kind="character",
        entity_id="char-42",
    )
    assert found is not None
    assert found.project_id == created.project_id


def test_route_creates_bound_project_and_is_idempotent_with_409() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")

    created = client.post(
        "/studio/projects",
        json={
            "name": "Ember Knight",
            "external_subject": {
                "system": "example",
                "entity_kind": "character",
                "entity_id": "char-42",
                "display_name": "Ember Knight",
            },
            "preset_motions": ["idle", "walk"],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["manifest"]["external_subject"] == {
        "system": "example",
        "entity_kind": "character",
        "entity_id": "char-42",
        "display_name": "Ember Knight",
    }
    assert {m["motion_id"] for m in body["manifest"]["motions"]} == {"idle", "walk"}

    duplicate = client.post(
        "/studio/projects",
        json={
            "name": "Ember Knight (dup)",
            "external_subject": {
                "system": "example",
                "entity_kind": "character",
                "entity_id": "char-42",
                "display_name": "Ember Knight",
            },
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["project_id"] == body["project_id"]


def test_route_looks_up_project_for_external_subject() -> None:
    runtime = _studio_runtime()
    client = _studio_client(runtime, actor_id="owner-1")

    missing = client.get(
        "/studio/projects/for-external-subject",
        params={
            "system": "example",
            "entity_kind": "character",
            "entity_id": "char-42",
        },
    )
    assert missing.status_code == 404

    created = client.post(
        "/studio/projects",
        json={
            "name": "Ember Knight",
            "external_subject": {
                "system": "example",
                "entity_kind": "character",
                "entity_id": "char-42",
                "display_name": "Ember Knight",
            },
        },
    ).json()

    found = client.get(
        "/studio/projects/for-external-subject",
        params={
            "system": "example",
            "entity_kind": "character",
            "entity_id": "char-42",
        },
    )
    assert found.status_code == 200
    assert found.json()["project_id"] == created["project_id"]

    other_owner = _studio_client(runtime, actor_id="owner-2")
    invisible = other_owner.get(
        "/studio/projects/for-external-subject",
        params={
            "system": "example",
            "entity_kind": "character",
            "entity_id": "char-42",
        },
    )
    assert invisible.status_code == 404
