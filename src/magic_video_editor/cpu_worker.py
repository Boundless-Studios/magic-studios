"""Standalone, bounded FFmpeg execution for attached CPU workers.

Published with worker.py. No service credentials, database access, or arbitrary
commands: the agent supplies only the input files of one fenced job.
"""

from __future__ import annotations
import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

MAX_VIDEO_BYTES = 256 * 1024 * 1024
MAX_CUT_BYTES = 512 * 1024 * 1024
MAX_AUDIO_BYTES = 50 * 1024 * 1024
VIDEO_INPUT = ["-protocol_whitelist", "file,pipe", "-f", "mov"]
AUDIO_INPUT = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3,wav"]


# How a clip lands in the 768x432 cut. A source within 10% of 16:9 (a generated
# 768x448 take) is cropped to 16:9 and fills the frame. Anything else (an imported
# portrait or 4:3 clip) keeps its whole picture and is padded. Cropping happens
# before scaling and scaling only ever fits, so neither intermediate dimension can
# exceed the source or the frame (cover-scaling a 4x4000 sliver needs gigabytes).
_NEAR = "lt(abs(iw/ih/(768/432)-1),0.1)"
FRAME_FILTER = (
    f"crop=w='if({_NEAR},min(iw,ih*768/432),iw)':h='if({_NEAR},min(ih,iw*432/768),ih)',"
    "scale=768:432:force_original_aspect_ratio=decrease,"
    "pad=768:432:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24"
)
# Part of every assembled cut's file name: bump it whenever FRAME_FILTER or the mix
# changes how a cut renders, so an unchanged cut is rendered again instead of
# reusing a file made by the old renderer.
ASSEMBLY_RENDER = "render2"


def _run(args: list[str], timeout: float = 120) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "2",
            *args,
        ],
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError(
            "FFmpeg rejected movie media: "
            + result.stderr.decode(errors="replace")[-300:]
        )


def _last_frame(content: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="movie-frame-") as tmp:
        source = Path(tmp) / "source.mp4"
        out = Path(tmp) / "last.png"
        source.write_bytes(content)
        # update writes the final decoded frame, including variable-frame-rate tails.
        _run(
            [
                *VIDEO_INPUT,
                "-i",
                str(source),
                "-an",
                "-vf",
                "scale=768:448:force_original_aspect_ratio=increase,crop=768:448",
                "-update",
                "1",
                str(out),
            ]
        )
        return out.read_bytes()


def _extract_audio(content: bytes) -> bytes:
    """A clip's own soundtrack as mono 24 kHz WAV, with silence at either end
    trimmed, so it can serve as a voice reference."""
    with tempfile.TemporaryDirectory(prefix="movie-voice-") as tmp:
        source = Path(tmp) / "source.mp4"
        out = Path(tmp) / "voice.wav"
        source.write_bytes(content)
        _run(
            [
                *VIDEO_INPUT,
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-af",
                (
                    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
                    "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse"
                ),
                "-c:a",
                "pcm_s16le",
                str(out),
            ]
        )
        return out.read_bytes()


def _has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            *VIDEO_INPUT,
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0 and b"audio" in result.stdout


# Every clip carries the same AAC track layout so joins stream-copy and
# crossfade cleanly; a silent clip gets silence rather than losing its lane.
AUDIO_OUTPUT = ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k"]
# Intermediate clips carry PCM in MOV: stream-copying AAC through the concat
# demuxer shifts every later clip's sound by that file's priming samples,
# about 20 ms per clip, and the drift accumulates across a movie.
INTERMEDIATE_AUDIO = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]


