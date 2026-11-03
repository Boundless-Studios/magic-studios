from datetime import datetime
from inspect import Parameter, signature

import pytest

from magic_video_editor.studio.manifest import create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import ExportRecord, Release
from magic_video_editor.studio.store import (
    DuplicateReleaseName,
    ManifestMismatch,
    ProjectAlreadyExists,
    ProjectRecord,
    RevisionConflict,
    StoredManifest,
    StudioStore,
)


def test_store_contract_exposes_versioned_manifest_operations() -> None:
    assert set(StoredManifest.model_fields) == {
        "project_id",
        "revision",
        "manifest_digest",
        "manifest",
        "updated_at",
    }
    assert StoredManifest.model_fields["revision"].annotation is int
    assert StoredManifest.model_fields["updated_at"].annotation is datetime

    create = signature(StudioStore.create)
    assert list(create.parameters) == ["self", "manifest"]
    assert create.return_annotation == StoredManifest

    get = signature(StudioStore.get)
    assert list(get.parameters) == ["self", "project_id"]
    assert get.parameters["project_id"].annotation is str
    assert get.return_annotation == StoredManifest | None

    append = signature(StudioStore.append)
    assert list(append.parameters) == [
        "self",
        "project_id",
        "expected_revision",
        "manifest",
    ]
    assert append.parameters["expected_revision"].annotation is int
    assert append.return_annotation == StoredManifest

    create_release = signature(StudioStore.create_release)
    assert list(create_release.parameters) == [
        "self",
        "project_id",
        "expected_revision",
        "name",
        "manifest",
    ]
    assert (
        create_release.parameters["project_id"].kind
        is Parameter.KEYWORD_ONLY
    )
    assert (
        create_release.parameters["manifest"].kind
        is Parameter.KEYWORD_ONLY
    )
    assert create_release.return_annotation is Release

    get_release = signature(StudioStore.get_release)
    assert list(get_release.parameters) == ["self", "release_id"]
    assert get_release.parameters["release_id"].annotation is str
    assert get_release.return_annotation == Release | None


async def test_append_rejects_a_stale_expected_revision() -> None:
    store = MemoryStudioStore()
    original = create_project_manifest("project-1", "Original")
    created = await store.create(original)
    changed = original.model_copy(update={"name": "Changed"})

    with pytest.raises(RevisionConflict) as exc_info:
        await store.append(
            "project-1",
            expected_revision=created.revision - 1,
            manifest=changed,
        )

    assert exc_info.value.current_revision == created.revision
    assert (await store.get("project-1")) == created


async def test_list_revisions_returns_every_durable_revision_oldest_first() -> None:
    store = MemoryStudioStore()
    original = create_project_manifest("project-1", "Original")
    created = await store.create(original)
    changed = original.model_copy(update={"name": "Changed"})
    appended = await store.append("project-1", expected_revision=created.revision, manifest=changed)

    revisions = await store.list_revisions("project-1")

    assert [item.revision for item in revisions] == [created.revision, appended.revision]
    assert revisions[-1].manifest.name == "Changed"
    assert await store.list_revisions("missing-project") == ()


async def test_append_rejects_missing_project_before_creating_revision() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.append("project-1", expected_revision=0, manifest=manifest)

    assert exc_info.value.current_revision == 0
    assert await store.get("project-1") is None


async def test_create_rejects_duplicate_project_with_current_revision() -> None:
    store = MemoryStudioStore()
    original = create_project_manifest("project-1", "Original")
    created = await store.create(original)

    with pytest.raises(ProjectAlreadyExists) as exc_info:
        await store.create(original.model_copy(update={"name": "Duplicate"}))

    assert exc_info.value.current_revision == created.revision
    assert await store.get("project-1") == created


async def test_append_rejects_manifest_project_id_mismatch_before_write() -> None:
    store = MemoryStudioStore()
    original = create_project_manifest("project-1", "Original")
    created = await store.create(original)
    wrong_project = create_project_manifest("project-2", "Wrong")

    with pytest.raises(ManifestMismatch):
        await store.append(
            "project-1",
            expected_revision=created.revision,
            manifest=wrong_project,
        )

    assert await store.get("project-1") == created


