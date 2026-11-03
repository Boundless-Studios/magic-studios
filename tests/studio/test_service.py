from io import BytesIO
from inspect import signature

import pytest
from PIL import Image

from magic_video_editor.contracts import AssetStore, AssetTooLarge, StudioServicePort
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import MotionPreset
from magic_video_editor.studio.quality import SUBJECT_POLICY_V1, assess_subject_png
from magic_video_editor.studio.service import (
    MAX_SUBJECT_UPLOAD_BYTES,
    ProjectNotFound,
    StudioService,
    SubjectUploadError,
)


async def test_repair_project_restores_previous_revision_when_current_has_nulls() -> None:
    store = MemoryStudioStore()
    service = StudioService(store=store, assets=MemoryAssetStore())
    project = await service.create_project(
        "Ember Knight",
        "owner-1",
        preset_motions=(MotionPreset.IDLE, MotionPreset.WALK),
    )
    assert len(project.manifest.motions) == 2

    corrupt = project.manifest.model_dump(mode="json")
    corrupt["motions"] = [None, None]
    await store.append_raw(project.project_id, corrupt)
    assert await store.current_revision_number(project.project_id) == 2

    report = await service.repair_project(project.project_id, "owner-1")

    assert report.project_id == project.project_id
    assert report.current_revision_before == 2
    assert report.restored_from_revision == 1
    assert report.repaired is True
    assert report.new_revision == 3
    assert len(report.bad_revisions) == 1
    bad = report.bad_revisions[0]
    assert bad.revision == 2
    assert bad.null_paths == ("motions[0]", "motions[1]")
    assert bad.error_summary

    restored = await store.get(project.project_id)
    assert restored is not None
    assert restored.revision == 3
    assert len(restored.manifest.motions) == 2

    second_report = await service.repair_project(project.project_id, "owner-1")
    assert second_report.repaired is False
    assert second_report.restored_from_revision == 3
    assert second_report.new_revision is None
    assert second_report.bad_revisions == ()

    with pytest.raises(ProjectNotFound):
        await service.repair_project(project.project_id, "someone-else")


def test_service_contract_requires_trusted_actor_ids_for_owned_operations() -> None:
    create_project = signature(StudioServicePort.create_project)
    assert list(create_project.parameters) == [
        "self",
        "name",
        "actor_id",
        "external_subject",
        "preset_motions",
    ]

    get_project = signature(StudioServicePort.get_project)
    assert list(get_project.parameters) == ["self", "project_id", "actor_id"]

    create_upload = signature(StudioServicePort.create_subject_upload)
    assert list(create_upload.parameters) == [
        "self",
        "project_id",
        "content_type",
        "actor_id",
        "expected_revision",
    ]

    settle = signature(StudioServicePort.settle_subject_upload)
    assert list(settle.parameters) == [
        "self",
        "project_id",
        "expected_revision",
        "asset_key",
        "actor_id",
    ]

    approve = signature(StudioServicePort.approve_subject)
    assert list(approve.parameters) == ["self", "project_id", "expected_revision", "actor_id"]


async def test_service_rejects_non_png_upload_content_type_before_signing() -> None:
    assets = _RecordingAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    project = await service.create_project("Ember Knight", "owner-1")

    with pytest.raises(SubjectUploadError) as exc_info:
        await service.create_subject_upload(
            project.project_id,
            "image/jpeg",
            "owner-1",
        )

    assert exc_info.value.code == "subject_content_type_invalid"
    assert assets.created_uploads == []


async def test_service_assesses_subject_png_in_a_worker_thread(monkeypatch) -> None:
    assets = _BoundedReadAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    project = await service.create_project("Ember Knight", "owner-1")
    asset_key = f"sources/studio/{project.project_id}/subjects/uploaded.png"
    payload = _png_bytes()
    assets.assets[asset_key] = payload
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def record_to_thread(func, /, *args, **kwargs):
        assert kwargs == {}
        calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(
        "magic_video_editor.studio.service.asyncio.to_thread",
        record_to_thread,
    )

    stored = await service.settle_subject_upload(
        project.project_id,
        1,
        asset_key,
        "owner-1",
    )

    assert stored.revision == 2
    assert calls == [(assess_subject_png, (payload, SUBJECT_POLICY_V1))]
    assert assets.limited_reads == [(asset_key, MAX_SUBJECT_UPLOAD_BYTES)]


async def test_service_maps_bounded_asset_read_oversize_before_assessment(monkeypatch) -> None:
    assets = _OverLimitAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    project = await service.create_project("Ember Knight", "owner-1")
    to_thread_called = False

    async def fail_to_thread(*_args, **_kwargs):
        nonlocal to_thread_called
        to_thread_called = True
        raise AssertionError("assessment should not run for oversized payloads")

    monkeypatch.setattr(
        "magic_video_editor.studio.service.asyncio.to_thread",
        fail_to_thread,
    )

    with pytest.raises(SubjectUploadError) as exc_info:
        await service.settle_subject_upload(
            project.project_id,
            1,
            f"sources/studio/{project.project_id}/subjects/uploaded.png",
            "owner-1",
        )

    assert exc_info.value.code == "subject_png_bytes_too_large"
    assert to_thread_called is False


async def test_service_returns_project_not_found_for_missing_owned_projects() -> None:
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore())

    with pytest.raises(ProjectNotFound):
        await service.approve_subject("missing", 1, "owner-1")


class _RecordingAssetStore(MemoryAssetStore):
    def __init__(self) -> None:
        super().__init__()
        self.created_uploads: list[tuple[str, str]] = []

    async def create_upload_url(self, key: str, content_type: str) -> str:
        self.created_uploads.append((key, content_type))
        return await super().create_upload_url(key, content_type)


class _BoundedReadAssetStore(_RecordingAssetStore):
    def __init__(self) -> None:
        super().__init__()
        self.limited_reads: list[tuple[str, int]] = []

    async def read_limited(self, key: str, max_bytes: int) -> bytes:
        self.limited_reads.append((key, max_bytes))
        return await super().read(key)


class _OverLimitAssetStore:
    async def create_upload_url(self, key: str, content_type: str) -> str:
        raise AssertionError("upload url should not be requested")

    async def read(self, key: str) -> bytes:
        raise AssertionError("unbounded reads should not be used")

    async def read_limited(self, key: str, max_bytes: int) -> bytes:
        raise AssetTooLarge(key, max_bytes, max_bytes + 1)

    async def write_once(self, key: str, content: bytes, content_type: str) -> bool:
        raise AssertionError("write_once is not used in these tests")


def _png_bytes() -> bytes:
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    subject = Image.new("RGBA", (624, 624), (255, 64, 64, 255))
    image.paste(subject, (200, 200))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