def _assemble(
    sources: list[tuple[bytes, Any]],
    audio: list[tuple[bytes, Any]] | None = None,
) -> bytes:
    if not sources:
        raise ValueError("Cut is empty")
    with tempfile.TemporaryDirectory(prefix="movie-cut-") as tmp:
        root = Path(tmp)
        durations = []
        normalized = []
        deadline = time.monotonic() + 240
        for i, (content, clip) in enumerate(sources):
            if time.monotonic() >= deadline:
                raise ValueError("Assembly exceeded 240-second CPU budget")
            source = root / f"source-{i}.mp4"
            source.write_bytes(content)
            output = root / f"clip-{i}.mov"
            # A take's own sound (its dialogue) survives normalisation; the
            # bed is mixed around it later.
            if _has_audio(source) and not getattr(clip, "mute", False):
                # Pad the sound to the picture: a take whose audio ends early
                # would otherwise pull every later clip's audio forward.
                sound = ["-map", "0:v:0", "-map", "0:a:0", "-af", "aresample=async=1,apad"]
            else:
                # The clip's `-t` bounds the silence to the picture; `-shortest`
                # must not be used here — on FFmpeg 6.1 it ended the output before
                # any silence was written, leaving a clip with no audio stream.
                sound = [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                ]
            _run(
                [
                    "-ss",
                    str(clip.in_seconds),
                    *VIDEO_INPUT,
                    "-i",
                    str(source),
                    *sound,
                    "-t",
                    str(clip.out_seconds - clip.in_seconds),
                    "-vf",
                    FRAME_FILTER,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    *INTERMEDIATE_AUDIO,
                    "-movflags",
                    "+faststart",
                    "-threads",
                    "2",
                    str(output),
                ],
                timeout=max(0.1, min(120, deadline - time.monotonic())),
            )
            # Join arithmetic follows the picture: the container's duration is
            # the longer of the two streams, and padded audio would push every
            # dissolve past the frame it belongs on.
            picture = _video_duration(output)
            # A source not at 24 fps rounds to a different picture length than
            # its own sound; pad or trim the sound to the picture exactly so
            # every join keeps the two locked.
            fitted = root / f"fitted-{i}.mov"
            _run(
                [
                    *VIDEO_INPUT,
                    "-i",
                    str(output),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "copy",
                    "-af",
                    "apad",
                    "-t",
                    f"{picture:.6f}",
                    *INTERMEDIATE_AUDIO,
                    str(fitted),
                ],
                timeout=max(0.1, min(120, deadline - time.monotonic())),
            )
            output.unlink()
            normalized.append(fitted)
            durations.append(picture)
        # Pairwise rendering bounds decoder/filter memory even for long sequences.
        # Limit overlaps to half of either neighbour, so short trims remain valid.
        out = normalized[0]
        duration = durations[0]
        for i in range(1, len(normalized)):
            if time.monotonic() >= deadline:
                raise ValueError("Assembly exceeded 240-second CPU budget")
            requested = getattr(sources[i - 1][1], "transition_seconds", 0)
            overlap = (
                math.floor(min(requested, durations[i - 1] / 2, durations[i] / 2) * 24)
                / 24
            )
            joined = root / f"joined-{i}.mov"
            if overlap:
                # Some FFmpeg versions clear frame-rate metadata after setpts.
                # Re-establish CFR before xfade, not merely before encoding.
                filters = (
                    "[0:v]setpts=PTS-STARTPTS,fps=24,settb=AVTB[a];"
                    "[1:v]setpts=PTS-STARTPTS,fps=24,settb=AVTB[b];"
                    f"[a][b]xfade=transition=fade:duration={overlap}:offset={duration - overlap},format=yuv420p[v];"
                    f"[0:a][1:a]acrossfade=d={overlap}:c1=tri:c2=tri[s]"
                )
                args = [
                    *VIDEO_INPUT,
                    "-i",
                    str(out),
                    *VIDEO_INPUT,
                    "-i",
                    str(normalized[i]),
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    filters,
                    "-map",
                    "[v]",
                    "-map",
                    "[s]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    *INTERMEDIATE_AUDIO,
                    "-threads",
                    "2",
                ]
            else:
                listing = root / "concat.txt"
                listing.write_text(f"file '{out.name}'\nfile '{normalized[i].name}'")
                args = ["-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy"]
            _run(
                [*args, "-movflags", "+faststart", str(joined)],
                timeout=max(0.1, min(120, deadline - time.monotonic())),
            )
            duration = _video_duration(joined)
            if out not in normalized:
                out.unlink()
            out = joined
        if audio:
            mixed = root / "mixed.mov"
            inputs = [*VIDEO_INPUT, "-i", str(out)]
            filters = ["[0:a]asplit=2[voice][key]"]
            for index, (content, track) in enumerate(audio):
                source = root / f"audio-{index}"
                source.write_bytes(content)
                inputs.extend([*AUDIO_INPUT, "-i", str(source)])
                filters.append(f"[{index + 1}:a]{_cue_filter(track)}[a{index}]")
            labels = "".join(f"[a{index}]" for index in range(len(audio)))
            # Cues sit under the takes' own sound: the bed ducks about 9 dB
            # while a clip carries speech near -20 dBFS and returns between
            # lines; room tone below -34 dBFS leaves it alone.
            filters.append(f"{labels}amix=inputs={len(audio)}:normalize=0[bed]")
            filters.append(
                "[bed][key]sidechaincompress=threshold=0.02:ratio=3:attack=20:release=400[ducked]"
            )
            filters.append(
                "[voice][ducked]amix=inputs=2:normalize=0,alimiter=limit=0.95:level=false,apad[mix]"
            )
            _run(
                [
                    *inputs,
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    "0:v:0",
                    "-map",
                    "[mix]",
                    "-c:v",
                    "copy",
                    *INTERMEDIATE_AUDIO,
                    "-t",
                    str(duration),
                    "-movflags",
                    "+faststart",
                    str(mixed),
                ],
                timeout=max(0.1, min(120, deadline - time.monotonic())),
            )
            out = mixed
        out = _master(out, root / "master.mp4", deadline)
        if out.stat().st_size > MAX_CUT_BYTES:
            raise ValueError("Assembled cut exceeds 512 MiB")
        return out.read_bytes()


