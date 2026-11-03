"""T20: per-motion downloads and preview clips."""

from hashlib import sha256
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    FrameSlot,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionPreset,
    MotionSpecification,
    SubjectRevision,
)
from magic_video_editor.studio.quality import assess_motion
from magic_video_editor.studio.service import StudioService


def _sheet(frame_count: int) -> bytes:
    sheet = Image.new("RGBA", (32 * frame_count, 32), (0, 0, 0, 0))
    for index in range(frame_count):
        sheet.paste((index * 30 % 255, 60, 200, 255), (index * 32 + 4, 4, index * 32 + 28, 28))
    output = BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


async def _service_with_candidate(ordered_frames=None, facing=None):
    store = MemoryStudioStore()
    assets = MemoryAssetStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=1024, height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id="user-1", policy_version="subject-v1",
    )
    manifest = manifest.with_motion(
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
    )
    artifact = _sheet(4)
    artifact_key = "outputs/studio/project-1/motions/idle/source-a.png"
    assets.assets[artifact_key] = artifact
    metadata = MotionFrameMetadata(
        decodable=True, media_kind=MediaKind.SHEET, frame_count=4, width=128, height=32,
        constant_geometry=True, has_alpha=True,
        packing_estimate=ExportPackingEstimate(width=128, height=32),
    )
    kwargs = {"frame_duration_ms": 100, "loop_policy": LoopPolicy.LOOP}
    if ordered_frames is not None:
        kwargs["ordered_frames"] = ordered_frames
    if facing is not None:
        kwargs["facing"] = facing
    candidate = MotionCandidate(
        candidate_id="source-a", artifact_key=artifact_key, sha256=sha256(artifact).hexdigest(),
        duration_ms=400, metadata=metadata, **kwargs,
    )
    candidate = candidate.model_copy(
        update={"assessment": assess_motion(candidate, LoopPolicy.LOOP)}
    )
    motions = (
        manifest.motions[0].model_copy(
            update={"candidates": (candidate,), "selected_candidate_id": "source-a"}
        ),
    )
    manifest = manifest.model_copy(update={"motions": motions})
    project = await store.create(manifest)
    service = StudioService(store=store, assets=assets)
    return service, assets, project


async def test_candidate_sheet_download_is_a_packed_png_and_caches_byte_identical() -> None:
    service, assets, _ = await _service_with_candidate()

    first = await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")
    second = await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")

    assert first.key == second.key
    assert first.key.endswith("/sheet.png")
    assert assets.assets[first.key].startswith(b"\x89PNG\r\n\x1a\n")


async def test_candidate_sheet_download_honors_reorder_and_flip() -> None:
    from magic_video_editor.studio.models import Facing

    service, assets, _ = await _service_with_candidate(
        ordered_frames=(FrameSlot(index=3), FrameSlot(index=0)), facing=Facing.LEFT,
    )

    target = await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")

    sheet = Image.open(BytesIO(assets.assets[target.key]))
    # Reordered to 2 frames -> half the original strip width.
    assert sheet.size == (64, 32)


async def test_candidate_preview_webm_is_cached_and_byte_identical_on_repeat() -> None:
    service, assets, _ = await _service_with_candidate()

    first = await service.download_candidate_preview(
        "project-1", "idle", "source-a", "webm", "user-1"
    )
    second = await service.download_candidate_preview(
        "project-1", "idle", "source-a", "webm", "user-1"
    )

    assert first.key == second.key
    assert first.key.endswith("/preview.webm")
    content = assets.assets[first.key]
    assert content.startswith(b"\x1a\x45\xdf\xa3")


async def test_candidate_preview_gif_is_a_real_gif() -> None:
    service, assets, _ = await _service_with_candidate()

    target = await service.download_candidate_preview(
        "project-1", "idle", "source-a", "gif", "user-1"
    )

    assert assets.assets[target.key].startswith(b"GIF8")
    with Image.open(BytesIO(assets.assets[target.key])) as gif:
        assert gif.convert('RGBA').getchannel('A').getextrema() == (0, 255)
        assert gif.n_frames == 4


