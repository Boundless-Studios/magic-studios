import warnings
from io import BytesIO

import pytest
from PIL import Image

from magic_video_editor.studio.models import (
    FindingSeverity,
    QualityFinding,
    SubjectFinding,
)
from magic_video_editor.studio.quality import (
    SUBJECT_POLICY_V1,
    SubjectFindingCode,
    SubjectImageValidationError,
    SubjectPolicy,
    assess_subject_png,
)


def test_old_stored_blocking_severity_loads_and_renders_as_a_warning() -> None:
    """Quality findings are warnings, never blockers (operator decision,
    2026-09-05). A manifest persisted before this change may still carry
    `severity: "blocking"`; it must still load, and render as a warning."""
    subject_finding = SubjectFinding.model_validate(
        {
            "code": SubjectFindingCode.SUBJECT_TOUCHES_CANVAS_EDGE.value,
            "severity": "blocking",
            "title": "Subject Touches Edge",
            "explanation": "Opaque subject pixels must not touch the outer canvas edge.",
            "artifact_sha256": "a" * 64,
        }
    )
    assert subject_finding.severity is FindingSeverity.WARNING

    quality_finding = QualityFinding.model_validate(
        {
            "code": "motion_stale_subject",
            "severity": "blocking",
            "message": "stale selection",
        }
    )
    assert quality_finding.severity is FindingSeverity.WARNING


def test_assess_subject_png_is_deterministic_and_returns_typed_warning_findings() -> None:
    png_bytes = _png_bytes(
        size=(1024, 1024),
        subject_box=(412, 412, 612, 612),
        mode="RGBA",
    )

    first = assess_subject_png(png_bytes, policy=SUBJECT_POLICY_V1)
    second = assess_subject_png(png_bytes, policy=SUBJECT_POLICY_V1)

    assert first.assessment_id == second.assessment_id
    assert first.assessment_id
    assert first.policy_version == SUBJECT_POLICY_V1.version
    assert first.findings[0].code is SubjectFindingCode.SUBJECT_OCCUPANCY_TOO_LOW
    assert first.findings[0].severity is FindingSeverity.WARNING
    assert first.findings[0].artifact_sha256 == first.artifact_sha256
    assert first.findings[0].title
    assert first.findings[0].explanation
    assert first.findings[0].action_id == second.findings[0].action_id


def test_assess_subject_png_flags_small_rgb_images_touching_the_canvas_edge() -> None:
    png_bytes = _png_bytes(
        size=(256, 256),
        subject_box=(0, 0, 255, 255),
        mode="RGB",
    )

    assessment = assess_subject_png(png_bytes, policy=SUBJECT_POLICY_V1)

    assert {finding.code for finding in assessment.findings} >= {
        SubjectFindingCode.SUBJECT_DIMENSION_TOO_SMALL,
        SubjectFindingCode.SUBJECT_MISSING_ALPHA,
        SubjectFindingCode.SUBJECT_TOUCHES_CANVAS_EDGE,
    }
    # Quality findings are warnings, never blockers (operator decision, 2026-09-05).
    assert all(
        finding.severity is FindingSeverity.WARNING for finding in assessment.findings
    )


def test_assess_subject_png_warns_when_the_subject_fills_too_much_of_the_canvas() -> None:
    png_bytes = _png_bytes(
        size=(1024, 1024),
        subject_box=(12, 12, 1012, 1012),
        mode="RGBA",
    )

    assessment = assess_subject_png(png_bytes, policy=SUBJECT_POLICY_V1)

    assert [finding.code for finding in assessment.findings] == [
        SubjectFindingCode.SUBJECT_OCCUPANCY_TOO_HIGH
    ]
    assert assessment.width == 1024
    assert assessment.height == 1024


def test_assessment_identity_changes_when_policy_parameters_change() -> None:
    png_bytes = _png_bytes(
        size=(1024, 1024),
        subject_box=(200, 200, 824, 824),
        mode="RGBA",
    )
    stricter = SubjectPolicy(
        version=SUBJECT_POLICY_V1.version,
        min_dimension_px=SUBJECT_POLICY_V1.min_dimension_px,
        min_occupancy_ratio=0.40,
        max_occupancy_ratio=SUBJECT_POLICY_V1.max_occupancy_ratio,
        max_dimension_px=SUBJECT_POLICY_V1.max_dimension_px,
        max_pixels=SUBJECT_POLICY_V1.max_pixels,
        max_bytes=SUBJECT_POLICY_V1.max_bytes,
    )

    baseline = assess_subject_png(png_bytes, policy=SUBJECT_POLICY_V1)
    changed = assess_subject_png(png_bytes, policy=stricter)

    assert baseline.assessment_id != changed.assessment_id


def test_assess_subject_png_rejects_non_png_bytes() -> None:
    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(_jpeg_bytes(size=(1024, 1024)), policy=SUBJECT_POLICY_V1)

    assert exc_info.value.code == "subject_png_format_invalid"


