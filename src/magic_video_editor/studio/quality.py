"""Static quality checks for uploaded subject PNGs."""

import json
import subprocess
import tempfile
import warnings
from dataclasses import asdict, dataclass
from hashlib import sha256
from fractions import Fraction
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    FindingSeverity,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionState,
    QualityAssessment,
    QualityFinding,
    SubjectAssessment,
    SubjectFinding,
    SubjectFindingCode,
)

MOTION_POLICY_V1 = "motion-policy-v1"
SET_POLICY_V1 = "set-policy-v1"
MAX_MOTION_DIMENSION = 8192
MAX_MOTION_FRAMES = 2000
# Must cover the largest clip the studio can request from the pool (1344x1344 at 161
# frames, see _DIMENSION_BOUNDS/_LENGTH_BOUNDS in service.py); the budget only bounds
# ffprobe's -count_frames pass -- the probe hashes two frames, never the whole clip.
MAX_MOTION_DECODED_PIXELS = 320_000_000
MAX_PROBE_OUTPUT_BYTES = 65_536


def _positive_fraction(value: object) -> str | None:
    try:
        fraction = Fraction(str(value))
        if fraction <= 0:
            return None
        return f"{fraction.numerator}/{fraction.denominator}"
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def inspect_ffprobe_metadata(
    probe: dict[str, object], *, frame_hashes: tuple[str, ...] = ()
) -> MotionFrameMetadata:
    """Normalize bounded ffprobe JSON into provider-neutral quality metadata."""
    streams = probe.get("streams")
    frames = probe.get("frames", [])
    if not isinstance(streams, list) or not streams or not isinstance(frames, list):
        raise ValueError("ffprobe output must contain a video stream")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise TypeError("ffprobe video stream is invalid")
    width = stream.get("width")
    height = stream.get("height")
    pixel_format = stream.get("pix_fmt", "")
    tags = stream.get("tags")
    if (
        not isinstance(width, int)
        or width < 1
        or not isinstance(height, int)
        or height < 1
    ):
        raise ValueError("ffprobe video geometry is invalid")
    frame_count_value = stream.get("nb_read_frames", len(frames))
    try:
        frame_count = int(frame_count_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ffprobe frame count is invalid") from exc
    geometries: set[tuple[int, int]] = set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise TypeError("ffprobe frame is invalid")
        frame_width = frame.get("width", width)
        frame_height = frame.get("height", height)
        if not isinstance(frame_width, int) or not isinstance(frame_height, int):
            raise TypeError("ffprobe frame geometry is invalid")
        geometries.add((frame_width, frame_height))
    frame_rate = _positive_fraction(stream.get("avg_frame_rate")) or _positive_fraction(stream.get("r_frame_rate"))
    time_base = _positive_fraction(stream.get("time_base"))
    duration_ms = None
    if frame_rate is not None and frame_count > 0:
        duration_ms = float(Fraction(frame_count * 1000, 1) / Fraction(frame_rate))
    _require_motion_decode_budget(width, height, frame_count)
    return MotionFrameMetadata(
        decodable=True,
        media_kind=MediaKind.VIDEO,
        frame_rate=frame_rate,
        time_base=time_base,
        duration_ms=duration_ms,
        frame_count=frame_count,
        width=width,
        height=height,
        constant_geometry=len(geometries) <= 1,
        has_alpha=_pixel_format_has_alpha(pixel_format) or _stream_tags_have_alpha(tags),
        duplicate_terminal_frame=(
            len(frame_hashes) > 1 and frame_hashes[0] == frame_hashes[-1]
        )
        or _ffprobe_has_duplicate_terminal_frame(frames),
        packing_estimate=ExportPackingEstimate(
            width=width * max(frame_count, 1), height=height
        ),
    )


def inspect_sprite_png(
    png_bytes: bytes, declared_frame_count: int | None = None
) -> MotionFrameMetadata:
    """Extract bounded frame facts from an animated/sprite PNG with Pillow.

    `declared_frame_count` overrides the PNG's own embedded `studio_frame_count` text
    chunk (T21: an ingested strip from another tool -- Ludo, AutoSprite, an artist's
    export -- carries no such chunk, so the caller supplies the count it was told).
    """
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            if image.format != "PNG":
                raise ValueError("sprite artifact must be PNG")
            encoded_frame_count = getattr(image, "n_frames", 1)
            declared_frame_count = (
                declared_frame_count
                if declared_frame_count is not None
                else image.info.get("studio_frame_count")
            )
            static_sheet = encoded_frame_count == 1 and declared_frame_count is not None
            if static_sheet:
                try:
                    frame_count = int(declared_frame_count)
                except (TypeError, ValueError) as exc:
                    raise ValueError("sprite frame count is invalid") from exc
            else:
                frame_count = encoded_frame_count
            width, height = image.size
            if static_sheet:
                if frame_count < 1 or frame_count > MAX_MOTION_FRAMES:
                    raise ValueError("sprite frame count exceeds measurement limits")
                if width % frame_count != 0:
                    raise ValueError("sprite width must divide evenly into declared frames")
            _require_motion_decode_budget(
                width // frame_count if static_sheet else width, height, frame_count
            )
            geometries: set[tuple[int, int]] = set()
            first_hash: bytes | None = None
            last_hash: bytes | None = None
            has_alpha = False
            for index in range(frame_count):
                if static_sheet:
                    frame_width = width // frame_count
                    frame = image.crop(
                        (index * frame_width, 0, (index + 1) * frame_width, height)
                    )
                else:
                    image.seek(index)
                    frame = image
                geometries.add(frame.size)
                has_alpha = (
                    has_alpha or "A" in frame.getbands() or "transparency" in image.info
                )
                frame_hash = sha256(frame.convert("RGBA").tobytes()).digest()
                if index == 0:
                    first_hash = frame_hash
                last_hash = frame_hash
            return MotionFrameMetadata(
                decodable=True,
                media_kind=MediaKind.SHEET,
                frame_count=frame_count,
                width=width,
                height=height,
                constant_geometry=len(geometries) == 1,
                has_alpha=has_alpha,
                duplicate_terminal_frame=(frame_count > 1 and first_hash == last_hash),
                pivot_x=_read_png_pivot(
                    image.info, "studio_pivot_x",
                    width // frame_count if static_sheet else width,
                ),
                pivot_y=_read_png_pivot(image.info, "studio_pivot_y", height),
                packing_estimate=ExportPackingEstimate(
                    width=width if static_sheet else width * frame_count, height=height
                ),
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("sprite artifact is not a decodable PNG") from exc


def inspect_motion_artifact(content: bytes) -> MotionFrameMetadata:
    """Measure a bounded encoded artifact using Pillow or local ffprobe."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return inspect_sprite_png(content)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".video", delete=False) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        probe_output = _run_bounded_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=width,height,pix_fmt,nb_read_frames,avg_frame_rate,r_frame_rate,time_base:stream_tags=alpha_mode",
                "-of",
                "json",
                temporary_name,
            ],
        )
        decoded = json.loads(probe_output)
        if not isinstance(decoded, dict):
            raise TypeError("ffprobe output is invalid")
        metadata = inspect_ffprobe_metadata(decoded)
        hashes = _ffmpeg_terminal_frame_hashes(temporary_name, metadata.frame_count)
        return metadata.model_copy(
            update={
                "duplicate_terminal_frame": (
                    len(hashes) > 1 and hashes[0] == hashes[-1]
                )
            }
        )
    except MotionMeasurementBudgetExceeded:
        raise
    except (
        OSError,
        TypeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise ValueError("motion artifact is not decodable") from exc
    finally:
        if temporary_name is not None:
            from pathlib import Path

            Path(temporary_name).unlink(missing_ok=True)


def _ffmpeg_terminal_frame_hashes(path: str, frame_count: int) -> tuple[str, ...]:
    if frame_count < 2:
        return ()
    return tuple(
        _ffmpeg_selected_frame_hash(path, frame_index)
        for frame_index in (0, frame_count - 1)
    )


def _ffmpeg_selected_frame_hash(path: str, frame_index: int) -> str:
    output = _run_bounded_subprocess(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-f",
            "framemd5",
            "-",
        ],
    )
    for line in output.decode().splitlines():
        if not line.startswith("#") and line.strip():
            return line.rsplit(",", maxsplit=1)[-1].strip()
    raise ValueError("selected frame could not be hashed")


def _run_bounded_subprocess(command: list[str]) -> bytes:
    """Run a media probe without retaining unbounded child output in memory."""
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            command,
            stdout=output,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=15,
        )
        size = output.tell()
        if size > MAX_PROBE_OUTPUT_BYTES:
            raise ValueError("media probe output exceeds measurement limits")
        output.seek(0)
        return output.read(MAX_PROBE_OUTPUT_BYTES + 1)


def assess_motion(
    candidate: MotionCandidate, loop_policy: LoopPolicy
) -> QualityAssessment:
    metadata = candidate.metadata
    findings: list[QualityFinding] = []

    def warn(code: str, message: str, *, action_id: str | None = None) -> None:
        findings.append(
            QualityFinding(
                code=code, severity=FindingSeverity.WARNING, message=message,
                action_id=action_id,
            )
        )

    if metadata is None or not metadata.decodable:
        warn("motion_not_decodable", "Motion output must be decodable.")
        return QualityAssessment(
            policy_version=MOTION_POLICY_V1, findings=tuple(findings)
        )
    if metadata.frame_count == 0:
        warn("motion_no_frames", "Motion output must contain frames.")
    elif metadata.frame_count == 1:
        warn(
            "motion_single_frame_preview",
            "Single-frame motion should be reviewed before release.",
        )
    if not metadata.constant_geometry:
        warn("motion_geometry_changes", "Every frame must use one canvas geometry.")
    if not metadata.has_alpha:
        warn(
            "motion_missing_alpha", "Sprite output must preserve transparency.",
            action_id="derive_alpha",
        )
    if loop_policy is LoopPolicy.LOOP and metadata.duplicate_terminal_frame:
        warn(
            "motion_duplicate_terminal_loop_frame",
            "Looping motion must not duplicate its first frame at the end.",
        )
    elif loop_policy is LoopPolicy.ONCE:
        pass  # One-shot playback has no seam requirement.
    elif loop_policy is LoopPolicy.PING_PONG and metadata.duplicate_terminal_frame:
        warn(
            "motion_duplicate_ping_pong_terminal_frame",
            "Ping-pong playback must not repeat its turn-around frame.",
        )
    return QualityAssessment(policy_version=MOTION_POLICY_V1, findings=tuple(findings))


_ALPHA_PIXEL_FORMATS = frozenset(
    {"rgba", "argb", "bgra", "abgr", "ya8", "ya16le", "ya16be"}
)


def _pixel_format_has_alpha(pixel_format: object) -> bool:
    if not isinstance(pixel_format, str):
        return False
    normalized = pixel_format.lower()
    return normalized in _ALPHA_PIXEL_FORMATS or normalized.startswith(
        ("yuva", "gbrap")
    )


def _stream_tags_have_alpha(tags: object) -> bool:
    """VP9 (and other codecs with a side-channel alpha plane) keep `pix_fmt` at its
    opaque value regardless -- ffprobe surfaces the real signal as the container's
    `alpha_mode` stream tag instead (ported reading of the reference game's own VP9 alpha clips)."""
    if not isinstance(tags, dict):
        return False
    return str(tags.get("alpha_mode", "")) == "1"


def _ffprobe_has_duplicate_terminal_frame(frames: list[object]) -> bool:
    if (
        len(frames) < 2
        or not isinstance(frames[0], dict)
        or not isinstance(frames[-1], dict)
    ):
        return False
    for key in ("frame_hash", "pkt_hash"):
        first_hash = frames[0].get(key)
        last_hash = frames[-1].get(key)
        if isinstance(first_hash, str) and first_hash and first_hash == last_hash:
            return True
    return False


class MotionMeasurementBudgetExceeded(ValueError):
    """Raised when a motion artifact's geometry or frame count exceeds the bounded
    measurement budget -- distinct from a genuine decode failure."""


def _require_motion_decode_budget(width: int, height: int, frame_count: int) -> None:
    if width > MAX_MOTION_DIMENSION or height > MAX_MOTION_DIMENSION:
        raise MotionMeasurementBudgetExceeded(
            "motion dimensions exceed measurement limits"
        )
    if frame_count > MAX_MOTION_FRAMES:
        raise MotionMeasurementBudgetExceeded(
            "motion frame count exceeds measurement limits"
        )
    if width * height * max(frame_count, 1) > MAX_MOTION_DECODED_PIXELS:
        raise MotionMeasurementBudgetExceeded(
            "motion decoded pixels exceed measurement limits"
        )


def assess_set(
    motions: tuple[MotionState, ...],
    *,
    max_texture_dimension: int = 4096,
    current_subject_revision_id: str | None = None,
) -> QualityAssessment:
    findings: list[QualityFinding] = []

    def warn(code: str, message: str) -> None:
        findings.append(
            QualityFinding(
                code=code, severity=FindingSeverity.WARNING, message=message
            )
        )

    selected = []
    slots: set[tuple[object, str]] = set()
    for motion in motions:
        candidate = motion.selected_candidate
        if motion.required and candidate is None:
            warn(
                "set_required_selection_missing",
                f"{motion.specification.name} needs a selected candidate.",
            )
        if (
            motion.required
            and candidate is not None
            and current_subject_revision_id is not None
            and candidate.subject_revision_id is not None
            and candidate.subject_revision_id != current_subject_revision_id
        ):
            warn(
                "motion_stale_subject",
                f"{motion.specification.name} is selected from a take made for "
                "an earlier subject; regenerate it or choose that subject.",
            )
        if motion.specification.view_id is not None:
            if motion.specification.view_reference is None:
                warn("motion_view_reference_missing", f"{motion.specification.name} needs a supplied view reference.")
            elif candidate is not None:
                digest = sha256(motion.specification.model_dump_json(exclude_none=True).encode()).hexdigest()
                if candidate.motion_spec_digest != digest:
                    warn("motion_stale_view_reference", f"{motion.specification.name} is selected from an earlier view/action specification.")
        if candidate is not None and candidate.metadata is not None:
            selected.append(candidate.metadata)
        if candidate is not None:
            findings.extend(
                assess_motion(candidate, motion.specification.loop_policy).findings
            )
        slot = motion.specification.semantic_slot
        if slot:
            scope = (motion.specification.view_id, slot)
            if scope in slots:
                warn(
                    "set_semantic_slot_duplicate",
                    f"Semantic slot {slot!r} is used more than once.",
                )
            slots.add(scope)
    canvases = {
        (
            item.width // max(1, item.frame_count)
            if item.media_kind is MediaKind.SHEET and item.packing_estimate.width == item.width
            else item.width,
            item.height,
        )
        for item in selected
    }
    if len(canvases) > 1:
        warn("set_canvas_mismatch", "Frame canvases differ; use exported pivots or a common-canvas export.")
    baselines = {item.baseline_y for item in selected if item.baseline_y is not None}
    if len(baselines) > 1:
        warn(
            "set_baseline_mismatch",
            "Selected motions must share a baseline when supplied.",
        )
    if any(
        max(item.packing_estimate.width, item.packing_estimate.height)
        > max_texture_dimension
        for item in selected
    ):
        warn(
            "set_texture_limit_exceeded",
            "A selected motion exceeds the export texture limit.",
        )
    return QualityAssessment(policy_version=SET_POLICY_V1, findings=tuple(findings))


@dataclass(frozen=True)
class SubjectPolicy:
    version: str
    min_dimension_px: int
    min_occupancy_ratio: float
    max_occupancy_ratio: float
    max_dimension_px: int
    max_pixels: int
    max_bytes: int

    def canonical_key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


SUBJECT_POLICY_V1 = SubjectPolicy(
    version="subject-policy-v1",
    min_dimension_px=512,
    min_occupancy_ratio=0.35,
    max_occupancy_ratio=0.92,
    max_dimension_px=4_096,
    max_pixels=4_096 * 4_096,
    max_bytes=10_000_000,
)


class SubjectImageValidationError(ValueError):
    """Raised when an uploaded subject image cannot be assessed safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def assess_subject_png(
    png_bytes: bytes,
    policy: SubjectPolicy = SUBJECT_POLICY_V1,
) -> SubjectAssessment:
    _require_byte_budget(png_bytes, policy)
    artifact_sha256 = sha256(png_bytes).hexdigest()
    width, height, has_alpha, occupancy_ratio, bounding_box = _inspect_png(
        png_bytes,
        policy,
    )
    findings: list[SubjectFinding] = []
    assessment_id = _assessment_id(policy, artifact_sha256)

    if width < policy.min_dimension_px or height < policy.min_dimension_px:
        findings.append(
            _finding(
                assessment_id,
                SubjectFindingCode.SUBJECT_DIMENSION_TOO_SMALL,
                FindingSeverity.WARNING,
                "Canvas Too Small",
                (
                    f"Subject PNG must be at least {policy.min_dimension_px}px on both axes; "
                    f"received {width}x{height}."
                ),
                artifact_sha256,
            )
        )

    if not has_alpha:
        findings.append(
            _finding(
                assessment_id,
                SubjectFindingCode.SUBJECT_MISSING_ALPHA,
                FindingSeverity.WARNING,
                "Missing Transparency",
                "Subject PNG must include an alpha channel so the subject can be isolated.",
                artifact_sha256,
            )
        )

    if bounding_box is not None and _touches_canvas_edge(bounding_box, width, height):
        findings.append(
            _finding(
                assessment_id,
                SubjectFindingCode.SUBJECT_TOUCHES_CANVAS_EDGE,
                FindingSeverity.WARNING,
                "Subject Touches Edge",
                "Opaque subject pixels must not touch the outer canvas edge.",
                artifact_sha256,
            )
        )

    if occupancy_ratio < policy.min_occupancy_ratio:
        findings.append(
            _finding(
                assessment_id,
                SubjectFindingCode.SUBJECT_OCCUPANCY_TOO_LOW,
                FindingSeverity.WARNING,
                "Subject Occupancy Too Low",
                "Opaque subject pixels occupy too little of the canvas for the current framing.",
                artifact_sha256,
            )
        )
    if occupancy_ratio > policy.max_occupancy_ratio:
        findings.append(
            _finding(
                assessment_id,
                SubjectFindingCode.SUBJECT_OCCUPANCY_TOO_HIGH,
                FindingSeverity.WARNING,
                "Subject Occupancy Too High",
                "Opaque subject pixels occupy too much of the canvas for the current framing.",
                artifact_sha256,
            )
        )

    return SubjectAssessment(
        assessment_id=assessment_id,
        policy_version=policy.version,
        artifact_sha256=artifact_sha256,
        width=width,
        height=height,
        occupancy_ratio=occupancy_ratio,
        findings=tuple(findings),
    )


def require_rgba_png(
    png_bytes: bytes,
    policy: SubjectPolicy = SUBJECT_POLICY_V1,
) -> tuple[int, int]:
    """Decode `png_bytes` as a canonical RGBA PNG bounded by `policy`'s byte
    and dimension budgets, raising `SubjectImageValidationError` otherwise.
    Reuses the same decode/format/dimension guards as `assess_subject_png`
    (T3): a composed still is not a subject and carries no occupancy
    semantics, so it stops short of full subject assessment.
    """
    _require_byte_budget(png_bytes, policy)
    width, height, has_alpha, _occupancy_ratio, _bounding_box = _inspect_png(
        png_bytes, policy
    )
    if not has_alpha:
        raise SubjectImageValidationError(
            "composition_png_missing_alpha",
            "composed still must be an RGBA PNG with an alpha channel",
        )
    return width, height


def _require_byte_budget(png_bytes: bytes, policy: SubjectPolicy) -> None:
    if len(png_bytes) > policy.max_bytes:
        raise SubjectImageValidationError(
            "subject_png_bytes_too_large",
            f"subject upload exceeds the {policy.max_bytes} byte limit",
        )


def _inspect_png(
    png_bytes: bytes,
    policy: SubjectPolicy,
) -> tuple[int, int, bool, float, tuple[int, int, int, int] | None]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(png_bytes))
        with image:
            if image.format != "PNG":
                raise SubjectImageValidationError(
                    "subject_png_format_invalid",
                    "subject upload must be a PNG image",
                )
            width, height = image.size
            if width > policy.max_dimension_px or height > policy.max_dimension_px:
                raise SubjectImageValidationError(
                    "subject_png_dimensions_too_large",
                    (
                        "subject upload exceeds the maximum decoded dimensions of "
                        f"{policy.max_dimension_px}px"
                    ),
                )
            if width * height > policy.max_pixels:
                raise SubjectImageValidationError(
                    "subject_png_pixels_too_large",
                    (
                        "subject upload exceeds the maximum decoded pixel budget of "
                        f"{policy.max_pixels}"
                    ),
                )
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            rgba = _load_rgba(image)
            alpha = rgba.getchannel("A")
            histogram = alpha.histogram()
            opaque_pixels = sum(histogram[1:])
            occupancy_ratio = opaque_pixels / float(width * height)
            bounding_box = alpha.getbbox()
            return width, height, has_alpha, occupancy_ratio, bounding_box
    except SubjectImageValidationError:
        raise
    except Image.DecompressionBombError as exc:
        raise SubjectImageValidationError(
            "subject_png_decompression_bomb",
            "subject upload exceeds Pillow decompression safety limits",
        ) from exc
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise SubjectImageValidationError(
            "subject_png_decode_failed",
            "subject upload must be a decodable PNG",
        ) from exc


def _load_rgba(image: Image.Image) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image.load()
        return image.convert("RGBA")
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise SubjectImageValidationError(
            "subject_png_decompression_bomb",
            "subject upload exceeds Pillow decompression safety limits",
        ) from exc


def _assessment_id(policy: SubjectPolicy, artifact_sha256: str) -> str:
    return sha256(f"{artifact_sha256}:{policy.canonical_key()}".encode()).hexdigest()


def _finding(
    assessment_id: str,
    code: SubjectFindingCode,
    severity: FindingSeverity,
    title: str,
    explanation: str,
    artifact_sha256: str,
) -> SubjectFinding:
    return SubjectFinding(
        code=code,
        severity=severity,
        title=title,
        explanation=explanation,
        artifact_sha256=artifact_sha256,
        action_id=sha256(f"{assessment_id}:{code.value}".encode()).hexdigest(),
    )


def _touches_canvas_edge(
    bounding_box: tuple[int, int, int, int],
    width: int,
    height: int,
) -> bool:
    min_x, min_y, max_x, max_y = bounding_box
    return min_x == 0 or min_y == 0 or max_x == width or max_y == height


def _read_png_pivot(info: dict, key: str, extent: int) -> int | None:
    value = info.get(key)
    if value is None:
        return None
    try:
        pivot = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("sprite pivot is invalid") from exc
    if not 0 <= pivot <= extent:
        raise ValueError("sprite pivot is outside frame bounds")
    return pivot