async def test_memory_store_copies_manifests_on_write_and_read() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")

    created = await store.create(manifest)
    object.__setattr__(manifest, "name", "mutated input")
    object.__setattr__(created.manifest, "name", "mutated return")

    stored = await store.get("project-1")

    assert stored is not None
    assert stored.manifest.name == "Original"
    assert stored.manifest is not created.manifest

    changed = create_project_manifest("project-1", "Changed")
    appended = await store.append(
        "project-1",
        expected_revision=stored.revision,
        manifest=changed,
    )
    object.__setattr__(changed, "name", "mutated append input")
    object.__setattr__(appended.manifest, "name", "mutated append return")

    stored = await store.get("project-1")

    assert stored is not None
    assert stored.manifest.name == "Changed"
    assert stored.manifest is not appended.manifest


async def test_release_snapshot_is_immutable_and_retrievable() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)

    release = await store.create_release(
        project_id="project-1",
        expected_revision=created.revision,
        name="v1",
        manifest=manifest,
    )
    object.__setattr__(release.manifest, "name", "tampered")

    retrieved = await store.get_release(release.release_id)

    assert retrieved is not None
    assert retrieved.name == "v1"
    assert retrieved.manifest.name == "Original"
    assert retrieved.manifest_digest == created.manifest_digest
    assert retrieved is not release
    assert retrieved.manifest is not release.manifest


async def test_create_release_rejects_stale_revision_without_storing_snapshot() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)
    changed = manifest.model_copy(update={"name": "Changed"})
    current = await store.append(
        "project-1",
        expected_revision=created.revision,
        manifest=changed,
    )

    with pytest.raises(RevisionConflict) as exc_info:
        await store.create_release(
            project_id="project-1",
            expected_revision=created.revision,
            name="v1",
            manifest=changed,
        )

    assert exc_info.value.current_revision == current.revision
    assert store._releases == {}


async def test_create_release_rejects_manifest_project_id_mismatch() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)
    wrong_project = create_project_manifest("project-2", "Wrong")

    with pytest.raises(ManifestMismatch):
        await store.create_release(
            project_id="project-1",
            expected_revision=created.revision,
            name="v1",
            manifest=wrong_project,
        )

    assert store._releases == {}


async def test_create_release_rejects_manifest_digest_mismatch() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)
    caller_snapshot = manifest.model_copy(update={"name": "Caller bytes"})

    with pytest.raises(ManifestMismatch):
        await store.create_release(
            project_id="project-1",
            expected_revision=created.revision,
            name="v1",
            manifest=caller_snapshot,
        )

    assert store._releases == {}


async def test_create_release_rejects_duplicate_name() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)
    first = await store.create_release(
        project_id="project-1",
        expected_revision=created.revision,
        name="v1",
        manifest=manifest,
    )

    with pytest.raises(DuplicateReleaseName):
        await store.create_release(
            project_id="project-1",
            expected_revision=created.revision,
            name="v1",
            manifest=manifest,
        )

    assert await store.get_release(first.release_id) == first


async def test_create_release_rejects_missing_project() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.create_release(
            project_id="project-1",
            expected_revision=0,
            name="v1",
            manifest=manifest,
        )

    assert exc_info.value.current_revision == 0


async def test_create_records_created_by_from_the_manifest_owner() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original", owner_id="owner-1")

    await store.create(manifest)

    records = await store.list_projects("owner-1")
    assert len(records) == 1
    assert isinstance(records[0], ProjectRecord)
    assert records[0].created_by == "owner-1"
    assert records[0].created_at is not None


async def test_create_leaves_created_by_null_for_an_ownerless_manifest() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")

    await store.create(manifest)
    stored = await store.get("project-1")
    assert stored is not None
    assert stored.manifest.owner_id is None