def test_candidate_download_routes_return_signed_urls() -> None:
    import asyncio

    service, assets, project = asyncio.run(_service_with_candidate())
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = TestClient(create_app(runtime))

    sheet_response = client.get(
        "/studio/projects/project-1/motions/idle/candidates/source-a/sheet.png",
        headers={"X-Studio-Actor-Id": "user-1"},
    )
    assert sheet_response.status_code == 200
    assert sheet_response.json()["key"].endswith("/sheet.png")

    webm_response = client.get(
        "/studio/projects/project-1/motions/idle/candidates/source-a/preview.webm",
        headers={"X-Studio-Actor-Id": "user-1"},
    )
    assert webm_response.status_code == 200
    assert webm_response.json()["key"].endswith("/preview.webm")

    gif_response = client.get(
        "/studio/projects/project-1/motions/idle/candidates/source-a/preview.gif",
        headers={"X-Studio-Actor-Id": "user-1"},
    )
    assert gif_response.status_code == 200
    assert gif_response.json()["key"].endswith("/preview.gif")


def test_candidate_download_links_stream_attachments_with_take_filenames() -> None:
    import asyncio

    service, assets, _ = asyncio.run(_service_with_candidate())
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = TestClient(create_app(runtime))
    headers = {"X-Studio-Actor-Id": "user-1"}
    base = "/studio/projects/project-1/motions/idle/candidates/source-a"

    for path, filename in (
        ("sheet.png", "ember-idle-take-1.png"),
        ("preview.webm", "ember-idle-take-1.webm"),
        ("preview.gif", "ember-idle-take-1.gif"),
    ):
        response = client.get(f"{base}/{path}", headers=headers)
        assert response.status_code == 200
        link = response.json()["upload_url"]
        assert link.startswith("/studio/projects/project-1/files?key=")
        file = client.get(link, headers=headers)
        assert file.status_code == 200
        assert file.headers["content-disposition"].startswith(f'attachment; filename="{filename}"')
        # The link is the owner's alone.
        assert client.get(link, headers={"X-Studio-Actor-Id": "someone-else"}).status_code == 404


async def test_download_builds_a_missing_export_before_linking_to_it() -> None:
    service, assets, _ = await _service_with_candidate()

    for format in ("png", "gif", "webm"):
        if format == "png":
            target = await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")
        else:
            target = await service.download_candidate_preview("project-1", "idle", "source-a", format, "user-1")
        assert target.key in assets.assets, "A link must never point at an unrendered export"


def test_download_take_numbers_match_grouped_alpha_variants():
    from types import SimpleNamespace
    from magic_video_editor.studio.service import _candidate_take_number
    original = SimpleNamespace(candidate_id='original', recipe=None, duration_ms=400, metadata=SimpleNamespace(has_alpha=False))
    keyed = SimpleNamespace(candidate_id='alpha', recipe=SimpleNamespace(op='derive_alpha'), duration_ms=400, metadata=SimpleNamespace(has_alpha=True))
    packed = SimpleNamespace(candidate_id='packed', recipe=SimpleNamespace(op='resample'), duration_ms=400, metadata=SimpleNamespace(has_alpha=True))
    motion = SimpleNamespace(candidates=[original, keyed, packed])
    assert [_candidate_take_number(motion, candidate.candidate_id) for candidate in motion.candidates] == [1, 1, 2]


async def _service_with_alternate_walks():
    from magic_video_editor.studio.models import DerivationRecipe, NormalizationSpec
    from magic_video_editor.studio.quality import inspect_sprite_png

    service, assets, project = await _service_with_candidate()
    walks = []
    for count in (8, 12, 16):
        content = _sheet(count)
        key = f"outputs/studio/project-1/walk-{count}.png"
        assets.assets[key] = content
        walks.append(MotionCandidate(
            candidate_id=f"walk-{count}", artifact_key=key,
            sha256=sha256(content).hexdigest(), duration_ms=count * 100,
            frame_duration_ms=100, metadata=inspect_sprite_png(content),
        ))
    attack = project.manifest.motions[0].selected_candidate.model_copy(update={
        "recipe": DerivationRecipe(op="normalize", normalization=NormalizationSpec(
            scale=1, source_anchor_x=16, source_anchor_y=28,
            target_anchor_x=16, target_anchor_y=28, padding=0,
            reference_candidate_id=walks[2].candidate_id, reference_sha256=walks[2].sha256,
        )),
    })
    attack_motion = project.manifest.motions[0].model_copy(update={"candidates": (attack,)})
    walk_motion = attack_motion.model_copy(update={
        "specification": MotionSpecification(motion_id="walk", name="Walk", preset=MotionPreset.WALK),
        "candidates": tuple(walks), "selected_candidate_id": "walk-12",
    })
    project = await service._store.append(project.project_id, project.revision,
        project.manifest.model_copy(update={"motions": (attack_motion, walk_motion)}))
    return service, assets, project


