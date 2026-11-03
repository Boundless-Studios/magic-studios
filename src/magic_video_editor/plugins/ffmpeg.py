import asyncio
import json
import math
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from magic_video_editor.contracts import (
    SpriteSheetMetadata,
    TerminalVideoError,
    VideoDerivatives,
)


ALPHA_ENCODE_FIXED_SECONDS = 12.0
ALPHA_ENCODE_SECONDS_PER_MEGAPIXEL_SECOND = 2.847
MAX_ALPHA_ENCODE_SECONDS = 240.0
_FIXED_PROCESS_TIMEOUT_SECONDS = 30.0
_CHROMAKEY_FILTER = "chromakey=0x00B140:0.10:0.08"
_FRAME_LAYOUTS = {4: (2, 2), 8: (4, 2), 16: (4, 4), 32: (8, 4)}
_DEFAULT_FRAME_COUNT = 32
_DEFAULT_CELL_SIZE = 512


@dataclass(frozen=True)
class SpriteSheetPack:
    png: bytes
    metadata: SpriteSheetMetadata


def alpha_encode_budget_seconds(pixel_seconds: float | None) -> float:
    if pixel_seconds is None:
        return MAX_ALPHA_ENCODE_SECONDS
    budget = ALPHA_ENCODE_FIXED_SECONDS + (
        pixel_seconds / 1_000_000
    ) * ALPHA_ENCODE_SECONDS_PER_MEGAPIXEL_SECOND
    if budget > MAX_ALPHA_ENCODE_SECONDS:
        raise ValueError(
            "animation clip is too large to package: "
            f"projected alpha encode {budget:.0f}s exceeds the "
            f"{MAX_ALPHA_ENCODE_SECONDS:.0f}s packaging ceiling"
        )
    return budget

# Ported from the reference game's video_lab_animation.py:208 -- keys out the flat
# chroma-green backdrop (#00B140) its own render contract asks providers for.
CHROMAKEY_FILTER = "chromakey=0x00B140:0.10:0.08"
_CHROMAKEY_TIMEOUT_SECONDS = 180
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"


class ChromakeyDerivationFailed(RuntimeError):
    """The chromakey-to-alpha ffmpeg pass could not produce a decodable clip."""


class FrameExtractionFailed(RuntimeError):
    """A video source could not be sampled into individual frame images."""


async def chromakey_video_to_alpha_webm(source: bytes) -> bytes:
    """Key out the green backdrop and re-encode to a VP9 WebM carrying alpha."""

    def render() -> bytes:
        try:
            completed = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", "pipe:0", "-vf",
                    f"{CHROMAKEY_FILTER},format=yuva420p", "-c:v", "libvpx-vp9",
                    "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-f", "webm",
                    "pipe:1",
                ],
                input=source,
                capture_output=True,
                check=True,
                timeout=_CHROMAKEY_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ChromakeyDerivationFailed(
                "chromakey video derivation failed to run"
            ) from exc
        if not completed.stdout.startswith(_WEBM_MAGIC):
            raise ChromakeyDerivationFailed(
                "chromakey video derivation did not produce a decodable WebM"
            )
        return completed.stdout

    return await asyncio.to_thread(render)


_FRAME_EXTRACT_TIMEOUT_SECONDS = 60
_PROBE_TIMEOUT_SECONDS = 15


def _parse_probe_duration(stdout: bytes) -> float:
    """Ported from the reference game's `video_lab_animation._parse_probe`: prefer the stream's
    duration over the container's, since either can print "N/A"."""
    for line in stdout.decode().splitlines():
        value = line.strip()
        if not value or value.upper() == "N/A":
            continue
        try:
            parsed = float(value)
        except ValueError:
            continue
        if parsed > 0:
            return parsed
    raise RuntimeError("ffprobe reported no usable duration")


