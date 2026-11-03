from magic_video_editor.contracts import GpuGenerationRequest
from magic_video_editor.local_studio import DeterministicGpu, LocalAssetStore


def _request(**overrides: object) -> GpuGenerationRequest:
    defaults: dict[str, object] = dict(
        source_key="sources/subject.png",
        output_key="outputs/motion.png",
        prompt="idle",
        width=512,
        height=512,
        duration_seconds=4,
        seed=1,
        idempotency_key="project-1:idle:attempt-1",
        motion_id="idle",
        loop=True,
        preset="standard",
        steps=20,
        provider_parameters={},
    )
    defaults.update(overrides)
    return GpuGenerationRequest(**defaults)


async def test_local_gpu_output_varies_with_conditioning_start_frame() -> None:
    """T2: the deterministic local provider must let a test prove the
    approved shared start frame reached it -- otherwise a regression that
    drops conditioning before dispatch would go unnoticed locally."""
    assets = LocalAssetStore()
    gpu = DeterministicGpu(assets)

    unconditioned = _request(output_key="outputs/unconditioned.png")
    await gpu.generate(unconditioned)
    conditioned_a = _request(
        output_key="outputs/conditioned-a.png",
        start_frame_key="outputs/studio/project-1/compositions/comp-a.png",
    )
    await gpu.generate(conditioned_a)
    conditioned_b = _request(
        output_key="outputs/conditioned-b.png",
        start_frame_key="outputs/studio/project-1/compositions/comp-b.png",
    )
    await gpu.generate(conditioned_b)

    unconditioned_bytes = await assets.read("outputs/unconditioned.png")
    conditioned_a_bytes = await assets.read("outputs/conditioned-a.png")
    conditioned_b_bytes = await assets.read("outputs/conditioned-b.png")

    assert conditioned_a_bytes != unconditioned_bytes
    assert conditioned_a_bytes != conditioned_b_bytes


def test_local_frame_previews_decode_all_poses_and_cache(monkeypatch):
    import base64
    import subprocess
    from io import BytesIO
    from fastapi.testclient import TestClient
    from PIL import Image
    from magic_video_editor.local_studio import create_local_studio_app, create_local_studio_runtime
    source = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=s=64x64:r=8:d=1',
         '-frames:v', '8', '-pix_fmt', 'yuv420p', '-f', 'mp4',
         '-movflags', 'frag_keyframe+empty_moov', 'pipe:1'],
        capture_output=True, check=True, timeout=30,
    ).stdout
    runtime = create_local_studio_runtime()
    runtime.assets.assets['outputs/test.mp4'] = source
    with TestClient(create_local_studio_app(runtime=runtime)) as client:
        url = '/local-assets/outputs/test.mp4?frame_previews=8'
        response = client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert len(data['frames']) == len(data['poses']) == 8
        assert len(set(data['frames'])) == 8
        poster = client.get('/local-assets/outputs/test.mp4?poster=true')
        assert poster.status_code == 200
        assert poster.headers['content-type'] == 'image/png'
        with Image.open(BytesIO(poster.content)) as image:
            assert image.size == (384, 384)
        assert data['duration'] == 1
        with Image.open(BytesIO(base64.b64decode(data['frames'][0].split(',')[1]))) as frame:
            assert frame.size == (64, 64)
        async def unexpected_decode(*args):
            raise AssertionError('cached previews should not decode again')
        monkeypatch.setattr('magic_video_editor.local_frame_previews.build_frame_previews', unexpected_decode)
        assert client.get(url).json() == data
        assert client.get('/local-assets/outputs/test.mp4?frame_previews=99999').status_code == 422


def test_local_assets_support_video_byte_ranges():
    from fastapi.testclient import TestClient
    from magic_video_editor.local_studio import create_local_studio_app, create_local_studio_runtime
    runtime = create_local_studio_runtime()
    runtime.assets.assets['outputs/clip.mp4'] = b'0123456789'
    with TestClient(create_local_studio_app(runtime=runtime)) as client:
        url = '/local-assets/outputs/clip.mp4'
        for value, expected, content_range in [
            ('bytes=0-3', b'0123', 'bytes 0-3/10'),
            ('bytes=7-', b'789', 'bytes 7-9/10'),
            ('bytes=-4', b'6789', 'bytes 6-9/10'),
        ]:
            response = client.get(url, headers={'Range': value})
            assert response.status_code == 206
            assert response.content == expected
            assert response.headers['Content-Range'] == content_range
            assert response.headers['Accept-Ranges'] == 'bytes'
        assert client.get(url, headers={'Range': 'bytes=10-'}).status_code == 416
        assert client.get(url).content == b'0123456789'
