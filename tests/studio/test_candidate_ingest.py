"""T21: ingest an existing clip or sheet as a candidate."""

import subprocess
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import CandidateSource, MotionPreset, MotionSpecification, SubjectRevision
from magic_video_editor.studio.service import MotionUploadValidationError, StudioService


def _sheet_strip(frame_count: int, frame_width: int = 32) -> bytes:
    """A plain PNG strip with no embedded studio_frame_count -- as if it came from
    another tool (Ludo, AutoSprite, an artist's export)."""
    sheet = Image.new("RGBA", (frame_width * frame_count, 32), (0, 0, 0, 0))
    for index in range(frame_count):
        sheet.paste((10, 20, 30, 255), (index * frame_width + 4, 4, index * frame_width + 28, 28))
    output = BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


def _clip() -> bytes:
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=4:d=1",
            "-frames:v", "4", "-pix_fmt", "yuv420p", "-f", "mp4", "-movflags",
            "frag_keyframe+empty_moov", "pipe:1",
        ],
        check=True, capture_output=True, timeout=15,
    ).stdout


async def _service_with_motion():
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
    project = await store.create(manifest)
    service = StudioService(store=store, assets=assets)
    return service, assets, project


async def test_create_upload_returns_a_signed_put_in_the_motion_uploads_namespace() -> None:
    service, _, project = await _service_with_motion()

    target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )

    assert target.key.startswith("sources/studio/project-1/motion-uploads/")
    assert target.key.endswith(".png")


async def test_create_upload_rejects_an_unsupported_content_type() -> None:
    service, _, project = await _service_with_motion()

    try:
        await service.create_candidate_upload(
            "project-1", "idle", "image/jpeg", "user-1", project.revision
        )
    except MotionUploadValidationError as exc:
        assert exc.code == "motion_upload_content_type_invalid"
    else:
        raise AssertionError("expected MotionUploadValidationError")


async def test_settling_a_png_strip_with_frame_count_appends_an_upload_candidate() -> None:
    service, assets, project = await _service_with_motion()
    target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )
    assets.assets[target.key] = _sheet_strip(8)

    settled = await service.settle_candidate_upload(
        "project-1", project.revision, "idle", target.key, "user-1", frame_count=8
    )

    candidate = settled.manifest.motions[0].candidates[0]
    assert candidate.source is CandidateSource.UPLOAD
    assert candidate.metadata.frame_count == 8
    assert candidate.artifact_key == target.key


async def test_a_bad_frame_count_is_rejected_without_losing_the_upload() -> None:
    service, assets, project = await _service_with_motion()
    target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )
    assets.assets[target.key] = _sheet_strip(8, frame_width=32)  # width 256, not divisible by 5

    try:
        await service.settle_candidate_upload(
            "project-1", project.revision, "idle", target.key, "user-1", frame_count=5
        )
    except MotionUploadValidationError as exc:
        assert exc.code == "motion_upload_invalid"
    else:
        raise AssertionError("expected MotionUploadValidationError")

    # The upload itself is preserved -- only the settle failed.
    assert target.key in assets.assets


async def test_a_png_strip_without_frame_count_is_rejected() -> None:
    service, assets, project = await _service_with_motion()
    target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )
    assets.assets[target.key] = _sheet_strip(4)

    try:
        await service.settle_candidate_upload(
            "project-1", project.revision, "idle", target.key, "user-1", frame_count=None
        )
    except MotionUploadValidationError as exc:
        assert exc.code == "motion_upload_frame_count_required"
    else:
        raise AssertionError("expected MotionUploadValidationError")


async def test_settling_a_video_upload_needs_no_frame_count() -> None:
    service, assets, project = await _service_with_motion()
    target = await service.create_candidate_upload(
        "project-1", "idle", "video/mp4", "user-1", project.revision
    )
    assets.assets[target.key] = _clip()

    settled = await service.settle_candidate_upload(
        "project-1", project.revision, "idle", target.key, "user-1"
    )

    candidate = settled.manifest.motions[0].candidates[0]
    assert candidate.source is CandidateSource.UPLOAD
    assert candidate.metadata.frame_count == 4


async def test_settling_the_same_upload_twice_is_idempotent() -> None:
    service, assets, project = await _service_with_motion()
    target = await service.create_candidate_upload(
        "project-1", "idle", "image/png", "user-1", project.revision
    )
    assets.assets[target.key] = _sheet_strip(4)

    first = await service.settle_candidate_upload(
        "project-1", project.revision, "idle", target.key, "user-1", frame_count=4
    )
    second = await service.settle_candidate_upload(
        "project-1", first.revision, "idle", target.key, "user-1", frame_count=4
    )

    assert first.revision == second.revision
    assert len(second.manifest.motions[0].candidates) == 1


async def test_settle_rejects_an_asset_key_outside_the_project_namespace() -> None:
    service, assets, project = await _service_with_motion()
    assets.assets["sources/studio/other-project/motion-uploads/x.png"] = _sheet_strip(4)

    try:
        await service.settle_candidate_upload(
            "project-1", project.revision, "idle",
            "sources/studio/other-project/motion-uploads/x.png", "user-1", frame_count=4,
        )
    except MotionUploadValidationError as exc:
        assert exc.code == "motion_upload_key_invalid"
    else:
        raise AssertionError("expected MotionUploadValidationError")


def test_upload_and_settle_routes_round_trip() -> None:
    import asyncio

    service, assets, project = asyncio.run(_service_with_motion())
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = TestClient(create_app(runtime))

    upload_response = client.post(
        "/studio/projects/project-1/motions/idle/candidates/uploads",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={"content_type": "image/png", "expected_revision": project.revision},
    )
    assert upload_response.status_code == 200
    asset_key = upload_response.json()["key"]
    assets.assets[asset_key] = _sheet_strip(4)

    settle_response = client.post(
        "/studio/projects/project-1/motions/idle/candidates/settle-upload",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={"expected_revision": project.revision, "asset_key": asset_key, "frame_count": 4},
    )
    assert settle_response.status_code == 200
    candidates = settle_response.json()["manifest"]["motions"][0]["candidates"]
    assert candidates[0]["source"] == "upload"
    assert candidates[0]["metadata"]["frame_count"] == 4