async def test_alternate_walk_downloads_ignore_unrelated_stale_selected_calibration():
    from magic_video_editor.studio.exports import PNG_JSON_V1, ReleaseExporter
    from magic_video_editor.studio.models import Release

    service, assets, project = await _service_with_alternate_walks()
    original = project.manifest.model_dump()
    for candidate in project.manifest.motions[1].candidates:
        source = assets.assets[candidate.artifact_key]
        target = await service.download_candidate_sheet("project-1", "walk", candidate.candidate_id, "user-1")
        with Image.open(BytesIO(assets.assets[target.key])) as exported, Image.open(BytesIO(source)) as before:
            assert exported.size == before.size
            assert exported.tobytes() == before.tobytes()
            if candidate.candidate_id == "walk-16":
                assert exported.info["studio_pivot_x"] == "16"
                assert exported.info["studio_pivot_y"] == "28"
        for fmt, signature in (("gif", b"GIF8"), ("webm", b"\x1a\x45\xdf\xa3")):
            preview = await service.download_candidate_preview("project-1", "walk", candidate.candidate_id, fmt, "user-1")
            assert assets.assets[preview.key].startswith(signature)
        assert assets.assets[candidate.artifact_key] == source
    assert (await service.get_project("project-1", "user-1")).manifest.model_dump() == original
    with pytest.raises(ValueError, match="exact selected"):
        await ReleaseExporter(assets).export(Release(
            release_id="release-stale", name="Stale", manifest_digest="a" * 64,
            manifest=project.manifest, release_digest="b" * 64,
        ), profile=PNG_JSON_V1)


@pytest.mark.parametrize("tamper", ["hash", "missing"])
async def test_candidate_download_validates_own_immutable_reference_even_when_unselected(tamper):

    service, assets, project = await _service_with_alternate_walks()
    # Neither the attack nor its exact immutable reference needs to be selected.
    attack_motion = project.manifest.motions[0].model_copy(update={"selected_candidate_id": None})
    project = await service._store.append(project.project_id, project.revision,
        project.manifest.model_copy(update={"motions": (attack_motion, project.manifest.motions[1])}))
    await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")
    await service.download_candidate_preview("project-1", "idle", "source-a", "gif", "user-1")
    walks = project.manifest.motions[1]
    changed = walks.model_copy(update={"candidates": tuple(
        candidate.model_copy(update={"sha256": "f" * 64}) if candidate.candidate_id == "walk-16" else candidate
        for candidate in walks.candidates
        if tamper != "missing" or candidate.candidate_id != "walk-16")})
    await service._store.append(project.project_id, project.revision,
        project.manifest.model_copy(update={"motions": (project.manifest.motions[0], changed)}))
    for fmt in ("png", "gif", "webm"):
        with pytest.raises(ValueError, match="exact.*candidate"):
            if fmt == "png":
                await service.download_candidate_sheet("project-1", "idle", "source-a", "user-1")
            else:
                await service.download_candidate_preview("project-1", "idle", "source-a", fmt, "user-1")


async def test_candidate_download_rejects_only_relevant_pivot_conflicts():
    service, assets, project = await _service_with_alternate_walks()
    attack = project.manifest.motions[0]
    selected = attack.selected_candidate
    conflicting = selected.model_copy(update={"recipe": selected.recipe.model_copy(update={
        "normalization": selected.recipe.normalization.model_copy(update={"target_anchor_x": 15}),
    })})
    other = attack.model_copy(update={
        "specification": MotionSpecification(motion_id="other", name="Other", preset=MotionPreset.ATTACK),
        "candidates": (conflicting.model_copy(update={"candidate_id": "other-attack"}),),
        "selected_candidate_id": "other-attack",
    })
    await service._store.append(project.project_id, project.revision,
        project.manifest.model_copy(update={"motions": (*project.manifest.motions, other)}))
    # Conflicting old-walk registrations do not affect a new independent walk.
    await service.download_candidate_sheet("project-1", "walk", "walk-12", "user-1")
    for fmt in ("png", "gif", "webm"):
        with pytest.raises(ValueError, match="disagree on the reference pivot"):
            if fmt == "png":
                await service.download_candidate_sheet("project-1", "walk", "walk-16", "user-1")
            else:
                await service.download_candidate_preview("project-1", "walk", "walk-16", fmt, "user-1")