async def extract_video_frames(source: bytes, frame_count: int) -> list[bytes]:
    """Extract exactly `frame_count` frames from a source video as PNG bytes, sampled
    evenly across its duration (T19; ported reading of the reference game's `_sampling_fps` /
    `_pad_frame_paths`: fps=frame_count/duration, padded by repeating the last
    extracted frame when ffmpeg yields fewer than requested)."""

    def render() -> list[bytes]:
        with tempfile.TemporaryDirectory(prefix="magic-video-frames-") as directory:
            input_path = Path(directory) / "input.mp4"
            input_path.write_bytes(source)
            try:
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=duration:format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(input_path),
                    ],
                    capture_output=True, check=True, timeout=_PROBE_TIMEOUT_SECONDS,
                )
                try:
                    duration = _parse_probe_duration(probe.stdout)
                except RuntimeError:
                    # Streamed alpha WebM has no container duration. Count its
                    # decoded frames instead of rejecting Studio's own output.
                    counted = subprocess.run(
                        ['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                         '-show_entries', 'stream=avg_frame_rate,nb_read_frames', '-of', 'json', str(input_path)],
                        capture_output=True, check=True, timeout=_PROBE_TIMEOUT_SECONDS,
                    )
                    stream = json.loads(counted.stdout)['streams'][0]
                    duration = int(stream['nb_read_frames']) / float(Fraction(stream['avg_frame_rate']))
                    if not math.isfinite(duration) or duration <= 0:
                        raise RuntimeError('video has no usable duration')
                fps = frame_count / duration
                pattern = str(Path(directory) / "frame-%04d.png")
                subprocess.run(
                    [
                        "ffmpeg", "-v", "error", *alpha_video_decoder_args(source), "-i", str(input_path), "-vf", f"fps={fps}",
                        "-frames:v", str(frame_count), pattern,
                    ],
                    check=True, capture_output=True, timeout=_FRAME_EXTRACT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
                raise FrameExtractionFailed(
                    "video frame extraction failed to run"
                ) from exc
            frame_files = sorted(Path(directory).glob("frame-*.png"))
            if not frame_files:
                raise FrameExtractionFailed("video frame extraction produced no frames")
            frames = [path.read_bytes() for path in frame_files]
            if len(frames) < frame_count:
                frames = frames + [frames[-1]] * (frame_count - len(frames))
            return frames[:frame_count]

    return await asyncio.to_thread(render)


def alpha_video_decoder_args(source: bytes) -> list[str]:
    """FFmpeg's native VP8/VP9 decoders discard WebM alpha; libvpx keeps it."""
    if not source.startswith(_WEBM_MAGIC):
        return []
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', 'pipe:0'],
        input=source, capture_output=True, check=True, timeout=_PROBE_TIMEOUT_SECONDS,
    )
    codec = probe.stdout.strip().decode()
    return {'vp9': ['-c:v', 'libvpx-vp9'], 'vp8': ['-c:v', 'libvpx']}.get(codec, [])


_UNPACK_TIMEOUT_SECONDS = 120


class SheetUnpackFailed(RuntimeError):
    """A packed PNG strip could not be rebuilt into a loop clip."""


async def render_sheet_to_webm(
    sheet_png: bytes, *, frame_count: int, frame_duration_ms: int, frame_rate: str | None = None
) -> bytes:
    """Rebuild a loop clip (VP9 WebM, alpha-preserving) from a packed horizontal
    sprite strip at the candidate's own frame timing (T20; ported from the reference
    game's `unpack_sprite_sheet`, whose single-row grid this studio always produces)."""
    return await asyncio.to_thread(
        _unpack_sheet, sheet_png, frame_count, frame_duration_ms, "clip.webm",
        [
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
        ], frame_rate,
    )


async def render_sheet_to_gif(
    sheet_png: bytes, *, frame_count: int, frame_duration_ms: int, frame_rate: str | None = None
) -> bytes:
    """Rebuild a looping GIF from a packed horizontal sprite strip (T20)."""
    return await asyncio.to_thread(
        _unpack_sheet, sheet_png, frame_count, frame_duration_ms, "clip.gif", ["-loop", "0"], frame_rate,
    )


def _unpack_sheet(
    sheet_png: bytes, frame_count: int, frame_duration_ms: int, output_name: str,
    encode_args: list[str], frame_rate: str | None = None,
) -> bytes:
    if frame_count < 1:
        raise SheetUnpackFailed("sheet must describe at least one frame")
    if frame_duration_ms <= 0:
        raise SheetUnpackFailed("frame_duration_ms must be positive")
    try:
        rate = Fraction(frame_rate) if frame_rate is not None else Fraction(1000, frame_duration_ms)
    except (ValueError, ZeroDivisionError) as exc:
        raise SheetUnpackFailed("frame rate must be a positive rational") from exc
    if rate <= 0 or rate > 1000:
        raise SheetUnpackFailed("frame rate must be between zero and 1000")
    fps = f"{rate.numerator}/{rate.denominator}"
    filters = f"untile={frame_count}x1,settb=AVTB,setpts=N/(({fps})*TB)"
    if output_name.endswith('.gif'):
        filters += ',split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse=alpha_threshold=128'
    with tempfile.TemporaryDirectory(prefix="magic-video-unpack-") as directory:
        sheet_path = Path(directory) / "sheet.png"
        out_path = Path(directory) / output_name
        sheet_path.write_bytes(sheet_png)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(sheet_path), "-filter_complex",
                    filters,
                    "-r", fps, "-frames:v", str(frame_count),
                    *encode_args, str(out_path),
                ],
                check=True, capture_output=True, timeout=_UNPACK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SheetUnpackFailed("ffmpeg could not rebuild the clip") from exc
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise SheetUnpackFailed("ffmpeg produced no clip from the sprite sheet")
        return out_path.read_bytes()


