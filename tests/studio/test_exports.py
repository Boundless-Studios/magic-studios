import io
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from magic_video_editor.contracts import AssetStorageUnavailable
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.exports import (
    PNG_JSON_V1,
    ExportStorageUnavailable,
    ImmutableExportCollision,
    ReleaseAssetInvalid,
    ReleaseExporter,
    chromakey_sheet,
)
from magic_video_editor.studio.manifest import approve_set, set_digest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    Facing,
    FrameSlot,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionPreset,
    MotionSpecification,
    QualityAssessment,
)
from magic_video_editor.studio.service import (
    ExportNotFound,
    ReleaseNotFound,
    StudioService,
)

from .test_releases import PNG_SHEET, releasable_manifest


def _two_frame_mp4() -> bytes:
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=4x4:r=2:d=1", "-frames:v", "2",
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    ).stdout


async def test_export_converts_generated_video_to_a_real_png_sprite_sheet() -> None:
    video = _two_frame_mp4()
    manifest = releasable_manifest()
    motion = manifest.motions[0]
    candidate = MotionCandidate(
        candidate_id="video-1",
        artifact_key="outputs/studio/project-1/motions/idle/video-1.mp4",
        sha256=sha256(video).hexdigest(),
        duration_ms=1000,
        metadata=MotionFrameMetadata(
            decodable=True, media_kind=MediaKind.VIDEO, frame_count=2, width=4, height=4,
            constant_geometry=True, has_alpha=False,
            packing_estimate=ExportPackingEstimate(width=8, height=4),
        ),
        assessment=motion.candidates[0].assessment,
    )
    manifest = manifest.model_copy(update={
        "motions": (motion.model_copy(update={
            "candidates": (candidate,), "selected_candidate_id": candidate.candidate_id,
        }),),
    })
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "video-set", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    manifest = approve_set(
        manifest, assessment_id="video-set", approver_id="user-1",
        policy_version="set-policy-v1",
    )
    assets = MemoryAssetStore(assets={candidate.artifact_key: video})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "video", "user-1"
    )

    exported = await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)

    with zipfile.ZipFile(io.BytesIO(assets.assets[exported.artifact_key])) as archive:
        sheet = archive.read("motions/idle.png")
        package = json.loads(archive.read("manifest.json"))
    assert sheet.startswith(b"\x89PNG\r\n\x1a\n")
    assert package["motions"][0]["frame_geometry"] == {
        "frame_count": 2, "frame_width": 4, "frame_height": 4,
        "sheet_width": 8, "sheet_height": 4, "baseline_y": None,
        "pivot_x": None, "pivot_y": None,
    }


async def test_same_release_and_profile_emits_identical_archive() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    exporter = ReleaseExporter(assets)

    first = await exporter.export(release, profile=PNG_JSON_V1)
    first_bytes = assets.assets[first.artifact_key]
    second = await exporter.export(release, profile=PNG_JSON_V1)

    assert first == second
    assert assets.assets[second.artifact_key] == first_bytes
    assert assets.publish_count == 1
    assert first.export_id == second.export_id
    assert first.sha256 == second.sha256

    with zipfile.ZipFile(io.BytesIO(first_bytes)) as archive:
        assert archive.namelist() == ["manifest.json", "motions/idle.png"]
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["subject"]["revision_id"] == "subject-1"
    assert manifest["motions"][0]["ordered_frames"] == [0, 1, 2, 3]
    assert manifest["motions"][0]["duration_ms"] == 800
    assert manifest["motions"][0]["loop"] == "loop"
    assert manifest["motions"][0]["artifact_sha256"] == sha256(PNG_SHEET).hexdigest()
    assert manifest["quality"]["set"]["state"] == "approved"


async def test_reexporting_a_pre_existing_v1_export_does_not_collide() -> None:
    """T15: png-json v1 bytes changed shape after this deployment (holds,
    facing, packed frame counts), so re-requesting an export for a release
    already exported under v1 must not try to regenerate v1 bytes at the
    same immutable key -- the service now defaults "png-json" to v2, whose
    export_id differs, so the pre-existing v1 object is left untouched and
    the new v2 export succeeds independently."""
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=assets)
    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    # Simulate an export published before this deployment, directly against
    # the frozen v1 profile (bypassing the service's name-based default),
    # and durably recorded the same way the service records a real export.
    pre_existing_v1 = await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)
    await store.save_export(pre_existing_v1)

    new_export = await service.create_export(release.release_id, "png-json", "user-1")

    assert new_export.export_id != pre_existing_v1.export_id
    assert new_export.profile_version == "2"
    assert pre_existing_v1.profile_version == "1"
    # The pre-existing v1 object is untouched and still verifies.
    replayed = await service.get_export(pre_existing_v1.export_id, "user-1")
    assert replayed == pre_existing_v1