def _cue_filter(track: Any) -> str:
    """One cue's shape: trim to its window, fade in, swell towards its end,
    fade out, set its level, then place it in the movie."""
    trim_start = float(track.trim_start_seconds)
    offset = float(track.offset_seconds)
    end = getattr(track, "end_seconds", None)
    length = None if end is None else max(0.01, float(end) - offset)
    chain = [
        f"atrim=start={trim_start}"
        + (f":end={trim_start + length}" if length is not None else ""),
        "asetpts=PTS-STARTPTS",
    ]
    fade_in = float(getattr(track, "fade_in_seconds", 0) or 0)
    if fade_in:
        chain.append(f"afade=t=in:st=0:d={fade_in}")
    swell_db = float(getattr(track, "swell_db", 0) or 0)
    swell = float(getattr(track, "swell_seconds", 0) or 0)
    if swell_db and swell:
        if length is None:
            raise ValueError("A swell needs the cue's end in the movie")
        start = max(0.0, length - swell)
        # Linear ramp in dB from 0 to swell_db across [start, end].
        chain.append(
            f"volume=volume='pow(10,({swell_db}/20)*max(0,min(1,(t-{start})/{swell})))':eval=frame"
        )
    fade_out = float(getattr(track, "fade_out_seconds", 0) or 0)
    if fade_out:
        if length is None:
            raise ValueError("A fade-out needs the cue's end in the movie")
        chain.append(f"afade=t=out:st={max(0.0, length - fade_out)}:d={fade_out}")
    chain.append(f"volume={float(track.gain_db)}dB")
    chain.append(f"adelay={round(offset * 1000)}:all=1")
    return ",".join(chain)


MASTER_TARGET_LUFS = -16.0
MASTER_CEILING_DBTP = -1.5


def _integrated_lufs(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            *VIDEO_INPUT,
            "-i",
            str(path),
            "-vn",
            "-af",
            "ebur128=framelog=quiet",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in reversed(result.stderr.splitlines()):
        stripped = line.strip()
        if stripped.startswith("I:") and "LUFS" in stripped:
            try:
                return float(stripped.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _master(source: Path, target: Path, deadline: float) -> Path:
    """Master by gain the way the accepted trailer was: one static gain to the
    programme target, then a true-peak ceiling. A silent programme gets no
    gain. This is the one AAC encode: everything before it is PCM."""
    lufs = _integrated_lufs(source)
    silent = lufs is None or not math.isfinite(lufs) or lufs < -60
    gain = 0.0 if silent else MASTER_TARGET_LUFS - lufs
    limit = 10 ** (MASTER_CEILING_DBTP / 20)
    _run(
        [
            *VIDEO_INPUT,
            "-i",
            str(source),
            "-map",
            "0:v:0",
            # Optional: a programme with no sound at all still masters.
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-af",
            f"volume={gain:.2f}dB,alimiter=limit={limit:.4f}:level=false:attack=5:release=50",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(target),
        ],
        timeout=max(0.1, min(120, deadline - time.monotonic())),
    )
    return target


# The accepted hand-finished trailer's own dialogue cuts measure 0.0086-0.0114
# mean frame difference; a join above this reads as a jump.
JOIN_LIMIT = 0.012


def _grey_frame(source: Path, at: float) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, at):.4f}",
            *VIDEO_INPUT,
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=96:54,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode or len(result.stdout) != 96 * 54:
        raise ValueError("Could not decode a frame at the join")
    return result.stdout