class FfmpegVideoProcessor:
    def __init__(self, *, timeout_seconds: float = 240) -> None:
        self._timeout_seconds = timeout_seconds

    async def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg executable is unavailable")

    async def stop(self) -> None:
        # Every subprocess this component spawns is awaited inside the call
        # that spawned it, so there is nothing to release at shutdown. The
        # method exists because the runtime lifecycle stops every started
        # component, and its absence raised AttributeError on every worker
        # shutdown in production (Sentry MAGIC-VIDEO-EDITOR-BACKEND-1).
        return None

    async def prepare_source(self, source: bytes) -> bytes:
        with tempfile.TemporaryDirectory(prefix="magic-video-") as directory:
            input_path = Path(directory) / "input.mp4"
            output_path = Path(directory) / "output.mp4"
            input_path.write_bytes(source)
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(input_path), "-map", "0:v:0",
                "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
                str(output_path), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError("ffmpeg processing deadline exceeded")
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            if process.returncode < 0:
                raise RuntimeError(f"ffmpeg terminated by signal {-process.returncode}")
            if process.returncode > 0:
                detail = stderr.decode(errors="replace")[-500:]
                raise TerminalVideoError(f"ffmpeg rejected the video: {detail}")
            return output_path.read_bytes()

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        alpha_webm = (
            await _build_alpha_video(source)
            if requested_outputs.get("alpha", False)
            else None
        )
        sprite_sheet = (
            await _pack_sprite_sheet(source, _DEFAULT_FRAME_COUNT)
            if requested_outputs.get("sprite", False)
            else None
        )
        return VideoDerivatives(
            alpha_webm=alpha_webm,
            sprite_sheet_png=sprite_sheet.png if sprite_sheet else None,
            sprite_metadata=sprite_sheet.metadata if sprite_sheet else None,
        )


async def _communicate(
    process: asyncio.subprocess.Process,
    message: str,
    *,
    timeout_seconds: float,
) -> bytes | None:
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(f"{message} within the packaging deadline") from None
    if process.returncode != 0:
        detail = (stderr or b"").decode(errors="replace")[-4000:]
        raise RuntimeError(f"{message}: {detail}" if detail else message)
    return stderr


def _vp9_speed_args(cpu_used: int) -> tuple[str, ...]:
    return (
        "-cpu-used",
        str(cpu_used),
        "-row-mt",
        "1",
        "-threads",
        str(min(4, os.cpu_count() or 1)),
    )


async def _build_alpha_video(source_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="magic-video-alpha-") as directory:
        source = Path(directory) / "source.mp4"
        output = Path(directory) / "animation.webm"
        await asyncio.to_thread(source.write_bytes, source_bytes)
        try:
            budget = alpha_encode_budget_seconds(await _probe_video_metrics(source))
        except ValueError as exc:
            raise TerminalVideoError(str(exc)) from exc
        for cpu_used in (4, 8):
            output.unlink(missing_ok=True)
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"{_CHROMAKEY_FILTER},format=yuva420p",
                "-c:v",
                "libvpx-vp9",
                "-auto-alt-ref",
                "0",
                *_vp9_speed_args(cpu_used),
                "-an",
                str(output),
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await _communicate(
                    process,
                    "ffmpeg could not encode the alpha animation",
                    timeout_seconds=budget,
                )
                break
            except RuntimeError as exc:
                if "within the packaging deadline" not in str(exc) or cpu_used == 8:
                    raise
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no alpha animation")
        return await asyncio.to_thread(output.read_bytes)