async def test_export_uses_saved_frame_revision_order_timing_and_loop() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest(
        ordered_frames=(2, 0, 1),
        frame_duration_ms=90,
        candidate_loop_policy="once",
    ))
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "edited", "user-1"
    )

    exported = await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)

    with zipfile.ZipFile(io.BytesIO(assets.assets[exported.artifact_key])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["motions"][0]["ordered_frames"] == [2, 0, 1]
    assert manifest["motions"][0]["duration_ms"] == 270
    assert manifest["motions"][0]["loop"] == "once"


async def test_export_mirrors_a_flipped_candidates_sheet() -> None:
    from PIL import Image, ImageOps

    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    unflipped_created = await store.create(releasable_manifest())
    unflipped_release = await StudioService(store=store, assets=assets).create_release(
        "project-1", unflipped_created.revision, "unflipped", "user-1"
    )
    unflipped_exported = await ReleaseExporter(assets).export(unflipped_release, profile=PNG_JSON_V1)
    with zipfile.ZipFile(io.BytesIO(assets.assets[unflipped_exported.artifact_key])) as archive:
        unflipped_sheet = archive.read("motions/idle.png")

    flipped_store = MemoryStudioStore()
    flipped_created = await flipped_store.create(releasable_manifest(facing=Facing.LEFT))
    flipped_release = await StudioService(store=flipped_store, assets=assets).create_release(
        "project-1", flipped_created.revision, "flipped", "user-1"
    )
    flipped_exported = await ReleaseExporter(assets).export(flipped_release, profile=PNG_JSON_V1)
    with zipfile.ZipFile(io.BytesIO(assets.assets[flipped_exported.artifact_key])) as archive:
        flipped_sheet = archive.read("motions/idle.png")
        package = json.loads(archive.read("manifest.json"))

    # Each tile is mirrored in place: frame order is preserved, only the pose faces left.
    frame_width = package["motions"][0]["frame_geometry"]["frame_width"]
    with Image.open(io.BytesIO(unflipped_sheet)) as unflipped_image, Image.open(
        io.BytesIO(flipped_sheet)
    ) as flipped_image:
        unflipped = unflipped_image.convert("RGBA")
        flipped = flipped_image.convert("RGBA")
        assert flipped.size == unflipped.size
        for left in range(0, unflipped.width, frame_width):
            box = (left, 0, left + frame_width, unflipped.height)
            expected_tile = ImageOps.mirror(unflipped.crop(box))
            assert list(flipped.crop(box).getdata()) == list(expected_tile.getdata())
    assert package["motions"][0]["facing"] == "left"


async def test_export_reports_packed_frame_count_when_holds_expand_the_sheet() -> None:
    """T5: frame_geometry.frame_count must equal the number of cells actually
    packed into the exported strip, not the source's measured frame count,
    or a consumer slicing the strip by frame_count reads garbage."""
    from PIL import Image

    manifest = releasable_manifest()
    motion = manifest.motions[0]
    candidate = motion.candidates[0].model_copy(update={
        "ordered_frames": (FrameSlot(index=0, hold=2), FrameSlot(index=1, hold=3)),
    })
    manifest = manifest.model_copy(update={
        "motions": (motion.model_copy(update={"candidates": (candidate,)}),),
    })
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "held-set", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    manifest = approve_set(
        manifest, assessment_id="held-set", approver_id="user-1",
        policy_version="set-policy-v1",
    )
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "held", "user-1"
    )

    exported = await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)

    with zipfile.ZipFile(io.BytesIO(assets.assets[exported.artifact_key])) as archive:
        sheet = archive.read("motions/idle.png")
        package = json.loads(archive.read("manifest.json"))
    geometry = package["motions"][0]["frame_geometry"]
    with Image.open(io.BytesIO(sheet)) as opened:
        packed_cell_count = opened.width // geometry["frame_width"]
    assert geometry["frame_count"] == 5
    assert geometry["frame_count"] == packed_cell_count
    assert geometry["sheet_width"] == geometry["frame_width"] * 5


