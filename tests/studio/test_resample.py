"""T19: resample and cell size on a motion revision."""

import subprocess
from hashlib import sha256
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    CandidateSource,
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


async def test_streamed_webm_keeps_alpha_when_resampled_and_exported():
    from magic_video_editor.plugins.ffmpeg import chromakey_video_to_alpha_webm, extract_video_frames
    from magic_video_editor.studio.exports import _render_video_sprite_sheet
    source = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
         'color=c=0x00B140:s=32x32:r=4:d=1,drawbox=x=8:y=8:w=16:h=16:c=red:t=fill',
         '-frames:v', '4', '-pix_fmt', 'yuv420p', '-f', 'mp4',
         '-movflags', 'frag_keyframe+empty_moov', 'pipe:1'],
        check=True, capture_output=True, timeout=30,
    ).stdout
    webm = await chromakey_video_to_alpha_webm(source)
    frames = await extract_video_frames(webm, 4)
    assert len(frames) == 4
    for data in [*frames, await _render_video_sprite_sheet(webm, 4)]:
        with Image.open(BytesIO(data)) as im:
            assert im.mode == 'RGBA'
            assert im.getchannel('A').getextrema() == (0, 255)


def _video(frame_count: int, duration_s: float = 2.0) -> bytes:
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc=s=64x64:r={frame_count / duration_s}:d={duration_s}",
            "-frames:v", str(frame_count), "-pix_fmt", "yuva420p", "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov", "pipe:1",
        ],
        check=True, capture_output=True, timeout=30,
    ).stdout


def _sheet(frame_count: int) -> bytes:
    sheet = Image.new("RGBA", (64 * frame_count, 64), (0, 0, 0, 0))
    for index in range(frame_count):
        sheet.paste((index * 10 % 255, 40, 90, 255), (index * 64 + 10, 10, index * 64 + 54, 54))
    output = BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


async def _service_with_candidate(artifact: bytes, *, media_kind: MediaKind, frame_count: int):
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
    extension = "png" if media_kind is MediaKind.SHEET else "mp4"
    artifact_key = f"outputs/studio/project-1/motions/idle/source-a.{extension}"
    assets.assets[artifact_key] = artifact
    metadata = MotionFrameMetadata(
        decodable=True, media_kind=media_kind, frame_count=frame_count,
        width=64 * frame_count if media_kind is MediaKind.SHEET else 64,
        height=64, constant_geometry=True, has_alpha=True,
        packing_estimate=ExportPackingEstimate(
            width=64 * frame_count, height=64
        ),
    )
    candidate = MotionCandidate(
        candidate_id="source-a", artifact_key=artifact_key, sha256=sha256(artifact).hexdigest(),
        duration_ms=2000, metadata=metadata,
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
    return service, project


async def test_resampling_a_60_frame_video_to_16_frames_512_cell_yields_constant_geometry() -> None:
    service, project = await _service_with_candidate(
        _video(60), media_kind=MediaKind.VIDEO, frame_count=60
    )

    revised = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-a", "user-1",
        ordered_frames=tuple(FrameSlot(index=index) for index in range(60)),
        frame_duration_ms=100, loop_policy=LoopPolicy.LOOP,
        target_frame_count=16, cell_size=512,
    )

    candidate = revised.manifest.motions[0].selected_candidate
    assert candidate is not None
    assert candidate.metadata.frame_count == 16
    assert candidate.metadata.width == 512 * 16
    assert candidate.metadata.height == 512
    assert candidate.metadata.constant_geometry is True
    assert candidate.duration_ms == 1600
    assert candidate.source is CandidateSource.DERIVED
    assert candidate.recipe.op == "resample"
    assert candidate.recipe.target_frame_count == 16
    assert candidate.recipe.cell_size == 512
    assert candidate.artifact_key.endswith(".png")
    assert candidate.artifact_key != "outputs/studio/project-1/motions/idle/source-a.mp4"


