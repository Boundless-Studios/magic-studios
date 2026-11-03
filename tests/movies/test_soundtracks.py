import asyncio
import json
import subprocess

import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.models import Clip
from magic_video_editor.movies.soundtracks import AudioTrack


async def test_assembly_mixes_offset_audio_into_the_movie(tmp_path):
    assets = MemoryAssetStore()
    video = tmp_path / "source.mp4"
    audio = tmp_path / "score.wav"
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
            str(video),
        ],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            str(audio),
        ],
        check=True,
    )
    await assets.write_once("outputs/source.mp4", video.read_bytes(), "video/mp4")
    await assets.write_once("outputs/score.wav", audio.read_bytes(), "audio/wav")
    media = MovieMedia(assets)
    assert await media.audio_duration(audio.read_bytes()) == 1
    await media.assemble(
        [("outputs/source.mp4", Clip(shot_id="s", take_id="t", out_seconds=2))],
        "outputs/mixed.mp4",
        soundtracks=[
            AudioTrack(asset_key="outputs/score.wav", offset_seconds=0.5, gain_db=-7)
        ],
    )
    output = tmp_path / "mixed.mp4"
    output.write_bytes(await assets.read("outputs/mixed.mp4"))
    data = json.loads(
        await asyncio.to_thread(
            subprocess.check_output,
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(output),
            ],
        )
    )
    assert {stream["codec_type"] for stream in data["streams"]} == {"video", "audio"}
    assert 1.9 < float(data["format"]["duration"]) < 2.1


async def test_audio_validation_rejects_non_audio():
    with pytest.raises(ValueError):
        await MovieMedia(MemoryAssetStore()).audio_duration(b"not audio")


async def test_soundtrack_asset_grants_are_isolated_between_movies(tmp_path):
    from magic_video_editor.movies.service import MovieError, MovieService
    from magic_video_editor.movies.soundtracks import save_soundtracks
    from magic_video_editor.movies.store import MemoryMovieStore

    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, media=MovieMedia(assets))
    owner = await service.create("first", "First")
    other = await service.create("second", "Second")
    grant = await service.upload(owner.movie_id, "first", "audio/wav")
    with pytest.raises(MovieError, match="not granted"):
        await save_soundtracks(
            service,
            other.movie_id,
            "second",
            other.revision,
            [AudioTrack(asset_key=grant["asset_key"])],
        )
    with pytest.raises(MovieError, match="not found"):
        await save_soundtracks(service, owner.movie_id, "second", grant["revision"], [])
