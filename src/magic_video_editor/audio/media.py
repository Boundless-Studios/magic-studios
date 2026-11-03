"""Loudness renditions and measurements of candidate audio, with bounded FFmpeg runs.

The chain is CINDER's (`gen_sfx.py` `_encode`): EBU R128 loudness to -16 LUFS with
a -1.5 dBTP target, a limiter, 44.1 kHz. The rendition is a 16-bit WAV, as Under
Fire's `audio_renditions.py` makes, so it plays everywhere and a movie can take it
as a cue. The original is never changed. Measurements are taken on the original:
integrated loudness, true peak and mean spectral centroid.

These run on the service's own FFmpeg, which public accounts never use; their
candidates keep the original only.
"""

from __future__ import annotations

import asyncio
import math
import re
import subprocess
import tempfile
from pathlib import Path

from magic_video_editor.audio.models import Measurement
from magic_video_editor.cpu_worker import AUDIO_INPUT, _audio_duration

RENDITION_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.89,aresample=44100"
CENTROID_KEY = "lavfi.aspectralstats.1.centroid"
MAX_RENDITION_SECONDS = 600


def _source(tmp: str, content: bytes) -> Path:
    path = Path(tmp) / "source"
    path.write_bytes(content)
    return path


def _number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return round(number, 2) if math.isfinite(number) else None


def _measure(path: Path) -> Measurement:
    loudness = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", *AUDIO_INPUT, "-i", str(path),
         "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
        check=False, capture_output=True, text=True, timeout=120,
    )
    lufs = peak = None
    section = ""
    for line in loudness.stderr.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(("I:", "Peak:")):
            section = stripped
        if stripped.startswith("I:") and "LUFS" in stripped:
            lufs = _number(stripped.split()[1])
        elif stripped.startswith("Peak:") and section.startswith("True peak"):
            peak = _number(stripped.split()[1])
    centroid = None
    spectral = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *AUDIO_INPUT,
         "-i", str(path), "-af",
         f"aformat=channel_layouts=mono,aspectralstats,ametadata=print:key={CENTROID_KEY}:file=-",
         "-f", "null", "-"],
        check=False, capture_output=True, text=True, timeout=120,
    )
    values = [
        v for v in (
            _number(m.group(1))
            for m in re.finditer(rf"{re.escape(CENTROID_KEY)}=([-0-9.eE+]+)", spectral.stdout)
        )
        if v is not None and v > 0
    ]
    if values:
        centroid = round(sum(values) / len(values), 1)
    # -inf (digital silence) parses as None rather than a number.
    return Measurement(integrated_lufs=lufs, true_peak_dbtp=peak, centroid_hz=centroid)


def _render(path: Path, target: Path) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *AUDIO_INPUT,
         "-i", str(path), "-vn", "-af", RENDITION_FILTER, "-c:a", "pcm_s16le",
         "-f", "wav", str(target)],
        check=False, capture_output=True, timeout=300,
    )
    if result.returncode or not target.exists():
        raise ValueError("The audio could not be rendered.")
    return target.read_bytes()


def process(content: bytes) -> tuple[float, Measurement, bytes | None]:
    """Duration, measurements and (for audio up to ten minutes) the rendition."""
    duration = _audio_duration(content)
    with tempfile.TemporaryDirectory(prefix="audio-candidate-") as tmp:
        source = _source(tmp, content)
        measured = _measure(source)
        rendition = (
            _render(source, Path(tmp) / "rendition.wav")
            if duration <= MAX_RENDITION_SECONDS
            else None
        )
    return round(duration, 3), measured, rendition


class AudioMedia:
    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(2)

    async def process(self, content: bytes) -> tuple[float, Measurement, bytes | None]:
        async with self.slots:
            return await asyncio.to_thread(process, content)

    async def duration(self, content: bytes) -> float:
        async with self.slots:
            return round(await asyncio.to_thread(_audio_duration, content), 3)