async def test_a_short_sheet_source_pads_to_the_requested_count() -> None:
    service, project = await _service_with_candidate(
        _sheet(4), media_kind=MediaKind.SHEET, frame_count=4
    )

    revised = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-a", "user-1",
        ordered_frames=(
            FrameSlot(index=0), FrameSlot(index=1), FrameSlot(index=2), FrameSlot(index=3),
        ),
        frame_duration_ms=80, loop_policy=LoopPolicy.LOOP,
        target_frame_count=8, cell_size=256,
    )

    candidate = revised.manifest.motions[0].selected_candidate
    assert candidate is not None
    assert candidate.metadata.frame_count == 8
    assert candidate.metadata.width == 256 * 8
    assert candidate.metadata.height == 256


async def test_resample_requires_both_target_frame_count_and_cell_size() -> None:
    service, project = await _service_with_candidate(
        _sheet(4), media_kind=MediaKind.SHEET, frame_count=4
    )

    try:
        await service.create_motion_revision(
            "project-1", project.revision, "idle", "source-a", "user-1",
            ordered_frames=(FrameSlot(index=0), FrameSlot(index=1)),
            frame_duration_ms=80, loop_policy=LoopPolicy.LOOP,
            target_frame_count=8, cell_size=None,
        )
    except ValueError as exc:
        assert "target_frame_count and cell_size" in str(exc)
    else:
        raise AssertionError("expected ValueError")


async def test_resampling_is_deterministic_on_the_same_request() -> None:
    service, project = await _service_with_candidate(
        _sheet(4), media_kind=MediaKind.SHEET, frame_count=4
    )
    ordered = (
        FrameSlot(index=0), FrameSlot(index=1), FrameSlot(index=2), FrameSlot(index=3),
    )

    first = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-a", "user-1",
        ordered_frames=ordered, frame_duration_ms=80, loop_policy=LoopPolicy.LOOP,
        target_frame_count=8, cell_size=256,
    )
    second = await service.create_motion_revision(
        "project-1", first.revision, "idle", "source-a", "user-1",
        ordered_frames=ordered, frame_duration_ms=80, loop_policy=LoopPolicy.LOOP,
        target_frame_count=8, cell_size=256,
    )

    assert first.revision == second.revision
    assert len(second.manifest.motions[0].candidates) == 2


def test_resample_route_produces_a_new_packed_png_candidate() -> None:
    import asyncio

    service, project = asyncio.run(
        _service_with_candidate(_sheet(4), media_kind=MediaKind.SHEET, frame_count=4)
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/studio/projects/project-1/motions/idle/revisions",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={
            "expected_revision": project.revision,
            "source_candidate_id": "source-a",
            "ordered_frames": [0, 1, 2, 3],
            "frame_duration_ms": 90,
            "loop_policy": "loop",
            "target_frame_count": 8,
            "cell_size": 256,
        },
    )

    assert response.status_code == 200
    candidates = response.json()["manifest"]["motions"][0]["candidates"]
    assert candidates[-1]["metadata"]["frame_count"] == 8
    assert candidates[-1]["metadata"]["width"] == 256 * 8
    assert candidates[-1]["recipe"] == {
        "op": "resample", "key": None, "similarity": None, "blend": None,
        "target_frame_count": 8, "cell_size": 256, "normalization": None,
        "source_ordered_frames": [{"index": i, "hold": 1} for i in range(4)],
        "sampled_source_indices": [0, 1, 2, 3, 3, 3, 3, 3],
    }


def test_resample_route_rejects_an_unsupported_cell_size() -> None:
    import asyncio

    service, project = asyncio.run(
        _service_with_candidate(_sheet(4), media_kind=MediaKind.SHEET, frame_count=4)
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/studio/projects/project-1/motions/idle/revisions",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={
            "expected_revision": project.revision,
            "source_candidate_id": "source-a",
            "ordered_frames": [0, 1, 2, 3],
            "frame_duration_ms": 90,
            "loop_policy": "loop",
            "target_frame_count": 8,
            "cell_size": 333,
        },
    )

    assert response.status_code == 422


