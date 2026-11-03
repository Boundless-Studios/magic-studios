"""Assembly keeps each take's own sound and mixes cues around it."""

import asyncio
import json
import shutil
import subprocess

import numpy as np
import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.models import Clip
from magic_video_editor.movies.soundtracks import AudioTrack

SILENCE_DB = -60


def _clip(path, color, tone_hz=None, seconds=2):
    inputs = ["-f", "lavfi", "-i", f"color=c={color}:s=768x448:r=24:d={seconds}"]
    if tone_hz:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={tone_hz}:r=48000:d={seconds}"]
        codec = ["-c:a", "aac"]
    else:
        inputs += ["-an"]
        codec = []
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *inputs,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            *codec,
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _bed(path, seconds=4):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=110:r=48000:d={seconds}",
            "-af",
            "volume=-12dB",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _streams(path):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ]
    )
    return [(s["codec_type"], s["codec_name"]) for s in json.loads(out)["streams"]]


def _level_db(path, start, seconds, band=None):
    """Mean RMS level of a window of the output's audio, optionally one band."""
    filters = ([band] if band else []) + [
        "astats=measure_overall=RMS_level:measure_perchannel=none"
    ]
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-ss",
            str(start),
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    line = next(line for line in result.stderr.splitlines() if "RMS level dB" in line)
    return float(line.rsplit(":", 1)[1])


async def _sources(tmp_path, assets):
    # The "speech" tone sits far above the bed's band so the lowpass measurement
    # below reads the bed alone.
    spoken = await asyncio.to_thread(_clip, tmp_path / "spoken.mp4", "red", 4000)
    silent = await asyncio.to_thread(_clip, tmp_path / "silent.mp4", "blue")
    await assets.write_once("outputs/spoken.mp4", spoken, "video/mp4")
    await assets.write_once("outputs/silent.mp4", silent, "video/mp4")


def _cut(transition=0):
    return [
        (
            "outputs/spoken.mp4",
            Clip(
                shot_id="a",
                take_id="a",
                in_seconds=0,
                out_seconds=2,
                transition_seconds=transition,
            ),
        ),
        ("outputs/silent.mp4", Clip(shot_id="b", take_id="b", in_seconds=0, out_seconds=2)),
    ]


@pytest.mark.asyncio
async def test_a_muted_clip_loses_whatever_the_take_vocalised(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    await _sources(tmp_path, assets)
    await media.assemble(
        [("outputs/spoken.mp4", Clip(shot_id="a", take_id="a", in_seconds=0, out_seconds=2, mute=True)),
         ("outputs/spoken.mp4", Clip(shot_id="b", take_id="b", in_seconds=0, out_seconds=2))],
        "outputs/cut.mp4",
    )
    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    muted = await asyncio.to_thread(_level_db, output, 0.2, 1.5)
    spoken = await asyncio.to_thread(_level_db, output, 2.2, 1.5)
    assert muted < SILENCE_DB
    assert spoken > SILENCE_DB + 20


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [0, 0.5])
async def test_a_takes_own_sound_survives_the_cut_and_silent_takes_stay_silent(
    tmp_path, transition
):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    await _sources(tmp_path, assets)

    await media.assemble(_cut(transition), "outputs/cut.mp4")

    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    assert await asyncio.to_thread(_streams, output) == [
        ("video", "h264"),
        ("audio", "aac"),
    ]
    # The tone plays through the first take; nothing plays in the second.
    spoken = await asyncio.to_thread(_level_db, output, 0.2, 1.0)
    silent = await asyncio.to_thread(_level_db, output, 4 - transition - 1.2, 1.0)
    assert spoken > SILENCE_DB + 20
    assert silent < SILENCE_DB