def _join_diffs(content: bytes, times: list[float], fps: float = 24.0) -> list[float]:
    """Mean absolute difference (0-1) between the frame before and the frame at
    each join time, on small greyscale frames: a cut, a relit background or an
    identity shift all show up as a spike."""
    with tempfile.TemporaryDirectory(prefix="movie-joins-") as tmp:
        source = Path(tmp) / "source.mp4"
        source.write_bytes(content)
        diffs = []
        for at in times:
            before = _grey_frame(source, at - 1.5 / fps)
            after = _grey_frame(source, at + 0.5 / fps)
            total = sum(abs(a - b) for a, b in zip(before, after, strict=True))
            diffs.append(round(total / (255.0 * len(before)), 5))
        return diffs


def _video_duration(path: Path) -> float:
    """Length of the picture alone, from its frame count when the container
    reports one, so audio padding never enters join arithmetic."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            *VIDEO_INPUT,
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration,nb_frames,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Invalid video clip")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        frames = stream.get("nb_frames")
        rate = stream.get("r_frame_rate", "24/1")
        num, den = (int(part) for part in rate.split("/"))
        if frames and frames.isdigit() and den:
            return int(frames) * den / num
        duration = float(stream["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid video duration")
        return duration
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as e:
        raise ValueError("Clip must contain a valid video stream") from e


def _duration(content: bytes) -> float:
    with tempfile.TemporaryDirectory(prefix="movie-probe-") as tmp:
        source = Path(tmp) / "source.mp4"
        source.write_bytes(content)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                *VIDEO_INPUT,
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,width,height:format=duration",
                "-of",
                "json",
                str(source),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise ValueError("Invalid video clip")
        try:
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            duration = float(data["format"]["duration"])
            if (
                not math.isfinite(duration)
                or not 0.05 <= duration <= 600
                or stream["width"] * stream["height"] > 16_000_000
            ):
                raise ValueError("Clip duration or dimensions exceed limits")
            return duration
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("Clip must contain a valid video stream") from e


def _audio_duration(content: bytes) -> float:
    with tempfile.TemporaryDirectory(prefix="movie-audio-") as tmp:
        source = Path(tmp) / "audio"
        source.write_bytes(content)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                *AUDIO_INPUT,
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(source),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        try:
            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            if (
                result.returncode
                or not data["streams"]
                or not math.isfinite(duration)
                or not 0.05 <= duration <= 3600
            ):
                raise ValueError("Invalid audio")
            return duration
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Choose a valid audio file no longer than one hour."
            ) from exc


# v3: frame filling (FRAME_FILTER). Workers still holding the v2 renderer must not
# take new jobs, so the service submits only v3; old leases stay readable.
CPU_CAPABILITY = "movie.ffmpeg.process.v3"
CPU_CAPABILITIES = (CPU_CAPABILITY, "movie.ffmpeg.process.v2", "movie.ffmpeg.process.v1")
TRACK_KEYS = {
    "key",
    "offset_seconds",
    "trim_start_seconds",
    "gain_db",
    "end_seconds",
    "fade_in_seconds",
    "fade_out_seconds",
    "swell_db",
    "swell_seconds",
}


def _validated(payload, input_keys):
    from outbound_gpu_worker_pool.plugins import PluginRequestRejected

    def refuse():
        raise PluginRequestRejected("Invalid CPU movie processing request")

    if not isinstance(payload, dict) or set(payload) - {
        "operation",
        "sources",
        "audio",
    }:
        refuse()
    operation = payload.get("operation")
    sources, audio = payload.get("sources", []), payload.get("audio", [])
    if (
        operation not in {"assemble", "frame", "probe"}
        or not isinstance(sources, list)
        or not 1 <= len(sources) <= 16
        or not isinstance(audio, list)
        or len(audio) > 8
    ):
        refuse()
    if operation != "assemble" and (len(sources) != 1 or audio):
        refuse()
    used = []

    def number(value, low, high):
        return (
            type(value) in {int, float}
            and math.isfinite(value)
            and low <= value <= high
        )

    for source in sources:
        if not isinstance(source, dict) or (
            set(source) - {"transition_seconds", "mute"}
            if operation == "assemble"
            else set(source)
        ) != (
            {"key", "in_seconds", "out_seconds"} if operation == "assemble" else {"key"}
        ):
            refuse()
        if not isinstance(source["key"], str) or source["key"] not in input_keys:
            refuse()
        used.append(source["key"])
        if operation == "assemble" and (
            not number(source.get("transition_seconds", 0), 0, 1)
            or not isinstance(source.get("mute", False), bool)
            or not number(source["in_seconds"], 0, 600)
            or not number(source["out_seconds"], 0.05, 600)
            or source["in_seconds"] >= source["out_seconds"]
        ):
            refuse()
    for track in audio:
        if not isinstance(track, dict) or not {
            "key",
            "offset_seconds",
            "trim_start_seconds",
            "gain_db",
        } <= set(track) <= TRACK_KEYS:
            refuse()
        if (
            not isinstance(track["key"], str)
            or track["key"] not in input_keys
            or not number(track["offset_seconds"], 0, 3600)
            or not number(track["trim_start_seconds"], 0, 3600)
            or not number(track["gain_db"], -60, 12)
            or not (
                track.get("end_seconds") is None
                or (
                    number(track["end_seconds"], 0, 3600)
                    and track["end_seconds"] > track["offset_seconds"]
                )
            )
            or not number(track.get("fade_in_seconds", 0), 0, 30)
            or not number(track.get("fade_out_seconds", 0), 0, 30)
            or not number(track.get("swell_db", 0), -30, 30)
            or not number(track.get("swell_seconds", 0), 0, 60)
        ):
            refuse()
        used.append(track["key"])
    if set(used) != set(input_keys) or len(set(used)) > 16:
        refuse()
    return payload


class MovieCpuPlugin:
    def capabilities(self):
        from outbound_gpu_worker_pool.contracts import (
            CapabilitySchema,
            WorkerCapability,
        )
        from outbound_gpu_worker_pool.plugins import CapabilityManifest

        return CapabilityManifest(
            plugin_id="movie-cpu",
            plugin_version="3",
            capabilities=tuple(
                WorkerCapability(
                    capability_id=capability,
                    plugin_id="movie-cpu",
                    plugin_version="3",
                )
                for capability in CPU_CAPABILITIES
            ),
            schemas=tuple(
                CapabilitySchema(
                    capability_id=capability,
                    contract_version=1,
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["operation", "sources"],
                        "properties": {
                            "operation": {"enum": ["assemble", "frame", "probe"]},
                            "sources": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 16,
                                "items": {"type": "object"},
                            },
                            "audio": {
                                "type": "array",
                                "maxItems": 8,
                                "items": {"type": "object"},
                            },
                        },
                    },
                )
                for capability in CPU_CAPABILITIES
            ),
        )

    def validate(self, lease):
        from outbound_gpu_worker_pool.plugins import (
            PluginRequestRejected,
            ValidatedRequest,
        )

        if (
            lease.capability_id not in CPU_CAPABILITIES
            or lease.contract_version != 1
        ):
            raise PluginRequestRejected("Unsupported CPU capability contract")
        payload = _validated(lease.payload, lease.input_keys)
        return ValidatedRequest(lease.capability_id, 1, payload, None)

    async def health(self):
        from outbound_gpu_worker_pool.plugins import PluginHealth

        ready = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        return PluginHealth(
            ready, "FFmpeg and ffprobe ready" if ready else "Install FFmpeg and ffprobe"
        )

    async def cancel(self, job_id):
        # Subprocesses are bounded; the agent fences/discards cancelled results.
        return False

    async def execute(self, context, request):
        from outbound_gpu_worker_pool.plugins import PluginOutput

        if context.cancel.is_set():
            raise asyncio.CancelledError()
        payload = _validated(request.inputs, tuple(context.input_paths))
        total = 0

        def read(key, limit):
            nonlocal total
            path = context.input_paths[key]
            if path.stat().st_size > limit:
                raise ValueError("Movie input exceeds size limit")
            total += path.stat().st_size
            if total > MAX_CUT_BYTES:
                raise ValueError("Movie inputs exceed 512 MiB")
            return path.read_bytes()

        def process():
            sources = [
                (read(s["key"], MAX_VIDEO_BYTES), SimpleNamespace(**s))
                for s in payload["sources"]
            ]
            operation = payload["operation"]
            if operation == "assemble":
                audio = [
                    (read(t["key"], MAX_AUDIO_BYTES), SimpleNamespace(**t))
                    for t in payload.get("audio", [])
                ]
                return _assemble(sources, audio), "video/mp4", "output.mp4"
            if operation == "frame":
                return _last_frame(sources[0][0]), "image/png", "output.png"
            return (
                json.dumps({"duration_seconds": _duration(sources[0][0])}).encode(),
                "application/json",
                "output.json",
            )

        result, content_type, filename = await asyncio.to_thread(process)
        if context.cancel.is_set():
            raise asyncio.CancelledError()
        output = context.workspace / filename
        output.write_bytes(result)
        return PluginOutput(
            path=output,
            content_type=content_type,
            model_id="ffmpeg-cpu",
            model_version="1",
        )
