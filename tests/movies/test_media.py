import asyncio
import shutil
import subprocess

import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.models import Clip


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [0, 0.5, 1])
async def test_real_ffmpeg_final_frame_and_normalized_trimmed_cut(tmp_path, transition):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    for i, (size, color) in enumerate([("768x448", "red"), ("120x80", "blue")]):
        path = tmp_path / f"{i}.mp4"
        await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={size}:r=16:d=2",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
        await assets.write_once(f"outputs/{i}.mp4", path.read_bytes(), "video/mp4")
    frame = await media.final_frame("outputs/0.mp4")
    assert (await assets.read(frame)).startswith(b"\x89PNG")
    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(await assets.read(frame))).size == (768, 448)
    assert await media.duration_for_key("outputs/0.mp4") == pytest.approx(2, abs=0.1)
    await media.assemble(
        [
            (
                f"outputs/{i}.mp4",
                Clip(
                    shot_id=str(i),
                    take_id=str(i),
                    in_seconds=0.5,
                    out_seconds=1.5,
                    transition_seconds=transition,
                ),
            )
            for i in range(2)
        ],
        "outputs/cut.mp4",
    )
    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    result = await asyncio.to_thread(
        subprocess.check_output,
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(output),
        ],
    )
    overlap = min(transition, 0.5)
    assert float(result) == pytest.approx(2 - overlap, abs=0.05)
    if overlap:
        # Inspect decoded pixels at the join: purple is an actual blend, not a cut.
        pixels = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(1 - overlap / 2),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "crop=2:2:384:216",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]
        )
        red, green, blue = pixels[:3]
        assert 80 < red < 180 and green < 15 and 80 < blue < 180
    geometry = await asyncio.to_thread(
        subprocess.check_output,
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "csv=p=0",
            str(output),
        ],
    )
    assert geometry.decode().strip() == "h264,768,432"
    count = assets.publish_count
    await media.assemble([], "outputs/cut.mp4")
    assert assets.publish_count == count


@pytest.mark.asyncio
async def test_inspect_uploaded_video_reads_actual_duration(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    path = tmp_path / "import.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x60:r=16:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    assert await media.duration(path.read_bytes()) == 2


@pytest.mark.asyncio
async def test_video_import_does_not_follow_playlist_references(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    external = tmp_path / "external.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x60:r=16:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(external),
        ],
        check=True,
    )
    playlist = f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nfile://{external}\n#EXT-X-ENDLIST\n".encode()
    with pytest.raises(ValueError):
        await MovieMedia(MemoryAssetStore()).duration(playlist)


@pytest.mark.asyncio
async def test_final_frame_rejects_playlist_that_references_another_local_file(
    tmp_path,
):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    external = tmp_path / "external.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x60:r=16:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(external),
        ],
        check=True,
    )
    assets = MemoryAssetStore()
    playlist = f"ffconcat version 1.0\nfile {external}\n".encode()
    await assets.write_once("outputs/forged.mp4", playlist, "video/mp4")
    with pytest.raises(ValueError):
        await MovieMedia(assets).final_frame("outputs/forged.mp4")


@pytest.mark.asyncio
async def test_video_upload_requires_mp4_container(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    disguised = tmp_path / "input.avi"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x60:r=16:d=1",
            "-an",
            "-c:v",
            "mpeg4",
            str(disguised),
        ],
        check=True,
    )
    with pytest.raises(ValueError):
        await MovieMedia(MemoryAssetStore()).duration(disguised.read_bytes())


def test_legacy_clip_serialization_preserves_cut_digest():
    original = {"shot_id": "s", "take_id": "t", "in_seconds": 0.0, "out_seconds": 4.0}
    assert Clip.model_validate(original).model_dump() == original
    assert Clip(**original, transition_seconds=0.5).model_dump() != original