async def test_twelve_frame_loop_keeps_exact_sampled_poses_and_timing() -> None:
    from magic_video_editor.studio.routes import CreateMotionRevisionRequest
    selected = list(range(0, 24, 2))
    request = CreateMotionRevisionRequest.model_validate({
        "expected_revision": 1, "source_candidate_id": "source-a",
        "ordered_frames": [{"index": index, "hold": 1} for index in selected],
        "frame_duration_ms": 83, "loop_policy": "loop",
        "target_frame_count": 12, "cell_size": 256,
    })
    service, project = await _service_with_candidate(
        _sheet(25), media_kind=MediaKind.SHEET, frame_count=25,
    )
    revised = await service.create_motion_revision(
        "project-1", project.revision, "idle", "source-a", "user-1",
        ordered_frames=tuple(FrameSlot(index=index) for index in selected),
        frame_duration_ms=request.frame_duration_ms, loop_policy=LoopPolicy.LOOP,
        target_frame_count=request.target_frame_count, cell_size=request.cell_size,
    )
    candidate = revised.manifest.motions[0].selected_candidate
    assert candidate.metadata.frame_count == 12
    assert candidate.duration_ms == 996
    packed = service._assets.assets[candidate.artifact_key]
    with Image.open(BytesIO(packed)) as sheet:
        assert [sheet.getpixel((index * 256 + 128, 128))[:3] for index in range(12)] == [
            (source * 10 % 255, 40, 90) for source in selected
        ]


def test_normalization_route_records_calibration_and_expanded_pivot():
    import asyncio

    service, project = asyncio.run(
        _service_with_candidate(_sheet(4), media_kind=MediaKind.SHEET, frame_count=4)
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime))
    calibration = dict(scale=7, source_anchor_x=32, source_anchor_y=54,
                       target_anchor_x=128, target_anchor_y=230, padding=8)
    response = client.post(
        "/studio/projects/project-1/motions/idle/revisions",
        headers={"X-Studio-Actor-Id": "user-1"},
        json=dict(expected_revision=project.revision, source_candidate_id="source-a",
                  ordered_frames=[0, 1, 2, 3], frame_duration_ms=90, loop_policy="once",
                  target_frame_count=16, cell_size=256, normalization=calibration),
    )
    assert response.status_code == 200, response.text
    candidate = response.json()["manifest"]["motions"][0]["candidates"][-1]
    assert candidate["recipe"]["normalization"] == {**calibration, "reference_candidate_id": None, "reference_sha256": None, "cleanup_green_edges": False}
    assert candidate["recipe"]["op"] == "normalize"
    assert candidate["metadata"]["height"] > 256
    assert candidate["metadata"]["pivot_x"] > 128
    assert candidate["metadata"]["pivot_y"] > 230
    assert candidate["duration_ms"] == 16 * 90

def test_frame_route_is_owned_paginated_and_cache_is_reused():
    import asyncio
    service, project = asyncio.run(_service_with_candidate(_sheet(4), media_kind=MediaKind.SHEET, frame_count=4))
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime))
    path = '/studio/projects/project-1/motions/idle/candidates/source-a/frames?start=1&limit=2&stride=2'
    assert client.get(path, headers={'X-Studio-Actor-Id': 'intruder'}).status_code == 404
    response = client.get(path, headers={'X-Studio-Actor-Id': 'user-1'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [f['index'] for f in body['frames']] == [1, 3]
    assert body['frames'][0]['image'].startswith('data:image/png;base64,')
    assert body['frames'][0]['edge_contact'] is False
    assert client.get(path, headers={'X-Studio-Actor-Id': 'user-1'}).json() == body
    suggestion = client.post(path.split('/frames')[0] + '/loop-suggestion',
        headers={'X-Studio-Actor-Id': 'user-1'}, json={'mode': 'attack', 'target_frame_count': 12})
    assert suggestion.status_code == 200
    assert suggestion.json()['status'] == 'incomplete'


async def test_native_frame_analysis_uses_decoded_fps_not_requested_duration():
    from magic_video_editor.studio.frame_analysis import analyze_candidate_frames
    content = _video(12, duration_s=1)
    service, project = await _service_with_candidate(content, media_kind=MediaKind.VIDEO, frame_count=12)
    candidate = project.manifest.motions[0].candidates[0]
    assert candidate.duration_ms == 2000
    analysis = analyze_candidate_frames(content, candidate)
    assert analysis['frame_count'] == 12
    assert analysis['duration_ms'] == 1000
    assert analysis['fps'] == 12