@pytest.mark.asyncio
async def test_a_cue_ducks_under_the_takes_sound_and_returns_after_it(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    await _sources(tmp_path, assets)
    await assets.write_once(
        "outputs/bed.wav", await asyncio.to_thread(_bed, tmp_path / "bed.wav"), "audio/wav"
    )

    await media.assemble(
        _cut(), "outputs/cut.mp4", soundtracks=[AudioTrack(asset_key="outputs/bed.wav")]
    )

    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    # Measure only the bed's band: it sits several dB lower under the take's
    # tone than after the tone ends, and it is still audible in both.
    under = await asyncio.to_thread(_level_db, output, 0.5, 1.0, "lowpass=f=200")
    after = await asyncio.to_thread(_level_db, output, 2.8, 1.0, "lowpass=f=200")
    assert after - under > 4
    assert under > SILENCE_DB


def _short_audio_clip(path, seconds=2, audio_seconds=1):
    """Video runs the full length; its own sound stops early."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s=768x448:r=24:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=4000:r=48000:d={audio_seconds}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.mark.asyncio
async def test_a_take_with_short_audio_does_not_pull_the_next_clips_sound_forward(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    await assets.write_once("outputs/short.mp4", await asyncio.to_thread(_short_audio_clip, tmp_path / "short.mp4"), "video/mp4")
    await assets.write_once("outputs/spoken.mp4", await asyncio.to_thread(_clip, tmp_path / "spoken.mp4", "blue", 440), "video/mp4")
    await media.assemble(
        [("outputs/short.mp4", Clip(shot_id="a", take_id="a", in_seconds=0, out_seconds=2)),
         ("outputs/spoken.mp4", Clip(shot_id="b", take_id="b", in_seconds=0, out_seconds=2))],
        "outputs/cut.mp4",
    )
    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    # 1.2-1.9 s belongs to the first clip's silent tail; the second clip's tone
    # must not have moved into it.
    gap = await asyncio.to_thread(_level_db, output, 1.2, 0.7)
    second = await asyncio.to_thread(_level_db, output, 2.3, 1.0)
    assert gap < SILENCE_DB
    assert second > SILENCE_DB + 20


def _take_like_clip(path, seconds, seed):
    """A clip shaped like an H3 take: 24 fps picture, 32 kHz AAC sound, a
    length that is not a whole number of AAC frames, and a distinctive burst
    pattern so its sound can be located by correlation."""
    bursts = "+".join(f"between(t,{0.3 + 0.37 * k * (1 + seed / 10):.3f},{0.3 + 0.37 * k * (1 + seed / 10) + 0.12:.3f})" for k in range(6))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s=768x448:r=24:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={1500 + 200 * seed}:r=32000:d={seconds}",
         "-af", f"volume='if({bursts},1,0)':eval=frame",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "32000", "-shortest", str(path)],
        check=True,
    )
    return path.read_bytes()


def _envelope(path, start, seconds):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-i", str(path), "-t", f"{seconds:.4f}",
         "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    n = len(x) // 40
    e = np.log(np.sqrt(np.mean(x[: n * 40].reshape(n, 40) ** 2, axis=1) + 1e-9))
    return (e - e.mean()) / (e.std() + 1e-9)


def _offset_ms(take_path, cut_path, expected_at, seconds, window=0.5):
    """Where the take's sound sits in the cut relative to its picture; 5 ms hops."""
    take = _envelope(take_path, 0.0, seconds - 0.2)
    start = max(0.0, expected_at - window)
    cut = _envelope(cut_path, start, seconds - 0.2 + (expected_at - start) + window)
    best, lag = -1.0, 0
    for candidate in range(len(cut) - len(take)):
        c = float(np.dot(cut[candidate: candidate + len(take)], take) / len(take))
        if c > best:
            best, lag = c, candidate
    return (start + lag * 0.005 - expected_at) * 1000, best


@pytest.mark.asyncio
async def test_joins_do_not_accumulate_audio_drift(tmp_path):
    """Stream-copying AAC intermediates through the concat demuxer shifted each
    later clip's sound by that file's priming; six takes in, the last one ran
    tens of milliseconds late. Every clip's sound must sit within 10 ms of
    where its picture starts."""
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    assets = MemoryAssetStore()
    media = MovieMedia(assets)
    length = 2.71
    sources, paths = [], []
    for i in range(6):
        path = tmp_path / f"take{i}.mp4"
        await assets.write_once(f"outputs/{i}.mp4", await asyncio.to_thread(_take_like_clip, path, length, i), "video/mp4")
        sources.append((f"outputs/{i}.mp4", Clip(shot_id=str(i), take_id=str(i), in_seconds=0, out_seconds=length)))
        paths.append(path)
    await media.assemble(sources, "outputs/cut.mp4")
    output = tmp_path / "cut.mp4"
    output.write_bytes(await assets.read("outputs/cut.mp4"))
    offsets = []
    for i, path in enumerate(paths):
        ms, corr = await asyncio.to_thread(_offset_ms, path, output, i * length, length)
        assert corr > 0.5, f"clip {i} sound not found"
        offsets.append(round(ms))
    assert all(abs(ms) <= 10 for ms in offsets), offsets
