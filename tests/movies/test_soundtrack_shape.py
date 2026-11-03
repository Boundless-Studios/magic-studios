"""Cue shaping (end, fades, swell) and the programme master in assembly."""

import asyncio
import shutil
import subprocess

import pytest

from magic_video_editor.cpu_worker import _validated
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.models import Clip
from magic_video_editor.movies.soundtracks import AudioTrack


def _silent_clip(path, seconds=12):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s=768x448:r=24:d={seconds}",
         "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path.read_bytes()


def _tone(path, seconds=12):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=220:r=48000:d={seconds}",
         "-af", "volume=-20dB", str(path)],
        check=True,
    )
    return path.read_bytes()


def _level_db(path, start, seconds):
    result = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(seconds), "-i", str(path), "-af",
         "astats=measure_overall=RMS_level:measure_perchannel=none", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    line = next(line for line in result.stderr.splitlines() if "RMS level dB" in line)
    return float(line.rsplit(":", 1)[1])


def _integrated(path):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    line = next(line for line in reversed(result.stderr.splitlines()) if line.strip().startswith("I:"))
    return float(line.split()[1])


async def _assemble(tmp_path, track):
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    await assets.write_once("outputs/clip.mp4", await asyncio.to_thread(_silent_clip, tmp_path / "clip.mp4"), "video/mp4")
    await assets.write_once("outputs/tone.wav", await asyncio.to_thread(_tone, tmp_path / "tone.wav"), "audio/wav")
    await media.assemble(
        [("outputs/clip.mp4", Clip(shot_id="a", take_id="a", in_seconds=0, out_seconds=12))],
        "outputs/cut.mp4",
        soundtracks=[track],
    )
    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    return output


def test_a_swell_needs_a_duration_and_an_end_after_the_start():
    with pytest.raises(ValueError, match="swell"):
        AudioTrack(asset_key="k", swell_db=6)
    with pytest.raises(ValueError, match="end after"):
        AudioTrack(asset_key="k", offset_seconds=5, end_seconds=5)
    AudioTrack(asset_key="k", offset_seconds=1, end_seconds=8, swell_db=6, swell_seconds=3)


def test_the_worker_accepts_shaped_cues_and_refuses_bad_shapes():
    base = {"key": "a", "offset_seconds": 1, "trim_start_seconds": 0, "gain_db": -7}
    shaped = {**base, "end_seconds": 8, "fade_in_seconds": 1, "fade_out_seconds": 0.5, "swell_db": 9, "swell_seconds": 4}
    payload = {"operation": "assemble", "sources": [{"key": "v", "in_seconds": 0, "out_seconds": 2}], "audio": [shaped]}
    assert _validated(payload, ("v", "a")) is payload
    from outbound_gpu_worker_pool.plugins import PluginRequestRejected

    for bad in ({**base, "end_seconds": 1}, {**base, "swell_db": 40}, {**base, "shape": 1}):
        with pytest.raises(PluginRequestRejected):
            _validated({**payload, "audio": [bad]}, ("v", "a"))


@pytest.mark.asyncio
async def test_a_cue_swells_towards_its_end_then_stops_and_the_programme_is_mastered(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    # Tone under a silent clip: starts at 1 s, swells +12 dB over its last 4 s,
    # ends at 9 s with a short fade-out; nothing after 9 s.
    output = await _assemble(
        tmp_path,
        AudioTrack(asset_key="outputs/tone.wav", offset_seconds=1, end_seconds=9, gain_db=-6,
                   fade_in_seconds=0.5, fade_out_seconds=0.25, swell_db=12, swell_seconds=4),
    )
    early = await asyncio.to_thread(_level_db, output, 2.0, 1.0)
    late = await asyncio.to_thread(_level_db, output, 7.5, 1.0)
    after = await asyncio.to_thread(_level_db, output, 10.0, 1.5)
    before = await asyncio.to_thread(_level_db, output, 0.0, 0.8)
    assert 7 < late - early < 13
    assert after < -60 and before < -60
    # Master by gain: the programme lands on the target within a dB.
    assert abs(await asyncio.to_thread(_integrated, output) - (-16.0)) < 1.0


@pytest.mark.asyncio
async def test_a_flat_cue_is_unchanged_in_shape_and_still_mastered(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    output = await _assemble(tmp_path, AudioTrack(asset_key="outputs/tone.wav", gain_db=-12))
    early = await asyncio.to_thread(_level_db, output, 1.0, 1.0)
    late = await asyncio.to_thread(_level_db, output, 10.0, 1.0)
    assert abs(late - early) < 1
    assert abs(await asyncio.to_thread(_integrated, output) - (-16.0)) < 1.0