async def _probe_video_metrics(source: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_FIXED_PROCESS_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("ffprobe could not inspect animation workload") from None
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[-4000:]
        raise TerminalVideoError(
            f"ffprobe could not inspect animation workload: {detail}"
        )
    try:
        payload = json.loads(stdout)
        stream = payload["streams"][0]
        duration = float(stream.get("duration") or payload["format"]["duration"])
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TerminalVideoError(
            "ffprobe reported no usable animation workload"
        ) from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise TerminalVideoError("ffprobe reported no usable animation workload")
    return float(width * height) * duration


async def _probe_duration(source: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration:format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_FIXED_PROCESS_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("ffprobe could not inspect animation duration") from None
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[-4000:]
        raise RuntimeError(f"ffprobe could not inspect animation duration: {detail}")
    for raw_value in stdout.decode().splitlines():
        try:
            duration = float(raw_value.strip())
        except ValueError:
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RuntimeError("ffprobe reported no usable duration")


def _compose_sheet(
    frame_paths: list[Path], columns: int, rows: int
) -> tuple[bytes, int, int]:
    frames = [Image.open(path).convert("RGBA") for path in frame_paths]
    try:
        width, height = frames[0].size
        if any(frame.size != (width, height) for frame in frames):
            raise RuntimeError("animation frames have inconsistent dimensions")
        sheet = Image.new("RGBA", (width * columns, height * rows), (0, 0, 0, 0))
        try:
            for index, frame in enumerate(frames):
                sheet.paste(frame, ((index % columns) * width, (index // columns) * height))
            output = BytesIO()
            sheet.save(output, format="PNG")
            return output.getvalue(), width, height
        finally:
            sheet.close()
    finally:
        for frame in frames:
            frame.close()


async def _pack_sprite_sheet(
    source_bytes: bytes,
    frame_count: int,
) -> SpriteSheetPack:
    columns, rows = _FRAME_LAYOUTS[frame_count]
    with tempfile.TemporaryDirectory(prefix="magic-video-sprites-") as directory:
        root = Path(directory)
        source = root / "source.mp4"
        pattern = root / "frame-%03d.png"
        await asyncio.to_thread(source.write_bytes, source_bytes)
        duration = await _probe_duration(source)
        frame_duration_ms = _frame_duration_ms(duration, frame_count)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                f"fps={frame_count / duration},{_CHROMAKEY_FILTER},"
                f"scale='min({_DEFAULT_CELL_SIZE},iw)':'min({_DEFAULT_CELL_SIZE},ih)':"
                "force_original_aspect_ratio=decrease,format=rgba"
            ),
            "-frames:v",
            str(frame_count),
            str(pattern),
            stderr=asyncio.subprocess.PIPE,
        )
        await _communicate(
            process,
            "ffmpeg could not extract animation frames",
            timeout_seconds=MAX_ALPHA_ENCODE_SECONDS,
        )
        frame_paths = sorted(root.glob("frame-*.png"))
        if not frame_paths:
            raise RuntimeError("animation video contained no usable frames")
        frame_paths.extend([frame_paths[-1]] * (frame_count - len(frame_paths)))
        png, width, height = await asyncio.to_thread(
            _compose_sheet, frame_paths[:frame_count], columns, rows
        )
        return SpriteSheetPack(
            png=png,
            metadata=SpriteSheetMetadata(
                frame_count=frame_count,
                grid_columns=columns,
                grid_rows=rows,
                frame_width=width,
                frame_height=height,
                frame_duration_ms=frame_duration_ms,
            ),
        )


def _frame_duration_ms(duration_seconds: float, frame_count: int) -> int:
    return max(1, round(duration_seconds * 1000 / frame_count))