def test_chromakey_sheet_preserves_the_source_studio_frame_count() -> None:
    """T12: a derived (keyed-out) sheet must keep the source's declared
    studio_frame_count PNG text chunk, or inspect_sprite_png measures the
    re-saved static PNG as a single frame and every derived strip collapses
    to a one-frame candidate."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    from magic_video_editor.studio.quality import inspect_sprite_png

    frame_count = 4
    frame_size = 32
    sheet = Image.new("RGB", (frame_size * frame_count, frame_size), (0x00, 0xB1, 0x40))
    png_info = PngInfo()
    png_info.add_text("studio_frame_count", str(frame_count))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG", pnginfo=png_info)
    source_bytes = buffer.getvalue()

    derived_bytes = chromakey_sheet(source_bytes)

    metadata = inspect_sprite_png(derived_bytes)
    assert metadata.frame_count == frame_count


async def test_distinct_releases_of_same_manifest_have_isolated_exports() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=assets)
    first_release = await service.create_release(
        "project-1", created.revision, "candidate", "user-1"
    )
    second_release = await service.create_release(
        "project-1", created.revision, "production", "user-1"
    )

    first = await service.create_export(first_release.release_id, "png-json", "user-1")
    second = await service.create_export(
        second_release.release_id, "png-json", "user-1"
    )
    first_again = await service.create_export(
        first_release.release_id, "png-json", "user-1"
    )

    assert first_release.release_digest != second_release.release_digest
    assert first.export_id == sha256(
        f"{first_release.release_digest}png-json2".encode()
    ).hexdigest()
    assert first.export_id != second.export_id
    assert first.release_id == first_release.release_id
    assert second.release_id == second_release.release_id
    assert first_again == first
    assert assets.assets[first.artifact_key] == assets.assets[first_again.artifact_key]
    with pytest.raises(ExportNotFound, match="^export not found$"):
        await service.get_export(first.export_id, "other-user")


async def test_export_is_stamped_with_creation_and_completion_timestamps() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=assets)
    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    before = datetime.now(UTC)
    export = await service.create_export(release.release_id, "png-json", "user-1")
    after = datetime.now(UTC)

    assert isinstance(export.created_at, datetime)
    assert export.completed_at is not None
    assert before <= export.created_at <= after
    assert export.completed_at == export.created_at


async def test_list_release_exports_returns_newest_first_and_is_owner_scoped() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=assets)
    release = await service.create_release("project-1", created.revision, "v1", "user-1")

    first = await service.create_export(release.release_id, "example", "user-1")
    second = await service.create_export(release.release_id, "png-json", "user-1")

    exports = await service.list_release_exports(release.release_id, "user-1")

    assert [item.export_id for item in exports] == [second.export_id, first.export_id]

    with pytest.raises(ReleaseNotFound):
        await service.list_release_exports(release.release_id, "other-user")


async def test_export_rejects_tampered_or_oversized_sheet() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": b"wrong"})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    exporter = ReleaseExporter(assets, max_sheet_bytes=4)
    with pytest.raises(ValueError, match="limit"):
        await exporter.export(release, profile=PNG_JSON_V1)


async def test_export_metadata_survives_service_recreation() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    first_service = StudioService(store=store, assets=assets)
    release = await first_service.create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    exported = await first_service.create_export(release.release_id, "png-json", "user-1")

    restarted_service = StudioService(store=store, assets=assets)
    assert await restarted_service.get_export(exported.export_id, "user-1") == exported


async def test_export_rejects_sheet_bytes_that_do_not_match_release_digest() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": b"tampered"})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )

    with pytest.raises(ValueError, match="signed release"):
        await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)


async def test_export_skips_an_unselected_optional_motion() -> None:
    manifest = releasable_manifest().with_motion(
        MotionSpecification(
            motion_id="portrait", name="Portrait", preset=MotionPreset.IDLE,
            required=False,
        )
    )
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "optional-set", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    manifest = approve_set(
        manifest, assessment_id="optional-set", approver_id="user-1",
        policy_version="set-policy-v1",
    )
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )

    exported = await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)
    with zipfile.ZipFile(io.BytesIO(assets.assets[exported.artifact_key])) as archive:
        package = json.loads(archive.read("manifest.json"))
        assert archive.namelist() == ["manifest.json", "motions/idle.png"]
    assert [motion["motion_id"] for motion in package["motions"]] == ["idle"]


async def test_export_maps_missing_selected_asset_and_corrupt_collision() -> None:
    assets = MemoryAssetStore()
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    exporter = ReleaseExporter(assets)
    with pytest.raises(ReleaseAssetInvalid) as missing:
        await exporter.export(release, profile=PNG_JSON_V1)
    assert missing.value.code == "release_asset_missing"

    assets.assets["outputs/idle.png"] = PNG_SHEET
    export_id = sha256(f"{release.release_digest}png-json1".encode()).hexdigest()
    assets.assets[f"exports/studio/project-1/{export_id}.zip"] = b"corrupt"
    with pytest.raises(ImmutableExportCollision):
        await exporter.export(release, profile=PNG_JSON_V1)


async def test_cached_export_rechecks_published_object() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    exporter = ReleaseExporter(assets)
    record = await exporter.export(release, profile=PNG_JSON_V1)
    del assets.assets[record.artifact_key]

    with pytest.raises(ImmutableExportCollision):
        await exporter.export(release, profile=PNG_JSON_V1)


async def test_export_maps_transient_write_failure() -> None:
    class _UnavailableAssets(MemoryAssetStore):
        async def write_once(self, key: str, content: bytes, content_type: str) -> bool:
            raise AssetStorageUnavailable("provider unavailable")

    assets = _UnavailableAssets(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "v1", "user-1"
    )
    with pytest.raises(ExportStorageUnavailable):
        await ReleaseExporter(assets).export(release, profile=PNG_JSON_V1)


async def test_export_download_url_is_an_attachment_named_for_the_release() -> None:
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    service = StudioService(store=store, assets=assets)
    release = await service.create_release(
        "project-1", created.revision, "First Playable", "user-1"
    )
    record = await service.create_export(release.release_id, "png-json", "user-1")

    target = await service.create_export_download(record.export_id, "user-1")

    # A link the export route authorizes on every request, never a bearer URL.
    assert target.upload_url == f"/studio/exports/{record.export_id}/file"
    assert await service.export_file(record.export_id, "user-1") == (
        record.artifact_key, "ember-first-playable-png-json.zip"
    )