async def test_list_projects_returns_only_the_owners_projects_newest_first() -> None:
    store = MemoryStudioStore()
    mine_first = await store.create(
        create_project_manifest("project-1", "Mine 1", owner_id="owner-1")
    )
    await store.create(
        create_project_manifest("project-2", "Not mine", owner_id="owner-2")
    )
    await store.create(
        create_project_manifest("project-3", "Mine 2", owner_id="owner-1")
    )
    # Bump project-1's updated_at above project-3's by appending a revision.
    await store.append(
        "project-1",
        mine_first.revision,
        mine_first.manifest.model_copy(update={"name": "Mine 1 renamed"}),
    )

    records = await store.list_projects("owner-1")

    assert [record.stored.project_id for record in records] == [
        "project-1",
        "project-3",
    ]
    assert all(record.created_by == "owner-1" for record in records)


async def test_release_and_export_records_default_a_creation_timestamp_when_omitted() -> None:
    """Old rows/callers that never set a timestamp still load: both models
    stamp `created_at` from a default rather than requiring the caller to
    supply one."""
    manifest = create_project_manifest("project-1", "Original")
    release = Release(
        release_id="r1", name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    export = ExportRecord(
        export_id="e1", release_id="r1", profile_name="png-json",
        profile_version="2", artifact_key="k", sha256="b" * 64, size_bytes=1,
    )

    assert isinstance(release.created_at, datetime)
    assert isinstance(export.created_at, datetime)
    assert export.completed_at is None


async def test_list_release_exports_returns_newest_first_for_this_release_only() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Original")
    created = await store.create(manifest)
    release = await store.create_release(
        project_id="project-1", expected_revision=created.revision,
        name="v1", manifest=manifest,
    )
    other_release = await store.create_release(
        project_id="project-1", expected_revision=created.revision,
        name="v2", manifest=manifest,
    )
    await store.save_export(ExportRecord(
        export_id="e-other", release_id=other_release.release_id,
        profile_name="png-json", profile_version="2",
        artifact_key="k-other", sha256="c" * 64, size_bytes=1,
    ))
    first = await store.save_export(ExportRecord(
        export_id="e1", release_id=release.release_id,
        profile_name="png-json", profile_version="2",
        artifact_key="k1", sha256="a" * 64, size_bytes=1,
    ))
    second = await store.save_export(ExportRecord(
        export_id="e2", release_id=release.release_id,
        profile_name="gaia", profile_version="1",
        artifact_key="k2", sha256="b" * 64, size_bytes=1,
    ))

    exports = await store.list_release_exports(release.release_id)

    assert [item.export_id for item in exports] == [second.export_id, first.export_id]


async def test_list_raw_revisions_and_current_revision_number_survive_a_corrupt_row() -> None:
    store = MemoryStudioStore()
    original = create_project_manifest("project-1", "Original")
    created = await store.create(original)

    assert await store.current_revision_number("project-1") == created.revision
    assert await store.current_revision_number("missing-project") is None

    raw = await store.append_raw("project-1", {"project_id": "project-1", "motions": None})

    assert raw.revision == created.revision + 1
    assert await store.current_revision_number("project-1") == raw.revision

    revisions = await store.list_raw_revisions("project-1")

    assert [item.revision for item in revisions] == [raw.revision, created.revision]
    assert revisions[0].manifest == {"project_id": "project-1", "motions": None}
    assert revisions[1].manifest["name"] == "Original"
    assert await store.list_raw_revisions("missing-project") == ()


async def test_list_projects_surfaces_a_corrupt_current_revision_as_unreadable() -> None:
    store = MemoryStudioStore()
    readable = create_project_manifest("project-1", "Readable", owner_id="owner-1")
    await store.create(readable)
    corrupt = create_project_manifest("project-2", "Corrupt", owner_id="owner-1")
    await store.create(corrupt)

    raw = await store.append_raw(
        "project-2", {"project_id": "project-2", "name": "Corrupt raw name", "motions": None}
    )

    records = {record.project_id: record for record in await store.list_projects("owner-1")}

    assert set(records) == {"project-1", "project-2"}
    readable_record = records["project-1"]
    assert readable_record.unreadable is False
    assert readable_record.stored is not None
    assert readable_record.stored.project_id == "project-1"

    unreadable_record = records["project-2"]
    assert unreadable_record.unreadable is True
    assert unreadable_record.stored is None
    assert unreadable_record.raw_name == "Corrupt raw name"
    assert unreadable_record.revision == raw.revision
    assert unreadable_record.created_by == "owner-1"