def test_assess_subject_png_rejects_oversize_payload_bytes() -> None:
    png_bytes = _png_bytes(
        size=(512, 512),
        subject_box=(64, 64, 448, 448),
        mode="RGBA",
    )
    tiny_budget = SubjectPolicy(
        version="bytes-test",
        min_dimension_px=512,
        min_occupancy_ratio=0.35,
        max_occupancy_ratio=0.92,
        max_dimension_px=4096,
        max_pixels=4096 * 4096,
        max_bytes=len(png_bytes) - 1,
    )

    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(png_bytes, policy=tiny_budget)

    assert exc_info.value.code == "subject_png_bytes_too_large"


def test_assess_subject_png_does_not_hash_before_byte_budget(monkeypatch) -> None:
    png_bytes = _png_bytes(
        size=(512, 512),
        subject_box=(64, 64, 448, 448),
        mode="RGBA",
    )
    tiny_budget = SubjectPolicy(
        version="bytes-test",
        min_dimension_px=512,
        min_occupancy_ratio=0.35,
        max_occupancy_ratio=0.92,
        max_dimension_px=4096,
        max_pixels=4096 * 4096,
        max_bytes=len(png_bytes) - 1,
    )

    def fail_hash(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("hashing should not happen before byte budget passes")

    monkeypatch.setattr("magic_video_editor.studio.quality.sha256", fail_hash)

    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(png_bytes, policy=tiny_budget)

    assert exc_info.value.code == "subject_png_bytes_too_large"


def test_assess_subject_png_rejects_oversize_dimensions_before_decode() -> None:
    png_bytes = _png_bytes(
        size=(1024, 1024),
        subject_box=(64, 64, 960, 960),
        mode="RGBA",
    )
    bounded = SubjectPolicy(
        version="dimensions-test",
        min_dimension_px=512,
        min_occupancy_ratio=0.35,
        max_occupancy_ratio=0.92,
        max_dimension_px=1000,
        max_pixels=2_000_000,
        max_bytes=10_000_000,
    )

    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(png_bytes, policy=bounded)

    assert exc_info.value.code == "subject_png_dimensions_too_large"


def test_assess_subject_png_rejects_decompression_bomb_warnings(monkeypatch) -> None:
    bomb = _BombImage(warn=True)
    monkeypatch.setattr("magic_video_editor.studio.quality.Image.open", lambda _stream: bomb)

    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(b"png-bytes", policy=SUBJECT_POLICY_V1)

    assert exc_info.value.code == "subject_png_decompression_bomb"


def test_assess_subject_png_rejects_decompression_bomb_errors(monkeypatch) -> None:
    bomb = _BombImage(error=True)
    monkeypatch.setattr("magic_video_editor.studio.quality.Image.open", lambda _stream: bomb)

    with pytest.raises(SubjectImageValidationError) as exc_info:
        assess_subject_png(b"png-bytes", policy=SUBJECT_POLICY_V1)

    assert exc_info.value.code == "subject_png_decompression_bomb"


def _png_bytes(
    *,
    size: tuple[int, int],
    subject_box: tuple[int, int, int, int],
    mode: str,
) -> bytes:
    background = (0, 0, 0, 0) if mode == "RGBA" else (255, 255, 255)
    foreground = (255, 64, 64, 255) if mode == "RGBA" else (255, 64, 64)
    image = Image.new(mode, size, background)
    overlay = Image.new(mode, size, background)
    subject = Image.new(
        mode,
        (subject_box[2] - subject_box[0], subject_box[3] - subject_box[1]),
        foreground,
    )
    overlay.paste(subject, (subject_box[0], subject_box[1]))
    image.alpha_composite(overlay) if mode == "RGBA" else image.paste(overlay)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(*, size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (255, 64, 64))
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class _BombImage:
    format = "PNG"
    size = (1024, 1024)
    info: dict[str, str] = {}

    def __init__(self, *, warn: bool = False, error: bool = False) -> None:
        self._warn = warn
        self._error = error

    def __enter__(self) -> "_BombImage":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def load(self) -> None:
        if self._warn:
            warnings.warn("bomb", Image.DecompressionBombWarning)
        if self._error:
            raise Image.DecompressionBombError("bomb")

    def getbands(self) -> tuple[str, ...]:
        return ("R", "G", "B", "A")

    def convert(self, _mode: str) -> "_BombImage":
        return self

    def getchannel(self, _channel: str) -> "_AlphaChannel":
        return _AlphaChannel()

    def getbbox(self) -> tuple[int, int, int, int]:
        return (100, 100, 924, 924)


class _AlphaChannel:
    def histogram(self) -> list[int]:
        total_pixels = 1024 * 1024
        opaque = 824 * 824
        return [total_pixels - opaque] + [0] * 254 + [opaque]
